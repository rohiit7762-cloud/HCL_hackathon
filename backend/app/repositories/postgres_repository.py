"""Neon Postgres access: two pools with different roles.

- `service` pool (kb_ingest): trusted backend reads/writes (registry, directory,
  audit, ingestion). Never used for LLM-derived queries.
- `query` pool (kb_query): read-only, may only SELECT from api.* views. Every
  query runs in a transaction with tenant/employee context and a statement timeout.

Neon's pooled endpoints use PgBouncer in transaction mode, so prepared statements
are disabled and session settings are always SET LOCAL / set_config(..., true).
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.core.logging import get_logger

logger = get_logger(__name__)

Query = str | sql.SQL | sql.Composed


def use_selector_event_loop_on_windows() -> None:
    """psycopg's async driver needs a selector event loop on Windows."""
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class Database:
    def __init__(self, service_url: str, query_url: str, *, max_size: int = 5) -> None:
        if not service_url or not query_url:
            raise RuntimeError(
                "NEON_INGEST_URL and NEON_QUERY_URL must be set. Run scripts/setup_db.py first."
            )
        kwargs: dict[str, Any] = {"prepare_threshold": None}
        # Neon closes idle connections when its compute suspends, so every connection is
        # checked before use and idle ones are recycled well before Neon drops them.
        pool_options: dict[str, Any] = {
            "open": False,
            "min_size": 1,
            "max_size": max_size,
            "kwargs": kwargs,
            "check": AsyncConnectionPool.check_connection,
            "max_idle": 120,
            "reconnect_timeout": 30,
        }
        self.service = AsyncConnectionPool(service_url, **pool_options)
        self.query = AsyncConnectionPool(query_url, **pool_options)

    async def open(self) -> None:
        await self.service.open(wait=True, timeout=30)
        await self.query.open(wait=True, timeout=30)

    async def close(self) -> None:
        await self.query.close()
        await self.service.close()

    async def fetch_all(
        self, query: Query, params: Sequence[Any] | dict[str, Any] = ()
    ) -> list[dict[str, Any]]:
        async with self.service.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(query, params)
            return list(await cur.fetchall()) if cur.description else []

    async def execute(self, query: Query, params: Sequence[Any] | dict[str, Any] = ()) -> None:
        async with self.service.connection() as conn:
            await conn.execute(query, params)

    async def run_restricted(
        self,
        tenant_id: str,
        employee_id: str,
        statement: sql.Composed,
        params: Sequence[Any],
        *,
        timeout_ms: int,
        max_rows: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Run one compiled SELECT as kb_query with RLS context. Returns (rows, truncated)."""
        async with (
            self.query.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT set_config('app.tenant_id', %s, true), "
                "set_config('app.employee_id', %s, true)",
                (tenant_id, employee_id),
            )
            await cur.execute(
                sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(int(timeout_ms)))
            )
            await cur.execute(statement, params)
            rows = await cur.fetchmany(max_rows + 1)
        truncated = len(rows) > max_rows
        return [_json_safe_row(r) for r in rows[:max_rows]], truncated


def _json_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _json_safe(value) for key, value in row.items()}


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value
