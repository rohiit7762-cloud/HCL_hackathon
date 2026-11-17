"""Demo identity routes. Return 404 unless DEMO_MODE=true."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import Container, CurrentUser
from app.core.errors import NotFoundError
from app.schemas.api import DemoSessionRequest, DemoSessionResponse, MeResponse
from app.schemas.security import DemoUser

router = APIRouter(prefix="/api/v1", tags=["identity"])


@router.get("/demo/users", response_model=list[DemoUser])
async def list_demo_users(container: Container) -> list[DemoUser]:
    if container.demo_identity is None:
        raise NotFoundError()
    return await container.data.principals.list_demo_users(container.settings.demo_tenant_id)


@router.post("/demo/session", response_model=DemoSessionResponse)
async def create_demo_session(
    body: DemoSessionRequest, container: Container
) -> DemoSessionResponse:
    if container.demo_identity is None:
        raise NotFoundError()
    token, ttl = await container.demo_identity.issue(
        container.settings.demo_tenant_id, body.employee_id
    )
    return DemoSessionResponse(access_token=token, expires_in_seconds=ttl)


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentUser) -> MeResponse:
    return MeResponse(
        employee_id=user.employee_id,
        display_name=user.display_name,
        title=user.title,
        department=user.department,
        roles=sorted(user.roles),
        groups=sorted(user.groups),
        clearance=user.clearance,
        is_synthetic=user.is_synthetic,
    )
