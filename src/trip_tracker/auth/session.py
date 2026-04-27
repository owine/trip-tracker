"""HMAC-signed, time-limited session cookies via itsdangerous."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

_SALT = "trip-tracker.session.v1"


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
