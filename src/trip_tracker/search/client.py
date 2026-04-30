"""Singleton Meili client + dependency injection.

The Protocol shape lets tests inject a MagicMock without subclassing
the real AsyncClient class.
"""

from __future__ import annotations

from typing import Any, Protocol, cast

from fastapi import Request
from meilisearch_python_sdk import AsyncClient

from trip_tracker.config import Settings


class MeiliIndexProtocol(Protocol):
    """The subset of meilisearch_python_sdk.AsyncIndex methods we use."""

    async def update_documents(self, documents: list[dict[str, Any]]) -> Any: ...
    async def delete_document(self, document_id: str) -> Any: ...
    async def search(self, query: str, opt_params: dict[str, Any] | None = None) -> Any: ...


class MeiliClientProtocol(Protocol):
    """The subset of meilisearch_python_sdk.AsyncClient we use."""

    def index(self, uid: str) -> MeiliIndexProtocol: ...
    async def create_index(self, uid: str, primary_key: str | None = None) -> Any: ...
    async def delete_index(self, uid: str) -> Any: ...


def build_client(settings: Settings) -> MeiliClientProtocol:
    """Construct a Meili client from settings. One per process."""
    return cast(
        MeiliClientProtocol,
        AsyncClient(
            url=settings.meili_url,
            api_key=settings.meili_master_key.get_secret_value(),
        ),
    )


async def get_meili(request: Request) -> MeiliClientProtocol:
    """FastAPI dependency. Reads from app.state.meili (set in lifespan)."""
    return request.app.state.meili  # type: ignore[no-any-return]
