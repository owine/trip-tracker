"""Webhook ingest end-to-end. Spec §5."""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.models.raw_email import RawEmail

FIXTURE = Path(__file__).parent / "fixtures" / "webhooks" / "sample.eml"
SECRET = "x" * 32


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _headers(body: bytes, *, ts: int | None = None, nonce: str = "n1") -> dict[str, str]:
    return {
        "Content-Type": "message/rfc822",
        "X-Webhook-Signature": _sig(body),
        "X-Webhook-Timestamp": str(ts if ts is not None else int(time.time())),
        "X-Webhook-Nonce": nonce,
    }


async def _post(app, body: bytes, headers: dict[str, str], db_url: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        return await c.post("/api/ingest/email", content=body, headers=headers)


@pytest.mark.asyncio
async def test_happy_path_persists_raw_email(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    r = await _post(app, body, _headers(body, nonce="happy"), db_url)
    assert r.status_code == 202

    rows = (await db_session.execute(select(RawEmail))).scalars().all()
    assert len(rows) == 1
    assert rows[0].message_id == "<abc123-confirm@delta.com>"
    assert rows[0].mime_blob == body


@pytest.mark.asyncio
async def test_hmac_missing_returns_401(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, nonce="x")
    h.pop("X-Webhook-Signature")
    r = await _post(app, body, h, db_url)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_hmac_no_prefix_returns_401(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, nonce="x")
    h["X-Webhook-Signature"] = h["X-Webhook-Signature"].removeprefix("sha256=")
    r = await _post(app, body, h, db_url)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_body_too_big_returns_413(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("WEBHOOK_MAX_BODY_BYTES", "1024")  # tiny limit
    app = create_app()
    body = b"x" * 2048
    r = await _post(app, body, _headers(body, nonce="big"), db_url)
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_timestamp_skew_returns_400(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, ts=int(time.time()) - 10_000, nonce="old")
    r = await _post(app, body, h, db_url)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_replay_returns_202_silently(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, nonce="rep")
    r1 = await _post(app, body, h, db_url)
    assert r1.status_code == 202
    r2 = await _post(app, body, h, db_url)
    assert r2.status_code == 202  # silent — not 200, not 409
    # Still only one row
    n = await db_session.execute(select(func.count()).select_from(RawEmail))
    assert n.scalar_one() == 1


@pytest.mark.asyncio
async def test_duplicate_message_id_returns_202_silently(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    r1 = await _post(app, body, _headers(body, nonce="dup-a"), db_url)
    assert r1.status_code == 202
    r2 = await _post(app, body, _headers(body, nonce="dup-b"), db_url)
    assert r2.status_code == 202
    n = await db_session.execute(select(func.count()).select_from(RawEmail))
    assert n.scalar_one() == 1


@pytest.mark.asyncio
async def test_missing_nonce_returns_400(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, nonce="x")
    h.pop("X-Webhook-Nonce")
    r = await _post(app, body, h, db_url)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_oversized_nonce_returns_400(db_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, nonce="x" * 65)  # 65 > max 64
    r = await _post(app, body, h, db_url)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_non_integer_timestamp_returns_400(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, nonce="x")
    h["X-Webhook-Timestamp"] = "not-a-number"
    r = await _post(app, body, h, db_url)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_unknown_alias_still_persists(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Spec §5: unknown alias = persist anyway, parse_status='pending'.
    Owner is derived lazily via JOIN at /admin/raw-emails query time.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()  # to: oliver@trips.example.com — no alias for "oliver" yet
    r = await _post(app, body, _headers(body, nonce="orphan"), db_url)
    assert r.status_code == 202

    re = (await db_session.execute(select(RawEmail))).scalar_one()
    assert re.to_address == "oliver@trips.example.com"
    assert re.parse_status == "pending"


@pytest.mark.asyncio
async def test_missing_message_id_synthesizes(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Spec §5 step 6: missing Message-ID → synthesize <sha256:...@trip-tracker.local>."""
    import hashlib as _hashlib

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes().replace(b"Message-ID: <abc123-confirm@delta.com>\r\n", b"")
    expected_hex = _hashlib.sha256(body).hexdigest()
    r = await _post(app, body, _headers(body, nonce="synth"), db_url)
    assert r.status_code == 202

    re = (await db_session.execute(select(RawEmail))).scalar_one()
    assert re.message_id == f"<sha256:{expected_hex}@trip-tracker.local>"


@pytest.mark.asyncio
async def test_crlf_bom_body_round_trips_hmac(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """A BOM-prefixed CRLF body must HMAC-verify against the exact bytes sent.

    Validates that no middleware (uvicorn/ASGITransport) silently rewrites the
    request body. If this test fails, our HMAC math is computed over different
    bytes than the server sees.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = b"\xef\xbb\xbf" + FIXTURE.read_bytes()  # UTF-8 BOM prefix
    r = await _post(app, body, _headers(body, nonce="bom"), db_url)
    assert r.status_code == 202


@pytest.fixture(autouse=True)
def _reset_prune_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The webhook module owns a process-wide PruneGate singleton; reset it
    per-test so prune-frequency is deterministic and one test's prune doesn't
    silence the next test's expected prune.
    """
    from trip_tracker.ingest import webhook as wh

    monkeypatch.setattr(wh, "_PRUNE_GATE", wh.PruneGate(interval_seconds=60.0))
