"""Evidence Check: decide per task whether evidence supports an answer (§7.5)."""

from __future__ import annotations

from collections.abc import Sequence

from app.agents.formatting import render_evidence
from app.core.errors import AppError
from app.core.logging import get_logger
from app.prompts import UNTRUSTED_CONTENT_CLAUSE, PromptLibrary
from app.schemas.evidence import Evidence, EvidenceAssessment, EvidenceKind, EvidenceStatus
from app.schemas.interfaces import LLMProvider
from app.schemas.llm import ChatMessage, ModelRole
from app.schemas.planning import Task
from app.schemas.tools import Attempt

logger = get_logger(__name__)


class EvidenceChecker:
    def __init__(self, llm: LLMProvider, prompts: PromptLibrary, min_score: float) -> None:
        self._llm = llm
        self._prompts = prompts
        self._min_score = min_score

    async def assess(
        self, task: Task, evidence: Sequence[Evidence], attempt: Attempt
    ) -> EvidenceAssessment:
        gate = self._deterministic_gate(task, evidence, attempt)
        if gate is not None:
            return gate

        system = self._prompts.render(
            "evidence",
            untrusted_clause=UNTRUSTED_CONTENT_CLAUSE,
            task_id=task.task_id,
            attempt=str(attempt),
            question=task.plan.question,
            evidence=render_evidence(evidence),
        )
        try:
            judged = await self._llm.complete_structured(
                [ChatMessage(role="system", content=system)],
                EvidenceAssessment,
                role=ModelRole.EVIDENCE_JUDGE,
            )
        except AppError as exc:
            logger.warning("evidence_judge_fallback", extra={"reason": exc.code})
            return self._fallback(task, evidence, attempt)

        known = {e.evidence_id for e in evidence}
        relevant = [eid for eid in judged.relevant_evidence_ids if eid in known]
        status = judged.status
        if status == EvidenceStatus.SUFFICIENT and not relevant:
            status = EvidenceStatus.INSUFFICIENT
        return judged.model_copy(
            update={
                "task_id": task.task_id,
                "attempt": attempt,
                "status": status,
                "relevant_evidence_ids": relevant,
            }
        )

    def _deterministic_gate(
        self, task: Task, evidence: Sequence[Evidence], attempt: Attempt
    ) -> EvidenceAssessment | None:
        if not evidence:
            return EvidenceAssessment(
                task_id=task.task_id,
                attempt=attempt,
                status=EvidenceStatus.NO_EVIDENCE,
                missing_information=[task.plan.question],
            )
        has_structured = any(e.kind == EvidenceKind.STRUCTURED for e in evidence)
        scores = [e.score for e in evidence if e.score is not None]
        if not has_structured and scores and max(scores) < self._min_score:
            return EvidenceAssessment(
                task_id=task.task_id,
                attempt=attempt,
                status=EvidenceStatus.INSUFFICIENT,
                missing_information=[task.plan.question],
                refinement_hint="Results were only weakly related; try different terms.",
            )
        return None

    def _fallback(
        self, task: Task, evidence: Sequence[Evidence], attempt: Attempt
    ) -> EvidenceAssessment:
        relevant = [
            e.evidence_id
            for e in evidence
            if e.kind == EvidenceKind.STRUCTURED or e.score is None or e.score >= self._min_score
        ]
        return EvidenceAssessment(
            task_id=task.task_id,
            attempt=attempt,
            status=EvidenceStatus.SUFFICIENT if relevant else EvidenceStatus.INSUFFICIENT,
            relevant_evidence_ids=relevant,
        )
