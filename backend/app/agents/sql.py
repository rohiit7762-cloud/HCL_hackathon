"""SQL Agent over approved Neon Postgres views (§8.10).

The LLM writes a StructuredQuerySpec, never SQL text. The spec is validated against
the task's AccessScope and the view catalog, compiled with psycopg.sql identifiers
and bound parameters, and executed as the read-only kb_query role with RLS context.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any, Protocol

from psycopg import sql

from app.agents.formatting import neutralize
from app.core.errors import AppError
from app.core.logging import get_logger
from app.prompts import UNTRUSTED_CONTENT_CLAUSE, PromptLibrary
from app.repositories.base import SourceRegistry
from app.schemas.interfaces import LLMProvider
from app.schemas.llm import ChatMessage, ModelRole
from app.schemas.security import AccessScope, UserContext
from app.schemas.tools import (
    AggFn,
    FilterOp,
    SQLRequest,
    SQLResult,
    StructuredProvenance,
    StructuredQuerySpec,
    ToolFailure,
)
from app.security.view_catalog import VIEWS, ViewSpec

logger = get_logger(__name__)

AGGREGATION_REFUSAL = (
    "I can't provide that figure — it could reveal information about individual employees."
)
INVALID_SPEC_MESSAGE = "I couldn't turn this part into a valid query over the available data."
UNAVAILABLE_MESSAGE = "The structured data source is unavailable right now."
NO_VIEW_MESSAGE = "There is no structured data you can access for this part."

_OPERATORS = {
    FilterOp.EQ: "=",
    FilterOp.NEQ: "<>",
    FilterOp.LT: "<",
    FilterOp.LTE: "<=",
    FilterOp.GT: ">",
    FilterOp.GTE: ">=",
}
_NUMERIC_AGGS = {AggFn.SUM, AggFn.AVG}


class RestrictedExecutor(Protocol):
    async def run_restricted(
        self,
        tenant_id: str,
        employee_id: str,
        statement: sql.Composed,
        params: Sequence[Any],
        *,
        timeout_ms: int,
        max_rows: int,
    ) -> tuple[list[dict[str, Any]], bool]: ...


def aggregate_alias(fn: AggFn, column: str) -> str:
    return f"{fn.value}_{'all' if column == '*' else column}"


def validate_spec(spec: StructuredQuerySpec, scope: AccessScope) -> list[str]:
    """Return human-readable errors; empty when the spec is safe to compile."""
    errors: list[str] = []
    view = VIEWS.get(spec.view)
    if view is None or spec.view not in scope.allowed_views:
        return [f"view '{spec.view}' is not available; choose from {sorted(scope.allowed_views)}"]
    allowed = scope.allowed_columns.get(spec.view, frozenset()) & view.column_names

    def check(column: str, where: str) -> None:
        if column not in allowed:
            errors.append(f"{where}: column '{column}' is not available in {spec.view}")

    for column in spec.select:
        check(column, "select")
    for column in spec.group_by:
        check(column, "group_by")
    aliases = set()
    for fn, column in spec.aggregates:
        if column == "*":
            if fn != AggFn.COUNT:
                errors.append(f"aggregate: only count may use '*', not {fn.value}")
        else:
            check(column, "aggregate")
            col = view.column(column)
            if col and fn in _NUMERIC_AGGS and col.pg_type not in {"numeric", "integer"}:
                errors.append(f"aggregate: {fn.value} needs a numeric column, '{column}' is not")
        aliases.add(aggregate_alias(fn, column))
    if spec.aggregates and set(spec.select) - set(spec.group_by):
        errors.append("select: with aggregates, plain columns must also be in group_by")
    if spec.group_by and not spec.aggregates:
        errors.append("group_by requires at least one aggregate")
    for column, op, value in spec.filters:
        check(column, "filter")
        col = view.column(column)
        if col and col.pg_type == "jsonb":
            errors.append(f"filter: cannot filter on JSON column '{column}'")
        if op == FilterOp.IN and not (isinstance(value, list) and 0 < len(value) <= 50):
            errors.append(f"filter: 'in' on '{column}' needs a list of 1-50 values")
        elif op == FilterOp.BETWEEN and not (isinstance(value, list) and len(value) == 2):
            errors.append(f"filter: 'between' on '{column}' needs [low, high]")
        elif op not in {FilterOp.IN, FilterOp.BETWEEN} and isinstance(value, list | dict):
            errors.append(f"filter: '{op.value}' on '{column}' needs a single value")
    output_names = set(spec.select) | set(spec.group_by) | aliases
    for column, _ in spec.order_by:
        if column not in output_names:
            errors.append(f"order_by: '{column}' must be a selected column or aggregate alias")
    return errors


def compile_spec(
    spec: StructuredQuerySpec, view: ViewSpec, allowed: frozenset[str], limit: int
) -> tuple[sql.Composed, list[Any]]:
    """Compile a validated spec into a parameterized SELECT."""
    params: list[Any] = []
    schema, name = view.name.split(".", 1)
    projections: list[sql.Composable] = []
    if spec.aggregates:
        projections += [sql.Identifier(c) for c in spec.group_by]
        for fn, column in spec.aggregates:
            target = sql.SQL("*") if column == "*" else sql.Identifier(column)
            expr = sql.SQL("{}({})").format(sql.SQL(fn.value.upper()), target)
            if fn == AggFn.AVG:
                expr = sql.SQL("round(({})::numeric, 2)").format(expr)
            projections.append(
                sql.SQL("{} AS {}").format(expr, sql.Identifier(aggregate_alias(fn, column)))
            )
    else:
        columns = spec.select or [c.name for c in view.columns if c.name in allowed]
        projections += [sql.Identifier(c) for c in columns]

    query = sql.SQL("SELECT {} FROM {}").format(
        sql.SQL(", ").join(projections), sql.Identifier(schema, name)
    )
    where = _where_clause(spec, view, params)
    if where is not None:
        query += sql.SQL(" WHERE ") + where
    if spec.group_by:
        query += sql.SQL(" GROUP BY ") + sql.SQL(", ").join(
            sql.Identifier(c) for c in spec.group_by
        )
    if spec.order_by:
        query += sql.SQL(" ORDER BY ") + sql.SQL(", ").join(
            sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(d.upper())) for c, d in spec.order_by
        )
    query += sql.SQL(" LIMIT {}").format(sql.Literal(min(spec.limit, limit)))
    return query, params


def _where_clause(
    spec: StructuredQuerySpec, view: ViewSpec, params: list[Any]
) -> sql.Composable | None:
    clauses: list[sql.Composable] = []
    for column, op, value in spec.filters:
        col = view.column(column)
        assert col is not None
        ident = sql.Identifier(column)
        cast = sql.SQL(col.pg_type)
        if op == FilterOp.IN:
            params.append([_coerce(v, col.pg_type) for v in value])
            clauses.append(sql.SQL("{} = ANY(%s::{}[])").format(ident, cast))
        elif op == FilterOp.BETWEEN:
            params.extend(_coerce(v, col.pg_type) for v in value)
            clauses.append(sql.SQL("{} BETWEEN %s::{} AND %s::{}").format(ident, cast, cast))
        elif op == FilterOp.ILIKE:
            text = str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{text}%")
            clauses.append(sql.SQL("{}::text ILIKE %s").format(ident))
        else:
            params.append(_coerce(value, col.pg_type))
            clauses.append(sql.SQL("{} {} %s::{}").format(ident, sql.SQL(_OPERATORS[op]), cast))
    return sql.SQL(" AND ").join(clauses) if clauses else None


def _coerce(value: Any, pg_type: str) -> Any:
    if value is None:
        return None
    if pg_type in {"numeric", "integer"} and isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value
    if pg_type in {"timestamptz", "date"}:
        return str(value)
    return value if isinstance(value, int | float | bool) else str(value)


class SQLAgent:
    def __init__(
        self,
        llm: LLMProvider,
        executor: RestrictedExecutor,
        registry: SourceRegistry,
        prompts: PromptLibrary,
        *,
        timeout_ms: int,
        max_rows: int,
        min_aggregation_group: int,
    ) -> None:
        self._llm = llm
        self._executor = executor
        self._registry = registry
        self._prompts = prompts
        self._timeout_ms = timeout_ms
        self._max_rows = max_rows
        self._min_group = min_aggregation_group

    async def query(
        self, user: UserContext, scope: AccessScope, request: SQLRequest
    ) -> SQLResult | ToolFailure:
        views = [VIEWS[v] for v in sorted(scope.allowed_views) if v in VIEWS]
        if not views:
            return self._failure(request, "no_permitted_view", NO_VIEW_MESSAGE)

        messages = [
            ChatMessage(
                role="system",
                content=self._prompts.render(
                    "sql_spec",
                    untrusted_clause=UNTRUSTED_CONTENT_CLAUSE,
                    today=date.today().isoformat(),
                    catalog=_catalog_text(views, scope),
                    question=neutralize(request.question),
                    hint=f"Hint: {neutralize(request.hint)}" if request.hint else "",
                ),
            )
        ]
        spec = await self._ask(messages)
        errors = validate_spec(spec, scope) if spec else ["no valid query was produced"]
        if errors:
            retry = [
                *messages,
                ChatMessage(
                    role="user",
                    content="That query is invalid:\n- "
                    + "\n- ".join(errors)
                    + "\nReturn a corrected JSON object.",
                ),
            ]
            spec = await self._ask(retry)
            errors = validate_spec(spec, scope) if spec else ["no valid query was produced"]
        if errors or spec is None:
            logger.info(
                "sql_spec_invalid", extra={"task_id": request.task_id, "errors": len(errors)}
            )
            return self._failure(request, "invalid_spec", INVALID_SPEC_MESSAGE)

        view = VIEWS[spec.view]
        allowed = scope.allowed_columns.get(spec.view, frozenset())
        try:
            if await self._small_sensitive_group(user, spec, view):
                return self._failure(request, "no_permitted_view", AGGREGATION_REFUSAL)
            statement, params = compile_spec(spec, view, allowed, self._max_rows)
            rows, truncated = await self._executor.run_restricted(
                user.tenant_id,
                user.employee_id,
                statement,
                params,
                timeout_ms=self._timeout_ms,
                max_rows=min(spec.limit, self._max_rows),
            )
            dataset_ids = await self._registry.dataset_source_ids(user.tenant_id, view.datasets)
        except Exception as exc:  # database errors become a safe tool failure
            logger.warning(
                "sql_execution_failed",
                extra={"task_id": request.task_id, "exc_type": type(exc).__name__},
            )
            timed_out = "timeout" in type(exc).__name__.lower() or "cancel" in str(exc).lower()
            code = "timeout" if timed_out else "unavailable"
            return self._failure(request, code, UNAVAILABLE_MESSAGE)

        columns = list(rows[0].keys()) if rows else _output_columns(spec, view, allowed)
        return SQLResult(
            task_id=request.task_id,
            spec=spec,
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            provenance=StructuredProvenance(
                view=spec.view,
                dataset_source_ids=dataset_ids,
                query_fingerprint=hashlib.sha256(
                    json.dumps(spec.model_dump(mode="json"), sort_keys=True).encode()
                ).hexdigest(),
                executed_at=datetime.now(UTC),
            ),
        )

    async def _ask(self, messages: list[ChatMessage]) -> StructuredQuerySpec | None:
        try:
            return await self._llm.complete_structured(
                messages, StructuredQuerySpec, role=ModelRole.SQL_SPEC
            )
        except AppError:
            return None

    async def _small_sensitive_group(
        self, user: UserContext, spec: StructuredQuerySpec, view: ViewSpec
    ) -> bool:
        """Rule 4: aggregates over personal fields need groups of >= min size."""
        sensitive = any(
            column != "*" and (col := view.column(column)) is not None and col.sensitive
            for _, column in spec.aggregates
        )
        if not sensitive:
            return False
        count_spec = spec.model_copy(
            update={
                "select": list(spec.group_by),
                "aggregates": [(AggFn.COUNT, "*")],
                "order_by": [],
                "limit": 200,
            }
        )
        statement, params = compile_spec(count_spec, view, view.column_names, 200)
        rows, _ = await self._executor.run_restricted(
            user.tenant_id,
            user.employee_id,
            statement,
            params,
            timeout_ms=self._timeout_ms,
            max_rows=200,
        )
        counts = [int(r.get("count_all", 0)) for r in rows] or [0]
        return min(counts) < self._min_group

    @staticmethod
    def _failure(request: SQLRequest, code: Any, message: str) -> ToolFailure:
        return ToolFailure(task_id=request.task_id, code=code, safe_message=message)


def _catalog_text(views: Sequence[ViewSpec], scope: AccessScope) -> str:
    blocks = []
    for view in views:
        allowed = scope.allowed_columns.get(view.name, frozenset())
        columns = "\n".join(
            f"  - {c.name} ({c.pg_type}): {c.description}"
            for c in view.columns
            if c.name in allowed
        )
        blocks.append(f"### {view.name}\n{view.description}\nColumns:\n{columns}")
    return "\n\n".join(blocks)


def _output_columns(
    spec: StructuredQuerySpec, view: ViewSpec, allowed: frozenset[str]
) -> list[str]:
    if spec.aggregates:
        return [*spec.group_by, *(aggregate_alias(f, c) for f, c in spec.aggregates)]
    return spec.select or [c.name for c in view.columns if c.name in allowed]
