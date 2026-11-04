"""Report identity reconciliation constraints preserve data and immutable-identity history."""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT_REVISION = "e90f1c8a2b64"
IDENTITY_REVISION = "f0a4b8c1d2e3"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_reconciliation(connection: sa.Connection, *, target_kind: str, field_key: str) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO daily_report_reconciliations "
            "(id, org_id, revision_id, target_key, target_kind, target_id, field_key, "
            "source_document_id, source_record_id, source_region_id, source_page_number, source_path, "
            "source_review_version, source_fingerprint, source_value_hash, proposed_value_hash, "
            "resolution_state, reason, decided_by, decided_at, created_by) VALUES "
            "(:id, 'org_identity', 'drr_identity', :target_key, :target_kind, NULL, :field_key, "
            "'doc_identity', 'rec_identity', NULL, 1, 'payload.report_date', 1, :hash, :hash, :hash, "
            "'proposed', NULL, NULL, NULL, 'usr_identity')"
        ),
        {
            "id": f"drc_{target_kind}_{field_key}",
            "target_key": f"{target_kind}:{field_key}",
            "target_kind": target_kind,
            "field_key": field_key,
            "hash": "a" * 64,
        },
    )


def test_identity_reconciliation_migration_constraints_and_loss_protection(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'daily-report-identity-reconciliation.db'}"
    config = _alembic_config(database_url)
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        command.upgrade(config, PARENT_REVISION)
        command.upgrade(config, IDENTITY_REVISION)
        with engine.begin() as connection:
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == IDENTITY_REVISION
            _insert_reconciliation(connection, target_kind="report_identity", field_key="report_date")
            with pytest.raises(sa.exc.IntegrityError):
                _insert_reconciliation(connection, target_kind="report_identity", field_key="wellbore_id")

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

        command.upgrade(config, IDENTITY_REVISION)
        with engine.begin() as connection:
            version = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            assert version == IDENTITY_REVISION
    finally:
        engine.dispose()
