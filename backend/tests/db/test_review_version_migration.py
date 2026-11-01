"""The extracted-record review-version migration preserves existing evidence and supports rollback."""

from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT_REVISION = "f19a6d4c82b0"
HEAD_REVISION = "da0881aa121d"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_review_version_upgrade_backfills_cas_default_and_downgrades_without_losing_record(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'review-version.db'}"
    config = _alembic_config(database_url)
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        command.upgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO organizations (id, slug, name, kind, timezone, default_unit_system, "
                    "default_locale, settings, is_active, created_at, updated_at) "
                    "VALUES ('org_review_migration', 'review-migration', 'Review migration', 'operator', "
                    "'UTC', 'metric', 'en', '{}', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO extracted_records (id, org_id, record_type, payload_schema_key, "
                    "payload_schema_version, payload, method, validation_state, unit_context, quality_flags, "
                    "is_demo_fixture) VALUES ('rec_review_migration', 'org_review_migration', "
                    "'depth_reading', 'extracted.depth_reading', 1, :payload, 'rule', 'extracted', '{}', '[]', 0)"
                ),
                {"payload": json.dumps({"bit_depth_si": 1000.0})},
            )

        command.upgrade(config, HEAD_REVISION)
        with engine.begin() as connection:
            review_version, payload = connection.execute(
                sa.text(
                    "SELECT review_version, payload FROM extracted_records WHERE id='rec_review_migration'"
                )
            ).one()
            assert review_version == 1
            assert json.loads(payload) == {"bit_depth_si": 1000.0}
            first_write = connection.execute(
                sa.text(
                    "UPDATE extracted_records SET review_version=review_version+1 "
                    "WHERE id='rec_review_migration' AND review_version=1"
                )
            )
            stale_write = connection.execute(
                sa.text(
                    "UPDATE extracted_records SET review_version=review_version+1 "
                    "WHERE id='rec_review_migration' AND review_version=1"
                )
            )
            assert first_write.rowcount == 1
            assert stale_write.rowcount == 0
            assert connection.execute(
                sa.text("SELECT review_version FROM extracted_records WHERE id='rec_review_migration'")
            ).scalar_one() == 2

        command.downgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            columns = {row[1] for row in connection.execute(sa.text("PRAGMA table_info(extracted_records)"))}
            assert "review_version" not in columns
            assert json.loads(
                connection.execute(
                    sa.text("SELECT payload FROM extracted_records WHERE id='rec_review_migration'")
                ).scalar_one()
            ) == {"bit_depth_si": 1000.0}

        command.upgrade(config, HEAD_REVISION)
        with engine.begin() as connection:
            review_version, payload = connection.execute(
                sa.text("SELECT review_version, payload FROM extracted_records WHERE id='rec_review_migration'")
            ).one()
            assert review_version == 1
            assert json.loads(payload) == {"bit_depth_si": 1000.0}
    finally:
        engine.dispose()
