"""The two dialect-aware SQL fragments the telemetry layer needs, in one place.

They live in their own module rather than in the service because both the service and the outbox need
them, and a helper shared through a circular import is a helper that eventually cannot be imported at
all. Neither function builds a query: they choose the *dialect's* spelling of a clause and refuse an
untested dialect by name, which is the difference between a platform that supports two databases and one
that silently does something undefined on a third.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from drillai.core.errors import ValidationFailed

#: How many identities one statement may carry. SQLite historically caps a statement's parameters at 999
#: and Postgres at 65 535; four hundred keeps both dialects comfortable and keeps a batch at a handful of
#: statements rather than one per point.
IDENTITY_CHUNK = 400

__all__ = ["IDENTITY_CHUNK", "chunks", "conflict_aware_insert"]


def chunks(items: Sequence[Any], size: int) -> Iterable[list[Any]]:
    materialized = list(items)
    for start in range(0, len(materialized), size):
        yield materialized[start : start + size]


def conflict_aware_insert(dialect: str):
    """The INSERT the platform uses to store a row that may already exist.

    ``ON CONFLICT DO NOTHING`` is the database-level half of the replay guarantee: even if two workers
    race, only one row is written and the loser learns it from the statement's return rather than from an
    exception — which matters because a unique violation inside a savepoint leaves the surrounding
    transaction unusable on SQLite (verified), so an exception-based design would cost the whole batch.
    Postgres and SQLite expose the same clause through different imports; a dialect the platform has
    never been run against is refused *by name*, because compiling something that silently overwrites a
    stored measurement is exactly the failure this guards.
    """

    if dialect == "postgresql":
        return postgresql_insert
    if dialect == "sqlite":
        return sqlite_insert
    raise ValidationFailed(
        f"telemetry has no tested conflict clause for the {dialect} dialect",
        details={"dialect": dialect, "supported": ["postgresql", "sqlite"]},
    )
