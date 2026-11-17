"""CSV/JSON → Neon Postgres (§8.7).

Known datasets (employees, incidents, services) are validated row by row with
Pydantic and upserted into typed tables; anything else goes to core.dataset_rows
as JSONB. Each file loads in one transaction using the ingestion role.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.ingestion.parsers.text import decode_text
from app.repositories.postgres_repository import Database
from app.schemas.common import Classification

MAX_ROWS = 50_000


class EmployeeRow(BaseModel):
    employee_id: str
    name: str
    work_email: str | None = None
    title: str | None = None
    department: str | None = None
    team: str | None = None
    manager_id: str | None = None
    location: str | None = None
    expertise: list[str] = Field(default_factory=list)
    hire_date: date | None = None
    salary: float | None = None
    performance_rating: str | None = None
    personal_phone: str | None = None
    home_city: str | None = None
    clearance: Classification = Classification.INTERNAL
    roles: list[str] = Field(default_factory=lambda: ["employee"])
    groups: list[str] = Field(default_factory=list)
    demo_access_summary: str | None = None

    @field_validator("expertise", "roles", "groups", mode="before")
    @classmethod
    def _split(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [v.strip() for v in value.replace(";", ",").split(",") if v.strip()]
        return value

    @field_validator("manager_id", "salary", "hire_date", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if value == "" else value


class IncidentRow(BaseModel):
    incident_id: str
    priority: str = Field(pattern=r"^P[1-4]$")
    status: str
    service: str
    title: str
    summary: str | None = None
    root_cause_category: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None
    assignee_id: str | None = None

    @field_validator("resolved_at", "assignee_id", mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        return None if value == "" else value


class ServiceRow(BaseModel):
    service_id: str
    name: str
    owner_team: str | None = None
    tier: str | None = None
    oncall_group: str | None = None
    owner_employee_id: str | None = None


@dataclass
class LoadResult:
    row_count: int
    columns: list[str]
    warnings: list[str] = field(default_factory=list)


def read_records(filename: str, data: bytes) -> list[dict[str, Any]]:
    text = decode_text(data)
    if filename.lower().endswith(".json"):
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            parsed = next((v for v in parsed.values() if isinstance(v, list)), [parsed])
        if not isinstance(parsed, list) or not all(isinstance(r, dict) for r in parsed):
            raise ValueError("JSON must be a list of objects")
        records = parsed
    else:
        records = [dict(r) for r in csv.DictReader(io.StringIO(text))]
    if len(records) > MAX_ROWS:
        raise ValueError(f"too many rows (limit {MAX_ROWS})")
    return records


class StructuredLoader:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(
        self, tenant_id: str, dataset_name: str, filename: str, data: bytes
    ) -> LoadResult:
        records = read_records(filename, data)
        async with self._db.service.connection() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO core.tenants (tenant_id, name) VALUES (%s, %s) "
                "ON CONFLICT (tenant_id) DO NOTHING",
                (tenant_id, tenant_id),
            )
            if dataset_name == "employees":
                return await self._load_employees(conn, tenant_id, records)
            if dataset_name == "incidents":
                return await self._load_typed(
                    conn, tenant_id, records, IncidentRow, "incidents", "incident_id"
                )
            if dataset_name == "services":
                return await self._load_typed(
                    conn, tenant_id, records, ServiceRow, "services", "service_id"
                )
            return await self._load_generic(conn, tenant_id, dataset_name, records)

    async def _load_employees(
        self, conn: Any, tenant_id: str, records: Sequence[dict[str, Any]]
    ) -> LoadResult:
        rows, warnings = _validate(records, EmployeeRow)
        columns = [
            "employee_id",
            "name",
            "work_email",
            "title",
            "department",
            "team",
            "manager_id",
            "location",
            "expertise",
            "hire_date",
            "salary",
            "performance_rating",
            "personal_phone",
            "home_city",
            "clearance",
            "demo_access_summary",
        ]
        async with conn.cursor() as cur:
            await cur.executemany(
                _upsert_sql("employees", columns, "employee_id"),
                [(tenant_id, *[_value(getattr(r, c)) for c in columns]) for r in rows],
            )
            ids = [r.employee_id for r in rows]
            await cur.execute(
                "DELETE FROM core.employee_roles WHERE tenant_id = %s AND employee_id = ANY(%s)",
                (tenant_id, ids),
            )
            await cur.execute(
                "DELETE FROM core.employee_groups WHERE tenant_id = %s AND employee_id = ANY(%s)",
                (tenant_id, ids),
            )
            await cur.executemany(
                "INSERT INTO core.employee_roles (tenant_id, employee_id, role) "
                "VALUES (%s, %s, %s)",
                [(tenant_id, r.employee_id, role) for r in rows for role in sorted(set(r.roles))],
            )
            await cur.executemany(
                "INSERT INTO core.employee_groups (tenant_id, employee_id, group_name) "
                "VALUES (%s, %s, %s)",
                [(tenant_id, r.employee_id, g) for r in rows for g in sorted(set(r.groups))],
            )
        return LoadResult(len(rows), columns, warnings)

    async def _load_typed(
        self,
        conn: Any,
        tenant_id: str,
        records: Sequence[dict[str, Any]],
        model: type[BaseModel],
        table: str,
        key: str,
    ) -> LoadResult:
        rows, warnings = _validate(records, model)
        columns = list(model.model_fields)
        async with conn.cursor() as cur:
            await cur.executemany(
                _upsert_sql(table, columns, key),
                [(tenant_id, *[_value(getattr(r, c)) for c in columns]) for r in rows],
            )
        return LoadResult(len(rows), columns, warnings)

    async def _load_generic(
        self, conn: Any, tenant_id: str, dataset: str, records: Sequence[dict[str, Any]]
    ) -> LoadResult:
        columns = sorted({k for r in records for k in r})
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM core.dataset_rows WHERE tenant_id = %s AND dataset_name = %s",
                (tenant_id, dataset),
            )
            await cur.executemany(
                "INSERT INTO core.dataset_rows (tenant_id, dataset_name, row_number, data) "
                "VALUES (%s, %s, %s, %s::jsonb)",
                [
                    (tenant_id, dataset, i + 1, json.dumps(r, default=str))
                    for i, r in enumerate(records)
                ],
            )
        return LoadResult(len(records), columns)


def _validate(
    records: Sequence[dict[str, Any]], model: type[BaseModel]
) -> tuple[list[Any], list[str]]:
    rows: list[Any] = []
    warnings: list[str] = []
    for number, record in enumerate(records, start=1):
        try:
            rows.append(model.model_validate(record))
        except ValidationError as exc:
            warnings.append(f"row {number} skipped: {exc.error_count()} invalid field(s)")
    return rows, warnings[:20]


def _upsert_sql(table: str, columns: Sequence[str], key: str) -> str:
    # Table/column names come from fixed Pydantic models, never from input.
    cols = ", ".join(["tenant_id", *columns])
    placeholders = ", ".join(["%s"] * (len(columns) + 1))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in columns if c != key)
    return (
        f"INSERT INTO core.{table} ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT (tenant_id, {key}) DO UPDATE SET {updates}"
    )


def _value(value: Any) -> Any:
    return value.value if isinstance(value, Classification) else value
