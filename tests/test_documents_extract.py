"""extract_document saq task: pdfplumber → extracted_text."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from trip_tracker.config import Settings
from trip_tracker.documents.extract import extract_document
from trip_tracker.documents.helpers import sha256_hex
from trip_tracker.documents.storage import LocalFsStorage
from trip_tracker.models.document import Document
from trip_tracker.models.user import User

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "documents"


async def _seed_doc(
    db: AsyncSession,
    *,
    storage: LocalFsStorage,
    fixture: str,
) -> Document:
    u = User(email=f"{fixture}@x.com", display_name="EX")
    db.add(u)
    await db.flush()
    payload = (FIXTURE_DIR / fixture).read_bytes()
    sha = sha256_hex(payload)
    await storage.put(sha, payload)
    d = Document(
        owner_user_id=u.id,
        filename=fixture,
        mime_type="application/pdf",
        size_bytes=len(payload),
        sha256=sha,
        storage_key=f"{sha[:2]}/{sha}",
        extract_status="pending",
    )
    db.add(d)
    await db.commit()
    return d


@pytest.mark.asyncio
async def test_extract_text_pdf_populates_extracted_text(
    db_url: str, db_session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFsStorage(tmp_path)
    d = await _seed_doc(db_session, storage=storage, fixture="tiny-text.pdf")
    engine = create_async_engine(db_url)
    settings = Settings()
    ctx = {"engine": engine, "settings": settings, "storage": storage}
    await extract_document(ctx, document_id=str(d.id))
    await engine.dispose()
    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.extract_status == "extracted"
    assert refreshed.extract_method == "pdfplumber"
    assert "AIR FRANCE" in (refreshed.extracted_text or "")


@pytest.mark.asyncio
async def test_extract_empty_pdf_marked_empty(
    db_url: str, db_session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFsStorage(tmp_path)
    d = await _seed_doc(db_session, storage=storage, fixture="tiny-empty.pdf")
    engine = create_async_engine(db_url)
    ctx = {"engine": engine, "settings": Settings(), "storage": storage}
    await extract_document(ctx, document_id=str(d.id))
    await engine.dispose()
    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.extract_status == "empty"
    assert refreshed.extracted_text in (None, "")


@pytest.mark.asyncio
async def test_extract_idempotent_on_already_extracted(
    db_url: str, db_session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFsStorage(tmp_path)
    d = await _seed_doc(db_session, storage=storage, fixture="tiny-text.pdf")
    d.extract_status = "extracted"
    d.extracted_text = "preserved"
    db_session.add(d)
    await db_session.commit()
    engine = create_async_engine(db_url)
    ctx = {"engine": engine, "settings": Settings(), "storage": storage}
    await extract_document(ctx, document_id=str(d.id))
    await engine.dispose()
    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.extracted_text == "preserved"


@pytest.mark.asyncio
async def test_extract_marks_failed_on_pdfplumber_error(
    db_url: str, db_session: AsyncSession, tmp_path: Path, monkeypatch
) -> None:
    storage = LocalFsStorage(tmp_path)
    d = await _seed_doc(db_session, storage=storage, fixture="tiny-text.pdf")

    def _boom(buf):
        raise ValueError("simulated PDFSyntaxError")

    monkeypatch.setattr("trip_tracker.documents.extract._extract_pdf", _boom)
    engine = create_async_engine(db_url)
    ctx = {"engine": engine, "settings": Settings(), "storage": storage}
    await extract_document(ctx, document_id=str(d.id))
    await engine.dispose()
    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.extract_status == "failed"
    assert refreshed.extracted_text is None


@pytest.mark.asyncio
async def test_extract_marks_unsupported_for_non_pdf_mime(
    db_url: str, db_session: AsyncSession, tmp_path: Path
) -> None:
    storage = LocalFsStorage(tmp_path)
    u = User(email="ex2@x.com", display_name="EX2")
    db_session.add(u)
    await db_session.flush()
    d = Document(
        owner_user_id=u.id,
        filename="x.png",
        mime_type="image/png",
        size_bytes=10,
        sha256="2" * 64,
        storage_key="22/" + "2" * 64,
        extract_status="pending",
    )
    db_session.add(d)
    await db_session.commit()
    engine = create_async_engine(db_url)
    ctx = {"engine": engine, "settings": Settings(), "storage": storage}
    await extract_document(ctx, document_id=str(d.id))
    await engine.dispose()
    refreshed = (await db_session.execute(select(Document).where(Document.id == d.id))).scalar_one()
    await db_session.refresh(refreshed)
    assert refreshed.extract_status == "unsupported"
    assert refreshed.extracted_text is None
