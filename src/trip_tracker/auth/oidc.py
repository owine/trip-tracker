"""OIDC Authorization Code + PKCE client.

Discovery, authorize URL, token exchange, ID token validation.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
from joserfc import jwt
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry
from pydantic import BaseModel, ConfigDict, Field


class OIDCError(Exception):
    """Base OIDC client error."""


class OIDCDiscoveryError(OIDCError):
    pass


class OIDCTokenError(OIDCError):
    pass


class OIDCIDTokenInvalid(OIDCError):
    pass


@dataclass(frozen=True, slots=True)
class OIDCDiscovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    end_session_endpoint: str | None = None
    id_token_signing_alg_values_supported: list[str] = field(default_factory=list)
    response_types_supported: list[str] = field(default_factory=list)
    grant_types_supported: list[str] = field(default_factory=list)
    code_challenge_methods_supported: list[str] = field(default_factory=list)

    @classmethod
    async def fetch(cls, issuer: str, *, client: httpx.AsyncClient) -> OIDCDiscovery:
        url = issuer.rstrip("/") + "/.well-known/openid-configuration"
        r = await client.get(url, timeout=10.0)
        if r.status_code != 200:
            raise OIDCDiscoveryError(f"discovery {url} returned {r.status_code}")
        # JSON is untyped by nature; Any is correct here.
        data: dict[str, Any] = r.json()
        # Accept and ignore unknown fields.
        return cls(
            issuer=data["issuer"],
            authorization_endpoint=data["authorization_endpoint"],
            token_endpoint=data["token_endpoint"],
            jwks_uri=data["jwks_uri"],
            end_session_endpoint=data.get("end_session_endpoint"),
            id_token_signing_alg_values_supported=data.get(
                "id_token_signing_alg_values_supported", []
            ),
            response_types_supported=data.get("response_types_supported", []),
            grant_types_supported=data.get("grant_types_supported", []),
            code_challenge_methods_supported=data.get("code_challenge_methods_supported", []),
        )


class OIDCClaims(BaseModel):
    """Validated ID token claims we care about."""

    model_config = ConfigDict(extra="ignore")
    sub: str
    email: str
    preferred_username: str | None = None
    groups: list[str] = Field(default_factory=list)


def _pkce_pair() -> tuple[str, str]:
    """Return (verifier, challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


@dataclass
class OIDCClient:
    discovery: OIDCDiscovery
    client_id: str
    client_secret: str
    redirect_uri: str

    def build_authorize_url(self, *, scopes: list[str]) -> tuple[str, str, str]:
        """Return (url, state, code_verifier)."""
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(32)
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{self.discovery.authorization_endpoint}?{urlencode(params)}", state, verifier

    async def exchange_code(
        self,
        *,
        code: str,
        verifier: str,
        http: httpx.AsyncClient,
    ) -> OIDCClaims:
        r = await http.post(
            self.discovery.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code_verifier": verifier,
            },
            headers={"Accept": "application/json"},
            timeout=10.0,
        )
        if r.status_code != 200:
            raise OIDCTokenError(f"token endpoint {r.status_code}: {r.text}")
        # JSON is untyped by nature; Any is correct here.
        body: dict[str, Any] = r.json()
        id_token = body.get("id_token")
        if not id_token:
            raise OIDCTokenError("token response missing id_token")

        # Fetch JWKS and verify signature + claims.
        jwks_resp = await http.get(self.discovery.jwks_uri, timeout=10.0)
        if jwks_resp.status_code != 200:
            raise OIDCIDTokenInvalid(f"jwks fetch {jwks_resp.status_code}")
        keyset = KeySet.import_key_set(jwks_resp.json())

        # joserfc requires explicit algorithms list (security: no default = no
        # algorithm-confusion attacks). Prefer what the IdP advertises in
        # discovery; fall back to RS256 (≈99% of OIDC IdPs in practice).
        algorithms = self.discovery.id_token_signing_alg_values_supported or ["RS256"]

        try:
            token = jwt.decode(id_token, keyset, algorithms=algorithms)
            claims_registry = JWTClaimsRegistry(
                iss={"essential": True, "value": self.discovery.issuer},
                aud={"essential": True, "value": self.client_id},
                exp={"essential": True},
            )
            claims_registry.validate(token.claims)
        except Exception as e:
            raise OIDCIDTokenInvalid(str(e)) from e

        return OIDCClaims.model_validate(token.claims)
