"""GET /segments/{id} resolves to the canonical edit URL via 303 redirect.

Used by the inbox duplicates bucket: dedup records store only the segment
id (not the trip id), so links from there go through this redirect to land
on /trips/{trip_id}/segments/{segment_id}/edit.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import httpx
import pytest
from fastapi import Response as _Response
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    r = _Response()
    set_session_cookie(r, user_id=user.id, settings=settings)
    return {"tt_session": r.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


async def _seed_user_trip_segment(db: AsyncSession) -> Segment:
    u = User(id=OWNER_USER_ID, email="u1@x.com", display_name="U1")
    db.add(u)
    await db.flush()
    trip = Trip(
        title="Trip",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 7),
    )
    db.add(trip)
    await db.flush()
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=u.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 4, 16, 55, tzinfo=UTC),
        start_tz="UTC",
        details={},
        parse_source="test",
        parse_confidence=0.9,
    )
    db.add(seg)
    await db.commit()
    return seg


@pytest.mark.asyncio
async def test_view_segment_redirects_to_edit(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    seg = await _seed_user_trip_segment(db_session)
    user = (
        await db_session.execute(
            __import__("sqlalchemy").select(User).where(User.id == seg.owner_user_id)
        )
    ).scalar_one()

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as client,
    ):
        r = await client.get(f"/segments/{seg.id}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/trips/{seg.trip_id}/segments/{seg.id}/edit"


@pytest.mark.asyncio
async def test_view_segment_unknown_id_returns_404(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    seg = await _seed_user_trip_segment(db_session)
    user = (
        await db_session.execute(
            __import__("sqlalchemy").select(User).where(User.id == seg.owner_user_id)
        )
    ).scalar_one()

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as client,
    ):
        r = await client.get(f"/segments/{uuid.uuid4()}", follow_redirects=False)
    assert r.status_code == 404
