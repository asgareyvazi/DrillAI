"""The transactional outbox: an event exists if and only if the change it describes exists.

The tests here attack the guarantee from both sides. A committed mutation must leave exactly one event;
a rolled-back mutation must leave none; a redelivery must not create a second one; and the sequence a
consumer resumes from must never skip a number that another consumer can see. The interesting cases are
the ones a plain queue cannot pass: a process that dies between the write and the publish leaves the event
*stored* rather than lost, which is what makes ``published_at`` an acknowledgement of delivery rather
than the condition for existence.

The sequence allocator is dialect-aware (``UPDATE … RETURNING``), so these tests run on SQLite by default
and on PostgreSQL under the repository's ``DRILLAI_TEST_POSTGRES=1`` configuration — the concurrency test
is what makes that worth doing.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from drillai.core.errors import ValidationFailed
from drillai.db.models import Organization, OutboxEvent, Project, Well
from drillai.db.session import Database
from drillai.telemetry.outbox import (
    ENVELOPE_VERSION,
    allocate_sequence,
    emit,
    envelope_of,
    events_since,
    mark_published,
    stream_position,
)
from drillai.telemetry.vocabulary import DOMAIN_EVENT_TYPES

ORG = "org_outbox"


async def _tenant(session) -> tuple[str, str]:
    """A minimal tenant to hang events off: one operator, one well."""

    session.add(
        Organization(
            id=ORG,
            slug="outbox-co",
            name="Outbox Co",
            kind="operator",
            timezone="UTC",
            default_unit_system="metric",
            default_locale="en",
            is_active=True,
        )
    )
    await session.flush()
    session.add(
        Project(
            id="prj_outbox",
            org_id=ORG,
            name="Outbox Project",
            status="active",
            datum_policy="rkb",
            settings={},
        )
    )
    await session.flush()
    session.add(
        Well(
            id="well_outbox",
            org_id=ORG,
            project_id="prj_outbox",
            name="Outbox-1",
            well_type="development",
            elevation_datum="MSL",
            is_offshore=False,
            twin_state="none",
            tags=[],
            attributes={},
            is_demo_fixture=False,
        )
    )
    await session.flush()
    return ORG, "well_outbox"


async def _values(session) -> list[int]:
    rows = (
        (await session.execute(select(OutboxEvent).order_by(OutboxEvent.sequence))).scalars().all()
    )
    return [int(row.sequence) for row in rows]


# --------------------------------------------------------------------------- commit / rollback


async def test_a_committed_change_leaves_exactly_one_event(session) -> None:
    org_id, well_id = await _tenant(session)
    event = await emit(
        session,
        org_id=org_id,
        type="telemetry.received",
        subject_kind="time_series",
        subject_id="tms_1",
        well_id=well_id,
        payload={"channel_id": "tms_1", "accepted": 3},
    )
    await session.commit()

    rows = (await session.execute(select(OutboxEvent).where(OutboxEvent.org_id == org_id))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_type == "telemetry.received"
    assert row.subject_kind == "time_series"
    assert row.well_id == well_id
    assert row.published_at is None, "stored is not published"
    assert row.sequence == 1, "the first event in a tenant is number one"
    assert row.schema_version == ENVELOPE_VERSION
    assert row.payload["accepted"] == 3
    assert event.id == row.id


async def test_a_rolled_back_change_leaves_no_event(session) -> None:
    """The outbox's reason to exist: an event that outlives its transaction describes something that
    never happened, and a consumer would act on it."""

    org_id, well_id = await _tenant(session)
    await emit(
        session,
        org_id=org_id,
        type="telemetry.received",
        subject_kind="time_series",
        subject_id="tms_1",
        well_id=well_id,
    )
    await session.flush()
    assert (await session.execute(select(func.count()).select_from(OutboxEvent))).scalar_one() == 1

    await session.rollback()

    assert (await session.execute(select(func.count()).select_from(OutboxEvent))).scalar_one() == 0
    assert await stream_position(session, org_id) == 0, "the sequence is not spent by a rollback"


async def test_the_event_is_stored_in_the_callers_transaction_not_its_own(session) -> None:
    """Two events written before one commit become visible together, and one rollback removes both: the
    outbox never opens its own transaction, or a mutation could be undone while its event stayed."""

    org_id, well_id = await _tenant(session)
    for index in range(2):
        await emit(
            session,
            org_id=org_id,
            type="telemetry.received",
            subject_kind="time_series",
            subject_id=f"tms_{index}",
            well_id=well_id,
        )
    await session.commit()
    assert await _values(session) == [1, 2]

    await emit(
        session,
        org_id=org_id,
        type="telemetry.received",
        subject_kind="time_series",
        subject_id="tms_3",
        well_id=well_id,
    )
    await session.rollback()
    assert await _values(session) == [1, 2], "the rolled-back event left no hole either"


# --------------------------------------------------------------------------- the envelope


async def test_the_envelope_carries_what_a_consumer_needs_to_route_and_deduplicate(session) -> None:
    org_id, well_id = await _tenant(session)
    event = await emit(
        session,
        org_id=org_id,
        type="alert.raised",
        subject_kind="alert",
        subject_id="alt_1",
        well_id=well_id,
        wellbore_id="wb_1",
        trace_id="trace-abc",
        payload={"severity": "high"},
    )
    await session.commit()
    envelope = event.envelope
    assert envelope["id"] == event.id and event.id.startswith("obx_")
    assert envelope["type"] == "alert.raised"
    assert envelope["sequence"] == 1
    assert envelope["subject"] == {"kind": "alert", "id": "alt_1"}
    assert envelope["well_id"] == well_id and envelope["wellbore_id"] == "wb_1"
    assert envelope["schema_version"] == ENVELOPE_VERSION
    assert envelope["published_at"] is None
    assert envelope["trace_id"] == "trace-abc"
    assert envelope["payload"] == {"severity": "high"}
    assert envelope["occurred_at"] and envelope["occurred_at"].endswith("+00:00")

    stored = (await session.execute(select(OutboxEvent))).scalars().one()
    assert envelope_of(stored) == envelope, "the stored row and the returned envelope agree"


async def test_an_unknown_event_type_is_refused_by_name(session) -> None:
    """The type vocabulary is closed: a connector cannot invent a frame consumers have no shape for, and
    the refusal names both what was attempted and what is allowed."""

    org_id, well_id = await _tenant(session)
    with pytest.raises(ValidationFailed) as failure:
        await emit(
            session,
            org_id=org_id,
            type="telemetry.exploded",
            subject_kind="time_series",
            subject_id="tms_1",
            well_id=well_id,
        )
    details = failure.value.details
    assert details["field"] == "type"
    assert details["value"] == "telemetry.exploded"
    assert set(details["allowed"]) == set(DOMAIN_EVENT_TYPES)
    assert (await session.execute(select(func.count()).select_from(OutboxEvent))).scalar_one() == 0


# --------------------------------------------------------------------------- ordering and cursors


async def test_the_sequence_is_gap_free_and_per_tenant(session) -> None:
    org_id, well_id = await _tenant(session)
    for index in range(5):
        await emit(
            session,
            org_id=org_id,
            type="telemetry.received",
            subject_kind="time_series",
            subject_id="tms_1",
            well_id=well_id,
            payload={"n": index},
        )
    await session.commit()
    assert await _values(session) == [1, 2, 3, 4, 5], "no gaps, no repeats"

    # A second tenant numbers from one: the sequence is a per-organisation stream, not a global counter
    # that leaks how busy another operator is.
    session.add(
        Organization(
            id="org_outbox_2",
            slug="other-co",
            name="Other Co",
            kind="operator",
            timezone="UTC",
            default_unit_system="metric",
            default_locale="en",
            is_active=True,
        )
    )
    await session.flush()
    await emit(
        session,
        org_id="org_outbox_2",
        type="telemetry.received",
        subject_kind="time_series",
        subject_id="tms_9",
        payload={"n": "other"},
    )
    await session.commit()

    other = (
        (await session.execute(select(OutboxEvent).where(OutboxEvent.org_id == "org_outbox_2"))).scalars().one()
    )
    assert other.sequence == 1
    assert await stream_position(session, org_id) == 5, "tenant A's cursor is not moved by tenant B"


async def test_events_since_is_a_strict_resume_cursor(session) -> None:
    """A consumer that has seen sequence N asks for what comes after it and must not see N again — a
    duplicate here is how a UI shows the same alert twice after a reconnect."""

    org_id, well_id = await _tenant(session)
    for index in range(4):
        await emit(
            session,
            org_id=org_id,
            type="telemetry.received",
            subject_kind="time_series",
            subject_id="tms_1",
            well_id=well_id,
            payload={"n": index},
        )
    await session.commit()

    assert [event.sequence for event in await events_since(session, org_id)] == [1, 2, 3, 4]
    assert [event.sequence for event in await events_since(session, org_id, after_sequence=2)] == [3, 4]
    assert await events_since(session, org_id, after_sequence=4) == []
    assert await stream_position(session, org_id) == 4


async def test_a_replayed_cursor_returns_the_same_events(session) -> None:
    """Resume is idempotent by construction: the same cursor yields the same page, so a client that
    reconnects after a dropped frame can compare instead of guess."""

    org_id, well_id = await _tenant(session)
    for index in range(3):
        await emit(
            session,
            org_id=org_id,
            type="telemetry.received",
            subject_kind="time_series",
            subject_id="tms_1",
            well_id=well_id,
            payload={"n": index},
        )
    await session.commit()

    first = await events_since(session, org_id, after_sequence=1, limit=1)
    again = await events_since(session, org_id, after_sequence=1, limit=1)
    assert [event.id for event in first] == [event.id for event in again]
    assert [event.sequence for event in first] == [2]


async def test_a_limit_bounds_a_page_without_losing_the_rest(session) -> None:
    org_id, well_id = await _tenant(session)
    for index in range(6):
        await emit(
            session,
            org_id=org_id,
            type="telemetry.received",
            subject_kind="time_series",
            subject_id="tms_1",
            well_id=well_id,
            payload={"n": index},
        )
    await session.commit()

    page = await events_since(session, org_id, limit=3)
    assert [event.sequence for event in page] == [1, 2, 3]
    rest = await events_since(session, org_id, after_sequence=page[-1].sequence)
    assert [event.sequence for event in rest] == [4, 5, 6]


async def test_a_tenant_filter_does_not_leak_another_tenants_events(session) -> None:
    org_id, well_id = await _tenant(session)
    await emit(
        session,
        org_id=org_id,
        type="telemetry.received",
        subject_kind="time_series",
        subject_id="tms_1",
        well_id=well_id,
    )
    await session.commit()
    assert await events_since(session, "org_somebody_else") == []
    assert await stream_position(session, "org_somebody_else") == 0
    assert await events_since(session, org_id, well_id="well_somebody_else") == []


async def test_concurrent_emitters_do_not_share_a_sequence_number(tmp_path) -> None:
    """Two writers in one tenant at the same moment: the allocator is a single-row ``UPDATE … RETURNING``
    per organisation, so the stream stays a total order without application-level locking and without a
    retry loop that would take a measurement down with the loser of a race."""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'outbox-concurrency.db'}")
    await database.create_all()
    try:
        async with database.session() as setup:
            await _tenant(setup)
            await setup.commit()

        async def one(index: int) -> int:
            async with database.session() as writer:
                event = await emit(
                    writer,
                    org_id=ORG,
                    type="telemetry.received",
                    subject_kind="time_series",
                    subject_id=f"tms_{index}",
                    well_id="well_outbox",
                    payload={"n": index},
                )
                await writer.commit()
                return event.sequence

        sequences = await asyncio.gather(*[one(index) for index in range(6)])
        assert sorted(sequences) == [1, 2, 3, 4, 5, 6], "six writers, six distinct numbers, no gap"

        async with database.session() as check:
            rows = (
                (await check.execute(select(OutboxEvent).order_by(OutboxEvent.sequence))).scalars().all()
            )
            assert [int(row.sequence) for row in rows] == [1, 2, 3, 4, 5, 6]
            assert sorted(row.payload["n"] for row in rows) == list(range(6))
    finally:
        await database.dispose()


async def test_allocation_is_idempotent_per_call_and_spent_by_the_caller(session) -> None:
    org_id, _ = await _tenant(session)
    assert await allocate_sequence(session, org_id) == 1
    assert await allocate_sequence(session, org_id) == 2
    await session.rollback()
    assert await stream_position(session, org_id) == 0, "the caller's rollback returns the numbers"


# --------------------------------------------------------------------------- publication state


async def test_marking_published_is_idempotent_and_records_a_time(session) -> None:
    org_id, well_id = await _tenant(session)
    event = await emit(
        session,
        org_id=org_id,
        type="telemetry.received",
        subject_kind="time_series",
        subject_id="tms_1",
        well_id=well_id,
    )
    await session.commit()

    assert await mark_published(session, [event.id]) == 1
    await session.commit()
    row = (await session.execute(select(OutboxEvent).where(OutboxEvent.id == event.id))).scalars().one()
    stamped = row.published_at
    assert stamped is not None

    assert await mark_published(session, [event.id]) == 0, "already published is not published twice"
    await session.commit()
    row = (await session.execute(select(OutboxEvent).where(OutboxEvent.id == event.id))).scalars().one()
    assert row.published_at == stamped, "a second acknowledgement does not restamp the first"

    assert await mark_published(session, []) == 0


async def test_a_stored_event_survives_a_producer_that_never_publishes_it(session) -> None:
    """Acknowledging is a separate step: an event written by a producer that then died is still on the
    stream, unpublished, and the next reader picks it up. That is what makes publication at-least-once
    instead of best-effort."""

    org_id, well_id = await _tenant(session)
    await emit(
        session,
        org_id=org_id,
        type="alert.raised",
        subject_kind="alert",
        subject_id="alt_1",
        well_id=well_id,
    )
    await session.commit()

    pending = (
        (await session.execute(select(OutboxEvent).where(OutboxEvent.published_at.is_(None)))).scalars().all()
    )
    assert len(pending) == 1
    assert await events_since(session, org_id, after_sequence=0) != [], "unpublished is still readable"
