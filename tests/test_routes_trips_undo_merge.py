"""C2 tests: POST /trips/{target}/undo-merge/{source} (undo-merge route)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.ics.tokens import generate_token
from trip_tracker.models.document import Document
from trip_tracker.models.expense import Expense
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


async def _seed_merge_scenario(
    db: AsyncSession,
    *,
    sub_prefix: str = "c2",
    source_start: date = date(2026, 6, 1),
    source_end: date = date(2026, 6, 5),
    target_start: date = date(2026, 7, 10),
    target_end: date = date(2026, 7, 15),
    extra_source_users: list[tuple[str, str]] | None = None,
) -> tuple[User, Trip, Trip]:
    """Create a user + source (Jun 1-5) + target (Jul 10-15), NOT yet merged.

    extra_source_users: list of (oidc_subject, role) to add to source only.
    Returns (user_a, source, target).
    """
    user_a = User(
        oidc_subject=f"{sub_prefix}-owner",
        email=f"{sub_prefix}@x.com",
        display_name="C2 Owner",
    )
    db.add(user_a)
    await db.flush()

    source = Trip(
        title="C2 Source",
        start_date=source_start,
        end_date=source_end,
        created_by=user_a.id,
    )
    target = Trip(
        title="C2 Target",
        start_date=target_start,
        end_date=target_end,
        created_by=user_a.id,
    )
    db.add(source)
    db.add(target)
    await db.flush()

    db.add(TripTraveler(trip_id=source.id, user_id=user_a.id, role="owner"))
    db.add(TripTraveler(trip_id=target.id, user_id=user_a.id, role="owner"))

    extra_users: list[User] = []
    for sub, role in extra_source_users or []:
        extra_user = User(
            oidc_subject=sub,
            email=f"{sub}@x.com",
            display_name=f"Extra {sub}",
        )
        db.add(extra_user)
        await db.flush()
        db.add(TripTraveler(trip_id=source.id, user_id=extra_user.id, role=role))
        extra_users.append(extra_user)

    await db.commit()
    return user_a, source, target


# ---------------------------------------------------------------------------
# Test 1: Happy path - restores everything
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_within_window_restores_segments_expenses_documents_and_dates(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Undo within 7 days restores all FKs, dates, travelers, and lifts soft-delete."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, source, target = await _seed_merge_scenario(db_session, sub_prefix="c2-happy")

    base_dt = datetime(2026, 6, 3, 10, tzinfo=UTC)
    seg = Segment(
        trip_id=source.id,
        owner_user_id=user_a.id,
        type="flight",
        status="confirmed",
        start_at=base_dt,
        start_tz="UTC",
        start_location={"city": "SrcCity", "iata": "JFK"},
        end_location={"city": "TgtCity", "iata": "LHR"},
        details={},
        parse_source="test",
        parse_confidence=0.9,
    )
    exp = Expense(
        trip_id=source.id,
        owner_user_id=user_a.id,
        amount_minor=500,
        currency="USD",
        fx_rate=Decimal("1.0"),
        amount_home_minor=500,
        home_currency="USD",
        category="transport",
        incurred_on=date(2026, 6, 3),
        status="paid",
    )
    doc = Document(
        owner_user_id=user_a.id,
        trip_id=source.id,
        filename="itinerary.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        sha256="b" * 64,
        storage_key="bb/" + "b" * 64,
    )
    db_session.add(seg)
    db_session.add(exp)
    db_session.add(doc)
    await db_session.commit()

    source_id = source.id
    target_id = target.id

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
        # First merge
        r_merge = await c.post(f"/trips/{source_id}/merge-into/{target_id}")
        assert r_merge.status_code == 303

        # Then undo
        r_undo = await c.post(f"/trips/{target_id}/undo-merge/{source_id}")

    assert r_undo.status_code == 303
    assert r_undo.headers["location"] == f"/trips/{source_id}"

    # Refetch DB state
    await db_session.refresh(seg)
    await db_session.refresh(exp)
    await db_session.refresh(doc)
    await db_session.refresh(source)
    await db_session.refresh(target)

    # FKs restored to source
    assert seg.trip_id == source_id
    assert exp.trip_id == source_id
    assert doc.trip_id == source_id

    # Target dates restored to pre-merge values
    assert target.start_date == date(2026, 7, 10)
    assert target.end_date == date(2026, 7, 15)

    # Source dates unchanged
    assert source.start_date == date(2026, 6, 1)
    assert source.end_date == date(2026, 6, 5)

    # Source soft-delete lifted
    assert source.merged_into_id is None
    assert source.merged_at is None
    assert source.merge_audit is None

    # Source TripTraveler restored (creator at minimum)
    source_travelers = (
        (await db_session.execute(select(TripTraveler).where(TripTraveler.trip_id == source_id)))
        .scalars()
        .all()
    )
    source_traveler_ids = {tt.user_id for tt in source_travelers}
    assert user_a.id in source_traveler_ids


# ---------------------------------------------------------------------------
# Test 2: 410 after window expired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_after_window_returns_410(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Attempting undo after 7-day window returns 410."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, source, target = await _seed_merge_scenario(db_session, sub_prefix="c2-410win")

    # Manually simulate a merge with merged_at 8 days ago
    source.merged_into_id = target.id
    source.merged_at = datetime.now(UTC) - timedelta(days=8)
    source.merge_audit = {
        "source_segment_ids": [],
        "source_expense_ids": [],
        "source_document_ids": [],
        "added_traveler_user_ids": [],
        "source_start_date": source.start_date.isoformat(),
        "source_end_date": source.end_date.isoformat(),
        "target_start_date_pre_merge": target.start_date.isoformat(),
        "target_end_date_pre_merge": target.end_date.isoformat(),
        "schema_version": 1,
    }
    await db_session.commit()

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
        r = await c.post(f"/trips/{target.id}/undo-merge/{source.id}")

    assert r.status_code == 410


# ---------------------------------------------------------------------------
# Test 3: 409 if target was itself re-merged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_409_if_target_re_merged(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """409 when target was itself merged into a third trip after the original merge."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, source, target = await _seed_merge_scenario(db_session, sub_prefix="c2-409chain")

    # Create a third trip
    third = Trip(
        title="C2 Third",
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 10),
        created_by=user_a.id,
    )
    db_session.add(third)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=third.id, user_id=user_a.id, role="owner"))

    # Simulate source merged into target
    source.merged_into_id = target.id
    source.merged_at = datetime.now(UTC)
    source.merge_audit = {
        "source_segment_ids": [],
        "source_expense_ids": [],
        "source_document_ids": [],
        "added_traveler_user_ids": [],
        "source_start_date": source.start_date.isoformat(),
        "source_end_date": source.end_date.isoformat(),
        "target_start_date_pre_merge": target.start_date.isoformat(),
        "target_end_date_pre_merge": target.end_date.isoformat(),
        "schema_version": 1,
    }
    # Then target itself re-merged into third
    target.merged_into_id = third.id
    target.merged_at = datetime.now(UTC)
    await db_session.commit()

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
        r = await c.post(f"/trips/{target.id}/undo-merge/{source.id}")

    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Test 4: 410 lifted post-undo (GET /trips/{source_id})
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_lifts_source_410_post_undo(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Source trip returns 410 before undo, then 200 after undo."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, source, target = await _seed_merge_scenario(db_session, sub_prefix="c2-lift410")

    source_id = source.id
    target_id = target.id

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
        # Merge
        r_merge = await c.post(f"/trips/{source_id}/merge-into/{target_id}")
        assert r_merge.status_code == 303

        # Source should now be inaccessible (TripTraveler removed during merge).
        # The dep require_traveler_including_merged returns 404 when no TripTraveler row
        # exists, even if the trip itself has merged_into_id set (which would be 410).
        # Both 404 and 410 indicate the source is no longer accessible pre-undo.
        r_before = await c.get(f"/trips/{source_id}")
        assert r_before.status_code in (404, 410)

        # Undo
        r_undo = await c.post(f"/trips/{target_id}/undo-merge/{source_id}")
        assert r_undo.status_code == 303

        # Source should now be 200
        r_after = await c.get(f"/trips/{source_id}")
        assert r_after.status_code == 200


# ---------------------------------------------------------------------------
# Test 5: ICS feed re-includes segments after undo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_ics_feed_re_includes_segments(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """After undo, the source's segment appears back in the ICS feed."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, source, target = await _seed_merge_scenario(db_session, sub_prefix="c2-ics")

    # Mint an ICS token
    plaintext, token_hash = generate_token()
    user_a.ics_token_hash = token_hash
    await db_session.flush()

    base_dt = datetime(2026, 6, 3, 10, tzinfo=UTC)
    seg = Segment(
        trip_id=source.id,
        owner_user_id=user_a.id,
        type="flight",
        status="confirmed",
        start_at=base_dt,
        start_tz="UTC",
        start_location={"city": "UndoCity", "iata": "JFK"},
        end_location={"city": "UndoDest", "iata": "LHR"},
        details={},
        parse_source="test",
        parse_confidence=0.9,
    )
    db_session.add(seg)
    await db_session.commit()

    source_id = source.id
    target_id = target.id

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
        # Merge
        r_merge = await c.post(f"/trips/{source_id}/merge-into/{target_id}")
        assert r_merge.status_code == 303

        # ICS pre-undo: segment should appear on target (trip_id moved)
        r_ics_pre = await c.get(f"/ics/{plaintext}.ics")
        assert r_ics_pre.status_code == 200
        # Segment is on target trip; ICS should include it (target not soft-deleted)
        # Just verify feed works before undo
        assert "BEGIN:VCALENDAR" in r_ics_pre.text

        # Undo
        r_undo = await c.post(f"/trips/{target_id}/undo-merge/{source_id}")
        assert r_undo.status_code == 303

        # ICS post-undo: source is restored, segment is on source trip
        r_ics_post = await c.get(f"/ics/{plaintext}.ics")
        assert r_ics_post.status_code == 200

    body_post = r_ics_post.text
    vevent_blocks = [b for b in body_post.split("BEGIN:VEVENT") if "END:VEVENT" in b]
    # Source's segment city must appear in post-undo ICS
    assert any("UndoCity" in b or "JFK" in b for b in vevent_blocks), (
        "Source segment not found in post-undo ICS feed"
    )


# ---------------------------------------------------------------------------
# Test 6: Multi-user traveler audit — role preserved through merge → undo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_traveler_audit_restores_source_only_rows(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Undo restores added_traveler_user_ids back to source; role preserved."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, source, target = await _seed_merge_scenario(
        db_session,
        sub_prefix="c2-traveler",
        extra_source_users=[("c2-member-b", "companion")],
    )

    # Retrieve user_b from DB (extra user added to source only)
    user_b_row = (
        await db_session.execute(
            select(TripTraveler).where(
                TripTraveler.trip_id == source.id, TripTraveler.role == "companion"
            )
        )
    ).scalar_one()
    user_b_id = user_b_row.user_id

    source_id = source.id
    target_id = target.id

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
        # Merge: user_b should be added to target (in audit)
        r_merge = await c.post(f"/trips/{source_id}/merge-into/{target_id}")
        assert r_merge.status_code == 303

        await db_session.refresh(source)
        audit = source.merge_audit
        assert audit is not None
        assert str(user_b_id) in audit["added_traveler_user_ids"]

        # Verify target has user_b after merge
        target_rows_after_merge = (
            (
                await db_session.execute(
                    select(TripTraveler).where(TripTraveler.trip_id == target_id)
                )
            )
            .scalars()
            .all()
        )
        assert user_b_id in {tt.user_id for tt in target_rows_after_merge}

        # Undo
        r_undo = await c.post(f"/trips/{target_id}/undo-merge/{source_id}")
        assert r_undo.status_code == 303

    # After undo: source must have {user_a, user_b}; target must have only {user_a}
    source_rows = (
        (await db_session.execute(select(TripTraveler).where(TripTraveler.trip_id == source_id)))
        .scalars()
        .all()
    )
    source_user_ids = {tt.user_id for tt in source_rows}
    assert user_a.id in source_user_ids
    assert user_b_id in source_user_ids

    # Verify user_b's role on source is preserved as "companion"
    user_b_on_source = next(tt for tt in source_rows if tt.user_id == user_b_id)
    assert user_b_on_source.role == "companion"

    target_rows = (
        (await db_session.execute(select(TripTraveler).where(TripTraveler.trip_id == target_id)))
        .scalars()
        .all()
    )
    target_user_ids = {tt.user_id for tt in target_rows}
    assert target_user_ids == {user_a.id}


# ---------------------------------------------------------------------------
# Test 7: 403 on non-owner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undo_403_on_non_owner(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """User B cannot undo a merge on a target trip owned by user A (403)."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    _user_a, source, target = await _seed_merge_scenario(db_session, sub_prefix="c2-403")

    # Create user_b (non-owner)
    user_b = User(
        oidc_subject="c2-403-userb",
        email="c2-403-b@x.com",
        display_name="C2 Non-Owner",
    )
    db_session.add(user_b)
    await db_session.flush()

    # Simulate merge done by user_a
    source.merged_into_id = target.id
    source.merged_at = datetime.now(UTC)
    source.merge_audit = {
        "source_segment_ids": [],
        "source_expense_ids": [],
        "source_document_ids": [],
        "added_traveler_user_ids": [],
        "source_start_date": source.start_date.isoformat(),
        "source_end_date": source.end_date.isoformat(),
        "target_start_date_pre_merge": target.start_date.isoformat(),
        "target_end_date_pre_merge": target.end_date.isoformat(),
        "schema_version": 1,
    }
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user_b, settings),
            follow_redirects=False,
        ) as c,
    ):
        r = await c.post(f"/trips/{target.id}/undo-merge/{source.id}")

    assert r.status_code == 403
