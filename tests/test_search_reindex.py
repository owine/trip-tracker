"""reindex CLI: walks Postgres, batch-upserts to Meili."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.search.reindex import reindex_all


@pytest.mark.asyncio
async def test_reindex_walks_all_rows(db_url: str, db_session: AsyncSession) -> None:
    user = User(oidc_subject="r1", email="r1@x.com", display_name="R1")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(
        title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5), created_by=user.id
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    db_session.add(
        Segment(
            trip_id=trip.id,
            owner_user_id=user.id,
            type="flight",
            status="confirmed",
            start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
            start_tz="UTC",
            parse_source="manual",
            parse_confidence=1.0,
        )
    )
    await db_session.commit()

    fake_idx_trips = MagicMock()
    fake_idx_trips.update_documents = AsyncMock()
    fake_idx_trips.update_filterable_attributes = AsyncMock()
    fake_idx_trips.update_sortable_attributes = AsyncMock()
    fake_idx_segments = MagicMock()
    fake_idx_segments.update_documents = AsyncMock()
    fake_idx_segments.update_filterable_attributes = AsyncMock()
    fake_idx_segments.update_sortable_attributes = AsyncMock()
    fake_idx_documents = MagicMock()
    fake_idx_documents.update_documents = AsyncMock()
    fake_idx_documents.update_filterable_attributes = AsyncMock()
    fake_idx_documents.update_sortable_attributes = AsyncMock()
    fake_meili = MagicMock()
    fake_meili.delete_index = AsyncMock()
    fake_meili.create_index = AsyncMock()

    def fake_index(n: str):
        return {
            "trips": fake_idx_trips,
            "segments": fake_idx_segments,
            "documents": fake_idx_documents,
        }[n]

    fake_meili.index = MagicMock(side_effect=fake_index)

    engine = create_async_engine(db_url)
    counts = await reindex_all(engine, fake_meili, batch_size=100)
    await engine.dispose()

    assert counts["trips"] == 1
    assert counts["segments"] == 1
    fake_idx_trips.update_documents.assert_awaited()
    fake_idx_segments.update_documents.assert_awaited()


@pytest.mark.asyncio
async def test_reindex_dry_run_skips_meili(db_url: str, db_session: AsyncSession) -> None:
    # Seed at least one trip+segment so the walk has rows to traverse —
    # otherwise the dry-run early-return would mask any actual write attempt.
    user = User(oidc_subject="dr1", email="dr1@x.com", display_name="DR1")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(
        title="DR", start_date=date(2026, 7, 1), end_date=date(2026, 7, 2), created_by=user.id
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    db_session.add(
        Segment(
            trip_id=trip.id,
            owner_user_id=user.id,
            type="flight",
            status="confirmed",
            start_at=datetime(2026, 7, 1, 9, tzinfo=UTC),
            start_tz="UTC",
            parse_source="manual",
            parse_confidence=1.0,
        )
    )
    await db_session.commit()

    fake_meili = MagicMock()
    fake_meili.delete_index = AsyncMock()
    fake_meili.create_index = AsyncMock()
    fake_idx = MagicMock()
    fake_idx.update_documents = AsyncMock()
    fake_idx.update_filterable_attributes = AsyncMock()
    fake_idx.update_sortable_attributes = AsyncMock()
    fake_meili.index = MagicMock(return_value=fake_idx)

    engine = create_async_engine(db_url)
    counts = await reindex_all(engine, fake_meili, batch_size=100, dry_run=True)
    await engine.dispose()

    # Spec §8.1: dry-run reports zero "indexed" since nothing was sent.
    assert counts == {"trips": 0, "segments": 0, "documents": 0}
    fake_idx.update_documents.assert_not_called()
    fake_meili.delete_index.assert_not_called()
