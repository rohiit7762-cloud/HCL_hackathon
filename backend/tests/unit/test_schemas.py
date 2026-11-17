from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.api import QueryRequest
from app.schemas.common import Classification, DataCategory, SourceNeed
from app.schemas.planning import Intent, PlannedTask, PlannerOutput
from app.schemas.security import AccessScope


def _task(task_id: str, depends_on: list[str] | None = None) -> PlannedTask:
    return PlannedTask(
        task_id=task_id,
        question=f"question {task_id}",
        intent=Intent.FACT_LOOKUP,
        data_categories={DataCategory.GENERAL_KNOWLEDGE},
        source_needs={SourceNeed.DOCUMENTS},
        depends_on=depends_on or [],
    )


class TestClassification:
    def test_ordering(self) -> None:
        assert Classification.CONFIDENTIAL.allows(Classification.INTERNAL)
        assert Classification.INTERNAL.allows(Classification.INTERNAL)
        assert not Classification.INTERNAL.allows(Classification.CONFIDENTIAL)


class TestPlannerOutput:
    def test_valid_dependency_graph(self) -> None:
        output = PlannerOutput(tasks=[_task("t1"), _task("t2", ["t1"])])
        assert [t.task_id for t in output.tasks] == ["t1", "t2"]

    def test_rejects_duplicate_ids(self) -> None:
        with pytest.raises(ValidationError, match="unique"):
            PlannerOutput(tasks=[_task("t1"), _task("t1")])

    def test_rejects_unknown_dependency(self) -> None:
        with pytest.raises(ValidationError, match="unknown"):
            PlannerOutput(tasks=[_task("t1", ["t9"])])

    def test_rejects_cycle(self) -> None:
        with pytest.raises(ValidationError, match="cycle"):
            PlannerOutput(tasks=[_task("t1", ["t2"]), _task("t2", ["t1"])])

    def test_rejects_too_many_tasks(self) -> None:
        with pytest.raises(ValidationError):
            PlannerOutput(tasks=[_task(f"t{i}") for i in range(1, 10)])


class TestAccessScope:
    def _scope(self, sources: list[str]) -> AccessScope:
        return AccessScope.create(
            tenant_id="lumen",
            authorized_source_ids=sources,
            max_classification=Classification.INTERNAL,
            allowed_views=["api.incidents"],
            allowed_columns={"api.incidents": ["id", "priority"]},
        )

    def test_hash_is_order_independent_and_stable(self) -> None:
        assert self._scope(["a", "b"]).scope_hash == self._scope(["b", "a"]).scope_hash

    def test_hash_changes_when_scope_broadens(self) -> None:
        assert self._scope(["a"]).scope_hash != self._scope(["a", "b"]).scope_hash

    def test_tampered_scope_is_detected(self) -> None:
        scope = self._scope(["a"])
        tampered = scope.model_copy(update={"authorized_source_ids": frozenset({"a", "x"})})
        assert scope.hash_is_consistent()
        assert not tampered.hash_is_consistent()

    def test_scope_is_immutable(self) -> None:
        scope = self._scope(["a"])
        with pytest.raises(ValidationError):
            scope.tenant_id = "orbit"  # type: ignore[misc]


class TestQueryRequest:
    def test_limits(self) -> None:
        with pytest.raises(ValidationError):
            QueryRequest(query="")
        with pytest.raises(ValidationError):
            QueryRequest(query="x" * 4001)
        with pytest.raises(ValidationError):
            QueryRequest(query="ok", upload_ids=["a", "b", "c", "d"])
