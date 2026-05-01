"""phase8_home_currency

Revision ID: c9118b558178
Revises: 70ae34091470
Create Date: 2026-05-01 10:43:13.753305

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9118b558178"
down_revision: str | Sequence[str] | None = "70ae34091470"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column("home_currency", sa.String(length=3), nullable=False, server_default="USD"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("users", "home_currency")
