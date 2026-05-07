"""Trips routes: list and detail."""

from __future__ import annotations

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


async def _user(db: AsyncSession, *, email: str = "u@example.com") -> User:
    u = User(id=OWNER_USER_ID, email=email, display_name="U")
    db.add(u)
    await db.commit()
    return u


async def _trip(db: AsyncSession, **overrides: object) -> Trip:
    fields: dict[str, object] = {
        "title": "Default",
        "start_date": date(2026, 6, 1),
        "end_date": date(2026, 6, 5),
    }
    fields.update(overrides)
    t = Trip(**fields)
    db.add(t)
    await db.commit()
    return t


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    r = _Response()
    set_session_cookie(r, user_id=user.id, settings=settings)
    return {"tt_session": r.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


@pytest.mark.asyncio
async def test_list_shows_trips(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    me = await _user(db_session, email="me@x.com")
    await _trip(db_session, title="My Trip")

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(me, settings)
        ) as c,
    ):
        r = await c.get("/trips")
    assert r.status_code == 200
    assert "My Trip" in r.text


@pytest.mark.asyncio
async def test_detail_renders_segment_time_in_local_tz(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """The segment row on the detail page shows start_at converted to start_tz,
    not raw UTC labeled with the local tz."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    me = await _user(db_session)
    trip = await _trip(db_session)
    db_session.add(
        Segment(
            trip_id=trip.id,
            owner_user_id=me.id,
            type="flight",
            status="confirmed",
            start_at=datetime(2026, 6, 1, 13, 0, tzinfo=UTC),  # 09:00 EDT
            start_tz="America/New_York",
            start_location={"iata": "JFK", "city": "New York"},
            end_location={"iata": "CDG", "city": "Paris"},
            parse_source="manual",
            parse_confidence=1.0,
        )
    )
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(me, settings)
        ) as c,
    ):
        r = await c.get(f"/trips/{trip.id}")
    assert r.status_code == 200
    assert "2026-06-01 09:00 America/New_York" in r.text
    assert "13:00 America/New_York" not in r.text
