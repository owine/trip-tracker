"""RawEmail model: Message-ID uniqueness, parse_status check, jsonb headers."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.raw_email import RawEmail


@pytest.mark.asyncio
async def test_create_raw_email(db_session: AsyncSession) -> None:
    e = RawEmail(
        to_address="oliver@trips.example.com",
        from_address="confirmations@delta.com",
        subject="Your trip confirmation",
        message_id="<abc123@delta.com>",
        mime_blob=b"From: confirmations@delta.com\r\n\r\nbody",
        headers={"Subject": "Your trip confirmation"},
    )
    db_session.add(e)
    await db_session.commit()

    fetched = (
        await db_session.execute(
            select(RawEmail).where(RawEmail.message_id == "<abc123@delta.com>")
        )
    ).scalar_one()
    assert fetched.parse_status == "pending"
    assert fetched.headers["Subject"] == "Your trip confirmation"


@pytest.mark.asyncio
async def test_message_id_unique(db_session: AsyncSession) -> None:
    base = {
        "to_address": "oliver@trips.example.com",
        "from_address": "x@example.com",
        "message_id": "<dup@example.com>",
        "mime_blob": b"",
        "headers": {},
    }
    db_session.add(RawEmail(**base))
    await db_session.commit()
    db_session.add(RawEmail(**base))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_parse_status_check(db_session: AsyncSession) -> None:
    e = RawEmail(
        to_address="o@x.com",
        from_address="f@x.com",
        message_id="<x@x.com>",
        mime_blob=b"",
        headers={},
        parse_status="bogus",
    )
    db_session.add(e)
    with pytest.raises(IntegrityError):
        await db_session.commit()
