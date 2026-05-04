"""B7 + C1 tests: 410 on soft-deleted trip detail, ICS feed, and merge-into route."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
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


# ---------------------------------------------------------------------------
# C1 helpers
# ---------------------------------------------------------------------------


async def _c1_seed_two_trips(
    db: AsyncSession,
    *,
    source_sub: str = "c1-src",
    target_sub: str = "c1-tgt",
    same_user: bool = True,
) -> tuple[User, User, Trip, Trip]:
    """Create two users + two trips.

    If same_user=True both trips belong to user_a.
    If same_user=False source → user_a, target → user_b.
    """
    user_a = User(
        oidc_subject=source_sub,
        email=f"{source_sub}@x.com",
        display_name="C1 UserA",
    )
    db.add(user_a)
    await db.flush()

    if same_user:
        user_b = user_a
    else:
        user_b = User(
            oidc_subject=target_sub,
            email=f"{target_sub}@x.com",
            display_name="C1 UserB",
        )
        db.add(user_b)
        await db.flush()

    source = Trip(
        title="C1 Source",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        created_by=user_a.id,
    )
    db.add(source)
    await db.flush()
    db.add(TripTraveler(trip_id=source.id, user_id=user_a.id, role="owner"))

    target = Trip(
        title="C1 Target",
        start_date=date(2026, 7, 10),
        end_date=date(2026, 7, 15),
        created_by=user_b.id,
    )
    db.add(target)
    await db.flush()
    db.add(TripTraveler(trip_id=target.id, user_id=user_b.id, role="owner"))

    await db.commit()
    return user_a, user_b, source, target


# ---------------------------------------------------------------------------
# C1 tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_happy_path_reassigns_segments_expenses_documents_and_widens_dates(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POST /trips/{source}/merge-into/{target} reassigns FKs, widens dates, soft-deletes source."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, source, target = await _c1_seed_two_trips(db_session)

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
    db_session.add(seg)
    await db_session.flush()

    exp = Expense(
        trip_id=source.id,
        owner_user_id=user_a.id,
        amount_minor=1000,
        currency="USD",
        fx_rate=Decimal("1.0"),
        amount_home_minor=1000,
        home_currency="USD",
        category="transport",
        incurred_on=date(2026, 6, 3),
        status="paid",
    )
    db_session.add(exp)
    await db_session.flush()

    doc = Document(
        owner_user_id=user_a.id,
        trip_id=source.id,
        filename="itinerary.pdf",
        mime_type="application/pdf",
        size_bytes=100,
        sha256="a" * 64,
        storage_key="aa/" + "a" * 64,
    )
    db_session.add(doc)
    await db_session.commit()

    # Capture IDs before merge
    source_id = source.id
    target_id = target.id
    seg_id = seg.id
    exp_id = exp.id
    doc_id = doc.id

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
        r = await c.post(f"/trips/{source_id}/merge-into/{target_id}")

    assert r.status_code == 303
    assert r.headers["location"] == f"/trips/{target_id}?merged_from={source_id}"

    # Refetch in db_session to verify DB state
    await db_session.refresh(seg)
    await db_session.refresh(exp)
    await db_session.refresh(doc)
    await db_session.refresh(source)
    await db_session.refresh(target)

    assert seg.trip_id == target_id
    assert exp.trip_id == target_id
    assert doc.trip_id == target_id

    assert target.start_date == date(2026, 6, 1)  # widened from Jul 10
    assert target.end_date == date(2026, 7, 15)  # kept Jul 15

    assert source.merged_into_id == target_id
    assert source.merged_at is not None
    audit = source.merge_audit
    assert isinstance(audit, dict)
    assert set(audit.keys()) >= {
        "source_segment_ids",
        "source_expense_ids",
        "source_document_ids",
        "added_traveler_user_ids",
        "source_start_date",
        "source_end_date",
        "target_start_date_pre_merge",
        "target_end_date_pre_merge",
        "schema_version",
    }
    assert audit["schema_version"] == 1
    assert str(seg_id) in audit["source_segment_ids"]
    assert str(exp_id) in audit["source_expense_ids"]
    assert str(doc_id) in audit["source_document_ids"]
    # Pre-merge target dates (Jul 10-15 from _c1_seed_two_trips)
    assert audit["target_start_date_pre_merge"] == date(2026, 7, 10).isoformat()
    assert audit["target_end_date_pre_merge"] == date(2026, 7, 15).isoformat()


@pytest.mark.asyncio
async def test_merge_403_on_non_owner_source(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """User B cannot merge a source trip they don't own (user A owns source)."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    _user_a, user_b, source, target = await _c1_seed_two_trips(
        db_session,
        source_sub="c1-403src-a",
        target_sub="c1-403src-b",
        same_user=False,
    )
    # user_b owns target; user_a owns source. user_b tries to merge.
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
        r = await c.post(f"/trips/{source.id}/merge-into/{target.id}")

    assert r.status_code == 403


@pytest.mark.asyncio
async def test_merge_403_on_non_owner_target(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """User A cannot merge into a target trip they don't own (user B owns target)."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _user_b, source, target = await _c1_seed_two_trips(
        db_session,
        source_sub="c1-403tgt-a",
        target_sub="c1-403tgt-b",
        same_user=False,
    )
    # user_a owns source; user_b owns target. user_a tries to merge.
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
        r = await c.post(f"/trips/{source.id}/merge-into/{target.id}")

    assert r.status_code == 403


@pytest.mark.asyncio
async def test_merge_400_on_self_merge(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POST /trips/{x}/merge-into/{x} must return 400."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, source, _target = await _c1_seed_two_trips(
        db_session, source_sub="c1-self-a", target_sub="c1-self-b"
    )
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
        r = await c.post(f"/trips/{source.id}/merge-into/{source.id}")

    assert r.status_code == 400


@pytest.mark.asyncio
async def test_merge_400_if_source_already_merged(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Merging an already-merged source trip must return 400."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, source, target = await _c1_seed_two_trips(
        db_session, source_sub="c1-src-alr-a", target_sub="c1-src-alr-b"
    )
    # Pre-soft-delete source
    source.merged_into_id = target.id
    source.merged_at = datetime.now(UTC)
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
        r = await c.post(f"/trips/{source.id}/merge-into/{target.id}")

    assert r.status_code == 400


@pytest.mark.asyncio
async def test_merge_400_if_target_already_merged(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Merging into an already-merged target trip must return 400."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, source, target = await _c1_seed_two_trips(
        db_session, source_sub="c1-tgt-alr-a", target_sub="c1-tgt-alr-b"
    )
    # Pre-soft-delete target (point it elsewhere)
    target.merged_into_id = source.id
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
        r = await c.post(f"/trips/{source.id}/merge-into/{target.id}")

    assert r.status_code == 400


@pytest.mark.asyncio
async def test_merge_404_on_nonexistent_source(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Non-existent source UUID must return 404 (existence check BEFORE ownership)."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, _source, target = await _c1_seed_two_trips(
        db_session, source_sub="c1-404src-a", target_sub="c1-404src-b"
    )
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
        r = await c.post(f"/trips/{ghost_id}/merge-into/{target.id}")

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_merge_404_on_nonexistent_target(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Non-existent target UUID must return 404 (existence check BEFORE ownership)."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, source, _target = await _c1_seed_two_trips(
        db_session, source_sub="c1-404tgt-a", target_sub="c1-404tgt-b"
    )
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
        r = await c.post(f"/trips/{source.id}/merge-into/{ghost_id}")

    assert r.status_code == 404


@pytest.mark.asyncio
async def test_merge_audit_added_traveler_user_ids_diffs_correctly(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """added_traveler_user_ids must be only users added to target, NOT pre-existing ones."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()

    # Both source and target share user_a; source also has user_b.
    user_a = User(oidc_subject="c1-diff-a", email="c1-diff-a@x.com", display_name="Diff A")
    user_b = User(oidc_subject="c1-diff-b", email="c1-diff-b@x.com", display_name="Diff B")
    db_session.add(user_a)
    db_session.add(user_b)
    await db_session.flush()

    source = Trip(
        title="Diff Source",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        created_by=user_a.id,
    )
    target = Trip(
        title="Diff Target",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 5),
        created_by=user_a.id,
    )
    db_session.add(source)
    db_session.add(target)
    await db_session.flush()

    # Both trips have user_a; source also has user_b.
    db_session.add(TripTraveler(trip_id=source.id, user_id=user_a.id, role="owner"))
    db_session.add(TripTraveler(trip_id=source.id, user_id=user_b.id, role="companion"))
    db_session.add(TripTraveler(trip_id=target.id, user_id=user_a.id, role="owner"))
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
        r = await c.post(f"/trips/{source_id}/merge-into/{target_id}")

    assert r.status_code == 303

    await db_session.refresh(source)
    audit = source.merge_audit
    assert audit is not None
    # Only user_b should be listed (user_a was already on target)
    assert audit["added_traveler_user_ids"] == [str(user_b.id)]

    # Verify target's travelers: should have both user_a and user_b (no duplicates)
    rows = (
        (await db_session.execute(select(TripTraveler).where(TripTraveler.trip_id == target_id)))
        .scalars()
        .all()
    )
    traveler_user_ids = {tt.user_id for tt in rows}
    assert traveler_user_ids == {user_a.id, user_b.id}


@pytest.mark.asyncio
async def test_merge_listings_exclude_soft_deleted_after_merge(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """GET /trips must not show source trip after it is merged (soft-deleted)."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, _, source, target = await _c1_seed_two_trips(
        db_session, source_sub="c1-list-a", target_sub="c1-list-b"
    )
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
        # First merge source into target
        merge_r = await c.post(f"/trips/{source_id}/merge-into/{target_id}")
        assert merge_r.status_code == 303

        # Then check the listing
        list_r = await c.get("/trips")

    assert list_r.status_code == 200
    # Source trip title must not appear; target should still be visible
    assert "C1 Source" not in list_r.text
    assert "C1 Target" in list_r.text
