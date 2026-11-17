"""Source registry in Postgres (core.sources, core.datasets) and the audit sink."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.repositories.base import AuditEntry
from app.repositories.postgres_repository import Database
from app.schemas.common import Classification, SourceKind
from app.schemas.ingestion import SourceMetadata

_COLUMNS = (
    "source_id, tenant_id, title, kind, mime_type, classification, allowed_groups, "
    "allowed_roles, owner_department, contains_personal_data, contains_confidential_business, "
    "subject_employee_ids, parent_source_id, checksum_sha256, version, blob_path, ingested_by, "
    "ingested_at, revoked"
)


class PostgresSourceRegistry:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def list_sources(self, tenant_id: str) -> list[SourceMetadata]:
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM core.sources WHERE tenant_id = %s AND NOT revoked",
            (tenant_id,),
        )
        return [_to_source(r) for r in rows]

    async def get(self, source_id: str) -> SourceMetadata | None:
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM core.sources WHERE source_id = %s", (source_id,)
        )
        return _to_source(rows[0]) if rows else None

    async def find_latest_by_title(self, tenant_id: str, title: str) -> SourceMetadata | None:
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM core.sources WHERE tenant_id = %s AND title = %s "
            "AND parent_source_id IS NULL ORDER BY version DESC LIMIT 1",
            (tenant_id, title),
        )
        return _to_source(rows[0]) if rows else None

    async def find_by_checksum(self, tenant_id: str, checksum: str) -> SourceMetadata | None:
        rows = await self._db.fetch_all(
            f"SELECT {_COLUMNS} FROM core.sources WHERE tenant_id = %s AND checksum_sha256 = %s "
            "AND NOT revoked LIMIT 1",
            (tenant_id, checksum),
        )
        return _to_source(rows[0]) if rows else None

    async def upsert(self, source: SourceMetadata) -> None:
        values = source.model_dump()
        for key in ("allowed_groups", "allowed_roles", "subject_employee_ids"):
            values[key] = sorted(values[key])
        values["kind"] = source.kind.value
        values["classification"] = source.classification.value
        placeholders = ", ".join(f"%({c.strip()})s" for c in _COLUMNS.split(","))
        updates = ", ".join(f"{c.strip()} = EXCLUDED.{c.strip()}" for c in _COLUMNS.split(",")[1:])
        await self._db.execute(
            f"INSERT INTO core.sources ({_COLUMNS}) VALUES ({placeholders}) "
            f"ON CONFLICT (source_id) DO UPDATE SET {updates}",
            values,
        )

    async def delete(self, source_id: str) -> None:
        await self._db.execute(
            "DELETE FROM core.sources WHERE source_id = %s OR parent_source_id = %s",
            (source_id, source_id),
        )

    async def set_revoked(self, source_id: str, revoked: bool) -> None:
        await self._db.execute(
            "UPDATE core.sources SET revoked = %s WHERE source_id = %s OR parent_source_id = %s",
            (revoked, source_id, source_id),
        )

    async def dataset_source_ids(self, tenant_id: str, datasets: Sequence[str]) -> list[str]:
        if not datasets:
            return []
        rows = await self._db.fetch_all(
            "SELECT source_id FROM core.datasets WHERE tenant_id = %s AND dataset_name = ANY(%s) "
            "ORDER BY dataset_name",
            (tenant_id, list(datasets)),
        )
        return [r["source_id"] for r in rows]

    async def register_dataset(
        self,
        tenant_id: str,
        dataset_name: str,
        source_id: str,
        classification: Classification,
        allowed_groups: Sequence[str],
        columns: Sequence[str],
    ) -> None:
        await self._db.execute(
            "INSERT INTO core.datasets (tenant_id, dataset_name, source_id, classification, "
            "allowed_groups, columns) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (tenant_id, dataset_name) DO UPDATE SET source_id = EXCLUDED.source_id, "
            "classification = EXCLUDED.classification, allowed_groups = EXCLUDED.allowed_groups, "
            "columns = EXCLUDED.columns",
            (
                tenant_id,
                dataset_name,
                source_id,
                classification.value,
                list(allowed_groups),
                list(columns),
            ),
        )


class PostgresAuditSink:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, entry: AuditEntry) -> None:
        await self._db.execute(
            "INSERT INTO core.audit_log (tenant_id, employee_id, task_id, categories, allowed, "
            "reasons, policy_version) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                entry.tenant_id,
                entry.employee_id,
                entry.task_id,
                [c.value for c in entry.categories],
                entry.allowed,
                [r.value for r in entry.reasons],
                entry.policy_version,
            ),
        )


def _to_source(row: dict[str, Any]) -> SourceMetadata:
    data = dict(row)
    data["kind"] = SourceKind(data["kind"])
    data["classification"] = Classification(data["classification"])
    for key in ("allowed_groups", "allowed_roles", "subject_employee_ids"):
        data[key] = frozenset(data.get(key) or [])
    return SourceMetadata.model_validate(data)
