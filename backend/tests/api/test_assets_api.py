"""The master-data API: the mission scenario, the permission model, and the failure modes.

Everything here runs against the real application over ASGI with a real database (see
``tests/api/conftest.py``): no mocks, no borrowed service calls. The centrepiece is
``test_the_mission_scenario_end_to_end``, which walks the chain the mission describes —
project → field → well → wellbore → 12¼" section, a rename, a move to another field, a life-cycle
transition, a sidetrack with lineage, activating it, and adding sections — and then checks that the
identifiers, the history and the audit trail survived every step.
"""

from __future__ import annotations

import io

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from tests.api.conftest import headers

from drillai.db.models import Well, Wellbore

PREFIX = "/api/v1"


def _error(response) -> dict:
    return response.json()["error"]


async def _project(client, name: str = "North Sea", role: str = "well_manager") -> dict:
    response = await client.post(f"{PREFIX}/projects", json={"name": name, "operator": "DrillAI"}, headers=headers(role))
    assert response.status_code == 201, response.text
    return response.json()


async def _field(client, project_id: str, name: str = "Ekofisk", role: str = "engineer", **extra) -> dict:
    response = await client.post(
        f"{PREFIX}/fields", json={"project_id": project_id, "name": name, **extra}, headers=headers(role)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _well(client, project_id: str, name: str = "NF-12", role: str = "engineer", **extra) -> dict:
    response = await client.post(
        f"{PREFIX}/wells", json={"project_id": project_id, "name": name, **extra}, headers=headers(role)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _wellbore(client, well_id: str, name: str = "Main bore", role: str = "engineer", **extra) -> dict:
    response = await client.post(
        f"{PREFIX}/wells/{well_id}/wellbores", json={"name": name, **extra}, headers=headers(role)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _section(client, wellbore_id: str, sequence: int = 1, name: str = "12¼\" section", role: str = "engineer", **extra) -> dict:
    response = await client.post(
        f"{PREFIX}/wellbores/{wellbore_id}/sections",
        json={"sequence": sequence, "name": name, "hole_diameter_si": 0.31115, "hole_diameter_nominal": "12-1/4\"", **extra},
        headers=headers(role),
    )
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- the mission scenario


async def test_the_mission_scenario_end_to_end(client):
    """Project → field → well → wellbore → 12¼" section, rename, move field, transition, sidetrack,
    activate, more sections — with every identifier and the whole history preserved."""
    project = await _project(client)
    field_a = await _field(client, project["id"], "Ekofisk")
    field_b = await _field(client, project["id"], "Eldfisk")

    well = await _well(
        client,
        project["id"],
        "NF-12",
        field_id=field_a["id"],
        uwi=" no-15-9-1 ",
        api_number="15/9-19",
        well_type="development_producer",
        elevation_datum="rkb",
        surface_lat=56.5,
        surface_lon=3.2,
        kb_elevation_si=32.0,
        slot="A-12",
        pad_name="Nord",
        total_depth_planned_si=3200.0,
        spud_date="2026-01-15T00:00:00+00:00",
    )
    assert well["uwi"] == "NO-15-9-1", "identifiers are normalised, not stored as typed"
    assert well["field_id"] == field_a["id"]
    assert well["api_number"] == "15/9-19"
    assert well["slot"] == "A-12"
    assert well["allowed_transitions"] == ["drilling", "permitting"]

    wellbore = await _wellbore(client, well["id"], "Main bore", purpose="original", planned_td_md_si=3200.0)
    assert wellbore["is_active"] is True, "the first hole is the one being drilled"
    assert wellbore["datum"] == "rkb"

    section = await _section(
        client,
        wellbore["id"],
        sequence=3,
        name="12¼\" section",
        kind="production",
        planned_top_md_si=2400.0,
        planned_bottom_md_si=3200.0,
    )
    assert section["is_planned_only"] is True
    assert section["current_md_si"] is None, "a planned section has no current depth"
    assert section["semantics"]["planned_bottom_md_si"] == "plan"
    assert section["semantics"]["current_md_si"] == "progress"

    # A document is attached to the section, so the rename has something to preserve.
    upload = await client.post(
        f"{PREFIX}/documents",
        headers=headers(),
        files={"file": ("ddr.csv", io.BytesIO(b"date,depth_m\n2026-01-15,2400\n"), "text/csv")},
        data={"well_id": well["id"], "wellbore_id": wellbore["id"], "section_id": section["id"], "doc_type": "ddr"},
    )
    assert upload.status_code == 201, upload.text

    # --- rename: same id, new name, history intact ------------------------------------------------
    renamed = await client.patch(
        f"{PREFIX}/wells/{well['id']}",
        json={"name": "NF-12 ST1", "reason": "operator renamed the well", "expected_updated_at": well["updated_at"]},
        headers=headers(),
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["id"] == well["id"], "a rename is not a new well"
    assert renamed.json()["name"] == "NF-12 ST1"
    assert renamed.json()["uwi"] == "NO-15-9-1"

    # --- move to another field of the same project ------------------------------------------------
    moved = await client.patch(
        f"{PREFIX}/wells/{well['id']}", json={"field_id": field_b["id"]}, headers=headers()
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["field_id"] == field_b["id"]
    assert moved.json()["id"] == well["id"]

    # --- life-cycle transition --------------------------------------------------------------------
    transitioned = await client.post(
        f"{PREFIX}/wells/{well['id']}/lifecycle", json={"target": "drilling", "reason": "spudded"}, headers=headers()
    )
    assert transitioned.status_code == 200, transitioned.text
    assert transitioned.json()["status"] == "drilling"
    assert transitioned.json()["allowed_transitions"] == ["abandoned", "completing", "intervention", "suspended"]

    illegal = await client.post(f"{PREFIX}/wells/{well['id']}/lifecycle", json={"target": "p&a"}, headers=headers())
    assert illegal.status_code == 422
    assert illegal.json()["error"]["details"]["allowed"] == ["abandoned", "completing", "intervention", "suspended"]

    # --- sidetrack lineage ------------------------------------------------------------------------
    sidetrack = await _wellbore(
        client,
        well["id"],
        "ST-1",
        purpose="sidetrack",
        parent_wellbore_id=wellbore["id"],
        sequence=2,
        kickoff_md_si=2400.0,
    )
    assert sidetrack["parent_wellbore_id"] == wellbore["id"]
    assert sidetrack["is_active"] is False

    lineage = await client.get(f"{PREFIX}/wellbores/{sidetrack['id']}/lineage", headers=headers())
    assert lineage.status_code == 200
    assert [row["id"] for row in lineage.json()["items"]] == [wellbore["id"], sidetrack["id"]]
    assert lineage.json()["root_id"] == wellbore["id"]

    # --- activate the sidetrack -------------------------------------------------------------------
    activated = await client.post(f"{PREFIX}/wellbores/{sidetrack['id']}/activate?reason=main bore suspended", headers=headers())
    assert activated.status_code == 200, activated.text
    assert activated.json()["is_active"] is True
    listing = await client.get(f"{PREFIX}/wells/{well['id']}/wellbores", headers=headers())
    active = [row["id"] for row in listing.json()["items"] if row["is_active"]]
    assert active == [sidetrack["id"]], "exactly one active hole, and it is the one chosen"

    # --- two more sections on the sidetrack -------------------------------------------------------
    sidetrack_section = await _section(
        client, sidetrack["id"], sequence=1, name="8½\" section", kind="production", planned_bottom_md_si=3400.0
    )
    second = await _section(
        client, sidetrack["id"], sequence=2, name="6\" section", kind="liner", planned_top_md_si=3400.0, planned_bottom_md_si=3600.0
    )
    assert sidetrack_section["id"] != second["id"]

    # --- the whole structure in one read ----------------------------------------------------------
    structure = await client.get(f"{PREFIX}/wells/{well['id']}/structure", headers=headers())
    assert structure.status_code == 200, structure.text
    body = structure.json()
    assert body["well"]["id"] == well["id"]
    assert body["well"]["active_wellbore_id"] == sidetrack["id"]
    assert body["field"]["id"] == field_b["id"]
    assert [row["id"] for row in body["wellbores"]] == [wellbore["id"], sidetrack["id"]]
    assert len(body["wellbores"][1]["sections"]) == 2
    assert body["counts"]["documents"] == 1, "the document is still attached after the rename"
    assert body["wellbores"][0]["counts"]["documents"] == 1, "and it is still attributed to its wellbore"
    assert body["wellbores"][1]["counts"]["documents"] == 0

    # --- the ledger -------------------------------------------------------------------------------
    ledger = await client.get(f"{PREFIX}/wells/{well['id']}/audit-log", headers=headers())
    assert ledger.status_code == 200, ledger.text
    actions = [entry["action"] for entry in ledger.json()["items"]]
    for expected in ("well.create", "well.update", "well.lifecycle", "wellbore.create", "wellbore.activate", "section.create"):
        assert expected in actions, f"{expected} left no ledger entry"
    updates = [entry for entry in ledger.json()["items"] if entry["action"] == "well.update"]
    assert len(updates) == 2, "the rename and the move are separate, separately explainable edits"
    rename_entry = next(entry for entry in updates if "name" in entry["after"])
    assert rename_entry["before"]["name"] == "NF-12"
    assert rename_entry["after"]["name"] == "NF-12 ST1"
    assert rename_entry["details"]["changed"] == ["name"]
    move_entry = next(entry for entry in updates if "field_id" in entry["after"])
    assert move_entry["before"]["field_id"] == field_a["id"]
    assert move_entry["after"]["field_id"] == field_b["id"]
    assert rename_entry["actor_id"] == "usr_dev_admin_engineer"
    assert rename_entry["details"]["reason"] == "operator renamed the well"
    assert rename_entry["request_id"], "every ledger entry names the request that produced it"

    # --- the timeline shows the domain changes (§49) ---------------------------------------------
    timeline = await client.get(f"{PREFIX}/wells/{well['id']}/timeline", headers=headers())
    assert timeline.status_code == 200, timeline.text
    change_kinds = [
        entry
        for entry in timeline.json()["entries"]
        if entry["kind"] == "twin_change" and entry["category"] in {"well", "wellbore", "section"}
    ]
    assert any("updated" in entry["status"] for entry in change_kinds), "the rename must appear in the timeline"
    assert any(entry["status"] == "lifecycle" for entry in change_kinds)

    # --- the document still points at the same three ids -------------------------------------------
    detail = (await client.get(f"{PREFIX}/documents/{upload.json()['document']['id']}", headers=headers())).json()
    document = detail["document"]
    assert (document["well_id"], document["wellbore_id"], document["section_id"]) == (
        well["id"],
        wellbore["id"],
        section["id"],
    )


async def test_a_sidetrack_cannot_be_created_without_its_parent(client):
    """Lineage is structured data: the relation is required, never parsed out of the name."""
    project = await _project(client)
    well = await _well(client, project["id"])

    orphan = await client.post(
        f"{PREFIX}/wells/{well['id']}/wellbores",
        json={"name": "ST-1", "purpose": "sidetrack"},
        headers=headers(),
    )
    assert orphan.status_code == 422
    assert _error(orphan)["details"]["field"] == "parent_wellbore_id"

    # A name that *looks* like a sidetrack changes nothing: `original` with no parent is legal.
    plain = await client.post(
        f"{PREFIX}/wells/{well['id']}/wellbores", json={"name": "NF-12 ST-1 lookalike"}, headers=headers()
    )
    assert plain.status_code == 201
    assert plain.json()["purpose"] == "original"
    assert plain.json()["parent_wellbore_id"] is None


# --------------------------------------------------------------------------- the vocabulary (§5)


@pytest.mark.parametrize("well_type", ["development", "DEVELOPMENT", "dev", "producer", "oil"])
async def test_a_well_type_outside_the_canonical_vocabulary_is_refused_with_the_allowed_set(client, well_type):
    project = await _project(client)
    response = await client.post(
        f"{PREFIX}/wells", json={"project_id": project["id"], "name": "X", "well_type": well_type}, headers=headers()
    )
    assert response.status_code == 422, response.text
    error = _error(response)
    assert error["code"] == "platform.validation_failed"
    assert error["details"]["field"] == "well_type"
    assert error["details"]["value"] == well_type
    assert error["details"]["allowed"] == [
        "appraisal",
        "development_injector",
        "development_producer",
        "disposal",
        "exploration",
        "observation",
        "reentry",
        "sidetrack",
        "water_source",
    ]


@pytest.mark.parametrize(
    "well_type",
    ["exploration", "appraisal", "development_producer", "development_injector", "observation", "water_source", "disposal", "sidetrack", "reentry"],
)
async def test_every_canonical_well_type_is_accepted_and_stored_unchanged(client, well_type):
    project = await _project(client)
    response = await client.post(
        f"{PREFIX}/wells",
        json={"project_id": project["id"], "name": "TYPE-1", "well_type": well_type},
        headers=headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["well_type"] == well_type


@pytest.mark.parametrize("purpose", ["production", "development", "original_hole"])
async def test_a_wellbore_purpose_outside_the_vocabulary_is_refused(client, purpose):
    project = await _project(client)
    well = await _well(client, project["id"])
    response = await client.post(
        f"{PREFIX}/wells/{well['id']}/wellbores", json={"name": "Main", "purpose": purpose}, headers=headers()
    )
    assert response.status_code == 422, response.text
    assert _error(response)["details"]["allowed"] == [
        "bypass",
        "contingency",
        "original",
        "pilot",
        "reamed",
        "reentry",
        "sidetrack",
    ]


async def test_defaults_are_canonical_members_of_their_vocabularies(client):
    """The property the old defaults broke: omitting a field must not invent an invalid value."""
    project = await _project(client)
    well = await _well(client, project["id"])
    assert well["well_type"] == "development_producer"
    assert well["elevation_datum"] == "msl"
    wellbore = await _wellbore(client, well["id"], "Main")
    assert wellbore["purpose"] == "original"
    section = await _section(client, wellbore["id"])
    assert section["kind"] == "intermediate"


async def test_a_section_kind_outside_the_vocabulary_is_refused(client):
    project = await _project(client)
    well = await _well(client, project["id"])
    wellbore = await _wellbore(client, well["id"])
    response = await client.post(
        f"{PREFIX}/wellbores/{wellbore['id']}/sections",
        json={"sequence": 1, "name": "odd", "kind": "productionliner"},
        headers=headers(),
    )
    assert response.status_code == 422
    assert "production" in _error(response)["details"]["allowed"]


def _declared_keys(interface: str) -> set[str]:
    """The keys a TypeScript interface declares, read from ``types.ts``.

    A mirror that is only checked by hand drifts: this reads the frontend's own contract and compares
    it with the payload the server actually returns, so adding a backend field the interface does not
    know about is fine, while a key the interface *declares* and the server never sends is a bug the
    build would otherwise never see (the frontend would render ``undefined``).
    """
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[3] / "frontend" / "src" / "api" / "types.ts").read_text()
    body = re.search(rf"export interface {interface} \{{(.*?)\n\}}", source, re.S)
    assert body is not None, f"interface {interface} is not declared in types.ts"
    required: set[str] = set()
    for match in re.finditer(r"^\s{2}([A-Za-z_][A-Za-z0-9_]*)(\??):", body.group(1), re.M):
        if match.group(2) != "?":  # an optional key may legitimately be absent from one read
            required.add(match.group(1))
    return required


async def test_the_frontend_contract_declares_nothing_the_api_does_not_send(client):
    """§32/§33: ``types.ts`` mirrors the API, and the mirror is checked against the live payload."""
    project = await _project(client)
    field = await _field(client, project["id"], "Ekofisk")
    well = await _well(client, project["id"], "NF-12", field_id=field["id"])
    wellbore = await _wellbore(client, well["id"])
    section = await _section(client, wellbore["id"])

    detail = (await client.get(f"{PREFIX}/projects/{project['id']}", headers=headers())).json()
    for interface, payload in (
        ("Well", well),
        ("Wellbore", wellbore),
        ("WellSection", section),
        ("Project", detail),
    ):
        missing = _declared_keys(interface) - set(payload)
        assert missing == set(), f"types.ts declares {interface}.{sorted(missing)}, the API does not send it"


# --------------------------------------------------------------------------- permission model (§27)


@pytest.mark.parametrize(
    ("role", "status"),
    [
        ("viewer", 403),          # may read, may not write anything
        ("auditor", 403),         # reads everything, writes nothing
        ("data_manager", 403),    # owns ingestion, not the asset register
        ("engineer", 200),
        ("drilling_supervisor", 200),
        ("well_manager", 200),
        ("integrity_engineer", 200),
        ("admin", 403),           # administers the platform, is not an engineering authority
    ],
)
async def test_who_may_edit_master_data(client, role, status):
    project = await _project(client, name=f"P-{role}")
    well = await _well(client, project["id"], name=f"W-{role}")
    response = await client.patch(f"{PREFIX}/wells/{well['id']}", json={"operator": "X"}, headers=headers(role))
    assert response.status_code == status, f"{role}: {response.text}"
    if status == 403:
        assert _error(response)["code"] == "security.permission_denied"


@pytest.mark.parametrize("role", ["viewer", "auditor", "data_manager", "admin"])
async def test_who_may_transition_a_lifecycle(client, role):
    project = await _project(client, name=f"L-{role}")
    well = await _well(client, project["id"], name=f"L-{role}")
    response = await client.post(
        f"{PREFIX}/wells/{well['id']}/lifecycle", json={"target": "drilling"}, headers=headers(role)
    )
    assert response.status_code == 403, f"{role}: {response.text}"

    still_planned = await client.get(f"{PREFIX}/wells/{well['id']}", headers=headers("viewer"))
    assert still_planned.json()["status"] == "planned", "a refused transition must not have moved the well"


async def test_a_reader_can_still_read_the_structure_and_the_ledger(client):
    """Reading is not editing: the viewer sees the tree and the history, and nothing more."""
    project = await _project(client)
    well = await _well(client, project["id"])
    await _wellbore(client, well["id"])

    for path in (f"/wells/{well['id']}/structure", f"/wells/{well['id']}/audit-log", f"/wells/{well['id']}/wellbores", "/fields", "/rigs"):
        response = await client.get(f"{PREFIX}{path}", headers=headers("viewer"))
        assert response.status_code == 200, f"{path}: {response.text}"


async def test_an_unauthenticated_caller_is_refused_before_anything_else(client):
    for method, path in (("get", "/wells"), ("post", "/fields"), ("patch", "/wells/wel_x"), ("get", "/rigs")):
        response = await getattr(client, method)(f"{PREFIX}{path}", headers={"X-Dev-Roles": "not_a_role"})
        assert response.status_code == 403, f"{method} {path}: {response.text}"
        assert _error(response)["code"] == "security.permission_denied"


# --------------------------------------------------------------------------- update contract (§9)


async def test_an_edit_cannot_smuggle_a_status_or_an_identity_change(client):
    project = await _project(client)
    well = await _well(client, project["id"])

    status = await client.patch(f"{PREFIX}/wells/{well['id']}", json={"status": "producing"}, headers=headers())
    assert status.status_code == 422
    assert "the well lifecycle" in _error(status)["details"]["managed"]["status"]

    identity = await client.patch(f"{PREFIX}/wells/{well['id']}", json={"project_id": "prj_elsewhere"}, headers=headers())
    assert identity.status_code == 422
    assert _error(identity)["details"]["immutable"] == ["project_id"]

    unknown = await client.patch(f"{PREFIX}/wells/{well['id']}", json={"colour": "blue"}, headers=headers())
    assert unknown.status_code == 422
    assert _error(unknown)["details"]["unknown"] == ["colour"]


async def test_a_malformed_value_is_refused_by_type_at_the_boundary(client):
    project = await _project(client)
    well = await _well(client, project["id"])
    response = await client.patch(f"{PREFIX}/wells/{well['id']}", json={"tags": "not-a-list"}, headers=headers())
    assert response.status_code == 422, response.text
    assert "tags" in response.text


async def test_a_naive_timestamp_is_refused_rather_than_stored_as_local_time(client):
    project = await _project(client)
    response = await client.post(
        f"{PREFIX}/wells",
        json={"project_id": project["id"], "name": "TZ-1", "spud_date": "2026-01-15"},
        headers=headers(),
    )
    assert response.status_code == 422, response.text
    assert "timezone" in response.text.lower()


async def test_a_stale_edit_is_a_conflict_and_the_winner_is_not_overwritten(client):
    project = await _project(client)
    well = await _well(client, project["id"])
    read_at = well["updated_at"]

    first = await client.patch(
        f"{PREFIX}/wells/{well['id']}", json={"operator": "First"}, headers=headers(), params=None
    )
    assert first.status_code == 200
    assert first.json()["updated_at"] != read_at

    second = await client.patch(
        f"{PREFIX}/wells/{well['id']}",
        json={"operator": "Second", "expected_updated_at": read_at},
        headers=headers(),
    )
    assert second.status_code == 409, second.text
    error = _error(second)
    assert error["details"]["id"] == well["id"]
    assert error["details"]["expected_updated_at"] == read_at
    assert error["details"]["current_updated_at"] != read_at
    # Not retryable *as sent*: repeating the same request would conflict again, because the point is
    # that the caller's copy is out of date. The client re-reads, re-applies, and sends fresh data.
    assert error["retryable"] is False, "a stale write is not retried blindly"
    assert error["details"]["current_updated_at"], "the caller is told what the row looks like now"

    current = (await client.get(f"{PREFIX}/wells/{well['id']}", headers=headers())).json()
    assert current["operator"] == "First", "the second writer did not silently win"


async def test_editing_a_well_leaves_its_children_alone(client):
    project = await _project(client)
    well = await _well(client, project["id"])
    wellbore = await _wellbore(client, well["id"])
    section = await _section(client, wellbore["id"])

    response = await client.patch(
        f"{PREFIX}/wells/{well['id']}", json={"name": "Renamed", "total_depth_planned_si": 3300.0}, headers=headers()
    )
    assert response.status_code == 200

    children = (await client.get(f"{PREFIX}/wells/{well['id']}/wellbores", headers=headers())).json()
    assert [row["id"] for row in children["items"]] == [wellbore["id"]]
    sections = (await client.get(f"{PREFIX}/wellbores/{wellbore['id']}/sections", headers=headers())).json()
    assert [row["id"] for row in sections["items"]] == [section["id"]]


async def test_a_field_can_be_edited_and_the_alias_is_searchable(client):
    project = await _project(client)
    field = await _field(client, project["id"], "Ekofisk", aliases=["Ekofisk Vest", "2/4"])
    assert field["aliases"] == ["Ekofisk Vest", "2/4"]
    assert field["id"].startswith("fld_")

    updated = await client.patch(
        f"{PREFIX}/fields/{field['id']}",
        json={"notes": "Type area for the chalk", "aliases": ["Ekofisk Vest"]},
        headers=headers(),
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["notes"] == "Type area for the chalk"
    assert updated.json()["aliases"] == ["Ekofisk Vest"]

    found = await client.get(f"{PREFIX}/fields", params={"q": "vest"}, headers=headers())
    assert [row["id"] for row in found.json()["items"]] == [field["id"]]
    by_name = await client.get(f"{PREFIX}/fields", params={"project_id": project["id"]}, headers=headers())
    assert by_name.json()["total"] == 1


async def test_search_is_answered_by_the_server_across_identifiers(client):
    """§43: the browser never filters a page it was given; ``q`` looks at name, UWI, API and operator."""
    project = await _project(client)
    await _well(client, project["id"], "NF-12", uwi="NO-15-9-1", api_number="15/9-19", operator="Equinor")
    await _well(client, project["id"], "NF-13", uwi="NO-15-9-2", operator="Aker BP")

    for needle, expected in (("15-9-1", "NF-12"), ("15/9-19", "NF-12"), ("aker", "NF-13"), ("NF-1", None)):
        listing = await client.get(f"{PREFIX}/wells", params={"q": needle}, headers=headers())
        names = [row["name"] for row in listing.json()["items"]]
        if expected is None:
            assert names == ["NF-12", "NF-13"], needle
        else:
            assert names == [expected], needle


async def test_the_well_list_filters_in_the_database(client):
    project = await _project(client)
    field = await _field(client, project["id"], "F1")
    await _well(client, project["id"], "A-1", field_id=field["id"], well_type="exploration")
    await _well(client, project["id"], "A-2", well_type="development_producer")

    by_field = await client.get(f"{PREFIX}/wells", params={"field_id": field["id"]}, headers=headers())
    assert [row["name"] for row in by_field.json()["items"]] == ["A-1"]
    by_type = await client.get(f"{PREFIX}/wells", params={"well_type": "development_producer"}, headers=headers())
    assert [row["name"] for row in by_type.json()["items"]] == ["A-2"]
    assert by_field.json()["total"] == 1


# --------------------------------------------------------------------------- idempotency (§28)


async def test_a_retried_create_with_the_same_key_creates_one_well(client, db):
    project = await _project(client)
    body = {"project_id": project["id"], "name": "RETRY-1", "well_type": "exploration"}
    key = {"Idempotency-Key": "retry-create-1"}

    first = await client.post(f"{PREFIX}/wells", json=body, headers=headers(**key))
    assert first.status_code == 201, first.text
    second = await client.post(f"{PREFIX}/wells", json=body, headers=headers(**key))
    assert second.status_code == 201
    assert second.json() == first.json(), "a retry is answered from the stored response"

    async with db.session_factory() as session:
        count = (await session.execute(select(func.count()).select_from(Well))).scalar_one()
    assert count == 1, "the retry did not create a second well"


async def test_the_same_key_with_a_different_body_is_a_conflict(client):
    project = await _project(client)
    key = {"Idempotency-Key": "reused-key"}
    first = await client.post(
        f"{PREFIX}/wells", json={"project_id": project["id"], "name": "K-1"}, headers=headers(**key)
    )
    assert first.status_code == 201
    second = await client.post(
        f"{PREFIX}/wells", json={"project_id": project["id"], "name": "K-2"}, headers=headers(**key)
    )
    assert second.status_code == 409, second.text
    assert "different request body" in _error(second)["message"]


async def test_a_retried_transition_does_not_move_the_well_twice(client):
    """The duplicate a retry would otherwise cause: two lifecycle entries for one decision."""
    project = await _project(client)
    well = await _well(client, project["id"])
    key = {"Idempotency-Key": "transition-once"}
    body = {"target": "drilling", "reason": "spudded"}

    first = await client.post(f"{PREFIX}/wells/{well['id']}/lifecycle", json=body, headers=headers(**key))
    second = await client.post(f"{PREFIX}/wells/{well['id']}/lifecycle", json=body, headers=headers(**key))
    assert first.status_code == second.status_code == 200
    assert first.json()["status"] == second.json()["status"] == "drilling"

    ledger = (await client.get(f"{PREFIX}/wells/{well['id']}/audit-log", headers=headers())).json()["items"]
    assert [entry["action"] for entry in ledger].count("well.lifecycle") == 1


async def test_an_edit_retried_with_a_key_is_not_refused_as_stale(client):
    """Without the key, the retry of a successful edit would look like a stale write."""
    project = await _project(client)
    well = await _well(client, project["id"])
    key = {"Idempotency-Key": "edit-once"}
    body = {"operator": "Equinor", "expected_updated_at": well["updated_at"]}

    first = await client.patch(f"{PREFIX}/wells/{well['id']}", json=body, headers=headers(**key))
    second = await client.patch(f"{PREFIX}/wells/{well['id']}", json=body, headers=headers(**key))
    assert first.status_code == 200
    assert second.status_code == 200, second.text
    assert second.json() == first.json()


async def test_a_duplicate_well_is_refused_with_a_conflict_not_a_server_error(client):
    project = await _project(client)
    await _well(client, project["id"], "DUP-1")
    response = await client.post(f"{PREFIX}/wells", json={"project_id": project["id"], "name": "DUP-1"}, headers=headers())
    assert response.status_code == 409, response.text
    assert _error(response)["code"] == "platform.conflict"
    assert _error(response)["details"]["existing_id"]


async def test_the_database_itself_refuses_two_active_wellbores(client, db):
    """The rule is enforced by a constraint, not only by the service that happens to respect it."""
    from sqlalchemy.exc import IntegrityError

    project = await _project(client)
    well = await _well(client, project["id"])
    first = await _wellbore(client, well["id"], "Main bore")
    second = await _wellbore(client, well["id"], "ST-1", purpose="sidetrack", parent_wellbore_id=first["id"])

    async with db.session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                Wellbore.__table__.update().where(Wellbore.__table__.c.id == second["id"]).values(is_active=True)
            )
            await session.commit()


async def test_the_database_refuses_a_second_well_with_the_same_uwi(client, db):
    """Uniqueness is a database guarantee; the service's pre-check only produces the better message."""
    from sqlalchemy.exc import IntegrityError

    project = await _project(client)
    first = await _well(client, project["id"], "U-1", uwi="NO-15-9-9")
    async with db.session_factory() as session:
        org_id = (await session.execute(select(Well.org_id).where(Well.id == first["id"]))).scalar_one()
        session.add(
            Well(
                id="wel_forced",
                org_id=org_id,
                project_id=project["id"],
                name="U-2",
                uwi="NO-15-9-9",
                well_type="exploration",
                status="planned",
                elevation_datum="msl",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# --------------------------------------------------------------------------- tenancy (§42)


@pytest_asyncio.fixture
async def two_tenant_client(tmp_path, monkeypatch):
    """A second application with authentication *on*, two organizations and one token each.

    The development header cannot express a second tenant (the dev principal's org is the configured
    one), so cross-tenant isolation is tested the way it exists in production: bearer tokens whose
    rows carry the organization.
    """
    import httpx

    from drillai.api.app import create_app
    from drillai.core.config import get_settings, reset_settings_cache
    from drillai.db.models import Base, Membership, Organization, Project, User
    from drillai.security.passwords import hash_password, new_api_token

    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/tenancy.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    reset_settings_cache()
    application = create_app(get_settings())
    async with application.state.database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    tokens: dict[str, str] = {}
    ids: dict[str, dict[str, str]] = {}
    async with application.state.database.session_factory() as session:
        for slug in ("alpha", "bravo"):
            org = Organization(id=f"org_{slug}", slug=slug, name=slug.title())
            user = User(
                id=f"usr_{slug}",
                email=f"{slug}@example.com",
                display_name=slug.title(),
                password_hash=hash_password("correct-horse-battery-staple"),
            )
            session.add_all([org, user])
            await session.flush()
            session.add(Membership(id=f"mem_{slug}", user_id=user.id, org_id=org.id, role_key="engineer"))
            token, prefix, digest = new_api_token()
            from drillai.db.models import ApiToken

            session.add(
                ApiToken(
                    id=f"tok_{slug}",
                    org_id=org.id,
                    user_id=user.id,
                    name=f"{slug} token",
                    token_prefix=prefix,
                    token_hash=digest,
                )
            )
            tokens[slug] = token
            ids[slug] = {"org": org.id, "user": user.id}
            await session.flush()
        # Each tenant gets its own project, so a guessed id always has something to point at.
        for slug in ("alpha", "bravo"):
            session.add(
                Project(id=f"prj_{slug}", org_id=ids[slug]["org"], name=f"{slug} project", status="active", datum_policy="rkb")
            )
        await session.commit()

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http, tokens, ids, application.state.database
    await application.state.database.dispose()
    reset_settings_cache()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_a_guessed_identifier_from_another_org_is_a_404(two_tenant_client):
    http, tokens, _, _ = two_tenant_client
    alpha = _bearer(tokens["alpha"])
    bravo = _bearer(tokens["bravo"])

    well = (await http.post(f"{PREFIX}/wells", json={"project_id": "prj_alpha", "name": "A-1"}, headers=alpha)).json()
    wellbore = (
        await http.post(f"{PREFIX}/wells/{well['id']}/wellbores", json={"name": "Main"}, headers=alpha)
    ).json()
    section = (
        await http.post(f"{PREFIX}/wellbores/{wellbore['id']}/sections", json={"sequence": 1, "name": "S"}, headers=alpha)
    ).json()
    field = (await http.post(f"{PREFIX}/fields", json={"project_id": "prj_alpha", "name": "F"}, headers=alpha)).json()

    # The other tenant knows every id and every path; all of them are 404, and none of them leak.
    for method, path, kwargs in (
        ("get", f"/wells/{well['id']}", {}),
        ("get", f"/wells/{well['id']}/structure", {}),
        ("get", f"/wells/{well['id']}/wellbores", {}),
        ("get", f"/wells/{well['id']}/audit-log", {}),
        ("get", f"/wellbores/{wellbore['id']}", {}),
        ("get", f"/wellbores/{wellbore['id']}/lineage", {}),
        ("get", f"/wellbores/{wellbore['id']}/sections", {}),
        ("get", f"/fields/{field['id']}", {}),
        ("patch", f"/wells/{well['id']}", {"json": {"name": "stolen"}}),
        ("post", f"/wells/{well['id']}/lifecycle", {"json": {"target": "drilling"}}),
        ("post", f"/wells/{well['id']}/wellbores", {"json": {"name": "mine now"}}),
        ("post", f"/wellbores/{wellbore['id']}/activate", {}),
        ("patch", f"/wellbores/{wellbore['id']}/sections/{section['id']}", {"json": {"name": "stolen"}}),
    ):
        response = await getattr(http, method)(f"{PREFIX}{path}", headers=bravo, **kwargs)
        assert response.status_code == 404, f"{method} {path}: {response.status_code} {response.text}"

    # Lists are filtered by the token's organization, not by anything the caller sends.
    for path in ("/wells", "/fields", "/rigs"):
        listing = await http.get(f"{PREFIX}{path}", headers=bravo)
        assert listing.status_code == 200, listing.text
        assert listing.json()["items"] == [], f"{path} leaked another org's rows"

    # And a well in another org cannot be named as a field's project, or as a well's project.
    forged = await http.post(
        f"{PREFIX}/wells", json={"project_id": "prj_alpha", "name": "Smuggled"}, headers=bravo
    )
    assert forged.status_code == 404
    forged_field = await http.post(f"{PREFIX}/fields", json={"project_id": "prj_alpha", "name": "Smuggled"}, headers=bravo)
    assert forged_field.status_code == 404


async def test_a_field_from_another_project_is_refused_on_create_and_update(two_tenant_client):
    http, tokens, _, _ = two_tenant_client
    alpha = _bearer(tokens["alpha"])
    await http.post(f"{PREFIX}/fields", json={"project_id": "prj_alpha", "name": "F"}, headers=alpha)
    well = (await http.post(f"{PREFIX}/wells", json={"project_id": "prj_alpha", "name": "A-1"}, headers=alpha)).json()

    # A field in *another project of the same organization* is refused too: existence is not scope.
    http, _, ids, database = two_tenant_client
    from drillai.db.models import Field as FieldModel
    from drillai.db.models import Project

    async with database.session_factory() as session:
        session.add(
            Project(id="prj_alpha_2", org_id=ids["alpha"]["org"], name="second project", status="active", datum_policy="rkb")
        )
        session.add(FieldModel(id="fld_elsewhere", org_id=ids["alpha"]["org"], project_id="prj_alpha_2", name="Other field"))
        await session.commit()

    response = await http.patch(f"{PREFIX}/wells/{well['id']}", json={"field_id": "fld_elsewhere"}, headers=alpha)
    assert response.status_code == 422, response.text
    assert _error(response)["details"]["well_project_id"] == "prj_alpha"
