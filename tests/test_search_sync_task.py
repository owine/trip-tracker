"""sync_meili saq task: upserts on existing rows, deletes on missing rows."""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from trip_tracker.auth.session import OWNER_USER_ID
from trip_tracker.config import Settings
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User
from trip_tracker.search.client import MeiliClientProtocol


@pytest.mark.asyncio
async def test_sync_meili_upserts_existing_trip(db_url: str, db_session: AsyncSession) -> None:
    from trip_tracker.worker import sync_meili

    user = User(id=OWNER_USER_ID, email="m1@x.com", display_name="M1")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T1", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5))
    db_session.add(trip)
    await db_session.commit()

    fake_index = MagicMock()
    fake_index.update_documents = AsyncMock()
    fake_index.delete_document = AsyncMock()
    fake_meili = MagicMock(spec=MeiliClientProtocol)
    fake_meili.index = MagicMock(return_value=fake_index)

    engine = create_async_engine(db_url)
    ctx = {"settings": Settings(), "engine": engine, "meili": fake_meili}

    await sync_meili(ctx, entity="trip", entity_id=str(trip.id))

    fake_meili.index.assert_called_with("trips")
    fake_index.update_documents.assert_awaited_once()
    docs = fake_index.update_documents.call_args[0][0]
    assert docs[0]["id"] == str(trip.id)
    assert docs[0]["title"] == "T1"

    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_meili_deletes_when_row_missing(db_url: str, db_session: AsyncSession) -> None:
    """If the entity isn't in Postgres (deleted), issue a Meili delete instead."""
    from trip_tracker.worker import sync_meili

    fake_index = MagicMock()
    fake_index.update_documents = AsyncMock()
    fake_index.delete_document = AsyncMock()
    fake_meili = MagicMock(spec=MeiliClientProtocol)
    fake_meili.index = MagicMock(return_value=fake_index)

    bogus_id = uuid.uuid4()
    engine = create_async_engine(db_url)
    ctx = {"settings": Settings(), "engine": engine, "meili": fake_meili}

    await sync_meili(ctx, entity="segment", entity_id=str(bogus_id))

    fake_meili.index.assert_called_with("segments")
    fake_index.delete_document.assert_awaited_once_with(str(bogus_id))
    fake_index.update_documents.assert_not_called()

    await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_meili_sync_uses_stable_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enqueue_meili_sync sets a stable `key` per (entity, id).

    saq 0.26 doesn't accept `unique` as a Job field — it would fall through
    as a function kwarg and crash sync_meili. We rely on `key=` for
    job-status lookups; concurrent enqueues may double-process briefly,
    which is acceptable for our workload.
    """
    from trip_tracker.search.sync import enqueue_meili_sync

    fake_queue = MagicMock()
    fake_queue.enqueue = AsyncMock()
    fake_queue.disconnect = AsyncMock()

    monkeypatch.setattr("trip_tracker.search.sync._build_queue", lambda settings: fake_queue)

    seg_id = uuid.uuid4()
    await enqueue_meili_sync(Settings(), entity="segment", entity_id=seg_id)

    fake_queue.enqueue.assert_awaited_once()
    kwargs = fake_queue.enqueue.call_args.kwargs
    assert kwargs.get("entity") == "segment"
    assert kwargs.get("entity_id") == str(seg_id)
    assert "unique" not in kwargs  # explicitly NOT passed (saq compat)
    assert "meili_sync:segment:" in kwargs.get("key", "")
