"""Planner Agent: compliance assessment, decomposition and routing (§7.3).

The LLM only *describes* the query. Access is decided by the deterministic
PolicyService, and detections from the LLM and the SensitivityDetector are
unioned, so model output can only add restrictions (invariant I2).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date

from app.agents.formatting import wrap_user_query
from app.core.errors import AppError
from app.core.logging import get_logger
from app.prompts import UNTRUSTED_CONTENT_CLAUSE, PromptLibrary
from app.schemas.common import DataCategory, SourceNeed
from app.schemas.evidence import EvidenceAssessment
from app.schemas.interfaces import (
    LLMProvider,
    PolicyService,
    PrincipalRepository,
    SensitivityDetector,
)
from app.schemas.llm import ChatMessage, ModelRole
from app.schemas.planning import (
    Intent,
    PlannedTask,
    PlannerOutput,
    RefinedQuery,
    Task,
    TaskRoute,
)
from app.schemas.security import TaskAccessRequest, UserContext

logger = get_logger(__name__)

SELF_REF = "self"
UNSPECIFIED_SUBJECT = "<unspecified>"
_TEAM_REFS = re.compile(r"^(my )?(direct reports?|reports|team members?|team)$", re.IGNORECASE)

# Intents that imply structured data when the model gives no source needs.
_STRUCTURED_INTENTS = frozenset({Intent.METRICS, Intent.EXPERT_FINDER})
_PERSONAL_SUBJECT_CATEGORIES = frozenset({DataCategory.EMPLOYEE_PERSONAL})


class Planner:
    def __init__(
        self,
        llm: LLMProvider,
        policy: PolicyService,
        principals: PrincipalRepository,
        sensitivity: SensitivityDetector,
        prompts: PromptLibrary,
    ) -> None:
        self._llm = llm
        self._policy = policy
        self._principals = principals
        self._sensitivity = sensitivity
        self._prompts = prompts

    # --- planning --------------------------------------------------------
    async def plan(
        self,
        user: UserContext,
        query: str,
        *,
        history: Sequence[str] = (),
        upload_ids: Sequence[str] = (),
    ) -> list[Task]:
        planned = await self._decompose(query, history, upload_ids)
        tasks: list[Task] = []
        for item in planned:
            item = self._sanitize_uploads(item, upload_ids)
            access_request = await self.build_access_request(user, item)
            decision = await self._policy.evaluate_task(user, access_request)
            tasks.append(
                Task(
                    plan=item,
                    access_request=access_request,
                    decision=decision,
                    route=route_for(item),
                )
            )
        logger.info(
            "plan_ready",
            extra={
                "task_count": len(tasks),
                "declined": sum(1 for t in tasks if not t.decision.allowed),
            },
        )
        return tasks

    async def build_access_request(
        self, user: UserContext, item: PlannedTask, extra_subject_ids: Sequence[str] = ()
    ) -> TaskAccessRequest:
        """Merge LLM and deterministic findings and resolve people, server-side."""
        findings = [self._sensitivity.detect(text) for text in (item.original_span, item.question)]
        categories = set(item.data_categories)
        fields = {_normalize_field(f) for f in item.requested_fields}
        for finding in findings:
            categories |= finding.categories
            fields |= finding.fields

        subject_ids, unresolved = await self._resolve_subjects(user, item.subject_refs, categories)
        subject_ids |= set(extra_subject_ids)
        needs_subject = bool(categories & _PERSONAL_SUBJECT_CATEGORIES)
        if needs_subject and not subject_ids and not unresolved:
            unresolved = [UNSPECIFIED_SUBJECT]

        return TaskAccessRequest(
            task_id=item.task_id,
            data_categories=frozenset(categories),
            subject_employee_ids=frozenset(subject_ids),
            unresolved_subject_refs=tuple(unresolved),
            requested_fields=frozenset(f for f in fields if f),
            source_needs=frozenset(effective_source_needs(item)),
        )

    async def _decompose(
        self, query: str, history: Sequence[str], upload_ids: Sequence[str]
    ) -> list[PlannedTask]:
        system = self._prompts.render(
            "planner",
            untrusted_clause=UNTRUSTED_CONTENT_CLAUSE,
            today=date.today().isoformat(),
            history="\n".join(f"- {h}" for h in history[-5:]) or "(none)",
            upload_ids=", ".join(upload_ids) or "(none)",
        )
        messages = [
            ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=wrap_user_query(query)),
        ]
        try:
            output = await self._llm.complete_structured(
                messages, PlannerOutput, role=ModelRole.PLANNER
            )
            return output.tasks
        except AppError as exc:
            logger.warning("planner_fallback", extra={"reason": exc.code})
            return [self._fallback_task(query, upload_ids)]

    def _fallback_task(self, query: str, upload_ids: Sequence[str]) -> PlannedTask:
        findings = self._sensitivity.detect(query)
        needs = {SourceNeed.DOCUMENTS, SourceNeed.STRUCTURED}
        if upload_ids:
            needs.add(SourceNeed.IMAGES)
        return PlannedTask(
            task_id="t1",
            question=query[:1000],
            original_span=query,
            intent=Intent.FACT_LOOKUP,
            data_categories=set(findings.categories) or {DataCategory.GENERAL_KNOWLEDGE},
            requested_fields=sorted(findings.fields),
            source_needs=needs,
            retrieval_query=query[:1000],
            use_upload_ids=list(upload_ids),
        )

    async def _resolve_subjects(
        self, user: UserContext, refs: Sequence[str], categories: set[DataCategory]
    ) -> tuple[set[str], list[str]]:
        subject_ids: set[str] = set()
        to_resolve: list[str] = []
        unresolved: list[str] = []
        for ref in refs:
            if ref.strip().lower() in {SELF_REF, "me", "myself", "i"}:
                subject_ids.add(user.employee_id)
            elif _TEAM_REFS.match(ref.strip()):
                # "my direct reports" = the user's reports, from trusted identity data.
                if user.direct_report_ids:
                    subject_ids.update(user.direct_report_ids)
                else:
                    unresolved.append(ref.strip())
            elif ref.strip():
                to_resolve.append(ref.strip())
        if to_resolve:
            resolved = await self._principals.resolve_people(user, to_resolve)
            personal = bool(categories & _PERSONAL_SUBJECT_CATEGORIES)
            for ref in to_resolve:
                matches = resolved.get(ref, [])
                # Ambiguity is unsafe for personal data: treat it as unresolved.
                if not matches or (personal and len(matches) > 1):
                    unresolved.append(ref)
                else:
                    subject_ids.update(matches)
        return subject_ids, unresolved

    @staticmethod
    def _sanitize_uploads(item: PlannedTask, upload_ids: Sequence[str]) -> PlannedTask:
        allowed = set(upload_ids)
        kept = [u for u in item.use_upload_ids if u in allowed]
        if not kept and allowed and SourceNeed.IMAGES in item.source_needs:
            kept = list(upload_ids)
        return item.model_copy(update={"use_upload_ids": kept})

    # --- retry refinement ------------------------------------------------
    async def refine(
        self, task: Task, assessment: EvidenceAssessment, previous_query: str
    ) -> RefinedQuery:
        """Rephrase the search for the single retry. Never sees evidence excerpts."""
        system = self._prompts.render(
            "refine",
            untrusted_clause=UNTRUSTED_CONTENT_CLAUSE,
            task_id=task.task_id,
            question=task.plan.question,
            previous_query=previous_query,
            missing="; ".join(assessment.missing_information) or "(not specified)",
            hint=assessment.refinement_hint or "(none)",
        )
        try:
            refined = await self._llm.complete_structured(
                [ChatMessage(role="system", content=system)],
                RefinedQuery,
                role=ModelRole.REFINER,
            )
            return refined.model_copy(update={"task_id": task.task_id})
        except AppError as exc:
            logger.warning("refine_fallback", extra={"reason": exc.code})
            extra = " ".join(assessment.missing_information)
            return RefinedQuery(
                task_id=task.task_id,
                retrieval_query=f"{task.plan.question} {extra}".strip()[:1000],
                rationale="fallback",
            )


def effective_source_needs(item: PlannedTask) -> set[SourceNeed]:
    needs = set(item.source_needs)
    if item.use_upload_ids:
        needs.add(SourceNeed.IMAGES)
    if not needs:
        needs = (
            {SourceNeed.STRUCTURED}
            if item.intent in _STRUCTURED_INTENTS
            else {SourceNeed.DOCUMENTS}
        )
    return needs


def route_for(item: PlannedTask) -> TaskRoute:
    if item.intent == Intent.OUT_OF_SCOPE:
        return TaskRoute.NONE
    needs = effective_source_needs(item)
    unstructured = bool(needs & {SourceNeed.DOCUMENTS, SourceNeed.IMAGES})
    structured = SourceNeed.STRUCTURED in needs
    if unstructured and structured:
        return TaskRoute.BOTH
    return TaskRoute.SQL if structured else TaskRoute.RETRIEVAL


def _normalize_field(name: str) -> str:
    return "_".join(name.strip().lower().replace("-", " ").split())
