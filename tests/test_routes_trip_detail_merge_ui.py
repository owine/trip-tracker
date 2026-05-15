"""C6 tests: GET /trips/{id} merge-UI affordances.

Covers:
- Merge dropdown lists user's other non-merged trips, sorted by date proximity
- Confirm dialog renders source segment/expense/document counts
- Undo flash banner renders for ?merged_from=<source> within 7-day window
- Undo flash banner absent when window has expired
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
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


async def _seed_user(db: AsyncSession, sub: str) -> User:
    user = User(
        oidc_subject=sub,
        email=f"{sub}@x.com",
        display_name=f"User {sub}",
    )
    db.add(user)
    await db.flush()
    return user


async def _seed_trip(
    db: AsyncSession,
    user: User,
    *,
    title: str,
    start: date,
    end: date,
) -> Trip:
    trip = Trip(title=title, start_date=start, end_date=end, created_by=user.id)
    db.add(trip)
    await db.flush()
    db.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    return trip


@pytest.mark.asyncio
async def test_merge_dropdown_lists_other_non_merged_trips_sorted_by_date_proximity(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Dropdown shows other non-merged trips ordered by |start_date - current.start_date|."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _seed_user(db_session, "c6-drop")

    # Current trip: June 10
    current = await _seed_trip(
        db_session, user, title="Current", start=date(2026, 6, 10), end=date(2026, 6, 15)
    )
    # Near trip: June 20 (10 days away) — should appear first
    near = await _seed_trip(
        db_session, user, title="Near", start=date(2026, 6, 20), end=date(2026, 6, 25)
    )
    # Far trip: October 1 (~113 days away) — should appear second
    far = await _seed_trip(
        db_session, user, title="Far", start=date(2026, 10, 1), end=date(2026, 10, 5)
    )
    # Merged-out trip: should NOT appear
    soft_deleted = await _seed_trip(
        db_session, user, title="Gone", start=date(2026, 6, 12), end=date(2026, 6, 14)
    )
    soft_deleted.merged_into_id = current.id
    soft_deleted.merged_at = datetime.now(UTC)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
            follow_redirects=False,
        ) as c,
    ):
        r = await c.get(f"/trips/{current.id}")

    assert r.status_code == 200
    body = r.text

    # Extract <option> values from the merge select, in document order.
    select_match = re.search(
        r'<select[^>]*id="merge-into-select"[^>]*>(.*?)</select>', body, re.DOTALL
    )
    assert select_match is not None, "merge-into-select not found in detail HTML"
    select_html = select_match.group(1)
    option_ids = re.findall(r'<option value="([0-9a-f-]{36})"', select_html)

    assert option_ids == [str(near.id), str(far.id)]
    # Soft-deleted trip must not appear
    assert str(soft_deleted.id) not in option_ids
    # Current trip must not appear (no self-merge)
    assert str(current.id) not in option_ids


@pytest.mark.asyncio
async def test_merge_dialog_renders_source_counts(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Confirm dialog shows segment/expense/document counts of the source (current) trip."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _seed_user(db_session, "c6-counts")
    current = await _seed_trip(
        db_session, user, title="Counts", start=date(2026, 7, 1), end=date(2026, 7, 5)
    )
    # Need another trip so the dropdown (and therefore the dialog) renders.
    await _seed_trip(
        db_session, user, title="Other", start=date(2026, 7, 10), end=date(2026, 7, 12)
    )

    # 2 segments
    for hour in (10, 14):
        db_session.add(
            Segment(
                trip_id=current.id,
                owner_user_id=user.id,
                type="flight",
                status="confirmed",
                start_at=datetime(2026, 7, 2, hour, tzinfo=UTC),
                start_tz="UTC",
                start_location={"city": "A", "iata": "AAA"},
                end_location={"city": "B", "iata": "BBB"},
                details={},
                parse_source="test",
                parse_confidence=0.9,
            )
        )
    # 1 expense
    db_session.add(
        Expense(
            trip_id=current.id,
            owner_user_id=user.id,
            amount_minor=1000,
            currency="USD",
            fx_rate=Decimal("1.0"),
            amount_home_minor=1000,
            home_currency="USD",
            category="transit",
            incurred_on=date(2026, 7, 2),
            status="paid",
        )
    )
    # 1 document
    db_session.add(
        Document(
            owner_user_id=user.id,
            trip_id=current.id,
            filename="x.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256="c" * 64,
            storage_key="cc/" + "c" * 64,
        )
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
            follow_redirects=False,
        ) as c,
    ):
        r = await c.get(f"/trips/{current.id}")

    assert r.status_code == 200
    body = r.text
    dialog_match = re.search(
        r'<dialog[^>]*id="merge-confirm-dialog"[^>]*>(.*?)</dialog>', body, re.DOTALL
    )
    assert dialog_match is not None, "merge-confirm-dialog not found"
    dialog_html = dialog_match.group(1)
    assert "2 segments" in dialog_html
    assert "1 expense" in dialog_html
    assert "1 document" in dialog_html


@pytest.mark.asyncio
async def test_undo_flash_renders_when_merged_from_query_param_set_within_window(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """?merged_from=<source> within 7d renders undo flash with N days remaining."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _seed_user(db_session, "c6-flash-in")
    target = await _seed_trip(
        db_session, user, title="Target", start=date(2026, 8, 1), end=date(2026, 8, 10)
    )
    source = await _seed_trip(
        db_session, user, title="Source-Gone", start=date(2026, 8, 2), end=date(2026, 8, 5)
    )
    source.merged_into_id = target.id
    source.merged_at = datetime.now(UTC) - timedelta(days=3)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
            follow_redirects=False,
        ) as c,
    ):
        r = await c.get(f"/trips/{target.id}?merged_from={source.id}")

    assert r.status_code == 200
    body = r.text
    assert 'id="merge-undo-flash"' in body
    assert "Source-Gone" in body
    # 7 - 3 = 4 days remaining (modulo sub-day rounding, ceil gives 4).
    # Word-bounded so "14 days" / "24 days" can't falsely match.
    assert re.search(r"\b4 days\b", body) is not None
    # Undo form must POST to /trips/<target>/undo-merge/<source>
    assert f'action="/trips/{target.id}/undo-merge/{source.id}"' in body


@pytest.mark.asyncio
async def test_undo_flash_absent_when_outside_window(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Flash banner is suppressed when merged_at is more than 7 days ago."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _seed_user(db_session, "c6-flash-out")
    target = await _seed_trip(
        db_session, user, title="TargetExpired", start=date(2026, 1, 1), end=date(2026, 1, 5)
    )
    source = await _seed_trip(
        db_session, user, title="SourceExpired", start=date(2026, 1, 2), end=date(2026, 1, 3)
    )
    source.merged_into_id = target.id
    source.merged_at = datetime.now(UTC) - timedelta(days=10)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(user, settings),
            follow_redirects=False,
        ) as c,
    ):
        r = await c.get(f"/trips/{target.id}?merged_from={source.id}")

    assert r.status_code == 200
    assert 'id="merge-undo-flash"' not in r.text


@pytest.mark.asyncio
async def test_merge_dropdown_hidden_when_viewer_is_traveler_but_not_creator(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Non-creator travelers can't merge, so the dropdown must not render for them.

    Auth alignment with POST /trips/<source>/merge-into/<target>, which requires
    source.created_by == user.id AND target.created_by == user.id.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    creator = await _seed_user(db_session, "c6-creator")
    viewer = await _seed_user(db_session, "c6-traveler")

    # Trip created by `creator`; `viewer` is a co-traveler.
    trip = await _seed_trip(
        db_session, creator, title="Shared", start=date(2026, 9, 1), end=date(2026, 9, 5)
    )
    db_session.add(TripTraveler(trip_id=trip.id, user_id=viewer.id, role="companion"))
    # An "other" trip exists (viewer's own) so dropdown would otherwise render.
    await _seed_trip(
        db_session, viewer, title="ViewerOwn", start=date(2026, 9, 10), end=date(2026, 9, 12)
    )
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            cookies=_cookie(viewer, settings),
            follow_redirects=False,
        ) as c,
    ):
        r = await c.get(f"/trips/{trip.id}")

    assert r.status_code == 200
    body = r.text
    assert 'id="merge-into-select"' not in body
    assert 'id="merge-confirm-dialog"' not in body
