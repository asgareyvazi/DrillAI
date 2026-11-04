"""Report identity and asset-scope migrations preserve data and reject ambiguous legacy rows."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT_REVISION = "e90f1c8a2b64"
IDENTITY_REVISION = "f0a4b8c1d2e3"
SCOPE_REVISION = "a6f4c2d8e901"
TARGET_SCOPE_REVISION = "c2e7a1b9d608"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_reconciliation(
    connection: sa.Connection,
    *,
    target_kind: str,
    field_key: str,
    target_id: str | None = None,
    suffix: str | None = None,
    org_id: str = "org_identity",
    revision_id: str = "drr_identity",
) -> None:
    id_suffix = f"_{suffix}" if suffix else ""
    key_suffix = f":{suffix}" if suffix else ""
    connection.execute(
        sa.text(
            "INSERT INTO daily_report_reconciliations "
            "(id, org_id, revision_id, target_key, target_kind, target_id, field_key, "
            "source_document_id, source_record_id, source_region_id, source_page_number, source_path, "
            "source_review_version, source_fingerprint, source_value_hash, proposed_value_hash, "
            "resolution_state, reason, decided_by, decided_at, created_by) VALUES "
            "(:id, :org_id, :revision_id, :target_key, :target_kind, :target_id, :field_key, "
            "'doc_identity', 'rec_identity', NULL, 1, 'payload.report_date', 1, :hash, :hash, :hash, "
            "'proposed', NULL, NULL, NULL, 'usr_identity')"
        ),
        {
            "id": f"drc_{target_kind}_{field_key}{id_suffix}",
            "target_key": f"{target_kind}:{field_key}{key_suffix}",
            "target_kind": target_kind,
            "field_key": field_key,
            "target_id": target_id,
            "org_id": org_id,
            "revision_id": revision_id,
            "hash": "a" * 64,
        },
    )


def _insert_scope_fixture(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO organizations "
            "(id, slug, name, kind, timezone, default_unit_system, default_locale, settings, is_active) "
            "VALUES ('org_scope', 'org-scope', 'Scope test organization', 'tenant', 'UTC', 'SI', 'en', '{}', 1)"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO organizations "
            "(id, slug, name, kind, timezone, default_unit_system, default_locale, settings, is_active) "
            "VALUES ('org_scope_other', 'org-scope-other', 'Other scope organization', 'tenant', 'UTC', 'SI', 'en', '{}', 1)"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO projects (id, org_id, name, status, settings, datum_policy) "
            "VALUES ('prj_scope', 'org_scope', 'Scope test project', 'active', '{}', 'rkb')"
        )
    )
    for well_id, name in (("wel_scope_a", "Scope well A"), ("wel_scope_b", "Scope well B")):
        connection.execute(
            sa.text(
                "INSERT INTO wells "
                "(id, org_id, project_id, name, well_type, status, elevation_datum, is_offshore, twin_state, "
                "target_formations, tags, attributes, is_demo_fixture) "
                "VALUES (:id, 'org_scope', 'prj_scope', :name, 'development_producer', 'planned', 'msl', 0, "
                "'planned', '[]', '[]', '{}', 0)"
            ),
            {"id": well_id, "name": name},
        )
        connection.execute(
            sa.text(
                "INSERT INTO wellbores "
                "(id, org_id, well_id, name, purpose, sequence, status, is_active, datum, attributes) "
                "VALUES (:id, 'org_scope', :well_id, :name, 'original', 1, 'planned', 1, 'rkb', '{}')"
            ),
            {"id": f"wlb_{well_id[-1]}", "well_id": well_id, "name": f"{name} main bore"},
        )


def _insert_scope_report(
    connection: sa.Connection, *, report_id: str, well_id: str, wellbore_id: str, report_date: str
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO daily_drilling_reports "
            "(id, org_id, well_id, wellbore_id, report_date, report_timezone, shift_code, identity_key, created_by) "
            "VALUES (:report_id, 'org_scope', :well_id, :wellbore_id, :report_date, 'UTC', 'day', :identity_key, 'usr_scope')"
        ),
        {
            "report_id": report_id,
            "well_id": well_id,
            "wellbore_id": wellbore_id,
            "report_date": report_date,
            "identity_key": f"shift:day:{report_id}",
        },
    )


def _insert_revision_target_rows(
    connection: sa.Connection,
    *,
    report_id: str,
    revision_id: str,
    activity_id: str,
    measurement_id: str | None = None,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO daily_drilling_report_revisions "
            "(id, org_id, report_id, revision_number, source_kind, created_by) "
            "VALUES (:revision_id, 'org_scope', :report_id, 1, 'import', 'usr_scope')"
        ),
        {"revision_id": revision_id, "report_id": report_id},
    )
    connection.execute(
        sa.text(
            "INSERT INTO daily_report_activities "
            "(id, org_id, revision_id, sequence, activity_kind, title, start_time_basis, end_time_basis, "
            "duration_basis, depth_reference, depth_basis, cause_basis, resolution_state) "
            "VALUES (:activity_id, 'org_scope', :revision_id, 0, 'operation', 'Synthetic target activity', "
            "'unknown', 'unknown', 'unknown', 'unknown', 'unknown', 'unknown', 'proposed')"
        ),
        {"activity_id": activity_id, "revision_id": revision_id},
    )
    if measurement_id is not None:
        connection.execute(
            sa.text(
                "INSERT INTO daily_report_measurements "
                "(id, org_id, revision_id, parameter_key, ordinal, dimension, canonical_unit, value_si, "
                "source_value, source_unit, accepted_input_value, accepted_input_unit, value_basis, "
                "time_basis, depth_reference, depth_basis, resolution_state) "
                "VALUES (:measurement_id, 'org_scope', :revision_id, 'mud_weight', 0, 'mud_weight', "
                "'kg/m3', 1101.26, 9.2, 'ppg', 9.2, 'ppg', 'reported', 'unknown', 'unknown', 'unknown', 'proposed')"
            ),
            {"measurement_id": measurement_id, "revision_id": revision_id},
        )


def test_identity_reconciliation_migration_constraints_and_loss_protection(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'daily-report-identity-reconciliation.db'}"
    config = _alembic_config(database_url)
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        command.upgrade(config, PARENT_REVISION)
        command.upgrade(config, IDENTITY_REVISION)
        with engine.begin() as connection:
            _insert_scope_fixture(connection)
            connection.execute(
                sa.text(
                    "INSERT INTO wellbores "
                    "(id, org_id, well_id, name, purpose, sequence, status, is_active, datum, attributes) "
                    "VALUES ('wlb_wrong_org', 'org_scope_other', 'wel_scope_a', 'Wrong organization bore', "
                    "'sidetrack', 2, 'planned', 0, 'rkb', '{}')"
                )
            )
            _insert_scope_report(
                connection,
                report_id="ddr_wrong_bore",
                well_id="wel_scope_a",
                wellbore_id="wlb_b",
                report_date="2026-11-01",
            )

        with pytest.raises(RuntimeError, match="Refusing to add organization/well scope constraints"):
            command.upgrade(config, SCOPE_REVISION)
        with engine.begin() as connection:
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == IDENTITY_REVISION
            assert connection.execute(
                sa.text("SELECT COUNT(*) FROM wellbores WHERE id='wlb_wrong_org'")
            ).scalar_one() == 1
            assert connection.execute(
                sa.text("SELECT COUNT(*) FROM daily_drilling_reports WHERE id='ddr_wrong_bore'")
            ).scalar_one() == 1
            connection.execute(sa.text("DELETE FROM daily_drilling_reports WHERE id='ddr_wrong_bore'"))
            connection.execute(sa.text("DELETE FROM wellbores WHERE id='wlb_wrong_org'"))

        command.upgrade(config, SCOPE_REVISION)
        with engine.begin() as connection:
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == SCOPE_REVISION
            _insert_scope_report(
                connection,
                report_id="ddr_target_a",
                well_id="wel_scope_a",
                wellbore_id="wlb_a",
                report_date="2026-11-04",
            )
            _insert_scope_report(
                connection,
                report_id="ddr_target_b",
                well_id="wel_scope_a",
                wellbore_id="wlb_a",
                report_date="2026-11-05",
            )
            _insert_revision_target_rows(
                connection,
                report_id="ddr_target_a",
                revision_id="drr_target_a",
                activity_id="dra_target_a",
                measurement_id="drm_target_a",
            )
            _insert_revision_target_rows(
                connection,
                report_id="ddr_target_b",
                revision_id="drr_target_b",
                activity_id="dra_target_b",
            )
            _insert_reconciliation(
                connection,
                target_kind="activity",
                field_key="activity",
                target_id="dra_target_a",
                suffix="valid_activity",
                org_id="org_scope",
                revision_id="drr_target_a",
            )
            _insert_reconciliation(
                connection,
                target_kind="measurement",
                field_key="mud_weight",
                target_id="drm_target_a",
                suffix="valid_measurement",
                org_id="org_scope",
                revision_id="drr_target_a",
            )
            _insert_reconciliation(
                connection,
                target_kind="activity",
                field_key="activity",
                target_id="dra_target_b",
                suffix="cross_revision_activity",
                org_id="org_scope",
                revision_id="drr_target_a",
            )
            _insert_reconciliation(
                connection,
                target_kind="measurement",
                field_key="mud_weight",
                target_id="drm_missing",
                suffix="orphan_measurement",
                org_id="org_scope",
                revision_id="drr_target_a",
            )
            _insert_reconciliation(connection, target_kind="report_identity", field_key="report_date")
            with pytest.raises(sa.exc.IntegrityError):
                _insert_reconciliation(connection, target_kind="report_identity", field_key="wellbore_id")

        with pytest.raises(RuntimeError, match="Refusing to add daily-report reconciliation target-scope constraints"):
            command.upgrade(config, TARGET_SCOPE_REVISION)
        with engine.begin() as connection:
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == SCOPE_REVISION
            assert connection.execute(
                sa.text(
                    "SELECT target_id FROM daily_report_reconciliations "
                    "WHERE id='drc_activity_activity_cross_revision_activity'"
                )
            ).scalar_one() == "dra_target_b"
            assert connection.execute(
                sa.text("SELECT COUNT(*) FROM daily_report_reconciliations")
            ).scalar_one() == 5
            connection.execute(
                sa.text(
                    "DELETE FROM daily_report_reconciliations "
                    "WHERE id IN ('drc_activity_activity_cross_revision_activity', "
                    "'drc_measurement_mud_weight_orphan_measurement')"
                )
            )
            assert connection.execute(
                sa.text("SELECT COUNT(*) FROM daily_report_reconciliations")
            ).scalar_one() == 3

        command.upgrade(config, TARGET_SCOPE_REVISION)
        with engine.begin() as connection:
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == TARGET_SCOPE_REVISION
            columns = {
                column["name"]
                for column in sa.inspect(connection).get_columns("daily_report_reconciliations")
            }
            assert "target_id" not in columns
            assert {"activity_target_id", "measurement_target_id"} <= columns
            assert connection.execute(
                sa.text(
                    "SELECT activity_target_id FROM daily_report_reconciliations "
                    "WHERE id='drc_activity_activity_valid_activity'"
                )
            ).scalar_one() == "dra_target_a"
            assert connection.execute(
                sa.text(
                    "SELECT measurement_target_id FROM daily_report_reconciliations "
                    "WHERE id='drc_measurement_mud_weight_valid_measurement'"
                )
            ).scalar_one() == "drm_target_a"
            assert connection.execute(
                sa.text(
                    "SELECT activity_target_id, measurement_target_id FROM daily_report_reconciliations "
                    "WHERE id='drc_report_identity_report_date'"
                )
            ).one() == (None, None)

        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            with pytest.raises(sa.exc.IntegrityError), connection.begin():
                connection.execute(
                    sa.text(
                        "UPDATE daily_report_reconciliations SET activity_target_id='dra_target_b' "
                        "WHERE id='drc_activity_activity_valid_activity'"
                    )
                )
            with pytest.raises(sa.exc.IntegrityError), connection.begin():
                connection.execute(
                    sa.text(
                        "UPDATE daily_report_reconciliations SET measurement_target_id='drm_missing' "
                        "WHERE id='drc_measurement_mud_weight_valid_measurement'"
                    )
                )

        command.downgrade(config, SCOPE_REVISION)
        with engine.begin() as connection:
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == SCOPE_REVISION
            assert "target_id" in {
                column["name"]
                for column in sa.inspect(connection).get_columns("daily_report_reconciliations")
            }
            assert connection.execute(
                sa.text(
                    "SELECT target_id FROM daily_report_reconciliations "
                    "WHERE id='drc_activity_activity_valid_activity'"
                )
            ).scalar_one() == "dra_target_a"

        command.upgrade(config, TARGET_SCOPE_REVISION)
        with pytest.raises(RuntimeError, match="Refusing to remove report-identity reconciliation support"):
            command.downgrade(config, PARENT_REVISION)

        with engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM daily_report_reconciliations WHERE target_kind='report_identity'"))
        command.downgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == PARENT_REVISION
            with pytest.raises(sa.exc.IntegrityError):
                _insert_reconciliation(connection, target_kind="report_identity", field_key="report_date")

        command.upgrade(config, TARGET_SCOPE_REVISION)
        with engine.begin() as connection:
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == TARGET_SCOPE_REVISION
            connection.execute(sa.text("DELETE FROM daily_report_reconciliations"))

        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            connection.commit()
            with connection.begin():
                _insert_scope_report(
                    connection,
                    report_id="ddr_valid_scope",
                    well_id="wel_scope_a",
                    wellbore_id="wlb_a",
                    report_date="2026-11-02",
                )
            with pytest.raises(sa.exc.IntegrityError), connection.begin():
                _insert_scope_report(
                    connection,
                    report_id="ddr_wrong_scope",
                    well_id="wel_scope_a",
                    wellbore_id="wlb_b",
                    report_date="2026-11-03",
                )
            with pytest.raises(sa.exc.IntegrityError), connection.begin():
                connection.execute(
                    sa.text(
                        "INSERT INTO wellbores "
                        "(id, org_id, well_id, name, purpose, sequence, status, is_active, datum, attributes) "
                        "VALUES ('wlb_post_migration_wrong_org', 'org_scope_other', 'wel_scope_a', "
                        "'Wrong organization bore after migration', 'sidetrack', 2, 'planned', 0, 'rkb', '{}')"
                    )
                )
    finally:
        engine.dispose()
