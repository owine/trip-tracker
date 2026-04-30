"""SQLAlchemy ORM event listeners for documents.

Disk cleanup on Document delete. Registered at import time. The Storage
backend is set via `set_storage_for_events(storage)` from app/worker
startup so this module stays import-free of heavy deps.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import event

from trip_tracker.models.document import Document

if TYPE_CHECKING:
    from trip_tracker.documents.storage import StorageBackend

_logger = logging.getLogger(__name__)
_storage: StorageBackend | None = None


def set_storage_for_events(storage: StorageBackend) -> None:
    """Inject the storage backend used by the after_delete listener."""
    global _storage
    _storage = storage


@event.listens_for(Document, "after_delete")
def _document_after_delete(_mapper: Any, _connection: Any, target: Document) -> None:
    """Schedule disk cleanup after the row is deleted."""
    if _storage is None:
        _logger.warning(
            "Document %s deleted but storage not set; orphan file at %s",
            target.id,
            target.storage_key,
        )
        return
    import asyncio

    storage = _storage
    key = target.storage_key
    _task = asyncio.create_task(_safe_delete(storage, key))
    # Prevent the task from being garbage-collected before it completes.
    # The reference is intentionally unused beyond keeping the task alive.
    del _task


async def _safe_delete(storage: StorageBackend, key: str) -> None:
    try:
        await storage.delete(key)
    except Exception:
        _logger.exception("storage.delete failed for key=%s", key)
