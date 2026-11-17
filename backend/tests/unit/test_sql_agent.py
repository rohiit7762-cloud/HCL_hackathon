"""SQL Agent: spec validation, safe compilation, aggregation guard, never raw SQL."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from psycopg import sql

from app.agents.sql import AGGREGATION_REFUSAL, SQLAgent, compile_spec, validate_spec
from app.prompts import PromptLibrary
from app.schemas.common import Classification
from app.schemas.llm import ModelRole
from app.schemas.security import AccessScope
from app.schemas.tools import (
    AggFn,
    FilterOp,
    SQLRequest,
    SQLResult,
    StructuredQuerySpec,
    ToolFailure,
)
from app.security.view_catalog import EMPLOYEE_HR, INCIDENTS, MANAGER_VISIBLE_COLUMNS
from tests.fakes.data_layer import ASHA, MORGAN, InMemoryRegistry
from tests.fakes.llm import FakeLLMProvider


def scope(views: dict[str, frozenset[str]]) -> AccessScope:
    return AccessScope.create(
        tenant_id="lumen",
        authorized_source_ids=[],
        max_classification=Classification.INTERNAL,
        allowed_views=views.keys(),
        allowed_columns=views,
    )


OPS = scope({INCIDENTS.name: INCIDENTS.column_names})


def render(statement: sql.Composed) -> str:
    return statement.as_string(None)


def test_valid_spec_compiles_to_parameterized_sql() -> None:
    spec = StructuredQuerySpec(
        view="api.incidents",
        aggregates=[(AggFn.COUNT, "*")],
        filters=[
            ("priority", FilterOp.EQ, "P1"),
            ("service", FilterOp.ILIKE, "payments"),
            ("created_at", FilterOp.BETWEEN, ["2026-10-01", "2026-11-01"]),
        ],
    )
    assert validate_spec(spec, OPS) == []
    statement, params = compile_spec(spec, INCIDENTS, INCIDENTS.column_names, 200)
    text = render(statement)
    assert 'FROM "api"."incidents"' in text
    assert 'COUNT(*) AS "count_all"' in text
    assert "P1" not in text and "payments" not in text  # values are bound, not inlined
    assert params == ["P1", "%payments%", "2026-10-01", "2026-11-01"]


def test_injection_in_values_is_bound_not_executed() -> None:
    spec = StructuredQuerySpec(
        view="api.incidents", filters=[("title", FilterOp.EQ, "x'; DROP TABLE core.incidents;--")]
    )
    statement, params = compile_spec(spec, INCIDENTS, INCIDENTS.column_names, 50)
    assert "DROP" not in render(statement)
    assert params == ["x'; DROP TABLE core.incidents;--"]


@pytest.mark.parametrize(
    ("spec", "error"),
    [
        (StructuredQuerySpec(view="core.employees"), "not available"),
        (StructuredQuerySpec(view="api.employee_hr"), "not available"),
        (StructuredQuerySpec(view="api.incidents", select=["salary"]), "column 'salary'"),
        (StructuredQuerySpec(view="api.incidents", aggregates=[(AggFn.SUM, "title")]), "numeric"),
        (
            StructuredQuerySpec(
                view="api.incidents", select=["service"], aggregates=[(AggFn.COUNT, "*")]
            ),
            "group_by",
        ),
        (StructuredQuerySpec(view="api.incidents", order_by=[("evil; --", "asc")]), "order_by"),
    ],
)
def test_invalid_specs_are_rejected(spec: StructuredQuerySpec, error: str) -> None:
    errors = validate_spec(spec, OPS)
    assert errors and any(error in e for e in errors)


def test_manager_scope_blocks_salary_column() -> None:
    manager = scope({EMPLOYEE_HR.name: MANAGER_VISIBLE_COLUMNS})
    spec = StructuredQuerySpec(view="api.employee_hr", select=["name", "salary"])
    assert any("salary" in e for e in validate_spec(spec, manager))


class RecordingExecutor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[str] = []

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
        self.statements.append(render(statement))
        return self.rows, False


def agent(llm: FakeLLMProvider, executor: RecordingExecutor) -> SQLAgent:
    return SQLAgent(
        llm,
        executor,
        InMemoryRegistry(),
        PromptLibrary(),
        timeout_ms=1000,
        max_rows=200,
        min_aggregation_group=5,
    )


async def test_agent_reasks_once_then_fails_safely() -> None:
    llm = FakeLLMProvider().default(ModelRole.SQL_SPEC, {"view": "core.employees"})
    executor = RecordingExecutor([])
    result = await agent(llm, executor).query(
        ASHA, OPS, SQLRequest(task_id="t1", question="list employees", attempt=1)
    )
    assert isinstance(result, ToolFailure) and result.code == "invalid_spec"
    assert len(llm.calls_for(ModelRole.SQL_SPEC)) == 2
    assert executor.statements == []


async def test_agent_returns_rows_with_provenance() -> None:
    llm = FakeLLMProvider().default(
        ModelRole.SQL_SPEC,
        {
            "view": "api.incidents",
            "aggregates": [["count", "*"]],
            "filters": [["priority", "eq", "P1"]],
        },
    )
    executor = RecordingExecutor([{"count_all": 3}])
    result = await agent(llm, executor).query(
        ASHA, OPS, SQLRequest(task_id="t1", question="How many P1s?", attempt=1)
    )
    assert isinstance(result, SQLResult)
    assert result.rows == [{"count_all": 3}]
    assert result.provenance.view == "api.incidents"
    assert len(result.provenance.query_fingerprint) == 64


async def test_small_group_salary_aggregate_is_refused() -> None:
    hr_scope = scope({EMPLOYEE_HR.name: EMPLOYEE_HR.column_names})
    llm = FakeLLMProvider().default(
        ModelRole.SQL_SPEC,
        {
            "view": "api.employee_hr",
            "aggregates": [["avg", "salary"]],
            "filters": [["team", "eq", "finance"]],
        },
    )
    executor = RecordingExecutor([{"count_all": 2}])
    result = await agent(llm, executor).query(
        MORGAN, hr_scope, SQLRequest(task_id="t1", question="avg finance salary", attempt=1)
    )
    assert isinstance(result, ToolFailure)
    assert result.safe_message == AGGREGATION_REFUSAL
    assert len(executor.statements) == 1  # only the COUNT pre-check ran
