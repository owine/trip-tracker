"""phase11_single_user_collapse

Revision ID: f175b03585e7
Revises: 4cf28c429f18
Create Date: 2026-05-06 19:04:50.241733

Drop multi-user columns/tables and seed the single owner row.

Constraint names verified against live DB 2026-05-06:
  - trips FK on created_by:    fk_trips_created_by_users
  - trips FK on merged_into_id: fk_trips_merged_into
  - trips index covering created_by: ix_trips_owner_dates
  - users unique on oidc_subject: uq_users_oidc_subject

expenses.created_by_id does NOT exist on the DB — not included here.

This migration is intentionally irreversible.
"""

from __future__ import annotations

import os
import uuid

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f175b03585e7"
down_revision: str | None = "4cf28c429f18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------ #
    # Drop FK-bearing tables first so we don't violate referential        #
    # integrity when later dropping the columns they point to.            #
    # ------------------------------------------------------------------ #
    op.drop_table("trip_merge_dismissals")
    op.drop_table("trip_travelers")

    # ------------------------------------------------------------------ #
    # Drop columns + related constraints/indexes from trips               #
    # ------------------------------------------------------------------ #
    # Drop the composite index that covers created_by before dropping the column.
    op.drop_index("ix_trips_owner_dates", table_name="trips")

    with op.batch_alter_table("trips", schema=None) as batch_op:
        # Drop the FK from trips.created_by -> users.id
        batch_op.drop_constraint("fk_trips_created_by_users", type_="foreignkey")
        batch_op.drop_column("created_by")

        # Drop the self-referential FK from trips.merged_into_id -> trips.id
        batch_op.drop_constraint("fk_trips_merged_into", type_="foreignkey")
        batch_op.drop_column("merged_into_id")

        batch_op.drop_column("merged_at")
        batch_op.drop_column("merge_audit")

    # ------------------------------------------------------------------ #
    # Drop columns from users                                             #
    # ------------------------------------------------------------------ #
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_constraint("uq_users_oidc_subject", type_="unique")
        batch_op.drop_column("oidc_subject")
        batch_op.drop_column("is_admin")

    # ------------------------------------------------------------------ #
    # Seed the single owner row                                           #
    # OWNER_USER_ID = uuid.UUID(int=1) matches auth/session.py constant  #
    # ------------------------------------------------------------------ #
    owner_email = os.environ.get("OWNER_EMAIL")
    if not owner_email:
        raise RuntimeError(
            "OWNER_EMAIL must be set when running phase11_single_user_collapse migration"
        )

    op.execute(
        sa.text(
            """
            INSERT INTO users (id, email, display_name, home_currency, created_at, updated_at)
            VALUES (:id, :email, 'Owner', 'USD', now(), now())
            ON CONFLICT (email) DO NOTHING
            """
        ).bindparams(
            id=uuid.UUID(int=1),
            email=owner_email,
        )
    )


def downgrade() -> None:
    raise RuntimeError(
        "phase11_single_user_collapse is not reversible. "
        "To revert, restore from a pre-phase11 backup."
    )
