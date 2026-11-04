"""Bind polymorphic daily-report reconciliation targets to their revision and tenant.

Revision ID: c2e7a1b9d608
Revises: a6f4c2d8e901
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c2e7a1b9d608"
down_revision = "a6f4c2d8e901"
branch_labels = None
depends_on = None

_RECONCILIATIONS = "daily_report_reconciliations"
_ACTIVITY_UNIQUE = "uq_daily_report_activity_org_revision_id"
_MEASUREMENT_UNIQUE = "uq_daily_report_measurement_org_revision_id"
_ACTIVITY_FK = "fk_daily_report_reconciliation_activity_target_scope"
_MEASUREMENT_FK = "fk_daily_report_reconciliation_measurement_target_scope"
_TARGET_ID_CHECK = "daily_report_reconciliation_target_id_consistent"

_TARGET_ID_CHECK_SQL = (
    "(target_kind IN ('revision_field', 'report_identity') AND "
    "activity_target_id IS NULL AND measurement_target_id IS NULL) OR "
    "(target_kind = 'activity' AND activity_target_id IS NOT NULL AND measurement_target_id IS NULL) OR "
    "(target_kind = 'measurement' AND activity_target_id IS NULL AND measurement_target_id IS NOT NULL)"
)

_LEGACY_TARGET_ID_CHECK_SQL = (
    "(target_kind IN ('revision_field', 'report_identity') AND target_id IS NULL) OR "
    "(target_kind IN ('activity', 'measurement') AND target_id IS NOT NULL)"
)


def _orphan_counts(connection: sa.Connection, *, legacy: bool) -> tuple[int, int]:
    if legacy:
        activity_target = "r.target_id"
        measurement_target = "r.target_id"
    else:
        activity_target = "r.activity_target_id"
        measurement_target = "r.measurement_target_id"
    invalid_activities = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM daily_report_reconciliations AS r "
                "WHERE r.target_kind = 'activity' AND NOT EXISTS ("
                "SELECT 1 FROM daily_report_activities AS a "
                f"WHERE a.id = {activity_target} AND a.org_id = r.org_id AND a.revision_id = r.revision_id)"
            )
        ).scalar_one()
    )
    invalid_measurements = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM daily_report_reconciliations AS r "
                "WHERE r.target_kind = 'measurement' AND NOT EXISTS ("
                "SELECT 1 FROM daily_report_measurements AS m "
                f"WHERE m.id = {measurement_target} AND m.org_id = r.org_id AND m.revision_id = r.revision_id)"
            )
        ).scalar_one()
    )
    return invalid_activities, invalid_measurements


def _refuse_orphans(activity_count: int, measurement_count: int, *, direction: str) -> None:
    if activity_count or measurement_count:
        raise RuntimeError(
            f"Refusing to {direction} daily-report reconciliation target-scope constraints: "
            f"{activity_count} activity and {measurement_count} measurement target row(s) "
            "do not resolve to a child in the same organization and revision. "
            "Preserve and resolve the affected records through an audited process before retrying."
        )


def upgrade() -> None:
    connection = op.get_bind()
    invalid_activities, invalid_measurements = _orphan_counts(connection, legacy=True)
    _refuse_orphans(invalid_activities, invalid_measurements, direction="add")

    # These keys let one polymorphic target column be represented as two nullable, typed foreign keys.
    with op.batch_alter_table("daily_report_activities", schema=None) as batch:
        batch.create_unique_constraint(_ACTIVITY_UNIQUE, ["org_id", "revision_id", "id"])
    with op.batch_alter_table("daily_report_measurements", schema=None) as batch:
        batch.create_unique_constraint(_MEASUREMENT_UNIQUE, ["org_id", "revision_id", "id"])

    op.add_column(_RECONCILIATIONS, sa.Column("activity_target_id", sa.String(length=64), nullable=True))
    op.add_column(_RECONCILIATIONS, sa.Column("measurement_target_id", sa.String(length=64), nullable=True))
    connection.execute(
        sa.text(
            "UPDATE daily_report_reconciliations SET activity_target_id = target_id "
            "WHERE target_kind = 'activity'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE daily_report_reconciliations SET measurement_target_id = target_id "
            "WHERE target_kind = 'measurement'"
        )
    )

    with op.batch_alter_table(_RECONCILIATIONS, schema=None) as batch:
        batch.drop_constraint(_TARGET_ID_CHECK, type_="check")
        batch.drop_index("ix_daily_report_reconciliations_target_id")
        batch.drop_column("target_id")
        batch.create_check_constraint(_TARGET_ID_CHECK, _TARGET_ID_CHECK_SQL)
        batch.create_foreign_key(
            _ACTIVITY_FK,
            "daily_report_activities",
            ["org_id", "revision_id", "activity_target_id"],
            ["org_id", "revision_id", "id"],
            deferrable=True,
            initially="DEFERRED",
        )
        batch.create_foreign_key(
            _MEASUREMENT_FK,
            "daily_report_measurements",
            ["org_id", "revision_id", "measurement_target_id"],
            ["org_id", "revision_id", "id"],
            deferrable=True,
            initially="DEFERRED",
        )

    op.create_index(
        "ix_daily_report_reconciliations_activity_target_id",
        _RECONCILIATIONS,
        ["activity_target_id"],
    )
    op.create_index(
        "ix_daily_report_reconciliations_measurement_target_id",
        _RECONCILIATIONS,
        ["measurement_target_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    invalid_activities, invalid_measurements = _orphan_counts(connection, legacy=False)
    _refuse_orphans(invalid_activities, invalid_measurements, direction="remove")

    op.add_column(_RECONCILIATIONS, sa.Column("target_id", sa.String(length=64), nullable=True))
    connection.execute(
        sa.text(
            "UPDATE daily_report_reconciliations "
            "SET target_id = COALESCE(activity_target_id, measurement_target_id)"
        )
    )
    op.drop_index(
        "ix_daily_report_reconciliations_activity_target_id",
        table_name=_RECONCILIATIONS,
    )
    op.drop_index(
        "ix_daily_report_reconciliations_measurement_target_id",
        table_name=_RECONCILIATIONS,
    )

    with op.batch_alter_table(_RECONCILIATIONS, schema=None) as batch:
        batch.drop_constraint(_ACTIVITY_FK, type_="foreignkey")
        batch.drop_constraint(_MEASUREMENT_FK, type_="foreignkey")
        batch.drop_constraint(_TARGET_ID_CHECK, type_="check")
        batch.drop_column("activity_target_id")
        batch.drop_column("measurement_target_id")
        batch.create_check_constraint(_TARGET_ID_CHECK, _LEGACY_TARGET_ID_CHECK_SQL)

    op.create_index(
        "ix_daily_report_reconciliations_target_id",
        _RECONCILIATIONS,
        ["target_id"],
    )
    with op.batch_alter_table("daily_report_measurements", schema=None) as batch:
        batch.drop_constraint(_MEASUREMENT_UNIQUE, type_="unique")
    with op.batch_alter_table("daily_report_activities", schema=None) as batch:
        batch.drop_constraint(_ACTIVITY_UNIQUE, type_="unique")
