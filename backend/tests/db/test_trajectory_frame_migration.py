"""Trajectory frame migration adds nullable provenance without guessing legacy references."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT_REVISION = "e13acb7d4f20"
HEAD_REVISION = "b83e4a6d1c29"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _columns(connection: sa.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(sa.text(f"PRAGMA table_info({table})"))}


def test_trajectory_frame_migration_adds_and_reverses_only_nullable_provenance_columns(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'trajectory-frame.db'}"
    config = _alembic_config(database_url)
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    trajectory_columns = {"azimuth_reference", "azimuth_reference_id"}
    station_columns = {
        "input_azimuth_deg",
        "input_azimuth_reference",
        "input_azimuth_reference_id",
    }
    try:
        command.upgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            assert not trajectory_columns & _columns(connection, "trajectories")
            assert not station_columns & _columns(connection, "trajectory_stations")
            connection.execute(
                sa.text(
                    "INSERT INTO trajectories "
                    "(wellbore_id, name, kind, version, datum, is_current, station_count, validation, id, org_id) "
                    "VALUES ('wb_legacy', 'Legacy survey', 'actual', 1, 'rkb', 1, 1, '{}', 'trj_legacy', 'org_legacy')"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO trajectory_stations "
                    "(trajectory_id, wellbore_id, station_index, md_si, inclination_deg, azimuth_deg, source, "
                    "magnetic_reference, grid_correction_deg, quality_flags, raw_source, id, org_id) "
                    "VALUES ('trj_legacy', 'wb_legacy', 0, 500, 12, 30, 'report', 'old magnetic label', 2.5, "
                    "'[]', '{\"legacy\":\"preserved\"}', 'sta_legacy', 'org_legacy')"
                )
            )

        command.upgrade(config, HEAD_REVISION)
        with engine.begin() as connection:
            assert trajectory_columns <= _columns(connection, "trajectories")
            assert station_columns <= _columns(connection, "trajectory_stations")
            assert "magnetic_reference" in _columns(connection, "trajectory_stations")
            trajectory = connection.execute(
                sa.text(
                    "SELECT datum, azimuth_reference, azimuth_reference_id FROM trajectories WHERE id='trj_legacy'"
                )
            ).one()
            station = connection.execute(
                sa.text(
                    "SELECT magnetic_reference, grid_correction_deg, input_azimuth_deg, "
                    "input_azimuth_reference, input_azimuth_reference_id, raw_source "
                    "FROM trajectory_stations WHERE id='sta_legacy'"
                )
            ).one()
            assert tuple(trajectory) == ("rkb", None, None)
            assert tuple(station[:5]) == ("old magnetic label", 2.5, None, None, None)
            assert station.raw_source == '{"legacy":"preserved"}'

        command.downgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            assert not trajectory_columns & _columns(connection, "trajectories")
            assert not station_columns & _columns(connection, "trajectory_stations")
            assert "magnetic_reference" in _columns(connection, "trajectory_stations")
            trajectory = connection.execute(
                sa.text("SELECT datum FROM trajectories WHERE id='trj_legacy'")
            ).scalar_one()
            station = connection.execute(
                sa.text(
                    "SELECT magnetic_reference, grid_correction_deg, raw_source "
                    "FROM trajectory_stations WHERE id='sta_legacy'"
                )
            ).one()
            assert trajectory == "rkb"
            assert tuple(station) == ("old magnetic label", 2.5, '{"legacy":"preserved"}')
    finally:
        engine.dispose()
