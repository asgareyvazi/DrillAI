"""The asset service against a real database: every mutation, and what it leaves behind.

These tests drive :class:`~drillai.assets.AssetService` directly, with a real session and the real
schema. That is deliberate: the properties under test are ones a mock cannot have — that a mutation
writes the ledger *and* the change record inside the same transaction, that another tenant's id is a
404 rather than a successful read, that a second active hole is refused by the database itself, and
that a rename leaves every id that pointed at the well pointing at the same well.

The API-level tests (``tests/api/test_assets_api.py``) cover the wire contract; this file covers the
rules, including the ones the API can only surface as a 422.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.assets import AssetService
from drillai.assets.vocabulary import DEFAULT_WELL_TYPE, DEFAULT_WELLBORE_PURPOSE
from drillai.core.errors import Conflict, NotFound, ValidationFailed
from drillai.core.idempotency import complete, replay_or_reserve
from drillai.db.models import (
    AuditLog,
    ChangeRecord,
    Document,
    Operation,
    Organization,
    Project,
    Rig,
    Well,
)
from drillai.security.rbac import SYSTEM_ROLES, principal_from_roles

ORG_A = "org_alpha"
ORG_B = "org_bravo"


@pytest_asyncio.fixture
async def two_orgs(session: AsyncSession) -> None:
    """Two organizations, so "scoped to the caller's org" is a property and not a claim."""
    session.add_all(
        [
            Organization(id=ORG_A, slug="alpha", name="Alpha"),
            Organization(id=ORG_B, slug="bravo", name="Bravo"),
        ]
    )
    await session.flush()


def service(session: AsyncSession, org_id: str = ORG_A, role: str = "engineer") -> AssetService:
    return AssetService(
        session,
        org_id=org_id,
        principal=principal_from_roles(
            principal_id=f"usr_{role}", org_id=org_id, roles=[SYSTEM_ROLES[role]], display_name=f"Test {role}"
        ),
        request_id="req_test_1",
    )


async def ledger(
    session: AsyncSession, *, action: str | None = None, well_id: str | None = None
) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.occurred_at, AuditLog.id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if well_id:
        stmt = stmt.where(AuditLog.well_id == well_id)
    return list((await session.execute(stmt)).scalars().all())


async def changes(session: AsyncSession, well_id: str) -> list[ChangeRecord]:
    stmt = (
        select(ChangeRecord)
        .where(ChangeRecord.well_id == well_id)
        .order_by(ChangeRecord.occurred_at, ChangeRecord.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _project(session: AsyncSession, org: str = ORG_A, name: str = "P") -> Project:
    return await service(session, org).create_project(name=name)


async def _well(session: AsyncSession, org: str = ORG_A, name: str = "W-1", **kwargs) -> Well:
    project = await _project(session, org, name=f"project for {name}")
    return await service(session, org).create_well(project_id=project.id, name=name, **kwargs)


# --------------------------------------------------------------------------- creation and identity


async def test_creating_a_well_records_both_ledgers(session, two_orgs):
    """A create is not a ``session.add``: it is a governed act with an audit row and a change record."""
    well = await _well(session, name="NF-12")
    assert well.well_type == DEFAULT_WELL_TYPE, "the canonical default, not the drifted 'development'"

    audit = await ledger(session, action="well.create", well_id=well.id)
    assert len(audit) == 1
    entry = audit[0]
    assert entry.resource_kind == "well"
    assert entry.resource_id == well.id
    assert entry.actor_id == "usr_engineer"
    assert entry.actor_kind == "user"
    assert entry.action_level == "L2", "well.create is a draft-level act"
    assert entry.permission_decision["required"] == "well.create"
    assert entry.request_id == "req_test_1"
    assert entry.after["name"] == "NF-12"

    record = await changes(session, well.id)
    assert [row.change_type for row in record] == ["created"]
    assert record[0].subject_kind == "well"
    assert record[0].source == "api"


async def test_a_project_created_without_a_principal_is_recorded_as_a_system_act(session, two_orgs):
    """The ledger never claims a user for a change no user made."""
    project = await AssetService(session, org_id=ORG_A).create_project(name="Unattended")
    entry = (await ledger(session, action="project.create"))[0]
    assert entry.project_id == project.id
    assert entry.actor_kind == "system"
    assert entry.actor_id is None


async def test_a_duplicate_well_name_in_the_same_project_is_a_conflict_not_a_database_error(
    session, two_orgs
):
    project = await _project(session, name="One")
    first = await service(session).create_well(project_id=project.id, name="NF-12")
    with pytest.raises(Conflict) as clash:
        await service(session).create_well(project_id=project.id, name="NF-12")
    assert clash.value.details["existing_id"] == first.id

    # A different project is a different namespace: two projects may each have an NF-12.
    other = await _project(session, name="Two")
    second = await service(session).create_well(project_id=other.id, name="NF-12")
    assert second.id != first.id


async def test_identifiers_are_unique_within_the_organization_and_not_across_it(session, two_orgs):
    project = await _project(session, name="UWIs")
    await service(session).create_well(project_id=project.id, name="A", uwi="NO-15-9-1")
    with pytest.raises(Conflict) as duplicate:
        await service(session).create_well(project_id=project.id, name="B", uwi="no-15-9-1")
    assert duplicate.value.details["field"] == "uwi", "a UWI is one identifier whatever its case"

    # The same regulator identifier in a second organization is a different well.
    b_project = await _project(session, ORG_B, name="UWIs")
    second_org = await service(session, ORG_B).create_well(project_id=b_project.id, name="A", uwi="NO-15-9-1")
    assert second_org.uwi == "NO-15-9-1"


async def test_many_wells_may_have_no_identifier_at_all(session, two_orgs):
    """The uniqueness scopes are partial: an optional identity must not make the second well illegal."""
    project = await _project(session, name="Unidentified")
    for index in range(3):
        well = await service(session).create_well(project_id=project.id, name=f"U-{index}")
        assert well.uwi is None and well.api_number is None


async def test_wells_are_scoped_to_the_callers_organization(session, two_orgs):
    well = await _well(session, ORG_A, name="Alpha-1")
    with pytest.raises(NotFound):
        await service(session, ORG_B).get_well(well.id)
    with pytest.raises(NotFound):
        await service(session, ORG_B).update_well(well.id, {"name": "stolen"})


# --------------------------------------------------------------------------- editing


async def test_a_rename_keeps_the_id_and_records_before_and_after(session, two_orgs):
    well = await _well(session, name="NF-12")
    before_rows = await changes(session, well.id)
    renamed = await service(session).update_well(
        well.id, {"name": "NF-12 ST1", "uwi": "no-15-9-2"}, reason="typo"
    )

    assert renamed.id == well.id
    audit = await ledger(session, action="well.update")
    assert len(audit) == 1
    assert audit[0].before == {"name": "NF-12", "uwi": None}
    # Identifiers are trimmed and upper-cased on write, so a UWI typed in lower case with a stray space
    # is the same identifier rather than a second well.
    assert audit[0].after == {"name": "NF-12 ST1", "uwi": "NO-15-9-2"}
    assert audit[0].details["changed"] == ["name", "uwi"]

    records = await changes(session, well.id)
    assert len(records) == len(before_rows) + 1
    assert records[-1].change_type == "updated"
    assert records[-1].reason == "typo"


async def test_an_edit_that_changes_nothing_is_not_recorded_as_a_change(session, two_orgs):
    well = await _well(session, name="NF-12")
    await service(session).update_well(well.id, {"name": "NF-12"})
    assert await ledger(session, action="well.update") == []
    assert [row.change_type for row in await changes(session, well.id)] == ["created"]


async def test_a_field_that_identifies_the_record_is_refused_by_name(session, two_orgs):
    well = await _well(session, name="NF-12")
    for payload, expected in (
        ({"project_id": "prj_other"}, "identify the well"),
        ({"status": "producing"}, "the well lifecycle"),
        ({"twin_state": "drilling"}, "the twin"),
        ({"rig_id": "rig_1"}, "rig assignment"),
        ({"nonsense": 1}, "unknown well fields"),
    ):
        with pytest.raises(ValidationFailed) as refused:
            await service(session).update_well(well.id, payload)
        assert expected in refused.value.message, payload


async def test_a_well_can_move_between_fields_of_its_own_project_only(session, two_orgs):
    project = await _project(session, name="North")
    field = await service(session).create_field(project_id=project.id, name="Ekofisk")
    well = await service(session).create_well(project_id=project.id, name="NF-1", field_id=field.id)
    assert well.field_id == field.id

    elsewhere = await _project(session, name="South")
    other_field = await service(session).create_field(project_id=elsewhere.id, name="Valhall")
    with pytest.raises(ValidationFailed) as wrong_project:
        await service(session).update_well(well.id, {"field_id": other_field.id})
    assert wrong_project.value.details["well_project_id"] == project.id

    # A field in the same project is a legitimate move.
    second = await service(session).create_field(project_id=project.id, name="Eldfisk")
    moved = await service(session).update_well(well.id, {"field_id": second.id})
    assert moved.field_id == second.id
    assert moved.id == well.id


async def test_a_stale_write_is_refused_with_both_timestamps(session, two_orgs):
    well = await _well(session, name="NF-12")
    read_at = well.updated_at
    await service(session).update_well(well.id, {"operator": "Someone else"})

    with pytest.raises(Conflict) as stale:
        await service(session).update_well(well.id, {"name": "Late"}, expected_updated_at=read_at)
    assert stale.value.details["id"] == well.id
    assert stale.value.details["expected_updated_at"] != stale.value.details["current_updated_at"]

    current = (await service(session).get_well(well.id)).updated_at
    fresh = await service(session).update_well(well.id, {"name": "Informed"}, expected_updated_at=current)
    assert fresh.name == "Informed"


# --------------------------------------------------------------------------- lifecycle


async def test_the_lifecycle_matrix_is_enforced_through_the_service(session, two_orgs):
    well = await _well(session, name="NF-12")
    await service(session).transition_well(well.id, "drilling")
    await service(session).transition_well(well.id, "completing")
    producing = await service(session).transition_well(well.id, "producing")
    assert producing.status == "producing"

    with pytest.raises(ValidationFailed) as backwards:
        await service(session).transition_well(well.id, "planned")
    assert backwards.value.details["allowed"] == sorted({"shut_in", "intervention", "suspended", "abandoned"})

    with pytest.raises(ValidationFailed):
        await service(session).transition_well(well.id, "drilling")  # producing -> drilling is not an edge

    await service(session).transition_well(well.id, "abandoned")
    await service(session).transition_well(well.id, "p&a")
    with pytest.raises(ValidationFailed) as terminal:
        await service(session).transition_well(well.id, "drilling")
    assert terminal.value.details["allowed"] == []


async def test_a_transition_records_who_moved_it_and_where_from(session, two_orgs):
    well = await _well(session, name="NF-12")
    await service(session).transition_well(well.id, "drilling", reason="spudded 06:00")
    entry = (await ledger(session, action="well.lifecycle"))[0]
    assert entry.before == {"status": "planned"}
    assert entry.after == {"status": "drilling"}
    assert entry.details["reason"] == "spudded 06:00"
    assert entry.actor_id == "usr_engineer"
    record = (await changes(session, well.id))[-1]
    assert record.change_type == "lifecycle"
    assert record.field_paths == ["status"]


async def test_the_status_cannot_be_moved_by_an_ordinary_edit(session, two_orgs):
    well = await _well(session, name="NF-12")
    with pytest.raises(ValidationFailed) as refused:
        await service(session).update_well(well.id, {"status": "producing"})
    assert refused.value.details["managed"]["status"].startswith("the well lifecycle")


async def test_transitioning_to_the_state_a_well_is_already_in_is_refused(session, two_orgs):
    well = await _well(session, name="NF-12")
    with pytest.raises(ValidationFailed) as same:
        await service(session).transition_well(well.id, "planned")
    assert "already" in same.value.message


# --------------------------------------------------------------------------- rigs


async def test_assigning_a_rig_keeps_both_records_in_agreement(session, two_orgs):
    rig = Rig(id="rig_1", org_id=ORG_A, name="Maersk Invincible")
    session.add(rig)
    await session.flush()
    well = await _well(session, name="NF-12")

    assigned = await service(session).assign_rig(well.id, rig.id)
    await session.refresh(rig)
    assert assigned.rig_id == rig.id
    assert rig.current_well_id == well.id
    assert len(await ledger(session, action="well.assign_rig")) == 1

    detached = await service(session).assign_rig(well.id, None)
    await session.refresh(rig)
    assert detached.rig_id is None
    assert rig.current_well_id is None, "the rig must not still claim a well it left"


async def test_a_rig_cannot_work_two_wells_at_once(session, two_orgs):
    rig = Rig(id="rig_1", org_id=ORG_A, name="Maersk Invincible")
    session.add(rig)
    await session.flush()
    first = await _well(session, name="NF-12")
    second = await _well(session, name="NF-13")

    await service(session).assign_rig(first.id, rig.id)
    with pytest.raises(Conflict) as busy:
        await service(session).assign_rig(second.id, rig.id)
    assert busy.value.details["well_ids"] == [first.id]


async def test_a_rig_from_another_organization_cannot_be_attached(session, two_orgs):
    session.add(Rig(id="rig_b", org_id=ORG_B, name="Bravo rig"))
    await session.flush()
    well = await _well(session, ORG_A, name="NF-12")
    with pytest.raises(NotFound):
        await service(session).assign_rig(well.id, "rig_b")


# --------------------------------------------------------------------------- fields


async def test_field_master_data_is_scoped_to_its_project_and_org(session, two_orgs):
    project = await _project(session, name="North")
    field = await service(session).create_field(
        project_id=project.id, name="Ekofisk", aliases=["Ekofisk Vest"], country="NO"
    )
    assert field.aliases == ["Ekofisk Vest"]

    with pytest.raises(Conflict) as duplicate:
        await service(session).create_field(project_id=project.id, name="ekofisk")
    assert duplicate.value.details["existing_id"] == field.id

    with pytest.raises(NotFound):
        await service(session, ORG_B).get_field(field.id)

    with pytest.raises(ValidationFailed) as immutable:
        await service(session).update_field(field.id, {"project_id": "prj_other"})
    assert immutable.value.details["immutable"] == ["project_id"]

    updated = await service(session).update_field(field.id, {"aliases": ["Ekofisk", "Ekofisk Vest"]})
    assert updated.aliases == ["Ekofisk", "Ekofisk Vest"]
    assert (await ledger(session, action="field.update"))[0].details["changed"] == ["aliases"]


async def test_fields_are_searchable_by_name_and_alias(session, two_orgs):
    project = await _project(session, name="Search")
    await service(session).create_field(project_id=project.id, name="Ekofisk", aliases=["Ekofisk Vest"])
    await service(session).create_field(project_id=project.id, name="Valhall")

    both, total = await service(session).list_fields(query="ekof")
    assert total == 1 and both[0].name == "Ekofisk"
    alias_only, alias_total = await service(session).list_fields(query="vest")
    assert alias_total == 1 and alias_only[0].name == "Ekofisk"
    _, all_total = await service(session).list_fields()
    assert all_total == 2


# --------------------------------------------------------------------------- wellbores and lineage


async def test_the_first_wellbore_is_the_active_one_and_later_ones_wait(session, two_orgs):
    well = await _well(session, name="NF-12")
    first = await service(session).create_wellbore(well.id, name="Main bore")
    second = await service(session).create_wellbore(
        well.id, name="Sidetrack", purpose="sidetrack", parent_wellbore_id=first.id
    )

    assert first.is_active is True
    assert second.is_active is False, "a new hole does not silently become the one being drilled"
    assert first.sequence == 1 and second.sequence == 2, "positions are chosen, not duplicated"
    assert second.purpose == "sidetrack"
    assert second.parent_wellbore_id == first.id


async def test_lineage_is_structured_and_cycles_are_impossible(session, two_orgs):
    well = await _well(session, name="NF-12")
    root = await service(session).create_wellbore(well.id, name="Main bore")
    branch = await service(session).create_wellbore(
        well.id, name="ST-1", purpose="sidetrack", parent_wellbore_id=root.id
    )
    deeper = await service(session).create_wellbore(
        well.id, name="ST-2", purpose="sidetrack", parent_wellbore_id=branch.id
    )

    chain = await service(session).wellbore_lineage(deeper.id)
    assert [row.id for row in chain] == [root.id, branch.id, deeper.id], "oldest first"

    with pytest.raises(ValidationFailed) as loop:
        await service(session).update_wellbore(branch.id, {"parent_wellbore_id": deeper.id})
    assert "circular" in loop.value.message

    with pytest.raises(ValidationFailed) as itself:
        await service(session).update_wellbore(root.id, {"parent_wellbore_id": root.id})
    assert "own parent" in itself.value.message


async def test_a_derived_hole_must_name_its_parent_and_an_original_hole_must_not(session, two_orgs):
    well = await _well(session, name="NF-12")
    root = await service(session).create_wellbore(well.id, name="Main bore")

    with pytest.raises(ValidationFailed) as orphan:
        await service(session).create_wellbore(well.id, name="ST-1", purpose="sidetrack")
    assert orphan.value.details["field"] == "parent_wellbore_id"

    with pytest.raises(ValidationFailed) as contradictory:
        await service(session).create_wellbore(
            well.id, name="Main bore 2", purpose="original", parent_wellbore_id=root.id
        )
    assert contradictory.value.details["purpose"] == "original"

    assert (
        await service(session).create_wellbore(well.id, name="Main bore")
    ).purpose == DEFAULT_WELLBORE_PURPOSE


async def test_a_parent_in_another_well_or_another_org_is_refused(session, two_orgs):
    left = await _well(session, name="NF-12")
    right = await _well(session, name="NF-13")
    root = await service(session).create_wellbore(left.id, name="Main bore")

    with pytest.raises(ValidationFailed) as other_well:
        await service(session).create_wellbore(
            right.id, name="ST-1", purpose="sidetrack", parent_wellbore_id=root.id
        )
    assert other_well.value.details["parent_well_id"] == left.id

    with pytest.raises(NotFound):
        await service(session, ORG_B).create_wellbore(
            left.id, name="ST-1", purpose="sidetrack", parent_wellbore_id=root.id
        )


async def test_a_duplicate_position_is_refused_and_a_missing_one_is_chosen(session, two_orgs):
    well = await _well(session, name="NF-12")
    await service(session).create_wellbore(well.id, name="Main bore", sequence=1)
    with pytest.raises(ValidationFailed) as taken:
        await service(session).create_wellbore(well.id, name="Clash", sequence=1)
    assert taken.value.details["used"] == [1]

    chosen = await service(session).create_wellbore(well.id, name="Next")
    assert chosen.sequence == 2

    with pytest.raises(ValidationFailed):
        await service(session).create_wellbore(well.id, name="Zero", sequence=0)


async def test_activation_is_exclusive_and_abandoned_holes_cannot_be_activated(session, two_orgs):
    well = await _well(session, name="NF-12")
    root = await service(session).create_wellbore(well.id, name="Main bore")
    branch = await service(session).create_wellbore(
        well.id, name="ST-1", purpose="sidetrack", parent_wellbore_id=root.id
    )

    await service(session).activate_wellbore(branch.id, reason="sidetrack is drilling")
    await session.refresh(root)
    assert root.is_active is False and branch.is_active is True
    assert len(await ledger(session, action="wellbore.activate")) == 1

    await service(session).transition_wellbore(root.id, "abandoned")
    await session.refresh(root)
    assert root.is_active is False
    with pytest.raises(ValidationFailed) as gone:
        await service(session).activate_wellbore(root.id)
    assert gone.value.details["status"] == "abandoned"


async def test_the_whole_lineage_is_audited_when_it_changes(session, two_orgs):
    well = await _well(session, name="NF-12")
    root = await service(session).create_wellbore(well.id, name="Main bore")
    await service(session).create_wellbore(
        well.id, name="ST-1", purpose="sidetrack", parent_wellbore_id=root.id
    )
    assert len(await ledger(session, action="wellbore.create")) == 2
    assert [row.change_type for row in await changes(session, well.id)] == ["created", "created", "created"]


# --------------------------------------------------------------------------- sections


async def test_a_section_records_what_was_drilled_and_derives_the_plan_only_flag(session, two_orgs):
    well = await _well(session, name="NF-12")
    wellbore = await service(session).create_wellbore(well.id, name="Main bore")

    planned = await service(session).create_section(
        wellbore.id, sequence=1, name='17-1/2" section', planned_top_md_si=100.0, planned_bottom_md_si=800.0
    )
    assert planned.is_planned_only is True

    drilled = await service(session).create_section(
        wellbore.id,
        sequence=2,
        name='12-1/4" section',
        planned_top_md_si=800.0,
        planned_bottom_md_si=2500.0,
        actual_top_md_si=800.0,
        actual_bottom_md_si=2480.0,
    )
    assert drilled.is_planned_only is False


async def test_the_plan_only_flag_cannot_be_asserted_by_a_caller(session, two_orgs):
    well = await _well(session, name="NF-12")
    wellbore = await service(session).create_wellbore(well.id, name="Main bore")
    section = await service(session).create_section(
        wellbore.id, sequence=1, name="12-1/4", planned_bottom_md_si=100.0
    )

    with pytest.raises(ValidationFailed) as refused:
        await service(session).update_section(section.id, {"is_planned_only": False})
    assert refused.value.details["managed"]["is_planned_only"].startswith("the service")

    # Recording the depth is what changes the flag — and it changes it by itself.
    updated = await service(session).update_section(
        section.id, {"actual_top_md_si": 0.0, "actual_bottom_md_si": 100.0}
    )
    assert updated.is_planned_only is False


async def test_a_recorded_as_drilled_measurement_cannot_be_erased_by_an_edit(session, two_orgs):
    well = await _well(session, name="NF-12")
    wellbore = await service(session).create_wellbore(well.id, name="Main bore")
    section = await service(session).create_section(
        wellbore.id,
        sequence=1,
        name="12-1/4",
        planned_bottom_md_si=100.0,
        actual_bottom_md_si=98.0,
        actual_top_md_si=0.0,
    )
    with pytest.raises(ValidationFailed) as erased:
        await service(session).update_section(section.id, {"actual_bottom_md_si": None})
    assert erased.value.details["cleared"] == ["actual_bottom_md_si"]


async def test_section_geometry_is_validated_and_never_rewrites_the_other_class(session, two_orgs):
    well = await _well(session, name="NF-12")
    wellbore = await service(session).create_wellbore(well.id, name="Main bore")
    section = await service(session).create_section(
        wellbore.id,
        sequence=1,
        name="12-1/4",
        planned_top_md_si=800.0,
        planned_bottom_md_si=2500.0,
        actual_top_md_si=800.0,
        actual_bottom_md_si=2480.0,
    )

    with pytest.raises(ValidationFailed):
        await service(session).update_section(
            section.id, {"planned_bottom_md_si": 700.0}
        )  # above its own top
    with pytest.raises(ValidationFailed):
        await service(session).update_section(
            section.id, {"actual_bottom_md_si": 790.0}
        )  # above the actual top
    with pytest.raises(ValidationFailed):
        await service(session).update_section(section.id, {"current_md_si": 100.0})  # above the section top

    revised = await service(session).update_section(section.id, {"planned_bottom_md_si": 2600.0})
    assert revised.planned_bottom_md_si == 2600.0
    assert revised.actual_bottom_md_si == 2480.0, "a plan revision does not move the as-drilled record"
    assert revised.actual_top_md_si == 800.0

    entry = (await ledger(session, action="section.update"))[0]
    assert entry.details["plan_revised_with_as_drilled_geometry"] is True


async def test_a_section_without_as_drilled_geometry_has_no_current_depth(session, two_orgs):
    """§19: the plan is never presented as where the bit is."""
    well = await _well(session, name="NF-12")
    wellbore = await service(session).create_wellbore(well.id, name="Main bore")
    section = await service(session).create_section(
        wellbore.id, sequence=1, name='12-1/4" section', planned_bottom_md_si=2500.0
    )
    assert section.current_md_si is None
    assert section.actual_bottom_md_si is None
    assert section.planned_bottom_md_si == 2500.0
    assert section.is_planned_only is True


async def test_a_section_cannot_be_called_drilled_without_a_recorded_bottom(session, two_orgs):
    well = await _well(session, name="NF-12")
    wellbore = await service(session).create_wellbore(well.id, name="Main bore")
    section = await service(session).create_section(
        wellbore.id, sequence=1, name="12-1/4", planned_bottom_md_si=2500.0
    )

    await service(session).transition_section(section.id, "drilling")
    with pytest.raises(ValidationFailed) as unsupported:
        await service(session).transition_section(section.id, "drilled")
    assert unsupported.value.details["field"] == "actual_bottom_md_si"

    await service(session).update_section(
        section.id, {"actual_top_md_si": 800.0, "actual_bottom_md_si": 2495.0}
    )
    drilled = await service(session).transition_section(section.id, "drilled")
    assert drilled.status == "drilled"
    assert drilled.is_planned_only is False


async def test_section_sequence_is_unique_within_its_wellbore(session, two_orgs):
    well = await _well(session, name="NF-12")
    wellbore = await service(session).create_wellbore(well.id, name="Main bore")
    await service(session).create_section(wellbore.id, sequence=1, name="A")
    with pytest.raises(ValidationFailed) as taken:
        await service(session).create_section(wellbore.id, sequence=1, name="B")
    assert taken.value.details["used"] == [1]


async def test_a_section_of_another_wellbore_or_org_is_not_reachable(session, two_orgs):
    well = await _well(session, name="NF-12")
    wellbore = await service(session).create_wellbore(well.id, name="Main bore")
    section = await service(session).create_section(wellbore.id, sequence=1, name="A")
    with pytest.raises(NotFound):
        await service(session, ORG_B).get_section(section.id)


# --------------------------------------------------------------------------- context integrity


async def test_context_counts_attribute_records_to_the_node_they_hang_off(session, two_orgs):
    well = await _well(session, name="NF-12")
    left = await service(session).create_wellbore(well.id, name="Main bore")
    right = await service(session).create_wellbore(
        well.id, name="ST-1", purpose="sidetrack", parent_wellbore_id=left.id
    )

    session.add_all(
        [
            Document(
                id="doc_1", org_id=ORG_A, well_id=well.id, wellbore_id=left.id, doc_type="ddr", title="one"
            ),
            Document(
                id="doc_2", org_id=ORG_A, well_id=well.id, wellbore_id=right.id, doc_type="ddr", title="two"
            ),
            Document(id="doc_3", org_id=ORG_A, well_id=well.id, doc_type="report", title="well level"),
            Operation(id="opr_1", org_id=ORG_A, well_id=well.id, wellbore_id=left.id, name="Drilling"),
        ]
    )
    await session.flush()

    counts = await service(session).well_context_counts(well.id)
    assert counts["documents"]["total"] == 3, "two on wellbores plus one on the well itself"
    assert counts["documents"]["well"] == 1
    assert counts["documents"]["by_wellbore"] == {left.id: 1, right.id: 1}
    assert counts["operations"]["by_wellbore"] == {left.id: 1}
    assert counts["operations"]["total"] == 1
    assert counts["twin_aspects"] == {"total": 0, "well": 0, "by_wellbore": {}}
    assert counts["evidence"] == {"total": 0, "well": 0, "by_wellbore": {}}


async def test_renaming_a_well_leaves_every_dependent_record_attached(session, two_orgs):
    """§22/§40: the identity that other records carry is the id, and a rename cannot break it."""
    well = await _well(session, name="NF-12")
    wellbore = await service(session).create_wellbore(well.id, name="Main bore")
    section = await service(session).create_section(wellbore.id, sequence=1, name='12-1/4" section')
    session.add(
        Document(
            id="doc_1",
            org_id=ORG_A,
            well_id=well.id,
            wellbore_id=wellbore.id,
            section_id=section.id,
            doc_type="ddr",
            title="DDR",
        )
    )
    await session.flush()

    await service(session).update_well(well.id, {"name": "NF-12 ST1"})
    await service(session).update_wellbore(wellbore.id, {"name": 'Main bore (12-1/4")'})
    await service(session).update_section(section.id, {"name": '12-1/4" section (rev B)'})

    document = (await session.execute(select(Document).where(Document.id == "doc_1"))).scalar_one()
    assert (document.well_id, document.wellbore_id, document.section_id) == (well.id, wellbore.id, section.id)
    assert (await service(session).get_well(well.id)).id == well.id

    counts = await service(session).well_context_counts(well.id)
    assert counts["documents"]["by_wellbore"] == {wellbore.id: 1}


async def test_the_well_ledger_is_the_history_of_that_well_alone(session, two_orgs):
    first = await _well(session, name="NF-12")
    second = await _well(session, name="NF-13")
    await service(session).update_well(second.id, {"operator": "Only the second"})

    history = await service(session).well_history(first.id)
    assert {row.well_id for row in history} == {first.id}
    assert [row.action for row in history] == ["well.create"]
    assert [row.action for row in await service(session).well_history(second.id)] == [
        "well.update",
        "well.create",
    ]

    with pytest.raises(NotFound):
        await service(session, ORG_B).well_history(first.id)


# --------------------------------------------------------------------------- idempotency


async def test_a_repeated_key_returns_the_stored_response_and_a_different_body_conflicts(session, two_orgs):
    org = ORG_A
    body = {"name": "NF-12"}
    assert (
        await replay_or_reserve(session, org_id=org, key="key-1", scope="asset.well.create", payload=body)
        is None
    )
    await complete(session, org_id=org, key="key-1", scope="asset.well.create", response={"id": "wel_1"})

    replayed = await replay_or_reserve(
        session, org_id=org, key="key-1", scope="asset.well.create", payload=body
    )
    assert replayed == {"id": "wel_1"}

    with pytest.raises(Conflict) as different:
        await replay_or_reserve(
            session, org_id=org, key="key-1", scope="asset.well.create", payload={"name": "Other"}
        )
    assert different.value.details["idempotency_key"] == "key-1"

    # Another organization using the same key string is a different caller.
    assert (
        await replay_or_reserve(session, org_id=ORG_B, key="key-1", scope="asset.well.create", payload=body)
        is None
    )
    # A different scope is a different operation.
    assert (
        await replay_or_reserve(
            session, org_id=org, key="key-1", scope="asset.well.update:wel_1", payload=body
        )
        is None
    )


async def test_a_key_reserved_but_not_completed_is_reported_as_in_flight(session, two_orgs):
    body = {"target": "drilling"}
    scope = "asset.well.lifecycle:wel_1"
    assert await replay_or_reserve(session, org_id=ORG_A, key="key-2", scope=scope, payload=body) is None
    with pytest.raises(Conflict) as in_flight:
        await replay_or_reserve(session, org_id=ORG_A, key="key-2", scope=scope, payload=body)
    assert "in progress" in in_flight.value.message


async def test_no_key_means_no_replay_record_is_written(session, two_orgs):
    assert await replay_or_reserve(session, org_id=ORG_A, key=None, scope="s", payload={}) is None
    await complete(session, org_id=ORG_A, key=None, scope="s", response={"id": "x"})
    from drillai.db.models import IdempotencyKey

    assert (await session.execute(select(IdempotencyKey))).scalars().all() == []


# --------------------------------------------------------------------------- timestamps


async def test_a_latent_well_row_is_still_transitionable(session, two_orgs):
    """A row written before the vocabulary existed must not be stuck once it appears."""
    project = await _project(session, name="Legacy")
    well = Well(
        id="wel_legacy",
        org_id=ORG_A,
        project_id=project.id,
        name="OLD-1",
        well_type="exploration",
        status="drilling",
    )
    session.add(well)
    await session.flush()

    moved = await service(session).transition_well(well.id, "suspended")
    assert moved.status == "suspended"

    legacy_status = Well(
        id="wel_legacy_2",
        org_id=ORG_A,
        project_id=project.id,
        name="OLD-2",
        well_type="exploration",
        status="spudded",
    )
    session.add(legacy_status)
    await session.flush()
    with pytest.raises(ValidationFailed) as unknown_state:
        await service(session).transition_well(legacy_status.id, "drilling")
    assert unknown_state.value.details["known"] == sorted(
        {
            "planned",
            "permitting",
            "drilling",
            "completing",
            "producing",
            "shut_in",
            "intervention",
            "suspended",
            "abandoned",
            "p&a",
        }
    )
