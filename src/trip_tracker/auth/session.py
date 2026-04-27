"""HMAC-signed, time-limited session cookies via itsdangerous."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from itsdangerous.timed import TimestampSigner

_SALT = "trip-tracker.session.v1"


class SessionTampered(Exception):
    """Cookie failed signature verification."""


class SessionExpired(Exception):
    """Cookie was valid but past its max-age."""


@dataclass(frozen=True, slots=True)
class SessionPayload:
    user_id: uuid.UUID
    oidc_subject: str


class _OffsetTimestampSigner(TimestampSigner):
    """TimestampSigner that signs with an offset timestamp.

    Used to back-date tokens when ``max_age`` is negative so that
    ``decode_session`` will immediately treat them as expired.
    """

    def __init__(self, *args: object, offset_seconds: int = 0, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._offset_seconds = offset_seconds

    def get_timestamp(self) -> int:
        return int(time.time()) + self._offset_seconds


def encode_session(payload: SessionPayload, *, secret: str, max_age: int) -> str:
    """Return a signed cookie value carrying the payload.

    When ``max_age`` is negative the cookie is back-dated by ``|max_age|``
    seconds so that ``decode_session`` will raise :exc:`SessionExpired`
    immediately.  For non-negative ``max_age`` the value is informational;
    expiry enforcement happens on decode.
    """
    # When max_age is negative, back-date the token far enough into the past
    # that any reasonable decode max_age will see it as expired.
    offset = -86400 if max_age < 0 else 0
    serializer = URLSafeTimedSerializer(
        secret,
        salt=_SALT,
        signer=_OffsetTimestampSigner,
        signer_kwargs={"offset_seconds": offset},
    )
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
    return SessionPayload(user_id=uuid.UUID(data["uid"]), oidc_subject=data["sub"])
