"""include operation scope in document identity and serialize duplicate uploads

Revision ID: c0f6d8a9b271
Revises: d4e7a9c12b83
"""

from __future__ import annotations

import datetime as dt
import hashlib

import sqlalchemy as sa
from alembic import op

revision = "c0f6d8a9b271"
down_revision = "d4e7a9c12b83"
branch_labels = None
depends_on = None


def document_logical_key(
    *,
    content_sha256: str | None,
    section_id: str | None,
    wellbore_id: str | None,
    well_id: str | None,
    project_id: str | None,
    operation_id: str | None,
    doc_type: str | None,
    revision: str | None,
) -> str | None:
    """Frozen copy of the current logical document key for this migration."""
    if not content_sha256:
        return None
    scope = section_id or wellbore_id or well_id or project_id or ""
    parts = "|".join(
        [
            content_sha256,
            scope,
            operation_id or "",
            (doc_type or "").strip().lower(),
            (revision or "").strip().lower(),
        ]
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:64]


def _rebuild_keys(*, include_operation: bool) -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT d.id, d.org_id, a.sha256, d.checksum, d.section_id, d.wellbore_id, "
            "d.well_id, d.project_id, d.operation_id, d.doc_type, d.revision "
            "FROM documents d LEFT JOIN raw_artifacts a ON a.id = d.raw_artifact_id"
        )
    ).fetchall()
    for row in rows:
        key = document_logical_key(
            content_sha256=row[2] or row[3],
            section_id=row[4],
            wellbore_id=row[5],
            well_id=row[6],
            project_id=row[7],
            operation_id=row[8] if include_operation else None,
            doc_type=row[9],
            revision=row[10],
        )
        connection.execute(
            sa.text("UPDATE documents SET logical_key = :key WHERE id = :document_id"),
            {"key": key, "document_id": row[0]},
        )


def _keep_one_key_per_tenant() -> None:
    """Preserve duplicate legacy documents, but index only their deterministic oldest representative."""
    connection = op.get_bind()
    duplicates = connection.execute(
        sa.text(
            "SELECT org_id, logical_key FROM documents "
            "WHERE logical_key IS NOT NULL GROUP BY org_id, logical_key HAVING COUNT(*) > 1"
        )
    ).fetchall()
    for org_id, key in duplicates:
        ids = connection.execute(
            sa.text(
                "SELECT id FROM documents WHERE org_id = :org_id AND logical_key = :key "
                "ORDER BY created_at, id"
            ),
            {"org_id": org_id, "key": key},
        ).fetchall()
        for (duplicate_id,) in ids[1:]:
            connection.execute(
                sa.text("UPDATE documents SET logical_key = NULL WHERE id = :document_id"),
                {"document_id": duplicate_id},
            )


def _merge_raw_artifact_metadata(canonical: dict[str, object], duplicates: list[dict[str, object]]) -> None:
    """Preserve each duplicate's provenance and fail closed on conflicting security metadata.

    The unique digest index requires one row per tenant and content. Deleting duplicate rows without
    carrying their metadata forward would silently discard source, uploader, retention, quarantine,
    scan, and arbitrary attributes. The canonical row keeps its primary provenance fields; every
    removed row is archived in its JSON attributes. Conflicting summary fields are marked explicitly
    rather than inheriting the oldest row's potentially less restrictive value.
    """
    if not duplicates:
        return

    canonical_attributes = canonical.get("attributes")
    if isinstance(canonical_attributes, dict):
        merged_attributes = dict(canonical_attributes)
    else:
        merged_attributes = {"legacy_attributes_value": canonical_attributes}
    archive_key = "migration_deduplicated_raw_artifacts"
    previous_archive = merged_attributes.get(archive_key)
    if isinstance(previous_archive, list):
        archive = list(previous_archive)
    elif previous_archive is None:
        archive = []
    else:
        archive = [{"preexisting_archive_value": previous_archive}]

    for duplicate in duplicates:
        created_at = duplicate.get("created_at")
        archive.append(
            {
                key: created_at.isoformat() if key == "created_at" and isinstance(created_at, dt.datetime) else value
                for key, value in duplicate.items()
            }
        )
    merged_attributes[archive_key] = archive
    canonical["attributes"] = merged_attributes

    scan_statuses = {
        str(value).strip().lower()
        for value in [canonical.get("scan_status"), *(row.get("scan_status") for row in duplicates)]
        if value is not None and str(value).strip()
    }
    scan_status_conflict = len(scan_statuses) > 1
    canonical["is_quarantined"] = bool(canonical.get("is_quarantined")) or any(
        bool(row.get("is_quarantined")) for row in duplicates
    ) or scan_status_conflict

    for field, conflict_value in (
        ("retention_class", "mixed"),
        ("scan_status", "review_required"),
        ("source_kind", "mixed"),
    ):
        values = {
            str(value).strip()
            for value in [canonical.get(field), *(row.get(field) for row in duplicates)]
            if value is not None and str(value).strip()
        }
        if len(values) > 1:
            canonical[field] = conflict_value
        elif values:
            canonical[field] = values.pop()


def _keep_one_raw_artifact_per_tenant_digest() -> None:
    """Repoint legacy references to the deterministic oldest copy of identical bytes."""
    connection = op.get_bind()
    # SHA-256 text is canonically lowercase. Normalizing legacy values before grouping makes the
    # unique key agree with the value produced by hashlib for subsequent uploads.
    connection.execute(sa.text("UPDATE raw_artifacts SET sha256 = LOWER(sha256)"))
    duplicates = connection.execute(
        sa.text(
            "SELECT org_id, sha256 FROM raw_artifacts "
            "GROUP BY org_id, sha256 HAVING COUNT(*) > 1"
        )
    ).fetchall()
    raw_artifacts = sa.table(
        "raw_artifacts",
        sa.column("id", sa.String(64)),
        sa.column("org_id", sa.String(64)),
        sa.column("sha256", sa.String(64)),
        sa.column("blob_key", sa.String(400)),
        sa.column("byte_size", sa.Integer()),
        sa.column("content_type", sa.String(160)),
        sa.column("original_filename", sa.String(400)),
        sa.column("source_kind", sa.String(40)),
        sa.column("source_ref", sa.JSON()),
        sa.column("uploaded_by", sa.String(64)),
        sa.column("retention_class", sa.String(40)),
        sa.column("is_quarantined", sa.Boolean()),
        sa.column("scan_status", sa.String(24)),
        sa.column("attributes", sa.JSON()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    for org_id, digest in duplicates:
        rows = connection.execute(
            sa.select(raw_artifacts)
            .where(raw_artifacts.c.org_id == org_id, raw_artifacts.c.sha256 == digest)
            .order_by(raw_artifacts.c.created_at, raw_artifacts.c.id)
            .with_for_update()
        ).mappings().all()
        if len(rows) < 2:
            continue
        canonical = dict(rows[0])
        canonical_id = str(canonical["id"])
        duplicate_rows = [dict(row) for row in rows[1:]]
        duplicate_ids = [str(row["id"]) for row in duplicate_rows]
        _merge_raw_artifact_metadata(canonical, duplicate_rows)
        connection.execute(
            raw_artifacts.update()
            .where(raw_artifacts.c.id == canonical_id)
            .values(
                attributes=canonical["attributes"],
                retention_class=canonical["retention_class"],
                is_quarantined=canonical["is_quarantined"],
                scan_status=canonical["scan_status"],
                source_kind=canonical["source_kind"],
            )
        )
        for duplicate_id in duplicate_ids:
            # These are the database references to raw artifacts. Documents have a foreign key;
            # ingestion_jobs retain the reference as an indexed audit field without a foreign key.
            connection.execute(
                sa.text("UPDATE documents SET raw_artifact_id = :canonical WHERE raw_artifact_id = :duplicate"),
                {"canonical": canonical_id, "duplicate": duplicate_id},
            )
            connection.execute(
                sa.text("UPDATE ingestion_jobs SET raw_artifact_id = :canonical WHERE raw_artifact_id = :duplicate"),
                {"canonical": canonical_id, "duplicate": duplicate_id},
            )
            connection.execute(
                sa.text("DELETE FROM raw_artifacts WHERE id = :duplicate"),
                {"duplicate": duplicate_id},
            )


def upgrade() -> None:
    _keep_one_raw_artifact_per_tenant_digest()
    op.create_index(
        "uq_raw_artifacts_org_sha256",
        "raw_artifacts",
        ["org_id", "sha256"],
        unique=True,
    )
    _rebuild_keys(include_operation=True)
    _keep_one_key_per_tenant()
    op.create_index(
        "uq_documents_org_logical_key",
        "documents",
        ["org_id", "logical_key"],
        unique=True,
        sqlite_where=sa.text("logical_key IS NOT NULL"),
        postgresql_where=sa.text("logical_key IS NOT NULL"),
    )


def downgrade() -> None:
    """Drop the new identity rules; raw-artifact deduplication is intentionally irreversible.

    The upgrade archives the removed raw rows' metadata in the canonical artifact, but repoints all
    document/job references before deleting duplicate rows and does not retain a reference map. A
    downgrade therefore restores the legacy document keys and drops uniqueness, while keeping the
    merged artifact and its archive intact. It does *not* recreate the old raw-row cardinality or
    claim that duplicate references have been restored; operators who require that historical shape
    must restore a database backup from before this migration.
    """
    op.drop_index("uq_documents_org_logical_key", table_name="documents")
    _rebuild_keys(include_operation=False)
    op.drop_index("uq_raw_artifacts_org_sha256", table_name="raw_artifacts")
