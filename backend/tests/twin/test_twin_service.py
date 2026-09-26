"""Digital well twin: revisioning, state separation, snapshots and time travel."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from drillai.core.errors import TwinStateError, ValidationFailed
from drillai.db.models import Base, Organization, Project, Well
from drillai.twin.aspects import StateKind
from drillai.twin.service import AspectRevision, ChangeEvent, TwinService, resolve_current


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
    org = Organization(slug="twin-org", name="Twin Org")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="Twin project")
    session.add(project)
    await session.flush()
    row = Well(org_id=org.id, project_id=project.id, name="TW-1", kb_elevation_si=30.0)
    session.add(row)
    await session.flush()
    return row


def revision(**overrides) -> AspectRevision:
    payload = {"md_si": 1000.0, "incidence_deg": 0.0}
    data = {
        "aspect": "trajectory",
        "state_kind": StateKind.PLANNED,
        "payload": payload,
        "summary": "planned vertical to 1000 m",
        "computed_by": "engine",
        "data_quality": "good",
    }
    data.update(overrides)
    return AspectRevision(**data)


async def test_planned_and_actual_coexist_and_current_prefers_actual(session, well):
    service = TwinService(session, org_id=well.org_id)
    planned = await service.write_aspect(
        well_id=well.id,
        revision=revision(payload={"md_si": 1000.0}, summary="plan"),
    )
    actual = await service.write_aspect(
        well_id=well.id,
        revision=revision(state_kind=StateKind.ACTUAL, payload={"md_si": 1003.5}, summary="survey"),
    )
    assert planned.id != actual.id
    state = await service.current_state(well_id=well.id)
    assert set(state["trajectory"]) == {"planned", "actual"}

    resolved = resolve_current(state, "trajectory")
    assert resolved["value"] == {"md_si": 1003.5}
    assert resolved["source_state_kind"] == "actual"
    assert resolved["actual_matches_plan"] is False
    assert "differs" in resolved["rationale"]


async def test_identical_revision_is_not_duplicated(session, well):
    service = TwinService(session, org_id=well.org_id)
    first = await service.write_aspect(well_id=well.id, revision=revision())
    second = await service.write_aspect(well_id=well.id, revision=revision(source_refs=["doc_1"]))
    assert first.id == second.id
    assert second.source_refs == ["doc_1"]
    assert len(await service.list_aspects(well_id=well.id)) == 1


async def test_new_revision_supersedes_and_keeps_history(session, well):
    service = TwinService(session, org_id=well.org_id)
    old = await service.write_aspect(well_id=well.id, revision=revision(payload={"md_si": 1000.0}))
    new = await service.write_aspect(well_id=well.id, revision=revision(payload={"md_si": 1200.0}))
    assert new.supersedes_id == old.id
    assert old.is_current is False
    assert old.valid_to is not None

    current = await service.list_aspects(well_id=well.id, current_only=True)
    assert [row.id for row in current] == [new.id]
    every = await service.list_aspects(well_id=well.id, include_superseded=True)
    assert {row.id for row in every} == {old.id, new.id}


async def test_history_is_newest_first(session, well):
    service = TwinService(session, org_id=well.org_id)
    for md in (1000.0, 1100.0, 1200.0):
        await service.write_aspect(well_id=well.id, revision=revision(payload={"md_si": md}))
    history = await service.history(well_id=well.id, aspect_key="trajectory")
    assert [row.payload["md_si"] for row in history] == [1200.0, 1100.0, 1000.0]


async def test_unknown_aspect_and_unsupported_state_kind_are_rejected(session, well):
    service = TwinService(session, org_id=well.org_id)
    with pytest.raises(ValidationFailed):
        await service.write_aspect(well_id=well.id, revision=revision(aspect="nonsense"))
    # cements are not predicted
    with pytest.raises(TwinStateError):
        await service.write_aspect(
            well_id=well.id,
            revision=revision(aspect="cement_job", state_kind=StateKind.PREDICTED),
        )
    with pytest.raises(ValidationFailed):
        await service.write_aspect(well_id=well.id, revision=revision(payload={}))
    with pytest.raises(ValidationFailed):
        await service.write_aspect(well_id=well.id, revision=revision(confidence=1.5))


async def test_well_scoped_aspect_rejects_section_scope(session, well):
    service = TwinService(session, org_id=well.org_id)
    with pytest.raises(ValidationFailed):
        await service.write_aspect(
            well_id=well.id,
            revision=revision(aspect="identity", payload={"name": "TW-1"}),
            section_id="sec_1",
        )


async def test_snapshot_freezes_state_and_compares(session, well):
    service = TwinService(session, org_id=well.org_id)
    await service.write_aspect(well_id=well.id, revision=revision())
    await service.write_aspect(
        well_id=well.id,
        revision=revision(payload={"mud_weight_si": 1200.0}, aspect="mud_program", state_kind=StateKind.PLANNED),
    )
    before = await service.snapshot(well_id=well.id, label="as-approved", kind="baseline")
    assert before.completeness == pytest.approx(2 / 16, abs=1e-4)
    assert set(before.summary) == {"trajectory", "mud_program"}

    await service.write_aspect(
        well_id=well.id,
        revision=revision(payload={"mud_weight_si": 1300.0}, aspect="mud_program", state_kind=StateKind.PLANNED),
    )
    after = await service.snapshot(well_id=well.id, label="mud +100", kind="manual", parent_snapshot_id=before.id)

    diff = await service.compare_snapshots(before.id, after.id)
    assert diff["only_left"] == []
    assert diff["only_right"] == []
    assert [item["aspect_state"] for item in diff["changed"]] == ["mud_program/planned"]
    assert diff["unchanged"] == ["trajectory/planned"]
    assert diff["left"]["label"] == "as-approved"


async def test_snapshot_requires_data(session, well):
    service = TwinService(session, org_id=well.org_id)
    with pytest.raises(TwinStateError):
        await service.snapshot(well_id=well.id, label="empty")


async def test_change_records_capture_before_and_after(session, well):
    service = TwinService(session, org_id=well.org_id)
    record = await service.record_change(
        well.id,
        ChangeEvent(
            subject_kind="artifact_version",
            subject_id="artv_1",
            change_type="update",
            field_paths=["sections[2].mud_weight_si"],
            before={"mud_weight_si": 1200.0},
            after={"mud_weight_si": 1300.0},
            reason="well control margin",
            actor_kind="user",
            actor_id="usr_1",
            source="api",
        ),
        project_id=well.project_id,
    )
    history = await service.change_history(well_id=well.id)
    assert [row.id for row in history] == [record.id]
    assert history[0].after == {"mud_weight_si": 1300.0}
