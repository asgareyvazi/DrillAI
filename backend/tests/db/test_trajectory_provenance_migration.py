"""Legacy survey datum and station timestamps stay unverified across migration boundaries."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT_REVISION = "b83e4a6d1c29"
HEAD_REVISION = "c79f2a61d5b3"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _json_value(value):
    return json.loads(value) if isinstance(value, str) else value


def test_legacy_datum_and_station_time_are_preserved_but_not_trusted(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'trajectory-provenance.db'}"
    config = _alembic_config(database_url)
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    original_validation = {"is_valid": True, "issues": []}
    original_raw_source = {"legacy": "preserved"}
    original_quality_flags = ["legacy_flag"]
    original_measured_at = "2026-03-15 00:00:00"
    try:
        command.upgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO trajectories "
                    "(wellbore_id, name, kind, version, datum, is_current, station_count, validation, "
                    "azimuth_reference, azimuth_reference_id, id, org_id) "
                    "VALUES ('wb_legacy', 'Legacy survey', 'actual', 1, 'rkb', 1, 1, :validation, "
                    "NULL, NULL, 'trj_legacy', 'org_legacy')"
                ),
                {"validation": json.dumps(original_validation)},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO trajectory_stations "
                    "(trajectory_id, wellbore_id, station_index, md_si, inclination_deg, azimuth_deg, source, "
                    "measured_at, quality_flags, raw_source, id, org_id) "
                    "VALUES ('trj_legacy', 'wb_legacy', 0, 500, 12, 30, 'report', :measured_at, "
                    ":quality_flags, :raw_source, 'sta_legacy', 'org_legacy')"
                ),
                {
                    "measured_at": original_measured_at,
                    "quality_flags": json.dumps(original_quality_flags),
                    "raw_source": json.dumps(original_raw_source),
                },
            )

        command.upgrade(config, HEAD_REVISION)
        with engine.begin() as connection:
            trajectory = connection.execute(
                sa.text(
                    "SELECT datum, datum_provenance, azimuth_reference, azimuth_reference_id, validation "
                    "FROM trajectories WHERE id='trj_legacy'"
                )
            ).one()
            validation = _json_value(trajectory.validation)
            assert tuple(trajectory[:4]) == ("rkb", "legacy_unverified", None, None)
            assert validation["is_valid"] is False
            assert validation["state"] == "legacy_frame_unclassified"
            assert validation["datum_provenance"] == "legacy_unverified"
            assert validation["legacy_datum_value"] == "rkb"
            assert validation["legacy_validation_before_datum_provenance"] == original_validation
            assert any(issue["code"] == "legacy_frame_unclassified" for issue in validation["issues"])

            station = connection.execute(
                sa.text(
                    "SELECT measured_at, measured_at_provenance, quality_flags, raw_source "
                    "FROM trajectory_stations WHERE id='sta_legacy'"
                )
            ).one()
            flags = _json_value(station.quality_flags)
            raw_source = _json_value(station.raw_source)
            assert station.measured_at is None
            assert station.measured_at_provenance == "legacy_unverified"
            assert flags == [*original_quality_flags, "legacy_measured_at_provenance_unverified"]
            assert raw_source["legacy"] == "preserved"
            assert raw_source["legacy_measured_at_provenance_backup"]["measured_at"] == "2026-03-15T00:00:00"
            assert raw_source["legacy_measured_at_provenance_backup"]["raw_source"] == original_raw_source
            assert raw_source["legacy_measured_at_provenance_backup"]["quality_flags"] == original_quality_flags

            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    sa.text("UPDATE trajectories SET datum_provenance='unrecognised' WHERE id='trj_legacy'")
                )

        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE trajectories SET datum_provenance='source_document' WHERE id='trj_legacy'")
            )
        with pytest.raises(RuntimeError, match="post-migration datum provenance"):
            command.downgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE trajectories SET datum_provenance='legacy_unverified' WHERE id='trj_legacy'")
            )
            connection.execute(
                sa.text(
                    "INSERT INTO trajectory_stations "
                    "(trajectory_id, wellbore_id, station_index, md_si, inclination_deg, azimuth_deg, source, "
                    "measured_at, measured_at_provenance, quality_flags, raw_source, id, org_id) "
                    "VALUES ('trj_legacy', 'wb_legacy', 1, 600, 13, 31, 'report', :measured_at, "
                    "'source_record', :quality_flags, :raw_source, 'sta_new_source_time', 'org_legacy')"
                ),
                {
                    "measured_at": "2026-03-16 00:00:00",
                    "quality_flags": json.dumps([]),
                    "raw_source": json.dumps({"source": "post-migration"}),
                },
            )
        with pytest.raises(RuntimeError, match="source-record measurement time"):
            command.downgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            # The downgrade preflights all rows before altering SQLite's non-transactional DDL.
            assert connection.execute(
                sa.text("SELECT datum_provenance FROM trajectories WHERE id='trj_legacy'")
            ).scalar_one() == "legacy_unverified"
            assert connection.execute(
                sa.text("SELECT measured_at_provenance FROM trajectory_stations WHERE id='sta_legacy'")
            ).scalar_one() == "legacy_unverified"
            assert "datum_provenance" in {
                row[1] for row in connection.execute(sa.text("PRAGMA table_info(trajectories)"))
            }
            connection.execute(sa.text("DELETE FROM trajectory_stations WHERE id='sta_new_source_time'"))

        command.downgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            trajectory = connection.execute(
                sa.text("SELECT datum, validation FROM trajectories WHERE id='trj_legacy'")
            ).one()
            assert trajectory.datum == "rkb"
            assert _json_value(trajectory.validation) == original_validation

            station = connection.execute(
                sa.text("SELECT measured_at, quality_flags, raw_source FROM trajectory_stations WHERE id='sta_legacy'")
            ).one()
            assert dt.datetime.fromisoformat(station.measured_at) == dt.datetime.fromisoformat(original_measured_at)
            assert _json_value(station.quality_flags) == original_quality_flags
            assert _json_value(station.raw_source) == original_raw_source
            trajectory_columns = {
                row[1] for row in connection.execute(sa.text("PRAGMA table_info(trajectories)"))
            }
            station_columns = {
                row[1] for row in connection.execute(sa.text("PRAGMA table_info(trajectory_stations)"))
            }
            assert "datum_provenance" not in trajectory_columns
            assert "measured_at_provenance" not in station_columns
    finally:
        engine.dispose()
