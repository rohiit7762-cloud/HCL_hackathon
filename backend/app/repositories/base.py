"""Internal storage interfaces used by the data layer (not part of the shared contracts)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.schemas.common import Classification, DataCategory
from app.schemas.ingestion import ChunkRecord, SourceMetadata
from app.schemas.security import DenialReason


class SourceRegistry(Protocol):
    async def list_sources(self, tenant_id: str) -> list[SourceMetadata]: ...

    async def get(self, source_id: str) -> SourceMetadata | None: ...

    async def find_latest_by_title(self, tenant_id: str, title: str) -> SourceMetadata | None: ...

    async def find_by_checksum(self, tenant_id: str, checksum: str) -> SourceMetadata | None: ...

    async def upsert(self, source: SourceMetadata) -> None: ...

    async def delete(self, source_id: str) -> None: ...

    async def dataset_source_ids(self, tenant_id: str, datasets: Sequence[str]) -> list[str]: ...

    async def register_dataset(
        self,
        tenant_id: str,
        dataset_name: str,
        source_id: str,
        classification: Classification,
        allowed_groups: Sequence[str],
        columns: Sequence[str],
    ) -> None: ...


@dataclass(frozen=True)
class AuditEntry:
    tenant_id: str
    employee_id: str
    task_id: str
    categories: tuple[DataCategory, ...]
    allowed: bool
    reasons: tuple[DenialReason, ...]
    policy_version: str


class AuditSink(Protocol):
    async def record(self, entry: AuditEntry) -> None: ...


@dataclass(frozen=True)
class ChunkHit:
    chunk: ChunkRecord
    score: float  # cosine similarity in [0, 1]
    image_source_id: str | None = None


class VectorStore(Protocol):
    def ensure_compatible(self, model_id: str, dimension: int) -> None: ...

    async def upsert_chunks(
        self,
        chunks: Sequence[ChunkRecord],
        embeddings: Sequence[Sequence[float]],
        extra: dict[str, Any] | None = None,
    ) -> None: ...

    async def delete_source(self, source_id: str) -> None: ...

    async def query(
        self,
        embedding: Sequence[float],
        *,
        tenant_id: str,
        source_ids: Sequence[str],
        modalities: Sequence[str],
        top_k: int,
    ) -> list[ChunkHit]: ...

    async def get_chunk(self, source_id: str, location_label: str | None) -> ChunkRecord | None: ...
