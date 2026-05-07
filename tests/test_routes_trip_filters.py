"""Regression tests: soft-deleted trips (merged_into_id IS NOT NULL) must not
surface in any user-facing listing or by-id lookup.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
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


async def _seed(
    db: AsyncSession,
) -> tuple[User, Trip, Trip]:
    """Create one user with two trips: one active, one soft-deleted."""
    user = User(oidc_subject="filter-test-sub", email="filter@x.com", display_name="Filter User")
    db.add(user)
    await db.flush()

    active = Trip(
        title="Active Trip",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 10),
        created_by=user.id,
    )
    db.add(active)
    await db.flush()
    db.add(TripTraveler(trip_id=active.id, user_id=user.id, role="owner"))

    merged = Trip(
        title="Merged Trip",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 10),
        created_by=user.id,
    )
    db.add(merged)
    await db.flush()
    db.add(TripTraveler(trip_id=merged.id, user_id=user.id, role="owner"))

    # Soft-delete: point merged_into_id to the active trip.
    merged.merged_into_id = active.id
    merged.merged_at = datetime.now(tz=UTC)
    await db.commit()
    return user, active, merged


@pytest.mark.asyncio
async def test_trip_list_excludes_soft_deleted(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """/trips listing must not include soft-deleted trips."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, _active, _merged = await _seed(db_session)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get("/trips")

    assert r.status_code == 200
    assert "Active Trip" in r.text
    assert "Merged Trip" not in r.text


@pytest.mark.asyncio
async def test_segments_new_dropdown_excludes_soft_deleted(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """/segments/new trip dropdown must not include soft-deleted trips."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, _active, _merged = await _seed(db_session)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get("/segments/new?type=flight")

    assert r.status_code == 200
    assert "Active Trip" in r.text
    assert "Merged Trip" not in r.text


@pytest.mark.asyncio
async def test_map_per_trip_soft_deleted_returns_404(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """/trips/<soft-deleted-id>/map must return 404."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, _active, merged = await _seed(db_session)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get(f"/trips/{merged.id}/map")

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_documents_list_soft_deleted_returns_404(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """/trips/<soft-deleted-id>/documents must return 404."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, _active, merged = await _seed(db_session)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get(f"/trips/{merged.id}/documents")

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_lifetime_atlas_excludes_soft_deleted_segments(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """`GET /map` lifetime atlas must not include segments from soft-deleted trips."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, active, merged = await _seed(db_session)

    base = datetime(2026, 7, 1, 9, tzinfo=UTC)
    db_session.add(
        Segment(
            trip_id=active.id,
            owner_user_id=user.id,
            type="flight",
            status="confirmed",
            start_at=base,
            start_tz="UTC",
            start_location={"city": "ActiveStart", "iata": "JFK"},
            end_location={"city": "ActiveEnd", "iata": "LHR"},
            details={},
            parse_source="test",
            parse_confidence=0.9,
        )
    )
    db_session.add(
        Segment(
            trip_id=merged.id,
            owner_user_id=user.id,
            type="flight",
            status="confirmed",
            start_at=base,
            start_tz="UTC",
            start_location={"city": "MergedStart", "iata": "CDG"},
            end_location={"city": "MergedEnd", "iata": "AMS"},
            details={},
            parse_source="test",
            parse_confidence=0.9,
        )
    )
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get("/map")

    assert r.status_code == 200
    # Markers/arcs are JSON-injected; trip IDs appear in the payload.
    assert str(active.id) in r.text
    assert str(merged.id) not in r.text


@pytest.mark.asyncio
async def test_segment_edit_form_soft_deleted_trip_returns_404(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """GET /trips/<soft-deleted-id>/segments/<seg_id>/edit must 404."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, _active, merged = await _seed(db_session)

    seg = Segment(
        trip_id=merged.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 7, 5, 9, tzinfo=UTC),
        start_tz="UTC",
        start_location={"city": "X", "iata": "JFK"},
        end_location={"city": "Y", "iata": "LHR"},
        details={},
        parse_source="test",
        parse_confidence=0.9,
    )
    db_session.add(seg)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get(f"/trips/{merged.id}/segments/{seg.id}/edit")

    assert r.status_code == 404
