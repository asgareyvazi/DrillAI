"""Allow source reconciliation of immutable daily-report identity fields.

Revision ID: f0a4b8c1d2e3
Revises: e90f1c8a2b64
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f0a4b8c1d2e3"
down_revision = "e90f1c8a2b64"
branch_labels = None
depends_on = None

_TABLE = "daily_report_reconciliations"
_TARGET_KIND = "daily_report_reconciliation_target_kind_valid"
_TARGET_ID = "daily_report_reconciliation_target_id_consistent"
_TARGET_FIELD = "daily_report_reconciliation_target_field_consistent"
_OLD_TARGET_KIND = "ck_daily_report_reconciliations_daily_report_reconciliation_target_kind_valid"
_OLD_TARGET_ID = "ck_daily_report_reconciliations_daily_report_reconciliation_target_id_consistent"
_OLD_TARGET_FIELD = "ck_daily_report_reconciliations_daily_report_reconciliation_target_field_consistent"


_KIND_CHECK = "target_kind IN ('revision_field', 'report_identity', 'activity', 'measurement')"
_ID_CHECK = (
    "(target_kind IN ('revision_field', 'report_identity') AND target_id IS NULL) OR "
    "(target_kind IN ('activity', 'measurement') AND target_id IS NOT NULL)"
)
_FIELD_CHECK = (
    "(target_kind = 'revision_field' AND field_key IN "
    "('period_start_at', 'period_end_at', 'summary', 'operations_summary', 'safety_notes', 'next_period_plan', 'environmental_notes')) "
    "OR (target_kind = 'report_identity' AND field_key IN ('report_date', 'report_number')) "
    "OR (target_kind = 'activity' AND field_key = 'activity') "
    "OR (target_kind = 'measurement' AND field_key IN "
    "('depth_md', 'depth_tvd', 'depth_tvdss', 'bit_depth_md', 'hole_depth_md', 'daily_progress_md', 'rop', 'wob', "
    "'hookload', 'rotary_speed', 'flow_rate', 'standpipe_pressure', 'torque', 'mud_weight', 'mud_weight_in', "
    "'mud_weight_out', 'mud_temperature', 'funnel_viscosity'))"
)


_OLD_KIND_CHECK = "target_kind IN ('revision_field', 'activity', 'measurement')"
_OLD_ID_CHECK = (
    "(target_kind = 'revision_field' AND target_id IS NULL) OR "
    "(target_kind IN ('activity', 'measurement') AND target_id IS NOT NULL)"
)
_OLD_FIELD_CHECK = (
    "(target_kind = 'revision_field' AND field_key IN "
    "('period_start_at', 'period_end_at', 'summary', 'operations_summary', 'safety_notes', 'next_period_plan', 'environmental_notes')) "
    "OR (target_kind = 'activity' AND field_key = 'activity') "
    "OR (target_kind = 'measurement' AND field_key IN "
    "('depth_md', 'depth_tvd', 'depth_tvdss', 'bit_depth_md', 'hole_depth_md', 'daily_progress_md', 'rop', 'wob', "
    "'hookload', 'rotary_speed', 'flow_rate', 'standpipe_pressure', 'torque', 'mud_weight', 'mud_weight_in', "
    "'mud_weight_out', 'mud_temperature', 'funnel_viscosity'))"
)


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_OLD_TARGET_KIND, type_="check")
        batch.drop_constraint(_OLD_TARGET_ID, type_="check")
        batch.drop_constraint(_OLD_TARGET_FIELD, type_="check")
        batch.create_check_constraint(_TARGET_KIND, _KIND_CHECK)
        batch.create_check_constraint(_TARGET_ID, _ID_CHECK)
        batch.create_check_constraint(_TARGET_FIELD, _FIELD_CHECK)


def downgrade() -> None:
    connection = op.get_bind()
    identity_rows = connection.execute(
        sa.text(f"SELECT COUNT(*) FROM {_TABLE} WHERE target_kind = 'report_identity'")
    ).scalar_one()
    if identity_rows:
        raise RuntimeError(
            f"Refusing to remove report-identity reconciliation support while {identity_rows} review record(s) exist"
        )
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_TARGET_KIND, type_="check")
        batch.drop_constraint(_TARGET_ID, type_="check")
        batch.drop_constraint(_TARGET_FIELD, type_="check")
        batch.create_check_constraint(_OLD_TARGET_KIND, _OLD_KIND_CHECK)
        batch.create_check_constraint(_OLD_TARGET_ID, _OLD_ID_CHECK)
        batch.create_check_constraint(_OLD_TARGET_FIELD, _OLD_FIELD_CHECK)
