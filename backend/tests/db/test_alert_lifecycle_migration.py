"""The alert life-cycle migration, on a database that already holds alerts and outbox events.

Three facts are proved here, and the differences between them are the point:

* a database whose alerts use the old status and severity spellings is **repaired** into the closed
  vocabulary;
* a database holding a status nobody has mapped **stops the migration**, naming the column, the value and
  the row count, because an alert whose state the platform cannot name is an alert nobody can reason about;
* the outbox counter is **seeded from the stream that already exists**, so the first event written after
  the migration continues the numbering rather than restarting at 1 — which every connected consumer
  would read as "no gap" while it silently missed everything.

The rule-unit column is checked here too: it must arrive nullable, because a rule written before it
existed was compared in the channel's canonical unit and must not be silently reinterpreted.
"""

from __future__ import annotations

import datetime as dt
import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from drillai.core.config import reset_settings_cache

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
BEFORE = "e6b2f47c9a15"  # the revision this migration revises
AFTER = "f7c3a8d21b46"


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _migrate(database_url: str, revision: str) -> None:
    command.upgrade(_alembic_config(database_url), revision)


def _downgrade(database_url: str) -> None:
    command.downgrade(_alembic_config(database_url), BEFORE)


def _insert_legacy_rows(
    database_url: str,
    *,
    status: str = "open",
    severity: str = "warning",
    alerts: int = 1,
    events: int = 0,
    highest_sequence: int = 0,
) -> None:
    """The rows a pre-CP9 runtime would have written: a spellings-only status, and a bare sequence."""

    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    now = dt.datetime(2026, 3, 15, 8, 0)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO organizations (id, slug, name, kind, timezone, default_unit_system, "
                    "default_locale, settings, is_active, created_at, updated_at) VALUES ('org_legacy', "
                    "'legacy', 'Legacy', 'operator', 'UTC', 'metric', 'en', '{}', 1, :now, :now)"
                ),
                {"now": now},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO projects (id, org_id, name, status, datum_policy, settings, created_at, "
                    "updated_at) VALUES ('prj_legacy', 'org_legacy', 'Legacy project', 'active', 'rkb', "
                    "'{}', :now, :now)"
                ),
                {"now": now},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO wells (id, org_id, project_id, name, status, well_type, elevation_datum, "
                    "is_offshore, twin_state, target_formations, tags, attributes, is_demo_fixture, "
                    "created_at, updated_at) VALUES ('wel_legacy', 'org_legacy', 'prj_legacy', 'LEG-1', "
                    "'drilling', 'development_producer', 'rkb', 0, 'design', '[]', '[]', '{}', 0, "
                    ":now, :now)"
                ),
                {"now": now},
            )
            for index in range(alerts):
                connection.execute(
                    sa.text(
                        "INSERT INTO alerts (id, org_id, well_id, kind, severity, status, title, "
                        "action_level, raised_at, observed_value, threshold_value, unit, "
                        "notified_channels, attributes, created_at, updated_at) VALUES (:id, "
                        "'org_legacy', 'wel_legacy', 'telemetry', :severity, :status, 'Legacy alert', "
                        "'L2', :now, 4200.0, 4000.0, 'psi', '[]', '{}', :now, :now)"
                    ),
                    {"id": f"alt_legacy_{index}", "severity": severity, "status": status, "now": now},
                )
            for index in range(events):
                connection.execute(
                    sa.text(
                        "INSERT INTO outbox_events (id, org_id, event_type, sequence, subject_kind, "
                        "subject_id, occurred_at, payload, schema_version, created_at, updated_at) "
                        "VALUES (:id, 'org_legacy', 'telemetry.received', :sequence, 'time_series', "
                        "'tms_1', :now, '{}', 1, :now, :now)"
                    ),
                    {"id": f"obx_legacy_{index}", "sequence": index + 1, "now": now},
                )
            if events and highest_sequence > events:
                # A stream whose numbers are already sparse (rows were pruned by a retention job): the
                # counter must continue from the highest number, not from the row count.
                connection.execute(
                    sa.text(
                        "INSERT INTO outbox_events (id, org_id, event_type, sequence, subject_kind, "
                        "subject_id, occurred_at, payload, schema_version, created_at, updated_at) "
                        "VALUES ('obx_legacy_high', 'org_legacy', 'telemetry.received', :sequence, "
                        "'time_series', 'tms_1', :now, '{}', 1, :now, :now)"
                    ),
                    {"sequence": highest_sequence, "now": now},
                )
    finally:
        engine.dispose()


def _rows(database_url: str, sql: str) -> list[tuple]:
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        with engine.begin() as connection:
            return list(connection.execute(sa.text(sql)).all())
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _settings_cache():
    reset_settings_cache()
    yield
    reset_settings_cache()


def test_the_old_spellings_are_repaired_to_the_closed_vocabulary(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/alert-legacy.db"
    _migrate(database_url, BEFORE)
    _insert_legacy_rows(database_url, status="open", severity="warning", alerts=3)

    _migrate(database_url, AFTER)

    rows = _rows(database_url, "SELECT DISTINCT status, severity FROM alerts")
    assert rows == [("raised", "medium")], rows

    # The column the rules need is present and nullable: an old rule keeps meaning what it meant.
    columns = _rows(database_url, "PRAGMA table_info(alert_rules)")
    unit_column = next(row for row in columns if row[1] == "unit")
    assert unit_column[3] == 0, "nullable, so existing rules are not reinterpreted"


def test_a_status_nobody_has_mapped_stops_the_migration_and_is_named(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/alert-unknown.db"
    _migrate(database_url, BEFORE)
    _insert_legacy_rows(database_url, status="haemorrhaging", alerts=2)

    with pytest.raises(RuntimeError) as failure:
        _migrate(database_url, AFTER)
    message = str(failure.value)
    assert "alerts" in message and "status" in message
    assert "haemorrhaging" in message
    assert "2 row(s)" in message, "the operator needs the size of the problem, not just its existence"

    # Refused means untouched: the rows still carry the value the migration would not guess at.
    assert _rows(database_url, "SELECT DISTINCT status FROM alerts") == [("haemorrhaging",)]
    # and the allocator was not left behind half-built
    tables = [row[0] for row in _rows(database_url, "SELECT name FROM sqlite_master WHERE type='table'")]
    assert "outbox_sequences" not in tables or not _rows(database_url, "SELECT * FROM outbox_sequences")


def test_the_sequence_is_seeded_from_the_highest_number_already_used(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/alert-sequence.db"
    _migrate(database_url, BEFORE)
    _insert_legacy_rows(database_url, events=3, highest_sequence=41)

    _migrate(database_url, AFTER)

    assert _rows(database_url, "SELECT org_id, last_sequence FROM outbox_sequences") == [("org_legacy", 41)]

    # The next writer continues from 42 rather than restarting at 1, which a resuming consumer would
    # read as "everything after 41 is missing" — or worse, as "nothing happened".
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("UPDATE outbox_sequences SET last_sequence = last_sequence + 1 WHERE org_id = 'org_legacy'")
            )
            next_number = connection.execute(
                sa.text("SELECT last_sequence FROM outbox_sequences WHERE org_id = 'org_legacy'")
            ).scalar_one()
        assert next_number == 42
    finally:
        engine.dispose()


def test_a_tenant_with_no_events_gets_no_counter_row(tmp_path) -> None:
    """An organisation that has never emitted anything has no stream to continue, and the allocator
    creates its row on first use rather than inventing one for every tenant."""

    database_url = f"sqlite+aiosqlite:///{tmp_path}/alert-empty.db"
    _migrate(database_url, BEFORE)
    _insert_legacy_rows(database_url, events=0)

    _migrate(database_url, AFTER)

    assert _rows(database_url, "SELECT COUNT(*) FROM outbox_sequences") == [(0,)]


def test_the_vocabulary_reverses_and_the_allocator_drops_on_downgrade(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path}/alert-cycle.db"
    _migrate(database_url, BEFORE)
    _insert_legacy_rows(database_url, status="open", severity="warning", events=1)

    _migrate(database_url, AFTER)
    assert _rows(database_url, "SELECT DISTINCT status FROM alerts") == [("raised",)]

    _downgrade(database_url)
    assert _rows(database_url, "SELECT DISTINCT status FROM alerts") == [("open",)]
    assert _rows(database_url, "SELECT DISTINCT severity FROM alerts") == [("warning",)]
    tables = [row[0] for row in _rows(database_url, "SELECT name FROM sqlite_master WHERE type='table'")]
    assert "outbox_sequences" not in tables, "the allocator is new, so the downgrade removes it"
    columns = [row[1] for row in _rows(database_url, "PRAGMA table_info(alert_rules)")]
    assert "unit" not in columns

    # ...and the chain applies again on the database the downgrade left behind.
    _migrate(database_url, AFTER)
    assert _rows(database_url, "SELECT DISTINCT status FROM alerts") == [("raised",)]
    assert _rows(database_url, "SELECT org_id, last_sequence FROM outbox_sequences") == [("org_legacy", 1)]


def test_per_well_outbox_sequence_migration_backfills_and_reverses_cleanly(tmp_path) -> None:
    """Revision `a9d4e21b8c60` backfills per-well contiguous sequences (`well_sequence`) from
    interleaved organization events, seeds `outbox_well_sequences`, and reverses cleanly."""

    database_url = f"sqlite+aiosqlite:///{tmp_path}/well-seq-migration.db"
    _migrate(database_url, AFTER)

    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    now = dt.datetime(2026, 3, 15, 8, 0)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO organizations (id, slug, name, kind, timezone, default_unit_system, "
                    "default_locale, settings, is_active, created_at, updated_at) VALUES ('org_m', "
                    "'multi', 'Multi', 'operator', 'UTC', 'metric', 'en', '{}', 1, :now, :now)"
                ),
                {"now": now},
            )
            for seq, wid in ((1, "wel_a"), (2, "wel_b"), (3, "wel_b"), (4, "wel_a"), (5, None)):
                connection.execute(
                    sa.text(
                        "INSERT INTO outbox_events (id, org_id, well_id, event_type, sequence, "
                        "subject_kind, subject_id, occurred_at, payload, schema_version, "
                        "created_at, updated_at) VALUES (:id, 'org_m', :wid, 'telemetry.received', "
                        ":seq, 'time_series', 'tms_1', :now, '{}', 1, :now, :now)"
                    ),
                    {"id": f"obx_m_{seq}", "wid": wid, "seq": seq, "now": now},
                )
    finally:
        engine.dispose()

    _migrate(database_url, "a9d4e21b8c60")

    events = _rows(
        database_url,
        "SELECT sequence, well_id, well_sequence FROM outbox_events ORDER BY sequence ASC",
    )
    assert events == [
        (1, "wel_a", 1),
        (2, "wel_b", 1),
        (3, "wel_b", 2),
        (4, "wel_a", 2),
        (5, None, None),
    ]
    counters = _rows(
        database_url,
        "SELECT org_id, well_id, last_sequence FROM outbox_well_sequences ORDER BY well_id ASC",
    )
    assert counters == [("org_m", "wel_a", 2), ("org_m", "wel_b", 2)]

    command.downgrade(_alembic_config(database_url), AFTER)
    tables = [row[0] for row in _rows(database_url, "SELECT name FROM sqlite_master WHERE type='table'")]
    assert "outbox_well_sequences" not in tables

    _migrate(database_url, "a9d4e21b8c60")
    counters_again = _rows(
        database_url,
        "SELECT org_id, well_id, last_sequence FROM outbox_well_sequences ORDER BY well_id ASC",
    )
    assert counters_again == [("org_m", "wel_a", 2), ("org_m", "wel_b", 2)]

