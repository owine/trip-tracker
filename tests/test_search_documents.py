"""Documents Meili index: doc renderer + orphan traveler_ids fallback."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.session import OWNER_USER_ID
from trip_tracker.models.document import Document
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User
from trip_tracker.search.sync import document_to_doc


@pytest.mark.asyncio
async def test_document_to_doc_shape(db_session: AsyncSession) -> None:
    u = User(id=OWNER_USER_ID, email="dt1@x.com", display_name="DT1")
    db_session.add(u)
    await db_session.flush()
    t = Trip(
        title="Paris vacation",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 7),
    )
    db_session.add(t)
    await db_session.flush()
    d = Document(
        owner_user_id=u.id,
        trip_id=t.id,
        filename="bp.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="f" * 64,
        storage_key="ff/" + "f" * 64,
        extracted_text="AIR FRANCE",
    )
    db_session.add(d)
    await db_session.commit()

    doc = await document_to_doc(d, db=db_session)
    assert doc["id"] == str(d.id)
    assert doc["filename"] == "bp.pdf"
    assert doc["extracted_text"] == "AIR FRANCE"
    assert str(u.id) in doc["traveler_ids"]
    assert doc["trip_id"] == str(t.id)
    assert doc["segment_id"] is None
    assert isinstance(doc["created_at_unix"], int)


@pytest.mark.asyncio
async def test_orphan_document_traveler_ids_falls_back_to_owner(
    db_session: AsyncSession,
) -> None:
    u = User(id=OWNER_USER_ID, email="dt2@x.com", display_name="DT2")
    db_session.add(u)
    await db_session.flush()
    d = Document(
        owner_user_id=u.id,
        filename="o.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="9" * 64,
        storage_key="99/" + "9" * 64,
    )
    db_session.add(d)
    await db_session.commit()
    doc = await document_to_doc(d, db=db_session)
    assert doc["traveler_ids"] == [str(u.id)]
    assert doc["trip_id"] is None
