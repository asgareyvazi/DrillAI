"""Make per-document DDR promotion identity unique at the database boundary.

Revision ID: f19a6d4c82b0
Revises: c0f6d8a9b271
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f19a6d4c82b0"
down_revision = "c0f6d8a9b271"
branch_labels = None
depends_on = None


_PROMOTION_INDEXES = (
    ("operations", "ix_operations_promotion_fingerprint", "uq_operations_org_promotion_fingerprint"),
    ("events", "ix_events_promotion_fingerprint", "uq_events_org_promotion_fingerprint"),
)


def _reject_existing_duplicates() -> None:
    connection = op.get_bind()
    for table_name, _old_index, _unique_index in _PROMOTION_INDEXES:
        duplicates = connection.execute(
            sa.text(
                f"SELECT org_id, promotion_fingerprint, COUNT(*) AS row_count FROM {table_name} "
                "WHERE promotion_fingerprint IS NOT NULL "
                "GROUP BY org_id, promotion_fingerprint HAVING COUNT(*) > 1"
            )
        ).fetchall()
        if duplicates:
            sample = [(row[0], row[1], row[2]) for row in duplicates[:5]]
            raise RuntimeError(
                f"cannot enforce unique {table_name} promotion identity; review duplicate rows first: {sample}"
            )


def upgrade() -> None:
    _reject_existing_duplicates()
    for table_name, old_index, unique_index in _PROMOTION_INDEXES:
        op.drop_index(old_index, table_name=table_name)
        op.create_index(
            unique_index,
            table_name,
            ["org_id", "promotion_fingerprint"],
            unique=True,
        )


def downgrade() -> None:
    for table_name, old_index, unique_index in reversed(_PROMOTION_INDEXES):
        op.drop_index(unique_index, table_name=table_name)
        op.create_index(old_index, table_name, ["promotion_fingerprint"], unique=False)
