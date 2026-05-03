"""phase9bc merge columns

Adds:
  - trips.merged_into_id, trips.merged_at, trips.merge_audit
  - ix_trips_owner_dates partial index (active trips only)
  - trip_merge_dismissals table

Per spec §4.2.

Revision ID: 4cf28c429f18
Revises: 22d2a492799d
Create Date: 2026-05-03 16:02:09.746661

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "4cf28c429f18"
down_revision: str | Sequence[str] | None = "22d2a492799d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # 1. Trip soft-delete columns + audit
    op.add_column(
        "trips",
        sa.Column("merged_into_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("trips", sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trips", sa.Column("merge_audit", postgresql.JSONB, nullable=True))
    op.create_foreign_key(
        "fk_trips_merged_into",
        "trips",
        "trips",
        ["merged_into_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # SET NULL not CASCADE — if the target of a merge is later hard-deleted,
    # the source row should survive with a null pointer rather than vanish.

    # 2. Partial index for the owner+date range query (also covers the
    #    WHERE merged_into_id IS NULL filter clause everywhere).
    op.create_index(
        "ix_trips_owner_dates",
        "trips",
        ["created_by", "start_date", "end_date"],
        postgresql_where=sa.text("merged_into_id IS NULL"),
    )

    # 3. Per-pair dismissal table
    op.create_table(
        "trip_merge_dismissals",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trip_a_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trip_b_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "dismissed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trip_a_id"], ["trips.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trip_b_id"], ["trips.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "user_id", "trip_a_id", "trip_b_id", name="pk_trip_merge_dismissals"
        ),
    )
    # Pair-uniqueness via expression UNIQUE INDEX. Postgres allows this in a
    # UNIQUE INDEX even though it can't be used as a PRIMARY KEY expression.
    # The LEAST/GREATEST canonicalization ensures (A,B) and (B,A) are treated
    # as the same pair regardless of insert order.
    op.execute(
        "CREATE UNIQUE INDEX uq_trip_merge_dismissals_pair "
        "ON trip_merge_dismissals ("
        "  user_id, "
        "  LEAST(trip_a_id, trip_b_id), "
        "  GREATEST(trip_a_id, trip_b_id)"
        ")"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS uq_trip_merge_dismissals_pair")
    op.drop_table("trip_merge_dismissals")
    op.drop_index("ix_trips_owner_dates", table_name="trips")
    op.drop_constraint("fk_trips_merged_into", "trips", type_="foreignkey")
    op.drop_column("trips", "merge_audit")
    op.drop_column("trips", "merged_at")
    op.drop_column("trips", "merged_into_id")
