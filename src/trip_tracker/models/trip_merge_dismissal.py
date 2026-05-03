"""Dismissed trip-merge suggestions, per-user, per-pair."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.models.base import Base


class TripMergeDismissal(Base):
    __tablename__ = "trip_merge_dismissals"

    # Composite PK on (user_id, trip_a_id, trip_b_id) matches the migration's
    # pk_trip_merge_dismissals. Pair-uniqueness across LEAST/GREATEST lives
    # only in the DB-side expression UNIQUE INDEX (uq_trip_merge_dismissals_pair),
    # not in the ORM — C3 will issue the unique-aware INSERT via raw SQL.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    trip_a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        primary_key=True,
    )
    trip_b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        primary_key=True,
    )
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
