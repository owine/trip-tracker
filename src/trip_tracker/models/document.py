"""Document ORM. Spec §4.1."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trip_tracker.models.base import Base

if TYPE_CHECKING:
    from trip_tracker.models.raw_email import RawEmail
    from trip_tracker.models.segment import Segment
    from trip_tracker.models.trip import Trip
    from trip_tracker.models.user import User


class Document(Base):
    """Stored file (text PDF in v0.5.0) + extracted text."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "sha256", name="uq_documents_owner_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trips.id", ondelete="CASCADE"), nullable=True
    )
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("segments.id", ondelete="SET NULL"),
        nullable=True,
    )
    raw_email_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("raw_emails.id", ondelete="SET NULL"),
        nullable=True,
    )
    filename: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(String, nullable=True)
    extract_status: Mapped[str] = mapped_column(
        String, nullable=False, default="pending", server_default="pending"
    )
    extract_method: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    owner: Mapped[User] = relationship(back_populates=None, lazy="raise")
    trip: Mapped[Trip | None] = relationship(back_populates=None, lazy="raise")
    segment: Mapped[Segment | None] = relationship(back_populates=None, lazy="raise")
    raw_email: Mapped[RawEmail | None] = relationship(back_populates=None, lazy="raise")
