"""ensure_indexes_configured: runs idempotent Meili settings updates."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from trip_tracker.search.client import ensure_indexes_configured


@pytest.mark.asyncio
async def test_ensure_indexes_configured_calls_settings() -> None:
    fake_idx_trips = MagicMock()
    fake_idx_trips.update_filterable_attributes = AsyncMock()
    fake_idx_trips.update_sortable_attributes = AsyncMock()
    fake_idx_segments = MagicMock()
    fake_idx_segments.update_filterable_attributes = AsyncMock()
    fake_idx_segments.update_sortable_attributes = AsyncMock()

    fake_meili = MagicMock()
    fake_meili.create_index = AsyncMock()

    def index_router(name: str):
        return {"trips": fake_idx_trips, "segments": fake_idx_segments}[name]

    fake_meili.index = MagicMock(side_effect=index_router)

    await ensure_indexes_configured(fake_meili)

    fake_idx_trips.update_filterable_attributes.assert_awaited_with(
        ["traveler_ids", "start_date", "end_date"]
    )
    fake_idx_trips.update_sortable_attributes.assert_awaited_with(["start_date"])
    fake_idx_segments.update_filterable_attributes.assert_awaited_with(
        ["traveler_ids", "trip_id", "type", "start_at_unix"]
    )
    fake_idx_segments.update_sortable_attributes.assert_awaited_with(["start_at_unix"])


@pytest.mark.asyncio
async def test_ensure_indexes_configured_swallows_create_index_conflict() -> None:
    """create_index raises on conflict; ensure_indexes_configured swallows.
    Subsequent update_* calls must still fire."""
    fake_idx = MagicMock()
    fake_idx.update_filterable_attributes = AsyncMock()
    fake_idx.update_sortable_attributes = AsyncMock()
    fake_meili = MagicMock()
    fake_meili.create_index = AsyncMock(side_effect=Exception("index_already_exists"))
    fake_meili.index = MagicMock(return_value=fake_idx)

    # Should not raise
    await ensure_indexes_configured(fake_meili)

    # update_* still got called for both indexes
    assert fake_idx.update_filterable_attributes.await_count == 2
    assert fake_idx.update_sortable_attributes.await_count == 2
