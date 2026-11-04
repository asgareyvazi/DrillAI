"""Distinguish provider acceptance from recipient delivery confirmation.

Revision ID: e90f1c8a2b64
Revises: d2a4f6c8b109

New successful notification attempts record their acceptance time and narrowly defined evidence.
Legacy ``sent_at`` and ``sent`` rows are intentionally left untouched and are not promoted to
recipient-delivered status.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e90f1c8a2b64"
down_revision = "d2a4f6c8b109"
branch_labels = None
depends_on = None

_ACCEPTANCE_EVIDENCE = ("http_2xx", "smtp_server_accepted", "local_in_app_outbox")


def upgrade() -> None:
    connection = op.get_bind()
    duplicate_pending_keys = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM ("
            "SELECT org_id, idempotency_key FROM outbound_messages "
            "WHERE idempotency_key IS NOT NULL "
            "AND status IN ('queued', 'sending', 'failed_retryable') "
            "GROUP BY org_id, idempotency_key HAVING COUNT(*) > 1)"
        )
    ).scalar_one()
    if duplicate_pending_keys:
        raise RuntimeError(
            "cannot enforce notification idempotency while duplicate pending keys exist; "
            "reconcile those rows without deleting history"
        )

    with op.batch_alter_table("outbound_messages") as batch:
        batch.add_column(sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("acceptance_evidence", sa.String(length=40), nullable=True))
        batch.create_check_constraint(
            "valid_notification_acceptance_evidence",
            "acceptance_evidence IS NULL OR acceptance_evidence IN "
            "('http_2xx', 'smtp_server_accepted', 'local_in_app_outbox')",
        )
        batch.create_check_constraint(
            "accepted_notification_has_evidence",
            "status != 'accepted' OR (accepted_at IS NOT NULL AND acceptance_evidence IS NOT NULL)",
        )
    op.create_index(
        "uq_outbound_messages_pending_idempotency_key",
        "outbound_messages",
        ["org_id", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text(
            "idempotency_key IS NOT NULL AND status IN ('queued', 'sending', 'failed_retryable')"
        ),
        postgresql_where=sa.text(
            "idempotency_key IS NOT NULL AND status IN ('queued', 'sending', 'failed_retryable')"
        ),
    )


def downgrade() -> None:
    connection = op.get_bind()
    accepted_rows = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM outbound_messages "
            "WHERE accepted_at IS NOT NULL OR acceptance_evidence IS NOT NULL "
            "OR status IN ('accepted', 'reconciliation_required')"
        )
    ).scalar_one()
    if accepted_rows:
        raise RuntimeError(
            "cannot drop notification acceptance evidence while accepted or reconciliation-required rows exist"
        )

    op.drop_index("uq_outbound_messages_pending_idempotency_key", table_name="outbound_messages")
    with op.batch_alter_table("outbound_messages") as batch:
        batch.drop_constraint("accepted_notification_has_evidence", type_="check")
        batch.drop_constraint("valid_notification_acceptance_evidence", type_="check")
        batch.drop_column("acceptance_evidence")
        batch.drop_column("accepted_at")
