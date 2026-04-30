"""phase5 documents

Revision ID: ca9d57f3f7f4
Revises: 905263cb9862
Create Date: 2026-04-30 15:09:13.219521

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "ca9d57f3f7f4"
down_revision: str | Sequence[str] | None = "905263cb9862"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("raw_email_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("extract_status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("extract_method", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trip_id"], ["trips.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["segment_id"], ["segments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["raw_email_id"], ["raw_emails.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("owner_user_id", "sha256", name="uq_documents_owner_sha256"),
    )
    op.create_index("ix_documents_trip_id", "documents", ["trip_id"])
    op.create_index("ix_documents_segment_id", "documents", ["segment_id"])
    op.create_index("ix_documents_owner", "documents", ["owner_user_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_owner", table_name="documents")
    op.drop_index("ix_documents_segment_id", table_name="documents")
    op.drop_index("ix_documents_trip_id", table_name="documents")
    op.drop_table("documents")
