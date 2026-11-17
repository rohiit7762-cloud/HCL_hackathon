"""Answer Agent: one grounded, structured draft over authorized evidence (§7.6).

Declined tasks are never sent to the model; their refusals are added later from
fixed templates by the ResponseValidator.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.agents.formatting import neutralize, render_evidence
from app.core.errors import AppError
from app.core.logging import get_logger
from app.graph.state import OutcomeStatus, TaskOutcome
from app.prompts import UNTRUSTED_CONTENT_CLAUSE, PromptLibrary
from app.schemas.answer import AnswerDraft, AnswerSegment, SegmentKind, TaskAnswerDraft
from app.schemas.interfaces import LLMProvider
from app.schemas.llm import ChatMessage, ModelRole
from app.schemas.planning import Task

logger = get_logger(__name__)

ANSWERABLE = frozenset({OutcomeStatus.READY, OutcomeStatus.INSUFFICIENT})
FALLBACK_GAP = "I couldn't generate a reliable answer for this part."


class AnswerAgent:
    def __init__(self, llm: LLMProvider, prompts: PromptLibrary) -> None:
        self._llm = llm
        self._prompts = prompts

    async def draft(self, tasks: Sequence[Task], outcomes: dict[str, TaskOutcome]) -> AnswerDraft:
        answerable = [
            t for t in tasks if t.task_id in outcomes and outcomes[t.task_id].status in ANSWERABLE
        ]
        if not answerable:
            return AnswerDraft()

        system = self._prompts.render(
            "answer",
            untrusted_clause=UNTRUSTED_CONTENT_CLAUSE,
            tasks="\n\n".join(self._task_block(t, outcomes) for t in answerable),
        )
        try:
            draft = await self._llm.complete_structured(
                [ChatMessage(role="system", content=system)],
                AnswerDraft,
                role=ModelRole.ANSWER,
            )
        except AppError as exc:
            logger.warning("answer_fallback", extra={"reason": exc.code})
            draft = AnswerDraft()

        return self._normalize(draft, [t.task_id for t in answerable])

    def evidence_for(self, task: Task, outcomes: dict[str, TaskOutcome]) -> list:  # type: ignore[type-arg]
        """Evidence the model may use for a task: its own plus its dependencies'."""
        items = list(outcomes[task.task_id].relevant_evidence())
        for dep in task.plan.depends_on:
            if dep in outcomes and outcomes[dep].status in ANSWERABLE:
                items.extend(outcomes[dep].relevant_evidence())
        return items

    def _task_block(self, task: Task, outcomes: dict[str, TaskOutcome]) -> str:
        outcome = outcomes[task.task_id]
        note = ""
        if outcome.status == OutcomeStatus.INSUFFICIENT and outcome.assessment:
            missing = "; ".join(outcome.assessment.missing_information)
            note = f"\nNote: evidence is incomplete. Missing: {neutralize(missing) or 'unknown'}."
        return (
            f"### Task {task.task_id}\nQuestion: {neutralize(task.plan.question)}{note}\n"
            + render_evidence(self.evidence_for(task, outcomes))
        )

    @staticmethod
    def _normalize(draft: AnswerDraft, task_ids: list[str]) -> AnswerDraft:
        """Keep one entry per answerable task, in order; fill gaps for missing ones."""
        by_id = {a.task_id: a for a in draft.task_answers if a.task_id in task_ids}
        answers = [
            by_id.get(task_id)
            or TaskAnswerDraft(
                task_id=task_id,
                segments=[AnswerSegment(text=FALLBACK_GAP, kind=SegmentKind.GAP)],
            )
            for task_id in task_ids
        ]
        return AnswerDraft(overview=None, task_answers=answers)
