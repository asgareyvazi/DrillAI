#!/usr/bin/env python3
"""Read a scoped engineering-product revision from a dedicated synthetic CP150 SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import unquote

_SQLITE_URL_PREFIX = "sqlite+aiosqlite:///"
_E2E_DATABASE_NAME = "e2e.db"
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_FRONTEND_ROOT = _REPOSITORY_ROOT / "frontend"


def _isolated_database_path(database_url: str) -> Path:
    if (
        not database_url.startswith(_SQLITE_URL_PREFIX)
        or "?" in database_url
        or "#" in database_url
    ):
        raise ValueError("readback requires a plain dedicated SQLite E2E URL")
    raw = unquote(database_url[len(_SQLITE_URL_PREFIX) :])
    candidate = Path(raw)
    if not candidate.is_absolute() or candidate.name != _E2E_DATABASE_NAME:
        raise ValueError("readback requires an absolute isolated e2e.db path")
    if candidate.is_symlink() or candidate.parent.is_symlink():
        raise ValueError("readback refuses a symlinked E2E database path")
    if candidate.parent.parent != _FRONTEND_ROOT or not candidate.parent.name.startswith(".e2e-cp150-"):
        raise ValueError("readback is restricted to a dedicated frontend/.e2e-cp150-* database")
    resolved = candidate.resolve(strict=True)
    if resolved != candidate:
        raise ValueError("readback refuses a path that resolves outside its dedicated E2E directory")
    return resolved


def _json_object(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("stored engineering-product JSON is malformed")
    return parsed


def read_snapshot(
    database_url: str,
    *,
    artifact_id: str,
    well_id: str,
    wellbore_id: str,
    version_id: str,
) -> dict[str, object]:
    if not all(isinstance(value, str) and value.strip() for value in (artifact_id, well_id, wellbore_id, version_id)):
        raise ValueError("readback requires explicit artifact, well, wellbore and revision identities")
    database_path = _isolated_database_path(database_url)
    connection_uri = f"{database_path.as_uri()}?mode=ro"
    with closing(sqlite3.connect(connection_uri, uri=True, timeout=5.0)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        artifact_row = connection.execute(
            """
            SELECT id, org_id, project_id, well_id, wellbore_id, section_id, operation_id,
                   kind, title, code, status, current_version, is_demo_fixture
            FROM artifacts
            WHERE id = ? AND well_id = ? AND wellbore_id = ?
            """,
            (artifact_id, well_id, wellbore_id),
        ).fetchone()
        if artifact_row is None:
            raise ValueError("no engineering product exists in the requested E2E wellbore scope")
        if artifact_row["kind"] not in {"program", "procedure"}:
            raise ValueError("the requested artifact is not a drilling program or controlled procedure")
        version_row = connection.execute(
            """
            SELECT id, artifact_id, version, state, schema_key, schema_version, payload,
                   content_hash, authored_by, authored_at, reviewed_by, reviewed_at,
                   approved_by, approved_at, approval_request_id, change_reason,
                   supersedes_version_id, validation_summary, engine_run_ids
            FROM artifact_versions
            WHERE id = ? AND artifact_id = ? AND org_id = ?
            """,
            (version_id, artifact_id, artifact_row["org_id"]),
        ).fetchone()
        if version_row is None:
            raise ValueError("the requested immutable revision is missing from the scoped E2E database")
        version_rows = connection.execute(
            """
            SELECT id, version, state, content_hash, authored_by, reviewed_by, approved_by,
                   approval_request_id, supersedes_version_id
            FROM artifact_versions
            WHERE artifact_id = ? AND org_id = ?
            ORDER BY version
            """,
            (artifact_id, artifact_row["org_id"]),
        ).fetchall()
        approval_row = None
        if version_row["approval_request_id"]:
            approval_row = connection.execute(
                """
                SELECT id, org_id, subject_kind, subject_id, status, requested_by, decided_by,
                       decision_note, request_payload
                FROM approval_requests
                WHERE id = ? AND org_id = ? AND subject_kind = 'engineering_artifact_version'
                  AND subject_id = ?
                """,
                (version_row["approval_request_id"], artifact_row["org_id"], version_id),
            ).fetchone()

    artifact = dict(artifact_row)
    version = dict(version_row)
    version["payload"] = _json_object(version["payload"])
    version["validation_summary"] = _json_object(version["validation_summary"])
    version["engine_run_ids"] = json.loads(version["engine_run_ids"] or "[]")
    revisions = [dict(row) for row in version_rows]
    approval = None
    if approval_row is not None:
        approval = dict(approval_row)
        approval.pop("request_payload")
        request_payload = _json_object(approval_row["request_payload"])
        review = request_payload.get("review")
        approval["reviewed_by"] = review.get("reviewed_by") if isinstance(review, dict) else None
    return {"artifact": artifact, "version": version, "versions": revisions, "approval": approval}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database_url")
    parser.add_argument("artifact_id")
    parser.add_argument("well_id")
    parser.add_argument("wellbore_id")
    parser.add_argument("version_id")
    args = parser.parse_args()
    try:
        snapshot = read_snapshot(
            args.database_url,
            artifact_id=args.artifact_id,
            well_id=args.well_id,
            wellbore_id=args.wellbore_id,
            version_id=args.version_id,
        )
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError):
        # Keep the helper's process output scoped; it is consumed by an automated browser test.
        raise SystemExit("Read-only engineering-product database readback failed for the isolated fixture.") from None
    print(json.dumps(snapshot, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
