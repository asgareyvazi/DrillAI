"""Read-only CP136 SQLite evidence snapshots used by the real-browser daily-report journey."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT_SPEC = importlib.util.spec_from_file_location(
    "verify_daily_report_db_readback", REPO_ROOT / "scripts" / "verify_daily_report_db_readback.py"
)
assert _SCRIPT_SPEC is not None and _SCRIPT_SPEC.loader is not None
_SCRIPT_MODULE = importlib.util.module_from_spec(_SCRIPT_SPEC)
_SCRIPT_SPEC.loader.exec_module(_SCRIPT_MODULE)
read_snapshot = _SCRIPT_MODULE.read_snapshot


def _isolated_database_path(root: Path) -> Path:
    return root / ".e2e-readback" / "e2e.db"


def _seed_isolated_report_database(path: Path) -> None:
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE daily_drilling_reports (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, well_id TEXT NOT NULL,
                wellbore_id TEXT NOT NULL, report_date TEXT NOT NULL, report_number TEXT,
                latest_revision_number INTEGER NOT NULL, latest_approved_revision_number INTEGER
            );
            CREATE TABLE daily_drilling_report_revisions (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, report_id TEXT NOT NULL,
                revision_number INTEGER NOT NULL, edit_version INTEGER NOT NULL, status TEXT NOT NULL,
                source_kind TEXT NOT NULL, source_document_id TEXT, source_record_id TEXT,
                summary TEXT, content_hash TEXT
            );
            CREATE TABLE daily_report_activities (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                sequence INTEGER NOT NULL, title TEXT NOT NULL, operation_kind TEXT,
                duration_seconds INTEGER, duration_source_value REAL, duration_source_unit TEXT,
                source_document_id TEXT, source_record_id TEXT, resolution_state TEXT NOT NULL
            );
            CREATE TABLE daily_report_measurements (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                parameter_key TEXT NOT NULL, ordinal INTEGER NOT NULL, source_value REAL NOT NULL,
                source_unit TEXT NOT NULL, accepted_input_value REAL NOT NULL,
                accepted_input_unit TEXT NOT NULL, value_si REAL NOT NULL, canonical_unit TEXT NOT NULL,
                source_document_id TEXT, source_record_id TEXT, resolution_state TEXT NOT NULL
            );
            CREATE TABLE daily_report_reconciliations (
                id TEXT PRIMARY KEY, org_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                resolution_state TEXT NOT NULL, source_document_id TEXT, source_record_id TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO daily_drilling_reports VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("report-1", "org-1", "well-1", "bore-1", "2026-06-01", "DDR-1", 1, 1),
        )
        connection.execute(
            "INSERT INTO daily_drilling_report_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "revision-1", "org-1", "report-1", 1, 2, "approved", "import",
                "document-1", "record-1", "Synthetic drilling ahead", "a" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO daily_report_activities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "activity-1", "org-1", "revision-1", 1, "Synthetic drilling ahead", "drilling",
                28_800, 8.0, "h", "document-1", "record-2", "accepted",
            ),
        )
        connection.execute(
            "INSERT INTO daily_report_measurements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "measurement-1", "org-1", "revision-1", "mud_weight", 0, 12.5, "ppg",
                12.5, "ppg", 1_497.915, "kg/m3", "document-1", "record-2", "accepted",
            ),
        )
        for index in range(5):
            connection.execute(
                "INSERT INTO daily_report_reconciliations VALUES (?, ?, ?, ?, ?, ?)",
                (f"reconciliation-{index}", "org-1", "revision-1", "accepted", "document-1", f"record-{index + 1}"),
            )


def test_snapshot_is_scoped_read_only_and_contains_only_hashes_for_free_text(tmp_path) -> None:
    database = _isolated_database_path(tmp_path)
    _seed_isolated_report_database(database)
    original_digest = hashlib.sha256(database.read_bytes()).hexdigest()
    database_url = f"sqlite+aiosqlite:///{database.as_posix()}"

    snapshot = read_snapshot(database_url, report_id="report-1", well_id="well-1", wellbore_id="bore-1")

    assert snapshot["report"] == {
        "id": "report-1",
        "well_id": "well-1",
        "wellbore_id": "bore-1",
        "report_date": "2026-06-01",
        "report_number": "DDR-1",
        "latest_revision_number": 1,
        "latest_approved_revision_number": 1,
    }
    assert snapshot["revision"]["summary_sha256"] == hashlib.sha256(
        b"Synthetic drilling ahead"
    ).hexdigest()
    assert snapshot["activity"]["title_sha256"] == hashlib.sha256(
        b"Synthetic drilling ahead"
    ).hexdigest()
    assert snapshot["activity"]["duration_source_value"] == 8.0
    assert snapshot["measurement"] == {
        "parameter_key": "mud_weight",
        "source_value": 12.5,
        "source_unit": "ppg",
        "accepted_input_value": 12.5,
        "accepted_input_unit": "ppg",
        "value_si": 1_497.915,
        "canonical_unit": "kg/m3",
        "source_document_id": "document-1",
        "source_record_id": "record-2",
        "resolution_state": "accepted",
    }
    assert snapshot["reconciliation"] == {
        "total": 5,
        "accepted": 5,
        "source_documents": 1,
        "source_records": 5,
    }
    serialized = json.dumps(snapshot, sort_keys=True)
    assert "Synthetic drilling ahead" not in serialized
    assert hashlib.sha256(database.read_bytes()).hexdigest() == original_digest

    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection, pytest.raises(
        sqlite3.OperationalError
    ):
        connection.execute("UPDATE daily_drilling_reports SET report_number = 'changed'")


def test_snapshot_rejects_unscoped_database_urls_and_cross_well_reads(tmp_path) -> None:
    database = _isolated_database_path(tmp_path)
    _seed_isolated_report_database(database)
    database_url = f"sqlite+aiosqlite:///{database.as_posix()}"

    for unsafe_url in (
        "postgresql://not-supported",
        database_url + "?mode=rw",
        database_url + "#fragment",
        "sqlite+aiosqlite:///relative/e2e.db",
        f"sqlite+aiosqlite:///{(tmp_path / 'production.db').as_posix()}",
    ):
        with pytest.raises(ValueError):
            read_snapshot(unsafe_url, report_id="report-1", well_id="well-1", wellbore_id="bore-1")

    with pytest.raises(ValueError, match="requested E2E well/wellbore scope"):
        read_snapshot(database_url, report_id="report-1", well_id="other-well", wellbore_id="bore-1")
    with pytest.raises(ValueError, match="requested E2E well/wellbore scope"):
        read_snapshot(database_url, report_id="report-1", well_id="well-1", wellbore_id="other-bore")
