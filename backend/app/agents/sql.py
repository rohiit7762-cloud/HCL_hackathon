from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from psycopg import sql

from app.core.config import Settings
from app.schemas.interfaces import LLMProvider
from app.schemas.llm import ChatMessage, ModelRole
from app.schemas.security import AccessScope, UserContext
from app.schemas.tools import (
    SQLRequest,
    SQLResult,
    StructuredProvenance,
    StructuredQuerySpec,
    ToolFailure,
)


class SQLAgent:
    def __init__(
        self, llm: LLMProvider, settings: Settings, view_columns: dict[str, dict[str, str]]
    ):
        self.llm, self.settings, self.view_columns = llm, settings, view_columns

    def _validate(self, spec: StructuredQuerySpec, scope: AccessScope) -> None:
        if spec.view not in scope.allowed_views:
            raise ValueError("view is outside access scope")
        allowed = scope.allowed_columns.get(spec.view, frozenset())
        refs = (
            set(spec.select)
            | set(spec.group_by)
            | {c for c, _, _ in spec.filters}
            | {c for _, c in spec.aggregates if c != "*"}
            | {c for c, _ in spec.order_by}
        )
        if not refs.issubset(allowed):
            raise ValueError("column is outside access scope")
        if spec.limit > 200:
            raise ValueError("limit exceeds policy")
        known = self.view_columns.get(spec.view, {})
        if not refs.issubset(known):
            raise ValueError("unknown column")

    def _compile(self, spec: StructuredQuerySpec):
        cols = [sql.Identifier(c) for c in spec.select]
        for fn, col in spec.aggregates:
            cols.append(
                sql.SQL("{}({}) AS {}").format(
                    sql.SQL(fn.value),
                    sql.Identifier(col) if col != "*" else sql.SQL("*"),
                    sql.Identifier(f"{fn.value}_{col}"),
                )
            )
        if not cols:
            raise ValueError("select or aggregate required")
        query = sql.SQL("SELECT {} FROM api.{}").format(
            sql.SQL(", ").join(cols), sql.Identifier(spec.view)
        )
        clauses = []
        params = []
        for col, op, val in spec.filters:
            if op.value == "between":
                if not isinstance(val, (list, tuple)) or len(val) != 2:
                    raise ValueError("between requires two values")
                clauses.append(sql.SQL("{} BETWEEN %s AND %s").format(sql.Identifier(col)))
                params.extend(val)
            elif op.value == "in":
                if not isinstance(val, (list, tuple)) or not val:
                    raise ValueError("in requires values")
                clauses.append(sql.SQL("{} = ANY(%s)").format(sql.Identifier(col)))
                params.append(list(val))
            else:
                opmap = {
                    "eq": "=",
                    "neq": "<>",
                    "lt": "<",
                    "lte": "<=",
                    "gt": ">",
                    "gte": ">=",
                    "ilike": "ILIKE",
                }
                clauses.append(
                    sql.SQL("{} {} %s").format(sql.Identifier(col), sql.SQL(opmap[op.value]))
                )
                params.append(val)
        if clauses:
            query += sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
        if spec.group_by:
            query += sql.SQL(" GROUP BY ") + sql.SQL(", ").join(map(sql.Identifier, spec.group_by))
        if spec.order_by:
            query += sql.SQL(" ORDER BY ") + sql.SQL(", ").join(
                sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(d.upper()))
                for c, d in spec.order_by
            )
        query += sql.SQL(" LIMIT %s")
        params.append(min(spec.limit, self.settings.sql_max_rows) + 1)
        return query, params

    async def query(
        self, user: UserContext, scope: AccessScope, request: SQLRequest
    ) -> SQLResult | ToolFailure:
        try:
            schema = StructuredQuerySpec
            messages = [
                ChatMessage(
                    role="user",
                    content=f"Question: {request.question}\nHint: {request.hint or ''}\nAllowed views: {sorted(scope.allowed_views)}\nAllowed columns: {dict(scope.allowed_columns)}\nReturn only a StructuredQuerySpec. Never emit SQL.",
                )
            ]
            errors = []
            for attempt in range(2):
                spec = await self.llm.complete_structured(messages, schema, role=ModelRole.SQL_SPEC)
                try:
                    self._validate(spec, scope)
                    query, params = self._compile(spec)
                    break
                except (ValueError, KeyError) as exc:
                    errors.append(str(exc))
                    messages.append(ChatMessage(role="assistant", content=spec.model_dump_json()))
                    messages.append(
                        ChatMessage(
                            role="user",
                            content=f"Invalid query spec: {exc}. Correct it using only the allowed schema.",
                        )
                    )
            else:
                return ToolFailure(
                    task_id=request.task_id,
                    code="invalid_spec",
                    safe_message="The structured query could not be validated.",
                )
            import psycopg
            from psycopg.rows import dict_row

            async with await psycopg.AsyncConnection.connect(
                self.settings.neon_query_url.get_secret_value(),
                connect_timeout=5,
                row_factory=dict_row,
            ) as conn:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('app.tenant_id',%s,true),set_config('app.employee_id',%s,true)",
                        (user.tenant_id, user.employee_id),
                    )
                    await conn.execute(
                        "SELECT set_config('statement_timeout',%s,true)",
                        (str(self.settings.sql_timeout_ms),),
                    )
                    fingerprint = hashlib.sha256(query.as_string(conn).encode()).hexdigest()
                    cur = await conn.execute(query, params)
                    rows = await cur.fetchmany(min(spec.limit, self.settings.sql_max_rows) + 1)
                    columns = [d.name for d in cur.description]
                    truncated = len(rows) > min(spec.limit, self.settings.sql_max_rows)
                    rows = rows[: min(spec.limit, self.settings.sql_max_rows)]
                    source_ids = sorted({str(r["source_id"]) for r in rows if r.get("source_id")})
            return SQLResult(
                task_id=request.task_id,
                spec=spec,
                columns=columns,
                rows=rows,
                row_count=len(rows),
                truncated=truncated,
                provenance=StructuredProvenance(
                    view=spec.view,
                    dataset_source_ids=source_ids,
                    query_fingerprint=fingerprint,
                    executed_at=datetime.now(UTC),
                ),
            )
        except Exception:
            return ToolFailure(
                task_id=request.task_id,
                code="unavailable",
                safe_message="Structured data is temporarily unavailable.",
            )
