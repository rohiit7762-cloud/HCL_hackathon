from __future__ import annotations

from pathlib import Path
import json

from app.schemas.ingestion import SourceMetadata


class SourceRepository:
    """Small repository adapter; replace with NeonSourceRepository for deployed DI."""

    def __init__(self, manifest: str | None = None) -> None:
        self._sources: dict[str, SourceMetadata] = {}
        self._manifest = Path(manifest).resolve() if manifest else None
        if self._manifest and self._manifest.is_file():
            try:
                records = json.loads(self._manifest.read_text(encoding="utf-8"))
                self._sources = {item["source_id"]: SourceMetadata.model_validate(item) for item in records}
            except (OSError, ValueError):
                self._sources = {}

    def get(self, source_id: str) -> SourceMetadata | None:
        return self._sources.get(source_id)

    def by_checksum(self, tenant_id: str, checksum: str) -> SourceMetadata | None:
        return next(
            (
                s
                for s in self._sources.values()
                if s.tenant_id == tenant_id and s.checksum_sha256 == checksum
            ),
            None,
        )

    def save(self, source: SourceMetadata) -> None:
        self._sources[source.source_id] = source
        if self._manifest:
            self._manifest.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._manifest.with_suffix(".tmp")
            temporary.write_text(json.dumps([s.model_dump(mode="json") for s in self._sources.values()]), encoding="utf-8")
            temporary.replace(self._manifest)

    def all(self, tenant_id: str) -> list[SourceMetadata]:
        return [s for s in self._sources.values() if s.tenant_id == tenant_id]


class BlobStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, tenant_id: str, source_id: str, version: int, data: bytes) -> str:
        path = self.root / tenant_id / source_id / str(version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path.relative_to(self.root))

    def get(self, relative: str) -> bytes | None:
        path = (self.root / relative).resolve()
        if self.root not in path.parents or not path.is_file():
            return None
        return path.read_bytes()
