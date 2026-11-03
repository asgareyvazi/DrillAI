"""Classify legacy trajectory datum and station-time provenance conservatively.

Revision ID: c79f2a61d5b3
Revises: b83e4a6d1c29

Legacy trajectory datum strings (including ``rkb``) were not distinguishable from earlier application
defaults. Legacy station ``measured_at`` values could also have been copied from a report-level date.
Keep those original values in auditable JSON snapshots, expose neither as a verified datum nor as a
station measurement instant, and require new writes to declare provenance explicitly.
"""

from __future__ import annotations

import copy
import datetime as dt
import json

import sqlalchemy as sa
from alembic import op

revision = "c79f2a61d5b3"
down_revision = "b83e4a6d1c29"
branch_labels = None
depends_on = None

_LEGACY_TIME_BACKUP = "legacy_measured_at_provenance_backup"
_LEGACY_TIME_FLAG = "legacy_measured_at_provenance_unverified"


def _json_object(value: object) -> dict:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return copy.deepcopy(decoded) if isinstance(decoded, dict) else {}
    return {}


def _json_list(value: object) -> list:
    if isinstance(value, list):
        return copy.deepcopy(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return []
        return copy.deepcopy(decoded) if isinstance(decoded, list) else []
    return []


def _legacy_validation(previous: dict, datum: object, frame_known: bool) -> dict:
    state = "legacy_datum_provenance_unverified" if frame_known else "legacy_frame_unclassified"
    code = "legacy_datum_provenance_unverified" if frame_known else "legacy_frame_unclassified"
    message = (
        "The stored datum value is preserved, but its origin (operator declaration versus historical "
        "application default) is unverified; do not use it as a compatible survey reference."
        if frame_known
        else "The stored datum value and survey azimuth frame are preserved, but the legacy frame identity "
        "and datum origin are unverified; do not use this trajectory for frame-compatible calculations."
    )
    issues = copy.deepcopy(previous.get("issues")) if isinstance(previous.get("issues"), list) else []
    issue = {"severity": "warning", "code": code, "message": message}
    if issue not in issues:
        issues.append(issue)
    return {
        **copy.deepcopy(previous),
        "is_valid": False,
        "state": state,
        "datum_provenance": "legacy_unverified",
        "legacy_datum_value": datum,
        "legacy_validation_before_datum_provenance": copy.deepcopy(previous),
        "issues": issues,
    }


def _instant_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return str(value)


def _parse_instant(value: object) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        # The prior UtcDateTime type stored naive SQLite values as UTC; restore that exact instant.
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def upgrade() -> None:
    op.add_column(
        "trajectories",
        sa.Column(
            "datum_provenance",
            sa.String(length=32),
            server_default=sa.text("'unknown'"),
            nullable=False,
            comment="source of the vertical datum declaration; legacy values remain unverified",
        ),
    )
    op.add_column(
        "trajectory_stations",
        sa.Column(
            "measured_at_provenance",
            sa.String(length=32),
            server_default=sa.text("'unknown'"),
            nullable=False,
            comment="only source_record timestamps may be exposed as station measurement instants",
        ),
    )
    connection = op.get_bind()
    trajectories = sa.table(
        "trajectories",
        sa.column("id", sa.String()),
        sa.column("datum", sa.String()),
        sa.column("azimuth_reference", sa.String()),
        sa.column("azimuth_reference_id", sa.String()),
        sa.column("datum_provenance", sa.String()),
        sa.column("validation", sa.JSON()),
    )
    rows = connection.execute(
        sa.select(
            trajectories.c.id,
            trajectories.c.datum,
            trajectories.c.azimuth_reference,
            trajectories.c.azimuth_reference_id,
            trajectories.c.validation,
        )
    ).all()
    for row in rows:
        previous = _json_object(row.validation)
        frame_known = (
            row.azimuth_reference in {"grid", "true", "magnetic"}
            and isinstance(row.azimuth_reference_id, str)
            and bool(row.azimuth_reference_id.strip())
        )
        connection.execute(
            sa.update(trajectories)
            .where(trajectories.c.id == row.id)
            .values(
                datum_provenance="legacy_unverified",
                validation=_legacy_validation(previous, row.datum, frame_known),
            )
        )

    stations = sa.table(
        "trajectory_stations",
        sa.column("id", sa.String()),
        sa.column("measured_at", sa.DateTime(timezone=True)),
        sa.column("measured_at_provenance", sa.String()),
        sa.column("raw_source", sa.JSON()),
        sa.column("quality_flags", sa.JSON()),
    )
    rows = connection.execute(
        sa.select(stations.c.id, stations.c.measured_at, stations.c.raw_source, stations.c.quality_flags)
        .where(stations.c.measured_at.is_not(None))
    ).all()
    for row in rows:
        previous_source = _json_object(row.raw_source)
        previous_flags = _json_list(row.quality_flags)
        backup = {
            "measured_at": _instant_text(row.measured_at),
            "raw_source": copy.deepcopy(previous_source),
            "quality_flags": copy.deepcopy(previous_flags),
        }
        if _LEGACY_TIME_FLAG not in previous_flags:
            previous_flags.append(_LEGACY_TIME_FLAG)
        audited_source = {
            **previous_source,
            _LEGACY_TIME_BACKUP: backup,
        }
        connection.execute(
            sa.update(stations)
            .where(stations.c.id == row.id)
            .values(
                measured_at=None,
                measured_at_provenance="legacy_unverified",
                raw_source=audited_source,
                quality_flags=previous_flags,
            )
        )

    with op.batch_alter_table("trajectories") as batch:
        batch.create_check_constraint(
            op.f("ck_trajectories_datum_provenance_valid"),
            "datum_provenance IN ('unknown', 'source_document', 'engine_input', 'legacy_unverified')",
        )
    with op.batch_alter_table("trajectory_stations") as batch:
        batch.create_check_constraint(
            op.f("ck_trajectory_stations_measured_at_provenance_valid"),
            "measured_at_provenance IN ('unknown', 'source_record', 'legacy_unverified')",
        )
        batch.create_check_constraint(
            op.f("ck_trajectory_stations_measured_at_provenance_consistent"),
            "(measured_at IS NULL AND measured_at_provenance IN ('unknown', 'legacy_unverified')) OR "
            "(measured_at IS NOT NULL AND measured_at_provenance = 'source_record')",
        )


def downgrade() -> None:
    connection = op.get_bind()
    trajectories = sa.table(
        "trajectories",
        sa.column("id", sa.String()),
        sa.column("datum", sa.String()),
        sa.column("azimuth_reference", sa.String()),
        sa.column("azimuth_reference_id", sa.String()),
        sa.column("datum_provenance", sa.String()),
        sa.column("validation", sa.JSON()),
    )
    trajectory_rows = connection.execute(
        sa.select(
            trajectories.c.id,
            trajectories.c.datum,
            trajectories.c.azimuth_reference,
            trajectories.c.azimuth_reference_id,
            trajectories.c.datum_provenance,
            trajectories.c.validation,
        )
    ).all()
    trajectory_restorations = []
    for row in trajectory_rows:
        if row.datum_provenance != "legacy_unverified":
            raise RuntimeError(
                "cannot downgrade trajectory provenance after a post-migration datum provenance value was written"
            )
        current = _json_object(row.validation)
        previous = current.get("legacy_validation_before_datum_provenance")
        if not isinstance(previous, dict):
            raise RuntimeError("cannot restore trajectory validation: the legacy snapshot is missing")
        frame_known = (
            row.azimuth_reference in {"grid", "true", "magnetic"}
            and isinstance(row.azimuth_reference_id, str)
            and bool(row.azimuth_reference_id.strip())
        )
        if current != _legacy_validation(previous, row.datum, frame_known):
            raise RuntimeError("cannot downgrade trajectory provenance after its validation was edited")
        trajectory_restorations.append((row.id, previous))

    stations = sa.table(
        "trajectory_stations",
        sa.column("id", sa.String()),
        sa.column("measured_at", sa.DateTime(timezone=True)),
        sa.column("measured_at_provenance", sa.String()),
        sa.column("raw_source", sa.JSON()),
        sa.column("quality_flags", sa.JSON()),
    )
    station_rows = connection.execute(
        sa.select(
            stations.c.id,
            stations.c.measured_at,
            stations.c.measured_at_provenance,
            stations.c.raw_source,
            stations.c.quality_flags,
        )
    ).all()
    station_restorations = []
    for row in station_rows:
        if row.measured_at_provenance == "source_record":
            raise RuntimeError(
                "cannot downgrade station time provenance after a source-record measurement time was written"
            )
        if row.measured_at_provenance == "legacy_unverified":
            current_source = _json_object(row.raw_source)
            backup = current_source.get(_LEGACY_TIME_BACKUP)
            if not isinstance(backup, dict):
                raise RuntimeError("cannot restore legacy station time: the provenance snapshot is missing")
            previous_source = backup.get("raw_source")
            previous_flags = backup.get("quality_flags")
            previous_time = _parse_instant(backup.get("measured_at"))
            if not isinstance(previous_source, dict) or not isinstance(previous_flags, list):
                raise RuntimeError("cannot restore legacy station time: the provenance snapshot is malformed")
            expected_flags = copy.deepcopy(previous_flags)
            if _LEGACY_TIME_FLAG not in expected_flags:
                expected_flags.append(_LEGACY_TIME_FLAG)
            expected_source = {**copy.deepcopy(previous_source), _LEGACY_TIME_BACKUP: backup}
            if (
                row.measured_at is not None
                or current_source != expected_source
                or _json_list(row.quality_flags) != expected_flags
                or previous_time is None
            ):
                raise RuntimeError("cannot downgrade station time provenance after its audit snapshot was edited")
            station_restorations.append((row.id, previous_time, previous_source, previous_flags))

    # Preflight every row before changing constraints or data. SQLite DDL is not transactional.
    for row_id, previous_validation in trajectory_restorations:
        connection.execute(
            sa.update(trajectories)
            .where(trajectories.c.id == row_id)
            .values(validation=previous_validation)
        )

    with op.batch_alter_table("trajectory_stations") as batch:
        batch.drop_constraint(op.f("ck_trajectory_stations_measured_at_provenance_consistent"), type_="check")
    for row_id, previous_time, previous_source, previous_flags in station_restorations:
        connection.execute(
            sa.update(stations)
            .where(stations.c.id == row_id)
            .values(
                measured_at=previous_time,
                measured_at_provenance="unknown",
                raw_source=previous_source,
                quality_flags=previous_flags,
            )
        )

    with op.batch_alter_table("trajectory_stations") as batch:
        batch.drop_constraint(op.f("ck_trajectory_stations_measured_at_provenance_valid"), type_="check")
        batch.drop_column("measured_at_provenance")
    with op.batch_alter_table("trajectories") as batch:
        batch.drop_constraint(op.f("ck_trajectories_datum_provenance_valid"), type_="check")
        batch.drop_column("datum_provenance")
