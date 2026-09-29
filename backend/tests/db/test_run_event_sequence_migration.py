"""The run-event sequence migration, on a database that already contains duplicates.

The defect this migration repairs was silent: a resumed run wrote events with sequence numbers the
client had already consumed, and nothing in the schema refused them. Existing deployments therefore
hold rows the new constraint would reject, so the migration repairs first and constrains second — and
that ordering is what this module proves, against a real Alembic run on a real file database.

This is a *data* test, not a schema test. A fresh database contains no duplicates, so migrating one
proves nothing about the situation the migration exists for; the fixture here writes the exact row
set the old runtime produced (a suspended run's log, then the resumed stretch numbering from 1 again)
and then asserts on the rows that survive.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import pathlib

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from drillai.core.clock import UTC
from drillai.core.config import reset_settings_cache
from drillai.db.models import RunEvent

BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
BEFORE = "9c1f4b7d5a20"  # the revision before `(run_id, seq)` became unique

# What the old runtime wrote: `run_started`, the approval pause, then the resumed runtime restarting
# its counter from 1 — a repeat of seq 1 and 2, plus a tail event.
DUPLICATED_RUN_ROWS = (
    (1, "run_started"),
    (2, "approval_requested"),
    (1, "run_resumed"),
    (2, "node_started"),
    (3, "run_completed"),
)
# An untouched run in the same database: its numbers are already unique and must not be rewritten.
CLEAN_RUN_ROWS = ((1, "run_started"), (2, "run_completed"))


def _alembic_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_rows(database_url: str, run_id: str, org_id: str, rows, *, happened_at: dt.datetime) -> None:
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        with engine.begin() as connection:
            for index, (seq, event_type) in enumerate(rows):
                connection.execute(
                    RunEvent.__table__.insert().values(
                        id=f"rev_{run_id}_{index}",
                        org_id=org_id,
                        run_id=run_id,
                        seq=seq,
                        type=event_type,
                        message=event_type,
                        payload={},
                        level="info",
                        # Strictly increasing timestamps: the repair orders by them, so the recorded
                        # order has to be unambiguous for the assertion below to mean anything.
                        occurred_at=happened_at + dt.timedelta(seconds=index),
                    )
                )
    finally:
        engine.dispose()


def _rows(database_url: str, run_id: str) -> list[tuple[str, int, str]]:
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                sa.text("SELECT id, seq, type FROM run_events WHERE run_id = :run_id ORDER BY seq, id"),
                {"run_id": run_id},
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]
    finally:
        engine.dispose()


def _append(database_url: str, run_id: str, org_id: str, seq: int) -> None:
    engine = sa.create_engine(database_url.replace("+aiosqlite", ""))
    try:
        with engine.begin() as connection:
            connection.execute(
                RunEvent.__table__.insert().values(
                    id=f"rev_{run_id}_after",
                    org_id=org_id,
                    run_id=run_id,
                    seq=seq,
                    type="post_migration_event",
                    message="post migration event",
                    payload={},
                    level="info",
                    occurred_at=dt.datetime.now(tz=UTC),
                )
            )
    finally:
        engine.dispose()


async def test_the_migration_repairs_duplicate_sequences_then_constrains(tmp_path, monkeypatch):
    database_url = f"sqlite+aiosqlite:///{tmp_path}/migration.db"
    monkeypatch.setenv("DRILLAI_DATABASE_URL", database_url)
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    # `alembic/env.py` reads the settings object, not this test's config object: without dropping the
    # cached settings, a suite run would migrate whichever database the previous test had configured.
    reset_settings_cache()
    config = _alembic_config(database_url)
    upgrade = lambda revision: asyncio.to_thread(command.upgrade, config, revision)  # noqa: E731

    # A database as the defect left it: the old schema, one run with a duplicated log, one without.
    await upgrade(BEFORE)
    broken, clean = "run_duplicated", "run_clean"
    started_at = dt.datetime(2026, 3, 15, 6, 0, tzinfo=UTC)
    await asyncio.to_thread(
        _insert_rows, database_url, broken, "org_1", DUPLICATED_RUN_ROWS, happened_at=started_at
    )
    await asyncio.to_thread(
        _insert_rows, database_url, clean, "org_1", CLEAN_RUN_ROWS, happened_at=started_at
    )
    before_rows = await asyncio.to_thread(_rows, database_url, broken)
    assert [seq for _id, seq, _type in before_rows] == [1, 1, 2, 2, 3]

    await upgrade("head")

    # 1. Nothing was deleted: every row is still there, still carrying its own event type.
    after_rows = await asyncio.to_thread(_rows, database_url, broken)
    assert len(after_rows) == len(before_rows) == len(DUPLICATED_RUN_ROWS)
    assert [type_ for _id, _seq, type_ in after_rows] == [type_ for _seq, type_ in DUPLICATED_RUN_ROWS]

    # 2. The numbers are now dense, ascending and unique, in the order they were recorded.
    sequences = [seq for _id, seq, _type in after_rows]
    assert sequences == [1, 2, 3, 4, 5]
    assert len(set(sequences)) == len(sequences)

    # 3. The repair is deterministic: the same input yields the same numbering (same ids, same seqs).
    assert after_rows == [(f"rev_{broken}_{index}", index + 1, type_) for index, (_seq, type_) in enumerate(DUPLICATED_RUN_ROWS)]

    # 4. An already-valid run is left exactly as it was — the repair is scoped to the runs it must fix.
    assert await asyncio.to_thread(_rows, database_url, clean) == [
        (f"rev_{clean}_{index}", seq, type_) for index, (seq, type_) in enumerate(CLEAN_RUN_ROWS)
    ]

    # 5. The constraint is in the schema now, and the repaired run accepts the next number the runtime
    #    would write (`max + 1`, which is what `_next_event_seq` computes) but refuses a repeat.
    await asyncio.to_thread(_append, database_url, broken, "org_1", 6)
    assert [seq for _id, seq, _type in await asyncio.to_thread(_rows, database_url, broken)] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]
    with pytest.raises(sa.exc.IntegrityError):
        await asyncio.to_thread(_append, database_url, broken, "org_1", 3)

    # 6. And the migration is reversible: the constraint can be dropped and re-created.
    await upgrade(BEFORE)
    await upgrade("head")
    assert [seq for _id, seq, _type in await asyncio.to_thread(_rows, database_url, broken)] == [
        1,
        2,
        3,
        4,
        5,
        6,
    ]
    reset_settings_cache()
