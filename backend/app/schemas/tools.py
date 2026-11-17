"""Tool request/result contracts for the Retrieval and SQL agents (§6.4)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.common import Modality

Attempt = Literal[1, 2]

DEFAULT_MODALITIES: frozenset[Modality] = frozenset(
    {Modality.TEXT, Modality.IMAGE_CAPTION, Modality.IMAGE_OCR}
)


class RetrievalRequest(BaseModel):
    task_id: str
    query: str = Field(min_length=1, max_length=2000)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    modalities: frozenset[Modality] = DEFAULT_MODALITIES
    top_k: int = Field(8, ge=1, le=20)
    upload_ids: list[str] = Field(default_factory=list, max_length=3)
    need_visual_interpretation: bool = False
    attempt: Attempt


class SQLRequest(BaseModel):
    task_id: str
    question: str = Field(min_length=1, max_length=2000)
    hint: str | None = Field(default=None, max_length=1000)
    attempt: Attempt


class FilterOp(StrEnum):
    EQ = "eq"
    NEQ = "neq"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"
    BETWEEN = "between"
    ILIKE = "ilike"


class AggFn(StrEnum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class StructuredQuerySpec(BaseModel):
    """What the SQL Agent's LLM produces. The LLM never writes SQL text."""

    view: str
    select: list[str] = Field(default_factory=list)
    aggregates: list[tuple[AggFn, str]] = Field(default_factory=list)  # (fn, column or "*")
    filters: list[tuple[str, FilterOp, Any]] = Field(default_factory=list)
    group_by: list[str] = Field(default_factory=list)
    order_by: list[tuple[str, Literal["asc", "desc"]]] = Field(default_factory=list)
    limit: int = Field(50, ge=1, le=200)


class StructuredProvenance(BaseModel):
    view: str
    dataset_source_ids: list[str]
    query_fingerprint: str  # hash of the compiled SQL, never the SQL itself
    executed_at: datetime


class SQLResult(BaseModel):
    task_id: str
    spec: StructuredQuerySpec
    columns: list[str]
    rows: list[dict[str, Any]] = Field(max_length=200)
    row_count: int
    truncated: bool
    provenance: StructuredProvenance


ToolFailureCode = Literal[
    "timeout", "unavailable", "invalid_spec", "no_permitted_view", "capability_limit"
]

# Failures where a retry cannot help; the orchestrator never retries these.
NON_RETRYABLE_FAILURES: frozenset[str] = frozenset({"no_permitted_view", "capability_limit"})


class ToolFailure(BaseModel):
    task_id: str
    code: ToolFailureCode
    safe_message: str  # templated, safe to show to the user
