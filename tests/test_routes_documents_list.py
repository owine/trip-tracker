"""GET /trips/{id}/documents — full-page list."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date

import httpx
import pytest
from fastapi import Response as _Response
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings
from trip_tracker.models.document import Document
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
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as client,
    ):
        yield client


@pytest.fixture
def authenticated_client_factory(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)

    def _make(user):
        settings = Settings()
        app = create_app(settings=settings)
        return _ctx(app, settings, user)

    return _make


async def _seed(db: AsyncSession) -> tuple[User, Trip, Document]:
    u = User(id=OWNER_USER_ID, email="lst1@x.com", display_name="LST1")
    db.add(u)
    await db.flush()
    t = Trip(title="Paris", start_date=date(2026, 6, 1), end_date=date(2026, 6, 7))
    db.add(t)
    await db.flush()
    d = Document(
        owner_user_id=u.id,
        trip_id=t.id,
        filename="bp.pdf",
        mime_type="application/pdf",
        size_bytes=1024,
        sha256="x" * 64,
        storage_key="xx/" + "x" * 64,
        extract_status="extracted",
        extracted_text="hello",
    )
    db.add(d)
    await db.commit()
    return u, t, d


@pytest.mark.asyncio
async def test_list_renders_document(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u, t, d = await _seed(db_session)
    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/trips/{t.id}/documents")
    assert r.status_code == 200
    assert "bp.pdf" in r.text
    assert "Upload PDF" in r.text  # form heading
    assert f"/documents/{d.id}/download" in r.text


@pytest.mark.asyncio
async def test_list_empty_state(db_session: AsyncSession, authenticated_client_factory) -> None:
    u = User(id=OWNER_USER_ID, email="lst2@x.com", display_name="LST2")
    db_session.add(u)
    await db_session.flush()
    t = Trip(title="Empty", start_date=date(2026, 6, 1), end_date=date(2026, 6, 2))
    db_session.add(t)
    await db_session.commit()
    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/trips/{t.id}/documents")
    assert r.status_code == 200
    assert "No documents yet" in r.text
