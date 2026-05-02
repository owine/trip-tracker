"""Singleton Meili client + dependency injection.

The Protocol shape lets tests inject a MagicMock without subclassing
the real AsyncClient class.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any, Protocol, cast

from fastapi import Request
from meilisearch_python_sdk import AsyncClient

from trip_tracker.config import WorkerSettings

logger = logging.getLogger(__name__)


class MeiliIndexProtocol(Protocol):
    """The subset of meilisearch_python_sdk.AsyncIndex methods we use."""

    async def update_documents(self, documents: list[dict[str, Any]]) -> Any: ...
    async def delete_document(self, document_id: str) -> Any: ...
    async def search(
        self,
        query: str | None = None,
        *,
        filter: str | None = None,
        limit: int = 20,
    ) -> Any: ...
    async def update_filterable_attributes(self, attrs: list[str]) -> Any: ...
    async def update_sortable_attributes(self, attrs: list[str]) -> Any: ...


class MeiliClientProtocol(Protocol):
    """The subset of meilisearch_python_sdk.AsyncClient we use."""

    def index(self, uid: str) -> MeiliIndexProtocol: ...
    async def create_index(self, uid: str, primary_key: str | None = None) -> Any: ...
    async def delete_index(self, uid: str) -> Any: ...


_TRIP_FILTERABLE = ["traveler_ids", "start_date", "end_date"]
_TRIP_SORTABLE = ["start_date"]
_SEGMENT_FILTERABLE = ["traveler_ids", "trip_id", "type", "start_at_unix"]
_SEGMENT_SORTABLE = ["start_at_unix"]
_DOCUMENTS_FILTERABLE = ["traveler_ids", "trip_id", "segment_id", "owner_user_id"]
_DOCUMENTS_SORTABLE = ["created_at_unix"]


async def ensure_indexes_configured(meili: MeiliClientProtocol) -> None:
    """Ensure both indexes exist with the right filterable/sortable attrs.

    Idempotent. Run on app startup.
    """
    for name, filterable, sortable in (
        ("trips", _TRIP_FILTERABLE, _TRIP_SORTABLE),
        ("segments", _SEGMENT_FILTERABLE, _SEGMENT_SORTABLE),
        ("documents", _DOCUMENTS_FILTERABLE, _DOCUMENTS_SORTABLE),
    ):
        with contextlib.suppress(Exception):
            # Meili raises on conflict if the index already exists — idempotent.
            await meili.create_index(name, primary_key="id")
        idx = meili.index(name)
        await idx.update_filterable_attributes(filterable)
        await idx.update_sortable_attributes(sortable)
        logger.info("Meili index %r configured", name)


def build_client(settings: WorkerSettings) -> MeiliClientProtocol:
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
