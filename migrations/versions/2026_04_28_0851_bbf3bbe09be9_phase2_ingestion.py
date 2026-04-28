"""phase2 ingestion

Revision ID: bbf3bbe09be9
Revises: 8e8121194c7d
Create Date: 2026-04-28 08:51:24.230039

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "bbf3bbe09be9"
down_revision: str | Sequence[str] | None = "8e8121194c7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # forwarding_aliases
    op.create_table(
        "forwarding_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("local_part", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forwarding_aliases")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_forwarding_aliases_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("local_part", name=op.f("uq_forwarding_aliases_local_part")),
    )

    # trips
    op.create_table(
        "trips",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("primary_destination", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("cover_color", sa.String(16), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trips")),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_trips_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("end_date >= start_date", name=op.f("ck_trips_date_range")),
    )

    # trip_travelers
    op.create_table(
        "trip_travelers",
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("trip_id", "user_id", name=op.f("pk_trip_travelers")),
        sa.ForeignKeyConstraint(
            ["trip_id"],
            ["trips.id"],
            name=op.f("fk_trip_travelers_trip_id_trips"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_trip_travelers_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("role IN ('owner', 'companion')", name=op.f("ck_trip_travelers_role")),
    )

    # raw_emails (created BEFORE segments since segments.raw_email_id FKs into it)
    op.create_table(
        "raw_emails",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("to_address", sa.String(320), nullable=False),
        sa.Column("from_address", sa.String(320), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("message_id", sa.String(998), nullable=False),
        sa.Column("mime_blob", sa.LargeBinary(), nullable=False),
        sa.Column("headers", postgresql.JSONB(), nullable=False),
        sa.Column(
            "parse_status", sa.String(16), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_emails")),
        sa.UniqueConstraint("message_id", name=op.f("uq_raw_emails_message_id")),
        sa.CheckConstraint(
            "parse_status IN ('pending', 'parsed', 'failed', 'no_segments', 'review')",
            name=op.f("ck_raw_emails_parse_status"),
        ),
    )
    op.create_index("ix_raw_emails_received_at", "raw_emails", [sa.text("received_at DESC")])
    op.create_index("ix_raw_emails_parse_status", "raw_emails", ["parse_status"])
    op.create_index("ix_raw_emails_to_address", "raw_emails", ["to_address"])

    # segments
    op.create_table(
        "segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'confirmed'"), nullable=False),
        sa.Column("confirmation_number", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(128), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_tz", sa.String(64), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_tz", sa.String(64), nullable=True),
        sa.Column("start_location", postgresql.JSONB(), nullable=True),
        sa.Column("end_location", postgresql.JSONB(), nullable=True),
        sa.Column(
            "details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("parse_source", sa.String(64), nullable=False),
        sa.Column("parse_confidence", sa.Float(precision=53), nullable=False),
        sa.Column("raw_email_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("superseded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_segments")),
        sa.ForeignKeyConstraint(
            ["trip_id"], ["trips.id"], name=op.f("fk_segments_trip_id_trips"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            name=op.f("fk_segments_owner_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["raw_email_id"],
            ["raw_emails.id"],
            name=op.f("fk_segments_raw_email_id_raw_emails"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["segments.id"],
            name=op.f("fk_segments_superseded_by_segments"),
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "type IN ('flight', 'lodging', 'car', 'train', 'transfer', 'activity')",
            name=op.f("ck_segments_type"),
        ),
        sa.CheckConstraint(
            "status IN ('confirmed', 'cancelled', 'tentative')",
            name=op.f("ck_segments_status"),
        ),
        sa.CheckConstraint(
            "parse_confidence >= 0 AND parse_confidence <= 1",
            name=op.f("ck_segments_confidence_range"),
        ),
    )
    # PostgreSQL 18 requires double-parens around the expression in GENERATED ALWAYS AS.
    # sa.Computed(persisted=True) emits single-paren syntax, which fails on PG 18, so
    # we add the column via raw DDL instead.
    op.execute(
        """
        ALTER TABLE segments
        ADD COLUMN search_text tsvector
        GENERATED ALWAYS AS ((
            to_tsvector(
                'simple'::regconfig,
                coalesce(provider, '') || ' ' ||
                coalesce(confirmation_number, '') || ' ' ||
                coalesce(start_location ->> 'name', '') || ' ' ||
                coalesce(end_location   ->> 'name', '') || ' ' ||
                coalesce(start_location ->> 'city', '') || ' ' ||
                coalesce(end_location   ->> 'city', '')
            )
        )) STORED
        """
    )

    op.create_index("ix_segments_trip_id", "segments", ["trip_id"])
    op.create_index("ix_segments_owner_user_id_start_at", "segments", ["owner_user_id", "start_at"])
    op.create_index("ix_segments_start_at", "segments", ["start_at"])
    op.create_index("ix_segments_search_text", "segments", ["search_text"], postgresql_using="gin")

    # webhook_replay_cache
    op.create_table(
        "webhook_replay_cache",
        sa.Column("ts_seconds", sa.BigInteger(), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ts_seconds", "nonce", name=op.f("pk_webhook_replay_cache")),
    )
    op.create_index("ix_webhook_replay_cache_expires_at", "webhook_replay_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_table("webhook_replay_cache")
    op.drop_table("segments")
    op.drop_table("raw_emails")
    op.drop_table("trip_travelers")
    op.drop_table("trips")
    op.drop_table("forwarding_aliases")
