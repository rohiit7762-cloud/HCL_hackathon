from __future__ import annotations

import csv
import io
import json
import uuid

from app.core.config import Settings
from app.schemas.ingestion import SourceMetadata
from app.schemas.security import UserContext


class StructuredLoader:
    """Transactional CSV/JSON loader; identifiers come only from the fixed table map."""

    TABLES = {
        "employees": (
            "core.employees",
            {
                "employee_id",
                "name",
                "work_email",
                "title",
                "department",
                "manager_id",
                "location",
                "expertise",
                "hire_date",
                "salary",
                "performance_rating",
                "personal_phone",
                "home_city",
                "clearance",
            },
        ),
        "incidents": (
            "core.incidents",
            {
                "id",
                "priority",
                "status",
                "service",
                "title",
                "summary",
                "created_at",
                "resolved_at",
                "assignee_id",
                "root_cause_category",
            },
        ),
        "services": ("core.services", {"id", "name", "owner_team", "tier", "oncall_group"}),
    }

    def __init__(self, settings: Settings):
        self.settings = settings

    async def load(
        self,
        filename: str,
        data: bytes,
        user: UserContext,
        source: SourceMetadata,
        dataset: str | None,
    ) -> int:
        import psycopg

        name = dataset or filename.rsplit(".", 1)[0].lower()
        if name not in self.TABLES:
            name = "custom_" + name
        if filename.lower().endswith(".csv"):
            rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
        else:
            raw = json.loads(data)
            rows = raw if isinstance(raw, list) else [raw]
        if not rows or not all(isinstance(r, dict) for r in rows):
            raise ValueError("Structured data must contain a non-empty list of objects")
        if name in self.TABLES:
            table, allowed = self.TABLES[name]
            fields = sorted(set.intersection(*(set(r) for r in rows)) & allowed)
            if not fields:
                raise ValueError("No recognized fields in structured dataset")
            if (
                name in {"employees", "incidents", "services"}
                and "employee_id" not in fields
                and name == "employees"
            ):
                raise ValueError("employees dataset requires employee_id")
            if name in {"incidents", "services"} and "id" not in fields:
                raise ValueError(f"{name} dataset requires id")
            sql = (
                f"INSERT INTO {table} (tenant_id,{','.join(fields)}) VALUES (%s,{','.join(['%s'] * len(fields))}) ON CONFLICT (tenant_id,{('employee_id' if name == 'employees' else 'id')}) DO UPDATE SET "
                + ",".join(f"{f}=EXCLUDED.{f}" for f in fields if f not in {"id", "employee_id"})
            )
            values = [(user.tenant_id, *[r.get(f) for f in fields]) for r in rows]
        else:
            table = "core.dataset_rows"
            sql = "INSERT INTO core.dataset_rows(tenant_id,dataset_name,row_id,data,source_id) VALUES(%s,%s,%s,%s::jsonb,%s) ON CONFLICT DO NOTHING"
            values = [
                (
                    user.tenant_id,
                    name,
                    str(r.get("id") or r.get("employee_id") or uuid.uuid4()),
                    json.dumps(r),
                    source.source_id,
                )
                for r in rows
            ]
        async with await psycopg.AsyncConnection.connect(
            self.settings.neon_ingest_url.get_secret_value(), connect_timeout=5
        ) as conn:
            async with conn.transaction():
                await conn.execute("SELECT set_config('app.tenant_id',%s,true)", (user.tenant_id,))
                await conn.execute(
                    "INSERT INTO core.sources(tenant_id,source_id,title,kind,mime_type,classification,allowed_groups,allowed_roles,contains_personal_data,contains_confidential_business,subject_employee_ids,checksum_sha256,version,blob_path,ingested_by,ingested_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        source.tenant_id,
                        source.source_id,
                        source.title,
                        source.kind.value,
                        source.mime_type,
                        source.classification.value,
                        list(source.allowed_groups),
                        list(source.allowed_roles),
                        source.contains_personal_data,
                        source.contains_confidential_business,
                        list(source.subject_employee_ids),
                        source.checksum_sha256,
                        source.version,
                        source.blob_path,
                        source.ingested_by,
                        source.ingested_at,
                    ),
                )
                if name not in self.TABLES:
                    schema = sorted({k for row in rows for k in row})
                    await conn.execute(
                        "INSERT INTO core.datasets(tenant_id,dataset_name,classification,allowed_groups,columns,source_id) VALUES(%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT DO NOTHING",
                        (
                            user.tenant_id,
                            name,
                            source.classification.value,
                            list(source.allowed_groups),
                            json.dumps({k: "text" for k in schema}),
                            source.source_id,
                        ),
                    )
                await conn.executemany(sql, values)
        return len(rows)
