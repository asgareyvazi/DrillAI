"""Enforce organization and well scope across wellbores and daily reports.

Revision ID: a6f4c2d8e901
Revises: f0a4b8c1d2e3
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a6f4c2d8e901"
down_revision = "f0a4b8c1d2e3"
branch_labels = None
depends_on = None


def _scope_violation_counts(connection: sa.Connection) -> tuple[int, int]:
    invalid_wellbores = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM wellbores AS b "
                "LEFT JOIN wells AS w ON w.id = b.well_id "
                "WHERE w.id IS NULL OR b.org_id <> w.org_id"
            )
        ).scalar_one()
    )
    invalid_reports = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM daily_drilling_reports AS r "
                "LEFT JOIN wells AS w ON w.id = r.well_id "
                "LEFT JOIN wellbores AS b ON b.id = r.wellbore_id "
                "WHERE w.id IS NULL OR b.id IS NULL "
                "OR r.org_id <> w.org_id OR r.org_id <> b.org_id OR r.well_id <> b.well_id"
            )
        ).scalar_one()
    )
    return invalid_wellbores, invalid_reports


def upgrade() -> None:
    connection = op.get_bind()
    invalid_wellbores, invalid_reports = _scope_violation_counts(connection)
    if invalid_wellbores or invalid_reports:
        raise RuntimeError(
            "Refusing to add organization/well scope constraints: "
            f"{invalid_wellbores} wellbore row(s) disagree with their parent well and "
            f"{invalid_reports} daily report row(s) disagree with their well or wellbore. "
            "Resolve existing data through an audited, owner-approved process before retrying."
        )

    with op.batch_alter_table("wells", schema=None) as batch:
        batch.create_unique_constraint("uq_wells_org_id", ["org_id", "id"])

    with op.batch_alter_table("wellbores", schema=None) as batch:
        batch.create_unique_constraint(
            "uq_wellbores_org_wellbore_scope", ["org_id", "well_id", "id"]
        )
        batch.create_foreign_key(
            "fk_wellbores_org_well_scope",
            "wells",
            ["org_id", "well_id"],
            ["org_id", "id"],
            ondelete="CASCADE",
        )

    with op.batch_alter_table("daily_drilling_reports", schema=None) as batch:
        batch.create_foreign_key(
            "fk_daily_drilling_reports_wellbore_scope",
            "wellbores",
            ["org_id", "well_id", "wellbore_id"],
            ["org_id", "well_id", "id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("daily_drilling_reports", schema=None) as batch:
        batch.drop_constraint("fk_daily_drilling_reports_wellbore_scope", type_="foreignkey")

    with op.batch_alter_table("wellbores", schema=None) as batch:
        batch.drop_constraint("fk_wellbores_org_well_scope", type_="foreignkey")
        batch.drop_constraint("uq_wellbores_org_wellbore_scope", type_="unique")

    with op.batch_alter_table("wells", schema=None) as batch:
        batch.drop_constraint("uq_wells_org_id", type_="unique")
