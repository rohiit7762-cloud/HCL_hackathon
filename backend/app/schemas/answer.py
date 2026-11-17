"""Answer Agent draft contracts (§6.6)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class SegmentKind(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    RECOMMENDATION = "recommendation"
    UNCERTAINTY = "uncertainty"
    GAP = "gap"


# Segments that make claims and therefore must cite evidence.
CLAIM_KINDS: frozenset[SegmentKind] = frozenset(
    {SegmentKind.FACT, SegmentKind.INFERENCE, SegmentKind.RECOMMENDATION}
)


class AnswerSegment(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    kind: SegmentKind
    evidence_ids: list[str] = Field(default_factory=list)


class TaskAnswerDraft(BaseModel):
    task_id: str
    segments: list[AnswerSegment] = Field(default_factory=list)


class AnswerDraft(BaseModel):
    overview: str | None = Field(default=None, max_length=2000)
    task_answers: list[TaskAnswerDraft] = Field(default_factory=list)
