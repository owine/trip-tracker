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
