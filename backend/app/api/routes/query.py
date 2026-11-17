from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import Container, CurrentUser
from app.schemas.api import QueryRequest, QueryResponse

router = APIRouter(prefix="/api/v1", tags=["query"])


@router.post("/query", response_model=QueryResponse)
async def query(body: QueryRequest, user: CurrentUser, container: Container) -> QueryResponse:
    return await container.orchestrator.run(user, body)
