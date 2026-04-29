"""phase3 llm_budget

Revision ID: 905263cb9862
Revises: bbf3bbe09be9
Create Date: 2026-04-29 14:02:35.596056

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "905263cb9862"
down_revision = "bbf3bbe09be9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_budget",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("cost_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("llm_budget")
