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

Sequence allocation lives on two axes on purpose:

1. ``outbox_sequences`` holds one counter row per organization (`sequence`), giving tenant-wide
   consumers a gap-free total order across all wells;
2. ``outbox_well_sequences`` holds one counter row per ``(org_id, well_id)`` (`well_sequence`), giving
   each well's live stream (`GET /wells/{well_id}/live/stream`) a gap-free 1..N sequence of its own so
   that high-rate activity on Well B never inflates the backlog calculation or creates false gaps on
   Well A.

Both counters are advanced with ``UPDATE ... RETURNING`` in deterministic order (`org` then `well`), so
concurrent writers are serialized without unique-index collisions or deadlocks.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import utc_now
from drillai.core.errors import ValidationFailed
from drillai.core.ids import new_id
from drillai.db.models import OutboxEvent, OutboxSequence, OutboxWellSequence
from drillai.telemetry.sql import conflict_aware_insert
from drillai.telemetry.vocabulary import DOMAIN_EVENT_TYPES

#: The envelope version. Bumped when a field changes *meaning* — a consumer can then refuse an envelope
#: it does not understand instead of misreading one. Adding an optional field does not bump it.
ENVELOPE_VERSION = 1

#: The scoped stream-cursor wire version.
STREAM_CURSOR_VERSION = 1


@dataclass(frozen=True)
class StreamCursor:
    """A validated, scope-bound position on either an organization stream or one well's stream."""

    org_id: str
    well_id: str | None
    sequence: int
    version: int = STREAM_CURSOR_VERSION

    @property
    def scope_kind(self) -> str:
        return "well" if self.well_id is not None else "org"


def encode_stream_cursor(
    *, org_id: str, well_id: str | None = None, sequence: int
) -> str:
    """Encode a stream cursor binding ``(org_id, well_id, sequence)`` into an opaque token."""

    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise ValidationFailed(
            "the stream sequence cursor must be a non-negative integer",
            details={"field": "sequence", "reason": "invalid_cursor", "value": sequence},
        )
    if not org_id:
        raise ValidationFailed(
            "the stream cursor requires an organization scope",
            details={"field": "org_id", "reason": "invalid_cursor"},
        )
    payload = json.dumps(
        {
            "v": STREAM_CURSOR_VERSION,
            "org": org_id,
            "well": well_id,
            "seq": int(sequence),
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_stream_cursor(
    cursor: str,
    *,
    expected_org_id: str | None = None,
    expected_well_id: str | None = None,
    require_well_scope: bool = False,
) -> StreamCursor:
    """Decode and validate a scoped stream cursor against the caller's organization and well."""

    if not isinstance(cursor, str) or not cursor.strip():
        raise ValidationFailed(
            "the stream cursor is not a position this API produced",
            details={"field": "cursor", "reason": "invalid_cursor", "value": str(cursor)[:64]},
        )
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValidationFailed(
            "the stream cursor is not a position this API produced",
            details={"field": "cursor", "reason": "invalid_cursor", "value": cursor[:64]},
        ) from None
    if not isinstance(data, dict):
        raise ValidationFailed(
            "the stream cursor does not name a position",
            details={"field": "cursor", "reason": "invalid_cursor", "value": cursor[:64]},
        )
    version = data.get("v")
    org_id = data.get("org")
    well_id = data.get("well")
    seq = data.get("seq")
    if (
        isinstance(version, bool)
        or version != STREAM_CURSOR_VERSION
        or not isinstance(org_id, str)
        or not org_id
        or (well_id is not None and (not isinstance(well_id, str) or not well_id))
        or isinstance(seq, bool)
        or not isinstance(seq, int)
        or seq < 0
    ):
        raise ValidationFailed(
            "the stream cursor does not carry a valid version, scope and sequence",
            details={"field": "cursor", "reason": "invalid_cursor", "value": cursor[:64]},
        )
    if expected_org_id is not None and org_id != expected_org_id:
        raise ValidationFailed(
            "the stream cursor belongs to another organisation",
            details={
                "field": "cursor",
                "reason": "cursor_scope_mismatch",
                "expected_org_id": expected_org_id,
            },
        )
    if require_well_scope and well_id is None:
        raise ValidationFailed(
            "the stream cursor is organization-scoped rather than scoped to this well",
            details={
                "field": "cursor",
                "reason": "cursor_scope_mismatch",
                "expected_well_id": expected_well_id,
                "cursor_well_id": None,
            },
        )
    if expected_well_id is not None and well_id != expected_well_id:
        raise ValidationFailed(
            "the stream cursor belongs to another well stream",
            details={
                "field": "cursor",
                "reason": "cursor_scope_mismatch",
                "expected_well_id": expected_well_id,
                "cursor_well_id": well_id,
            },
        )
    return StreamCursor(org_id=org_id, well_id=well_id, sequence=seq, version=version)


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
    well_sequence: int | None = None

    @property
    def envelope(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "well_sequence": self.well_sequence,
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


def well_stream_sequence(row: OutboxEvent) -> int:
    """The contiguous per-well sequence of a stored outbox row (falling back to ``sequence`` only for
    rows without a well scope)."""

    if row.well_sequence is not None:
        return int(row.well_sequence)
    return int(row.sequence)


def envelope_of(row: OutboxEvent, *, stream_well_id: str | None = None) -> dict[str, Any]:
    """Shape a stored row as an envelope.

    When ``stream_well_id`` is omitted, ``sequence`` is the organization-wide sequence (`DomainEvent.envelope`).
    When ``stream_well_id`` is provided (for `GET /wells/{well_id}/live/stream`), ``sequence`` is the
    well's own contiguous `well_sequence`, ``org_sequence`` preserves the tenant-wide number, and
    ``cursor`` carries the validated well-scoped cursor token.
    """

    base = DomainEvent(
        id=row.id,
        sequence=int(row.sequence),
        well_sequence=int(row.well_sequence) if row.well_sequence is not None else None,
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
    if stream_well_id is None:
        return base
    stream_seq = well_stream_sequence(row)
    return {
        **base,
        "sequence": stream_seq,
        "well_sequence": stream_seq,
        "org_sequence": int(row.sequence),
        "cursor": encode_stream_cursor(
            org_id=row.org_id, well_id=stream_well_id, sequence=stream_seq
        ),
    }


async def allocate_sequence(session: AsyncSession, org_id: str) -> int:
    """The next sequence number for this organization's stream."""

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


async def allocate_well_sequence(session: AsyncSession, org_id: str, well_id: str) -> int:
    """The next contiguous sequence number for ``(org_id, well_id)``."""

    advanced = await session.execute(
        update(OutboxWellSequence)
        .where(OutboxWellSequence.org_id == org_id, OutboxWellSequence.well_id == well_id)
        .values(last_sequence=OutboxWellSequence.last_sequence + 1)
        .returning(OutboxWellSequence.last_sequence)
    )
    value = advanced.scalar_one_or_none()
    if value is not None:
        return int(value)

    insert = conflict_aware_insert(session.get_bind().dialect.name)
    created = await session.execute(
        insert(OutboxWellSequence)
        .values(org_id=org_id, well_id=well_id, last_sequence=1)
        .on_conflict_do_nothing(index_elements=["org_id", "well_id"])
        .returning(OutboxWellSequence.last_sequence)
    )
    value = created.scalar_one_or_none()
    if value is not None:
        return int(value)

    advanced = await session.execute(
        update(OutboxWellSequence)
        .where(OutboxWellSequence.org_id == org_id, OutboxWellSequence.well_id == well_id)
        .values(last_sequence=OutboxWellSequence.last_sequence + 1)
        .returning(OutboxWellSequence.last_sequence)
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
    """Record one domain event in the caller's transaction. Returns the envelope that was stored."""

    if type not in DOMAIN_EVENT_TYPES:
        raise ValidationFailed(
            "the domain event type is not one the platform publishes",
            details={"field": "type", "value": type, "allowed": list(DOMAIN_EVENT_TYPES)},
        )
    # Lock order is always org counter first, well counter second, preventing deadlocks across wells.
    sequence = await allocate_sequence(session, org_id)
    well_sequence = (
        await allocate_well_sequence(session, org_id, well_id) if well_id is not None else None
    )
    moment = occurred_at or utc_now()
    row = OutboxEvent(
        id=new_id("obx"),
        org_id=org_id,
        sequence=sequence,
        well_sequence=well_sequence,
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
        well_sequence=well_sequence,
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
    cursor: str | None = None,
    well_id: str | None = None,
    types: tuple[str, ...] | None = None,
    limit: int = 200,
) -> list[OutboxEvent]:
    """The stream as of a position: strictly greater than ``after_sequence`` (or ``cursor``), in order.

    When ``well_id`` is given, the stream is that well's contiguous `well_sequence` stream; when
    ``well_id`` is ``None``, the stream is the organization-wide `sequence` stream. If ``cursor`` is
    supplied, its scope (`org_id` and `well_id`) is validated before reading.
    """

    if cursor is not None:
        decoded = decode_stream_cursor(
            cursor,
            expected_org_id=org_id,
            expected_well_id=well_id,
            require_well_scope=(well_id is not None),
        )
        effective_after = decoded.sequence
    else:
        if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or after_sequence < 0:
            raise ValidationFailed(
                "after_sequence must be a non-negative integer",
                details={"field": "after_sequence", "reason": "invalid_cursor", "value": after_sequence},
            )
        effective_after = after_sequence

    bound = max(1, min(limit, 1000))
    if well_id is not None:
        well_seq_expr = func.coalesce(OutboxEvent.well_sequence, OutboxEvent.sequence)
        statement = (
            select(OutboxEvent)
            .where(
                OutboxEvent.org_id == org_id,
                OutboxEvent.well_id == well_id,
                well_seq_expr > effective_after,
            )
            .order_by(well_seq_expr.asc(), OutboxEvent.sequence.asc())
            .limit(bound)
        )
    else:
        statement = (
            select(OutboxEvent)
            .where(OutboxEvent.org_id == org_id, OutboxEvent.sequence > effective_after)
            .order_by(OutboxEvent.sequence.asc())
            .limit(bound)
        )
    if types:
        statement = statement.where(OutboxEvent.event_type.in_(types))
    return list((await session.execute(statement)).scalars().all())


async def stream_position(
    session: AsyncSession, org_id: str, *, well_id: str | None = None
) -> int:
    """The current end of the stream — on the well's contiguous sequence when ``well_id`` is given, or
    on the organization sequence when ``well_id`` is ``None``."""

    if well_id is not None:
        well_seq_expr = func.coalesce(OutboxEvent.well_sequence, OutboxEvent.sequence)
        statement = select(func.max(well_seq_expr)).where(
            OutboxEvent.org_id == org_id, OutboxEvent.well_id == well_id
        )
    else:
        statement = select(func.max(OutboxEvent.sequence)).where(OutboxEvent.org_id == org_id)
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
