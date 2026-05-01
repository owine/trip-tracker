"""phase6 ics_token

Revision ID: 2dead0c2dfd4
Revises: ca9d57f3f7f4
Create Date: 2026-04-30 19:33:18.470446

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2dead0c2dfd4"
down_revision: str | Sequence[str] | None = "ca9d57f3f7f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("ics_token_hash", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint("uq_users_ics_token_hash", "users", ["ics_token_hash"])
    op.create_index("ix_users_ics_token_hash", "users", ["ics_token_hash"])


def downgrade() -> None:
    op.drop_index("ix_users_ics_token_hash", table_name="users")
    op.drop_constraint("uq_users_ics_token_hash", "users", type_="unique")
    op.drop_column("users", "ics_token_hash")
