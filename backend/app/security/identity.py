"""Trusted identity resolution (§7.2, invariant I1).

Tokens carry ONLY tenant_id and employee_id. Roles, groups and clearance are
loaded fresh from the PrincipalRepository on every request; anything the
client sends about roles or groups is ignored.
"""

from __future__ import annotations

import time
from typing import Protocol

import jwt

from app.core.errors import AuthenticationError
from app.schemas.interfaces import PrincipalRepository
from app.schemas.security import UserContext

_ALGORITHM = "HS256"
_TOKEN_TYPE = "demo"
MIN_SECRET_LENGTH = 32


class IdentityResolver(Protocol):
    async def resolve(self, token: str) -> UserContext: ...


class DemoTokenIdentityResolver:
    """Issues and verifies short-lived HMAC tokens for synthetic demo identities."""

    def __init__(self, principals: PrincipalRepository, secret: str, ttl_minutes: int) -> None:
        if len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"DEMO_JWT_SECRET must be at least {MIN_SECRET_LENGTH} characters in demo mode"
            )
        self._principals = principals
        self._secret = secret
        self._ttl_seconds = ttl_minutes * 60

    async def issue(self, tenant_id: str, employee_id: str) -> tuple[str, int]:
        """Return (token, expires_in_seconds). Raises AuthenticationError for unknown users."""
        if await self._principals.get_user_context(tenant_id, employee_id) is None:
            raise AuthenticationError("Unknown demo identity.")
        now = int(time.time())
        claims = {
            "sub": employee_id,
            "tid": tenant_id,
            "typ": _TOKEN_TYPE,
            "iat": now,
            "exp": now + self._ttl_seconds,
        }
        return jwt.encode(claims, self._secret, algorithm=_ALGORITHM), self._ttl_seconds

    async def resolve(self, token: str) -> UserContext:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[_ALGORITHM],
                options={"require": ["sub", "tid", "exp", "typ"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError() from exc
        if claims.get("typ") != _TOKEN_TYPE:
            raise AuthenticationError()
        user = await self._principals.get_user_context(str(claims["tid"]), str(claims["sub"]))
        if user is None:
            raise AuthenticationError()
        return user.model_copy(update={"is_synthetic": True})


class EntraIdIdentityResolver:
    """Placeholder for production sign-in with Microsoft Entra ID (not implemented)."""

    async def resolve(self, token: str) -> UserContext:
        raise AuthenticationError("Entra ID sign-in is not configured.")
