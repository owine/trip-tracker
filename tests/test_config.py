"""Settings should load from env vars and fail loudly on missing required ones."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trip_tracker.config import Settings


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("OIDC_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "trip-tracker")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://trips.example.com/auth/callback")
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("FORWARDEMAIL_RELAY_TOKEN", "fe-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MEILI_URL", "http://localhost:7700")
    monkeypatch.setenv("MEILI_MASTER_KEY", "meili-key")

    s = Settings()
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.session_secret.get_secret_value() == "x" * 32
    assert s.oidc_issuer == "https://auth.example.com"
    assert s.admin_group == "trip-tracker:admin"  # default
    assert s.forwardemail_relay_token.get_secret_value() == "fe-token"


def test_settings_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "DATABASE_URL",
        "SESSION_SECRET",
        "OIDC_ISSUER",
        "OIDC_CLIENT_ID",
        "OIDC_CLIENT_SECRET",
        "OIDC_REDIRECT_URI",
        "BASE_URL",
        "WEBHOOK_SECRET",
        "FORWARDEMAIL_RELAY_TOKEN",
        "ANTHROPIC_API_KEY",
        "REDIS_URL",
        "MEILI_URL",
        "MEILI_MASTER_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_session_secret_minimum_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "tooshort")
    monkeypatch.setenv("OIDC_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "trip-tracker")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "s")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://trips.example.com/auth/callback")
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("FORWARDEMAIL_RELAY_TOKEN", "fe-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MEILI_URL", "http://localhost:7700")
    monkeypatch.setenv("MEILI_MASTER_KEY", "meili-key")
    with pytest.raises(ValidationError):
        Settings()
