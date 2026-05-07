"""Document link / unlink / delete routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime

import httpx
import pytest
from fastapi import Response as _Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings
from trip_tracker.models.document import Document
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User


def _cookie(user, settings):
    r = _Response()
    set_session_cookie(r, user_id=user.id, settings=settings)
    return {"tt_session": r.headers["set-cookie"].split(";")[0].split("=", 1)[1]}


@asynccontextmanager
async def _ctx(app, settings, user):
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
def authenticated_client_factory(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)

    def _make(user):
        # Rebuild Settings + app per call so tests can monkeypatch env BEFORE
        # invoking the factory and have the changes take effect.
        settings = Settings()
        app = create_app(settings=settings)
        return _ctx(app, settings, user)

    return _make


async def _seed_with_segment_and_doc(
    db: AsyncSession,
) -> tuple[User, Trip, Segment, Document]:
    u = User(id=OWNER_USER_ID, email="lk1@x.com", display_name="LK1")
    db.add(u)
    await db.flush()
    t = Trip(
        title="T",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 2),
    )
    db.add(t)
    await db.flush()
    s = Segment(
        trip_id=t.id,
        owner_user_id=u.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
        parse_source="manual",
        parse_confidence=1.0,
    )
    db.add(s)
    await db.flush()
    d = Document(
        owner_user_id=u.id,
        trip_id=t.id,
        filename="x.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="d" * 64,
        storage_key="dd/" + "d" * 64,
    )
    db.add(d)
    await db.commit()
    return u, t, s, d


@pytest.mark.asyncio
async def test_link_attaches_segment_id(db_session, authenticated_client_factory) -> None:
    u, _t, s, d = await _seed_with_segment_and_doc(db_session)
    async with authenticated_client_factory(u) as client:
        r = await client.post(f"/documents/{d.id}/link", data={"segment_id": str(s.id)})
    assert r.status_code in (200, 303)
    await db_session.refresh(d)
    assert d.segment_id == s.id


@pytest.mark.asyncio
async def test_unlink_clears_segment_id(db_session, authenticated_client_factory) -> None:
    u, _t, _s, d = await _seed_with_segment_and_doc(db_session)
    d.segment_id = _s.id
    db_session.add(d)
    await db_session.commit()
    async with authenticated_client_factory(u) as client:
        r = await client.post(f"/documents/{d.id}/unlink")
    assert r.status_code in (200, 303)
    await db_session.refresh(d)
    assert d.segment_id is None


@pytest.mark.asyncio
async def test_delete_removes_row(db_session, authenticated_client_factory) -> None:
    u, _t, _s, d = await _seed_with_segment_and_doc(db_session)
    async with authenticated_client_factory(u) as client:
        r = await client.delete(f"/documents/{d.id}")
    assert r.status_code in (200, 204, 303)
    rows = (await db_session.execute(select(Document).where(Document.id == d.id))).scalars().all()
    assert rows == []
