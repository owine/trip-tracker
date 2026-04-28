"""WebhookReplay: 24h replay-protection cache for ingest webhook."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.models.base import Base


class WebhookReplay(Base):
    __tablename__ = "webhook_replay_cache"

    ts_seconds: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    # No `index=True` — same rule as RawEmail/Segment: the Alembic migration
    # owns `ix_*` index creation. The composite PK already covers (ts_seconds,
    # nonce) lookups; the explicit `ix_webhook_replay_cache_expires_at` exists
    # only to make the prune query fast.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
