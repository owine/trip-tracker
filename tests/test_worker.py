"""End-to-end worker test: webhook → enqueue → worker → DB write.

Tests call `parse_raw_email` directly with a hand-built ctx dict (no real
Redis or saq queue runtime); the webhook test patches `enqueue_parse` to
verify the post-commit handoff.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.user import User
from trip_tracker.parsers.base import ParseResult, SegmentDraft

_FIXTURE_MIME = (
    b"Subject: Test\r\n"
    b"From: test@example.com\r\n"
    b"To: oliver@trips.example.com\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"Hello world\r\n"
)


@pytest.mark.asyncio
async def test_webhook_enqueues_parse_task(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POST /api/ingest/email enqueues parse_raw_email(id) after commit."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", "x" * 32)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    settings = Settings()

    user = User(email="t@x.com", display_name="T")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)

    sig = "sha256=" + hmac.new(b"x" * 32, _FIXTURE_MIME, hashlib.sha256).hexdigest()
    nonce = secrets.token_hex(16)

    with patch("trip_tracker.ingest.webhook.enqueue_parse") as mock_enqueue:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        ):
            r = await c.post(
                "/api/ingest/email",
                content=_FIXTURE_MIME,
                headers={
                    "X-Webhook-Signature": sig,
                    "X-Webhook-Nonce": nonce,
                    "X-Webhook-Timestamp": str(int(time.time())),
                    "Content-Type": "message/rfc822",
                },
            )

    assert r.status_code == 202
    mock_enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_parse_raw_email_writes_segment(
    db_url: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker task: given a RawEmail id, parse it and write a Segment."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.parsers.dispatch import ParseOutcome
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")

    user = User(email="w@x.com", display_name="W")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="confirmations@airexample.com",
        subject="Flight",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw)
    await db_session.commit()

    fake_outcome = ParseOutcome(
        result=ParseResult(
            segments=[
                SegmentDraft(
                    type="flight",
                    start_at=datetime(2026, 6, 1, tzinfo=UTC),
                    start_tz="UTC",
                    start_location={"city": "New York"},
                    end_location={"city": "Paris"},
                )
            ],
            confidence=0.9,
            source="rules:test",
        ),
    )

    fake_dispatch = AsyncMock(return_value=fake_outcome)
    engine = create_async_engine(db_url)

    with patch("trip_tracker.worker.dispatch_parse", new=fake_dispatch):
        await parse_raw_email(
            {"settings": Settings(), "engine": engine},
            raw_email_id=str(raw.id),
        )

    await engine.dispose()
    await db_session.refresh(raw)
    assert raw.parse_status == "parsed"


@pytest.mark.asyncio
async def test_parse_raw_email_missing_id_is_noop(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Unknown raw_email_id logs + returns without raising."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    # The db_session fixture initializes the schema; reuse db_url for the engine.
    engine = create_async_engine(db_url)
    await parse_raw_email(
        {"settings": Settings(), "engine": engine},
        raw_email_id=str(uuid.uuid4()),
    )
    await engine.dispose()


@pytest.mark.asyncio
async def test_parse_raw_email_already_parsed_skipped(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """A RawEmail with parse_status != 'pending' is skipped (idempotent re-run)."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="x@y.com",
        subject="t",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="parsed",  # already done
    )
    db_session.add(raw)
    await db_session.commit()

    fake_dispatch = AsyncMock()
    engine = create_async_engine(db_url)
    with patch("trip_tracker.worker.dispatch_parse", new=fake_dispatch):
        await parse_raw_email(
            {"settings": Settings(), "engine": engine},
            raw_email_id=str(raw.id),
        )
    await engine.dispose()
    fake_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_parse_raw_email_no_alias_marks_no_segments(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """RawEmail addressed to an unknown alias gets parse_status='no_segments'."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="unknown-alias@trips.example.com",
        from_address="x@y.com",
        subject="t",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw)
    await db_session.commit()

    engine = create_async_engine(db_url)
    await parse_raw_email(
        {"settings": Settings(), "engine": engine},
        raw_email_id=str(raw.id),
    )
    await engine.dispose()
    await db_session.refresh(raw)
    assert raw.parse_status == "no_segments"


@pytest.mark.asyncio
async def test_parse_raw_email_empty_segments_marks_no_segments(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Dispatcher returns segments=[] (high-confidence 'nothing here') → no_segments."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.parsers.dispatch import ParseOutcome
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    user = User(email="ns@x.com", display_name="NS")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="marketing@retailer.com",
        subject="Sale!",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw)
    await db_session.commit()

    fake_outcome = ParseOutcome(
        result=ParseResult(segments=[], confidence=0.9, source="rules:none"),
    )
    fake_dispatch = AsyncMock(return_value=fake_outcome)
    engine = create_async_engine(db_url)
    with patch("trip_tracker.worker.dispatch_parse", new=fake_dispatch):
        await parse_raw_email(
            {"settings": Settings(), "engine": engine},
            raw_email_id=str(raw.id),
        )
    await engine.dispose()
    await db_session.refresh(raw)
    assert raw.parse_status == "no_segments"


@pytest.mark.asyncio
async def test_parse_raw_email_with_pdf_attachment_but_no_segments_still_enqueues_extract(
    db_url: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    _mock_worker_doc_queue: object,  # noqa: PT019 — used to assert on enqueue calls
) -> None:
    """Email with PDF attached but unparseable body → no segments, but the
    boarding-pass PDF should still be persisted + extract_document enqueued.
    """
    from email.message import EmailMessage

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.models.document import Document
    from trip_tracker.parsers.dispatch import ParseOutcome
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)

    # Build a MIME body with a PDF attachment and unparseable text body
    msg = EmailMessage()
    msg["From"] = "airline@example.com"
    msg["To"] = "oliver@trips.example.com"
    msg["Subject"] = "Boarding Pass"
    msg["Message-ID"] = "<pdf-no-segments@test>"
    msg.set_content("Some random text the parser won't extract segments from.")
    pdf_payload = b"%PDF-1.4\nfake boarding pass content\n"
    msg.add_attachment(pdf_payload, maintype="application", subtype="pdf", filename="boarding.pdf")
    mime_blob = msg.as_bytes()

    user = User(email="ps@x.com", display_name="PS")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="airline@example.com",
        subject="Boarding Pass",
        message_id="<pdf-no-segments@test>",
        mime_blob=mime_blob,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw)
    await db_session.commit()

    # Mock dispatch_parse to return empty segments (no trip info extracted)
    fake_outcome = ParseOutcome(
        result=ParseResult(segments=[], confidence=1.0, source="manual"),
    )
    fake_dispatch = AsyncMock(return_value=fake_outcome)
    engine = create_async_engine(db_url)

    with patch("trip_tracker.worker.dispatch_parse", new=fake_dispatch):
        await parse_raw_email(
            {"settings": Settings(), "engine": engine},
            raw_email_id=str(raw.id),
        )

    await engine.dispose()

    # Verify the document was persisted with extract_status="pending"
    docs = (
        (await db_session.execute(select(Document).where(Document.raw_email_id == raw.id)))
        .scalars()
        .all()
    )
    assert len(docs) == 1
    assert docs[0].filename == "boarding.pdf"
    assert docs[0].extract_status == "pending"

    # Verify that enqueue was called for the document
    from unittest.mock import MagicMock

    mock_queue = _mock_worker_doc_queue  # type: ignore[name-defined]
    if isinstance(mock_queue, MagicMock):
        mock_queue.enqueue.assert_awaited()
        assert mock_queue.enqueue.await_count >= 1
        # Check that the call was for extract_document
        call_args = mock_queue.enqueue.call_args_list
        assert any(
            "extract_document" in str(call) and str(docs[0].id) in str(call) for call in call_args
        )

    # Verify parse_status was set to "no_segments"
    await db_session.refresh(raw)
    assert raw.parse_status == "no_segments"


@pytest.mark.asyncio
async def test_parse_raw_email_low_confidence_marks_review(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Dispatcher confidence < llm_confidence_floor → parse_status='review'."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.parsers.dispatch import ParseOutcome
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    user = User(email="r@x.com", display_name="R")
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
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw)
    await db_session.commit()

    fake_outcome = ParseOutcome(
        result=ParseResult(
            segments=[
                SegmentDraft(
                    type="flight",
                    start_at=datetime(2026, 6, 1, tzinfo=UTC),
                    start_tz="UTC",
                )
            ],
            confidence=0.5,  # below llm_confidence_floor=0.7
            source="llm:haiku-4-5",
        ),
        llm_input_tokens=1000,
        llm_output_tokens=200,
    )
    fake_dispatch = AsyncMock(return_value=fake_outcome)
    engine = create_async_engine(db_url)
    with patch("trip_tracker.worker.dispatch_parse", new=fake_dispatch):
        await parse_raw_email(
            {"settings": Settings(), "engine": engine},
            raw_email_id=str(raw.id),
        )
    await engine.dispose()
    await db_session.refresh(raw)
    assert raw.parse_status == "review"


@pytest.mark.asyncio
async def test_parse_raw_email_attaches_to_existing_trip(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """If clustering returns 'attach', segment is written with the existing trip_id."""
    from datetime import date

    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.models.segment import Segment
    from trip_tracker.models.trip import Trip
    from trip_tracker.parsers.dispatch import ParseOutcome
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    user = User(email="att@x.com", display_name="ATT")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))

    # Pre-existing trip the segment will attach to
    trip = Trip(
        title="Paris June 2026",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        primary_destination="Paris",
    )
    db_session.add(trip)

    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="x@y.com",
        subject="t",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw)
    await db_session.commit()

    fake_outcome = ParseOutcome(
        result=ParseResult(
            segments=[
                SegmentDraft(
                    type="flight",
                    start_at=datetime(2026, 6, 3, 9, tzinfo=UTC),
                    start_tz="UTC",
                    start_location={"city": "Paris"},
                    end_location={"city": "Paris"},
                )
            ],
            confidence=0.9,
            source="rules:test",
        ),
    )
    fake_dispatch = AsyncMock(return_value=fake_outcome)
    engine = create_async_engine(db_url)
    with patch("trip_tracker.worker.dispatch_parse", new=fake_dispatch):
        await parse_raw_email(
            {"settings": Settings(), "engine": engine},
            raw_email_id=str(raw.id),
        )
    await engine.dispose()

    # The new segment is attached to the existing trip
    from sqlalchemy import select

    seg = (
        await db_session.execute(select(Segment).where(Segment.raw_email_id == raw.id))
    ).scalar_one()
    assert seg.trip_id == trip.id


@pytest.mark.asyncio
async def test_parse_raw_email_ambiguous_decision_attaches_to_best_trip(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Ambiguous cluster decisions carry a trip_id (the best-scoring candidate)
    just like 'attach' does. The worker must use it, not fall back to None —
    Segment.trip_id is NOT NULL, and a None there used to crash the parse.
    Regression for the worker.py 'else: trip_id = None' bug present from
    project init through v0.8.0.
    """
    from datetime import date

    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.models.segment import Segment
    from trip_tracker.models.trip import Trip
    from trip_tracker.parsers.cluster import ClusterDecision
    from trip_tracker.parsers.dispatch import ParseOutcome
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    user = User(email="amb@x.com", display_name="AMB")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))

    trip = Trip(
        title="Paris June 2026",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        primary_destination="Paris",
    )
    db_session.add(trip)

    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="x@y.com",
        subject="t",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw)
    await db_session.commit()

    fake_outcome = ParseOutcome(
        result=ParseResult(
            segments=[
                SegmentDraft(
                    type="flight",
                    start_at=datetime(2026, 6, 3, 9, tzinfo=UTC),
                    start_tz="UTC",
                    start_location={"city": "Paris"},
                    end_location={"city": "Paris"},
                )
            ],
            confidence=0.9,
            source="rules:test",
        ),
    )
    fake_dispatch = AsyncMock(return_value=fake_outcome)
    fake_cluster = AsyncMock(return_value=ClusterDecision(kind="ambiguous", trip_id=trip.id))
    engine = create_async_engine(db_url)
    with (
        patch("trip_tracker.worker.dispatch_parse", new=fake_dispatch),
        patch("trip_tracker.worker.cluster_for_user", new=fake_cluster),
    ):
        await parse_raw_email(
            {"settings": Settings(), "engine": engine},
            raw_email_id=str(raw.id),
        )
    await engine.dispose()

    from sqlalchemy import select

    seg = (
        await db_session.execute(select(Segment).where(Segment.raw_email_id == raw.id))
    ).scalar_one()
    assert seg.trip_id == trip.id, (
        "ambiguous decisions must inherit decision.trip_id; previously the "
        "worker would set trip_id=None and the INSERT would fail under "
        "Segment.trip_id NOT NULL"
    )


@pytest.mark.asyncio
async def test_worker_startup_creates_engine(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """startup() populates ctx['engine'] and ctx['settings']."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    from trip_tracker.worker import shutdown, startup

    ctx: dict = {}
    await startup(ctx)
    assert "engine" in ctx
    assert "settings" in ctx
    await shutdown(ctx)


@pytest.mark.asyncio
async def test_worker_shutdown_no_engine_is_safe() -> None:
    """shutdown() doesn't raise when no engine was created."""
    from trip_tracker.worker import shutdown

    await shutdown({})  # empty ctx


# ---------------------------------------------------------------------------
# Phase 9 Track A — dedup gate (T-A4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_raw_email_all_drafts_dedup_marks_duplicate(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """All drafts match existing segments → status='duplicate', no new Segment,
    X-Tt-Dedup-Against header lists the matched segment ids."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.models.segment import Segment
    from trip_tracker.parsers.dispatch import ParseOutcome
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    from datetime import date

    from trip_tracker.models.trip import Trip

    user = User(email="dup1@x.com", display_name="DUP1")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))

    trip = Trip(
        title="Seed Trip",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        primary_destination="Paris",
    )
    db_session.add(trip)
    await db_session.flush()

    # Seed an existing Segment that the draft will match (strong match: same
    # confirmation_number + provider).
    seeded = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        confirmation_number="ABC123",
        provider="Air Example",
        start_at=datetime(2026, 6, 1, 10, tzinfo=UTC),
        start_tz="UTC",
        parse_source="rules:test",
        parse_confidence=0.9,
    )
    db_session.add(seeded)
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="confirmations@airexample.com",
        subject="Re-forward",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw)
    await db_session.commit()
    seeded_id = seeded.id

    fake_outcome = ParseOutcome(
        result=ParseResult(
            segments=[
                SegmentDraft(
                    type="flight",
                    confirmation_number="ABC123",
                    provider="Air Example",
                    start_at=datetime(2026, 6, 1, 10, tzinfo=UTC),
                    start_tz="UTC",
                )
            ],
            confidence=0.9,
            source="rules:test",
        ),
    )
    fake_dispatch = AsyncMock(return_value=fake_outcome)
    engine = create_async_engine(db_url)
    with patch("trip_tracker.worker.dispatch_parse", new=fake_dispatch):
        await parse_raw_email(
            {"settings": Settings(), "engine": engine},
            raw_email_id=str(raw.id),
        )
    await engine.dispose()

    await db_session.refresh(raw)
    assert raw.parse_status == "duplicate"
    assert raw.headers is not None
    assert "X-Tt-Dedup-Against" in raw.headers
    assert str(seeded_id) in raw.headers["X-Tt-Dedup-Against"]

    # No new Segment created (the seeded one is the only row).
    rows = (
        (await db_session.execute(select(Segment).where(Segment.owner_user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].id == seeded_id


@pytest.mark.asyncio
async def test_parse_raw_email_mixed_drafts_persists_fresh_only(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """One draft matches existing, one is new → status='review', exactly ONE
    new segment created, X-Tt-Dedup-Partial header records the matched draft."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.models.segment import Segment
    from trip_tracker.parsers.dispatch import ParseOutcome
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    from datetime import date

    from trip_tracker.models.trip import Trip

    user = User(email="dup2@x.com", display_name="DUP2")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))

    trip = Trip(
        title="Seed Trip 2",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 5),
        primary_destination="London",
    )
    db_session.add(trip)
    await db_session.flush()

    seeded = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        confirmation_number="DUP999",
        provider="Air Example",
        start_at=datetime(2026, 7, 1, 10, tzinfo=UTC),
        start_tz="UTC",
        parse_source="rules:test",
        parse_confidence=0.9,
    )
    db_session.add(seeded)
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="confirmations@airexample.com",
        subject="Itinerary",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw)
    await db_session.commit()
    seeded_id = seeded.id

    fake_outcome = ParseOutcome(
        result=ParseResult(
            segments=[
                # Will dedup against seed (strong match)
                SegmentDraft(
                    type="flight",
                    confirmation_number="DUP999",
                    provider="Air Example",
                    start_at=datetime(2026, 7, 1, 10, tzinfo=UTC),
                    start_tz="UTC",
                ),
                # Fresh — different confirmation #, no transit IATAs
                SegmentDraft(
                    type="flight",
                    confirmation_number="FRESH1",
                    provider="Air Example",
                    start_at=datetime(2026, 7, 5, 10, tzinfo=UTC),
                    start_tz="UTC",
                    start_location={"city": "NYC"},
                    end_location={"city": "LON"},
                ),
            ],
            confidence=0.9,
            source="rules:test",
        ),
    )
    fake_dispatch = AsyncMock(return_value=fake_outcome)
    engine = create_async_engine(db_url)
    with patch("trip_tracker.worker.dispatch_parse", new=fake_dispatch):
        await parse_raw_email(
            {"settings": Settings(), "engine": engine},
            raw_email_id=str(raw.id),
        )
    await engine.dispose()

    await db_session.refresh(raw)
    assert raw.parse_status == "review"
    assert raw.headers is not None
    assert "X-Tt-Dedup-Partial" in raw.headers
    partial = raw.headers["X-Tt-Dedup-Partial"]
    assert isinstance(partial, list)
    assert len(partial) == 1
    assert partial[0]["existing_id"] == str(seeded_id)
    assert partial[0]["draft_type"] == "flight"
    assert "draft_start_at" in partial[0]

    # Exactly one new segment persisted (the FRESH1 draft); seeded is unchanged.
    rows = (
        (await db_session.execute(select(Segment).where(Segment.owner_user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    new_rows = [r for r in rows if r.id != seeded_id]
    assert len(new_rows) == 1
    assert new_rows[0].confirmation_number == "FRESH1"
    assert new_rows[0].raw_email_id == raw.id


@pytest.mark.asyncio
async def test_parse_raw_email_all_fresh_unchanged_behavior(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Drafts that match nothing existing → behavior identical to pre-dedup
    (status='parsed', segment created, no dedup headers)."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.models.segment import Segment
    from trip_tracker.parsers.dispatch import ParseOutcome
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    user = User(email="fresh@x.com", display_name="FRESH")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="confirmations@airexample.com",
        subject="New trip",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw)
    await db_session.commit()

    fake_outcome = ParseOutcome(
        result=ParseResult(
            segments=[
                SegmentDraft(
                    type="flight",
                    confirmation_number="NEWONE",
                    provider="Air Example",
                    start_at=datetime(2026, 8, 1, tzinfo=UTC),
                    start_tz="UTC",
                    start_location={"city": "NYC"},
                    end_location={"city": "PAR"},
                )
            ],
            confidence=0.9,
            source="rules:test",
        ),
    )
    fake_dispatch = AsyncMock(return_value=fake_outcome)
    engine = create_async_engine(db_url)
    with patch("trip_tracker.worker.dispatch_parse", new=fake_dispatch):
        await parse_raw_email(
            {"settings": Settings(), "engine": engine},
            raw_email_id=str(raw.id),
        )
    await engine.dispose()

    await db_session.refresh(raw)
    assert raw.parse_status == "parsed"
    assert raw.headers is not None
    assert "X-Tt-Dedup-Against" not in raw.headers
    assert "X-Tt-Dedup-Partial" not in raw.headers

    rows = (
        (await db_session.execute(select(Segment).where(Segment.owner_user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].confirmation_number == "NEWONE"


@pytest.mark.asyncio
async def test_parse_raw_email_reforward_same_confirmation_dedupes(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Re-forwarding the same confirmation creates only the first Segment;
    second RawEmail ends in 'duplicate' state. End: 1 Segment, 2 RawEmail."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine

    from trip_tracker.models.segment import Segment
    from trip_tracker.parsers.dispatch import ParseOutcome
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    user = User(email="rfwd@x.com", display_name="RFWD")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))

    raw1 = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="confirmations@airexample.com",
        subject="Itinerary",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="pending",
    )
    raw2 = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="confirmations@airexample.com",
        subject="Fwd: Itinerary",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw1)
    db_session.add(raw2)
    await db_session.commit()

    fake_outcome = ParseOutcome(
        result=ParseResult(
            segments=[
                SegmentDraft(
                    type="flight",
                    confirmation_number="RFW001",
                    provider="Air Example",
                    start_at=datetime(2026, 9, 1, tzinfo=UTC),
                    start_tz="UTC",
                    start_location={"iata": "JFK"},
                    end_location={"iata": "CDG"},
                )
            ],
            confidence=0.9,
            source="rules:test",
        ),
    )
    fake_dispatch = AsyncMock(return_value=fake_outcome)
    engine = create_async_engine(db_url)
    with patch("trip_tracker.worker.dispatch_parse", new=fake_dispatch):
        await parse_raw_email(
            {"settings": Settings(), "engine": engine},
            raw_email_id=str(raw1.id),
        )
        await parse_raw_email(
            {"settings": Settings(), "engine": engine},
            raw_email_id=str(raw2.id),
        )
    await engine.dispose()

    await db_session.refresh(raw1)
    await db_session.refresh(raw2)
    assert raw1.parse_status == "parsed"
    assert raw2.parse_status == "duplicate"

    rows = (
        (await db_session.execute(select(Segment).where(Segment.owner_user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].raw_email_id == raw1.id
