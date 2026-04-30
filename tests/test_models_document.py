"""Document ORM + cascade tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.document import Document
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


@pytest.mark.asyncio
async def test_document_unique_owner_sha256(db_session: AsyncSession) -> None:
    u = User(oidc_subject="d1", email="d1@x.com", display_name="D1")
    db_session.add(u)
    await db_session.flush()
    db_session.add(
        Document(
            owner_user_id=u.id,
            filename="a.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="a" * 64,
            storage_key="aa/" + "a" * 64,
        )
    )
    await db_session.commit()
    db_session.add(
        Document(
            owner_user_id=u.id,
            filename="b.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="a" * 64,
            storage_key="aa/" + "a" * 64,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_trip_delete_cascades_documents(db_session: AsyncSession) -> None:
    u = User(oidc_subject="d2", email="d2@x.com", display_name="D2")
    db_session.add(u)
    await db_session.flush()
    t = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 2), created_by=u.id)
    db_session.add(t)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    db_session.add(
        Document(
            owner_user_id=u.id,
            trip_id=t.id,
            filename="x.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="b" * 64,
            storage_key="bb/" + "b" * 64,
        )
    )
    await db_session.commit()
    await db_session.delete(t)
    await db_session.commit()
    rows = (await db_session.execute(select(Document))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_segment_delete_sets_segment_id_null(db_session: AsyncSession) -> None:
    u = User(oidc_subject="d3", email="d3@x.com", display_name="D3")
    db_session.add(u)
    await db_session.flush()
    t = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 2), created_by=u.id)
    db_session.add(t)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    s = Segment(
        trip_id=t.id,
        owner_user_id=u.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(s)
    await db_session.flush()
    d = Document(
        owner_user_id=u.id,
        trip_id=t.id,
        segment_id=s.id,
        filename="x.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="c" * 64,
        storage_key="cc/" + "c" * 64,
    )
    db_session.add(d)
    await db_session.commit()
    await db_session.delete(s)
    await db_session.commit()
    await db_session.refresh(d)
    assert d.segment_id is None
    assert d.trip_id == t.id
