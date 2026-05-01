"""phase8_expenses

Revision ID: 70ae34091470
Revises: 2dead0c2dfd4
Create Date: 2026-05-01 10:30:41.372233

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "70ae34091470"
down_revision: str | Sequence[str] | None = "2dead0c2dfd4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "expenses",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("fx_rate", sa.Numeric(20, 10), nullable=False),
        sa.Column("amount_home_minor", sa.BigInteger(), nullable=False),
        sa.Column("home_currency", sa.String(length=3), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("incurred_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="paid"),
        sa.Column("deposit_minor", sa.BigInteger(), nullable=True),
        sa.Column("cancellation_deadline", sa.Date(), nullable=True),
        sa.Column("cancellation_fee_minor", sa.BigInteger(), nullable=True),
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
        sa.ForeignKeyConstraint(["trip_id"], ["trips.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["segment_id"], ["segments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_expenses_trip", "expenses", ["trip_id"])
    op.create_index("ix_expenses_segment", "expenses", ["segment_id"])
    op.create_index("ix_expenses_owner", "expenses", ["owner_user_id"])
    op.create_index("ix_expenses_incurred", "expenses", ["incurred_on"])


def downgrade() -> None:
    op.drop_index("ix_expenses_incurred", table_name="expenses")
    op.drop_index("ix_expenses_owner", table_name="expenses")
    op.drop_index("ix_expenses_segment", table_name="expenses")
    op.drop_index("ix_expenses_trip", table_name="expenses")
    op.drop_table("expenses")
