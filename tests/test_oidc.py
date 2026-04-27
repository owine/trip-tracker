"""OIDC client: authorize URL, token exchange, ID token validation."""

from __future__ import annotations

import httpx
import pytest
import respx

from trip_tracker.auth.oidc import (
    OIDCClaims,
    OIDCClient,
    OIDCDiscovery,
    OIDCTokenError,
)

ISSUER = "https://auth.example.com"
DISCOVERY = {
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


@pytest.mark.asyncio
async def test_discovery_fetches_metadata() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{ISSUER}/.well-known/openid-configuration").mock(
            return_value=httpx.Response(200, json=DISCOVERY)
        )
        disc = await OIDCDiscovery.fetch(ISSUER, client=httpx.AsyncClient())
        assert disc.token_endpoint == DISCOVERY["token_endpoint"]


def test_build_authorize_url_includes_pkce() -> None:
    disc = OIDCDiscovery(**DISCOVERY)
    client = OIDCClient(
        discovery=disc,
        client_id="trip-tracker",
        client_secret="secret",
        redirect_uri="https://trips.example.com/auth/callback",
    )
    url, state, verifier = client.build_authorize_url(
        scopes=["openid", "profile", "email", "groups"]
    )
    assert url.startswith(disc.authorization_endpoint)
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url
    assert f"state={state}" in url
    assert "scope=openid+profile+email+groups" in url
    assert len(verifier) >= 43  # PKCE verifier minimum length per RFC 7636


@pytest.mark.asyncio
async def test_exchange_code_returns_claims() -> None:
    """Use a freshly generated RSA key, sign an ID token, and verify the client validates it."""
    # Use authlib.jose.RSAKey (modern import path; rfc7517 sub-import removed in 1.x)
    from authlib.jose import RSAKey, jwt

    key = RSAKey.generate_key(2048, is_private=True)
    public_jwks = {"keys": [key.as_dict(is_private=False)]}

    id_token_payload = {
        "iss": ISSUER,
        "sub": "abc-123",
        "aud": "trip-tracker",
        "exp": 9999999999,
        "iat": 1000000000,
        "email": "oliver@example.com",
        "preferred_username": "oliver",
        "groups": ["trip-tracker:admin"],
    }
    id_token = jwt.encode({"alg": "RS256", "kid": key.kid}, id_token_payload, key).decode()

    with respx.mock(assert_all_called=False) as router:
        router.post(DISCOVERY["token_endpoint"]).mock(
            return_value=httpx.Response(200, json={
                "id_token": id_token,
                "access_token": "at",
                "token_type": "Bearer",
                "expires_in": 3600,
            })
        )
        router.get(DISCOVERY["jwks_uri"]).mock(return_value=httpx.Response(200, json=public_jwks))

        client = OIDCClient(
            discovery=OIDCDiscovery(**DISCOVERY),
            client_id="trip-tracker",
            client_secret="secret",
            redirect_uri="https://trips.example.com/auth/callback",
        )
        claims = await client.exchange_code(
            code="theauthcode",
            verifier="v" * 64,
            http=httpx.AsyncClient(),
        )
        assert isinstance(claims, OIDCClaims)
        assert claims.sub == "abc-123"
        assert claims.email == "oliver@example.com"
        assert "trip-tracker:admin" in claims.groups


@pytest.mark.asyncio
async def test_token_endpoint_error_raises() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.post(DISCOVERY["token_endpoint"]).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant"})
        )
        client = OIDCClient(
            discovery=OIDCDiscovery(**DISCOVERY),
            client_id="trip-tracker",
            client_secret="secret",
            redirect_uri="https://trips.example.com/auth/callback",
        )
        with pytest.raises(OIDCTokenError):
            await client.exchange_code(code="x", verifier="v" * 64, http=httpx.AsyncClient())
