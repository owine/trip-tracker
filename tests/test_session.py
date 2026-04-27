"""Signed session cookie helpers."""

from __future__ import annotations

import uuid

import pytest

from trip_tracker.auth.session import (
    SessionExpired,
    SessionPayload,
    SessionTampered,
    decode_session,
    encode_session,
)


def test_round_trip() -> None:
    secret = "x" * 32
    user_id = uuid.uuid4()
    payload = SessionPayload(user_id=user_id, oidc_subject="abc-123")
    cookie = encode_session(payload, secret=secret, max_age=3600)
    decoded = decode_session(cookie, secret=secret, max_age=3600)
    assert decoded.user_id == user_id
    assert decoded.oidc_subject == "abc-123"


def test_tampered_cookie_raises() -> None:
    secret = "x" * 32
    cookie = encode_session(
        SessionPayload(user_id=uuid.uuid4(), oidc_subject="s"), secret=secret, max_age=3600,
    )
    tampered = cookie[:-2] + ("AA" if cookie[-2:] != "AA" else "BB")
    with pytest.raises(SessionTampered):
        decode_session(tampered, secret=secret, max_age=3600)


def test_wrong_secret_raises() -> None:
    cookie = encode_session(
        SessionPayload(user_id=uuid.uuid4(), oidc_subject="s"),
        secret="x" * 32, max_age=3600,
    )
    with pytest.raises(SessionTampered):
        decode_session(cookie, secret="y" * 32, max_age=3600)


def test_expired_cookie_raises() -> None:
    cookie = encode_session(
        SessionPayload(user_id=uuid.uuid4(), oidc_subject="s"),
        secret="x" * 32, max_age=-1,
    )
    with pytest.raises(SessionExpired):
        decode_session(cookie, secret="x" * 32, max_age=1)
