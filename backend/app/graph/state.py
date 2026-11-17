"""LangGraph state definitions for the query workflow."""

from __future__ import annotations

import operator
from enum import StrEnum
from typing import Annotated, TypedDict

from pydantic import BaseModel, Field

from app.schemas.answer import AnswerDraft
from app.schemas.api import ProgressEvent, QueryRequest, QueryResponse
from app.schemas.evidence import Evidence, EvidenceAssessment
from app.schemas.planning import Task
from app.schemas.security import UserContext
from app.schemas.tools import ToolFailure

MAX_RETRIEVAL_ATTEMPTS = 2  # one initial attempt + one retry (invariant I6)


class OutcomeStatus(StrEnum):
    READY = "ready"  # evidence judged sufficient
    INSUFFICIENT = "insufficient"  # attempts exhausted; answer what is supported
    FAILED = "failed"  # tool failures or timeout
    DECLINED = "declined"  # policy denied; never executed
    OUT_OF_SCOPE = "out_of_scope"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"


class TaskOutcome(BaseModel):
    task_id: str
    status: OutcomeStatus
    attempts: int = Field(0, ge=0, le=MAX_RETRIEVAL_ATTEMPTS)
    tool_calls: int = 0
    evidence: list[Evidence] = Field(default_factory=list)
    assessment: EvidenceAssessment | None = None
    failures: list[ToolFailure] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def usable_by_dependents(self) -> bool:
        """A dependent task can build on this outcome only if it found something."""
        if self.status == OutcomeStatus.READY:
            return True
        return self.status == OutcomeStatus.INSUFFICIENT and bool(self.evidence)

    def relevant_evidence(self) -> list[Evidence]:
        if self.assessment and self.assessment.relevant_evidence_ids:
            wanted = set(self.assessment.relevant_evidence_ids)
            chosen = [e for e in self.evidence if e.evidence_id in wanted]
            if chosen:
                return chosen
        return list(self.evidence)


def merge_outcomes(
    left: dict[str, TaskOutcome], right: dict[str, TaskOutcome]
) -> dict[str, TaskOutcome]:
    return {**left, **right}


class QueryState(TypedDict, total=False):
    """Top-level graph state."""

    user: UserContext
    request: QueryRequest
    request_id: str
    conversation_id: str
    history: list[str]
    deadline: float  # monotonic time by which task execution must finish
    tasks: list[Task]
    outcomes: Annotated[dict[str, TaskOutcome], merge_outcomes]
    progress: Annotated[list[ProgressEvent], operator.add]
    warnings: Annotated[list[str], operator.add]
    draft: AnswerDraft
    response: QueryResponse


class TaskRunState(TypedDict, total=False):
    """State of the per-task subgraph (execute → assess → maybe refine → execute)."""

    user: UserContext
    task: Task
    deadline: float
    dependency_evidence: list[Evidence]
    attempt: int
    query: str
    keywords: list[str]
    sql_hint: str | None
    evidence: list[Evidence]
    failures: list[ToolFailure]
    warnings: list[str]
    assessment: EvidenceAssessment | None
    tool_calls: int
    progress: list[ProgressEvent]
