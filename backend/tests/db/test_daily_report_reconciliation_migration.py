"""Additive reconciliation-ledger migration, schema parity and data-preserving rollback guard."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT_REVISION = "c5ca12bb7b5d"
HEAD_REVISION = "c79f2a61d5b3"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_reconciliation_migration_schema_drift_and_loss_protection(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'daily-report-reconciliation-migration.db'}"
    config = _alembic_config(database_url)
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        command.upgrade(config, PARENT_REVISION)
        command.upgrade(config, HEAD_REVISION)
        with engine.begin() as connection:
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == HEAD_REVISION
            tables = set(sa.inspect(connection).get_table_names())
            assert "daily_report_reconciliations" in tables
            columns = {
                column["name"]
                for column in sa.inspect(connection).get_columns("daily_report_reconciliations")
            }
            assert {
                "revision_id",
                "target_key",
                "target_kind",
                "field_key",
                "source_document_id",
                "source_record_id",
                "source_path",
                "source_review_version",
                "source_fingerprint",
                "source_value_hash",
                "proposed_value_hash",
                "resolution_state",
                "reason",
                "decided_by",
                "decided_at",
            } <= columns
            connection.execute(
                sa.text(
                    "INSERT INTO daily_report_reconciliations "
                    "(id, org_id, revision_id, target_key, target_kind, target_id, field_key, "
                    "source_document_id, source_record_id, source_region_id, source_page_number, source_path, "
                    "source_review_version, source_fingerprint, source_value_hash, proposed_value_hash, "
                    "resolution_state, reason, decided_by, decided_at, created_by) VALUES "
                    "('drc_migration_row', 'org_migration', 'drr_migration', 'field:summary', "
                    "'revision_field', NULL, 'summary', 'doc_migration', 'rec_migration', NULL, NULL, "
                    "'payload.summary', 1, :hash, :hash, :hash, 'proposed', NULL, NULL, NULL, 'usr_migration')"
                ),
                {"hash": "a" * 64},
            )

        with pytest.raises(RuntimeError, match="refusing to drop 1 source reconciliation decisions"):
            command.downgrade(config, PARENT_REVISION)

        with engine.begin() as connection:
            connection.execute(
                sa.text("DELETE FROM daily_report_reconciliations WHERE id='drc_migration_row'")
            )
        command.downgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            assert "daily_report_reconciliations" not in set(sa.inspect(connection).get_table_names())

        command.upgrade(config, HEAD_REVISION)
        with engine.begin() as connection:
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == HEAD_REVISION
    finally:
        engine.dispose()
