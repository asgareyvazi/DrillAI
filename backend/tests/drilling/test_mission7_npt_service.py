"""Integration against persisted and tenant-scoped event rows."""
import datetime as dt

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from drillai.db.models import Base, Event, Organization, Project, Well
from drillai.drilling.npt import NptService

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(bind=engine, expire_on_commit=False)() as sess:
        yield sess
    await engine.dispose()


async def test_interval_reconciliation_respects_tenant_scope_and_does_not_change_recorded_totals(session):
    organizations = [Organization(slug="m7-first", name="First"), Organization(slug="m7-second", name="Second")]
    session.add_all(organizations)
    await session.flush()
    project = Project(org_id=organizations[0].id, name="Project")
    session.add(project)
    await session.flush()
    well = Well(org_id=organizations[0].id, project_id=project.id, name="Well")
    session.add(well)
    await session.flush()
    start = dt.datetime(2026, 3, 15, tzinfo=dt.UTC)
    for org, offset in ((organizations[0], 0), (organizations[0], 1), (organizations[1], 0)):
        session.add(Event(org_id=org.id, well_id=well.id, kind="npt", is_npt=True, title="Recorded",
                          occurred_at=start + dt.timedelta(hours=offset), occurred_at_basis="source_record",
                          ended_at=start + dt.timedelta(hours=offset + 2), ended_at_basis="source_record",
                          npt_hours=2, npt_hours_basis="reported", source="manual"))
    await session.flush()
    summary = (await NptService(session, org_id=organizations[0].id).summarise(well.id, include_offsets=False)).to_dict()
    assert summary["total_hours"] == 4
    assert summary["interval_reconciliation"]["union_hours"] == 3
    assert summary["interval_reconciliation"]["overlap_hours"] == 1
    assert summary["interval_reconciliation"]["event_count"] == 2


async def test_generic_event_duration_is_not_explicit_npt_interval_evidence(session):
    org = Organization(slug="m8-duration", name="Duration provenance")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="Project")
    session.add(project)
    await session.flush()
    well = Well(org_id=org.id, project_id=project.id, name="Well")
    session.add(well)
    await session.flush()
    start = dt.datetime(2026, 3, 15, tzinfo=dt.UTC)
    event = Event(org_id=org.id, well_id=well.id, kind="npt", is_npt=True, title="Mixed activity",
                  occurred_at=start, occurred_at_basis="source_record",
                  ended_at=start + dt.timedelta(hours=2), ended_at_basis="source_record",
                  duration_hours=2, duration_basis="reported", npt_hours=None, npt_hours_basis="unknown",
                  source="manual")
    session.add(event)
    await session.commit()
    service = NptService(session, org_id=org.id)
    before = (await service.summarise(well.id, include_offsets=False)).to_dict()
    assert before["total_hours"] == 2  # Legacy row sum remains distinct, not a deduplicated value.
    assert before["interval_reconciliation"]["status"] == "unavailable_incomplete"
    assert before["interval_reconciliation"]["union_hours"] is None
    assert before["interval_reconciliation"]["missing_npt_hours_count"] == 1
    assert before["cases"][0]["hours_source"] == "generic_event_duration"
    assert "generic duration" in before["metric_contract"]["numerator"]
    assert any("non-NPT time" in note for note in before["notes"])

    # An explicit reported quantity consistent with both timestamps makes the interval eligible;
    # this is synthetic software evidence, not proof that actual rig downtime was complete.
    event.npt_hours = 2
    event.npt_hours_basis = "reported"
    await session.commit()
    after = (await service.summarise(well.id, include_offsets=False)).to_dict()
    assert after["interval_reconciliation"]["status"] == "complete_for_matching_events"
    assert after["interval_reconciliation"]["union_hours"] == 2
    assert after["interval_reconciliation"]["missing_npt_hours_count"] == 0
    assert after["cases"][0]["hours_source"] == "explicit_npt_hours"

    session.add(Event(org_id=org.id, well_id=well.id, kind="npt", is_npt=True, title="Unknown duration",
                      npt_hours=None, npt_hours_basis="unknown", duration_hours=None,
                      duration_basis="unknown", source="manual"))
    await session.commit()
    incomplete = (await service.summarise(well.id, include_offsets=False)).to_dict()
    missing = next(case for case in incomplete["cases"] if case["title"] == "Unknown duration")
    assert missing["hours_source"] == "missing"
    assert missing["hours"] is None
    assert incomplete["interval_reconciliation"]["union_hours"] is None
