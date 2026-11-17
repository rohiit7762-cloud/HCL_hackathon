"""End-to-end LangGraph workflow with a scripted LLM, the real policy, and fake tools."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from app.api.dependencies import ServiceContainer, build_container
from app.core.config import Settings
from app.core.errors import ValidationFailedError
from app.schemas.api import QueryRequest, QueryResponse, TaskStatus
from app.schemas.llm import ModelRole
from app.schemas.security import UserContext
from tests.fakes.data_layer import ASHA, ORBIT_USER, RILEY, DataLayerHarness, build_test_data_layer
from tests.fakes.llm import FakeLLMProvider
from tests.scripted import judge_insufficient, planned, scripted_llm

CORPUS = {
    "runbook": [
        (
            "p. 2 · Escalation",
            "Escalation path for payments outages: page the payments "
            "on-call, then the platform lead after 15 minutes.",
        ),
        (
            "p. 3 · Failover",
            "Ledger database failover: promote the standby replica. "
            "Recovery time objective RTO is 30 minutes.",
        ),
    ],
    "onboarding": [
        ("lines 1-5 · Week one", "In your first week set up VPN access, laptop encryption and SSO.")
    ],
    "falcon": [("p. 1", "Project Falcon acquisition valuation is confidential.")],
    "orbit-doc": [("p. 1", "Orbit escalation path for payments outages uses pagers.")],
}

SETTINGS = Settings(app_env="test", demo_jwt_secret="x" * 40, query_timeout_seconds=30)


async def make(llm: FakeLLMProvider, **kwargs: Any) -> tuple[ServiceContainer, DataLayerHarness]:
    layer = build_test_data_layer(corpus=CORPUS, **kwargs)
    container = await build_container(SETTINGS, data=layer.data, llm=llm)
    return container, layer


async def ask(container: ServiceContainer, who: UserContext, query: str) -> QueryResponse:
    return await container.orchestrator.run(who, QueryRequest(query=query))


ESCALATION = {
    "task_id": "t1",
    "question": "What is the escalation path for payments outages?",
    "data_categories": ["general_knowledge"],
    "retrieval_query": "escalation path payments outages",
}


async def test_i5_partial_refusal_declines_only_restricted_part() -> None:
    salary = {
        "task_id": "t2",
        "question": "What is Sam Patel's salary?",
        "original_span": "what is Sam Patel's salary?",
        "data_categories": ["employee_personal"],
        "subject_refs": ["Sam Patel"],
        "requested_fields": ["salary"],
        "source_needs": ["structured"],
    }
    container, layer = await make(
        scripted_llm(planned(ESCALATION, salary)),
        sql_results={"api.employee_hr": [{"salary": 185000}]},
    )

    response = await ask(container, ASHA, "Escalation path for payments, and Sam Patel's salary?")

    statuses = {t.task_id: t.status for t in response.tasks}
    assert statuses == {"t1": TaskStatus.ANSWERED, "t2": TaskStatus.DECLINED}
    assert "page the payments on-call" in response.answer_markdown
    assert "another employee's personal information" in response.answer_markdown
    assert "185" not in response.answer_markdown  # I10: nothing leaked
    assert layer.sql.calls == []  # declined task never executed
    assert [r.task_id for _, r, _ in layer.retrieval.calls] == ["t1"]
    assert response.citations and response.citations[0].source_id == "runbook"


async def test_i2_llm_misclassification_cannot_grant_access() -> None:
    sneaky = {
        "task_id": "t1",
        "question": "What is Sam Patel's salary?",
        "original_span": "What is Sam Patel's salary?",
        "data_categories": ["general_knowledge"],
        "subject_refs": ["Sam Patel"],
    }
    container, layer = await make(scripted_llm(planned(sneaky)))
    response = await ask(container, ASHA, "What is Sam Patel's salary?")
    assert response.tasks[0].status == TaskStatus.DECLINED
    assert layer.retrieval.calls == []


async def test_i6_retry_cap_two_attempts_then_gap() -> None:
    llm = scripted_llm(planned(ESCALATION)).default(ModelRole.EVIDENCE_JUDGE, judge_insufficient)
    container, layer = await make(llm)

    response = await ask(container, ASHA, "What is the escalation path?")

    assert len(layer.retrieval.calls_for("t1")) == 2
    assert len(llm.calls_for(ModelRole.REFINER)) == 1
    assert response.tasks[0].attempts == 2
    assert response.tasks[0].status in {TaskStatus.PARTIAL, TaskStatus.INSUFFICIENT_EVIDENCE}


async def test_i6_retry_uses_same_scope_and_refined_query() -> None:
    llm = scripted_llm(planned(ESCALATION)).default(ModelRole.EVIDENCE_JUDGE, judge_insufficient)
    container, layer = await make(llm)
    await ask(container, ASHA, "What is the escalation path?")

    (_, first, scope1), (_, second, scope2) = layer.retrieval.calls
    assert (first.attempt, second.attempt) == (1, 2)
    assert second.query == "refined query" and second.keywords == ["RTO"]
    assert scope1.scope_hash == scope2.scope_hash  # never broadened
    assert "falcon" not in scope2.authorized_source_ids


async def test_sufficient_evidence_is_not_retried() -> None:
    llm = scripted_llm(planned(ESCALATION))
    container, layer = await make(llm)
    response = await ask(container, ASHA, "What is the escalation path?")
    assert len(layer.retrieval.calls) == 1
    assert llm.calls_for(ModelRole.REFINER) == []
    assert response.tasks[0].attempts == 1


async def test_retry_then_success() -> None:
    from tests.scripted import judge_sufficient

    llm = scripted_llm(planned(ESCALATION))
    llm.script(ModelRole.EVIDENCE_JUDGE, judge_insufficient, judge_sufficient)
    container, layer = await make(llm)
    response = await ask(container, ASHA, "What is the escalation path?")
    assert len(layer.retrieval.calls) == 2
    assert response.tasks[0].status == TaskStatus.ANSWERED


async def test_confidential_business_same_answer_as_missing_for_unauthorized() -> None:
    falcon = {
        "task_id": "t1",
        "question": "Summarize the Project Falcon memo",
        "original_span": "Summarize the Project Falcon memo",
        "intent": "summarize",
        "data_categories": ["general_knowledge"],
        "retrieval_query": "Project Falcon memo",
    }
    container, _ = await make(scripted_llm(planned(falcon), planned(falcon)))

    asha = await ask(container, ASHA, "Summarize the Project Falcon memo")
    riley = await ask(container, RILEY, "Summarize the Project Falcon memo")

    assert "Falcon acquisition" not in asha.answer_markdown
    assert all(c.source_id != "falcon" for c in asha.citations)
    assert any(c.source_id == "falcon" for c in riley.citations)


async def test_cross_tenant_content_never_surfaces() -> None:
    container, _ = await make(scripted_llm(planned(ESCALATION), planned(ESCALATION)))
    lumen = await ask(container, ASHA, "What is the escalation path?")
    orbit = await ask(container, ORBIT_USER, "What is the escalation path?")
    assert all(c.source_id != "orbit-doc" for c in lumen.citations)
    assert all(c.source_id == "orbit-doc" for c in orbit.citations)


async def test_dependency_failure_skips_dependent_task() -> None:
    first = {
        "task_id": "t1",
        "question": "Which team owns the Mars datacenter?",
        "data_categories": ["general_knowledge"],
        "retrieval_query": "zzzz qqqq",
    }
    second = {
        "task_id": "t2",
        "question": "What is that team's runbook?",
        "data_categories": ["general_knowledge"],
        "depends_on": ["t1"],
    }
    container, layer = await make(scripted_llm(planned(first, second)))
    response = await ask(container, ASHA, "Who owns Mars DC and what's their runbook?")
    statuses = {t.task_id: t.status for t in response.tasks}
    assert statuses["t2"] == TaskStatus.INSUFFICIENT_EVIDENCE
    assert layer.retrieval.calls_for("t2") == []


async def test_planner_failure_falls_back_to_single_task() -> None:
    llm = scripted_llm(ValidationFailedError())
    container, layer = await make(llm)
    response = await ask(container, ASHA, "escalation path payments outages")
    assert len(response.tasks) == 1
    assert layer.retrieval.calls


async def test_out_of_scope_gets_fixed_message() -> None:
    joke = {
        "task_id": "t1",
        "question": "Tell me a joke",
        "intent": "out_of_scope",
        "data_categories": ["general_knowledge"],
    }
    container, layer = await make(scripted_llm(planned(joke)))
    response = await ask(container, ASHA, "Tell me a joke")
    assert "company knowledge" in response.answer_markdown
    assert layer.retrieval.calls == []


async def test_progress_events_are_templated() -> None:
    container, _ = await make(scripted_llm(planned(ESCALATION)))
    response = await ask(container, ASHA, "What is the escalation path for payments outages?")
    steps = [e.step for e in response.progress]
    assert steps[0] == "identity" and steps[-1] == "validation"
    assert all("escalation" not in e.message.lower() for e in response.progress)


async def test_i11_logs_contain_no_query_text(caplog: pytest.LogCaptureFixture) -> None:
    secret = "zebra-unicorn-secret-phrase"
    task = {**ESCALATION, "question": f"What about {secret}?"}
    container, _ = await make(scripted_llm(planned(task)))
    with caplog.at_level(logging.DEBUG):
        await ask(container, ASHA, f"What about {secret}?")
    for record in caplog.records:
        assert secret not in record.getMessage()
        assert all(secret not in str(v) for v in record.__dict__.values())
