"""Settings should load from env vars and fail loudly on missing required ones."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trip_tracker.config import Settings, WorkerSettings


def test_settings_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_SESSION_TOKEN", "x" * 32)
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("FORWARDEMAIL_RELAY_TOKEN", "fe-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MEILI_URL", "http://localhost:7700")
    monkeypatch.setenv("MEILI_MASTER_KEY", "meili-key")

    s = Settings(_env_file=None)
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.session_secret.get_secret_value() == "x" * 32
    assert s.owner_email == "owner@example.com"
    assert s.owner_session_token == "x" * 32
    assert s.forwardemail_relay_token.get_secret_value() == "fe-token"


def test_settings_missing_required_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "DATABASE_URL",
        "SESSION_SECRET",
        "OWNER_EMAIL",
        "OWNER_SESSION_TOKEN",
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
        Settings(_env_file=None)


def test_session_secret_minimum_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "tooshort")
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_SESSION_TOKEN", "x" * 32)
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("FORWARDEMAIL_RELAY_TOKEN", "fe-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MEILI_URL", "http://localhost:7700")
    monkeypatch.setenv("MEILI_MASTER_KEY", "meili-key")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_worker_settings_loads_without_app_only_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: WorkerSettings boots with only worker-needed env vars set.

    The whole point of the AppSettings/WorkerSettings split is that the worker
    container doesn't need SESSION/OWNER/BASE_URL/WEBHOOK env vars to start.
    Verify by deleting those and instantiating WorkerSettings.
    """
    for var in (
        "SESSION_SECRET",
        "OWNER_EMAIL",
        "OWNER_SESSION_TOKEN",
        "BASE_URL",
        "WEBHOOK_SECRET",
        "FORWARDEMAIL_RELAY_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MEILI_URL", "http://localhost:7700")
    monkeypatch.setenv("MEILI_MASTER_KEY", "meili-key")

    s = WorkerSettings()
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert s.anthropic_api_key.get_secret_value() == "sk-ant-test"
    # Defaults still apply
    assert s.llm_daily_budget_cents == 100
    assert s.llm_confidence_floor == 0.7


def test_settings_is_a_worker_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings inherits from WorkerSettings — covariance lets functions typed
    `WorkerSettings` accept full Settings instances. Verify the IS-A relationship."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_SESSION_TOKEN", "x" * 32)
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("FORWARDEMAIL_RELAY_TOKEN", "fe-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MEILI_URL", "http://localhost:7700")
    monkeypatch.setenv("MEILI_MASTER_KEY", "meili-key")

    s = Settings(_env_file=None)
    assert isinstance(s, WorkerSettings)


def test_settings_requires_owner_email_and_session_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """OWNER_EMAIL and OWNER_SESSION_TOKEN are required (no defaults).
    OWNER_SESSION_TOKEN must be at least 32 chars."""
    from trip_tracker.config import Settings

    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_SESSION_TOKEN", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://t:t@localhost/t")
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("FORWARDEMAIL_RELAY_TOKEN", "fe-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MEILI_URL", "http://localhost:7700")
    monkeypatch.setenv("MEILI_MASTER_KEY", "meili-key")
    s = Settings(_env_file=None)
    assert s.owner_email == "owner@example.com"
    assert s.owner_session_token == "x" * 32


def test_settings_rejects_short_owner_session_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from trip_tracker.config import Settings

    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_SESSION_TOKEN", "tooshort")
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://t:t@localhost/t")
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("FORWARDEMAIL_RELAY_TOKEN", "fe-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MEILI_URL", "http://localhost:7700")
    monkeypatch.setenv("MEILI_MASTER_KEY", "meili-key")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_no_longer_has_oidc_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    from trip_tracker.config import Settings

    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_SESSION_TOKEN", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://t:t@localhost/t")
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("FORWARDEMAIL_RELAY_TOKEN", "fe-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("MEILI_URL", "http://localhost:7700")
    monkeypatch.setenv("MEILI_MASTER_KEY", "meili-key")
    s = Settings(_env_file=None)
    assert not hasattr(s, "oidc_issuer")
    assert not hasattr(s, "oidc_client_id")
    assert not hasattr(s, "admin_group")
