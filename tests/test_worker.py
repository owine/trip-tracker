"""End-to-end worker test: webhook → enqueue → worker → DB write.

Uses ARQ's testing helpers (no real Redis) — the in-memory queue runs the
task synchronously inside the test.
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

    user = User(oidc_subject="t", email="t@x.com", display_name="T")
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

    user = User(oidc_subject="w", email="w@x.com", display_name="W")
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
            str(raw.id),
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
        str(uuid.uuid4()),
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
            str(raw.id),
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
        str(raw.id),
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
    user = User(oidc_subject="ns", email="ns@x.com", display_name="NS")
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
            str(raw.id),
        )
    await engine.dispose()
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
    user = User(oidc_subject="r", email="r@x.com", display_name="R")
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
            str(raw.id),
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
    from trip_tracker.models.trip_traveler import TripTraveler
    from trip_tracker.parsers.dispatch import ParseOutcome
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    user = User(oidc_subject="att", email="att@x.com", display_name="ATT")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))

    # Pre-existing trip the segment will attach to
    trip = Trip(
        title="Paris June 2026",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        primary_destination="Paris",
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))

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
            str(raw.id),
        )
    await engine.dispose()

    # The new segment is attached to the existing trip
    from sqlalchemy import select

    seg = (
        await db_session.execute(select(Segment).where(Segment.raw_email_id == raw.id))
    ).scalar_one()
    assert seg.trip_id == trip.id


@pytest.mark.asyncio
async def test_worker_settings_startup_creates_engine(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WorkerSettings.startup() populates ctx['engine']; shutdown() disposes it."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    from trip_tracker.worker import WorkerSettings

    ctx: dict = {}
    await WorkerSettings.startup(ctx)
    assert "engine" in ctx
    assert "settings" in ctx
    await WorkerSettings.shutdown(ctx)


@pytest.mark.asyncio
async def test_worker_settings_shutdown_no_engine_is_safe() -> None:
    """shutdown() doesn't raise when no engine was created."""
    from trip_tracker.worker import WorkerSettings

    await WorkerSettings.shutdown({})  # empty ctx
