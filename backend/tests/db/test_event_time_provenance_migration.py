"""Event and operation time provenance migration never promotes administrative timestamps."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PARENT_REVISION = "c79f2a61d5b3"
HEAD_REVISION = "e90f1c8a2b64"
_BACKUP_KEY = "cp84_event_time_provenance_backup"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_legacy_rows(connection: sa.Connection) -> None:
    connection.execute(
        sa.text(
            "INSERT INTO events "
            "(id, org_id, well_id, kind, is_npt, title, occurred_at, ended_at, severity, status, "
            "source, source_kind, cause_basis, classification_source, tags, attributes, is_demo_fixture, "
            "created_at, updated_at) VALUES "
            "('evn_anchor', 'org_legacy', 'wel_legacy', 'npt', 1, 'legacy placeholder', "
            "'2026-03-15 08:00:00', '2026-03-15 12:00:00', 'high', 'open', 'report', 'ddr_promotion', "
            "'unknown', 'derived', :tags, :attributes, 0, '2026-03-15 08:01:00', '2026-03-15 08:01:00')"
        ),
        {
            "tags": json.dumps(["ddr"]),
            "attributes": json.dumps(
                {"occurred_at_basis": "ingestion_timestamp_fallback", "retain": "audit-value"}
            ),
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO events "
            "(id, org_id, well_id, kind, is_npt, title, occurred_at, severity, status, source, source_kind, "
            "cause_basis, classification_source, tags, attributes, is_demo_fixture, created_at, updated_at) VALUES "
            "('evn_legacy_time', 'org_legacy', 'wel_legacy', 'observation', 0, 'unverified legacy time', "
            "'2026-03-16 08:00:00', 'low', 'open', 'manual', 'manual', 'unknown', 'recorded', :tags, "
            ":attributes, 0, '2026-03-16 08:01:00', '2026-03-16 08:01:00')"
        ),
        {"tags": json.dumps([]), "attributes": json.dumps({"retain": "unverified"})},
    )
    connection.execute(
        sa.text(
            "INSERT INTO operations "
            "(id, org_id, well_id, wellbore_id, operation_class, sequence, name, kind, phase, status, "
            "actual_start, actual_end, actual_duration_hours, depth_from_md_si, depth_to_md_si, hole_diameter_si, "
            "is_productive, npt_hours, invisible_lost_time_hours, cost_usd, source, data_quality, attributes, "
            "is_demo_fixture, created_at, updated_at, source_kind) VALUES "
            "('opr_legacy', 'org_legacy', 'wel_legacy', 'wb_legacy', 'actual', 1, 'legacy drilling', "
            "'drilling', 'drilling', 'completed', '2026-03-15 00:00:00', '2026-03-15 08:00:00', 8, "
            "1000, 1100, 0.31115, 1, 1.5, 0, 12.5, 'report', 'extracted', :attributes, 0, "
            "'2026-03-15 08:01:00', '2026-03-15 08:01:00', 'import')"
        ),
        {"attributes": json.dumps({})},
    )
    connection.execute(
        sa.text(
            "INSERT INTO extracted_records "
            "(id, org_id, record_type, payload_schema_key, payload_schema_version, payload, observed_at, "
            "depth_md_si, depth_tvd_si, method, validation_state, unit_context, quality_flags, is_demo_fixture, "
            "created_at, region_unknown, review_version) VALUES "
            "('rec_legacy', 'org_legacy', 'npt', 'extracted.npt', 1, :payload, '2026-03-15 00:00:00', "
            "1500, 1200, 'table_row', 'extracted', :unit_context, :quality_flags, 0, "
            "'2026-03-15 08:01:00', 0, 1)"
        ),
        {
            "payload": json.dumps({"time": "2026-03-15"}),
            "unit_context": json.dumps({}),
            "quality_flags": json.dumps([]),
        },
    )


def test_migration_keeps_unknown_event_times_unknown_and_preserves_rollback_data(tmp_path):
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'event-time-provenance.db'}"
    config = _alembic_config(database_url)
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        command.upgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            _insert_legacy_rows(connection)

        command.upgrade(config, HEAD_REVISION)
        command.check(config)
        with engine.begin() as connection:
            anchor = connection.execute(
                sa.text(
                    "SELECT occurred_at, occurred_at_basis, ended_at, ended_at_basis, attributes "
                    "FROM events WHERE id='evn_anchor'"
                )
            ).one()
            attributes = json.loads(anchor.attributes) if isinstance(anchor.attributes, str) else anchor.attributes
            assert anchor.occurred_at is None
            assert anchor.occurred_at_basis == "legacy_unverified"
            assert anchor.ended_at is not None
            assert anchor.ended_at_basis == "legacy_unverified"
            assert attributes["retain"] == "audit-value"
            assert attributes[_BACKUP_KEY]["occurred_at_basis"] == "ingestion_timestamp_fallback"
            assert attributes[_BACKUP_KEY]["occurred_at"].startswith("2026-03-15T08:00:00")
            assert attributes[_BACKUP_KEY]["ended_at"].startswith("2026-03-15T12:00:00")

            legacy = connection.execute(
                sa.text("SELECT occurred_at, occurred_at_basis FROM events WHERE id='evn_legacy_time'")
            ).one()
            assert legacy.occurred_at is not None
            assert legacy.occurred_at_basis == "legacy_unverified"

            operation = connection.execute(
                sa.text(
                    "SELECT actual_start_basis, actual_end_basis, actual_duration_basis, depth_to_md_basis, "
                    "npt_hours_basis, cost_basis FROM operations WHERE id='opr_legacy'"
                )
            ).one()
            assert tuple(operation) == ("legacy_unverified",) * 6
            record = connection.execute(
                sa.text(
                    "SELECT observed_at_basis, depth_md_basis, depth_tvd_basis "
                    "FROM extracted_records WHERE id='rec_legacy'"
                )
            ).one()
            assert tuple(record) == ("legacy_unverified",) * 3

            with pytest.raises(sa.exc.IntegrityError):
                connection.execute(
                    sa.text("UPDATE events SET occurred_at_basis='report_date_anchor' WHERE id='evn_legacy_time'")
                )

        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE events SET occurred_at_basis='source_record' WHERE id='evn_legacy_time'")
            )
        with pytest.raises(RuntimeError, match="post-migration time, depth, or value basis"):
            command.downgrade(config, PARENT_REVISION)

        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE events SET occurred_at_basis='legacy_unverified' WHERE id='evn_legacy_time'")
            )
            connection.execute(
                sa.text(
                    "INSERT INTO events "
                    "(id, org_id, well_id, kind, is_npt, title, occurred_at, occurred_at_basis, "
                    "ended_at_basis, depth_md_basis, depth_tvd_basis, duration_basis, npt_hours_basis, "
                    "cost_basis, severity, status, source, source_kind, cause_basis, classification_source, "
                    "tags, attributes, is_demo_fixture, created_at, updated_at) VALUES "
                    "('evn_new_undated', 'org_legacy', 'wel_legacy', 'observation', 0, 'new undated event', "
                    "NULL, 'legacy_unverified', 'legacy_unverified', 'legacy_unverified', 'legacy_unverified', "
                    "'legacy_unverified', 'legacy_unverified', 'legacy_unverified', 'low', 'open', 'manual', "
                    "'manual', 'unknown', 'recorded', :tags, :attributes, 0, "
                    "'2026-03-17 08:01:00', '2026-03-17 08:01:00')"
                ),
                {"tags": json.dumps([]), "attributes": json.dumps({})},
            )

        with pytest.raises(RuntimeError, match="cannot restore legacy event time"):
            command.downgrade(config, PARENT_REVISION)

        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE events SET occurred_at_basis='unknown' WHERE id='evn_new_undated'")
            )
        with pytest.raises(RuntimeError, match="post-migration time, depth, or value basis"):
            command.downgrade(config, PARENT_REVISION)

        with engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM events WHERE id='evn_new_undated'"))
        command.downgrade(config, PARENT_REVISION)
        with engine.begin() as connection:
            restored = connection.execute(
                sa.text("SELECT occurred_at, ended_at, attributes FROM events WHERE id='evn_anchor'")
            ).one()
            assert str(restored.occurred_at).startswith("2026-03-15 08:00:00")
            assert str(restored.ended_at).startswith("2026-03-15 12:00:00")
            restored_attributes = (
                json.loads(restored.attributes) if isinstance(restored.attributes, str) else restored.attributes
            )
            assert restored_attributes == {"occurred_at_basis": "ingestion_timestamp_fallback", "retain": "audit-value"}
            event_columns = {column["name"] for column in sa.inspect(connection).get_columns("events")}
            assert "occurred_at_basis" not in event_columns
            assert "actual_start_basis" not in {
                column["name"] for column in sa.inspect(connection).get_columns("operations")
            }
    finally:
        engine.dispose()
