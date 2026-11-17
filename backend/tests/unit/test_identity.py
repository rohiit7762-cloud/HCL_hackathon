"""Identity resolution (I1): tokens carry only IDs; roles come from server data."""

from __future__ import annotations

import time

import jwt
import pytest

from app.core.errors import AuthenticationError
from app.security.identity import DemoTokenIdentityResolver
from tests.fakes.data_layer import InMemoryPrincipals

SECRET = "s" * 40


def resolver() -> DemoTokenIdentityResolver:
    return DemoTokenIdentityResolver(InMemoryPrincipals(), SECRET, ttl_minutes=5)


async def test_round_trip_loads_roles_from_server() -> None:
    token, ttl = await resolver().issue("lumen", "E103")
    user = await resolver().resolve(token)
    assert user.employee_id == "E103" and "hr" in user.roles and user.is_synthetic
    assert ttl == 300


async def test_i1_role_claims_in_token_are_ignored() -> None:
    forged = jwt.encode(
        {
            "sub": "E100",
            "tid": "lumen",
            "typ": "demo",
            "exp": int(time.time()) + 60,
            "roles": ["hr", "admin"],
            "groups": ["finance"],
        },
        SECRET,
        algorithm="HS256",
    )
    user = await resolver().resolve(forged)
    assert "hr" not in user.roles and "finance" not in user.groups


@pytest.mark.parametrize(
    "token",
    [
        jwt.encode(
            {"sub": "E100", "tid": "lumen", "typ": "demo", "exp": int(time.time()) + 60},
            "wrong-secret-" * 4,
            algorithm="HS256",
        ),
        jwt.encode(
            {"sub": "E100", "tid": "lumen", "typ": "demo", "exp": int(time.time()) - 10},
            SECRET,
            algorithm="HS256",
        ),
        jwt.encode(
            {"sub": "E999", "tid": "lumen", "typ": "demo", "exp": int(time.time()) + 60},
            SECRET,
            algorithm="HS256",
        ),
        "not-a-token",
    ],
)
async def test_bad_tokens_are_rejected(token: str) -> None:
    with pytest.raises(AuthenticationError):
        await resolver().resolve(token)


def test_short_secret_rejected() -> None:
    with pytest.raises(ValueError):
        DemoTokenIdentityResolver(InMemoryPrincipals(), "short", 5)
