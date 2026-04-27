"""End-to-end auth flow tests against a real Postgres + mocked Authelia."""

from __future__ import annotations

import httpx
import pytest
import respx
from authlib.jose import RSAKey, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.models.user import User

ISSUER = "https://auth.example.com"
DISC = {
    "issuer": ISSUER,
    "authorization_endpoint": f"{ISSUER}/api/oidc/authorization",
    "token_endpoint": f"{ISSUER}/api/oidc/token",
    "jwks_uri": f"{ISSUER}/jwks.json",
    "end_session_endpoint": f"{ISSUER}/api/oidc/logout",
    "id_token_signing_alg_values_supported": ["RS256"],
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code"],
    "code_challenge_methods_supported": ["S256"],
}


@pytest.fixture
def signed_id_token(monkeypatch: pytest.MonkeyPatch) -> tuple[str, dict]:  # type: ignore[type-arg]
    payload = {
        "iss": ISSUER,
        "sub": "subj-1",
        "aud": "trip-tracker",
        "exp": 9999999999,
        "iat": 1000000000,
        "email": "oliver@example.com",
        "preferred_username": "oliver",
        "groups": [],
    }
    key = RSAKey.generate_key(2048, is_private=True)
    token = jwt.encode({"alg": "RS256", "kid": key.kid}, payload, key).decode()
    return token, {"keys": [key.as_dict(is_private=False)]}


@pytest.mark.asyncio
async def test_login_redirects_to_authorize(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{ISSUER}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(200, json=DISC)
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.get("/auth/login", follow_redirects=False)
            assert r.status_code == 302
            assert r.headers["location"].startswith(DISC["authorization_endpoint"])
            assert "tt_oauth_state" in r.cookies
            assert "tt_oauth_pkce" in r.cookies


@pytest.mark.asyncio
async def test_callback_creates_user_and_sets_session(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    signed_id_token: tuple[str, dict],  # type: ignore[type-arg]
    db_session: AsyncSession,
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    id_token, jwks = signed_id_token
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        with respx.mock(assert_all_called=False) as router:
            router.get(f"{ISSUER}/.well-known/openid-configuration").mock(
                return_value=httpx.Response(200, json=DISC)
            )
            router.post(DISC["token_endpoint"]).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id_token": id_token,
                        "access_token": "at",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    },
                )
            )
            router.get(DISC["jwks_uri"]).mock(return_value=httpx.Response(200, json=jwks))

            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
                cookies={"tt_oauth_state": "thestate", "tt_oauth_pkce": "v" * 64},
            ) as client:
                r = await client.get(
                    "/auth/callback",
                    params={"code": "thecode", "state": "thestate"},
                    follow_redirects=False,
                )
                assert r.status_code == 302
                assert "tt_session" in r.cookies

    # User upserted, marked admin (first user).
    user = (
        await db_session.execute(select(User).where(User.oidc_subject == "subj-1"))
    ).scalar_one()
    assert user.is_admin is True
    assert user.email == "oliver@example.com"
