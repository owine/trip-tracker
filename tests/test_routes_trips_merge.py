"""B7 tests: 410 on soft-deleted trip detail + ICS feed filters merged trips."""

from __future__ import annotations

from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.ics.tokens import generate_token
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
    """Create one user with two trips: one active, one soft-deleted (merged)."""
    user = User(
        oidc_subject="b7-test-sub",
        email="b7@x.com",
        display_name="B7 User",
    )
    db.add(user)
    await db.flush()

    active = Trip(
        title="B7 Active Trip",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 10),
        created_by=user.id,
    )
    db.add(active)
    await db.flush()
    db.add(TripTraveler(trip_id=active.id, user_id=user.id, role="owner"))

    merged = Trip(
        title="B7 Merged Trip",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 10),
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
async def test_trip_detail_soft_deleted_returns_410(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """GET /trips/<soft-deleted-id> must return 410 Gone with body referencing target."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, active, merged = await _seed(db_session)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get(f"/trips/{merged.id}")

    assert r.status_code == 410
    # Body must reference the target trip URL so clients can redirect.
    assert str(active.id) in r.text


@pytest.mark.asyncio
async def test_ics_feed_excludes_soft_deleted_trip_segments(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """ICS feed must include active-trip segments but not merged-trip segments."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, active, merged = await _seed(db_session)

    # Mint an ICS token directly (same approach as settings route).
    plaintext, token_hash = generate_token()
    user.ics_token_hash = token_hash
    await db_session.commit()

    base = datetime(2026, 8, 5, 10, tzinfo=UTC)

    # Segment on the active trip — should appear in feed.
    db_session.add(
        Segment(
            trip_id=active.id,
            owner_user_id=user.id,
            type="flight",
            status="confirmed",
            start_at=base,
            start_tz="UTC",
            start_location={"city": "ActiveCity", "iata": "JFK"},
            end_location={"city": "ActiveDest", "iata": "LHR"},
            details={},
            parse_source="test",
            parse_confidence=0.9,
        )
    )

    # Segment on the merged (soft-deleted) trip — must NOT appear in feed.
    db_session.add(
        Segment(
            trip_id=merged.id,
            owner_user_id=user.id,
            type="flight",
            status="confirmed",
            start_at=base,
            start_tz="UTC",
            start_location={"city": "MergedCity", "iata": "CDG"},
            end_location={"city": "MergedDest", "iata": "AMS"},
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
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        r = await c.get(f"/ics/{plaintext}.ics")

    assert r.status_code == 200
    body = r.text

    # Parse VEVENT blocks to confirm active segment present, merged segment absent.
    # Split on VEVENT boundaries for precise membership check.
    vevent_blocks = [block for block in body.split("BEGIN:VEVENT") if "END:VEVENT" in block]
    # At least one VEVENT must exist (active segment).
    assert len(vevent_blocks) >= 1, "Expected at least one VEVENT in ICS feed"

    # Active segment's cities must appear somewhere in the feed.
    assert "ActiveCity" in body or "JFK" in body

    # Merged segment's cities must NOT appear in any VEVENT block.
    for block in vevent_blocks:
        assert "MergedCity" not in block, "Soft-deleted trip segment leaked into ICS VEVENT"
        assert "CDG" not in block, "Soft-deleted trip segment leaked into ICS VEVENT"
