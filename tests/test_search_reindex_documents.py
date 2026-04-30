"""reindex extension: third walk for Documents."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from trip_tracker.models.document import Document
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.search.reindex import reindex_all


@pytest.mark.asyncio
async def test_reindex_walks_documents(db_url: str, db_session: AsyncSession) -> None:
    u = User(oidc_subject="rd1", email="rd1@x.com", display_name="RD1")
    db_session.add(u)
    await db_session.flush()
    t = Trip(
        title="T",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 2),
        created_by=u.id,
    )
    db_session.add(t)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    db_session.add(
        Document(
            owner_user_id=u.id,
            trip_id=t.id,
            filename="r.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="1" * 64,
            storage_key="11/" + "1" * 64,
            extract_status="extracted",
            extracted_text="hello",
        )
    )
    await db_session.commit()

    indexes = {n: MagicMock() for n in ("trips", "segments", "documents")}
    for idx in indexes.values():
        idx.update_documents = AsyncMock()
        idx.update_filterable_attributes = AsyncMock()
        idx.update_sortable_attributes = AsyncMock()
    fake_meili = MagicMock()
    fake_meili.delete_index = AsyncMock()
    fake_meili.create_index = AsyncMock()
    fake_meili.index = MagicMock(side_effect=lambda n: indexes[n])

    engine = create_async_engine(db_url)
    counts = await reindex_all(engine, fake_meili, batch_size=50)
    await engine.dispose()

    assert counts["documents"] == 1
    indexes["documents"].update_documents.assert_awaited()
