"""daily report source reconciliation ledger

Revision ID: e13acb7d4f20
Revises: c5ca12bb7b5d
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e13acb7d4f20"
down_revision = "c5ca12bb7b5d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_report_reconciliations",
        sa.Column("revision_id", sa.String(length=64), nullable=False),
        sa.Column("target_key", sa.String(length=180), nullable=False),
        sa.Column("target_kind", sa.String(length=24), nullable=False),
        sa.Column("target_id", sa.String(length=64), nullable=True),
        sa.Column("field_key", sa.String(length=48), nullable=False),
        sa.Column("source_document_id", sa.String(length=64), nullable=False),
        sa.Column("source_record_id", sa.String(length=64), nullable=False),
        sa.Column("source_region_id", sa.String(length=64), nullable=True),
        sa.Column("source_page_number", sa.Integer(), nullable=True),
        sa.Column("source_path", sa.String(length=256), nullable=False),
        sa.Column("source_review_version", sa.Integer(), nullable=False),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_value_hash", sa.String(length=64), nullable=False),
        sa.Column("proposed_value_hash", sa.String(length=64), nullable=False),
        sa.Column("resolution_state", sa.String(length=24), server_default="proposed", nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=64), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("org_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint("length(trim(target_key)) > 0", name="ck_daily_report_reconciliations_daily_report_reconciliation_target_nonempty"),
        sa.CheckConstraint("target_kind IN ('revision_field', 'activity', 'measurement')", name="ck_daily_report_reconciliations_daily_report_reconciliation_target_kind_valid"),
        sa.CheckConstraint("(target_kind = 'revision_field' AND target_id IS NULL) OR (target_kind IN ('activity', 'measurement') AND target_id IS NOT NULL)", name="ck_daily_report_reconciliations_daily_report_reconciliation_target_id_consistent"),
        sa.CheckConstraint("(target_kind = 'revision_field' AND field_key IN ('period_start_at', 'period_end_at', 'summary', 'operations_summary', 'safety_notes', 'next_period_plan', 'environmental_notes')) OR (target_kind = 'activity' AND field_key = 'activity') OR (target_kind = 'measurement' AND field_key IN (" + ", ".join(f"'{key}'" for key in ("depth_md", "depth_tvd", "depth_tvdss", "bit_depth_md", "hole_depth_md", "daily_progress_md", "rop", "wob", "hookload", "rotary_speed", "flow_rate", "standpipe_pressure", "torque", "mud_weight", "mud_weight_in", "mud_weight_out", "mud_temperature", "funnel_viscosity")) + "))", name="ck_daily_report_reconciliations_daily_report_reconciliation_target_field_consistent"),
        sa.CheckConstraint("length(trim(field_key)) > 0", name="ck_daily_report_reconciliations_daily_report_reconciliation_field_nonempty"),
        sa.CheckConstraint("source_page_number IS NULL OR source_page_number >= 1", name="ck_daily_report_reconciliations_daily_report_reconciliation_page_positive"),
        sa.CheckConstraint("source_review_version >= 1", name="ck_daily_report_reconciliations_daily_report_reconciliation_source_version_positive"),
        sa.CheckConstraint("length(source_fingerprint) = 64 AND length(source_value_hash) = 64 AND length(proposed_value_hash) = 64", name="ck_daily_report_reconciliations_daily_report_reconciliation_hashes_sha256"),
        sa.CheckConstraint("resolution_state IN ('proposed', 'accepted', 'corrected', 'rejected', 'unresolved')", name="ck_daily_report_reconciliations_daily_report_reconciliation_state_valid"),
        sa.CheckConstraint("(resolution_state = 'proposed' AND decided_by IS NULL AND decided_at IS NULL AND reason IS NULL) OR (resolution_state <> 'proposed' AND decided_by IS NOT NULL AND decided_at IS NOT NULL AND reason IS NOT NULL AND length(trim(reason)) > 0)", name="ck_daily_report_reconciliations_daily_report_reconciliation_decision_complete"),
        sa.ForeignKeyConstraint(["revision_id", "org_id"], ["daily_drilling_report_revisions.id", "daily_drilling_report_revisions.org_id"], name="fk_daily_report_reconciliation_revision_tenant", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_document_id"], ["documents.id"], name="fk_daily_report_reconciliations_source_document_id_documents", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_record_id"], ["extracted_records.id"], name=op.f("fk_daily_report_reconciliations_source_record_id_extracted_records"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_region_id"], ["document_regions.id"], name=op.f("fk_daily_report_reconciliations_source_region_id_document_regions"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_daily_report_reconciliations"),
        sa.UniqueConstraint("revision_id", "target_key", name="uq_daily_report_reconciliation_target"),
    )
    op.create_index("ix_daily_report_reconciliations_created_at", "daily_report_reconciliations", ["created_at"])
    op.create_index("ix_daily_report_reconciliations_org_id", "daily_report_reconciliations", ["org_id"])
    op.create_index("ix_daily_report_reconciliations_revision_id", "daily_report_reconciliations", ["revision_id"])
    op.create_index("ix_daily_report_reconciliations_target_id", "daily_report_reconciliations", ["target_id"])
    op.create_index("ix_daily_report_reconciliations_source_document_id", "daily_report_reconciliations", ["source_document_id"])
    op.create_index("ix_daily_report_reconciliations_source_record_id", "daily_report_reconciliations", ["source_record_id"])
    op.create_index("ix_daily_report_reconciliations_source_region_id", "daily_report_reconciliations", ["source_region_id"])
    op.create_index("ix_daily_report_reconciliation_revision_state", "daily_report_reconciliations", ["org_id", "revision_id", "resolution_state"])
    op.create_index("ix_daily_report_reconciliation_source", "daily_report_reconciliations", ["org_id", "source_record_id"])


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.execute(sa.text("SELECT COUNT(*) FROM daily_report_reconciliations")).scalar_one()
    if count:
        raise RuntimeError(
            f"refusing to drop {count} source reconciliation decisions; archive/export the evidence before downgrade"
        )
    op.drop_index("ix_daily_report_reconciliation_source", table_name="daily_report_reconciliations")
    op.drop_index("ix_daily_report_reconciliation_revision_state", table_name="daily_report_reconciliations")
    op.drop_index("ix_daily_report_reconciliations_source_region_id", table_name="daily_report_reconciliations")
    op.drop_index("ix_daily_report_reconciliations_source_record_id", table_name="daily_report_reconciliations")
    op.drop_index("ix_daily_report_reconciliations_source_document_id", table_name="daily_report_reconciliations")
    op.drop_index("ix_daily_report_reconciliations_target_id", table_name="daily_report_reconciliations")
    op.drop_index("ix_daily_report_reconciliations_revision_id", table_name="daily_report_reconciliations")
    op.drop_index("ix_daily_report_reconciliations_org_id", table_name="daily_report_reconciliations")
    op.drop_index("ix_daily_report_reconciliations_created_at", table_name="daily_report_reconciliations")
    op.drop_table("daily_report_reconciliations")
