"""HTTP API end to end with an injected container (fake LLM + in-memory data layer)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import build_container
from app.core.config import Settings
from app.main import create_app
from tests.fakes.data_layer import build_test_data_layer
from tests.scripted import planned, scripted_llm

SETTINGS = Settings(app_env="test", demo_jwt_secret="x" * 40)
CORPUS = {
    "runbook": [
        ("p. 2", "Escalation path for payments outages: page the payments on-call engineer first.")
    ]
}
PLAN = planned(
    {
        "task_id": "t1",
        "question": "What is the escalation path for payments?",
        "data_categories": ["general_knowledge"],
        "retrieval_query": "escalation path payments outages",
    }
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    import asyncio

    layer = build_test_data_layer(corpus=CORPUS)
    llm = scripted_llm(*[PLAN] * 5)
    container = asyncio.run(build_container(SETTINGS, data=layer.data, llm=llm))
    with TestClient(create_app(SETTINGS, container=container)) as test_client:
        yield test_client


def login(client: TestClient, employee_id: str) -> dict[str, str]:
    response = client.post("/api/v1/demo/session", json={"employee_id": employee_id})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_demo_users_and_me(client: TestClient) -> None:
    users = client.get("/api/v1/demo/users").json()
    assert {u["employee_id"] for u in users} >= {"E100", "E103"}
    me = client.get("/api/v1/me", headers=login(client, "E103")).json()
    assert me["employee_id"] == "E103" and "hr" in me["roles"] and me["is_synthetic"]


def test_query_requires_auth(client: TestClient) -> None:
    assert client.post("/api/v1/query", json={"query": "hi"}).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.post("/api/v1/query", json={"query": "hi"}, headers=bad).status_code == 401


def test_query_returns_cited_answer(client: TestClient) -> None:
    response = client.post(
        "/api/v1/query", json={"query": "escalation path?"}, headers=login(client, "E100")
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tasks"][0]["status"] == "answered"
    assert body["citations"][0]["source_id"] == "runbook"
    assert "[1]" in body["answer_markdown"]


def test_i1_client_supplied_roles_are_ignored(client: TestClient) -> None:
    headers = {**login(client, "E100"), "X-Roles": "hr,admin"}
    me = client.get("/api/v1/me", headers=headers).json()
    assert "hr" not in me["roles"]
    response = client.post("/api/v1/query", json={"query": "q", "roles": ["hr"]}, headers=headers)
    assert response.status_code == 200


def test_i12_unauthorized_source_is_indistinguishable_from_missing(client: TestClient) -> None:
    headers = login(client, "E100")
    forbidden = client.get("/api/v1/sources/falcon", headers=headers)
    missing = client.get("/api/v1/sources/does-not-exist", headers=headers)
    assert forbidden.status_code == missing.status_code == 404
    assert forbidden.json()["error"]["code"] == missing.json()["error"]["code"]
    assert client.get("/api/v1/sources/runbook", headers=headers).status_code == 200


def test_ingest_is_role_gated(client: TestClient) -> None:
    files = {"file": ("a.md", b"# Title\n\nBody text.", "text/markdown")}
    data = {"options": '{"classification": "internal"}'}
    assert (
        client.post(
            "/api/v1/ingest", files=files, data=data, headers=login(client, "E100")
        ).status_code
        == 403
    )
    ok = client.post("/api/v1/ingest", files=files, data=data, headers=login(client, "E900"))
    assert ok.status_code == 200 and ok.json()["status"] == "indexed"


def test_upload_rejects_non_images(client: TestClient) -> None:
    headers = login(client, "E100")
    bad = client.post(
        "/api/v1/uploads", files={"file": ("x.png", b"MZ\x00", "image/png")}, headers=headers
    )
    assert bad.status_code == 422
    good = client.post(
        "/api/v1/uploads",
        files={"file": ("x.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")},
        headers=headers,
    )
    assert good.status_code == 200 and good.json()["upload_id"]


def test_demo_routes_hidden_when_demo_mode_off() -> None:
    import asyncio

    settings = Settings(app_env="test", demo_mode=False)
    layer = build_test_data_layer()
    container = asyncio.run(build_container(settings, data=layer.data, llm=scripted_llm()))
    with TestClient(create_app(settings, container=container)) as c:
        assert c.get("/api/v1/demo/users").status_code == 404
