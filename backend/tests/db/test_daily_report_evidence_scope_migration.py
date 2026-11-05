"""Evidence-scope migration refuses ambiguous legacy links and enforces tenant/document identity."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "c2e7a1b9d608"
HEAD_REVISION = "d8f3b4c2a917"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_fixtures(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO organizations "
            "(id, slug, name, kind, timezone, default_unit_system, default_locale, settings, is_active) "
            "VALUES ('org_evidence', 'org-evidence', 'Evidence test organization', 'tenant', 'UTC', 'SI', 'en', '{}', 1)"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO organizations "
            "(id, slug, name, kind, timezone, default_unit_system, default_locale, settings, is_active) "
            "VALUES ('org_evidence_other', 'org-evidence-other', 'Other evidence organization', 'tenant', 'UTC', 'SI', 'en', '{}', 1)"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO projects (id, org_id, name, status, settings, datum_policy) "
            "VALUES ('prj_evidence', 'org_evidence', 'Evidence project', 'active', '{}', 'rkb')"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO wells "
            "(id, org_id, project_id, name, well_type, status, elevation_datum, is_offshore, twin_state, "
            "target_formations, tags, attributes, is_demo_fixture) "
            "VALUES ('wel_evidence', 'org_evidence', 'prj_evidence', 'Evidence well', "
            "'development_producer', 'planned', 'msl', 0, 'planned', '[]', '[]', '{}', 0)"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO wellbores "
            "(id, org_id, well_id, name, purpose, sequence, status, is_active, datum, attributes) "
            "VALUES ('wlb_evidence', 'org_evidence', 'wel_evidence', 'Evidence main bore', "
            "'original', 1, 'planned', 1, 'rkb', '{}')"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO daily_drilling_reports "
            "(id, org_id, well_id, wellbore_id, report_date, report_timezone, shift_code, identity_key, created_by) "
            "VALUES ('ddr_evidence', 'org_evidence', 'wel_evidence', 'wlb_evidence', "
            "'2026-11-04', 'UTC', 'day', 'shift:day:evidence', 'usr_evidence')"
        )
    )

    documents = (
        ("doc_evidence", "org_evidence", "Evidence source A"),
        ("doc_evidence_other", "org_evidence", "Evidence source B"),
        ("doc_evidence_foreign", "org_evidence_other", "Foreign tenant evidence"),
    )
    for document_id, org_id, title in documents:
        connection.execute(
            sa.text(
                "INSERT INTO documents "
                "(id, org_id, doc_type, title, page_count, status, extraction_summary, source_units, "
                "classification, tags, has_tables, has_figures, ocr_required, is_demo_fixture, attributes) "
                "VALUES (:id, :org_id, 'other', :title, 0, 'uploaded', '{}', '{}', 'internal', '[]', "
                "0, 0, 0, 0, '{}')"
            ),
            {"id": document_id, "org_id": org_id, "title": title},
        )

    records = (
        ("rec_evidence", "org_evidence", "doc_evidence"),
        ("rec_evidence_other", "org_evidence", "doc_evidence_other"),
        ("rec_evidence_foreign", "org_evidence_other", "doc_evidence_foreign"),
    )
    for record_id, org_id, document_id in records:
        connection.execute(
            sa.text(
                "INSERT INTO extracted_records "
                "(id, org_id, document_id, record_type, payload_schema_key, payload_schema_version, payload, "
                "method, validation_state, unit_context, quality_flags, is_demo_fixture, review_version, "
                "observed_at_basis, depth_md_basis, depth_tvd_basis) "
                "VALUES (:id, :org_id, :document_id, 'general', 'evidence.test', 1, '{}', 'manual', "
                "'extracted', '{}', '[]', 0, 1, 'unknown', 'unknown', 'unknown')"
            ),
            {"id": record_id, "org_id": org_id, "document_id": document_id},
        )

    regions = (
        ("rgn_evidence", "org_evidence", "doc_evidence"),
        ("rgn_evidence_other", "org_evidence", "doc_evidence_other"),
        ("rgn_evidence_foreign", "org_evidence_other", "doc_evidence_foreign"),
    )
    for region_id, org_id, document_id in regions:
        connection.execute(
            sa.text(
                "INSERT INTO document_regions "
                "(id, org_id, document_id, kind, order_index, bbox, attributes) "
                "VALUES (:id, :org_id, :document_id, 'paragraph', 0, '{}', '{}')"
            ),
            {"id": region_id, "org_id": org_id, "document_id": document_id},
        )

    connection.execute(
        sa.text(
            "INSERT INTO daily_drilling_report_revisions "
            "(id, org_id, report_id, revision_number, source_kind, source_document_id, created_by) "
            "VALUES ('drr_evidence', 'org_evidence', 'ddr_evidence', 1, 'import', "
            "'doc_evidence_foreign', 'usr_evidence')"
        )
    )
    for activity_id, sequence, source_document_id, source_record_id in (
        ("dra_evidence_document_mismatch", 0, "doc_evidence", "rec_evidence_other"),
        ("dra_evidence_foreign_record", 1, None, "rec_evidence_foreign"),
    ):
        connection.execute(
            sa.text(
                "INSERT INTO daily_report_activities "
                "(id, org_id, revision_id, sequence, activity_kind, title, source_document_id, source_record_id, "
                "start_time_basis, end_time_basis, duration_basis, depth_reference, depth_basis, cause_basis, "
                "resolution_state) "
                "VALUES (:id, 'org_evidence', 'drr_evidence', :sequence, 'operation', 'Evidence activity', "
                ":source_document_id, :source_record_id, 'unknown', 'unknown', 'unknown', 'unknown', 'unknown', "
                "'unknown', 'proposed')"
            ),
            {
                "id": activity_id,
                "sequence": sequence,
                "source_document_id": source_document_id,
                "source_record_id": source_record_id,
            },
        )

    connection.execute(
        sa.text(
            "INSERT INTO daily_report_measurements "
            "(id, org_id, revision_id, parameter_key, ordinal, dimension, canonical_unit, value_si, source_value, "
            "source_unit, accepted_input_value, accepted_input_unit, value_basis, time_basis, depth_reference, "
            "depth_basis, source_document_id, source_region_id, resolution_state) "
            "VALUES ('drm_evidence_region_mismatch', 'org_evidence', 'drr_evidence', 'mud_weight', 0, "
            "'mud_weight', 'kg/m3', 1000, 1, 'kg/m3', 1, 'kg/m3', 'reported', 'unknown', 'unknown', 'unknown', "
            "'doc_evidence', 'rgn_evidence_other', 'proposed')"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO daily_report_reconciliations "
            "(id, org_id, revision_id, target_key, target_kind, activity_target_id, measurement_target_id, "
            "field_key, source_document_id, source_record_id, source_region_id, source_path, source_review_version, "
            "source_fingerprint, source_value_hash, proposed_value_hash, resolution_state, created_by) "
            "VALUES ('drc_evidence_region_foreign', 'org_evidence', 'drr_evidence', 'report_identity:report_date', "
            "'report_identity', NULL, NULL, 'report_date', 'doc_evidence', 'rec_evidence', 'rgn_evidence_foreign', "
            "'payload.report_date', 1, :hash, :hash, :hash, 'proposed', 'usr_evidence')"
        ),
        {"hash": "a" * 64},
    )


def _assert_constraint_rejects(connection: sa.Connection, statement: str) -> None:
    with pytest.raises(sa.exc.IntegrityError), connection.begin():
        connection.execute(sa.text(statement))


def test_daily_report_evidence_scope_migration_preflights_and_enforces_links(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'daily-report-evidence-scope.db').as_posix()}"
    config = _alembic_config(database_url)
    command.upgrade(config, PREVIOUS_REVISION)

    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            _insert_fixtures(connection)

        with pytest.raises(
            RuntimeError, match="Refusing to add daily-report evidence scope constraints"
        ) as error:
            command.upgrade(config, HEAD_REVISION)
        message = str(error.value)
        assert "daily_drilling_report_revisions.source_document_tenant=1" in message
        assert "daily_report_activities.source_record_tenant=1" in message
        assert "daily_report_activities.source_record_document=1" in message
        assert "daily_report_measurements.source_region_document=1" in message
        assert "daily_report_reconciliations.source_region_tenant=1" in message
        assert "daily_report_reconciliations.source_region_document=1" in message

        with engine.begin() as connection:
            assert (
                connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
                == PREVIOUS_REVISION
            )
            connection.execute(
                sa.text(
                    "UPDATE daily_drilling_report_revisions SET source_document_id='doc_evidence' "
                    "WHERE id='drr_evidence'"
                )
            )
            connection.execute(
                sa.text(
                    "UPDATE daily_report_activities SET source_document_id='doc_evidence_other' "
                    "WHERE id='dra_evidence_document_mismatch'"
                )
            )
            connection.execute(
                sa.text("DELETE FROM daily_report_activities WHERE id='dra_evidence_foreign_record'")
            )
            connection.execute(
                sa.text(
                    "UPDATE daily_report_measurements SET source_document_id='doc_evidence_other' "
                    "WHERE id='drm_evidence_region_mismatch'"
                )
            )
            connection.execute(
                sa.text(
                    "UPDATE daily_report_reconciliations SET source_region_id='rgn_evidence' "
                    "WHERE id='drc_evidence_region_foreign'"
                )
            )

        command.upgrade(config, HEAD_REVISION)
        with engine.connect() as connection:
            assert (
                connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
                == HEAD_REVISION
            )
            inspector = sa.inspect(connection)
            expected_uniques = {
                "documents": {"uq_documents_org_id_id"},
                "document_regions": {
                    "uq_document_regions_org_id_id",
                    "uq_document_regions_document_id_id",
                },
                "extracted_records": {
                    "uq_extracted_records_org_id_id",
                    "uq_extracted_records_document_id_id",
                },
            }
            for table, names in expected_uniques.items():
                assert names <= {item["name"] for item in inspector.get_unique_constraints(table)}

            prefixes = {
                "daily_drilling_report_revisions": "daily_report_revision",
                "daily_report_activities": "daily_report_activity",
                "daily_report_measurements": "daily_report_measurement",
                "daily_report_reconciliations": "daily_report_reconciliation",
            }
            for table, prefix in prefixes.items():
                foreign_keys = {item["name"] for item in inspector.get_foreign_keys(table)}
                assert {
                    f"fk_{prefix}_source_document_tenant",
                    f"fk_{prefix}_source_record_tenant",
                    f"fk_{prefix}_source_record_document",
                    f"fk_{prefix}_source_region_tenant",
                    f"fk_{prefix}_source_region_document",
                } <= foreign_keys
                checks = {item["name"] for item in inspector.get_check_constraints(table)}
                assert f"ck_{table}_{prefix}_source_region_requires_document" in checks
                assert f"ck_{table}_{prefix}_source_page_requires_document" in checks

        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            connection.commit()

            _assert_constraint_rejects(
                connection,
                "UPDATE daily_drilling_report_revisions SET source_document_id='doc_evidence_foreign' "
                "WHERE id='drr_evidence'",
            )
            _assert_constraint_rejects(
                connection,
                "UPDATE daily_report_activities SET source_document_id=NULL, "
                "source_record_id='rec_evidence_foreign' WHERE id='dra_evidence_document_mismatch'",
            )
            _assert_constraint_rejects(
                connection,
                "UPDATE daily_report_activities SET source_document_id='doc_evidence', "
                "source_record_id='rec_evidence_other' WHERE id='dra_evidence_document_mismatch'",
            )
            _assert_constraint_rejects(
                connection,
                "UPDATE daily_report_measurements SET source_region_id='rgn_evidence_foreign' "
                "WHERE id='drm_evidence_region_mismatch'",
            )
            _assert_constraint_rejects(
                connection,
                "UPDATE daily_report_measurements SET source_document_id=NULL "
                "WHERE id='drm_evidence_region_mismatch'",
            )
            _assert_constraint_rejects(
                connection,
                "UPDATE daily_drilling_report_revisions SET source_document_id=NULL, source_page_number=1 "
                "WHERE id='drr_evidence'",
            )

        command.downgrade(config, PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert (
                connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
                == PREVIOUS_REVISION
            )
            assert (
                connection.execute(sa.text("SELECT COUNT(*) FROM daily_report_reconciliations")).scalar_one()
                == 1
            )
            assert "uq_documents_org_id_id" not in {
                item["name"] for item in sa.inspect(connection).get_unique_constraints("documents")
            }

        command.upgrade(config, HEAD_REVISION)
        with engine.connect() as connection:
            assert (
                connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
                == HEAD_REVISION
            )
    finally:
        engine.dispose()
