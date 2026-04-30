"""Meili client + dependency injection."""

from __future__ import annotations

from trip_tracker.config import Settings
from trip_tracker.search.client import (
    MeiliClientProtocol,
    build_client,
)


def test_protocol_methods_exist() -> None:
    """The Protocol describes the surface our code uses."""
    assert hasattr(MeiliClientProtocol, "index")


def test_build_client_constructs_async_client() -> None:
    """build_client returns a Protocol-conformant AsyncClient instance."""
    s = Settings()  # autouse fixture sets all required env vars
    client = build_client(s)
    assert client is not None
    assert hasattr(client, "index")
