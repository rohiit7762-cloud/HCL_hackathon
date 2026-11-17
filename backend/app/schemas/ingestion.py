"""Ingestion, chunk and source contracts (§6.7)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Classification, Modality, SourceKind
from app.schemas.evidence import EvidenceLocation


class SourceMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str
    tenant_id: str
    title: str
    kind: SourceKind
    mime_type: str
    classification: Classification
    allowed_groups: frozenset[str] = frozenset()  # empty = no group restriction
    allowed_roles: frozenset[str] = frozenset()  # empty = no role restriction
    owner_department: str | None = None
    contains_personal_data: bool = False
    contains_confidential_business: bool = False
    subject_employee_ids: frozenset[str] = frozenset()
    parent_source_id: str | None = None  # e.g. an image embedded in a PDF
    checksum_sha256: str
    version: int = Field(ge=1)
    blob_path: str
    ingested_by: str
    ingested_at: datetime
    revoked: bool = False


class IngestOptions(BaseModel):
    """Multipart form fields on POST /api/v1/ingest."""

    title: str | None = Field(default=None, max_length=300)
    classification: Classification
    allowed_groups: list[str] = Field(default_factory=list)
    allowed_roles: list[str] = Field(default_factory=list)
    contains_personal_data: bool = False
    contains_confidential_business: bool = False
    subject_employee_ids: list[str] = Field(default_factory=list)
    dataset_name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,62}$")


class IngestResult(BaseModel):
    source_id: str
    version: int
    status: Literal["indexed", "loaded", "skipped_duplicate", "failed"]
    chunk_count: int = 0
    row_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class ChunkRecord(BaseModel):
    chunk_id: str  # sha256(source_id, version, ordinal, modality)
    source_id: str
    source_version: int
    tenant_id: str
    ordinal: int
    modality: Modality
    text: str
    heading_path: list[str] = Field(default_factory=list)
    location: EvidenceLocation
    token_count: int
    classification: Classification
    embedding_model: str


class SourcePreview(BaseModel):
    source_id: str
    title: str
    kind: SourceKind
    classification: Classification
    location_label: str | None = None
    excerpt: str | None = None
    image_url: str | None = None
