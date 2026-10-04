"""operational provenance: canonical vocabularies, region linkage, first-class promotion identity

Three related repairs to the layer between an uploaded document and the records the rest of the
platform reads. Each was reproduced against the running application before being written here.

**The status vocabularies had drifted out of use.** ``documents.status`` was written as ``parsing``
while a document was being read and ``parsed`` or ``ingested`` when it finished — none of which is in
the model's own ``DOC_STATUSES``. ``extracted_records.validation_state`` was written as
``unvalidated`` by the pipeline and as ``validated`` by the DDR processor, while ``VALIDATION_STATES``
declared ``extracted``/``rule_validated``/``human_validated``/``rejected``/``superseded`` and
``drilling/state.py`` *read* ``validated``. Six spellings, four declared, one read. The repair maps
each stored value to the canonical one it meant, in a way that can be stated in one line per case:

============================  =========================  ==========================================
stored                        canonical                  why
============================  =========================  ==========================================
``parsing``                   ``extracting``             a document being read is being extracted
``parsed``                    ``partially_extracted``    text was read but nothing usable came out
``ingested``                  ``extracted``              records and chunks were produced
``unvalidated``               ``extracted``              a machine wrote it; nothing has checked it
``validated``                 ``rule_validated``         a deterministic rule accepted it
============================  =========================  ==========================================

``parsed`` mapping to ``partially_extracted`` rather than to ``extracted`` is the one judgement here,
and it is deliberate: the old pipeline wrote ``parsed`` only when the document produced *no* records
and *no* chunks, so a row carrying it records a document that could not be fully used. Mapping it to
``extracted`` would relabel an incomplete read as a complete one, which is the failure this whole
checkpoint exists to remove.

**Region linkage was lost at the write.** ``document_chunks.region_id`` and
``extracted_records.region_id`` were both written as ``NULL`` — the chunk builder was called with
``region_id=None`` explicitly, and the record builder hard-coded it — even though the pipeline was
iterating the very regions the values came from and the draft carried the region's order index. The
columns existed, were indexed, and were never populated, so "which part of page 7 said this?" could
not be answered from the database. This migration adds ``extracted_records.region_unknown``, which is
what an extractor that genuinely cannot name a region must set, and the pipeline now fills both
columns from the region map it already builds.

**Promotion identity was hidden in a JSON blob.** Re-processing a DDR needs to know which records were
already promoted, and the answer lived in ``operations.attributes['promotion_fingerprint']`` and
``events.attributes[...]``. The check therefore loaded every operation of the well and scanned it in
Python — a cost that grows with the well's history and repeats once per extracted record. The
fingerprint and the source record become indexed columns, so the same question is an index lookup.
Existing rows are backfilled from the attributes they already carry; nothing is invented, and a row
whose blob has no fingerprint keeps ``NULL`` and is simply not deduplicated by fingerprint (it is
still deduplicated by the source record id it also carries, or by its own primary key).

``documents.logical_key`` is added for the same reason in the other direction: deciding whether an
upload is a new logical document or a repeat depends on scope, type and revision, and that decision
was being made by scanning for any document already pointing at the artifact — with no regard for
which well it belonged to.
"""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from alembic import op

revision = "b8f2a6d31c04"
down_revision = "c4a91e0d7b52"
branch_labels = None
depends_on = None

#: stored value → canonical value, for the two vocabularies that had drifted.
_DOCUMENT_STATUS_REPAIRS: tuple[tuple[str, str], ...] = (
    ("parsing", "extracting"),
    ("parsed", "partially_extracted"),
    ("ingested", "extracted"),
)

#: The report written onto a trajectory whose stations were transcribed from a report and never
#: independently checked. Frozen in this file because a migration must not change what it wrote when
#: the application code it was derived from is later edited.
_UNVALIDATED_TRAJECTORY_REPORT = {
    "is_valid": True,
    "issues": [
        {
            "severity": "warning",
            "code": "not_independently_validated",
            "message": (
                "stations transcribed from a report; positional accuracy depends on the survey "
                "programme and has not been independently verified"
            ),
        }
    ],
}

_VALIDATION_STATE_REPAIRS: tuple[tuple[str, str], ...] = (
    ("unvalidated", "extracted"),
    ("validated", "rule_validated"),
)

#: Legacy free-text ``source`` values mapped onto ``source_kind``. ``ddr`` and ``report`` are how the
#: promotion path used to name itself before the column existed.
_SOURCE_KIND_BY_LEGACY: tuple[tuple[str, str], ...] = (
    ("ddr", "ddr_promotion"),
    ("report", "ddr_promotion"),
    ("extracted", "ddr_promotion"),
    ("import", "import"),
    ("connector", "connector"),
    ("integration", "integration"),
    ("system", "system"),
)


def _table(name: str, *columns: tuple[str, sa.types.TypeEngine]) -> sa.TableClause:
    return sa.table(name, *[sa.column(column, type_) for column, type_ in columns])


def _backfill_promotion_identity(table_name: str) -> None:
    """Copy the promotion fingerprint out of ``attributes`` into its own column, per row.

    Deliberately row-by-row rather than as one SQL statement: the value lives inside a JSON document,
    and the two dialects read JSON differently (``->>`` on PostgreSQL, ``json_extract`` on SQLite).
    Doing it in Python keeps the migration dialect-neutral, and the row count here is bounded by the
    operations and events a deployment has actually promoted.
    """
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(f"SELECT id, attributes, source_document_id FROM {table_name}")
    ).fetchall()
    if not rows:
        return
    updates: list[dict[str, object]] = []
    for row in rows:
        raw = row[1]
        attributes: dict[str, object] = {}
        if isinstance(raw, dict):
            attributes = raw
        elif isinstance(raw, str) and raw.strip():
            import json

            try:
                parsed = json.loads(raw)
            except ValueError:  # a malformed blob is left alone rather than guessed at
                parsed = {}
            attributes = parsed if isinstance(parsed, dict) else {}
        fingerprint = attributes.get("promotion_fingerprint")
        source_kind = attributes.get("source_kind")
        if source_kind is None and attributes.get("extraction_fingerprint"):
            source_kind = "ddr_promotion"
        updates.append(
            {
                "pk": row[0],
                "fingerprint": fingerprint if isinstance(fingerprint, str) else None,
                "source_kind": source_kind if isinstance(source_kind, str) else None,
                "source_document_id": row[2],
            }
        )
    for update in updates:
        connection.execute(
            sa.text(
                f"""
                UPDATE {table_name}
                SET promotion_fingerprint = COALESCE(:fingerprint, promotion_fingerprint),
                    source_kind = COALESCE(:source_kind, source_kind)
                WHERE id = :pk
                """
            ),
            update,
        )


def _backfill_document_logical_key() -> None:
    """Give every existing document the logical key the new upload path would compute for it.

    The key is a digest of the identity triple that decides duplication: the artifact's bytes, the
    scope the document is attached to, and its type/revision. Documents with no artifact (a row
    created by a connector that stores metadata only) get no key: there is nothing to compare, and an
    invented key would make two unrelated documents look like the same one.
    """
    connection = op.get_bind()
    # Joined to the artefact because the identity is keyed on the *bytes* (their digest) rather than
    # on the artefact's primary key — see ``documents.identity.logical_key`` for why that distinction
    # is load-bearing.
    rows = connection.execute(
        sa.text(
            "SELECT d.id, a.sha256, d.section_id, d.wellbore_id, d.well_id, d.project_id, "
            "d.doc_type, d.revision "
            "FROM documents d LEFT JOIN raw_artifacts a ON a.id = d.raw_artifact_id"
        )
    ).fetchall()
    for row in rows:
        key = document_logical_key(
            content_sha256=row[1],
            section_id=row[2],
            wellbore_id=row[3],
            well_id=row[4],
            project_id=row[5],
            doc_type=row[6],
            revision=row[7],
        )
        if key is None:
            continue
        connection.execute(
            sa.text("UPDATE documents SET logical_key = :key WHERE id = :pk"),
            {"key": key, "pk": row[0]},
        )


def document_logical_key(
    *,
    content_sha256: str | None,
    section_id: str | None,
    well_id: str | None,
    wellbore_id: str | None,
    project_id: str | None,
    doc_type: str | None,
    revision: str | None,
) -> str | None:
    """The identity of a logical document — kept in step with ``documents.identity.logical_key``.

    Defined here as well as in the service because a migration cannot import application code whose
    behaviour may have moved on; this function is the frozen definition the repair used, and
    ``tests/documents/test_document_identity.py`` asserts the two agree so they cannot drift.
    """
    if not content_sha256:
        return None
    # The scope token is the most specific reference present, in this order; the empty string means no
    # scope was named. ``doc_type`` and ``revision`` are case-folded, because "Rev-B" and "rev-b" are
    # one revision and the platform does not get to decide otherwise.
    scope = section_id or wellbore_id or well_id or project_id or ""
    parts = "|".join(
        [
            content_sha256,
            scope,
            (doc_type or "").strip().lower(),
            (revision or "").strip().lower(),
        ]
    )
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:64]


def upgrade() -> None:
    connection = op.get_bind()

    # 1. vocabulary repair, before the new code can read a value it does not know.
    for stored, canonical in _DOCUMENT_STATUS_REPAIRS:
        connection.execute(
            sa.text("UPDATE documents SET status = :canonical WHERE status = :stored"),
            {"canonical": canonical, "stored": stored},
        )
    for stored, canonical in _VALIDATION_STATE_REPAIRS:
        connection.execute(
            sa.text(
                "UPDATE extracted_records SET validation_state = :canonical "
                "WHERE validation_state = :stored"
            ),
            {"canonical": canonical, "stored": stored},
        )

    # 2. the new columns. Every one of them is nullable or has a server-side default, so the
    #    migration is additive: no existing row is rewritten to a value the platform made up.
    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.add_column(sa.Column("logical_key", sa.String(80), nullable=True))
        batch_op.create_index("ix_documents_logical_key", ["logical_key"])

    with op.batch_alter_table("extracted_records", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("region_unknown", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("validation_rule", sa.String(60), nullable=True))

    with op.batch_alter_table("evidence_links", schema=None) as batch_op:
        batch_op.add_column(sa.Column("quote_check", sa.String(24), nullable=True))

    for table_name in ("operations", "events"):
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.add_column(sa.Column("source_record_id", sa.String(64), nullable=True))
            batch_op.add_column(sa.Column("promotion_fingerprint", sa.String(64), nullable=True))
            batch_op.add_column(
                sa.Column("source_kind", sa.String(24), nullable=False, server_default="manual")
            )
            batch_op.create_index(f"ix_{table_name}_source_record_id", ["source_record_id"])
            batch_op.create_index(f"ix_{table_name}_promotion_fingerprint", ["promotion_fingerprint"])
            batch_op.create_index(f"ix_{table_name}_source_kind", ["source_kind"])

    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("cause_basis", sa.String(24), nullable=False, server_default="unknown")
        )
        batch_op.add_column(
            sa.Column("classification_source", sa.String(24), nullable=False, server_default="recorded")
        )

    # 3. ``trajectories.validation`` is a JSON object consumed as a validation report; the DDR
    #    processor wrote the bare string "unvalidated" into it, so a client asking for a trajectory
    #    received a string where every reader expects ``{"is_valid": ..., "issues": [...]}``. Such rows
    #    are rewritten to the report shape that processor now writes. Read row by row and tested with
    #    ``isinstance`` rather than with a JSON function, so one statement serves SQLite and
    #    PostgreSQL alike; the replacement text is valid JSON in both.
    for row in connection.execute(sa.text("SELECT id, validation FROM trajectories")).fetchall():
        if isinstance(row[1], str):
            connection.execute(
                sa.text("UPDATE trajectories SET validation = :report WHERE id = :id"),
                {"id": row[0], "report": json.dumps(_UNVALIDATED_TRAJECTORY_REPORT)},
            )

    # 4. backfill the new identity columns from what the rows already carry.
    _backfill_promotion_identity("operations")
    _backfill_promotion_identity("events")
    _backfill_document_logical_key()

    # 5. the legacy ``source`` values that named the promotion path are recorded as such, so a
    #    promoted row from before this revision is distinguishable from a manual one afterwards.
    for legacy, kind in _SOURCE_KIND_BY_LEGACY:
        for table_name in ("operations", "events"):
            connection.execute(
                sa.text(
                    f"UPDATE {table_name} SET source_kind = :kind WHERE source = :legacy "
                    "AND source_kind = 'manual'"
                ),
                {"kind": kind, "legacy": legacy},
            )

    # 6. events whose classification came from the NPT code rather than from the report.
    connection.execute(
        sa.text(
            "UPDATE events SET classification_source = 'derived' "
            "WHERE npt_code IS NOT NULL AND is_npt = true AND classification_source = 'recorded'"
        )
    )

    # 7. an event that has a cause text but no basis recorded: the pre-existing rows came from a
    #    promotion that did not distinguish, and 'recorded' is the honest reading of a row promoted
    #    from a report's own words.
    connection.execute(
        sa.text(
            "UPDATE events SET cause_basis = 'recorded' "
            "WHERE root_cause IS NOT NULL AND cause_basis = 'unknown' "
            "AND source_kind = 'ddr_promotion'"
        )
    )


def downgrade() -> None:
    """Drop the new columns. The repairs are **not** reversed, for the same reason as the previous
    revision: restoring ``parsed`` and ``unvalidated`` would hand back a database whose values the
    application no longer recognises, and the next ``upgrade`` would repair them again; and the
    trajectory report rewrite cannot be inverted, because the report it replaced carried no trace of
    having been a bare string beyond the fact that it was not one."""
    for table_name in ("operations", "events"):
        with op.batch_alter_table(table_name, schema=None) as batch_op:
            batch_op.drop_index(f"ix_{table_name}_source_kind")
            batch_op.drop_index(f"ix_{table_name}_promotion_fingerprint")
            batch_op.drop_index(f"ix_{table_name}_source_record_id")
            batch_op.drop_column("source_kind")
            batch_op.drop_column("promotion_fingerprint")
            batch_op.drop_column("source_record_id")

    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_column("classification_source")
        batch_op.drop_column("cause_basis")

    with op.batch_alter_table("evidence_links", schema=None) as batch_op:
        batch_op.drop_column("quote_check")

    with op.batch_alter_table("extracted_records", schema=None) as batch_op:
        batch_op.drop_column("validation_rule")
        batch_op.drop_column("region_unknown")

    with op.batch_alter_table("documents", schema=None) as batch_op:
        batch_op.drop_index("ix_documents_logical_key")
        batch_op.drop_column("logical_key")
