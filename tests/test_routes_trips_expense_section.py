"""Trip-detail expense section + rollups + FxError swallow — Task 8."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import Response as _Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from trip_tracker.app import create_app
from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings
from trip_tracker.expenses.fx import FxError
from trip_tracker.models.expense import Expense
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User


def _normalize(text: str) -> str:
    """Collapse runs of whitespace (incl. newlines) to a single space."""
    return re.sub(r"\s+", " ", text)


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    r = _Response()
    set_session_cookie(r, user_id=user.id, settings=settings)
    return {"tt_session": r.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


@asynccontextmanager
async def _ctx(app, settings, user):  # type: ignore[no-untyped-def]
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
        ) as client,
    ):
        yield client


@pytest.fixture
def authenticated_client_factory(db_url, monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("DATABASE_URL", db_url)

    def _make(user: User):  # type: ignore[no-untyped-def]
        settings = Settings()
        app = create_app(settings=settings)
        return _ctx(app, settings, user)

    return _make


@pytest.fixture(autouse=True)
def _mock_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid real Redis connections in routes/trips._redis."""
    fake = MagicMock()
    fake.get = AsyncMock(return_value=None)
    fake.set = AsyncMock()
    fake.aclose = AsyncMock()
    monkeypatch.setattr(
        "trip_tracker.routes.trips.AsyncRedis",
        MagicMock(from_url=MagicMock(return_value=fake)),
    )


async def _seed(db: AsyncSession, *, home_currency: str = "USD") -> tuple[User, Trip]:
    u = User(
        id=OWNER_USER_ID,
        email="exp_section@x.com",
        display_name="Exp",
        home_currency=home_currency,
    )
    db.add(u)
    await db.flush()
    t = Trip(
        title="Trip",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
    )
    db.add(t)
    await db.commit()
    return u, t


def _exp(
    trip: Trip,
    user: User,
    *,
    amount_minor: int,
    currency: str,
    fx_rate: Decimal,
    amount_home_minor: int,
    home_currency: str = "USD",
    category: str = "food",
    status: str = "paid",
    incurred_on: date | None = None,
    cancellation_deadline: date | None = None,
) -> Expense:
    return Expense(
        trip_id=trip.id,
        owner_user_id=user.id,
        amount_minor=amount_minor,
        currency=currency,
        fx_rate=fx_rate,
        amount_home_minor=amount_home_minor,
        home_currency=home_currency,
        category=category,
        incurred_on=incurred_on or date(2026, 6, 2),
        status=status,
        cancellation_deadline=cancellation_deadline,
    )


def _award_segment(trip: Trip, user: User) -> Segment:
    return Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 1, 12, 0, tzinfo=UTC),
        start_tz="UTC",
        details={
            "award": {
                "program": "Chase UR",
                "points_spent": 75000,
                "cash_copay_minor": 560,
                "cash_copay_currency": "USD",
                "cash_equivalent_minor": 150000,
                "cash_equivalent_currency": "USD",
            }
        },
        parse_source="manual",
        parse_confidence=1.0,
    )


@pytest.mark.asyncio
async def test_trip_detail_renders_paid_total_in_home_currency(
    db_session, authenticated_client_factory
) -> None:
    u, t = await _seed(db_session)
    db_session.add(
        _exp(
            t,
            u,
            amount_minor=3800,
            currency="EUR",
            fx_rate=Decimal("1.07"),
            amount_home_minor=4066,
        )
    )
    db_session.add(
        _exp(
            t,
            u,
            amount_minor=2000,
            currency="USD",
            fx_rate=Decimal("1.0"),
            amount_home_minor=2000,
        )
    )
    await db_session.commit()

    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/trips/{t.id}")
    assert r.status_code == 200
    assert "USD 60.66" in _normalize(r.text)


@pytest.mark.asyncio
async def test_trip_detail_renders_expected_total_paid_plus_pending(
    db_session, authenticated_client_factory
) -> None:
    u, t = await _seed(db_session)
    db_session.add(
        _exp(
            t,
            u,
            amount_minor=3800,
            currency="EUR",
            fx_rate=Decimal("1.07"),
            amount_home_minor=4066,
            status="paid",
        )
    )
    db_session.add(
        _exp(
            t,
            u,
            amount_minor=8200,
            currency="USD",
            fx_rate=Decimal("1.0"),
            amount_home_minor=8200,
            status="pending",
            category="lodging",
        )
    )
    await db_session.commit()

    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/trips/{t.id}")
    assert r.status_code == 200
    norm = _normalize(r.text)
    assert "Expected" in norm
    assert "USD 122.66" in norm


@pytest.mark.asyncio
async def test_trip_detail_by_category_excludes_pending(
    db_session, authenticated_client_factory
) -> None:
    u, t = await _seed(db_session)
    db_session.add(
        _exp(
            t,
            u,
            amount_minor=3800,
            currency="EUR",
            fx_rate=Decimal("1.07"),
            amount_home_minor=4066,
            status="paid",
            category="food",
        )
    )
    db_session.add(
        _exp(
            t,
            u,
            amount_minor=8200,
            currency="USD",
            fx_rate=Decimal("1.0"),
            amount_home_minor=8200,
            status="pending",
            category="lodging",
        )
    )
    await db_session.commit()

    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/trips/{t.id}")
    assert r.status_code == 200
    # Category rollup line shows Food but not Lodging.
    # (Lodging may appear elsewhere via expense rows, so look for the rollup
    # summary signature: "Food USD ...")
    norm = _normalize(r.text)
    assert "Food USD" in norm
    assert "Lodging USD" not in norm


@pytest.mark.asyncio
async def test_trip_detail_saved_by_points_when_award_set(
    db_session, authenticated_client_factory, monkeypatch
) -> None:
    u, t = await _seed(db_session)
    seg = _award_segment(t, u)
    db_session.add(seg)
    await db_session.commit()
    flag_modified(seg, "details")
    await db_session.commit()

    async def _fake_get_rate(base: str, target: str, redis):  # type: ignore[no-untyped-def]
        return Decimal("1")

    monkeypatch.setattr("trip_tracker.expenses.fx.get_rate", _fake_get_rate)

    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/trips/{t.id}")
    assert r.status_code == 200
    norm = _normalize(r.text)
    assert "Saved by points" in norm
    assert "USD 1494.40" in norm


@pytest.mark.asyncio
async def test_trip_detail_swallows_fxerror_in_saved_by_points(
    db_session, authenticated_client_factory, monkeypatch
) -> None:
    u, t = await _seed(db_session)
    seg = _award_segment(t, u)
    db_session.add(seg)
    await db_session.commit()
    flag_modified(seg, "details")
    await db_session.commit()

    async def _boom(base: str, target: str, redis):  # type: ignore[no-untyped-def]
        raise FxError("frankfurter down")

    monkeypatch.setattr("trip_tracker.expenses.fx.get_rate", _boom)

    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/trips/{t.id}")
    assert r.status_code == 200
    assert "Saved by points" not in r.text


@pytest.mark.asyncio
async def test_trip_detail_cancellation_warning_renders_within_30_days(
    db_session, authenticated_client_factory
) -> None:
    u, t = await _seed(db_session)
    deadline = date.today() + timedelta(days=5)
    db_session.add(
        _exp(
            t,
            u,
            amount_minor=8200,
            currency="USD",
            fx_rate=Decimal("1.0"),
            amount_home_minor=8200,
            status="pending",
            category="lodging",
            cancellation_deadline=deadline,
        )
    )
    await db_session.commit()

    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/trips/{t.id}")
    assert r.status_code == 200
    assert "Deposit forfeit after" in r.text
    assert deadline.isoformat() in r.text


@pytest.mark.asyncio
async def test_trip_detail_no_cancellation_warning_when_far_out(
    db_session, authenticated_client_factory
) -> None:
    u, t = await _seed(db_session)
    deadline = date.today() + timedelta(days=60)
    db_session.add(
        _exp(
            t,
            u,
            amount_minor=8200,
            currency="USD",
            fx_rate=Decimal("1.0"),
            amount_home_minor=8200,
            status="pending",
            category="lodging",
            cancellation_deadline=deadline,
        )
    )
    await db_session.commit()

    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/trips/{t.id}")
    assert r.status_code == 200
    assert "Deposit forfeit after" not in r.text
