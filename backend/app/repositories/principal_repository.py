"""Employee directory and identity data from Postgres (trusted backend reads)."""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.repositories.postgres_repository import Database
from app.schemas.common import Classification
from app.schemas.security import DemoUser, UserContext


class PostgresPrincipalRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_user_context(self, tenant_id: str, employee_id: str) -> UserContext | None:
        rows = await self._db.fetch_all(
            """
            SELECT e.employee_id, e.name, e.title, e.department, e.clearance,
                   coalesce((SELECT array_agg(role) FROM core.employee_roles r
                             WHERE r.tenant_id = e.tenant_id AND r.employee_id = e.employee_id),
                            '{}') AS roles,
                   coalesce((SELECT array_agg(group_name) FROM core.employee_groups g
                             WHERE g.tenant_id = e.tenant_id AND g.employee_id = e.employee_id),
                            '{}') AS groups,
                   coalesce((SELECT array_agg(d.employee_id) FROM core.employees d
                             WHERE d.tenant_id = e.tenant_id AND d.manager_id = e.employee_id),
                            '{}') AS reports
            FROM core.employees e
            WHERE e.tenant_id = %s AND e.employee_id = %s
            """,
            (tenant_id, employee_id),
        )
        if not rows:
            return None
        row = rows[0]
        return UserContext(
            tenant_id=tenant_id,
            employee_id=row["employee_id"],
            display_name=row["name"],
            title=row["title"] or "",
            roles=frozenset(row["roles"]),
            groups=frozenset(row["groups"]),
            department=row["department"] or "",
            clearance=Classification(row["clearance"]),
            direct_report_ids=frozenset(row["reports"]),
        )

    async def list_demo_users(self, tenant_id: str) -> list[DemoUser]:
        rows = await self._db.fetch_all(
            "SELECT employee_id, name, title, demo_access_summary FROM core.employees "
            "WHERE tenant_id = %s AND demo_access_summary IS NOT NULL ORDER BY employee_id",
            (tenant_id,),
        )
        return [
            DemoUser(
                tenant_id=tenant_id,
                employee_id=r["employee_id"],
                display_name=r["name"],
                title=r["title"] or "",
                access_summary=r["demo_access_summary"],
            )
            for r in rows
        ]

    async def resolve_people(self, user: UserContext, refs: Sequence[str]) -> dict[str, list[str]]:
        resolved: dict[str, list[str]] = {}
        for ref in refs:
            name = re.sub(r"'s$", "", ref.strip())
            if not name:
                resolved[ref] = []
                continue
            rows = await self._db.fetch_all(
                """
                SELECT employee_id FROM core.employees
                WHERE tenant_id = %(tenant)s
                  AND (lower(name) = lower(%(name)s)
                       OR lower(split_part(name, ' ', 1)) = lower(%(name)s)
                       OR lower(split_part(name, ' ', 2)) = lower(%(name)s)
                       OR lower(employee_id) = lower(%(name)s))
                ORDER BY employee_id
                """,
                {"tenant": user.tenant_id, "name": name},
            )
            resolved[ref] = [r["employee_id"] for r in rows]
        return resolved
