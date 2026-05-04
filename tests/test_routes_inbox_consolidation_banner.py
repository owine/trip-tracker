"""C5 tests: inbox review-row consolidation banner."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
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


_MIME = (
    b"Subject: Test\r\nFrom: x@y.com\r\nTo: oliver@trips.example.com\r\n"
    b"Content-Type: text/plain\r\n\r\nbody\r\n"
)


async def _make_user(db: AsyncSession, *, sub: str, alias: str = "oliver") -> User:
    user = User(oidc_subject=sub, email=f"{sub}@x.com", display_name=sub)
    db.add(user)
    await db.flush()
    db.add(ForwardingAlias(local_part=alias, user_id=user.id))
    await db.flush()
    return user


async def _make_raw(db: AsyncSession, *, subject: str = "Test flight") -> RawEmail:
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="x@y.com",
        subject=subject,
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_MIME,
        headers={},
        parse_status="review",
    )
    db.add(raw)
    await db.flush()
    return raw


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


def _paris_segment(
    trip: Trip,
    user: User,
    *,
    raw: RawEmail | None = None,
    offset_days: int = 0,
) -> Segment:
    base = datetime(2026, 6, 5, tzinfo=UTC) + timedelta(days=offset_days)
    return Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        raw_email_id=raw.id if raw else None,
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
async def test_inbox_renders_consolidation_buttons_when_candidates_exist(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Trip A (existing) + raw_email auto-trip B both end in Paris → MEDIUM.

    GET /inbox must show a form button that POSTs
    /inbox/{raw_id}/confirm?target_trip={A.id} inside the review row.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = await _make_user(db_session, sub="c5-inbox-banner")
    # Trip A: pre-existing trip the user has (with a Paris segment).
    trip_a = await _make_trip(
        db_session,
        user=user,
        title="C5 Existing Trip A",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 10),
    )
    db_session.add(_paris_segment(trip_a, user, offset_days=0))

    # Auto-trip B: created by parse worker for raw_email.
    raw = await _make_raw(db_session, subject="Paris flight")
    trip_b = await _make_trip(
        db_session,
        user=user,
        title="C5 Auto Trip B",
        start_date=date(2026, 6, 5),
        end_date=date(2026, 6, 15),
    )
    db_session.add(_paris_segment(trip_b, user, raw=raw, offset_days=2))
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
        r = await c.get("/inbox")

    assert r.status_code == 200
    body = r.text
    # The banner must show a form that POSTs to confirm with target_trip=A.id
    expected_action = f"/inbox/{raw.id}/confirm?target_trip={trip_a.id}"
    assert expected_action in body, (
        f"Expected consolidation button action '{expected_action}' in inbox body"
    )
    # Trip A's title must appear as the button label
    assert "C5 Existing Trip A" in body


@pytest.mark.asyncio
async def test_inbox_no_banner_when_no_candidates(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Single raw_email with auto-trip, no other trips → no consolidation buttons."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = await _make_user(db_session, sub="c5-inbox-no-cands")
    raw = await _make_raw(db_session, subject="Solo email")
    auto_trip = await _make_trip(
        db_session,
        user=user,
        title="Solo Auto Trip",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 10),
    )
    db_session.add(_paris_segment(auto_trip, user, raw=raw, offset_days=0))
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
        r = await c.get("/inbox")

    assert r.status_code == 200
    # No confirm?target_trip button for this raw email.
    assert f"/inbox/{raw.id}/confirm?target_trip=" not in r.text


@pytest.mark.asyncio
async def test_inbox_consolidation_banner_excludes_dismissed_pair(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Dismissed pair must NOT appear as consolidation button in inbox row."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = await _make_user(db_session, sub="c5-inbox-dismissed")
    trip_a = await _make_trip(
        db_session,
        user=user,
        title="C5 Dismissed Existing",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 10),
    )
    db_session.add(_paris_segment(trip_a, user, offset_days=0))

    raw = await _make_raw(db_session, subject="Dismissed paris flight")
    trip_b = await _make_trip(
        db_session,
        user=user,
        title="C5 Dismissed Auto",
        start_date=date(2026, 6, 5),
        end_date=date(2026, 6, 15),
    )
    db_session.add(_paris_segment(trip_b, user, raw=raw, offset_days=2))

    # Insert dismissal for the pair (A ↔ B).
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
        r = await c.get("/inbox")

    assert r.status_code == 200
    # Trip A is dismissed → must NOT appear as a consolidation target button.
    assert f"/inbox/{raw.id}/confirm?target_trip={trip_a.id}" not in r.text
