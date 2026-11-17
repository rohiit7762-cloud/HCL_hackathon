from __future__ import annotations

import time

import jwt
from fastapi import APIRouter, HTTPException, Request

from app.demo import DEMO_USERS, answer_demo, context_for
from app.schemas.api import (
    DemoSessionRequest,
    DemoSessionResponse,
    MeResponse,
    QueryRequest,
    QueryResponse,
)

router = APIRouter(prefix="/api/v1", tags=["demo"])


def _user(request: Request):
    token = request.headers.get("authorization", "").removeprefix("Bearer ")
    try:
        settings = request.app.state.settings
        claims = jwt.decode(
            token,
            settings.demo_jwt_secret.get_secret_value(),
            algorithms=["HS256"],
            audience="lumen-demo",
        )
        user = context_for(claims["sub"])
        if user is None or claims.get("tenant") != user.tenant_id:
            raise ValueError
        return user
    except Exception as exc:
        raise HTTPException(401, "Authentication required") from exc


@router.get("/demo/users")
async def demo_users():
    return DEMO_USERS


@router.post("/demo/session", response_model=DemoSessionResponse)
async def demo_session(body: DemoSessionRequest, request: Request):
    settings = request.app.state.settings
    user = context_for(body.employee_id)
    if not settings.demo_mode or user is None:
        raise HTTPException(404, "Demo identity unavailable")
    key = settings.demo_jwt_secret.get_secret_value()
    if not key:
        raise HTTPException(503, "Set DEMO_JWT_SECRET in .env")
    ttl = settings.demo_token_ttl_minutes * 60
    token = jwt.encode(
        {
            "sub": user.employee_id,
            "tenant": user.tenant_id,
            "aud": "lumen-demo",
            "exp": int(time.time()) + ttl,
        },
        key,
        algorithm="HS256",
    )
    return DemoSessionResponse(access_token=token, expires_in_seconds=ttl)


@router.get("/me", response_model=MeResponse)
async def me(request: Request):
    user = _user(request)
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


@router.post("/query", response_model=QueryResponse)
async def query(body: QueryRequest, request: Request):
    user = _user(request)
    rid = getattr(request.state, "request_id", "local-demo")
    orchestrator = getattr(request.app.state, "orchestrator", None)
    if orchestrator:
        return await orchestrator.query(user, body.query, rid, body.conversation_id)
    return answer_demo(body.query, user, rid, body.conversation_id)
