"""GET /map: lifetime atlas — auth + all-trips JSON marshaling."""

from __future__ import annotations

from contextlib import asynccontextmanager
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


def _cookie(user, settings):
    r = _Response()
    set_session_cookie(r, user_id=user.id, settings=settings)
    return {"tt_session": r.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


@asynccontextmanager
async def _ctx(app, settings, user):
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c,
    ):
        yield c


@pytest.fixture
def authenticated_client_factory(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)

    def _make(user):
        settings = Settings()
        app = create_app(settings=settings)
        return _ctx(app, settings, user)

    return _make


async def _seed(db: AsyncSession) -> User:
    u = User(id=OWNER_USER_ID, email="m1@x.com", display_name="M1")
    db.add(u)
    await db.flush()
    t = Trip(
        title="Paris",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 7),
    )
    db.add(t)
    await db.flush()
    db.add(
        Segment(
            trip_id=t.id,
            owner_user_id=u.id,
            type="flight",
            status="confirmed",
            provider="Air France",
            start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
            start_tz="UTC",
            end_at=datetime(2026, 6, 1, 22, tzinfo=UTC),
            end_tz="Europe/Paris",
            start_location={"iata": "JFK", "city": "New York"},
            end_location={"iata": "CDG", "city": "Paris"},
            details={"flight_number": "AF007"},
            parse_source="manual",
            parse_confidence=1.0,
        )
    )
    await db.commit()
    return u


@pytest.mark.asyncio
async def test_anonymous_request_401(db_url, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        r = await c.get("/map", follow_redirects=False)
    assert r.status_code in (401, 302, 303)


@pytest.mark.asyncio
async def test_authed_request_200(db_session: AsyncSession, authenticated_client_factory) -> None:
    u = await _seed(db_session)
    async with authenticated_client_factory(u) as c:
        r = await c.get("/map")
    assert r.status_code == 200
    assert "leaflet" in r.text.lower()
    assert 'id="map-data"' in r.text


@pytest.mark.asyncio
async def test_map_data_includes_jfk_and_cdg_markers(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u = await _seed(db_session)
    async with authenticated_client_factory(u) as c:
        r = await c.get("/map")
    # JSON blob is in <script id="map-data" type="application/json">...</script>
    # JFK is at lat 40.64, CDG at lat 49.01.
    assert "40.6" in r.text  # JFK lat (approx)
    assert "49.0" in r.text  # CDG lat (approx)
