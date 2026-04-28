"""Settings validators for webhook env vars."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trip_tracker.config import Settings


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-set required Phase 1 envs (autouse fixture cleared via per-test monkeypatch)."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("OIDC_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "trip-tracker")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://trips.example.com/auth/callback")
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("WEBHOOK_SECRET", "x" * 32)


def test_signature_header_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    s = Settings()
    assert s.webhook_signature_header == "X-Webhook-Signature"


def test_signature_header_empty_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBHOOK_SIGNATURE_HEADER", "   ")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    "header",
    ["Authorization", "cookie", "Host", "Content-Length", "X-Forwarded-For"],
)
def test_signature_header_reserved_rejected(monkeypatch: pytest.MonkeyPatch, header: str) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBHOOK_SIGNATURE_HEADER", header)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("seconds", [0, -1, 3601, 100_000])
def test_tolerance_out_of_range_rejected(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS", str(seconds))
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("size", [0, -1, 200 * 1024 * 1024])
def test_max_body_out_of_range_rejected(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBHOOK_MAX_BODY_BYTES", str(size))
    with pytest.raises(ValidationError):
        Settings()
