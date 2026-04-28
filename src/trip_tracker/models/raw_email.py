"""RawEmail: stored MIME bytes for an incoming forwarded confirmation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, LargeBinary, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.models.base import Base


class RawEmail(Base):
    __tablename__ = "raw_emails"
    __table_args__ = (
        CheckConstraint(
            "parse_status IN ('pending', 'parsed', 'failed', 'no_segments', 'review')",
            name="ck_raw_emails_parse_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Note: NO `index=True` on these columns — the Alembic migration creates
    # the indexes with explicit names. Setting `index=True` here would cause
    # SQLAlchemy's metadata naming convention (from models/base.py) to also
    # auto-create an index, colliding with the migration on the same column.
    # `unique=True` on message_id is fine because Alembic creates the unique
    # *constraint* (named `uq_raw_emails_message_id`) which Postgres backs
    # with a btree — different name space from `ix_*` indexes.
    to_address: Mapped[str] = mapped_column(String(320), nullable=False)
    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_id: Mapped[str] = mapped_column(String(998), unique=True, nullable=False)
    mime_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    parse_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
