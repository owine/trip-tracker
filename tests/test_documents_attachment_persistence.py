"""persist_pdf_attachments: extract + UPSERT + storage write."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.config import Settings
from trip_tracker.documents.persist import persist_pdf_attachments
from trip_tracker.models.document import Document
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.user import User

PDF = b"%PDF-1.4\nfake bp\n"


def _email_with(*pdfs: tuple[str, bytes], non_pdf: bytes | None = None) -> bytes:
    msg = EmailMessage()
    msg["From"] = "airline@example.com"
    msg["To"] = "oliver@trips.example.com"
    msg["Subject"] = "Your boarding pass"
    msg["Message-ID"] = "<persist1@test>"
    msg.set_content("Body text.")
    for filename, payload in pdfs:
        msg.add_attachment(payload, maintype="application", subtype="pdf", filename=filename)
    if non_pdf is not None:
        msg.add_attachment(non_pdf, maintype="image", subtype="png", filename="snap.png")
    return msg.as_bytes()


@pytest.mark.asyncio
async def test_pdf_attachment_creates_document_with_owner(
    db_session: AsyncSession, tmp_path: Path
) -> None:
    u = User(oidc_subject="pa1", email="pa1@x.com", display_name="PA1")
    db_session.add(u)
    await db_session.flush()
    re_ = RawEmail(
        to_address="oliver@trips.example.com",
        from_address="x@x.com",
        subject="bp",
        message_id="<pa1@test>",
        mime_blob=b"",
        headers={},
        parse_status="pending",
    )
    db_session.add(re_)
    await db_session.commit()

    settings = Settings(documents_dir=tmp_path)
    body = _email_with(("bp.pdf", PDF))

    new_ids = await persist_pdf_attachments(
        db_session,
        settings,
        raw_email_id=re_.id,
        owner_user_id=u.id,
        body=body,
    )
    await db_session.commit()  # caller commits; helper does not

    docs = (await db_session.execute(select(Document))).scalars().all()
    assert len(docs) == 1
    assert docs[0].filename == "bp.pdf"
    assert docs[0].owner_user_id == u.id
    assert docs[0].raw_email_id == re_.id
    assert docs[0].extract_status == "pending"
    assert docs[0].segment_id is None
    assert docs[0].trip_id is None
    assert new_ids == [docs[0].id]
    file_path = tmp_path / docs[0].storage_key
    assert file_path.exists()
    assert file_path.read_bytes() == PDF


@pytest.mark.asyncio
async def test_idempotent_on_duplicate_sha256(db_session: AsyncSession, tmp_path: Path) -> None:
    u = User(oidc_subject="pa2", email="pa2@x.com", display_name="PA2")
    db_session.add(u)
    await db_session.flush()
    re_ = RawEmail(
        to_address="o@x",
        from_address="x",
        subject="x",
        message_id="<pa2@test>",
        mime_blob=b"",
        headers={},
        parse_status="pending",
    )
    db_session.add(re_)
    await db_session.commit()

    settings = Settings(documents_dir=tmp_path)
    body = _email_with(("bp.pdf", PDF))

    first_ids = await persist_pdf_attachments(
        db_session,
        settings,
        raw_email_id=re_.id,
        owner_user_id=u.id,
        body=body,
    )
    await db_session.commit()
    second_ids = await persist_pdf_attachments(
        db_session,
        settings,
        raw_email_id=re_.id,
        owner_user_id=u.id,
        body=body,
    )
    await db_session.commit()

    docs = (await db_session.execute(select(Document))).scalars().all()
    assert len(docs) == 1
    assert len(first_ids) == 1
    assert second_ids == []  # second call: existing row, no new id returned


@pytest.mark.asyncio
async def test_non_pdf_attachment_dropped(db_session: AsyncSession, tmp_path: Path) -> None:
    u = User(oidc_subject="pa3", email="pa3@x.com", display_name="PA3")
    db_session.add(u)
    await db_session.flush()
    re_ = RawEmail(
        to_address="o@x",
        from_address="x",
        subject="x",
        message_id="<pa3@test>",
        mime_blob=b"",
        headers={},
        parse_status="pending",
    )
    db_session.add(re_)
    await db_session.commit()

    settings = Settings(documents_dir=tmp_path)
    body = _email_with(non_pdf=b"\x89PNG\r\n")

    new_ids = await persist_pdf_attachments(
        db_session,
        settings,
        raw_email_id=re_.id,
        owner_user_id=u.id,
        body=body,
    )
    await db_session.commit()

    docs = (await db_session.execute(select(Document))).scalars().all()
    assert docs == []
    assert new_ids == []
