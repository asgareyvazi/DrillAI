"""Telemetry suite fixtures: the two-tenant fabric plus a measured statement counter.

``statement_counter`` exists because "this endpoint does not have an N+1" is a claim about the SQL the
endpoint issues, and a claim about SQL that is not measured is a belief. The counter attaches to the
real engine the application uses and records every statement executed inside the block, so a test can
assert a ceiling — and, more tellingly, assert that the ceiling does *not* move when the number of
channels grows.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import event
from tests.fixtures.fabric import DDR_BYTES, Fabric, Tenant, fabric

__all__ = ["DDR_BYTES", "Fabric", "Tenant", "fabric", "statement_counter"]


@contextmanager
def statement_counter(fabric: Fabric) -> Iterator[list[str]]:
    """Record every SQL statement the application issues inside the block.

    Yields the list itself, so a test can assert a count, print the statements when it fails, or check
    that a particular statement was never issued (``SELECT`` inside a loop, say).
    """

    engine = fabric.app.state.database.engine
    sync_engine = getattr(engine, "sync_engine", engine)
    recorded: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        recorded.append(" ".join(str(statement).split()))

    event.listen(sync_engine, "before_cursor_execute", _record)
    try:
        yield recorded
    finally:
        event.remove(sync_engine, "before_cursor_execute", _record)


@pytest.fixture
def counter(fabric: Fabric):
    """``with counter() as statements: ...`` — bound to the fabric's engine."""

    def _make() -> Iterator[list[str]]:
        return statement_counter(fabric)

    return _make
