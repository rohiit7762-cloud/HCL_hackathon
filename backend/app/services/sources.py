"""Protected source previews and images (§8.11). None means 'not found or not permitted'."""

from __future__ import annotations

from app.repositories.base import SourceRegistry, VectorStore
from app.repositories.blob_store import BlobStore
from app.schemas.common import SourceKind
from app.schemas.ingestion import SourcePreview
from app.schemas.interfaces import PolicyService
from app.schemas.security import UserContext

MAX_PREVIEW_CHARS = 1200


class SourceServiceImpl:
    def __init__(
        self,
        policy: PolicyService,
        registry: SourceRegistry,
        vectors: VectorStore,
        blobs: BlobStore,
    ) -> None:
        self._policy = policy
        self._registry = registry
        self._vectors = vectors
        self._blobs = blobs

    async def get_preview(
        self, user: UserContext, source_id: str, evidence_location: str | None = None
    ) -> SourcePreview | None:
        if not await self._policy.can_access_source(user, source_id):
            return None
        source = await self._registry.get(source_id)
        if source is None:
            return None
        chunk = await self._vectors.get_chunk(source_id, evidence_location)
        excerpt = chunk.text[:MAX_PREVIEW_CHARS] if chunk else None
        return SourcePreview(
            source_id=source.source_id,
            title=source.title,
            kind=source.kind,
            classification=source.classification,
            location_label=chunk.location.label if chunk else None,
            excerpt=excerpt,
            image_url=f"/api/v1/sources/{source.source_id}/image"
            if source.kind == SourceKind.IMAGE
            else None,
        )

    async def get_image(self, user: UserContext, source_id: str) -> tuple[bytes, str] | None:
        if not await self._policy.can_access_source(user, source_id):
            return None
        source = await self._registry.get(source_id)
        if source is None or source.kind != SourceKind.IMAGE:
            return None
        data = await self._blobs.read(source.blob_path)
        return (data, source.mime_type) if data is not None else None
