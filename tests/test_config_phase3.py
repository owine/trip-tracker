"""Phase 3 settings: Anthropic + Redis + LLM budget config."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trip_tracker.config import Settings


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set required Phase 1/2 envs."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("OIDC_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "trip-tracker")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://trips.example.com/auth/callback")
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("WEBHOOK_SECRET", "x" * 32)


def test_anthropic_api_key_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """ANTHROPIC_API_KEY must be set."""
    _base_env(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    with pytest.raises(ValidationError, match="anthropic_api_key"):
        Settings()


def test_redis_url_required(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(ValidationError, match="redis_url"):
        Settings()


def test_llm_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM_DAILY_BUDGET_CENTS, LLM_MODEL, LLM_CONFIDENCE_FLOOR have defaults."""
    _base_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    s = Settings()
    assert s.llm_daily_budget_cents == 100
    assert s.llm_model == "claude-haiku-4-5-20251001"
    assert s.llm_confidence_floor == pytest.approx(0.7)


def test_llm_confidence_floor_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confidence floor must be in [0, 1]."""
    _base_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("LLM_CONFIDENCE_FLOOR", "1.5")
    with pytest.raises(ValidationError):
        Settings()
