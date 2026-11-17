from __future__ import annotations

from app.repositories.source_repository import BlobStore, SourceRepository
from app.schemas.ingestion import SourcePreview
from app.schemas.interfaces import PolicyService
from app.schemas.security import UserContext


class SourceService:
    def __init__(self, registry: SourceRepository, blobs: BlobStore, policy: PolicyService):
        self.registry, self.blobs, self.policy = registry, blobs, policy

    async def get_preview(
        self, user: UserContext, source_id: str, evidence_location: str | None = None
    ) -> SourcePreview | None:
        if not await self.policy.can_access_source(user, source_id):
            return None
        source = self.registry.get(source_id)
        if source is None:
            return None
        excerpt = None
        if evidence_location:
            raw = self.blobs.get(source.blob_path)
            if raw and source.mime_type.startswith("text/"):
                lines = raw.decode("utf-8", errors="replace").splitlines()
                try:
                    index = max(0, int(evidence_location) - 1)
                    excerpt = "\n".join(lines[index : index + 8])[:1200]
                except ValueError:
                    pass
        return SourcePreview(
            source_id=source_id,
            title=source.title,
            kind=source.kind,
            classification=source.classification,
            location_label=evidence_location,
            excerpt=excerpt,
        )

    async def get_image(self, user: UserContext, source_id: str) -> tuple[bytes, str] | None:
        if not await self.policy.can_access_source(user, source_id):
            return None
        source = self.registry.get(source_id)
        if source is None or source.kind.value != "image":
            return None
        data = self.blobs.get(source.blob_path)
        return (data, source.mime_type) if data else None
