from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.main import REQUEST_ID_HEADER, create_app


def _client() -> TestClient:
    app = create_app(Settings(app_env="test", demo_jwt_secret="x" * 32))

    @app.get("/boom-not-found")
    async def boom_not_found() -> None:
        raise NotFoundError()

    @app.get("/boom-unexpected")
    async def boom_unexpected() -> None:
        raise RuntimeError("secret internal detail")

    return TestClient(app, raise_server_exceptions=False)


def test_health() -> None:
    response = _client().get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.headers[REQUEST_ID_HEADER]


def test_app_error_maps_to_stable_body() -> None:
    response = _client().get("/boom-not-found")
    assert response.status_code == 404
    body = response.json()["error"]
    assert body["code"] == "not_found"
    assert body["request_id"] == response.headers[REQUEST_ID_HEADER]


def test_unexpected_error_hides_details() -> None:
    response = _client().get("/boom-unexpected")
    assert response.status_code == 500
    assert "secret" not in response.text
