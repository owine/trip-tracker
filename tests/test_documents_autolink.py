"""Auto-link heuristic: filename → segment.id."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.session import OWNER_USER_ID
from trip_tracker.documents.autolink import (
    autolink_pending_for_email,
    match_attachment_to_segment,
)
from trip_tracker.models.document import Document
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User

# ─── Pure-function tests (no DB) ─────────────────────────────────────


@dataclass
class FakeSeg:
    id: uuid.UUID
    type: str
    confirmation_number: str | None
    details: dict[str, str]
    start_at: datetime


def _seg(
    *,
    conf: str | None = None,
    fnum: str | None = None,
    tnum: str | None = None,
    type_: str = "flight",
    start: datetime = datetime(2026, 6, 1, 13, tzinfo=UTC),
) -> FakeSeg:
    details: dict[str, str] = {}
    if fnum:
        details["flight_number"] = fnum
    if tnum:
        details["train_number"] = tnum
    return FakeSeg(uuid.uuid4(), type_, conf, details, start)


def test_match_by_confirmation_number() -> None:
    s = _seg(conf="ABC123")
    assert match_attachment_to_segment("BoardingPass_ABC123.pdf", [s]) == s.id


def test_match_by_flight_number() -> None:
    s = _seg(fnum="AF7237")
    assert match_attachment_to_segment("af7237_paris.pdf", [s]) == s.id


def test_match_by_train_number() -> None:
    s = _seg(tnum="9023", type_="train")
    assert match_attachment_to_segment("Ticket_9023.pdf", [s]) == s.id


def test_match_by_unique_date_dash() -> None:
    s = _seg(start=datetime(2026, 6, 1, 13, tzinfo=UTC))
    assert match_attachment_to_segment("boarding_2026-06-01.pdf", [s]) == s.id


def test_match_by_unique_date_compact() -> None:
    s = _seg(start=datetime(2026, 6, 1, 13, tzinfo=UTC))
    assert match_attachment_to_segment("boarding_20260601.pdf", [s]) == s.id


def test_no_match_returns_none() -> None:
    s = _seg(conf="ABC123", fnum="AF999")
    assert match_attachment_to_segment("random.pdf", [s]) is None


def test_ambiguous_date_returns_none() -> None:
    s1 = _seg(start=datetime(2026, 6, 1, 13, tzinfo=UTC))
    s2 = _seg(start=datetime(2026, 6, 1, 19, tzinfo=UTC))
    assert match_attachment_to_segment("boarding_2026-06-01.pdf", [s1, s2]) is None


def test_first_match_wins_when_both_conf_and_date_apply() -> None:
    s_conf = _seg(conf="XYZ999")
    s_date = _seg(start=datetime(2026, 6, 5, tzinfo=UTC))
    assert match_attachment_to_segment("BP_XYZ999_2026-06-05.pdf", [s_date, s_conf]) == s_conf.id


def test_case_insensitive_confirmation_match() -> None:
    s = _seg(conf="AbC123")
    assert match_attachment_to_segment("boardingpass_abc123.pdf", [s]) == s.id


def test_word_boundary_prevents_partial_match() -> None:
    s = _seg(conf="ABC123")
    # "ABC123" exists but not as a word boundary
    assert match_attachment_to_segment("PREABXC123POST.pdf", [s]) is None


# ─── ORM-level integration tests ─────────────────────────────────────


async def _seed_email_seg_doc(
    db: AsyncSession,
    *,
    doc_filename: str,
    seg_conf: str | None = None,
    pre_linked_segment: bool = False,
) -> tuple[User, Trip, RawEmail, Segment, Document]:
    u = User(id=OWNER_USER_ID, email="al1@x.com", display_name="AL1")
    db.add(u)
    await db.flush()
    t = Trip(
        title="T",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 2),
    )
    db.add(t)
    await db.flush()
    re_ = RawEmail(
        to_address="oliver@trips.example.com",
        from_address="x@x.com",
        subject="bp",
        message_id=f"<{doc_filename}@test>",
        mime_blob=b"",
        headers={},
        parse_status="parsed",
    )
    db.add(re_)
    await db.flush()
    s = Segment(
        trip_id=t.id,
        owner_user_id=u.id,
        type="flight",
        status="confirmed",
        confirmation_number=seg_conf,
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
        parse_source="manual",
        parse_confidence=1.0,
        raw_email_id=re_.id,
    )
    db.add(s)
    await db.flush()
    d = Document(
        owner_user_id=u.id,
        raw_email_id=re_.id,
        segment_id=s.id if pre_linked_segment else None,
        trip_id=t.id if pre_linked_segment else None,
        filename=doc_filename,
        mime_type="application/pdf",
        size_bytes=10,
        sha256="e" * 64,
        storage_key="ee/" + "e" * 64,
    )
    db.add(d)
    await db.commit()
    return u, t, re_, s, d


@pytest.mark.asyncio
async def test_autolink_pending_for_email_links_via_confirmation_number(
    db_session: AsyncSession,
) -> None:
    _, t, re_, s, d = await _seed_email_seg_doc(
        db_session,
        doc_filename="Itinerary_K8YH3M_2026.pdf",
        seg_conf="K8YH3M",
    )
    await autolink_pending_for_email(db_session, raw_email_id=re_.id)
    await db_session.refresh(d)
    assert d.segment_id == s.id
    assert d.trip_id == t.id


@pytest.mark.asyncio
async def test_autolink_skips_already_linked_documents(
    db_session: AsyncSession,
) -> None:
    """Manual /documents/{id}/link must be preserved — heuristic only touches
    docs with segment_id IS NULL.
    """
    _, _, re_, s, d = await _seed_email_seg_doc(
        db_session,
        doc_filename="Itinerary_K8YH3M_2026.pdf",
        seg_conf="K8YH3M",
        pre_linked_segment=True,  # already linked
    )
    # Sanity: pre-link state
    assert d.segment_id == s.id
    # Mutate the segment's confirmation_number after seed so we'd verify
    # the heuristic isn't running over it
    s.confirmation_number = "OTHER_CONF"
    db_session.add(s)
    await db_session.commit()

    await autolink_pending_for_email(db_session, raw_email_id=re_.id)
    await db_session.refresh(d)
    # Still linked to the original segment — autolink didn't touch it.
    assert d.segment_id == s.id


@pytest.mark.asyncio
async def test_autolink_no_op_when_no_pending_docs(
    db_session: AsyncSession,
) -> None:
    """No Document with this raw_email_id → helper returns immediately."""
    re_ = RawEmail(
        to_address="x",
        from_address="x",
        subject="x",
        message_id="<empty@test>",
        mime_blob=b"",
        headers={},
        parse_status="parsed",
    )
    db_session.add(re_)
    await db_session.commit()
    # Just shouldn't raise:
    await autolink_pending_for_email(db_session, raw_email_id=re_.id)


@pytest.mark.asyncio
async def test_autolink_no_op_when_no_segments(
    db_session: AsyncSession,
) -> None:
    """No Segment with this raw_email_id → helper returns immediately."""
    u = User(id=OWNER_USER_ID, email="al2@x.com", display_name="AL2")
    db_session.add(u)
    await db_session.flush()
    re_ = RawEmail(
        to_address="x",
        from_address="x",
        subject="x",
        message_id="<noseg@test>",
        mime_blob=b"",
        headers={},
        parse_status="parsed",
    )
    db_session.add(re_)
    await db_session.flush()
    d = Document(
        owner_user_id=u.id,
        raw_email_id=re_.id,
        segment_id=None,
        trip_id=None,
        filename="something.pdf",
        mime_type="application/pdf",
        size_bytes=10,
        sha256="e" * 64,
        storage_key="ee/" + "e" * 64,
    )
    db_session.add(d)
    await db_session.commit()
    # Should not error; docs should remain unchanged
    await autolink_pending_for_email(db_session, raw_email_id=re_.id)
    await db_session.refresh(d)
    assert d.segment_id is None
    assert d.trip_id is None
