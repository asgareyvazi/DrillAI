"""The NPT read-model: what it counts, how it counts it, and what it refuses to claim.

NPT attribution drives money and blame, so the rules are explicit: hours come from recorded events,
the Pareto is a real cumulative distribution, and controllability is reported in three buckets
(controllable / uncontrollable / unknown) that always add up to the total.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from drillai.core.errors import ValidationFailed
from drillai.db.models import Base, Event, NptCode, Operation, Organization, Project, Well, Wellbore
from drillai.drilling.classifiers import STANDARD_NPT_CODES
from drillai.drilling.npt import NptService

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest.fixture
async def well(session: AsyncSession) -> Well:
    org = Organization(slug="npt-org", name="NPT Org")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="NPT project")
    session.add(project)
    await session.flush()
    for row in STANDARD_NPT_CODES:
        session.add(
            NptCode(
                org_id=org.id,
                code=str(row["code"]),
                name=str(row["name"]),
                category=str(row["category"]),
                subcategory=row.get("subcategory"),
                is_operator_controllable=bool(row["controllable"]),
                is_system=True,
            )
        )
    well = Well(org_id=org.id, project_id=project.id, name="NPT-1")
    session.add(well)
    await session.flush()
    return well


def _event(well: Well, *, code: str | None, category: str | None, hours: float, hour: int) -> Event:
    started = dt.datetime(2026, 3, 15, hour, tzinfo=dt.UTC)
    return Event(
        org_id=well.org_id,
        project_id=well.project_id,
        well_id=well.id,
        kind="npt",
        category=category,
        npt_code=code,
        npt_category=category,
        is_npt=True,
        title=f"{category or 'unclassified'} loss",
        occurred_at=started,
        occurred_at_basis="source_record",
        ended_at=started + dt.timedelta(hours=hours),
        ended_at_basis="source_record",
        duration_hours=hours,
        npt_hours=hours,
        source="report",
    )


async def test_controllability_split_is_exhaustive_and_never_guesses(session, well):
    session.add_all(
        [
            _event(well, code="STUCK_PIPE", category="stuck_pipe", hours=6.0, hour=8),
            _event(well, code="WEATHER", category="weather", hours=3.0, hour=1),
            # A loss whose code is not in the catalogue: real, recorded, but its controllability
            # was never established. It must not be counted as either of the other two buckets.
            _event(well, code=None, category="unclassified", hours=1.5, hour=20),
        ]
    )
    await session.flush()

    summary = await NptService(session, org_id=well.org_id).summarise(well.id, include_offsets=False)
    payload = summary.to_dict()

    assert payload["total_hours"] == pytest.approx(10.5)
    assert payload["controllable_hours"] == pytest.approx(6.0)
    assert payload["uncontrollable_hours"] == pytest.approx(3.0)
    assert payload["unknown_controllability_hours"] == pytest.approx(1.5)
    assert payload["controllable_hours"] + payload["uncontrollable_hours"] + payload[
        "unknown_controllability_hours"
    ] == pytest.approx(payload["total_hours"])

    case = next(row for row in payload["cases"] if row["code"] == "STUCK_PIPE")
    assert case["category"] == "stuck_pipe"
    assert case["subcategory"] == "pipe_stuck"
    assert case["operator_controllable"] is True
    classified = next(row for row in payload["cases"] if row["code"] is None)
    assert classified["operator_controllable"] is None
    assert classified["subcategory"] is None
    # "recorded" means the case comes from an event row (as opposed to an operation roll-up); it does
    # not claim a code was assigned.
    assert classified["classification_source"] == "recorded"
    assert payload["notes"]


async def test_pareto_is_ordered_and_cumulates_to_one_hundred(session, well):
    session.add_all(
        [
            _event(well, code="STUCK_PIPE", category="stuck_pipe", hours=4.0, hour=1),
            _event(well, code="LOST_CIRC", category="lost_circulation", hours=3.0, hour=5),
            _event(well, code="WEATHER", category="weather", hours=2.0, hour=9),
            _event(well, code="TOOL_FAILURE", category="downhole_tools", hours=1.0, hour=12),
        ]
    )
    await session.flush()

    payload = (
        await NptService(session, org_id=well.org_id).summarise(well.id, include_offsets=False)
    ).to_dict()
    rows = payload["by_category"]
    # The Pareto carries every standard category so a chart axis is stable, but only recorded losses
    # have hours; zero-hour rows must never be read as "this happened and cost nothing".
    assert [row["hours"] for row in rows if row["hours"]] == [4.0, 3.0, 2.0, 1.0]
    assert all(row["hours"] == 0.0 for row in rows if not row["occurrences"])
    recorded = [row for row in rows if row["occurrences"]]
    assert recorded[0]["percent_of_total"] == pytest.approx(40.0)
    assert recorded[0]["cumulative_percent"] == pytest.approx(40.0)
    assert recorded[-1]["cumulative_percent"] == pytest.approx(100.0)
    assert sum(row["percent_of_total"] for row in rows) == pytest.approx(100.0)
    assert all(row["operator_controllable"] is None for row in rows if not row["occurrences"])
    assert {row["key"] for row in payload["by_code"]} == {
        "STUCK_PIPE",
        "LOST_CIRC",
        "WEATHER",
        "TOOL_FAILURE",
    }


async def test_a_well_without_recorded_loss_reports_zero_and_says_why(session, well):
    payload = (
        await NptService(session, org_id=well.org_id).summarise(well.id, include_offsets=False)
    ).to_dict()
    assert payload["total_hours"] == 0.0
    assert payload["event_count"] == 0
    assert payload["cases"] == []
    assert payload["percent_of_well_time"] is None, "no measured time means no percentage"
    assert any("never an assumed zero" in note for note in payload["notes"])


async def test_scoped_npt_percentage_uses_the_same_time_window_for_its_denominator(session, well):
    in_scope = dt.datetime(2026, 3, 15, 8, tzinfo=dt.UTC)
    out_of_scope = dt.datetime(2026, 3, 14, 8, tzinfo=dt.UTC)
    wellbore = Wellbore(org_id=well.org_id, well_id=well.id, name="Main bore", sequence=1)
    session.add(wellbore)
    await session.flush()
    outside_event = _event(well, code="WEATHER", category="weather", hours=8.0, hour=1)
    outside_event.occurred_at = out_of_scope
    outside_event.ended_at = out_of_scope + dt.timedelta(hours=8)
    session.add_all(
        [
            _event(well, code="STUCK_PIPE", category="stuck_pipe", hours=2.0, hour=8),
            outside_event,
            Operation(
                org_id=well.org_id,
                project_id=well.project_id,
                well_id=well.id,
                wellbore_id=wellbore.id,
                operation_class="actual",
                sequence=1,
                name="In-window productive drilling",
                kind="drilling",
                actual_start=in_scope,
                actual_start_basis="source_record",
                actual_end=in_scope + dt.timedelta(hours=6),
                actual_end_basis="source_record",
                actual_duration_hours=6.0,
                actual_duration_basis="reported",
                is_productive=True,
            ),
            Operation(
                org_id=well.org_id,
                project_id=well.project_id,
                well_id=well.id,
                wellbore_id=wellbore.id,
                operation_class="actual",
                sequence=2,
                name="Out-of-window productive drilling",
                kind="drilling",
                actual_start=out_of_scope,
                actual_start_basis="source_record",
                actual_end=out_of_scope + dt.timedelta(hours=4),
                actual_end_basis="source_record",
                actual_duration_hours=20.0,
                actual_duration_basis="reported",
                is_productive=True,
            ),
        ]
    )
    await session.flush()

    payload = (
        await NptService(session, org_id=well.org_id).summarise(
            well.id,
            basis="events",
            since=dt.datetime(2026, 3, 15, 0, tzinfo=dt.UTC),
            until=dt.datetime(2026, 3, 16, 0, tzinfo=dt.UTC),
            include_offsets=False,
        )
    ).to_dict()
    assert payload["total_hours"] == pytest.approx(2.0)
    assert payload["productive_hours"] == pytest.approx(6.0)
    assert payload["denominator_hours"] == pytest.approx(8.0)
    assert payload["percent_of_well_time"] == pytest.approx(25.0)
    assert payload["scope"]["since"] == "2026-03-15T00:00:00+00:00"
    assert payload["scope"]["until"] == "2026-03-16T00:00:00+00:00"
    assert payload["metric_contract"]["selected_basis"] == "events"
    assert payload["metric_contract"]["denominator"].startswith("matching actual productive")
    assert payload["metric_contract"]["window_hours_policy"].startswith("recorded interval rows are included on overlap")
    assert payload["generated_at"]


async def test_npt_metric_window_rejects_ambiguous_or_empty_time_bounds(session, well):
    service = NptService(session, org_id=well.org_id)
    with pytest.raises(ValidationFailed, match="must include a timezone offset"):
        await service.summarise(well.id, since=dt.datetime(2026, 3, 15))
    with pytest.raises(ValidationFailed, match="earlier than until"):
        await service.summarise(
            well.id,
            since=dt.datetime(2026, 3, 16, tzinfo=dt.UTC),
            until=dt.datetime(2026, 3, 16, tzinfo=dt.UTC),
        )
