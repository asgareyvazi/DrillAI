"""Trajectory revision identity constraints refuse ambiguous legacy history without rewriting it."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT_REVISION = "e2b4c6d8f013"
HEAD_REVISION = "f29a3c8d7e10"


def _config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_trajectory(connection: sa.Connection, *, trajectory_id: str, name: str, version: int = 1) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO trajectories "
            "(wellbore_id, name, kind, version, datum, is_current, station_count, validation, id, org_id) "
            "VALUES ('wb_revision_test', :name, 'actual', :version, 'unknown', 1, 0, '{}', :id, 'org_revision_test')"
        ),
        {"id": trajectory_id, "name": name, "version": version},
    )


def _current_revision(connection: sa.Connection) -> str:
    return connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()


def test_revision_migration_preserves_legacy_rows_and_guards_new_identity(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'trajectory-revision.db'}"
    config = _config(database_url)
    engine = sa.create_engine(database_url)
    try:
        command.upgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            _insert_trajectory(connection, trajectory_id="trj_legacy", name="Legacy actual")

        command.upgrade(config, HEAD_REVISION)
        with engine.begin() as connection:
            assert _current_revision(connection) == HEAD_REVISION
            row = connection.execute(
                sa.text("SELECT version, is_current, revision_reason FROM trajectories WHERE id='trj_legacy'")
            ).one()
            assert tuple(row) == (1, 1, None)

            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    sa.text(
                        "INSERT INTO trajectories "
                        "(wellbore_id, name, kind, version, datum, is_current, station_count, validation, id, org_id) "
                        "VALUES ('wb_revision_test', 'Legacy actual', 'actual', 1, 'unknown', 0, 0, '{}', 'trj_duplicate_version', 'org_revision_test')"
                    )
                )

            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    sa.text(
                        "INSERT INTO trajectories "
                        "(wellbore_id, name, kind, version, datum, is_current, station_count, validation, id, org_id) "
                        "VALUES ('wb_revision_test', 'Invalid version', 'actual', 0, 'unknown', 0, 0, '{}', 'trj_zero_version', 'org_revision_test')"
                    )
                )

            connection.execute(
                sa.text("UPDATE trajectories SET revision_reason='synthetic migration-test reason' WHERE id='trj_legacy'")
            )

        with pytest.raises(RuntimeError, match="revision reason"):
            command.downgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            assert _current_revision(connection) == HEAD_REVISION
            connection.execute(sa.text("UPDATE trajectories SET revision_reason=NULL WHERE id='trj_legacy'"))

        command.downgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            assert _current_revision(connection) == PARENT_REVISION
            assert "revision_reason" not in {column[1] for column in connection.execute(sa.text("PRAGMA table_info(trajectories)"))}
            assert connection.execute(sa.text("SELECT id FROM trajectories WHERE id='trj_legacy'")).scalar_one() == "trj_legacy"
    finally:
        engine.dispose()


def test_revision_migration_refuses_duplicate_legacy_versions_without_partial_ddl(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'trajectory-duplicate.db'}"
    config = _config(database_url)
    engine = sa.create_engine(database_url)
    try:
        command.upgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            _insert_trajectory(connection, trajectory_id="trj_legacy_1", name="Ambiguous actual")
            _insert_trajectory(connection, trajectory_id="trj_legacy_2", name="Ambiguous actual")

        with pytest.raises(RuntimeError, match="duplicate_version_groups=1"):
            command.upgrade(config, HEAD_REVISION)

        with engine.begin() as connection:
            assert _current_revision(connection) == PARENT_REVISION
            columns = {column[1] for column in connection.execute(sa.text("PRAGMA table_info(trajectories)"))}
            assert "revision_reason" not in columns
            assert connection.execute(sa.text("SELECT COUNT(*) FROM trajectories")).scalar_one() == 2
    finally:
        engine.dispose()
