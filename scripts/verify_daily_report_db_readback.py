#!/usr/bin/env python3
"""Read a synthetic daily-report snapshot from an isolated E2E SQLite database, without writes."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote

_SQLITE_URL_PREFIX = "sqlite+aiosqlite:///"
_E2E_DATABASE_NAME = "e2e.db"


def _isolated_database_path(raw_path: str) -> Path:
    database_path = Path(raw_path)
    if not database_path.is_absolute():
        raise ValueError("the E2E SQLite path must be absolute")
    resolved = database_path.resolve()
    if (
        resolved.name != _E2E_DATABASE_NAME
        or not (resolved.parent.name == ".e2e" or resolved.parent.name.startswith(".e2e-"))
    ):
        raise ValueError("readback is restricted to an isolated .e2e*/e2e.db database")
    return resolved


def read_snapshot(database_url: str, *, report_id: str, well_id: str, wellbore_id: str) -> dict[str, object]:
    if (
        not database_url.startswith(_SQLITE_URL_PREFIX)
        or "?" in database_url
        or "#" in database_url
        or not all((report_id, well_id, wellbore_id))
    ):
        raise ValueError("readback requires an isolated SQLite E2E URL and explicit report/well scope")
    raw_path = unquote(database_url[len(_SQLITE_URL_PREFIX) :])
    if not raw_path:
        raise ValueError("the E2E SQLite path is empty")
    database_path = _isolated_database_path(raw_path)
    connection_uri = f"{database_path.as_uri()}?mode=ro"

    # sqlite3.Connection's context manager only commits/rolls back; it does not close the handle.
    # Explicitly close this independent read-only connection after the scoped snapshot is materialized.
    with closing(sqlite3.connect(connection_uri, uri=True, timeout=5.0)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        report = connection.execute(
            """
            SELECT id, org_id, well_id, wellbore_id, report_date, report_number,
                   latest_revision_number, latest_approved_revision_number
            FROM daily_drilling_reports
            WHERE id = ? AND well_id = ? AND wellbore_id = ?
            """,
            (report_id, well_id, wellbore_id),
        ).fetchone()
        if report is None:
            raise ValueError("no report exists in the requested E2E well/wellbore scope")
        revision = connection.execute(
            """
            SELECT id, org_id, report_id, revision_number, edit_version, status, source_kind,
                   source_document_id, source_record_id, summary, content_hash
            FROM daily_drilling_report_revisions
            WHERE org_id = ? AND report_id = ? AND revision_number = ?
            """,
            (report["org_id"], report["id"], report["latest_revision_number"]),
        ).fetchone()
        if revision is None:
            raise ValueError("the report's current revision is missing from the E2E database")

        activity = connection.execute(
            """
            SELECT title, operation_kind, duration_seconds, duration_source_value,
                   duration_source_unit, source_document_id, source_record_id, resolution_state
            FROM daily_report_activities
            WHERE org_id = ? AND revision_id = ?
            ORDER BY sequence
            LIMIT 1
            """,
            (report["org_id"], revision["id"]),
        ).fetchone()
        measurement = connection.execute(
            """
            SELECT parameter_key, source_value, source_unit, accepted_input_value,
                   accepted_input_unit, value_si, canonical_unit, source_document_id,
                   source_record_id, resolution_state
            FROM daily_report_measurements
            WHERE org_id = ? AND revision_id = ?
            ORDER BY parameter_key, ordinal
            LIMIT 1
            """,
            (report["org_id"], revision["id"]),
        ).fetchone()
        reconciliation = connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN resolution_state = 'accepted' THEN 1 ELSE 0 END) AS accepted,
                   COUNT(DISTINCT source_document_id) AS source_documents,
                   COUNT(DISTINCT source_record_id) AS source_records
            FROM daily_report_reconciliations
            WHERE org_id = ? AND revision_id = ?
            """,
            (report["org_id"], revision["id"]),
        ).fetchone()

    if activity is None or measurement is None or reconciliation is None:
        raise ValueError("the imported report's typed evidence or reconciliation rows are missing")
    return {
        "report": {
            "id": report["id"],
            "well_id": report["well_id"],
            "wellbore_id": report["wellbore_id"],
            "report_date": report["report_date"],
            "report_number": report["report_number"],
            "latest_revision_number": report["latest_revision_number"],
            "latest_approved_revision_number": report["latest_approved_revision_number"],
        },
        "revision": {
            "id": revision["id"],
            "revision_number": revision["revision_number"],
            "edit_version": revision["edit_version"],
            "status": revision["status"],
            "source_kind": revision["source_kind"],
            "source_document_id": revision["source_document_id"],
            "source_record_id": revision["source_record_id"],
            "summary_sha256": hashlib.sha256((revision["summary"] or "").encode("utf-8")).hexdigest(),
            "content_hash": revision["content_hash"],
        },
        "activity": {
            "title_sha256": hashlib.sha256(activity["title"].encode("utf-8")).hexdigest(),
            "operation_kind": activity["operation_kind"],
            "duration_seconds": activity["duration_seconds"],
            "duration_source_value": activity["duration_source_value"],
            "duration_source_unit": activity["duration_source_unit"],
            "source_document_id": activity["source_document_id"],
            "source_record_id": activity["source_record_id"],
            "resolution_state": activity["resolution_state"],
        },
        "measurement": dict(measurement),
        "reconciliation": {
            "total": reconciliation["total"],
            "accepted": reconciliation["accepted"] or 0,
            "source_documents": reconciliation["source_documents"],
            "source_records": reconciliation["source_records"],
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database_url")
    parser.add_argument("report_id")
    parser.add_argument("well_id")
    parser.add_argument("wellbore_id")
    args = parser.parse_args(argv)
    try:
        result = read_snapshot(
            args.database_url,
            report_id=args.report_id,
            well_id=args.well_id,
            wellbore_id=args.wellbore_id,
        )
    except (OSError, sqlite3.Error, ValueError, TypeError):
        # Never echo a path, database URL, SQL exception, or source payload into Playwright output.
        print("DAILY_REPORT_DB_READBACK_FAILED: scoped read-only SQLite readback was unavailable", file=sys.stderr)
        return 2
    sys.stdout.write(json.dumps(result, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
