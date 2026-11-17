"""Typed HTTP client for the FastAPI backend. The only place the UI talks to the API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.schemas.api import DemoSessionResponse, MeResponse, QueryResponse, UploadResponse
from app.schemas.ingestion import SourcePreview
from app.schemas.security import DemoUser

QUERY_TIMEOUT = httpx.Timeout(120.0, connect=5.0)
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.request_id = request_id

    @property
    def session_expired(self) -> bool:
        return self.status == 401


@dataclass
class ApiClient:
    base_url: str

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = httpx.request(
                method,
                f"{self.base_url.rstrip('/')}{path}",
                headers=headers,
                timeout=timeout,
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise ApiError("The assistant took too long to respond. Please try again.") from exc
        except httpx.HTTPError as exc:
            raise ApiError("Can't reach the backend. Is it running?") from exc
        if response.status_code >= 400:
            message, request_id = "Something went wrong.", None
            try:
                error = response.json()["error"]
                message, request_id = error["message"], error.get("request_id")
            except (ValueError, KeyError, TypeError):
                pass
            raise ApiError(message, response.status_code, request_id)
        return response

    def health(self) -> bool:
        try:
            return self._request("GET", "/health").status_code == 200
        except ApiError:
            return False

    def demo_users(self) -> list[DemoUser]:
        return [
            DemoUser.model_validate(u) for u in self._request("GET", "/api/v1/demo/users").json()
        ]

    def login(self, employee_id: str) -> str:
        response = self._request("POST", "/api/v1/demo/session", json={"employee_id": employee_id})
        return DemoSessionResponse.model_validate(response.json()).access_token

    def me(self, token: str) -> MeResponse:
        return MeResponse.model_validate(self._request("GET", "/api/v1/me", token=token).json())

    def query(
        self, token: str, query: str, conversation_id: str | None, upload_ids: list[str]
    ) -> QueryResponse:
        body = {"query": query, "conversation_id": conversation_id, "upload_ids": upload_ids}
        response = self._request(
            "POST", "/api/v1/query", token=token, json=body, timeout=QUERY_TIMEOUT
        )
        return QueryResponse.model_validate(response.json())

    def upload(self, token: str, name: str, data: bytes, mime_type: str) -> str:
        response = self._request(
            "POST", "/api/v1/uploads", token=token, files={"file": (name, data, mime_type)}
        )
        return UploadResponse.model_validate(response.json()).upload_id

    def source_preview(self, token: str, source_id: str, location: str | None) -> SourcePreview:
        params = {"location": location} if location else None
        response = self._request("GET", f"/api/v1/sources/{source_id}", token=token, params=params)
        return SourcePreview.model_validate(response.json())

    def source_image(self, token: str, source_id: str) -> bytes:
        return self._request("GET", f"/api/v1/sources/{source_id}/image", token=token).content
