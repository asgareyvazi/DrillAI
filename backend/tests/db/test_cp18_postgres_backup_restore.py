"""CP18 Production Operations & PostgreSQL Backup / Restore / Migration Recovery Suite.

Verifies:
1. Real PostgreSQL 16 logical backup (`pg_dump --format=custom`), SHA-256 checksum & timing,
   catastrophic primary database loss simulation (`DROP DATABASE ... WITH (FORCE)`), clean
   target database restoration (`pg_restore --exit-on-error`), post-restore Alembic revision
   and zero-drift check (`alembic upgrade head && alembic check`), 20-table row-count parity,
   multi-tenant boundary isolation, live FastAPI application reads, and deterministic failure
   detection on corrupted backup archives (`@pytest.mark.postgres`).
2. Explicit architectural distinction between SQLite online `sqlite3.Connection.backup()`
   (local/dev only) and PostgreSQL `pg_dump`/`pg_restore` (production disaster recovery).
3. Cold-start `connector_out` resilience when `_active_egress_policy_snapshot` is `None`
   immediately after database restoration or API process startup.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

from drillai.db.models import Connector
from drillai.telemetry import connectors as connectors_mod

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_dr_drill_module():
    spec = importlib.util.spec_from_file_location(
        "verify_postgres_dr_drill", REPO_ROOT / "scripts" / "verify_postgres_dr_drill.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.postgres
async def test_cp18_01_postgres_logical_backup_restore_and_migration_recovery_drill(
    postgres_url: str,
) -> None:
    """Execute the full isolated PostgreSQL 16 backup/restore/migration/tenant-isolation/corruption drill."""
    mod = _load_dr_drill_module()
    report = await mod.run_postgres_dr_drill(postgres_url)

    assert report["drill_status"] == "PASSED"
    assert report["alembic_zero_drift_verified"] is True
    assert len(report["alembic_revision"]) >= 8

    artifact = report["backup_artifact"]
    assert artifact["format"] == "pg_dump_custom"
    assert artifact["size_bytes"] > 10_000
    assert len(artifact["sha256"]) == 64
    assert artifact["backup_duration_ms"] > 0.0
    assert artifact["restore_duration_ms"] > 0.0

    assert report["counts_match_exactly"] is True
    assert report["pre_backup_counts"] == report["post_restore_counts"]
    assert report["post_restore_counts"]["organizations"] == 2
    assert report["post_restore_counts"]["wells"] == 2
    assert report["post_restore_counts"]["time_series_points"] == 5

    app_ver = report["application_verification"]
    assert app_ver["health_ready_status"] == "ok"
    assert app_ver["database_dialect"] == "postgresql"
    assert app_ver["tenant_a_isolated"] is True
    assert app_ver["tenant_b_isolated"] is True
    assert app_ver["twin_snapshot_restored"] is True
    assert app_ver["egress_policy_revision_restored"] == 2

    assert report["corrupt_backup_failure_detected"] is True
    assert report["corrupt_restore_exit_code"] != 0


def test_cp18_02_sqlite_online_backup_api_vs_postgres_dr_distinction(tmp_path: Path) -> None:
    """Verify SQLite online backup API for local dev and ensure docs distinguish it from PostgreSQL DR."""
    src_db = tmp_path / "dev_live.db"
    dst_db = tmp_path / "dev_online_backup.db"

    with sqlite3.connect(src_db) as src_conn:
        src_conn.execute("CREATE TABLE dev_items (id TEXT PRIMARY KEY, val INTEGER)")
        src_conn.execute("INSERT INTO dev_items VALUES ('item_1', 42)")
        src_conn.commit()
        with sqlite3.connect(dst_db) as dst_conn:
            src_conn.backup(dst_conn)

    with sqlite3.connect(dst_db) as check_conn:
        row = check_conn.execute("SELECT id, val FROM dev_items").fetchone()
        assert row == ("item_1", 42)

    ops_doc = (REPO_ROOT / "docs" / "CONNECTORS_AND_OPERATIONS.md").read_text(encoding="utf-8")
    assert "pg_dump --format=custom" in ops_doc
    assert "pg_restore" in ops_doc
    assert "scripts/verify_postgres_dr_drill.py" in ops_doc
    assert "production_dr_capable: false" in ops_doc

    cp18_report = REPO_ROOT / "docs" / "mission-reports" / "CP18-OPERATIONS-READINESS.md"
    assert cp18_report.is_file()
    assert len(cp18_report.read_text(encoding="utf-8")) > 1500


def test_cp18_03_connector_out_cold_start_when_egress_snapshot_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """connector_out must not raise AttributeError when _active_egress_policy_snapshot is None on cold start."""
    monkeypatch.setattr(connectors_mod, "_active_egress_policy_snapshot", None)
    row = Connector(
        id="cnc_cold_01",
        org_id="org_01",
        well_id="wel_01",
        key="cold-start-cnc",
        name="Cold Start Connector",
        provider="witsml",
        protocol_profile="witsml.1.4.1.1.soap_http",
        endpoint_url="https://witsml.rig.example.com/store",
        status="stopped",
        desired_state="stopped",
        config_version=1,
        fencing_token=1,
        config={"auth_mode": "basic"},
    )
    serialized = connectors_mod.connector_out(row)
    assert serialized["worker"]["system_policy_revision"] >= 1
