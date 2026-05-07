"""Tests for POST /inbox/{raw_id}/confirm?target_trip=<uuid> (C4)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import Response as _Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    r = _Response()
    set_session_cookie(r, user_id=user.id, settings=settings)
    return {"tt_session": r.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


_MIME = (
    b"Subject: Test\r\nFrom: x@y.com\r\nTo: oliver@trips.example.com\r\n"
    b"Content-Type: text/plain\r\n\r\nbody\r\n"
)


async def _make_user(
    db: AsyncSession,
    *,
    email: str = "u1@x.com",
    name: str = "User1",
    alias: str = "oliver",
) -> User:
    """Create the owner user with a forwarding alias."""
    user = User(id=OWNER_USER_ID, email=email, display_name=name, home_currency="USD")
    db.add(user)
    await db.flush()
    db.add(ForwardingAlias(local_part=alias, user_id=user.id))
    await db.flush()
    return user


async def _make_raw(db: AsyncSession, *, parse_status: str = "review") -> RawEmail:
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="x@y.com",
        subject="Test",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_MIME,
        headers={},
        parse_status=parse_status,
    )
    db.add(raw)
    await db.flush()
    return raw


async def _make_trip(
    db: AsyncSession,
    *,
    user: User,
    title: str = "Trip",
    start: datetime,
    end: datetime,
) -> Trip:
    trip = Trip(
        title=title,
        start_date=start.date(),
        end_date=end.date(),
    )
    db.add(trip)
    await db.flush()
    return trip


async def _make_segment(
    db: AsyncSession,
    *,
    user: User,
    trip: Trip,
    raw: RawEmail,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Segment:
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=start or datetime(2026, 6, 1, 10, tzinfo=UTC),
        start_tz="UTC",
        end_at=end,
        parse_source="llm:haiku-4-5",
        parse_confidence=0.85,
        raw_email_id=raw.id,
    )
    db.add(seg)
    await db.flush()
    return seg


def _mock_redis(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch AsyncRedis in inbox routes so confirm doesn't need real Redis."""
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=None)
    fake_redis.set = AsyncMock()
    fake_redis.aclose = AsyncMock()
    monkeypatch.setattr(
        "trip_tracker.routes.inbox.AsyncRedis",
        MagicMock(from_url=MagicMock(return_value=fake_redis)),
    )
    return fake_redis


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_with_target_trip_reassigns_segments(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Happy path: segments move from auto-trip B to target trip A; B is deleted."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = await _make_user(db_session)
    raw = await _make_raw(db_session)

    # Trip A: the user's pre-existing trip (target).
    trip_a = await _make_trip(
        db_session,
        user=user,
        title="Trip A",
        start=datetime(2026, 5, 1),
        end=datetime(2026, 5, 10),
    )
    # Trip B: auto-created by parse worker; currently holds raw's segments.
    trip_b = await _make_trip(
        db_session,
        user=user,
        title="Auto-trip B",
        start=datetime(2026, 6, 1),
        end=datetime(2026, 6, 5),
    )

    seg1 = await _make_segment(db_session, user=user, trip=trip_b, raw=raw)
    seg2 = await _make_segment(
        db_session,
        user=user,
        trip=trip_b,
        raw=raw,
        start=datetime(2026, 6, 3, 14, tzinfo=UTC),
    )
    await db_session.commit()

    _mock_redis(monkeypatch)
    with patch("trip_tracker.routes.inbox.enqueue_meili_sync", new=AsyncMock()):
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
            r = await c.post(
                f"/inbox/{raw.id}/confirm?target_trip={trip_a.id}",
                follow_redirects=False,
            )

    assert r.status_code == 303

    # Segments moved to trip A.
    await db_session.refresh(seg1)
    await db_session.refresh(seg2)
    assert seg1.trip_id == trip_a.id
    assert seg2.trip_id == trip_a.id

    # Auto-trip B is gone (was empty after reassignment).
    deleted_b = (
        await db_session.execute(select(Trip).where(Trip.id == trip_b.id))
    ).scalar_one_or_none()
    assert deleted_b is None, "Auto-trip B should have been deleted (now empty)"

    # Raw email marked parsed.
    await db_session.refresh(raw)
    assert raw.parse_status == "parsed"


@pytest.mark.asyncio
async def test_confirm_with_target_trip_widens_target_dates(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Target trip A's date range is widened to include the moved segments' dates."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = await _make_user(db_session)
    raw = await _make_raw(db_session)

    # Trip A: narrow date range May 1-10.
    trip_a = await _make_trip(
        db_session,
        user=user,
        title="Trip A",
        start=datetime(2026, 5, 1),
        end=datetime(2026, 5, 10),
    )
    # Trip B: segments are in June (outside trip A's range).
    trip_b = await _make_trip(
        db_session,
        user=user,
        title="Auto-trip B",
        start=datetime(2026, 6, 1),
        end=datetime(2026, 6, 5),
    )
    # Segment: June 15, end June 20.
    await _make_segment(
        db_session,
        user=user,
        trip=trip_b,
        raw=raw,
        start=datetime(2026, 6, 15, 8, tzinfo=UTC),
        end=datetime(2026, 6, 20, 18, tzinfo=UTC),
    )
    await db_session.commit()

    _mock_redis(monkeypatch)
    with patch("trip_tracker.routes.inbox.enqueue_meili_sync", new=AsyncMock()):
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
            r = await c.post(
                f"/inbox/{raw.id}/confirm?target_trip={trip_a.id}",
                follow_redirects=False,
            )

    assert r.status_code == 303

    await db_session.refresh(trip_a)
    # start_date should stay May 1 (segment starts later).
    assert trip_a.start_date.month == 5
    assert trip_a.start_date.day == 1
    # end_date should have been extended to June 20 (segment end).
    assert trip_a.end_date.month == 6
    assert trip_a.end_date.day == 20


@pytest.mark.asyncio
async def test_confirm_with_target_trip_404_on_nonexistent(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Passing a random UUID as target_trip that doesn't exist returns 404.

    The raw email and its segments are left untouched (no side effects on error).
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = await _make_user(db_session)
    raw = await _make_raw(db_session, parse_status="review")
    trip_b = await _make_trip(
        db_session,
        user=user,
        title="Auto-trip B",
        start=datetime(2026, 6, 1),
        end=datetime(2026, 6, 5),
    )
    seg = await _make_segment(db_session, user=user, trip=trip_b, raw=raw)
    await db_session.commit()

    nonexistent_id = uuid.uuid4()

    _mock_redis(monkeypatch)
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
        r = await c.post(
            f"/inbox/{raw.id}/confirm?target_trip={nonexistent_id}",
            follow_redirects=False,
        )

    assert r.status_code == 404
    # Segment still in trip B (nothing changed).
    await db_session.refresh(seg)
    assert seg.trip_id == trip_b.id


@pytest.mark.asyncio
async def test_confirm_omitted_target_trip_is_existing_behavior(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POST without ?target_trip: segments stay in their auto-trip; parse_status=parsed."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = await _make_user(db_session)
    raw = await _make_raw(db_session)
    trip_b = await _make_trip(
        db_session,
        user=user,
        title="Auto-trip B",
        start=datetime(2026, 6, 1),
        end=datetime(2026, 6, 5),
    )
    seg = await _make_segment(db_session, user=user, trip=trip_b, raw=raw)
    await db_session.commit()

    _mock_redis(monkeypatch)
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
        # No ?target_trip param.
        r = await c.post(f"/inbox/{raw.id}/confirm", follow_redirects=False)

    assert r.status_code == 303

    await db_session.refresh(raw)
    assert raw.parse_status == "parsed"

    # Segment still belongs to auto-trip B (no reassignment).
    await db_session.refresh(seg)
    assert seg.trip_id == trip_b.id

    # Auto-trip B still exists.
    still_b = (
        await db_session.execute(select(Trip).where(Trip.id == trip_b.id))
    ).scalar_one_or_none()
    assert still_b is not None


@pytest.mark.asyncio
async def test_confirm_with_target_trip_preserves_other_trips_with_segments(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Trip B is NOT deleted if it still has segments from a different raw_email.

    Setup:
    - raw_email_1 has 1 segment in trip B.
    - raw_email_2 also has 1 segment in trip B (different email).
    - Confirm raw_email_1 with target=trip_a.
    - Segment from raw_1 moves to trip_a; trip B still has raw_2's segment → not deleted.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user = await _make_user(db_session)

    raw1 = await _make_raw(db_session)
    raw2 = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="y@z.com",
        subject="Second email",
        message_id=f"<{uuid.uuid4()}@z>",
        mime_blob=_MIME,
        headers={},
        parse_status="review",
    )
    db_session.add(raw2)
    await db_session.flush()

    trip_a = await _make_trip(
        db_session,
        user=user,
        title="Trip A",
        start=datetime(2026, 5, 1),
        end=datetime(2026, 5, 10),
    )
    trip_b = await _make_trip(
        db_session,
        user=user,
        title="Auto-trip B",
        start=datetime(2026, 6, 1),
        end=datetime(2026, 6, 5),
    )

    # raw1 → segment in trip B.
    seg1 = await _make_segment(
        db_session,
        user=user,
        trip=trip_b,
        raw=raw1,
        start=datetime(2026, 6, 1, 10, tzinfo=UTC),
    )
    # raw2 → segment ALSO in trip B.
    seg2 = await _make_segment(
        db_session,
        user=user,
        trip=trip_b,
        raw=raw2,
        start=datetime(2026, 6, 3, 10, tzinfo=UTC),
    )
    await db_session.commit()

    _mock_redis(monkeypatch)
    with patch("trip_tracker.routes.inbox.enqueue_meili_sync", new=AsyncMock()):
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
            # Only confirm raw1 with target_trip=A.
            r = await c.post(
                f"/inbox/{raw1.id}/confirm?target_trip={trip_a.id}",
                follow_redirects=False,
            )

    assert r.status_code == 303

    # seg1 moved to trip_a.
    await db_session.refresh(seg1)
    assert seg1.trip_id == trip_a.id

    # seg2 still in trip_b (from raw2).
    await db_session.refresh(seg2)
    assert seg2.trip_id == trip_b.id

    # Trip B must NOT be deleted — it still owns seg2.
    surviving_b = (
        await db_session.execute(select(Trip).where(Trip.id == trip_b.id))
    ).scalar_one_or_none()
    assert surviving_b is not None, "Trip B must survive because it still has seg2"
