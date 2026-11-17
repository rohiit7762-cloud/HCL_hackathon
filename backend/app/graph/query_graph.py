"""Top-level LangGraph workflow (§3).

plan_and_check_compliance → schedule ⇄ run_task (fan-out via Send, in depends_on
waves) → answer → final_compliance_validation → END

Declined and out-of-scope tasks get their outcome in the planning node and never
reach run_task. Nodes are thin: all logic lives in the agents and services.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agents.answer import AnswerAgent
from app.agents.compliance import ResponseValidator
from app.agents.planner import Planner
from app.graph import progress
from app.graph.state import OutcomeStatus, QueryState, TaskOutcome, TaskRunState
from app.graph.task_graph import TaskWorkflow, dependency_keywords, dependency_subject_ids
from app.schemas.api import QueryResponse
from app.schemas.interfaces import PolicyService
from app.schemas.planning import Task, TaskRoute

RUN_TASK = "run_task"
ANSWER = "answer"


class QueryWorkflow:
    def __init__(
        self,
        planner: Planner,
        task_workflow: TaskWorkflow,
        answer_agent: AnswerAgent,
        validator: ResponseValidator,
        policy: PolicyService,
    ) -> None:
        self._planner = planner
        self._task_workflow = task_workflow
        self._answer_agent = answer_agent
        self._validator = validator
        self._policy = policy
        self.graph = self._build()

    def _build(self) -> Any:
        graph = StateGraph(QueryState)
        graph.add_node("plan_and_check_compliance", self._plan)
        graph.add_node("schedule", self._schedule)
        graph.add_node(RUN_TASK, self._run_task)  # type: ignore[arg-type]  # Send payload
        graph.add_node(ANSWER, self._answer)
        graph.add_node("final_compliance_validation", self._validate)

        graph.add_edge(START, "plan_and_check_compliance")
        graph.add_edge("plan_and_check_compliance", "schedule")
        graph.add_conditional_edges("schedule", self._dispatch, [RUN_TASK, ANSWER])
        graph.add_edge(RUN_TASK, "schedule")
        graph.add_edge(ANSWER, "final_compliance_validation")
        graph.add_edge("final_compliance_validation", END)
        return graph.compile()

    # --- nodes -----------------------------------------------------------
    async def _plan(self, state: QueryState) -> dict[str, Any]:
        request = state["request"]
        tasks = await self._planner.plan(
            state["user"],
            request.query,
            history=state.get("history", []),
            upload_ids=request.upload_ids,
        )
        outcomes: dict[str, TaskOutcome] = {}
        events = [progress.identity_verified(), progress.planning_done(len(tasks))]
        for task in tasks:
            if not task.decision.allowed:
                outcomes[task.task_id] = TaskOutcome(
                    task_id=task.task_id, status=OutcomeStatus.DECLINED
                )
                events.append(progress.task_declined(task.task_id))
            elif task.route == TaskRoute.NONE:
                outcomes[task.task_id] = TaskOutcome(
                    task_id=task.task_id, status=OutcomeStatus.OUT_OF_SCOPE
                )
        events.append(progress.compliance_done())
        return {"tasks": tasks, "outcomes": outcomes, "progress": events}

    async def _schedule(self, state: QueryState) -> dict[str, Any]:
        """Resolve tasks blocked by failed dependencies and re-check policy for
        tasks whose dependencies introduced new people. Writes outcomes only."""
        outcomes = dict(state.get("outcomes", {}))
        updates: dict[str, TaskOutcome] = {}
        events = []
        changed = True
        while changed:  # propagate dependency failures transitively
            changed = False
            for task in state["tasks"]:
                if task.task_id in outcomes or not _deps_done(task, outcomes):
                    continue
                if not all(outcomes[d].usable_by_dependents for d in task.plan.depends_on):
                    outcome = TaskOutcome(
                        task_id=task.task_id, status=OutcomeStatus.DEPENDENCY_UNAVAILABLE
                    )
                    outcomes[task.task_id] = updates[task.task_id] = outcome
                    events.append(progress.dependency_unavailable(task.task_id))
                    changed = True
                elif task.plan.depends_on and not await self._still_allowed(state, task, outcomes):
                    outcome = TaskOutcome(task_id=task.task_id, status=OutcomeStatus.DECLINED)
                    outcomes[task.task_id] = updates[task.task_id] = outcome
                    events.append(progress.task_declined(task.task_id))
                    changed = True
        return {"outcomes": updates, "progress": events} if updates else {}

    def _dispatch(self, state: QueryState) -> list[Send] | str:
        outcomes = state.get("outcomes", {})
        ready = [
            task
            for task in state["tasks"]
            if task.task_id not in outcomes and _deps_done(task, outcomes)
        ]
        if not ready:
            return ANSWER
        return [Send(RUN_TASK, self._task_payload(state, task, outcomes)) for task in ready]

    async def _run_task(self, payload: TaskRunState) -> dict[str, Any]:
        outcome, events = await self._task_workflow.run(payload)
        return {
            "outcomes": {outcome.task_id: outcome},
            "progress": events,
            "warnings": outcome.warnings,
        }

    async def _answer(self, state: QueryState) -> dict[str, Any]:
        draft = await self._answer_agent.draft(state["tasks"], state.get("outcomes", {}))
        return {"draft": draft, "progress": [progress.answer_started()]}

    async def _validate(self, state: QueryState) -> dict[str, Any]:
        validated = await self._validator.validate(
            state["user"], state["tasks"], state.get("outcomes", {}), state["draft"]
        )
        events = [*state.get("progress", []), progress.validation_done()]
        warnings = list(dict.fromkeys([*state.get("warnings", []), *validated.warnings]))
        response = QueryResponse(
            request_id=state["request_id"],
            conversation_id=state["conversation_id"],
            answer_markdown=validated.answer_markdown,
            tasks=validated.tasks,
            citations=validated.citations,
            progress=events,
            warnings=warnings,
        )
        return {"response": response}

    # --- helpers ---------------------------------------------------------
    def _task_payload(
        self, state: QueryState, task: Task, outcomes: dict[str, TaskOutcome]
    ) -> TaskRunState:
        dep_evidence = [e for d in task.plan.depends_on for e in outcomes[d].relevant_evidence()]
        return TaskRunState(
            user=state["user"],
            task=task,
            deadline=state["deadline"],
            dependency_evidence=dep_evidence,
            keywords=dependency_keywords(dep_evidence),
        )

    async def _still_allowed(
        self, state: QueryState, task: Task, outcomes: dict[str, TaskOutcome]
    ) -> bool:
        """If dependencies surfaced people, the task is about them too: re-run policy."""
        dep_evidence = [e for d in task.plan.depends_on for e in outcomes[d].relevant_evidence()]
        new_ids = (
            set(dependency_subject_ids(dep_evidence)) - task.access_request.subject_employee_ids
        )
        if not new_ids:
            return True
        request = await self._planner.build_access_request(
            state["user"], task.plan, extra_subject_ids=sorted(new_ids)
        )
        decision = await self._policy.evaluate_task(state["user"], request)
        # Never broaden: the original scope still applies if the re-check allows.
        return decision.allowed


def _deps_done(task: Task, outcomes: dict[str, TaskOutcome]) -> bool:
    return all(dep in outcomes for dep in task.plan.depends_on)
