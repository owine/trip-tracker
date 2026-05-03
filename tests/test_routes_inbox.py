"""Inbox routes: list (3 buckets) + 5 actions per bucket 1."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.expense import Expense
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
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


_MIME = (
    b"Subject: Test\r\nFrom: x@y.com\r\nTo: oliver@trips.example.com\r\n"
    b"Content-Type: text/plain\r\n\r\nbody\r\n"
)


async def _setup_user_with_raw(
    db_session: AsyncSession, *, parse_status: str
) -> tuple[User, RawEmail]:
    user = User(oidc_subject="i", email="i@x.com", display_name="I")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
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
    db_session.add(raw)
    await db_session.commit()
    return user, raw


@pytest.mark.asyncio
async def test_inbox_list_shows_three_buckets(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, _ = await _setup_user_with_raw(db_session, parse_status="review")
    # Add a second raw email (no_segments) owned by the same user via existing alias.
    raw2 = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="x@y.com",
        subject="Test2",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_MIME,
        headers={},
        parse_status="no_segments",
    )
    db_session.add(raw2)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get("/inbox")
    assert r.status_code == 200
    assert "review" in r.text.lower()
    assert "no segments" in r.text.lower() or "no_segments" in r.text


@pytest.mark.asyncio
async def test_inbox_surfaces_duplicate_rows(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """A RawEmail with parse_status='duplicate' surfaces in the inbox UI."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, raw = await _setup_user_with_raw(db_session, parse_status="duplicate")
    raw.headers = {"X-Tt-Dedup-Against": [str(uuid.uuid4())]}
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get("/inbox")
    assert r.status_code == 200
    assert raw.from_address in r.text
    assert "Duplicates" in r.text


@pytest.mark.asyncio
async def test_inbox_confirm_action(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, raw = await _setup_user_with_raw(db_session, parse_status="review")

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.post(f"/inbox/{raw.id}/confirm", follow_redirects=False)
    assert r.status_code == 303
    await db_session.refresh(raw)
    assert raw.parse_status == "parsed"


@pytest.mark.asyncio
async def test_inbox_discard_action(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Discard marks the email as 'no_segments' AND deletes auto-created segments
    (per spec §6.1)."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, raw = await _setup_user_with_raw(db_session, parse_status="review")

    trip = Trip(
        title="Auto",
        start_date=datetime(2026, 6, 1).date(),
        end_date=datetime(2026, 6, 5).date(),
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
        parse_source="llm:haiku-4-5",
        parse_confidence=0.7,
        raw_email_id=raw.id,
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
        r = await c.post(f"/inbox/{raw.id}/discard", follow_redirects=False)
    assert r.status_code == 303
    await db_session.refresh(raw)
    assert raw.parse_status == "no_segments"

    rows = (
        (await db_session.execute(select(Segment).where(Segment.raw_email_id == raw.id)))
        .scalars()
        .all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_inbox_reparse_action(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, raw = await _setup_user_with_raw(db_session, parse_status="no_segments")

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    with patch("trip_tracker.routes.inbox.enqueue_parse", new=AsyncMock()) as mock_enqueue:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://test", cookies=_cookie(user, settings)
            ) as c,
        ):
            r = await c.post(f"/inbox/{raw.id}/reparse", follow_redirects=False)
    assert r.status_code == 303
    await db_session.refresh(raw)
    assert raw.parse_status == "pending"
    mock_enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbox_404_for_other_users_raw(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """A user can't act on a RawEmail they don't own (via alias)."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    _user_a, raw = await _setup_user_with_raw(db_session, parse_status="review")
    user_b = User(oidc_subject="b", email="b@x.com", display_name="B")
    db_session.add(user_b)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user_b, settings)
        ) as c,
    ):
        r = await c.post(f"/inbox/{raw.id}/confirm", follow_redirects=False)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# JSON-LD pricing → auto Expense tests
# ---------------------------------------------------------------------------


def _mock_redis_for_inbox(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch AsyncRedis used in the inbox confirm handler.

    freeze_fx calls redis.get/set; we return None (cache miss) so get_rate
    must also be patched to return a fixed rate without hitting Frankfurter.
    """
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=None)
    fake_redis.set = AsyncMock()
    fake_redis.aclose = AsyncMock()
    monkeypatch.setattr(
        "trip_tracker.routes.inbox.AsyncRedis",
        MagicMock(from_url=MagicMock(return_value=fake_redis)),
    )
    return fake_redis


async def _setup_confirmed_segment(
    db_session: AsyncSession,
    user: User,
    raw: RawEmail,
    *,
    details: dict,
    status: str = "confirmed",
    seg_type: str = "flight",
) -> Segment:
    """Create a Trip + Segment linked to `raw`, return the Segment."""
    trip = Trip(
        title="Auto-expense trip",
        start_date=datetime(2026, 6, 1).date(),
        end_date=datetime(2026, 6, 5).date(),
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type=seg_type,
        status=status,
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
        parse_source="json-ld",
        parse_confidence=0.95,
        raw_email_id=raw.id,
        details=details,
    )
    db_session.add(seg)
    await db_session.commit()
    return seg


@pytest.mark.asyncio
async def test_confirm_creates_expense_from_jsonld_price(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
) -> None:
    """Confirming a segment with total_price + price_currency creates an Expense."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user, raw = await _setup_user_with_raw(db_session, parse_status="review")
    # Ensure user has a home_currency set (server_default may not apply in tests)
    user.home_currency = "USD"
    await db_session.flush()

    await _setup_confirmed_segment(
        db_session,
        user,
        raw,
        details={"total_price": 250.00, "price_currency": "USD"},
    )

    _mock_redis_for_inbox(monkeypatch)
    # Patch get_rate so freeze_fx doesn't hit Frankfurter (USD→USD short-circuits,
    # but make it explicit for clarity).
    with patch(
        "trip_tracker.expenses.fx.get_rate",
        new=AsyncMock(return_value=Decimal("1")),
    ):
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
            r = await c.post(f"/inbox/{raw.id}/confirm", follow_redirects=False)

    assert r.status_code == 303

    expenses = (
        (await db_session.execute(select(Expense).where(Expense.owner_user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(expenses) == 1
    exp = expenses[0]
    assert exp.amount_minor == 25000  # 250.00 USD → 25000 cents
    assert exp.currency == "USD"
    assert exp.category == "transit"  # flight → Category.TRANSIT


@pytest.mark.asyncio
async def test_confirm_no_expense_when_price_missing(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
) -> None:
    """Confirming a segment without pricing data must NOT create an Expense."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user, raw = await _setup_user_with_raw(db_session, parse_status="review")

    await _setup_confirmed_segment(
        db_session,
        user,
        raw,
        details={"flight_number": "AB123"},  # no total_price / price_currency
    )

    _mock_redis_for_inbox(monkeypatch)
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
        r = await c.post(f"/inbox/{raw.id}/confirm", follow_redirects=False)

    assert r.status_code == 303

    expenses = (
        (await db_session.execute(select(Expense).where(Expense.owner_user_id == user.id)))
        .scalars()
        .all()
    )
    assert expenses == []


@pytest.mark.asyncio
async def test_confirm_no_expense_for_cancelled_segment(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
) -> None:
    """Confirming an email whose segment is cancelled must NOT create an Expense."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user, raw = await _setup_user_with_raw(db_session, parse_status="review")

    await _setup_confirmed_segment(
        db_session,
        user,
        raw,
        details={"total_price": 150.00, "price_currency": "EUR"},
        status="cancelled",
    )

    _mock_redis_for_inbox(monkeypatch)
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
        r = await c.post(f"/inbox/{raw.id}/confirm", follow_redirects=False)

    assert r.status_code == 303

    expenses = (
        (await db_session.execute(select(Expense).where(Expense.owner_user_id == user.id)))
        .scalars()
        .all()
    )
    assert expenses == []


@pytest.mark.asyncio
async def test_confirm_skips_expense_when_fx_fails(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
) -> None:
    """If freeze_fx raises FxError (e.g. Frankfurter outage), confirm still
    returns 303 and no Expense is created — the user can manually add it later.

    Critical: a Frankfurter outage must NOT block /inbox approvals.
    """
    from trip_tracker.expenses.fx import FxError

    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user, raw = await _setup_user_with_raw(db_session, parse_status="review")
    user.home_currency = "USD"
    await db_session.flush()

    # EUR (non-USD) so freeze_fx actually calls get_rate instead of short-circuiting.
    await _setup_confirmed_segment(
        db_session,
        user,
        raw,
        details={"total_price": 150.00, "price_currency": "EUR"},
    )

    _mock_redis_for_inbox(monkeypatch)
    # Patch the get_rate binding in freeze.py (where it's imported and used),
    # not in fx.py (where it's defined). The agent's first test patches the
    # latter and gets away with it because USD→USD short-circuits before
    # get_rate is called; non-USD tests must patch at the use site.
    with patch(
        "trip_tracker.expenses.freeze.get_rate",
        new=AsyncMock(side_effect=FxError("frankfurter timeout")),
    ):
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
            r = await c.post(f"/inbox/{raw.id}/confirm", follow_redirects=False)

    # Confirm still succeeds; expense silently skipped.
    assert r.status_code == 303
    expenses = (
        (await db_session.execute(select(Expense).where(Expense.owner_user_id == user.id)))
        .scalars()
        .all()
    )
    assert expenses == []


@pytest.mark.asyncio
async def test_confirm_uses_booking_time_for_incurred_on(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
) -> None:
    """Auto-imported expenses record `incurred_on` as the booking date (when
    the user actually paid), not the travel date. The JSON-LD parser sets
    `details.booking_time` from schema.org `bookingTime`."""
    from datetime import date as _date

    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    user, raw = await _setup_user_with_raw(db_session, parse_status="review")
    user.home_currency = "USD"
    await db_session.flush()

    # Travel date: June 1 (set by _setup_confirmed_segment); booking date: April 15.
    # incurred_on must be April 15 — what the user actually paid on.
    await _setup_confirmed_segment(
        db_session,
        user,
        raw,
        details={
            "total_price": 250.00,
            "price_currency": "USD",
            "booking_time": "2026-04-15T10:23:00+00:00",
        },
    )

    _mock_redis_for_inbox(monkeypatch)
    with patch(
        "trip_tracker.expenses.fx.get_rate",
        new=AsyncMock(return_value=Decimal("1")),
    ):
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
            r = await c.post(f"/inbox/{raw.id}/confirm", follow_redirects=False)

    assert r.status_code == 303
    expense = (
        await db_session.execute(select(Expense).where(Expense.owner_user_id == user.id))
    ).scalar_one()
    assert expense.incurred_on == _date(2026, 4, 15)
