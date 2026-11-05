"""Revision pointers and supersession must remain within one report and organization."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_REVISION = "d8f3b4c2a917"
HEAD_REVISION = "e2b4c6d8f013"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_scope_fixture(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO organizations "
            "(id, slug, name, kind, timezone, default_unit_system, default_locale, settings, is_active) "
            "VALUES ('org_lineage', 'org-lineage', 'Revision lineage test organization', 'tenant', 'UTC', 'SI', 'en', '{}', 1)"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO projects (id, org_id, name, status, settings, datum_policy) "
            "VALUES ('prj_lineage', 'org_lineage', 'Lineage project', 'active', '{}', 'rkb')"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO wells "
            "(id, org_id, project_id, name, well_type, status, elevation_datum, is_offshore, twin_state, "
            "target_formations, tags, attributes, is_demo_fixture) "
            "VALUES ('wel_lineage', 'org_lineage', 'prj_lineage', 'Lineage well', 'development_producer', "
            "'planned', 'msl', 0, 'planned', '[]', '[]', '{}', 0)"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO wellbores "
            "(id, org_id, well_id, name, purpose, sequence, status, is_active, datum, attributes) "
            "VALUES ('wlb_lineage', 'org_lineage', 'wel_lineage', 'Lineage main bore', 'original', 1, "
            "'planned', 1, 'rkb', '{}')"
        )
    )


def _insert_report(
    connection: sa.Connection,
    *,
    report_id: str,
    report_date: str,
    latest_revision_number: int,
    latest_approved_revision_number: int | None = None,
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO daily_drilling_reports "
            "(id, org_id, well_id, wellbore_id, report_date, report_timezone, shift_code, identity_key, "
            "latest_revision_number, latest_approved_revision_number, created_by) "
            "VALUES (:id, 'org_lineage', 'wel_lineage', 'wlb_lineage', :report_date, 'UTC', 'day', "
            ":identity_key, :latest_revision_number, :latest_approved_revision_number, 'usr_lineage')"
        ),
        {
            "id": report_id,
            "report_date": report_date,
            "identity_key": f"shift:day:{report_id}",
            "latest_revision_number": latest_revision_number,
            "latest_approved_revision_number": latest_approved_revision_number,
        },
    )


def _insert_revision(
    connection: sa.Connection,
    *,
    revision_id: str,
    report_id: str,
    revision_number: int,
    supersedes_revision_id: str | None = None,
    status: str = "draft",
) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO daily_drilling_report_revisions "
            "(id, org_id, report_id, revision_number, status, source_kind, supersedes_revision_id, created_by) "
            "VALUES (:id, 'org_lineage', :report_id, :revision_number, :status, 'manual', "
            ":supersedes_revision_id, 'usr_lineage')"
        ),
        {
            "id": revision_id,
            "report_id": report_id,
            "revision_number": revision_number,
            "status": status,
            "supersedes_revision_id": supersedes_revision_id,
        },
    )


def _assert_constraint_rejects(connection: sa.Connection, statement: str) -> None:
    with pytest.raises(sa.exc.IntegrityError), connection.begin():
        connection.execute(sa.text(statement))


def test_daily_report_revision_lineage_migration_preflights_and_enforces_scope(tmp_path: Path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'daily-report-revision-lineage.db').as_posix()}"
    config = _alembic_config(database_url)
    command.upgrade(config, PREVIOUS_REVISION)

    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            _insert_scope_fixture(connection)
            _insert_report(
                connection,
                report_id="ddr_lineage_a",
                report_date="2026-11-01",
                latest_revision_number=2,
                latest_approved_revision_number=7,
            )
            _insert_report(
                connection,
                report_id="ddr_lineage_b",
                report_date="2026-11-02",
                latest_revision_number=1,
            )
            _insert_report(
                connection,
                report_id="ddr_lineage_missing",
                report_date="2026-11-03",
                latest_revision_number=9,
            )
            _insert_revision(
                connection,
                revision_id="drr_lineage_a1",
                report_id="ddr_lineage_a",
                revision_number=1,
            )
            _insert_revision(
                connection,
                revision_id="drr_lineage_b1",
                report_id="ddr_lineage_b",
                revision_number=1,
            )
            _insert_revision(
                connection,
                revision_id="drr_lineage_a2",
                report_id="ddr_lineage_a",
                revision_number=2,
                supersedes_revision_id="drr_lineage_b1",
            )

        with pytest.raises(RuntimeError, match="Refusing to add daily-report revision lineage constraints") as error:
            command.upgrade(config, HEAD_REVISION)
        message = str(error.value)
        assert "latest_revision_pointer=1" in message
        assert "latest_approved_revision_pointer=1" in message
        assert "supersedes_scope=1" in message

        with engine.begin() as connection:
            assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == PREVIOUS_REVISION
            _insert_revision(
                connection,
                revision_id="drr_lineage_missing1",
                report_id="ddr_lineage_missing",
                revision_number=1,
            )
            connection.execute(
                sa.text(
                    "UPDATE daily_drilling_reports SET latest_revision_number=1 "
                    "WHERE id='ddr_lineage_missing'"
                )
            )
            connection.execute(
                sa.text(
                    "UPDATE daily_drilling_report_revisions SET status='approved' "
                    "WHERE id='drr_lineage_a1'"
                )
            )
            connection.execute(
                sa.text(
                    "UPDATE daily_drilling_reports SET latest_approved_revision_number=1 "
                    "WHERE id='ddr_lineage_a'"
                )
            )
            connection.execute(
                sa.text(
                    "UPDATE daily_drilling_report_revisions SET supersedes_revision_id='drr_lineage_a1' "
                    "WHERE id='drr_lineage_a2'"
                )
            )

        command.upgrade(config, HEAD_REVISION)
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD_REVISION
            inspector = sa.inspect(connection)
            assert {
                "uq_daily_report_revision_org_report_number",
                "uq_daily_report_revision_org_report_id",
            } <= {
                item["name"]
                for item in inspector.get_unique_constraints("daily_drilling_report_revisions")
            }
            revision_fks = {item["name"] for item in inspector.get_foreign_keys("daily_drilling_report_revisions")}
            assert "fk_daily_report_revision_supersedes_scope" in revision_fks
            report_fks = {item["name"] for item in inspector.get_foreign_keys("daily_drilling_reports")}
            assert {
                "fk_daily_drilling_reports_latest_revision_scope",
                "fk_daily_drilling_reports_latest_approved_revision_scope",
            } <= report_fks

        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.commit()
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            connection.commit()

            _assert_constraint_rejects(
                connection,
                "UPDATE daily_drilling_reports SET latest_revision_number=99 WHERE id='ddr_lineage_a'",
            )
            _assert_constraint_rejects(
                connection,
                "UPDATE daily_drilling_reports SET latest_approved_revision_number=99 WHERE id='ddr_lineage_a'",
            )
            _assert_constraint_rejects(
                connection,
                "UPDATE daily_drilling_report_revisions SET supersedes_revision_id='drr_lineage_b1' "
                "WHERE id='drr_lineage_a2'",
            )

        command.downgrade(config, PREVIOUS_REVISION)
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == PREVIOUS_REVISION
            assert connection.execute(
                sa.text("SELECT supersedes_revision_id FROM daily_drilling_report_revisions WHERE id='drr_lineage_a2'")
            ).scalar_one() == "drr_lineage_a1"
            assert connection.execute(
                sa.text("SELECT latest_revision_number FROM daily_drilling_reports WHERE id='ddr_lineage_a'")
            ).scalar_one() == 2

        command.upgrade(config, HEAD_REVISION)
        with engine.connect() as connection:
            assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one() == HEAD_REVISION
    finally:
        engine.dispose()
