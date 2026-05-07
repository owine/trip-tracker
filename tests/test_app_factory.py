"""Smoke tests for the FastAPI app factory."""

from __future__ import annotations

import pytest

from trip_tracker.app import create_app


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch):  # type: ignore[return]
    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_SESSION_TOKEN", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    from trip_tracker.config import Settings

    return create_app(Settings())


def test_create_app_returns_app(app) -> None:  # type: ignore[no-untyped-def]
    routes = {r.path for r in app.routes}  # type: ignore[union-attr]
    assert "/healthz" in routes
    assert "/auth/bootstrap" in routes
    assert "/api/ingest/email" in routes
    # OIDC routes are gone; single-user auth is bootstrap-only
    assert "/auth/login" not in routes
    assert "/auth/callback" not in routes
