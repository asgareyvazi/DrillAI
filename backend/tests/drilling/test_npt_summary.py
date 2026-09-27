"""The NPT read-model: what it counts, how it counts it, and what it refuses to claim.

NPT attribution drives money and blame, so the rules are explicit: hours come from recorded events,
the Pareto is a real cumulative distribution, and controllability is reported in three buckets
(controllable / uncontrollable / unknown) that always add up to the total.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from drillai.db.models import Base, Event, NptCode, Organization, Project, Well
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
        ended_at=started + dt.timedelta(hours=hours),
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
