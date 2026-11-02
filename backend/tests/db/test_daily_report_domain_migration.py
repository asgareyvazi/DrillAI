"""Additive DDR migration, schema drift and data-preserving rollback guard."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT_REVISION = "da0881aa121d"
HEAD_REVISION = "e13acb7d4f20"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_daily_report_migration_creates_typed_schema_and_refuses_data_loss_on_downgrade(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'daily-report-migration.db'}"
    config = _alembic_config(database_url)
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        command.upgrade(config, PARENT_REVISION)
        command.upgrade(config, HEAD_REVISION)
        with engine.begin() as connection:
            table_names = set(sa.inspect(connection).get_table_names())
            assert {
                "daily_drilling_reports",
                "daily_drilling_report_revisions",
                "daily_report_activities",
                "daily_report_measurements",
            } <= table_names
            measurement_columns = {
                column["name"] for column in sa.inspect(connection).get_columns("daily_report_measurements")
            }
            assert {
                "value_si",
                "source_value",
                "source_unit",
                "accepted_input_value",
                "accepted_input_unit",
                "time_basis",
                "depth_reference",
                "resolution_state",
            } <= measurement_columns
            connection.execute(
                sa.text(
                    "INSERT INTO daily_drilling_reports "
                    "(id, org_id, well_id, wellbore_id, report_date, report_timezone, shift_code, "
                    "identity_key, created_by) VALUES "
                    "('ddr_migration_row', 'org_migration', 'wel_migration', 'wlb_migration', "
                    "'2026-11-01', 'America/Denver', 'day', 'shift:day', 'usr_migration')"
                )
            )

        with pytest.raises(RuntimeError, match="daily_drilling_reports contains 1 row"):
            command.downgrade(config, PARENT_REVISION)

        with engine.begin() as connection:
            connection.execute(
                sa.text("DELETE FROM daily_drilling_reports WHERE id='ddr_migration_row'")
            )
        command.downgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            assert "daily_drilling_reports" not in set(sa.inspect(connection).get_table_names())

        command.upgrade(config, HEAD_REVISION)
        command.check(config)
        with engine.begin() as connection:
            assert "daily_report_measurements" in set(sa.inspect(connection).get_table_names())
    finally:
        engine.dispose()
