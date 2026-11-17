"""Liveness endpoint. Dependency checks (Chroma, Neon) are added once those services exist."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.schemas.api import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    return HealthResponse(status="ok", version=request.app.state.settings.app_version)
