"""Planner output and task contracts (§6.3)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import DataCategory, SourceNeed
from app.schemas.security import PolicyDecision, TaskAccessRequest

MAX_TASKS_PER_QUERY = 8


class Intent(StrEnum):
    FACT_LOOKUP = "fact_lookup"
    HOW_TO = "how_to"
    SUMMARIZE = "summarize"
    INCIDENT_INVESTIGATION = "incident_investigation"
    EXPERT_FINDER = "expert_finder"
    METRICS = "metrics"
    IMAGE_INTERPRETATION = "image_interpretation"
    COMPARISON = "comparison"
    ACTION_RECOMMENDATION = "action_recommendation"
    OUT_OF_SCOPE = "out_of_scope"


class PlannedTask(BaseModel):
    """One sub-question as produced by the planner LLM (validated, never trusted for access)."""

    task_id: str = Field(pattern=r"^t[1-9][0-9]?$")
    question: str = Field(min_length=1, max_length=1000)
    original_span: str = Field(default="", max_length=4000)
    intent: Intent
    data_categories: set[DataCategory] = Field(min_length=1)
    subject_refs: list[str] = Field(default_factory=list)
    requested_fields: list[str] = Field(default_factory=list)
    source_needs: set[SourceNeed] = Field(default_factory=set)
    depends_on: list[str] = Field(default_factory=list)
    retrieval_query: str | None = Field(default=None, max_length=1000)
    use_upload_ids: list[str] = Field(default_factory=list)


class PlannerOutput(BaseModel):
    tasks: list[PlannedTask] = Field(min_length=1, max_length=MAX_TASKS_PER_QUERY)

    @model_validator(mode="after")
    def _check_graph(self) -> PlannerOutput:
        ids = [task.task_id for task in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("task_id values must be unique")
        known = set(ids)
        for task in self.tasks:
            unknown = set(task.depends_on) - known
            if unknown:
                raise ValueError(f"{task.task_id} depends on unknown tasks {sorted(unknown)}")
            if task.task_id in task.depends_on:
                raise ValueError(f"{task.task_id} depends on itself")
        _assert_acyclic({task.task_id: task.depends_on for task in self.tasks})
        return self


def _assert_acyclic(edges: dict[str, list[str]]) -> None:
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            raise ValueError("depends_on contains a cycle")
        visiting.add(node)
        for dep in edges.get(node, []):
            visit(dep)
        visiting.discard(node)
        done.add(node)

    for node in edges:
        visit(node)


class TaskRoute(StrEnum):
    RETRIEVAL = "retrieval"
    SQL = "sql"
    BOTH = "both"
    NONE = "none"


class Task(BaseModel):
    """A planned task after the deterministic policy decision. Owned by the orchestrator."""

    plan: PlannedTask
    access_request: TaskAccessRequest
    decision: PolicyDecision
    route: TaskRoute

    @property
    def task_id(self) -> str:
        return self.plan.task_id


class RefinedQuery(BaseModel):
    """Planner.refine() output for the single retry. Cannot change scope or sources."""

    task_id: str
    retrieval_query: str | None = Field(default=None, max_length=1000)
    keywords: list[str] = Field(default_factory=list, max_length=12)
    sql_hint: str | None = Field(default=None, max_length=1000)
    rationale: str = Field(default="", max_length=1000)  # internal only
