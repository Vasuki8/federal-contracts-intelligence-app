"""Notices: all listed NAICS codes, and whether the response deadline has a time.

Both come from the first live SAM.gov sample (2026-10-06): records carry an
undocumented `naicsCodes` list, and deadlines can be plain dates.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-06
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE notices
            ADD COLUMN naics_codes text[] NOT NULL DEFAULT '{}',
            ADD COLUMN response_deadline_has_time boolean;
        CREATE INDEX notices_naics_codes_idx ON notices USING gin (naics_codes);
    """)


def downgrade() -> None:
    op.execute("""
        DROP INDEX notices_naics_codes_idx;
        ALTER TABLE notices
            DROP COLUMN response_deadline_has_time,
            DROP COLUMN naics_codes;
    """)
