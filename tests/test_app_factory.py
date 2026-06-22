"""Smoke tests for the FastAPI app factory."""

from __future__ import annotations

from starlette.routing import BaseRoute

from trip_tracker.app import create_app


def _iter_paths(routes: list[BaseRoute]) -> set[str]:
    """Collect every registered path from a (possibly nested) route list.

    FastAPI 0.137 stopped cloning included routes into a flat list: ``app.routes``
    is now a tree where each ``include_router`` call appears as a wrapper node
    (``_IncludedRouter``) that has no ``path`` and delegates to its
    ``original_router``. Recurse through both that wrapper and any mounted
    sub-apps (``routes``) so the smoke test sees every leaf path on both the old
    flat layout and the new tree layout.
    """
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if path is not None:
            paths.add(path)
        sub = getattr(route, "routes", None)
        if sub is None:
            inner = getattr(route, "original_router", None)
            sub = getattr(inner, "routes", None) if inner is not None else None
        if sub:
            paths |= _iter_paths(sub)
    return paths


def test_create_app_returns_app() -> None:
    app = create_app()
    routes = _iter_paths(app.routes)
    assert "/healthz" in routes
    assert "/auth/login" in routes
    assert "/auth/callback" in routes
    assert "/auth/logout" in routes
    assert "/api/ingest/email" in routes
