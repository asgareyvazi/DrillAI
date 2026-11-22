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
