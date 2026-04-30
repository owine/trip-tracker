"""Phase 4 settings: Meilisearch URL + master key."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trip_tracker.config import Settings


def test_meili_url_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEILI_URL", raising=False)
    with pytest.raises(ValidationError, match="meili_url"):
        Settings()


def test_meili_master_key_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEILI_MASTER_KEY", raising=False)
    with pytest.raises(ValidationError, match="meili_master_key"):
        Settings()


def test_meili_master_key_is_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """SecretStr — never leaks in repr or log output."""
    monkeypatch.setenv("MEILI_URL", "http://localhost:7700")
    monkeypatch.setenv("MEILI_MASTER_KEY", "super-secret-32-byte-value")
    s = Settings()
    assert "super-secret-32-byte-value" not in repr(s)
    assert s.meili_master_key.get_secret_value() == "super-secret-32-byte-value"
