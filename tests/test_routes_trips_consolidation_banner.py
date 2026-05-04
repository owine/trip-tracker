"""C5 tests: trip-detail consolidation banner."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_merge_dismissal import TripMergeDismissal
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


async def _make_user(db: AsyncSession, *, sub: str) -> User:
    user = User(oidc_subject=sub, email=f"{sub}@x.com", display_name=sub)
    db.add(user)
    await db.flush()
    return user


async def _make_trip(
    db: AsyncSession,
    *,
    user: User,
    title: str,
    start_date: date,
    end_date: date,
) -> Trip:
    trip = Trip(
        title=title,
        start_date=start_date,
        end_date=end_date,
        created_by=user.id,
    )
    db.add(trip)
    await db.flush()
    db.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db.flush()
    return trip


def _paris_segment(trip: Trip, user: User, *, offset_days: int = 0) -> Segment:
    base = datetime(2026, 6, 5, tzinfo=UTC) + timedelta(days=offset_days)
    return Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=base,
        start_tz="UTC",
        start_location={"city": "New York", "iata": "JFK"},
        end_location={"city": "Paris", "iata": "CDG"},
        details={},
        parse_source="test",
        parse_confidence=0.9,
    )


@pytest.mark.asyncio
async def test_trip_detail_renders_consolidation_banner_when_candidates_exist(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Trip A with Paris segment; Trip B also with Paris segment → MEDIUM candidate.

    GET /trips/{A.id} must render the consolidation banner with:
    - Trip B's title
    - A form action to /trips/{A}/merge-into/{B}
    - A form action to /trips/{A}/dismiss-merge/{B}
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = await _make_user(db_session, sub="c5-banner-exists")
    trip_a = await _make_trip(
        db_session,
        user=user,
        title="C5 Trip A",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 10),
    )
    trip_b = await _make_trip(
        db_session,
        user=user,
        title="C5 Trip B",
        start_date=date(2026, 6, 5),
        end_date=date(2026, 6, 15),
    )
    db_session.add(_paris_segment(trip_a, user, offset_days=0))
    db_session.add(_paris_segment(trip_b, user, offset_days=2))
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c,
    ):
        r = await c.get(f"/trips/{trip_a.id}")

    assert r.status_code == 200
    body = r.text
    assert 'id="consolidation-banner"' in body
    assert "C5 Trip B" in body
    assert f"/trips/{trip_a.id}/merge-into/{trip_b.id}" in body
    assert f"/trips/{trip_a.id}/dismiss-merge/{trip_b.id}" in body


@pytest.mark.asyncio
async def test_trip_detail_no_banner_when_no_candidates(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Single trip for user → no consolidation banner rendered."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = await _make_user(db_session, sub="c5-banner-absent")
    trip = await _make_trip(
        db_session,
        user=user,
        title="Solo Trip",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 10),
    )
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c,
    ):
        r = await c.get(f"/trips/{trip.id}")

    assert r.status_code == 200
    assert 'id="consolidation-banner"' not in r.text


@pytest.mark.asyncio
async def test_trip_detail_no_banner_when_pair_dismissed(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Two matching trips with dismissed pair → banner absent."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = await _make_user(db_session, sub="c5-banner-dismissed")
    trip_a = await _make_trip(
        db_session,
        user=user,
        title="C5 Dismissed A",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 10),
    )
    trip_b = await _make_trip(
        db_session,
        user=user,
        title="C5 Dismissed B",
        start_date=date(2026, 6, 5),
        end_date=date(2026, 6, 15),
    )
    db_session.add(_paris_segment(trip_a, user, offset_days=0))
    db_session.add(_paris_segment(trip_b, user, offset_days=2))

    # Insert dismissal directly so test doesn't depend on dismiss route.
    a_id, b_id = sorted([trip_a.id, trip_b.id], key=str)
    await db_session.execute(
        pg_insert(TripMergeDismissal)
        .values({"user_id": user.id, "trip_a_id": a_id, "trip_b_id": b_id})
        .on_conflict_do_nothing(index_elements=["user_id", "trip_a_id", "trip_b_id"])
    )
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c,
    ):
        r = await c.get(f"/trips/{trip_a.id}")

    assert r.status_code == 200
    assert 'id="consolidation-banner"' not in r.text
