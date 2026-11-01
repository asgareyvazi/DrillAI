"""version extracted record reviews

Revision ID: da0881aa121d
Revises: f19a6d4c82b0
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "da0881aa121d"
down_revision = "f19a6d4c82b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing extracted rows start at version 1. Review writes use a conditional UPDATE against
    # this column, so concurrent reviewers cannot overwrite one another on either supported dialect.
    op.add_column(
        "extracted_records",
        sa.Column("review_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )


def downgrade() -> None:
    # Review history remains in the audit ledger; downgrade removes only the CAS counter.
    op.drop_column("extracted_records", "review_version")
