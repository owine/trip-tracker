"""C3 tests: POST /trips/{id}/dismiss-merge/{other_id}."""

from __future__ import annotations

import uuid
from datetime import date

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_merge_dismissal import TripMergeDismissal
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.trips.consolidation import ConsolidationTarget, consolidation_candidates


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


async def _seed_two_trips(
    db: AsyncSession,
    *,
    sub_a: str = "c3-user-a",
    sub_b: str | None = None,
) -> tuple[User, User, Trip, Trip]:
    """Create two trips. If sub_b is None both trips belong to user_a.

    Returns (user_a, user_b, trip_a, trip_b). user_b == user_a when same_user.
    """
    user_a = User(
        oidc_subject=sub_a,
        email=f"{sub_a}@x.com",
        display_name="C3 UserA",
    )
    db.add(user_a)
    await db.flush()

    if sub_b is None:
        user_b = user_a
    else:
        user_b = User(
            oidc_subject=sub_b,
            email=f"{sub_b}@x.com",
            display_name="C3 UserB",
        )
        db.add(user_b)
        await db.flush()

    trip_a = Trip(
        title="C3 Trip A",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 10),
        created_by=user_a.id,
    )
    db.add(trip_a)
    await db.flush()
    db.add(TripTraveler(trip_id=trip_a.id, user_id=user_a.id, role="owner"))

    trip_b = Trip(
        title="C3 Trip B",
        start_date=date(2026, 6, 5),
        end_date=date(2026, 6, 15),
        created_by=user_b.id,
    )
    db.add(trip_b)
    await db.flush()
    db.add(TripTraveler(trip_id=trip_b.id, user_id=user_b.id, role="owner"))

    await db.commit()
    return user_a, user_b, trip_a, trip_b


# ---------------------------------------------------------------------------
# C3 tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dismiss_inserts_row_returns_303(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Happy path: POST inserts a canonical (LEAST, GREATEST) row and redirects 303."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, trip_a, trip_b = await _seed_two_trips(db_session, sub_a="c3-happy")

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user_a, settings),
            follow_redirects=False,
        ) as c,
    ):
        r = await c.post(f"/trips/{trip_a.id}/dismiss-merge/{trip_b.id}")

    assert r.status_code == 303
    assert r.headers["location"] == f"/trips/{trip_a.id}"

    # Verify the row exists in canonical (LEAST, GREATEST) order.
    a_id, b_id = sorted([trip_a.id, trip_b.id], key=str)
    row = (
        await db_session.execute(
            select(TripMergeDismissal).where(
                TripMergeDismissal.user_id == user_a.id,
                TripMergeDismissal.trip_a_id == a_id,
                TripMergeDismissal.trip_b_id == b_id,
            )
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.trip_a_id == a_id
    assert row.trip_b_id == b_id


@pytest.mark.asyncio
async def test_dismiss_idempotent_same_order(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POSTing the same pair (a, b) twice returns 303 both times; only ONE row exists."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, trip_a, trip_b = await _seed_two_trips(db_session, sub_a="c3-idem-same")

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user_a, settings),
            follow_redirects=False,
        ) as c,
    ):
        r1 = await c.post(f"/trips/{trip_a.id}/dismiss-merge/{trip_b.id}")
        r2 = await c.post(f"/trips/{trip_a.id}/dismiss-merge/{trip_b.id}")

    assert r1.status_code == 303
    assert r2.status_code == 303

    # Exactly one row in the table for this user+pair.
    a_id, b_id = sorted([trip_a.id, trip_b.id], key=str)
    rows = (
        (
            await db_session.execute(
                select(TripMergeDismissal).where(
                    TripMergeDismissal.user_id == user_a.id,
                    TripMergeDismissal.trip_a_id == a_id,
                    TripMergeDismissal.trip_b_id == b_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_dismiss_idempotent_reverse_order(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POSTing (a, b) then (b, a) returns 303 both times; only ONE row exists.

    This verifies that route-level canonicalization + the DB-side expression
    UNIQUE INDEX both agree: reverse-order pairs map to the same dismissal.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, trip_a, trip_b = await _seed_two_trips(db_session, sub_a="c3-idem-rev")

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user_a, settings),
            follow_redirects=False,
        ) as c,
    ):
        r1 = await c.post(f"/trips/{trip_a.id}/dismiss-merge/{trip_b.id}")
        r2 = await c.post(f"/trips/{trip_b.id}/dismiss-merge/{trip_a.id}")

    assert r1.status_code == 303
    assert r2.status_code == 303

    # Only one row regardless of insertion order.
    a_id, b_id = sorted([trip_a.id, trip_b.id], key=str)
    rows = (
        (
            await db_session.execute(
                select(TripMergeDismissal).where(
                    TripMergeDismissal.user_id == user_a.id,
                    TripMergeDismissal.trip_a_id == a_id,
                    TripMergeDismissal.trip_b_id == b_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_dismiss_404_on_nonexistent_trip(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Using a non-existent other_id UUID must return 404."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, trip_a, _ = await _seed_two_trips(db_session, sub_a="c3-404")
    ghost_id = uuid.uuid4()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user_a, settings),
            follow_redirects=False,
        ) as c,
    ):
        r = await c.post(f"/trips/{trip_a.id}/dismiss-merge/{ghost_id}")

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_dismiss_403_on_non_owner_trip(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """User A cannot dismiss a pair when they don't own trip B (403).

    Also verifies symmetry: user B cannot dismiss from user A's perspective.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, user_b, trip_a, trip_b = await _seed_two_trips(
        db_session, sub_a="c3-403-a", sub_b="c3-403-b"
    )

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as c,
    ):
        # user_a owns trip_a but NOT trip_b → 403
        c.cookies = _cookie(user_a, settings)  # type: ignore[assignment]
        r_a = await c.post(f"/trips/{trip_a.id}/dismiss-merge/{trip_b.id}")

        # user_b owns trip_b but NOT trip_a → 403 (symmetric)
        c.cookies = _cookie(user_b, settings)  # type: ignore[assignment]
        r_b = await c.post(f"/trips/{trip_b.id}/dismiss-merge/{trip_a.id}")

    assert r_a.status_code == 403
    assert r_b.status_code == 403


@pytest.mark.asyncio
async def test_dismiss_400_on_self_pair(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POST /trips/{a}/dismiss-merge/{a} must return 400 (self-pair is meaningless)."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, trip_a, _ = await _seed_two_trips(db_session, sub_a="c3-400-self")

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user_a, settings),
            follow_redirects=False,
        ) as c,
    ):
        r = await c.post(f"/trips/{trip_a.id}/dismiss-merge/{trip_a.id}")

    assert r.status_code == 400


@pytest.mark.asyncio
async def test_consolidation_candidates_excludes_dismissed_pair(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """After dismissing a pair, consolidation_candidates no longer returns it.

    Seeds two overlapping trips (WOULD be candidates), dismisses them via HTTP,
    then calls consolidation_candidates directly to verify the dismissed candidate
    is absent from results.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, trip_a, trip_b = await _seed_two_trips(db_session, sub_a="c3-integ-excl")

    # Dismiss via HTTP so we go through the real route + DB insert.
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user_a, settings),
            follow_redirects=False,
        ) as c,
    ):
        r = await c.post(f"/trips/{trip_a.id}/dismiss-merge/{trip_b.id}")
    assert r.status_code == 303

    # Now call consolidation_candidates directly.
    target_a = ConsolidationTarget.from_trip(trip_a, [])
    candidates = await consolidation_candidates(db_session, user_a, target_a)

    # trip_b must not appear in the returned candidates.
    candidate_ids = {c.trip_id for c in candidates}
    assert trip_b.id not in candidate_ids
