"""Final compliance validation of the answer draft (§7.7).

Only content that passes every check is released:
  1. citation validity      2. current source permissions
  3. sensitive-content scan 4. grounding check
  5. templated refusals     6. rendering with renumbered citations
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.agents.formatting import neutralize
from app.core.errors import AppError
from app.core.logging import get_logger
from app.graph.state import OutcomeStatus, TaskOutcome
from app.prompts import UNTRUSTED_CONTENT_CLAUSE, PromptLibrary
from app.schemas.answer import CLAIM_KINDS, AnswerDraft, AnswerSegment, SegmentKind
from app.schemas.api import Citation, TaskResult, TaskStatus
from app.schemas.common import DataCategory, SourceKind
from app.schemas.evidence import Evidence, EvidenceKind
from app.schemas.interfaces import LLMProvider, PolicyService, SensitivityDetector
from app.schemas.llm import ChatMessage, ModelRole
from app.schemas.planning import Task
from app.schemas.security import DenialReason, UserContext

logger = get_logger(__name__)

REDACTED = "[redacted]"
# Categories visible to every employee of the tenant. Spans in these categories are
# released when they appear verbatim in authorized evidence; personal and confidential
# categories must also have been allowed for the task itself.
ALWAYS_DISCLOSABLE: frozenset[DataCategory] = frozenset(
    {DataCategory.GENERAL_KNOWLEDGE, DataCategory.OPERATIONAL, DataCategory.EMPLOYEE_DIRECTORY}
)
MAX_CITATION_EXCERPT = 400

REFUSALS: dict[DenialReason, str] = {
    DenialReason.OTHER_EMPLOYEE_PERSONAL_DATA: (
        "I can't help with that part — it asks for another employee's personal information, "
        "which you're not authorized to access."
    ),
    DenialReason.CONFIDENTIAL_BUSINESS: (
        "I can't share that — it involves confidential company information outside your access."
    ),
    DenialReason.INSUFFICIENT_CLEARANCE: (
        "I can't help with that part — it requires access you don't currently have."
    ),
    DenialReason.CROSS_TENANT: (
        "I can't help with that part — it requires access you don't currently have."
    ),
    DenialReason.AGGREGATION_TOO_SMALL: (
        "I can't provide that figure — it could reveal information about individual employees."
    ),
    DenialReason.UNSUPPORTED_OPERATION: "I can't help with that type of request.",
}
DEFAULT_REFUSAL = "I can't help with that part of your request."
OUT_OF_SCOPE_MESSAGE = (
    "I can only help with questions about company knowledge, systems and processes."
)
UNVERIFIED_GAP = (
    "Some details couldn't be verified against the available sources, so I left them out."
)
NO_ANSWER_GAP = "I couldn't find enough information in the available sources to answer this."
DEPENDENCY_GAP = (
    "I couldn't answer this because an earlier part of your question couldn't be answered."
)
FAILED_MESSAGE = "I couldn't complete this part right now. Please try again."
GROUNDING_UNAVAILABLE = "Automatic grounding check unavailable; citations were still verified."


class GroundingVerdict(BaseModel):
    claim_id: str
    supported: bool


class GroundingVerdicts(BaseModel):
    verdicts: list[GroundingVerdict]


@dataclass
class ValidatedAnswer:
    answer_markdown: str
    tasks: list[TaskResult]
    citations: list[Citation]
    warnings: list[str] = field(default_factory=list)


@dataclass
class _WorkingTask:
    task: Task
    outcome: TaskOutcome
    allowed: dict[str, Evidence]  # evidence this task may cite
    segments: list[AnswerSegment]
    dropped: bool = False


class ResponseValidator:
    def __init__(
        self,
        llm: LLMProvider,
        policy: PolicyService,
        sensitivity: SensitivityDetector,
        prompts: PromptLibrary,
    ) -> None:
        self._llm = llm
        self._policy = policy
        self._sensitivity = sensitivity
        self._prompts = prompts

    async def validate(
        self,
        user: UserContext,
        tasks: Sequence[Task],
        outcomes: dict[str, TaskOutcome],
        draft: AnswerDraft,
    ) -> ValidatedAnswer:
        warnings: list[str] = []
        drafts = {a.task_id: a.segments for a in draft.task_answers}
        working = [
            _WorkingTask(
                task=t,
                outcome=outcomes[t.task_id],
                allowed=self._allowed_evidence(t, outcomes),
                segments=list(drafts.get(t.task_id, [])),
            )
            for t in tasks
            if t.task_id in outcomes
        ]
        answerable = [
            w
            for w in working
            if w.outcome.status in {OutcomeStatus.READY, OutcomeStatus.INSUFFICIENT}
        ]

        for w in answerable:
            self._enforce_citations(w)
        revoked = await self._revoked_sources(user, answerable)
        if revoked:
            logger.info("citations_revoked", extra={"count": len(revoked)})
            for w in answerable:
                w.allowed = {k: v for k, v in w.allowed.items() if v.source_id not in revoked}
                self._enforce_citations(w)
        for w in answerable:
            self._redact_segments(w)
        if not await self._check_grounding(answerable):
            warnings.append(GROUNDING_UNAVAILABLE)

        return self._render(working, warnings)

    # --- step 1: citation validity --------------------------------------
    @staticmethod
    def _allowed_evidence(task: Task, outcomes: dict[str, TaskOutcome]) -> dict[str, Evidence]:
        allowed: dict[str, Evidence] = {}
        for task_id in [task.task_id, *task.plan.depends_on]:
            outcome = outcomes.get(task_id)
            if outcome and outcome.status in {OutcomeStatus.READY, OutcomeStatus.INSUFFICIENT}:
                allowed.update({e.evidence_id: e for e in outcome.evidence})
        return allowed

    @staticmethod
    def _enforce_citations(w: _WorkingTask) -> None:
        kept: list[AnswerSegment] = []
        for segment in w.segments:
            valid = list(dict.fromkeys(i for i in segment.evidence_ids if i in w.allowed))
            if segment.kind in CLAIM_KINDS and not valid:
                w.dropped = True
                continue
            kept.append(segment.model_copy(update={"evidence_ids": valid}))
        w.segments = kept

    # --- step 2: current permissions ------------------------------------
    async def _revoked_sources(
        self, user: UserContext, working: Iterable[_WorkingTask]
    ) -> set[str]:
        cited: set[str] = set()
        for w in working:
            for segment in w.segments:
                for eid in segment.evidence_ids:
                    evidence = w.allowed[eid]
                    # Structured results are governed by view scope + RLS; uploads by the
                    # UploadStore owner check. Registry sources are re-checked here.
                    if evidence.source_kind in {SourceKind.STRUCTURED, SourceKind.USER_UPLOAD}:
                        continue
                    cited.add(evidence.source_id)
                    if evidence.derived_from:
                        cited.add(evidence.derived_from)
        revoked: set[str] = set()
        for source_id in sorted(cited):
            if not await self._policy.can_access_source(user, source_id):
                revoked.add(source_id)
        return revoked

    # --- step 3: sensitive content --------------------------------------
    def _redact_segments(self, w: _WorkingTask) -> None:
        corpus = [_searchable_text(e) for e in w.allowed.values()]
        categories = w.task.access_request.data_categories
        w.segments = [
            s.model_copy(update={"text": self._redact(s.text, categories, corpus)})
            for s in w.segments
        ]

    def _redact(self, text: str, categories: frozenset[DataCategory], corpus: Sequence[str]) -> str:
        spans = sorted(self._sensitivity.scan_output(text), key=lambda s: s.start, reverse=True)
        disclosable = categories | ALWAYS_DISCLOSABLE
        for span in spans:
            permitted = span.category in disclosable and any(span.text in doc for doc in corpus)
            if not permitted:
                text = text[: span.start] + REDACTED + text[span.end :]
        return text

    # --- step 4: grounding ----------------------------------------------
    async def _check_grounding(self, working: Sequence[_WorkingTask]) -> bool:
        claims: dict[str, tuple[_WorkingTask, int]] = {}
        blocks: list[str] = []
        for w in working:
            for index, segment in enumerate(w.segments):
                if segment.kind != SegmentKind.FACT:
                    continue
                claim_id = f"{w.task.task_id}#{index}"
                claims[claim_id] = (w, index)
                excerpts = "\n".join(
                    f'<evidence id="{eid}" untrusted="true">{neutralize(w.allowed[eid].excerpt)}'
                    "</evidence>"
                    for eid in segment.evidence_ids
                )
                blocks.append(
                    f'<claim id="{claim_id}">{neutralize(segment.text)}</claim>\n{excerpts}'
                )
        if not claims:
            return True

        system = self._prompts.render(
            "grounding", untrusted_clause=UNTRUSTED_CONTENT_CLAUSE, claims="\n\n".join(blocks)
        )
        try:
            result = await self._llm.complete_structured(
                [ChatMessage(role="system", content=system)],
                GroundingVerdicts,
                role=ModelRole.GROUNDING_CHECK,
            )
        except AppError as exc:
            logger.warning("grounding_unavailable", extra={"reason": exc.code})
            return False

        unsupported = {v.claim_id for v in result.verdicts if not v.supported}
        for w in working:
            removed = {
                index
                for claim_id, (owner, index) in claims.items()
                if owner is w and claim_id in unsupported
            }
            if removed:
                w.segments = [s for i, s in enumerate(w.segments) if i not in removed]
                w.dropped = True
        logger.info("grounding_checked", extra={"claims": len(claims), "removed": len(unsupported)})
        return True

    # --- steps 5 & 6: refusals and rendering ----------------------------
    def _render(self, working: Sequence[_WorkingTask], warnings: list[str]) -> ValidatedAnswer:
        citations: list[Citation] = []
        number_for: dict[str, int] = {}
        results: list[TaskResult] = []

        for w in working:
            status, body, cited = self._render_task(w, citations, number_for)
            results.append(
                TaskResult(
                    task_id=w.task.task_id,
                    question=w.task.plan.question,
                    status=status,
                    answer_markdown=body,
                    citation_ids=cited,
                    attempts=w.outcome.attempts,
                )
            )

        if len(results) == 1:
            markdown = results[0].answer_markdown
        else:
            markdown = "\n\n".join(
                f"**{n}. {neutralize(r.question)}**\n\n{r.answer_markdown}"
                for n, r in enumerate(results, start=1)
            )
        return ValidatedAnswer(
            answer_markdown=markdown, tasks=results, citations=citations, warnings=warnings
        )

    def _render_task(
        self,
        w: _WorkingTask,
        citations: list[Citation],
        number_for: dict[str, int],
    ) -> tuple[TaskStatus, str, list[int]]:
        status = w.outcome.status
        if status == OutcomeStatus.DECLINED:
            reasons = w.task.decision.reasons
            return (
                TaskStatus.DECLINED,
                REFUSALS.get(reasons[0], DEFAULT_REFUSAL) if reasons else DEFAULT_REFUSAL,
                [],
            )
        if status == OutcomeStatus.OUT_OF_SCOPE:
            return TaskStatus.ANSWERED, OUT_OF_SCOPE_MESSAGE, []
        if status == OutcomeStatus.DEPENDENCY_UNAVAILABLE:
            return TaskStatus.INSUFFICIENT_EVIDENCE, f"*{DEPENDENCY_GAP}*", []
        if status == OutcomeStatus.FAILED:
            message = w.outcome.failures[0].safe_message if w.outcome.failures else FAILED_MESSAGE
            return TaskStatus.FAILED, message, []

        segments = list(w.segments)
        has_claims = any(s.kind in CLAIM_KINDS for s in segments)
        if w.dropped:
            segments.append(AnswerSegment(text=UNVERIFIED_GAP, kind=SegmentKind.GAP))
        if not has_claims and not any(s.kind == SegmentKind.GAP for s in segments):
            segments.append(AnswerSegment(text=NO_ANSWER_GAP, kind=SegmentKind.GAP))

        lines: list[str] = []
        cited_numbers: list[int] = []
        for segment in segments:
            markers = ""
            for eid in segment.evidence_ids:
                if eid not in number_for:
                    number_for[eid] = len(number_for) + 1
                    citations.append(self._citation(number_for[eid], w.allowed[eid], w))
                number = number_for[eid]
                markers += f"[{number}]"
                if number not in cited_numbers:
                    cited_numbers.append(number)
            text = segment.text.strip()
            if segment.kind == SegmentKind.GAP:
                text = f"*{text}*"
            elif segment.kind == SegmentKind.UNCERTAINTY:
                text = f"*Note:* {text}"
            lines.append(f"{text} {markers}".rstrip())

        has_gaps = any(s.kind in {SegmentKind.GAP, SegmentKind.UNCERTAINTY} for s in segments)
        if not has_claims:
            task_status = TaskStatus.INSUFFICIENT_EVIDENCE
        elif has_gaps or w.outcome.status == OutcomeStatus.INSUFFICIENT:
            task_status = TaskStatus.PARTIAL
        else:
            task_status = TaskStatus.ANSWERED
        return task_status, "\n\n".join(lines), cited_numbers

    def _citation(self, number: int, evidence: Evidence, w: _WorkingTask) -> Citation:
        categories = w.task.access_request.data_categories
        corpus = [_searchable_text(e) for e in w.allowed.values()]
        excerpt = self._redact(evidence.excerpt, categories, corpus)
        if len(excerpt) > MAX_CITATION_EXCERPT:
            excerpt = excerpt[: MAX_CITATION_EXCERPT - 1].rstrip() + "…"
        previewable = evidence.source_kind not in {SourceKind.STRUCTURED, SourceKind.USER_UPLOAD}
        return Citation(
            citation_id=number,
            evidence_id=evidence.evidence_id,
            source_id=evidence.source_id,
            title=self._redact(evidence.source_title, categories, corpus),
            kind=evidence.kind,
            location_label=self._redact(evidence.location.label, categories, corpus),
            excerpt=excerpt,
            preview_url=f"/api/v1/sources/{evidence.source_id}" if previewable else None,
        )


def _searchable_text(evidence: Evidence) -> str:
    parts = [evidence.excerpt, evidence.source_title]
    if evidence.kind == EvidenceKind.STRUCTURED and evidence.structured:
        parts.extend(str(v) for row in evidence.structured.rows for v in row.values())
    return "\n".join(parts)
