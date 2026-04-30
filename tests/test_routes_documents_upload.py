"""POST /trips/{id}/documents — manual upload + dedup + size cap."""

from __future__ import annotations

import io
from contextlib import asynccontextmanager
from datetime import date

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.document import Document
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User

PDF_BODY = b"%PDF-1.4\n%fake content for tests\n"


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


async def _seed(db: AsyncSession) -> tuple[User, Trip]:
    u = User(oidc_subject="up1", email="up1@x.com", display_name="UP1")
    db.add(u)
    await db.flush()
    t = Trip(
        title="T",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 2),
        created_by=u.id,
    )
    db.add(t)
    await db.flush()
    db.add(TripTraveler(trip_id=t.id, user_id=u.id, role="owner"))
    await db.commit()
    return u, t


@pytest.mark.asyncio
async def test_upload_to_trip_creates_document(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u, t = await _seed(db_session)
    async with authenticated_client_factory(u) as client:
        r = await client.post(
            f"/trips/{t.id}/documents",
            files={"file": ("boarding-pass.pdf", io.BytesIO(PDF_BODY), "application/pdf")},
        )
    assert r.status_code in (200, 303)
    docs = (
        (await db_session.execute(select(Document).where(Document.trip_id == t.id))).scalars().all()
    )
    assert len(docs) == 1
    assert docs[0].filename == "boarding-pass.pdf"
    assert docs[0].extract_status == "pending"
    assert docs[0].mime_type == "application/pdf"


@pytest.mark.asyncio
async def test_upload_dedup_returns_303_on_second_upload(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u, t = await _seed(db_session)
    async with authenticated_client_factory(u) as client:
        r1 = await client.post(
            f"/trips/{t.id}/documents",
            files={"file": ("a.pdf", io.BytesIO(PDF_BODY), "application/pdf")},
        )
        assert r1.status_code in (200, 303)
        r2 = await client.post(
            f"/trips/{t.id}/documents",
            files={"file": ("a.pdf", io.BytesIO(PDF_BODY), "application/pdf")},
        )
        assert r2.status_code == 303

    docs = (await db_session.execute(select(Document))).scalars().all()
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_upload_rejects_non_pdf(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    u, t = await _seed(db_session)
    async with authenticated_client_factory(u) as client:
        r = await client.post(
            f"/trips/{t.id}/documents",
            files={"file": ("evil.png", io.BytesIO(b"\x89PNG\r\n"), "application/pdf")},
        )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_upload_size_cap_returns_413(
    db_session: AsyncSession, authenticated_client_factory, monkeypatch
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "32")
    u, t = await _seed(db_session)
    big = PDF_BODY * 10
    async with authenticated_client_factory(u) as client:
        r = await client.post(
            f"/trips/{t.id}/documents",
            files={"file": ("big.pdf", io.BytesIO(big), "application/pdf")},
        )
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_upload_requires_traveler_membership(
    db_session: AsyncSession, authenticated_client_factory
) -> None:
    _owner, t = await _seed(db_session)
    other = User(oidc_subject="up2", email="up2@x.com", display_name="UP2")
    db_session.add(other)
    await db_session.commit()
    async with authenticated_client_factory(other) as client:
        r = await client.post(
            f"/trips/{t.id}/documents",
            files={"file": ("a.pdf", io.BytesIO(PDF_BODY), "application/pdf")},
        )
    assert r.status_code in (403, 404)
