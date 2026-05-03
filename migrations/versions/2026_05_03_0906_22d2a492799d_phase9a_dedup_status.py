"""phase9a dedup status

Extends raw_emails.parse_status CHECK constraint to include 'duplicate'.
The new value lands on raw emails whose drafts were all caught by the
parse-time dedup gate (see src/trip_tracker/parsers/dedup.py, added
later in Phase 9 Track A).

Upgrade: drop ck_raw_emails_parse_status, recreate with 6 allowed values.
Downgrade: drop, recreate with original 5 values.

Revision ID: 22d2a492799d
Revises: c9118b558178
Create Date: 2026-05-03 09:06:03.764927

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "22d2a492799d"
down_revision: str | Sequence[str] | None = "c9118b558178"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Use raw SQL with the literal constraint name as it lives in the DB
# (the Base metadata naming_convention would otherwise double-prefix
# ck_raw_emails_ → ck_raw_emails_ck_raw_emails_parse_status).
def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE raw_emails DROP CONSTRAINT ck_raw_emails_parse_status"))
    op.execute(
        sa.text(
            "ALTER TABLE raw_emails ADD CONSTRAINT ck_raw_emails_parse_status "
            "CHECK (parse_status IN ('pending', 'parsed', 'failed', 'no_segments', 'review', 'duplicate'))"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("ALTER TABLE raw_emails DROP CONSTRAINT ck_raw_emails_parse_status"))
    op.execute(
        sa.text(
            "ALTER TABLE raw_emails ADD CONSTRAINT ck_raw_emails_parse_status "
            "CHECK (parse_status IN ('pending', 'parsed', 'failed', 'no_segments', 'review'))"
        )
    )
