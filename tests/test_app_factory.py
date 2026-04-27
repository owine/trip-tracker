"""Smoke tests for the FastAPI app factory."""

from __future__ import annotations

from trip_tracker.app import create_app


def test_create_app_returns_app() -> None:
    app = create_app()
    routes = {r.path for r in app.routes}
    assert "/healthz" in routes
    assert "/auth/login" in routes
    assert "/auth/callback" in routes
    assert "/auth/logout" in routes
