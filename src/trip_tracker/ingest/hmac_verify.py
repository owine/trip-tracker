"""HMAC verification + replay-cache primitives. Spec §5 steps 2, 4, 5."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.webhook_replay import WebhookReplay


def verify_signature(body: bytes, header_value: str, secret: bytes) -> bool:
    """Constant-time HMAC-SHA256 verification.

    Header MUST be of form ``sha256=<64 hex chars>``. Returns False for any
    deviation (missing/empty header, missing prefix, mismatched digest).
    """
    if not header_value or not header_value.startswith("sha256="):
        return False
    provided_hex = header_value.removeprefix("sha256=")
    expected_hex = hmac.new(secret, body, hashlib.sha256).hexdigest()
    # compare_digest requires equal-length strings; both are 64 hex chars
    if len(provided_hex) != len(expected_hex):
        return False
    return hmac.compare_digest(provided_hex, expected_hex)


async def record_nonce(session: AsyncSession, *, ts_seconds: int, nonce: str) -> bool:
    """Insert ``(ts_seconds, nonce)`` into webhook_replay_cache.

    Returns True if newly recorded, False if PK conflict (replay seen before).
    Caller is responsible for the surrounding transaction; this function does
    NOT commit. ``expires_at`` is set to now + 24h via SQL ``now()``.
    """
    stmt = (
        pg_insert(WebhookReplay)
        .values(
            ts_seconds=ts_seconds,
            nonce=nonce,
            expires_at=text("now() + interval '24 hours'"),
        )
        .on_conflict_do_nothing(index_elements=["ts_seconds", "nonce"])
    )
    result: CursorResult[tuple[()]] = await session.execute(stmt)  # type: ignore[assignment]
    return result.rowcount == 1


async def prune_replay_cache(session: AsyncSession) -> int:
    """Delete rows past ``expires_at``. Returns rows deleted."""
    result: CursorResult[tuple[()]] = await session.execute(  # type: ignore[assignment]
        delete(WebhookReplay).where(WebhookReplay.expires_at < text("now()"))
    )
    return result.rowcount or 0


@dataclass
class PruneGate:
    """Process-local 60s gate for opportunistic replay-cache pruning.

    Multi-worker uvicorn deploys get 1 prune per worker per minute, which is
    fine — pruning is hygiene, not correctness.
    """

    interval_seconds: float = 60.0
    _last: float = field(default=float("-inf"), init=False, repr=False)

    def should_prune(self, *, now: float | None = None) -> bool:
        t = now if now is not None else time.monotonic()
        if t - self._last >= self.interval_seconds:
            self._last = t
            return True
        return False
