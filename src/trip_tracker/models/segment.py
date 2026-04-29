"""Segment: a single trip leg (flight, lodging, car, etc.). Spec §3."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.models.base import Base

_SEARCH_TEXT_EXPR = (
    "(to_tsvector('simple'::regconfig, "  # ← opening paren added
    "coalesce(provider, '') || ' ' || "
    "coalesce(confirmation_number, '') || ' ' || "
    "coalesce(start_location ->> 'name', '') || ' ' || "
    "coalesce(end_location   ->> 'name', '') || ' ' || "
    "coalesce(start_location ->> 'city', '') || ' ' || "
    "coalesce(end_location   ->> 'city', ''))"
    ")"  # ← closing paren added
)


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (
        CheckConstraint(
            "type IN ('flight', 'lodging', 'car', 'train', 'transfer', 'activity')",
            name="ck_segments_type",
        ),
        CheckConstraint(
            "status IN ('confirmed', 'cancelled', 'tentative')",
            name="ck_segments_status",
        ),
        CheckConstraint(
            "parse_confidence >= 0 AND parse_confidence <= 1",
            name="ck_segments_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # NO `index=True` on these columns — see RawEmail comment. Alembic owns
    # all `ix_*` index creation; ORM-level index=True would collide.
    trip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="confirmed")
    confirmation_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    start_tz: Mapped[str] = mapped_column(String(64), nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_tz: Mapped[str | None] = mapped_column(String(64), nullable=True)
    start_location: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    end_location: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    parse_source: Mapped[str] = mapped_column(String(64), nullable=False)
    parse_confidence: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    # `Mapped[str | None]`: SQLAlchemy's TSVECTOR adapter round-trips as text.
    # Nullable in the ORM even though Postgres always populates it, because at
    # INSERT-time the Python side has no value to send and the generated value
    # is only visible after a refresh. Tests use raw SQL to read this column.
    search_text: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(_SEARCH_TEXT_EXPR, persisted=True), nullable=True
    )
    raw_email_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("raw_emails.id", ondelete="SET NULL"),
        nullable=True,
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("segments.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
