"""The transactional outbox: a mutation and the event that describes it, in one transaction.

The rule this module exists to enforce is that the platform never has to choose between "the change
happened" and "the change was announced". Writing an event *after* committing the mutation loses events
whenever the process dies in between; writing it *before* announces things that were then rolled back.
Both failures are silent, which is why they survive in systems for years. So the event row is inserted
into ``outbox_events`` inside the caller's transaction, and the same commit that stores a measurement
stores the fact that a measurement arrived:

    async with session.begin():                 # or whatever the caller's transaction is
        point = ...                             # the mutation
        await emit(session, org_id=..., type="telemetry.received", ...)

    # commit → both rows visible; rollback → neither.

Consumers (the WebSocket feed, an integration worker) read ``events_since`` and acknowledge with
``mark_published``. The feed does not *need* the published flag — a live subscriber reads by sequence —
but a durable consumer does, and the flag is what makes "this event has been delivered" a recorded fact
rather than an assumption.

Sequence allocation is the part worth reading twice. The counter lives in ``outbox_sequences``, one row
per organization, advanced with ``UPDATE ... RETURNING``: the row lock serializes concurrent writers
instead of the unique index rejecting the loser, and a rejected loser would take the *measurement* down
with it. ``allocate_sequence`` therefore never raises for a normal race, and there is no retry loop
around an exception anywhere in this file.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import utc_now
from drillai.core.errors import ValidationFailed
from drillai.core.ids import new_id
from drillai.db.models import OutboxEvent, OutboxSequence
from drillai.telemetry.sql import conflict_aware_insert
from drillai.telemetry.vocabulary import DOMAIN_EVENT_TYPES

#: The envelope version. Bumped when a field changes *meaning* — a consumer can then refuse an envelope
#: it does not understand instead of misreading one. Adding an optional field does not bump it.
ENVELOPE_VERSION = 1


@dataclass(frozen=True)
class DomainEvent:
    """One fact, with everything a consumer needs to place it: what, where, when, and in what order."""

    id: str
    sequence: int
    type: str
    org_id: str
    subject_kind: str
    subject_id: str | None
    well_id: str | None
    wellbore_id: str | None
    occurred_at: dt.datetime
    published_at: dt.datetime | None
    trace_id: str | None
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def envelope(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "type": self.type,
            "org_id": self.org_id,
            "subject": {"kind": self.subject_kind, "id": self.subject_id},
            "well_id": self.well_id,
            "wellbore_id": self.wellbore_id,
            "occurred_at": self.occurred_at.isoformat(),
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "trace_id": self.trace_id,
            "schema_version": ENVELOPE_VERSION,
            "payload": self.payload,
        }


def envelope_of(row: OutboxEvent) -> dict[str, Any]:
    """Shape a stored row as an envelope — the same shape ``DomainEvent.envelope`` produces."""

    return DomainEvent(
        id=row.id,
        sequence=int(row.sequence),
        type=row.event_type,
        org_id=row.org_id,
        subject_kind=row.subject_kind,
        subject_id=row.subject_id,
        well_id=row.well_id,
        wellbore_id=row.wellbore_id,
        occurred_at=row.occurred_at,
        published_at=row.published_at,
        trace_id=row.trace_id,
        payload=dict(row.payload or {}),
    ).envelope


async def allocate_sequence(session: AsyncSession, org_id: str) -> int:
    """The next sequence number for this organization's stream.

    Three statements in the worst case and no exceptions in any of them:

    1. ``UPDATE ... RETURNING`` the counter — the normal path.
    2. If no counter row exists yet, insert one with ``ON CONFLICT DO NOTHING``. Two concurrent first
       writers both try this; one inserts, the other is told nothing landed.
    3. The writer that lost step 2 re-runs step 1, which now finds the row.

    Step 3 is not a retry of a *failed* operation: nothing failed, the row it was looking for simply did
    not exist yet and does now. The distinction is the reason this can be written without a loop that
    catches exceptions.
    """

    advanced = await session.execute(
        update(OutboxSequence)
        .where(OutboxSequence.org_id == org_id)
        .values(last_sequence=OutboxSequence.last_sequence + 1)
        .returning(OutboxSequence.last_sequence)
    )
    value = advanced.scalar_one_or_none()
    if value is not None:
        return int(value)

    insert = conflict_aware_insert(session.get_bind().dialect.name)
    created = await session.execute(
        insert(OutboxSequence)
        .values(org_id=org_id, last_sequence=1)
        .on_conflict_do_nothing(index_elements=["org_id"])
        .returning(OutboxSequence.last_sequence)
    )
    value = created.scalar_one_or_none()
    if value is not None:
        return int(value)

    advanced = await session.execute(
        update(OutboxSequence)
        .where(OutboxSequence.org_id == org_id)
        .values(last_sequence=OutboxSequence.last_sequence + 1)
        .returning(OutboxSequence.last_sequence)
    )
    return int(advanced.scalar_one())


async def emit(
    session: AsyncSession,
    *,
    org_id: str,
    type: str,  # the envelope's own field name (the platform has no shadowing builtin here)
    subject_kind: str,
    subject_id: str | None = None,
    well_id: str | None = None,
    wellbore_id: str | None = None,
    payload: dict[str, Any] | None = None,
    trace_id: str | None = None,
    occurred_at: dt.datetime | None = None,
) -> DomainEvent:
    """Record one domain event in the caller's transaction. Returns the envelope that was stored.

    ``type`` is validated against the platform's vocabulary: a consumer that has to handle unknown types
    can ignore them safely, but a *typo* would look exactly like an unknown type while silently
    removing the event from every handler that wanted it.
    """

    if type not in DOMAIN_EVENT_TYPES:
        raise ValidationFailed(
            "the domain event type is not one the platform publishes",
            details={"field": "type", "value": type, "allowed": list(DOMAIN_EVENT_TYPES)},
        )
    sequence = await allocate_sequence(session, org_id)
    moment = occurred_at or utc_now()
    row = OutboxEvent(
        id=new_id("obx"),
        org_id=org_id,
        sequence=sequence,
        event_type=type,
        subject_kind=subject_kind,
        subject_id=subject_id,
        well_id=well_id,
        wellbore_id=wellbore_id,
        occurred_at=moment,
        payload=dict(payload or {}),
        trace_id=trace_id,
    )
    session.add(row)
    await session.flush()
    return DomainEvent(
        id=row.id,
        sequence=sequence,
        type=type,
        org_id=org_id,
        subject_kind=subject_kind,
        subject_id=subject_id,
        well_id=well_id,
        wellbore_id=wellbore_id,
        occurred_at=moment,
        published_at=None,
        trace_id=trace_id,
        payload=dict(payload or {}),
    )


async def events_since(
    session: AsyncSession,
    org_id: str,
    *,
    after_sequence: int = 0,
    well_id: str | None = None,
    types: tuple[str, ...] | None = None,
    limit: int = 200,
) -> list[OutboxEvent]:
    """The stream as of a position: strictly greater than ``after_sequence``, in sequence order.

    ``after_sequence`` is what a reconnecting consumer sends. Ordering by sequence (rather than by
    ``occurred_at``) is what makes the resume exact: two events can share a millisecond, and a timestamp
    cursor would then skip one of them — the gap the mission forbids.
    """

    bound = max(1, min(limit, 1000))
    statement = (
        select(OutboxEvent)
        .where(OutboxEvent.org_id == org_id, OutboxEvent.sequence > after_sequence)
        .order_by(OutboxEvent.sequence.asc())
        .limit(bound)
    )
    if well_id is not None:
        statement = statement.where(OutboxEvent.well_id == well_id)
    if types:
        statement = statement.where(OutboxEvent.event_type.in_(types))
    return list((await session.execute(statement)).scalars().all())


async def stream_position(
    session: AsyncSession, org_id: str, *, well_id: str | None = None
) -> int:
    """The current end of the stream — what a client is told at connect so it can detect a gap later."""

    statement = select(func.max(OutboxEvent.sequence)).where(OutboxEvent.org_id == org_id)
    if well_id is not None:
        statement = statement.where(OutboxEvent.well_id == well_id)
    return int((await session.execute(statement)).scalar_one() or 0)


async def mark_published(session: AsyncSession, event_ids: list[str]) -> int:
    """Record that a durable consumer has taken delivery. Idempotent: a second call changes nothing."""

    if not event_ids:
        return 0
    result = await session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id.in_(event_ids), OutboxEvent.published_at.is_(None))
        .values(published_at=utc_now())
    )
    return int(result.rowcount or 0)
