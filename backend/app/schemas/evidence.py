"""Evidence and evidence-assessment contracts (§6.5)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from app.schemas.common import Classification, SourceKind
from app.schemas.tools import Attempt, SQLResult, ToolFailure


class EvidenceKind(StrEnum):
    TEXT_CHUNK = "text_chunk"
    IMAGE = "image"
    STRUCTURED = "structured"
    VISION_OBSERVATION = "vision_observation"


class EvidenceLocation(BaseModel):
    page: int | None = None
    section_path: list[str] = Field(default_factory=list)
    paragraph_index: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    label: str  # human-readable, e.g. "p. 4 · Failover > Steps"


class Evidence(BaseModel):
    evidence_id: str  # unique within a request, e.g. "e7"
    task_id: str
    kind: EvidenceKind
    source_id: str
    source_title: str
    source_kind: SourceKind
    classification: Classification
    excerpt: str = Field(max_length=1500)
    location: EvidenceLocation
    score: float | None = None
    image_ref: str | None = None  # opaque, e.g. "/api/v1/sources/{id}/image"
    structured: SQLResult | None = None
    derived_from: str | None = None  # original image source_id for captions/OCR/vision
    retrieved_at: datetime


class RetrievalResult(BaseModel):
    task_id: str
    evidence: list[Evidence] = Field(default_factory=list)
    failure: ToolFailure | None = None
    warnings: list[str] = Field(default_factory=list)


class EvidenceStatus(StrEnum):
    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"
    NO_EVIDENCE = "no_evidence"
    CONTRADICTORY = "contradictory"


class EvidenceAssessment(BaseModel):
    task_id: str
    attempt: Attempt
    status: EvidenceStatus
    relevant_evidence_ids: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    refinement_hint: str | None = None
