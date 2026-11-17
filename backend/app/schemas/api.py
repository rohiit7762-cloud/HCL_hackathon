"""Public HTTP API contracts (§6.6). The Streamlit client imports these too."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.common import Classification
from app.schemas.evidence import EvidenceKind


class TaskStatus(StrEnum):
    ANSWERED = "answered"
    PARTIAL = "partial"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DECLINED = "declined"
    FAILED = "failed"


class Citation(BaseModel):
    citation_id: int  # rendered as [1], [2], ...
    evidence_id: str
    source_id: str
    title: str
    kind: EvidenceKind
    location_label: str
    excerpt: str = Field(max_length=400)
    preview_url: str | None = None


class TaskResult(BaseModel):
    task_id: str
    question: str
    status: TaskStatus
    answer_markdown: str
    citation_ids: list[int] = Field(default_factory=list)
    attempts: int = Field(ge=0, le=2)


ProgressStep = Literal[
    "identity",
    "planning",
    "compliance",
    "retrieval",
    "sql",
    "evidence_check",
    "retry",
    "answer",
    "validation",
]


class ProgressEvent(BaseModel):
    step: ProgressStep
    task_id: str | None = None
    status: Literal["started", "done", "skipped", "failed"]
    message: str  # fixed template text only; never contains content


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = Field(default=None, max_length=64)
    upload_ids: list[str] = Field(default_factory=list, max_length=3)


class QueryResponse(BaseModel):
    request_id: str
    conversation_id: str
    answer_markdown: str
    tasks: list[TaskResult]
    citations: list[Citation]
    progress: list[ProgressEvent]
    warnings: list[str] = Field(default_factory=list)


class DemoSessionRequest(BaseModel):
    employee_id: str = Field(min_length=1, max_length=64)


class DemoSessionResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in_seconds: int


class MeResponse(BaseModel):
    employee_id: str
    display_name: str
    title: str
    department: str
    roles: list[str]
    groups: list[str]
    clearance: Classification
    is_synthetic: bool


class UploadResponse(BaseModel):
    upload_id: str


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody
