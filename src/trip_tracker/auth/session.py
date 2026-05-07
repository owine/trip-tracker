"""HMAC-signed, time-limited session cookies via itsdangerous."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from trip_tracker.config import Settings

_SALT = "trip-tracker.session.v1"

# Well-known UUID for the single owner. Used by the seeding migration,
# the bootstrap route, and current_user. uuid.UUID(int=1) is the literal
# 00000000-0000-0000-0000-000000000001 — chosen for stability across env wipes.
OWNER_USER_ID: uuid.UUID = uuid.UUID(int=1)


class SessionTampered(Exception):
    """Cookie failed signature verification."""


class SessionExpired(Exception):
    """Cookie was valid but past its max-age."""


@dataclass(frozen=True, slots=True)
class SessionPayload:
    user_id: uuid.UUID
    oidc_subject: str


def encode_session(payload: SessionPayload, *, secret: str, max_age: int) -> str:  # noqa: ARG001
    """Return a signed cookie value carrying the payload.

    ``max_age`` is informational; expiry enforcement happens on decode.
    """
    serializer = URLSafeTimedSerializer(secret, salt=_SALT)
    return serializer.dumps({"uid": str(payload.user_id), "sub": payload.oidc_subject})


def decode_session(cookie: str, *, secret: str, max_age: int) -> SessionPayload:
    """Decode and verify a signed session cookie.

    Raises:
        SessionExpired: The cookie signature is valid but has exceeded ``max_age``.
        SessionTampered: The cookie failed signature verification.
    """
    serializer = URLSafeTimedSerializer(secret, salt=_SALT)
    try:
        data = serializer.loads(cookie, max_age=max_age)
    except SignatureExpired as e:
        raise SessionExpired(str(e)) from e
    except BadSignature as e:
        raise SessionTampered(str(e)) from e
    try:
        return SessionPayload(user_id=uuid.UUID(data["uid"]), oidc_subject=data["sub"])
    except (KeyError, ValueError) as e:
        raise SessionTampered(str(e)) from e


# ---------------------------------------------------------------------------
# Single-owner cookie helpers (used by /auth/bootstrap and current_user T4+)
# ---------------------------------------------------------------------------


def _get_serializer(secret: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt=_SALT)


def set_session_cookie(response: Response, user_id: uuid.UUID, settings: Settings) -> None:
    """Sign and attach a session cookie identifying ``user_id`` to ``response``."""
    serializer = _get_serializer(settings.session_secret.get_secret_value())
    payload = serializer.dumps({"user_id": str(user_id)})
    response.set_cookie(
        key=settings.session_cookie_name,
        value=payload,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=True,
        samesite="lax",
    )


def decode_session_cookie(value: str, settings: Settings) -> dict[str, str] | None:
    """Decode and verify a signed session cookie.

    Returns the payload dict on success, or ``None`` on invalid / expired /
    tampered cookie. The ``'user_id'`` key (when present) is a string-form UUID
    — callers must parse it back via ``uuid.UUID()``.
    """
    serializer = _get_serializer(settings.session_secret.get_secret_value())
    try:
        result: dict[str, str] = serializer.loads(value, max_age=settings.session_max_age_seconds)
        return result
    except BadSignature:
        return None
