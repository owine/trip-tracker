"""Expense CRUD routes — Task 6."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.expenses.fx import FxError
from trip_tracker.models.expense import Expense
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
    """Avoid real Redis connections in routes/expenses._redis."""
    fake = MagicMock()
    fake.get = AsyncMock(return_value=None)
    fake.set = AsyncMock()
    fake.aclose = AsyncMock()
    monkeypatch.setattr(
        "trip_tracker.routes.expenses.AsyncRedis",
        MagicMock(from_url=MagicMock(return_value=fake)),
    )


async def _seed(db: AsyncSession, *, home_currency: str = "USD") -> tuple[User, Trip]:
    u = User(
        oidc_subject="ex1",
        email="ex1@x.com",
        display_name="EX1",
        home_currency=home_currency,
    )
    db.add(u)
    await db.flush()
    t = Trip(
        title="Trip",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        created_by=u.id,
    )
    db.add(t)
    await db.flush()
    db.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    await db.commit()
    return u, t


async def _seed_expense(
    db: AsyncSession,
    user: User,
    trip: Trip,
    *,
    amount_minor: int = 3800,
    currency: str = "EUR",
    fx_rate: Decimal = Decimal("1.10"),
    amount_home_minor: int = 4180,
    home_currency: str = "USD",
) -> Expense:
    exp = Expense(
        trip_id=trip.id,
        owner_user_id=user.id,
        amount_minor=amount_minor,
        currency=currency,
        fx_rate=fx_rate,
        amount_home_minor=amount_home_minor,
        home_currency=home_currency,
        category="food",
        incurred_on=date(2026, 6, 2),
        status="paid",
    )
    db.add(exp)
    await db.commit()
    return exp


def _form(**overrides: object) -> dict[str, str]:
    base: dict[str, str] = {
        "amount_minor": "3800",
        "currency": "EUR",
        "category": "food",
        "incurred_on": "2026-06-02",
        "status": "paid",
        "home_currency_at_load": "USD",
    }
    base.update({k: str(v) for k, v in overrides.items()})
    return base


@pytest.mark.asyncio
async def test_create_expense_freezes_fx(
    db_session, authenticated_client_factory, monkeypatch
) -> None:
    u, t = await _seed(db_session)
    fake_freeze = AsyncMock(return_value=(Decimal("1.0900"), 4142))
    monkeypatch.setattr("trip_tracker.routes.expenses.freeze_fx", fake_freeze)

    async with authenticated_client_factory(u) as client:
        r = await client.post(f"/trips/{t.id}/expenses", data=_form())
    assert r.status_code in (200, 303)

    rows = (
        (await db_session.execute(select(Expense).where(Expense.trip_id == t.id))).scalars().all()
    )
    assert len(rows) == 1
    e = rows[0]
    assert e.amount_minor == 3800
    assert e.currency == "EUR"
    assert e.fx_rate == Decimal("1.0900")
    assert e.amount_home_minor == 4142
    assert e.home_currency == "USD"
    fake_freeze.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_expense_unauthorized_returns_404(
    db_session, authenticated_client_factory, monkeypatch
) -> None:
    _u, t = await _seed(db_session)
    other = User(oidc_subject="other", email="other@x.com", display_name="O", home_currency="USD")
    db_session.add(other)
    await db_session.commit()
    monkeypatch.setattr(
        "trip_tracker.routes.expenses.freeze_fx",
        AsyncMock(return_value=(Decimal("1"), 0)),
    )

    async with authenticated_client_factory(other) as client:
        r = await client.post(f"/trips/{t.id}/expenses", data=_form())
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_edit_amount_only_keeps_fx_rate(
    db_session, authenticated_client_factory, monkeypatch
) -> None:
    u, t = await _seed(db_session)
    exp = await _seed_expense(db_session, u, t)
    original_fx = exp.fx_rate

    fake_freeze = AsyncMock()
    fake_recompute = MagicMock(return_value=5500)
    monkeypatch.setattr("trip_tracker.routes.expenses.freeze_fx", fake_freeze)
    monkeypatch.setattr("trip_tracker.routes.expenses.recompute_home_minor", fake_recompute)

    async with authenticated_client_factory(u) as client:
        r = await client.post(
            f"/expenses/{exp.id}",
            data=_form(amount_minor="5000", currency="EUR"),
        )
    assert r.status_code in (200, 303)

    fake_freeze.assert_not_awaited()
    fake_recompute.assert_called_once()

    await db_session.refresh(exp)
    assert exp.fx_rate == original_fx
    assert exp.amount_minor == 5000
    assert exp.amount_home_minor == 5500


@pytest.mark.asyncio
async def test_edit_currency_changed_refetches_fx(
    db_session, authenticated_client_factory, monkeypatch
) -> None:
    u, t = await _seed(db_session)
    exp = await _seed_expense(db_session, u, t)

    fake_freeze = AsyncMock(return_value=(Decimal("0.7500"), 2850))
    monkeypatch.setattr("trip_tracker.routes.expenses.freeze_fx", fake_freeze)

    async with authenticated_client_factory(u) as client:
        r = await client.post(
            f"/expenses/{exp.id}",
            data=_form(amount_minor="3800", currency="GBP"),
        )
    assert r.status_code in (200, 303)
    fake_freeze.assert_awaited_once()

    await db_session.refresh(exp)
    assert exp.currency == "GBP"
    assert exp.fx_rate == Decimal("0.7500")
    assert exp.amount_home_minor == 2850


@pytest.mark.asyncio
async def test_edit_no_changes_no_recompute(
    db_session, authenticated_client_factory, monkeypatch
) -> None:
    u, t = await _seed(db_session)
    exp = await _seed_expense(db_session, u, t)
    orig_fx = exp.fx_rate
    orig_home_minor = exp.amount_home_minor

    fake_freeze = AsyncMock()
    fake_recompute = MagicMock()
    monkeypatch.setattr("trip_tracker.routes.expenses.freeze_fx", fake_freeze)
    monkeypatch.setattr("trip_tracker.routes.expenses.recompute_home_minor", fake_recompute)

    async with authenticated_client_factory(u) as client:
        r = await client.post(
            f"/expenses/{exp.id}",
            data=_form(amount_minor="3800", currency="EUR"),
        )
    assert r.status_code in (200, 303)
    fake_freeze.assert_not_awaited()
    fake_recompute.assert_not_called()

    await db_session.refresh(exp)
    assert exp.fx_rate == orig_fx
    assert exp.amount_home_minor == orig_home_minor


@pytest.mark.asyncio
async def test_create_with_fx_unavailable_renders_503_message(
    db_session, authenticated_client_factory, monkeypatch
) -> None:
    u, t = await _seed(db_session)
    monkeypatch.setattr(
        "trip_tracker.routes.expenses.freeze_fx",
        AsyncMock(side_effect=FxError("boom")),
    )

    async with authenticated_client_factory(u) as client:
        r = await client.post(f"/trips/{t.id}/expenses", data=_form())
    assert r.status_code == 200
    assert "Currency rates unavailable" in r.text

    rows = (
        (await db_session.execute(select(Expense).where(Expense.trip_id == t.id))).scalars().all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_create_with_home_currency_mismatch_re_renders(
    db_session, authenticated_client_factory, monkeypatch
) -> None:
    u, t = await _seed(db_session, home_currency="USD")
    fake_freeze = AsyncMock(return_value=(Decimal("1.0900"), 4142))
    monkeypatch.setattr("trip_tracker.routes.expenses.freeze_fx", fake_freeze)

    async with authenticated_client_factory(u) as client:
        r = await client.post(f"/trips/{t.id}/expenses", data=_form(home_currency_at_load="EUR"))
    assert r.status_code == 200
    assert "home currency changed" in r.text

    fake_freeze.assert_not_awaited()
    rows = (
        (await db_session.execute(select(Expense).where(Expense.trip_id == t.id))).scalars().all()
    )
    assert rows == []


@pytest.mark.asyncio
async def test_delete_expense_removes_row(db_session, authenticated_client_factory) -> None:
    u, t = await _seed(db_session)
    exp = await _seed_expense(db_session, u, t)

    async with authenticated_client_factory(u) as client:
        r = await client.post(f"/expenses/{exp.id}/delete")
    assert r.status_code in (200, 303)

    rows = (await db_session.execute(select(Expense).where(Expense.id == exp.id))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_cancellation_triple_persisted(
    db_session, authenticated_client_factory, monkeypatch
) -> None:
    u, t = await _seed(db_session)
    monkeypatch.setattr(
        "trip_tracker.routes.expenses.freeze_fx",
        AsyncMock(return_value=(Decimal("1.0000"), 3800)),
    )

    data = _form(
        amount_minor="3800",
        currency="USD",
        deposit_minor="500",
        cancellation_deadline="2026-05-15",
        cancellation_fee_minor="200",
    )
    async with authenticated_client_factory(u) as client:
        r = await client.post(f"/trips/{t.id}/expenses", data=data)
    assert r.status_code in (200, 303)

    e = (await db_session.execute(select(Expense).where(Expense.trip_id == t.id))).scalar_one()
    assert e.deposit_minor == 500
    assert e.cancellation_deadline == date(2026, 5, 15)
    assert e.cancellation_fee_minor == 200
