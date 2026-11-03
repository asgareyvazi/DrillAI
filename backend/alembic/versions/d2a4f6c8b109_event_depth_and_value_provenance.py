"""Carry event, operation and extraction provenance in typed columns.

Revision ID: d2a4f6c8b109
Revises: c79f2a61d5b3

Legacy operational timestamps, depths and numeric values remain visible but are explicitly unverified.
Known ingestion-time/report-date placeholders are removed from the event-time column and retained in an
audit snapshot. New writes must identify their time, depth and numeric-value basis; an absent source time
stays unknown rather than being replaced by a creation timestamp.
"""

from __future__ import annotations

import copy
import datetime as dt
import json

import sqlalchemy as sa
from alembic import op

revision = "d2a4f6c8b109"
down_revision = "c79f2a61d5b3"
branch_labels = None
depends_on = None

_BACKUP_KEY = "cp84_event_time_provenance_backup"
_TIME_BASES = (
    "unknown",
    "source_record",
    "operator_entered",
    "linked_operation",
    "report_date_anchor",
    "duration_derived",
    "duration_replayed",
    "legacy_unverified",
)
_EVENT_TIME_BASES = ("unknown", "source_record", "operator_entered", "legacy_unverified")
_DEPTH_BASES = (
    "unknown",
    "source_record",
    "operator_entered",
    "linked_operation",
    "engine_derived",
    "legacy_unverified",
)
_VALUE_BASES = (
    "unknown",
    "observed",
    "reported",
    "calculated",
    "planned",
    "inferred",
    "legacy_unverified",
)

_OPERATION_TIME_PAIRS = (
    ("planned_start", "planned_start_basis"),
    ("planned_end", "planned_end_basis"),
    ("actual_start", "actual_start_basis"),
    ("actual_end", "actual_end_basis"),
)
_OPERATION_DEPTH_PAIRS = (
    ("depth_from_md_si", "depth_from_md_basis"),
    ("depth_to_md_si", "depth_to_md_basis"),
)
_OPERATION_VALUE_PAIRS = (
    ("planned_duration_hours", "planned_duration_basis"),
    ("actual_duration_hours", "actual_duration_basis"),
    ("hole_diameter_si", "hole_diameter_basis"),
    ("npt_hours", "npt_hours_basis"),
    ("invisible_lost_time_hours", "invisible_lost_time_basis"),
    ("cost_usd", "cost_basis"),
)
_EVENT_TIME_PAIRS = (("occurred_at", "occurred_at_basis"), ("ended_at", "ended_at_basis"))
_EVENT_DEPTH_PAIRS = (("depth_md_si", "depth_md_basis"), ("depth_tvd_si", "depth_tvd_basis"))
_EVENT_VALUE_PAIRS = (
    ("duration_hours", "duration_basis"),
    ("npt_hours", "npt_hours_basis"),
    ("cost_usd", "cost_basis"),
)
_RECORD_TIME_PAIRS = (("observed_at", "observed_at_basis"),)
_RECORD_DEPTH_PAIRS = (("depth_md_si", "depth_md_basis"), ("depth_tvd_si", "depth_tvd_basis"))


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
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _basis_fields(pairs: tuple[tuple[str, str], ...]) -> tuple[str, ...]:
    return tuple(basis for _value, basis in pairs)


def _allowed_sql(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _consistency_sql(pairs: tuple[tuple[str, str], ...]) -> str:
    return " AND ".join(
        f"(({value} IS NULL AND {basis} IN ('unknown', 'legacy_unverified')) OR "
        f"({value} IS NOT NULL AND {basis} <> 'unknown'))"
        for value, basis in pairs
    )


def _add_columns(table: str, fields: tuple[str, ...]) -> None:
    with op.batch_alter_table(table) as batch:
        for field in fields:
            batch.add_column(
                sa.Column(
                    field,
                    sa.String(length=32),
                    server_default=sa.text("'unknown'"),
                    nullable=False,
                )
            )


def _create_basis_constraints(
    table: str,
    *,
    valid_groups: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...],
    consistency_name: str,
    pairs: tuple[tuple[str, str], ...],
) -> None:
    with op.batch_alter_table(table) as batch:
        for name, fields, values in valid_groups:
            valid_sql = " AND ".join(
                f"{field} IN ({_allowed_sql(values)})" for field in fields
            )
            batch.create_check_constraint(op.f(f"ck_{table}_{name}"), valid_sql)
        batch.create_check_constraint(
            op.f(f"ck_{table}_{consistency_name}"),
            _consistency_sql(pairs),
        )


def _backfill_legacy_basis(table: str, basis_fields: tuple[str, ...]) -> None:
    connection = op.get_bind()
    typed = sa.table(
        table,
        *(sa.column(field, sa.String()) for field in basis_fields),
    )
    connection.execute(
        sa.update(typed).values(**dict.fromkeys(basis_fields, "legacy_unverified"))
    )


def _event_time_placeholders_to_backup() -> None:
    """Remove only known non-occurrence times while retaining their exact prior values for audit/rollback."""
    connection = op.get_bind()
    events = sa.table(
        "events",
        sa.column("id", sa.String()),
        sa.column("occurred_at", sa.DateTime(timezone=True)),
        sa.column("ended_at", sa.DateTime(timezone=True)),
        sa.column("attributes", sa.JSON()),
    )
    rows = connection.execute(
        sa.select(events.c.id, events.c.occurred_at, events.c.ended_at, events.c.attributes)
    ).all()
    placeholders = {
        "ingestion_timestamp_fallback",
        "document_report_date",
        "report_date",
        "report_date_anchor",
        "linked_operation_start",
    }
    for row in rows:
        attributes = _json_object(row.attributes)
        previous_basis = attributes.get("occurred_at_basis")
        if previous_basis not in placeholders:
            continue
        attributes[_BACKUP_KEY] = {
            "occurred_at": _instant_text(row.occurred_at),
            "ended_at": _instant_text(row.ended_at),
            "occurred_at_basis": previous_basis,
        }
        connection.execute(
            sa.update(events)
            .where(events.c.id == row.id)
            .values(occurred_at=None, attributes=attributes)
        )


def upgrade() -> None:
    _add_columns(
        "operations",
        (
            *_basis_fields(_OPERATION_TIME_PAIRS),
            *_basis_fields(_OPERATION_DEPTH_PAIRS),
            *_basis_fields(_OPERATION_VALUE_PAIRS),
        ),
    )
    with op.batch_alter_table("events") as batch:
        batch.alter_column(
            "occurred_at",
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=False,
            nullable=True,
        )
        for field in _basis_fields(_EVENT_TIME_PAIRS):
            batch.add_column(
                sa.Column(
                    field,
                    sa.String(length=32),
                    server_default=sa.text("'unknown'"),
                    nullable=False,
                )
            )
    _add_columns(
        "events",
        (*_basis_fields(_EVENT_DEPTH_PAIRS), *_basis_fields(_EVENT_VALUE_PAIRS)),
    )
    _add_columns(
        "extracted_records",
        (*_basis_fields(_RECORD_TIME_PAIRS), *_basis_fields(_RECORD_DEPTH_PAIRS)),
    )

    _event_time_placeholders_to_backup()

    _backfill_legacy_basis(
        "operations",
        (
            *_basis_fields(_OPERATION_TIME_PAIRS),
            *_basis_fields(_OPERATION_DEPTH_PAIRS),
            *_basis_fields(_OPERATION_VALUE_PAIRS),
        ),
    )
    _backfill_legacy_basis(
        "events",
        (
            *_basis_fields(_EVENT_TIME_PAIRS),
            *_basis_fields(_EVENT_DEPTH_PAIRS),
            *_basis_fields(_EVENT_VALUE_PAIRS),
        ),
    )
    _backfill_legacy_basis(
        "extracted_records",
        (*_basis_fields(_RECORD_TIME_PAIRS), *_basis_fields(_RECORD_DEPTH_PAIRS)),
    )

    _create_basis_constraints(
        "operations",
        valid_groups=(
            ("operation_time_provenance_valid", _basis_fields(_OPERATION_TIME_PAIRS), _TIME_BASES),
            ("operation_depth_provenance_valid", _basis_fields(_OPERATION_DEPTH_PAIRS), _DEPTH_BASES),
            ("operation_value_provenance_valid", _basis_fields(_OPERATION_VALUE_PAIRS), _VALUE_BASES),
        ),
        consistency_name="operation_provenance_consistent",
        pairs=(*_OPERATION_TIME_PAIRS, *_OPERATION_DEPTH_PAIRS, *_OPERATION_VALUE_PAIRS),
    )
    _create_basis_constraints(
        "events",
        valid_groups=(
            ("event_time_provenance_valid", _basis_fields(_EVENT_TIME_PAIRS), _EVENT_TIME_BASES),
            ("event_depth_provenance_valid", _basis_fields(_EVENT_DEPTH_PAIRS), _DEPTH_BASES),
            ("event_value_provenance_valid", _basis_fields(_EVENT_VALUE_PAIRS), _VALUE_BASES),
        ),
        consistency_name="event_provenance_consistent",
        pairs=(*_EVENT_TIME_PAIRS, *_EVENT_DEPTH_PAIRS, *_EVENT_VALUE_PAIRS),
    )
    _create_basis_constraints(
        "extracted_records",
        valid_groups=(
            (
                "extracted_record_time_provenance_valid",
                _basis_fields(_RECORD_TIME_PAIRS),
                ("unknown", "source_record", "report_header_date", "operator_entered", "legacy_unverified"),
            ),
            (
                "extracted_record_depth_provenance_valid",
                _basis_fields(_RECORD_DEPTH_PAIRS),
                ("unknown", "source_record", "operator_entered", "engine_derived", "legacy_unverified"),
            ),
        ),
        consistency_name="extracted_record_provenance_consistent",
        pairs=(*_RECORD_TIME_PAIRS, *_RECORD_DEPTH_PAIRS),
    )


def _assert_no_new_provenance(table: str, basis_fields: tuple[str, ...]) -> None:
    connection = op.get_bind()
    typed = sa.table(
        table,
        *(sa.column(field, sa.String()) for field in basis_fields),
    )
    columns = [getattr(typed.c, field) for field in basis_fields]
    changed = connection.execute(
        sa.select(*columns)
        .where(sa.or_(*(column != "legacy_unverified" for column in columns)))
        .limit(1)
    ).first()
    if changed is not None:
        raise RuntimeError(
            f"cannot downgrade {table} provenance after a post-migration time, depth, or value basis was written"
        )


def downgrade() -> None:
    operation_basis = (
        *_basis_fields(_OPERATION_TIME_PAIRS),
        *_basis_fields(_OPERATION_DEPTH_PAIRS),
        *_basis_fields(_OPERATION_VALUE_PAIRS),
    )
    event_basis = (
        *_basis_fields(_EVENT_TIME_PAIRS),
        *_basis_fields(_EVENT_DEPTH_PAIRS),
        *_basis_fields(_EVENT_VALUE_PAIRS),
    )
    record_basis = (*_basis_fields(_RECORD_TIME_PAIRS), *_basis_fields(_RECORD_DEPTH_PAIRS))
    _assert_no_new_provenance("operations", operation_basis)
    _assert_no_new_provenance("events", event_basis)
    _assert_no_new_provenance("extracted_records", record_basis)

    connection = op.get_bind()
    events = sa.table(
        "events",
        sa.column("id", sa.String()),
        sa.column("occurred_at", sa.DateTime(timezone=True)),
        sa.column("ended_at", sa.DateTime(timezone=True)),
        sa.column("attributes", sa.JSON()),
    )
    rows = connection.execute(
        sa.select(events.c.id, events.c.occurred_at, events.c.ended_at, events.c.attributes)
    ).all()
    restorations: list[tuple[str, dt.datetime]] = []
    for row in rows:
        if row.occurred_at is not None:
            continue
        attributes = _json_object(row.attributes)
        backup = attributes.get(_BACKUP_KEY)
        if not isinstance(backup, dict):
            raise RuntimeError("cannot restore legacy event time: its audited provenance snapshot is missing")
        previous = _parse_instant(backup.get("occurred_at"))
        if previous is None:
            raise RuntimeError("cannot restore legacy event time: its audited timestamp is malformed")
        restorations.append((row.id, previous))
    for event_id, previous in restorations:
        connection.execute(
            sa.update(events).where(events.c.id == event_id).values(occurred_at=previous)
        )

    for table, basis_fields, valid_names, consistency_name in (
        (
            "operations",
            operation_basis,
            (
                "operation_time_provenance_valid",
                "operation_depth_provenance_valid",
                "operation_value_provenance_valid",
            ),
            "operation_provenance_consistent",
        ),
        (
            "events",
            event_basis,
            (
                "event_time_provenance_valid",
                "event_depth_provenance_valid",
                "event_value_provenance_valid",
            ),
            "event_provenance_consistent",
        ),
        (
            "extracted_records",
            record_basis,
            (
                "extracted_record_time_provenance_valid",
                "extracted_record_depth_provenance_valid",
            ),
            "extracted_record_provenance_consistent",
        ),
    ):
        with op.batch_alter_table(table) as batch:
            for valid_name in valid_names:
                batch.drop_constraint(op.f(f"ck_{table}_{valid_name}"), type_="check")
            batch.drop_constraint(op.f(f"ck_{table}_{consistency_name}"), type_="check")
            for field in basis_fields:
                batch.drop_column(field)

    with op.batch_alter_table("events") as batch:
        batch.alter_column(
            "occurred_at",
            existing_type=sa.DateTime(timezone=True),
            existing_nullable=True,
            nullable=False,
        )

    event_rows = connection.execute(sa.select(events.c.id, events.c.attributes)).all()
    for row in event_rows:
        attributes = _json_object(row.attributes)
        if _BACKUP_KEY not in attributes:
            continue
        attributes.pop(_BACKUP_KEY, None)
        connection.execute(
            sa.update(events).where(events.c.id == row.id).values(attributes=attributes)
        )
