"""GlitchTip (Sentry-protocol) error reporting.

Every test that needs an active client uses the `sentry_events` fixture, which
initialises the SDK through `init_sentry` and then swaps the client's transport
for an in-memory one. Nothing here touches the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sentry_sdk
import structlog
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from trip_tracker.config import Settings, WorkerSettings
from trip_tracker.logging_setup import configure_logging
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.user import User
from trip_tracker.parsers.base import ParseResult
from trip_tracker.sentry_setup import init_sentry, message_id_hash, scrub_breadcrumb, scrub_event

_DSN = "https://public@glitchtip.example.test/1"

# Markers that must never reach GlitchTip. They sit in the email body, the
# sender, and the Message-ID of the fixture below.
_BODY_MARKER = "CONFIRMATION-XK7Q2P"
_SENDER = "bookings@airline-marker.example"
_MESSAGE_ID = "<marker-message-id@airline-marker.example>"

_MIME = (
    f"Subject: Your booking\r\n"
    f"From: {_SENDER}\r\n"
    f"To: oliver@trips.example.com\r\n"
    f"Message-ID: {_MESSAGE_ID}\r\n"
    f"Content-Type: text/plain\r\n"
    f"\r\n"
    f"Your confirmation number is {_BODY_MARKER}.\r\n"
).encode()


class _CapturingTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(dict(event))


def _deactivate_sentry() -> None:
    client = sentry_sdk.get_client()
    if client.is_active():
        client.close()
    sentry_sdk.get_global_scope().set_client(None)
    sentry_sdk.get_global_scope().clear()
    sentry_sdk.get_isolation_scope().clear()
    sentry_sdk.get_current_scope().clear()


@pytest.fixture
def sentry_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    monkeypatch.setenv("SENTRY_DSN", _DSN)
    _deactivate_sentry()
    assert init_sentry(WorkerSettings(), component="test") is True
    transport = _CapturingTransport()
    sentry_sdk.get_client().transport = transport
    yield transport.events
    _deactivate_sentry()


# ---------------------------------------------------------------------------
# init_sentry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dsn", [None, "", "   "])
def test_init_is_noop_without_dsn(monkeypatch: pytest.MonkeyPatch, dsn: str | None) -> None:
    _deactivate_sentry()
    if dsn is None:
        monkeypatch.delenv("SENTRY_DSN", raising=False)
    else:
        monkeypatch.setenv("SENTRY_DSN", dsn)

    with patch("sentry_sdk.init") as sdk_init:
        assert init_sentry(WorkerSettings(), component="app") is False

    sdk_init.assert_not_called()
    assert sentry_sdk.get_client().is_active() is False


def test_init_configures_errors_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _deactivate_sentry()
    monkeypatch.setenv("SENTRY_DSN", _DSN)
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "staging")
    monkeypatch.setattr("trip_tracker.build_info.GIT_SHA", "0123456789abcdef")
    try:
        assert init_sentry(WorkerSettings(), component="worker") is True
        client = sentry_sdk.get_client()
        opts = client.options
        assert opts["release"] == "0123456789abcdef"
        assert opts["environment"] == "staging"
        assert opts["traces_sample_rate"] == 0
        assert opts["send_default_pii"] is False
        assert opts["include_local_variables"] is False
        assert opts["max_request_body_size"] == "never"
        assert opts["auto_session_tracking"] is False  # GlitchTip has no sessions
        assert opts["profiles_sample_rate"] is None
        assert opts["profile_session_sample_rate"] is None
        assert opts["enable_logs"] is False
        assert "anthropic" not in client.integrations
        assert sentry_sdk.get_global_scope()._tags["component"] == "worker"
    finally:
        _deactivate_sentry()


def test_init_leaves_release_unset_without_git_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    _deactivate_sentry()
    monkeypatch.setenv("SENTRY_DSN", _DSN)
    monkeypatch.setattr("trip_tracker.build_info.GIT_SHA", "unknown")
    captured: dict[str, Any] = {}
    with patch("sentry_sdk.init", side_effect=lambda **kw: captured.update(kw)):
        init_sentry(WorkerSettings(), component="app")
    assert captured["release"] is None


def test_create_app_initialises_sentry() -> None:
    from trip_tracker.app import create_app

    with patch("trip_tracker.app.init_sentry") as init:
        create_app(settings=Settings())
    init.assert_called_once()
    assert init.call_args.kwargs["component"] == "app"


@pytest.mark.asyncio
async def test_worker_startup_initialises_sentry() -> None:
    from trip_tracker.worker import startup

    ctx: dict[str, Any] = {}
    with (
        patch("trip_tracker.worker.init_sentry") as init,
        patch("trip_tracker.worker.create_async_engine"),
        patch("trip_tracker.worker.AsyncRedis"),
    ):
        await startup(ctx)
    init.assert_called_once()
    assert init.call_args.kwargs["component"] == "worker"


# ---------------------------------------------------------------------------
# Scrubbers
# ---------------------------------------------------------------------------


def test_message_id_hash_is_stable_and_opaque() -> None:
    h = message_id_hash(_MESSAGE_ID)
    assert h == message_id_hash(_MESSAGE_ID)
    assert len(h) == 16
    assert "marker" not in h
    assert message_id_hash(None) == "none"


def test_scrubber_strips_email_content() -> None:
    event: dict[str, Any] = {
        "request": {
            "url": "https://trips.example.com/api/ingest/forwardemail",
            "method": "POST",
            "query_string": "token=relay-secret",
            "data": {"raw": _MIME.decode(), "text": _BODY_MARKER},
            "cookies": {"tt_session": "abc"},
            "headers": {"content-type": "application/json"},
        },
        "exception": {
            "values": [
                {
                    "type": "IntegrityError",
                    "value": (
                        "duplicate key value violates unique constraint\n"
                        "[SQL: INSERT INTO segments (confirmation_number) VALUES ($1)]\n"
                        f"[parameters: ('{_BODY_MARKER}',)]\n"
                        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
                    ),
                    "stacktrace": {
                        "frames": [
                            {"function": "parse_raw_email", "vars": {"body": _MIME.decode()}},
                        ]
                    },
                }
            ]
        },
        "extra": {
            "raw_email_id": "11111111-1111-1111-1111-111111111111",
            "parser": "united",
            "from_address": _SENDER,
            "to_address": "oliver@trips.example.com",
            "subject": "Your booking",
            "body": _BODY_MARKER,
            "confirmation_number": _BODY_MARKER,
        },
    }

    out = scrub_event(event, {})

    assert out is not None
    dumped = json.dumps(out)
    assert _BODY_MARKER not in dumped
    assert _SENDER not in dumped
    assert "relay-secret" not in dumped
    assert "tt_session" not in dumped
    # Reproduction context survives.
    assert out["extra"]["raw_email_id"] == "11111111-1111-1111-1111-111111111111"
    assert out["extra"]["parser"] == "united"
    assert out["request"]["method"] == "POST"
    assert "[SQL: INSERT INTO segments" in out["exception"]["values"][0]["value"]


def test_scrubber_redacts_ics_token_in_url() -> None:
    event: dict[str, Any] = {"request": {"url": "https://trips.example.com/ics/s3cr3t-token.ics"}}
    out = scrub_event(event, {})
    assert out is not None
    assert "s3cr3t-token" not in out["request"]["url"]
    assert out["request"]["url"].endswith("/ics/[Filtered].ics")


def test_breadcrumb_keeps_log_template_not_interpolated_values() -> None:
    record = logging.LogRecord(
        name="trip_tracker.parsers.dispatch",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="vendor %s raised: %s",
        args=("united", f"bad date in {_BODY_MARKER}"),
        exc_info=None,
    )
    crumb = {
        "type": "log",
        "category": record.name,
        "level": "warning",
        "message": record.getMessage(),
        "data": {"leak": _BODY_MARKER},
    }
    out = scrub_breadcrumb(crumb, {"log_record": record})
    assert out is not None
    assert out["message"] == "vendor %s raised: %s"
    assert _BODY_MARKER not in json.dumps(out)


# ---------------------------------------------------------------------------
# structlog → GlitchTip
# ---------------------------------------------------------------------------


def test_structlog_error_becomes_event(
    sentry_events: list[dict[str, Any]], capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="INFO", format="json")
    structlog.get_logger("t").error("ingest_failed", raw_email_id="abc", from_address=_SENDER)

    assert len(sentry_events) == 1
    event = sentry_events[0]
    assert event["level"] == "error"
    assert event["message"] == "ingest_failed"
    assert event["extra"]["raw_email_id"] == "abc"
    assert _SENDER not in json.dumps(event)
    # stdout JSON line is unchanged by the Sentry processor.
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "ingest_failed"
    assert line["from_address"] == _SENDER
    assert set(line) == {"event", "raw_email_id", "from_address", "level", "timestamp"}


def test_structlog_exception_carries_traceback(sentry_events: list[dict[str, Any]]) -> None:
    configure_logging(level="INFO", format="json")
    try:
        raise ValueError("boom")
    except ValueError:
        structlog.get_logger("t").exception("parse_crashed", raw_email_id="abc")

    assert len(sentry_events) == 1
    exc = sentry_events[0]["exception"]["values"][-1]
    assert exc["type"] == "ValueError"
    assert exc["value"] == "boom"


def test_structlog_warning_is_breadcrumb_not_event(sentry_events: list[dict[str, Any]]) -> None:
    configure_logging(level="INFO", format="json")
    structlog.get_logger("t").warning("slow_thing", from_address=_SENDER)
    assert sentry_events == []

    sentry_sdk.capture_message("probe")
    crumbs = sentry_events[0]["breadcrumbs"]["values"]
    assert any(c["message"] == "slow_thing" for c in crumbs)
    assert _SENDER not in json.dumps(crumbs)


def test_structlog_output_unchanged_without_dsn(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _deactivate_sentry()
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    init_sentry(WorkerSettings(), component="app")
    configure_logging(level="INFO", format="json")
    structlog.get_logger("t").error("boom", k="v")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert set(line) == {"event", "k", "level", "timestamp"}


# ---------------------------------------------------------------------------
# Worker: parse failures
# ---------------------------------------------------------------------------


async def _seed_raw_email(db_session: AsyncSession) -> RawEmail:
    user = User(oidc_subject=f"s-{uuid.uuid4()}", email="s@x.com", display_name="S")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address=_SENDER,
        subject="Your booking",
        message_id=_MESSAGE_ID,
        mime_blob=_MIME,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw)
    await db_session.commit()
    return raw


def _assert_no_email_content(event: dict[str, Any]) -> None:
    dumped = json.dumps(event)
    assert _BODY_MARKER not in dumped
    assert _SENDER not in dumped
    assert _MESSAGE_ID not in dumped


@pytest.mark.asyncio
async def test_parse_failure_reports_exactly_one_event(
    db_url: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    sentry_events: list[dict[str, Any]],
) -> None:
    from trip_tracker.parsers.dispatch import ParseOutcome
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    raw = await _seed_raw_email(db_session)

    outcome = ParseOutcome(
        result=ParseResult(segments=[], confidence=0.0, source="none"), budget_skipped=True
    )
    engine = create_async_engine(db_url)
    with patch("trip_tracker.worker.dispatch_parse", new=AsyncMock(return_value=outcome)):
        await parse_raw_email({"settings": Settings(), "engine": engine}, raw_email_id=str(raw.id))
    await engine.dispose()

    await db_session.refresh(raw)
    assert raw.parse_status == "no_segments"
    assert len(sentry_events) == 1
    event = sentry_events[0]
    assert event["level"] == "error"
    assert event["tags"]["raw_email_id"] == str(raw.id)
    assert event["tags"]["parser"] == "none"
    assert event["tags"]["message_id_sha256"] == message_id_hash(_MESSAGE_ID)
    assert event["tags"]["budget_skipped"] == "true"
    assert event["fingerprint"] == ["parse-failed", "none"]
    _assert_no_email_content(event)


@pytest.mark.asyncio
async def test_parse_exception_reports_exactly_one_event(
    db_url: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    sentry_events: list[dict[str, Any]],
) -> None:
    """A crash inside the task is reported once, even though saq then logs it
    again with `logger.exception` from its own (parent) task."""
    from trip_tracker.worker import parse_raw_email

    monkeypatch.setenv("DATABASE_URL", db_url)
    raw = await _seed_raw_email(db_session)

    engine = create_async_engine(db_url)
    boom = AsyncMock(side_effect=RuntimeError("dispatch exploded"))
    with patch("trip_tracker.worker.dispatch_parse", new=boom):
        # Mirror saq.worker.Worker.process: job runs in a child task, and the
        # parent logs the failure via the stdlib `saq` logger.
        task = asyncio.create_task(
            parse_raw_email({"settings": Settings(), "engine": engine}, raw_email_id=str(raw.id))
        )
        try:
            await task
        except RuntimeError:
            logging.getLogger("saq.worker").exception("Error processing job %s", "job")
    await engine.dispose()

    assert len(sentry_events) == 1
    event = sentry_events[0]
    assert event["exception"]["values"][-1]["value"] == "dispatch exploded"
    assert event["tags"]["raw_email_id"] == str(raw.id)
    assert event["tags"]["message_id_sha256"] == message_id_hash(_MESSAGE_ID)
    _assert_no_email_content(event)


@pytest.mark.asyncio
async def test_doc_enqueue_redis_blip_is_breadcrumb_not_event(
    monkeypatch: pytest.MonkeyPatch, sentry_events: list[dict[str, Any]]
) -> None:
    from trip_tracker.worker import _enqueue_doc_extracts

    failing_queue = MagicMock()
    failing_queue.enqueue = AsyncMock(side_effect=ConnectionError("redis down"))
    failing_queue.disconnect = AsyncMock()
    monkeypatch.setattr("trip_tracker.worker._build_doc_queue", lambda s: failing_queue)

    await _enqueue_doc_extracts(WorkerSettings(), [uuid.uuid4()])
    assert sentry_events == []

    sentry_sdk.capture_message("probe")
    crumbs = sentry_events[0]["breadcrumbs"]["values"]
    assert any(c["message"] == "_enqueue_doc_extracts failed: %s" for c in crumbs)
