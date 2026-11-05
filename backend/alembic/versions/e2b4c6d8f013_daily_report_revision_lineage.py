"""Bind report revision pointers and supersession lineage to the same report.

Revision ID: e2b4c6d8f013
Revises: d8f3b4c2a917
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e2b4c6d8f013"
down_revision = "d8f3b4c2a917"
branch_labels = None
depends_on = None


_POINTER_CHECKS = {
    "latest_revision_pointer": (
        "NOT EXISTS (SELECT 1 FROM daily_drilling_report_revisions AS rev "
        "WHERE rev.org_id = report.org_id AND rev.report_id = report.id "
        "AND rev.revision_number = report.latest_revision_number)"
    ),
    "latest_approved_revision_pointer": (
        "report.latest_approved_revision_number IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM daily_drilling_report_revisions AS rev "
        "WHERE rev.org_id = report.org_id AND rev.report_id = report.id "
        "AND rev.revision_number = report.latest_approved_revision_number)"
    ),
}


def _violation_counts(connection: sa.Connection) -> dict[str, int]:
    counts = {
        name: int(
            connection.execute(
                sa.text(f"SELECT COUNT(*) FROM daily_drilling_reports AS report WHERE {predicate}")
            ).scalar_one()
        )
        for name, predicate in _POINTER_CHECKS.items()
    }
    counts["supersedes_scope"] = int(
        connection.execute(
            sa.text(
                "SELECT COUNT(*) FROM daily_drilling_report_revisions AS revision "
                "WHERE revision.supersedes_revision_id IS NOT NULL AND NOT EXISTS ("
                "SELECT 1 FROM daily_drilling_report_revisions AS source "
                "WHERE source.id = revision.supersedes_revision_id "
                "AND source.org_id = revision.org_id AND source.report_id = revision.report_id)"
            )
        ).scalar_one()
    )
    return counts


def _preflight(connection: sa.Connection) -> None:
    counts = _violation_counts(connection)
    invalid = [f"{name}={count}" for name, count in counts.items() if count]
    if invalid:
        raise RuntimeError(
            "Refusing to add daily-report revision lineage constraints: "
            + ", ".join(invalid)
            + ". Resolve the existing pointers through an audited, owner-approved process before retrying."
        )


def upgrade() -> None:
    connection = op.get_bind()
    _preflight(connection)

    with op.batch_alter_table("daily_drilling_report_revisions") as batch:
        batch.create_unique_constraint(
            "uq_daily_report_revision_org_report_number",
            ["org_id", "report_id", "revision_number"],
        )
        batch.create_unique_constraint(
            "uq_daily_report_revision_org_report_id",
            ["org_id", "report_id", "id"],
        )
        batch.create_foreign_key(
            "fk_daily_report_revision_supersedes_scope",
            "daily_drilling_report_revisions",
            ["org_id", "report_id", "supersedes_revision_id"],
            ["org_id", "report_id", "id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("daily_drilling_reports") as batch:
        batch.create_foreign_key(
            "fk_daily_drilling_reports_latest_revision_scope",
            "daily_drilling_report_revisions",
            ["org_id", "id", "latest_revision_number"],
            ["org_id", "report_id", "revision_number"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        )
        batch.create_foreign_key(
            "fk_daily_drilling_reports_latest_approved_revision_scope",
            "daily_drilling_report_revisions",
            ["org_id", "id", "latest_approved_revision_number"],
            ["org_id", "report_id", "revision_number"],
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        )


def downgrade() -> None:
    with op.batch_alter_table("daily_drilling_reports") as batch:
        batch.drop_constraint(
            "fk_daily_drilling_reports_latest_approved_revision_scope", type_="foreignkey"
        )
        batch.drop_constraint("fk_daily_drilling_reports_latest_revision_scope", type_="foreignkey")

    with op.batch_alter_table("daily_drilling_report_revisions") as batch:
        batch.drop_constraint("fk_daily_report_revision_supersedes_scope", type_="foreignkey")
        batch.drop_constraint("uq_daily_report_revision_org_report_id", type_="unique")
        batch.drop_constraint("uq_daily_report_revision_org_report_number", type_="unique")
