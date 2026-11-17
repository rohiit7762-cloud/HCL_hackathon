"""Per-task LangGraph subgraph: execute_tools → evidence_check → [refine_query → execute_tools].

The retry decision is an explicit conditional edge. MAX_RETRIEVAL_ATTEMPTS is
enforced there and asserted again before every tool call (invariant I6). The
access scope always comes from the task's PolicyDecision, never from the LLM.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph

from app.agents.evidence import EvidenceChecker
from app.agents.planner import Planner
from app.core.logging import get_logger
from app.graph import progress
from app.graph.state import (
    MAX_RETRIEVAL_ATTEMPTS,
    OutcomeStatus,
    TaskOutcome,
    TaskRunState,
)
from app.schemas.common import Classification, Modality, SourceKind, SourceNeed
from app.schemas.evidence import (
    Evidence,
    EvidenceAssessment,
    EvidenceKind,
    EvidenceLocation,
    EvidenceStatus,
)
from app.schemas.interfaces import PolicyService, RetrievalTool, SQLTool
from app.schemas.planning import Intent, Task, TaskRoute
from app.schemas.security import AccessScope, UserContext
from app.schemas.tools import (
    NON_RETRYABLE_FAILURES,
    Attempt,
    RetrievalRequest,
    SQLRequest,
    SQLResult,
    ToolFailure,
)

logger = get_logger(__name__)

TIMEOUT_MESSAGE = "This part took too long to look up. Please try again."
UNAVAILABLE_MESSAGE = "A data source was unavailable while answering this part."
MAX_TABLE_ROWS_IN_EXCERPT = 20


class ScopeViolationError(RuntimeError):
    """Raised if a task would run without, or with a changed, access scope."""


class TaskWorkflow:
    def __init__(
        self,
        planner: Planner,
        checker: EvidenceChecker,
        retrieval: RetrievalTool,
        sql: SQLTool,
        policy: PolicyService,
    ) -> None:
        self._planner = planner
        self._checker = checker
        self._retrieval = retrieval
        self._sql = sql
        self._policy = policy
        self._graph = self._build()

    def _build(self) -> Any:
        graph = StateGraph(TaskRunState)
        graph.add_node("execute_tools", self._execute_tools)
        graph.add_node("evidence_check", self._evidence_check)
        graph.add_node("refine_query", self._refine_query)
        graph.add_edge(START, "execute_tools")
        graph.add_edge("execute_tools", "evidence_check")
        graph.add_conditional_edges(
            "evidence_check", self._after_check, {"retry": "refine_query", "done": END}
        )
        graph.add_edge("refine_query", "execute_tools")
        return graph.compile()

    async def run(self, state: TaskRunState) -> tuple[TaskOutcome, list[Any]]:
        """Run the subgraph; return the task outcome and its progress events."""
        task = state["task"]
        initial: TaskRunState = {
            **state,
            "attempt": 0,
            "query": task.plan.retrieval_query or task.plan.question,
            "keywords": state.get("keywords", []),
            "sql_hint": None,
            "evidence": [],
            "failures": [],
            "warnings": [],
            "assessment": None,
            "tool_calls": 0,
            "progress": [],
        }
        final: TaskRunState = await self._graph.ainvoke(initial)
        return self._outcome(final), list(final.get("progress", []))

    # --- nodes -----------------------------------------------------------
    async def _execute_tools(self, state: TaskRunState) -> dict[str, Any]:
        task = state["task"]
        attempt = state.get("attempt", 0) + 1
        if attempt > MAX_RETRIEVAL_ATTEMPTS:  # defensive: the edge should prevent this
            raise AssertionError(f"retrieval attempt {attempt} exceeds cap for {task.task_id}")
        scope = _require_scope(task)

        events = list(state.get("progress", []))
        remaining = state["deadline"] - time.monotonic()
        if remaining <= 0:
            failure = ToolFailure(
                task_id=task.task_id, code="timeout", safe_message=TIMEOUT_MESSAGE
            )
            return {
                "attempt": attempt,
                "failures": [*state["failures"], failure],
                "progress": events,
            }

        calls: list[Any] = []
        if task.route in {TaskRoute.RETRIEVAL, TaskRoute.BOTH}:
            events.append(progress.retrieval_started(task.task_id))
            calls.append(self._call_retrieval(state, scope, attempt))
        if task.route in {TaskRoute.SQL, TaskRoute.BOTH}:
            events.append(progress.sql_started(task.task_id))
            calls.append(self._call_sql(state, scope, attempt))

        try:
            results = await asyncio.wait_for(asyncio.gather(*calls), timeout=remaining)
        except TimeoutError:
            results = [
                (
                    [],
                    [
                        ToolFailure(
                            task_id=task.task_id, code="timeout", safe_message=TIMEOUT_MESSAGE
                        )
                    ],
                    [],
                )
            ]

        evidence = list(state["evidence"])
        failures = list(state["failures"])
        warnings = list(state["warnings"])
        for new_evidence, new_failures, new_warnings in results:
            evidence = _merge_evidence(task.task_id, evidence, new_evidence)
            failures.extend(new_failures)
            warnings.extend(w for w in new_warnings if w not in warnings)

        return {
            "attempt": attempt,
            "evidence": evidence,
            "failures": failures,
            "warnings": warnings,
            "tool_calls": state.get("tool_calls", 0) + len(calls),
            "progress": events,
        }

    async def _evidence_check(self, state: TaskRunState) -> dict[str, Any]:
        task = state["task"]
        attempt: Attempt = 1 if state["attempt"] == 1 else 2
        assessment = await self._checker.assess(task, state["evidence"], attempt)
        sufficient = assessment.status == EvidenceStatus.SUFFICIENT
        return {
            "assessment": assessment,
            "progress": [*state["progress"], progress.evidence_checked(task.task_id, sufficient)],
        }

    def _after_check(self, state: TaskRunState) -> Literal["retry", "done"]:
        assessment = state.get("assessment")
        if assessment is None or assessment.status == EvidenceStatus.SUFFICIENT:
            return "done"
        if state["attempt"] >= MAX_RETRIEVAL_ATTEMPTS:
            return "done"
        if any(f.code in NON_RETRYABLE_FAILURES for f in state["failures"]):
            return "done"
        if state["deadline"] - time.monotonic() <= 0:
            return "done"
        return "retry"

    async def _refine_query(self, state: TaskRunState) -> dict[str, Any]:
        task = state["task"]
        assessment = state["assessment"]
        assert assessment is not None
        refined = await self._planner.refine(task, assessment, previous_query=state["query"])
        return {
            "query": refined.retrieval_query or state["query"],
            "keywords": refined.keywords,
            "sql_hint": refined.sql_hint,
            "progress": [*state["progress"], progress.retry_started(task.task_id)],
        }

    # --- tool calls ------------------------------------------------------
    async def _call_retrieval(
        self, state: TaskRunState, scope: AccessScope, attempt: int
    ) -> tuple[list[Evidence], list[ToolFailure], list[str]]:
        task, user = state["task"], state["user"]
        _assert_attempt(task, attempt, scope)
        needs = task.access_request.source_needs
        modalities = {Modality.TEXT}
        if SourceNeed.IMAGES in needs or task.plan.intent == Intent.IMAGE_INTERPRETATION:
            modalities |= {Modality.IMAGE_CAPTION, Modality.IMAGE_OCR}
        request = RetrievalRequest(
            task_id=task.task_id,
            query=state["query"][:2000],
            keywords=state.get("keywords", [])[:12],
            modalities=frozenset(modalities),
            upload_ids=task.plan.use_upload_ids[:3],
            need_visual_interpretation=task.plan.intent == Intent.IMAGE_INTERPRETATION,
            attempt=1 if attempt == 1 else 2,
        )
        try:
            result = await self._retrieval.retrieve(user, scope, request)
        except Exception as exc:
            logger.warning(
                "retrieval_error", extra={"task_id": task.task_id, "exc_type": type(exc).__name__}
            )
            return (
                [],
                [
                    ToolFailure(
                        task_id=task.task_id, code="unavailable", safe_message=UNAVAILABLE_MESSAGE
                    )
                ],
                [],
            )
        # Defense in depth (I3): re-check every result against the scope and registry.
        permitted = await self._policy.filter_evidence(user, scope, result.evidence)
        failures = [result.failure] if result.failure else []
        return permitted, failures, list(result.warnings)

    async def _call_sql(
        self, state: TaskRunState, scope: AccessScope, attempt: int
    ) -> tuple[list[Evidence], list[ToolFailure], list[str]]:
        task, user = state["task"], state["user"]
        _assert_attempt(task, attempt, scope)
        request = SQLRequest(
            task_id=task.task_id,
            question=task.plan.question,
            hint=state.get("sql_hint"),
            attempt=1 if attempt == 1 else 2,
        )
        try:
            result = await self._sql.query(user, scope, request)
        except Exception as exc:
            logger.warning(
                "sql_error", extra={"task_id": task.task_id, "exc_type": type(exc).__name__}
            )
            return (
                [],
                [
                    ToolFailure(
                        task_id=task.task_id, code="unavailable", safe_message=UNAVAILABLE_MESSAGE
                    )
                ],
                [],
            )
        if isinstance(result, ToolFailure):
            return [], [result], []
        if result.provenance.view not in scope.allowed_views:
            logger.error("sql_view_outside_scope", extra={"task_id": task.task_id})
            return (
                [],
                [
                    ToolFailure(
                        task_id=task.task_id,
                        code="no_permitted_view",
                        safe_message=UNAVAILABLE_MESSAGE,
                    )
                ],
                [],
            )
        return [structured_evidence(task.task_id, result)], [], []

    # --- outcome ---------------------------------------------------------
    @staticmethod
    def _outcome(state: TaskRunState) -> TaskOutcome:
        assessment: EvidenceAssessment | None = state.get("assessment")
        evidence = state.get("evidence", [])
        failures = state.get("failures", [])
        if assessment and assessment.status == EvidenceStatus.SUFFICIENT:
            status = OutcomeStatus.READY
        elif not evidence and failures:
            status = OutcomeStatus.FAILED
        else:
            status = OutcomeStatus.INSUFFICIENT
        return TaskOutcome(
            task_id=state["task"].task_id,
            status=status,
            attempts=state.get("attempt", 0),
            tool_calls=state.get("tool_calls", 0),
            evidence=evidence,
            assessment=assessment,
            failures=failures,
            warnings=state.get("warnings", []),
        )


def _require_scope(task: Task) -> AccessScope:
    scope = task.decision.scope
    if not task.decision.allowed or scope is None:
        raise ScopeViolationError(f"task {task.task_id} has no permitted scope")
    if not scope.hash_is_consistent():
        raise ScopeViolationError(f"task {task.task_id} scope was modified")
    return scope


def _assert_attempt(task: Task, attempt: int, scope: AccessScope) -> None:
    assert 1 <= attempt <= MAX_RETRIEVAL_ATTEMPTS, "retrieval attempt cap exceeded"
    expected = task.decision.scope
    if expected is None or scope.scope_hash != expected.scope_hash:
        raise ScopeViolationError(f"task {task.task_id} scope changed between attempts")


def _merge_evidence(task_id: str, existing: list[Evidence], new: list[Evidence]) -> list[Evidence]:
    """Deduplicate across attempts and assign request-unique IDs like 't1.e3'."""
    seen = {(e.source_id, e.kind, e.location.label, e.excerpt[:80]) for e in existing}
    merged = list(existing)
    for item in new:
        key = (item.source_id, item.kind, item.location.label, item.excerpt[:80])
        if key in seen:
            continue
        seen.add(key)
        merged.append(
            item.model_copy(
                update={"evidence_id": f"{task_id}.e{len(merged) + 1}", "task_id": task_id}
            )
        )
    return merged


def structured_evidence(task_id: str, result: SQLResult) -> Evidence:
    """Wrap a SQL result as STRUCTURED evidence with a compact Markdown table excerpt."""
    columns = result.columns
    lines = []
    if columns:
        lines.append("| " + " | ".join(columns) + " |")
        lines.append("|" + "---|" * len(columns))
        for row in result.rows[:MAX_TABLE_ROWS_IN_EXCERPT]:
            lines.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")
    summary = f"{result.row_count} row(s) from {result.provenance.view}"
    if result.truncated:
        summary += " (truncated)"
    excerpt = (summary + "\n" + "\n".join(lines))[:1500]
    source_id = (
        result.provenance.dataset_source_ids[0]
        if result.provenance.dataset_source_ids
        else f"view:{result.provenance.view}"
    )
    return Evidence(
        evidence_id=f"{task_id}.sql",
        task_id=task_id,
        kind=EvidenceKind.STRUCTURED,
        source_id=source_id,
        source_title=f"Structured data: {result.provenance.view}",
        source_kind=SourceKind.STRUCTURED,
        classification=Classification.INTERNAL,
        excerpt=excerpt,
        location=EvidenceLocation(label=f"query {result.provenance.query_fingerprint[:8]}"),
        structured=result,
        retrieved_at=datetime.now(UTC),
    )


def dependency_keywords(evidence: list[Evidence], limit: int = 8) -> list[str]:
    """Short entity-like values from dependency results (never raw excerpts)."""
    keywords: list[str] = []
    for item in evidence:
        if item.kind != EvidenceKind.STRUCTURED or not item.structured:
            continue
        for row in item.structured.rows:
            for value in row.values():
                if isinstance(value, str) and 0 < len(value) <= 60 and value not in keywords:
                    keywords.append(value)
                if len(keywords) >= limit:
                    return keywords
    return keywords


def dependency_subject_ids(evidence: list[Evidence]) -> list[str]:
    ids: list[str] = []
    for item in evidence:
        if item.structured:
            for row in item.structured.rows:
                value = row.get("employee_id")
                if isinstance(value, str) and value not in ids:
                    ids.append(value)
    return ids


__all__ = [
    "ScopeViolationError",
    "TaskWorkflow",
    "UserContext",
    "dependency_keywords",
    "dependency_subject_ids",
    "structured_evidence",
]
