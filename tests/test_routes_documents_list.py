"""GET /trips/{id}/documents — full-page list."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.document import Document
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


def _cookie(user, settings):
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


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
    u = User(oidc_subject="lst1", email="lst1@x.com", display_name="LST1")
    db.add(u)
    await db.flush()
    t = Trip(title="Paris", start_date=date(2026, 6, 1), end_date=date(2026, 6, 7), created_by=u.id)
    db.add(t)
    await db.flush()
    db.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
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
    u = User(oidc_subject="lst2", email="lst2@x.com", display_name="LST2")
    db_session.add(u)
    await db_session.flush()
    t = Trip(title="Empty", start_date=date(2026, 6, 1), end_date=date(2026, 6, 2), created_by=u.id)
    db_session.add(t)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    await db_session.commit()
    async with authenticated_client_factory(u) as client:
        r = await client.get(f"/trips/{t.id}/documents")
    assert r.status_code == 200
    assert "No documents yet" in r.text


@pytest.mark.asyncio
async def test_list_404_for_non_traveler(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    _u, t, _d = await _seed(db_session)
    other = User(oidc_subject="lst3", email="lst3@x.com", display_name="LST3")
    db_session.add(other)
    await db_session.commit()
    async with authenticated_client_factory(other) as client:
        r = await client.get(f"/trips/{t.id}/documents")
    assert r.status_code == 404
