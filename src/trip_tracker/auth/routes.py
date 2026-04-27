"""OIDC login / callback / logout endpoints."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.oidc import OIDCClient, OIDCDiscovery
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.models.user import User

router = APIRouter(prefix="/auth", tags=["auth"])

_OAUTH_STATE_COOKIE = "tt_oauth_state"
_OAUTH_PKCE_COOKIE = "tt_oauth_pkce"


def _settings_dep() -> Settings:
    return Settings()


async def _client_dep(settings: Settings = Depends(_settings_dep)) -> OIDCClient:  # noqa: B008
    async with httpx.AsyncClient() as http:
        discovery = await OIDCDiscovery.fetch(settings.oidc_issuer, client=http)
    return OIDCClient(
        discovery=discovery,
        client_id=settings.oidc_client_id,
        client_secret=settings.oidc_client_secret.get_secret_value(),
        redirect_uri=settings.oidc_redirect_uri,
    )


@router.get("/login")
async def login(client: OIDCClient = Depends(_client_dep)) -> RedirectResponse:  # noqa: B008
    url, state, verifier = client.build_authorize_url(
        scopes=["openid", "profile", "email", "groups"]
    )
    response = RedirectResponse(url, status_code=302)
    secure_cookie = url.startswith("https://")
    response.set_cookie(
        _OAUTH_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
    )
    response.set_cookie(
        _OAUTH_PKCE_COOKIE,
        verifier,
        max_age=600,
        httponly=True,
        secure=secure_cookie,
        samesite="lax",
    )
    return response


@router.get("/callback")
async def callback(
    request: Request,  # noqa: ARG001
    code: str,
    state: str,
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(_settings_dep),  # noqa: B008
    client: OIDCClient = Depends(_client_dep),  # noqa: B008
    tt_oauth_state: str | None = Cookie(default=None),
    tt_oauth_pkce: str | None = Cookie(default=None),
) -> RedirectResponse:
    if not tt_oauth_state or not tt_oauth_pkce:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="missing oauth cookies")
    if tt_oauth_state != state:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="state mismatch")

    async with httpx.AsyncClient() as http:
        claims = await client.exchange_code(code=code, verifier=tt_oauth_pkce, http=http)

    # Upsert user.
    user = (
        await db.execute(select(User).where(User.oidc_subject == claims.sub))
    ).scalar_one_or_none()
    if user is None:
        # First-user-is-admin or explicit group membership.
        existing_count = (await db.execute(select(func.count()).select_from(User))).scalar_one()
        is_admin = existing_count == 0 or settings.admin_group in claims.groups
        user = User(
            oidc_subject=claims.sub,
            email=claims.email,
            display_name=claims.preferred_username or claims.email,
            is_admin=is_admin,
        )
        db.add(user)
    else:
        user.email = claims.email
        if claims.preferred_username:
            user.display_name = claims.preferred_username
        if settings.admin_group in claims.groups:
            user.is_admin = True
    await db.commit()
    await db.refresh(user)

    cookie_value = encode_session(
        SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
        secret=settings.session_secret.get_secret_value(),
        max_age=settings.session_max_age_seconds,
    )
    response = RedirectResponse("/", status_code=302)
    secure = settings.base_url.startswith("https://")
    response.set_cookie(
        settings.session_cookie_name,
        cookie_value,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    response.delete_cookie(_OAUTH_STATE_COOKIE)
    response.delete_cookie(_OAUTH_PKCE_COOKIE)
    return response


@router.get("/logout")
async def logout(
    settings: Settings = Depends(_settings_dep),  # noqa: B008
    client: OIDCClient = Depends(_client_dep),  # noqa: B008
) -> RedirectResponse:
    target = client.discovery.end_session_endpoint or settings.base_url
    response = RedirectResponse(target, status_code=302)
    response.delete_cookie(settings.session_cookie_name)
    return response
