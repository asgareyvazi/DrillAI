"""Constrain daily-report evidence links to their tenant and declared document.

Revision ID: d8f3b4c2a917
Revises: c2e7a1b9d608
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d8f3b4c2a917"
down_revision = "c2e7a1b9d608"
branch_labels = None
depends_on = None

_SOURCE_TABLES = (
    "daily_drilling_report_revisions",
    "daily_report_activities",
    "daily_report_measurements",
    "daily_report_reconciliations",
)

_UNIQUE_CONSTRAINTS = {
    "documents": (("uq_documents_org_id_id", ("org_id", "id")),),
    "document_regions": (
        ("uq_document_regions_org_id_id", ("org_id", "id")),
        ("uq_document_regions_document_id_id", ("document_id", "id")),
    ),
    "extracted_records": (
        ("uq_extracted_records_org_id_id", ("org_id", "id")),
        ("uq_extracted_records_document_id_id", ("document_id", "id")),
    ),
}


def _source_fk_definitions(table: str) -> tuple[tuple[str, str, tuple[str, ...], tuple[str, ...]], ...]:
    prefix = {
        "daily_drilling_report_revisions": "daily_report_revision",
        "daily_report_activities": "daily_report_activity",
        "daily_report_measurements": "daily_report_measurement",
        "daily_report_reconciliations": "daily_report_reconciliation",
    }[table]
    return (
        (
            f"fk_{prefix}_source_document_tenant",
            "documents",
            ("org_id", "source_document_id"),
            ("org_id", "id"),
        ),
        (
            f"fk_{prefix}_source_record_tenant",
            "extracted_records",
            ("org_id", "source_record_id"),
            ("org_id", "id"),
        ),
        (
            f"fk_{prefix}_source_record_document",
            "extracted_records",
            ("source_document_id", "source_record_id"),
            ("document_id", "id"),
        ),
        (
            f"fk_{prefix}_source_region_tenant",
            "document_regions",
            ("org_id", "source_region_id"),
            ("org_id", "id"),
        ),
        (
            f"fk_{prefix}_source_region_document",
            "document_regions",
            ("source_document_id", "source_region_id"),
            ("document_id", "id"),
        ),
    )


def _scope_violation_counts(connection: sa.Connection) -> dict[str, dict[str, int]]:
    checks = {
        "source_document_tenant": (
            "r.source_document_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM documents AS d "
            "WHERE d.id = r.source_document_id AND d.org_id = r.org_id)"
        ),
        "source_record_tenant": (
            "r.source_record_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM extracted_records AS er "
            "WHERE er.id = r.source_record_id AND er.org_id = r.org_id)"
        ),
        "source_record_document": (
            "r.source_document_id IS NOT NULL AND r.source_record_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM extracted_records AS er "
            "WHERE er.id = r.source_record_id AND er.org_id = r.org_id "
            "AND er.document_id = r.source_document_id)"
        ),
        "source_region_tenant": (
            "r.source_region_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM document_regions AS dr "
            "WHERE dr.id = r.source_region_id AND dr.org_id = r.org_id)"
        ),
        "source_region_document": (
            "r.source_region_id IS NOT NULL AND r.source_document_id IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM document_regions AS dr "
            "WHERE dr.id = r.source_region_id AND dr.org_id = r.org_id "
            "AND dr.document_id = r.source_document_id)"
        ),
        "source_region_missing_document": "r.source_region_id IS NOT NULL AND r.source_document_id IS NULL",
        "source_page_missing_document": "r.source_page_number IS NOT NULL AND r.source_document_id IS NULL",
    }
    counts: dict[str, dict[str, int]] = {}
    for table in _SOURCE_TABLES:
        counts[table] = {
            name: int(
                connection.execute(
                    sa.text(f"SELECT COUNT(*) FROM {table} AS r WHERE {predicate}")
                ).scalar_one()
            )
            for name, predicate in checks.items()
        }
    return counts


def _preflight(connection: sa.Connection) -> None:
    counts = _scope_violation_counts(connection)
    invalid = [
        f"{table}.{relationship}={count}"
        for table, relationships in counts.items()
        for relationship, count in relationships.items()
        if count
    ]
    if invalid:
        raise RuntimeError(
            "Refusing to add daily-report evidence scope constraints: "
            + ", ".join(invalid)
            + ". Resolve existing rows through an audited, owner-approved process before retrying."
        )


def upgrade() -> None:
    connection = op.get_bind()
    _preflight(connection)

    for table, constraints in _UNIQUE_CONSTRAINTS.items():
        with op.batch_alter_table(table) as batch:
            for name, columns in constraints:
                batch.create_unique_constraint(name, list(columns))

    for table in _SOURCE_TABLES:
        with op.batch_alter_table(table) as batch:
            for name, target_table, local_columns, remote_columns in _source_fk_definitions(table):
                batch.create_foreign_key(
                    name,
                    target_table,
                    list(local_columns),
                    list(remote_columns),
                    ondelete="RESTRICT",
                )
            prefix = {
                "daily_drilling_report_revisions": "daily_report_revision",
                "daily_report_activities": "daily_report_activity",
                "daily_report_measurements": "daily_report_measurement",
                "daily_report_reconciliations": "daily_report_reconciliation",
            }[table]
            batch.create_check_constraint(
                f"{prefix}_source_region_requires_document",
                "source_region_id IS NULL OR source_document_id IS NOT NULL",
            )
            batch.create_check_constraint(
                f"{prefix}_source_page_requires_document",
                "source_page_number IS NULL OR source_document_id IS NOT NULL",
            )


def downgrade() -> None:
    for table in reversed(_SOURCE_TABLES):
        with op.batch_alter_table(table) as batch:
            prefix = {
                "daily_drilling_report_revisions": "daily_report_revision",
                "daily_report_activities": "daily_report_activity",
                "daily_report_measurements": "daily_report_measurement",
                "daily_report_reconciliations": "daily_report_reconciliation",
            }[table]
            batch.drop_constraint(op.f(f"ck_{table}_{prefix}_source_region_requires_document"), type_="check")
            batch.drop_constraint(op.f(f"ck_{table}_{prefix}_source_page_requires_document"), type_="check")
            for name, _target_table, _local_columns, _remote_columns in reversed(
                _source_fk_definitions(table)
            ):
                batch.drop_constraint(name, type_="foreignkey")

    for table, constraints in reversed(tuple(_UNIQUE_CONSTRAINTS.items())):
        with op.batch_alter_table(table) as batch:
            for name, _columns in reversed(constraints):
                batch.drop_constraint(name, type_="unique")
