"""HMAC-signed, time-limited session cookies via itsdangerous."""

from __future__ import annotations

import uuid

from fastapi import Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

from trip_tracker.config import Settings

_SALT = "trip-tracker.session.v1"

# Well-known UUID for the single owner. Used by the seeding migration,
# the bootstrap route, and current_user. uuid.UUID(int=1) is the literal
# 00000000-0000-0000-0000-000000000001 — chosen for stability across env wipes.
OWNER_USER_ID: uuid.UUID = uuid.UUID(int=1)


# ---------------------------------------------------------------------------
# Single-owner cookie helpers (used by /auth/bootstrap and current_user)
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
