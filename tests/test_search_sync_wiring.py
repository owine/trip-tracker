"""Verify write sites enqueue meili sync after commit.

Spec §5.1 catalogs 8 sites; this test exercises 3 representative ones
(create_segment, delete_segment, inbox.discard). Adding new write sites
later means adding new tests here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import Response as _Response
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    r = _Response()
    set_session_cookie(r, user_id=user.id, settings=settings)
    return {"tt_session": r.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


@pytest.mark.asyncio
async def test_create_segment_enqueues_meili_sync(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POST /segments enqueues sync for the new segment AND its trip."""
    from datetime import date as dtdate

    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(id=OWNER_USER_ID, email="t2@x.com", display_name="T2")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(
        title="Existing",
        start_date=dtdate(2026, 6, 1),
        end_date=dtdate(2026, 6, 5),
    )
    db_session.add(trip)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    with patch("trip_tracker.routes.segments.enqueue_meili_sync", new=AsyncMock()) as mock:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://test", cookies=_cookie(user, settings)
            ) as c,
        ):
            r = await c.post(
                "/segments",
                data={
                    "type": "flight",
                    "trip_selector_existing_trip_id": str(trip.id),
                    "status": "confirmed",
                    "start_local": "2026-06-02T09:00",
                    "start_tz": "America/New_York",
                    "end_local": "2026-06-02T22:00",
                    "end_tz": "Europe/Paris",
                },
                follow_redirects=False,
            )
    assert r.status_code == 303
    # Should be called at least once with entity='segment' and once with entity='trip'
    entities = {call.kwargs.get("entity") for call in mock.call_args_list}
    assert "segment" in entities
    assert "trip" in entities


@pytest.mark.asyncio
async def test_delete_segment_enqueues_meili_sync(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POST /trips/<tid>/segments/<sid>/delete enqueues sync for that segment."""
    from datetime import date as dtdate

    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(id=OWNER_USER_ID, email="t3@x.com", display_name="T3")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(
        title="T",
        start_date=dtdate(2026, 6, 1),
        end_date=dtdate(2026, 6, 5),
    )
    db_session.add(trip)
    await db_session.flush()
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 2, 13, tzinfo=UTC),
        start_tz="UTC",
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    with patch("trip_tracker.routes.segments.enqueue_meili_sync", new=AsyncMock()) as mock:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://test", cookies=_cookie(user, settings)
            ) as c,
        ):
            r = await c.post(
                f"/trips/{trip.id}/segments/{seg.id}/delete",
                follow_redirects=False,
            )
    assert r.status_code == 303
    mock.assert_awaited()
    kwargs = mock.call_args.kwargs
    assert kwargs.get("entity") == "segment"


@pytest.mark.asyncio
async def test_inbox_discard_enqueues_meili_sync(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POST /inbox/<rid>/discard enqueues sync for each cascaded segment."""
    from datetime import date as dtdate

    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(id=OWNER_USER_ID, email="t4@x.com", display_name="T4")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="x@y.com",
        subject="t",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=b"",
        headers={},
        parse_status="review",
    )
    trip = Trip(
        title="T",
        start_date=dtdate(2026, 6, 1),
        end_date=dtdate(2026, 6, 5),
    )
    db_session.add_all([raw, trip])
    await db_session.flush()
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
    with patch("trip_tracker.routes.inbox.enqueue_meili_sync", new=AsyncMock()) as mock:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://test", cookies=_cookie(user, settings)
            ) as c,
        ):
            r = await c.post(f"/inbox/{raw.id}/discard", follow_redirects=False)
    assert r.status_code == 303
    mock.assert_awaited()  # at least one segment sync was enqueued
    kwargs = mock.call_args.kwargs
    assert kwargs.get("entity") == "segment"
