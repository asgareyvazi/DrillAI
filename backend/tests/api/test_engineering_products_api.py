"""API/database integration for scoped, versioned drilling programs and controlled procedures."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from tests.api.conftest import headers

from drillai.db.models import Artifact, AuditLog

pytestmark = pytest.mark.asyncio


def _quantity(value: float, unit: str, dimension: str = "length") -> dict:
    return {
        "state": "known",
        "dimension": dimension,
        "value": value,
        "unit": unit,
        "source_kind": "operator_entered",
    }


def _program_payload(section_id: str) -> dict:
    return {
        "purpose": "Synthetic authoring-workflow fixture; not a field program.",
        "objectives": [{"sequence": 1, "statement": "Retain the explicitly entered synthetic target."}],
        "design_basis": [
            {
                "topic": "synthetic_fixture",
                "statement": "No field data was supplied to this test.",
                "basis": "unknown",
                "reason": "Test fixture intentionally contains no operating evidence.",
            }
        ],
        "sections": [
            {
                "sequence": 1,
                "name": "Synthetic test section",
                "section_id": section_id,
                "hole_diameter": _quantity(8.5, "in"),
                "planned_md_from": _quantity(0.0, "m"),
                "planned_md_to": _quantity(2000.0, "m"),
                "mud_density_min": _quantity(9.0, "ppg", "density"),
                "mud_density_max": _quantity(12.0, "ppg", "density"),
                "operating_parameters": [
                    {
                        "key": "flow_rate",
                        "label": "Synthetic test flow rate",
                        "value": _quantity(800.0, "gpm", "volume_flow"),
                    }
                ],
                "planned_operations": [
                    {
                        "sequence": 1,
                        "activity": "Synthetic test activity",
                        "purpose": "Workflow coverage only; not an operating instruction.",
                        "acceptance_criteria": ["Use only approved, independently reviewed design inputs."],
                    }
                ],
            }
        ],
    }


def _procedure_payload() -> dict:
    return {
        "purpose": "Synthetic controlled-procedure workflow fixture.",
        "scope_statement": "Test-only scope; not an operating procedure.",
        "prerequisites": [
            {
                "sequence": 1,
                "requirement": "A human reviewer verifies the exact revision reference.",
                "verification": "Compare the version ID and content hash in the controlled record.",
            }
        ],
        "steps": [
            {
                "sequence": 1,
                "title": "Verify the test record",
                "instruction": "Read the versioned test record only; do not perform field work.",
                "responsible_role": "test_reviewer",
                "preconditions": ["The fixture identifies itself as synthetic."],
                "hazards": ["The test record could be mistaken for an approved field instruction."],
                "control_measures": ["Keep the synthetic/not-accepted label visible."],
                "verification_criteria": ["The immutable revision ID and hash are present."],
                "response_if_not_met": "Stop this test workflow and record the mismatch.",
                "hold_point": True,
            }
        ],
        "stop_work_conditions": [
            {
                "sequence": 1,
                "trigger": "A reader treats the synthetic record as field authorization.",
                "immediate_response": "Stop; this record grants no operational authority.",
                "escalation_role": "test_reviewer",
            }
        ],
    }


async def _create_well(client, *, name: str = "Engineering product API test") -> tuple[str, str, str, str]:
    manager = headers("well_manager")
    project = await client.post("/api/v1/projects", json={"name": f"{name} project"}, headers=manager)
    assert project.status_code == 201, project.text
    well = await client.post(
        "/api/v1/wells",
        json={"project_id": project.json()["id"], "name": name, "well_type": "development_producer"},
        headers=manager,
    )
    assert well.status_code == 201, well.text
    well_id = well.json()["id"]
    wellbore = await client.post(
        f"/api/v1/wells/{well_id}/wellbores",
        json={"name": "Synthetic main bore", "purpose": "original", "planned_td_md_si": 2500.0},
        headers=manager,
    )
    assert wellbore.status_code == 201, wellbore.text
    section = await client.post(
        f"/api/v1/wellbores/{wellbore.json()['id']}/sections",
        json={"sequence": 1, "name": "Synthetic section", "kind": "intermediate"},
        headers=manager,
    )
    assert section.status_code == 201, section.text
    return project.json()["id"], well_id, wellbore.json()["id"], section.json()["id"]


async def _approve(client, artifact_id: str, version_id: str) -> dict:
    validated = await client.post(
        f"/api/v1/engineering-products/{artifact_id}/versions/{version_id}/validate",
        headers=headers("engineer"),
    )
    assert validated.status_code == 200, validated.text
    validation = validated.json()["validation"]
    assert validation["state"] in {"passed", "warnings"}
    assert validation["field_acceptance"] == "not_established"

    submitted = await client.post(
        f"/api/v1/engineering-products/{artifact_id}/versions/{version_id}/submit",
        headers=headers("drilling_supervisor"),
    )
    assert submitted.status_code == 202, submitted.text
    approval_id = submitted.json()["approval_request"]["id"]
    self_review = await client.post(
        f"/api/v1/engineering-products/{artifact_id}/versions/{version_id}/review",
        json={"notes": "This submitter must not review their own request."},
        headers=headers("drilling_supervisor"),
    )
    assert self_review.status_code == 409, self_review.text
    reviewed = await client.post(
        f"/api/v1/engineering-products/{artifact_id}/versions/{version_id}/review",
        json={"notes": "Synthetic independent-review check; not field acceptance."},
        headers=headers("well_manager"),
    )
    assert reviewed.status_code == 200, reviewed.text

    self_decision = await client.post(
        f"/api/v1/engineering-products/{artifact_id}/versions/{version_id}/decision",
        json={"approval_id": approval_id, "decision": "approved", "note": "Reviewer cannot decide this request."},
        headers=headers("well_manager"),
    )
    assert self_decision.status_code == 409, self_decision.text
    decision = await client.post(
        f"/api/v1/engineering-products/{artifact_id}/versions/{version_id}/decision",
        json={
            "approval_id": approval_id,
            "decision": "approved",
            "note": "Approved only for this synthetic API workflow test; no operational authorization.",
        },
        headers=headers("well_manager,drilling_supervisor"),
    )
    assert decision.status_code == 200, decision.text
    return decision.json()


async def test_versioned_program_and_procedure_review_export_and_stale_link(client, db):
    project_id, well_id, wellbore_id, section_id = await _create_well(client)
    payload = _program_payload(section_id)
    create_body = {
        "kind": "program",
        "title": "  Synthetic program v1  ",
        "code": " SYN-PROG-1 ",
        "project_id": project_id,
        "wellbore_id": wellbore_id,
        "payload": payload,
    }
    create_headers = headers("engineer", **{"Idempotency-Key": "synthetic-program-create-v1"})
    created = await client.post(
        f"/api/v1/wells/{well_id}/engineering-products",
        json=create_body,
        headers=create_headers,
    )
    assert created.status_code == 201, created.text
    created_json = created.json()
    artifact = created_json["artifact"]
    version = created_json["version"]
    assert artifact["title"] == "Synthetic program v1"
    assert artifact["code"] == "SYN-PROG-1"
    assert version["schema_key"] == "drilling_program"
    assert version["payload"]["sections"][0]["hole_diameter"]["value_si"] == pytest.approx(0.2159)
    assert version["content_hash"]

    replay = await client.post(
        f"/api/v1/wells/{well_id}/engineering-products",
        json=create_body,
        headers=create_headers,
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["artifact"]["id"] == artifact["id"]

    export_before_validation = await client.get(
        f"/api/v1/engineering-products/{artifact['id']}/versions/{version['id']}/exports/json",
        headers=headers("engineer"),
    )
    assert export_before_validation.status_code == 200, export_before_validation.text
    exported = export_before_validation.json()
    assert exported["revision"]["content_hash"] == version["content_hash"]
    assert exported["revision"]["field_acceptance"] == "not_established"
    assert exported["export_limits"]["operational_control"] == "not_available"

    approved_v1 = await _approve(client, artifact["id"], version["id"])
    assert approved_v1["version"]["state"] == "approved"
    assert approved_v1["artifact"]["status"] == "approved"
    assert approved_v1["version"]["content_hash"] == version["content_hash"]
    markdown = await client.get(
        f"/api/v1/engineering-products/{artifact['id']}/versions/{version['id']}/exports/markdown",
        headers=headers("engineer"),
    )
    assert markdown.status_code == 200, markdown.text
    assert "Synthetic program v1" in markdown.text
    assert "does not perform calculations" in markdown.text

    procedure_body = {
        "kind": "procedure",
        "title": "Synthetic procedure",
        "project_id": project_id,
        "wellbore_id": wellbore_id,
        "payload": _procedure_payload(),
        "linked_program_version_id": version["id"],
    }
    procedure_response = await client.post(
        f"/api/v1/wells/{well_id}/engineering-products",
        json=procedure_body,
        headers=headers("engineer", **{"Idempotency-Key": "synthetic-procedure-create-v1"}),
    )
    assert procedure_response.status_code == 201, procedure_response.text
    procedure = procedure_response.json()
    assert procedure["version"]["validation_summary"]["linked_program"]["version_id"] == version["id"]
    assert procedure["version"]["validation_summary"]["linked_program"]["content_hash"] == version["content_hash"]

    # A new approved program supersedes v1. The procedure continues to name v1 exactly and is blocked
    # until an author creates a new procedure revision with an explicit replacement reference.
    revision_path = f"/api/v1/engineering-products/{artifact['id']}/versions"
    competing_revisions = await asyncio.gather(
        client.post(
            revision_path,
            json={
                "expected_current_version": 1,
                "change_reason": "Synthetic concurrent candidate A; test exact-version supersession.",
                "payload": {**payload, "purpose": "Synthetic program revision candidate A; test data only."},
            },
            headers=headers("engineer", **{"Idempotency-Key": "synthetic-program-revision-v2-a"}),
        ),
        client.post(
            revision_path,
            json={
                "expected_current_version": 1,
                "change_reason": "Synthetic concurrent candidate B; test exact-version supersession.",
                "payload": {**payload, "purpose": "Synthetic program revision candidate B; test data only."},
            },
            headers=headers("engineer", **{"Idempotency-Key": "synthetic-program-revision-v2-b"}),
        ),
    )
    assert sorted(response.status_code for response in competing_revisions) == [201, 409], [
        (response.status_code, response.text) for response in competing_revisions
    ]
    revised_program = next(response for response in competing_revisions if response.status_code == 201)
    v2 = revised_program.json()["version"]
    await _approve(client, artifact["id"], v2["id"])

    stale_procedure = await client.post(
        f"/api/v1/engineering-products/{procedure['artifact']['id']}/versions/{procedure['version']['id']}/validate",
        headers=headers("engineer"),
    )
    assert stale_procedure.status_code == 200, stale_procedure.text
    stale_validation = stale_procedure.json()["validation"]
    assert stale_validation["state"] == "blocked"
    assert any(item["code"] == "linked_program_revision_not_currently_approved" for item in stale_validation["findings"])
    assert stale_procedure.json()["version"]["validation_summary"]["linked_program"]["version_id"] == version["id"]

    history = await client.get(f"/api/v1/engineering-products/{artifact['id']}/versions", headers=headers("auditor"))
    assert history.status_code == 200, history.text
    by_number = {item["version"]: item for item in history.json()["items"]}
    assert by_number[1]["state"] == "superseded"
    assert by_number[1]["content_hash"] == version["content_hash"]
    assert by_number[2]["state"] == "approved"

    async with db.session_factory() as session:
        audit_rows = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.org_id == artifact["org_id"],
                        AuditLog.resource_id.in_([artifact["id"], version["id"], v2["id"]]),
                    )
                )
            ).scalars().all()
        )
    audit_actions = {row.action for row in audit_rows}
    assert {
        "artifact.draft",
        "artifact.validate",
        "artifact.submit",
        "artifact.review",
        "artifact.approve",
        "artifact.export",
    } <= audit_actions


async def test_program_schema_and_revision_lifecycle_reject_invalid_or_self_approved_drafts(client):
    project_id, well_id, wellbore_id, _section_id = await _create_well(client, name="Invalid product test")
    invalid_body = {
        "kind": "program",
        "title": "Empty structured program",
        "project_id": project_id,
        "wellbore_id": wellbore_id,
        "payload": {"objectives": [], "sections": []},
    }
    created = await client.post(
        f"/api/v1/wells/{well_id}/engineering-products",
        json=invalid_body,
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    artifact = created.json()["artifact"]
    version = created.json()["version"]
    invalid = await client.post(
        f"/api/v1/engineering-products/{artifact['id']}/versions/{version['id']}/validate",
        headers=headers("engineer"),
    )
    assert invalid.status_code == 200, invalid.text
    assert invalid.json()["validation"]["state"] == "blocked"
    rejected_submit = await client.post(
        f"/api/v1/engineering-products/{artifact['id']}/versions/{version['id']}/submit",
        headers=headers("drilling_supervisor"),
    )
    assert rejected_submit.status_code == 422, rejected_submit.text

    wrong_scope = await client.post(
        f"/api/v1/wells/{well_id}/engineering-products",
        json={
            "kind": "program",
            "title": "Wrong section reference",
            "project_id": project_id,
            "wellbore_id": wellbore_id,
            "payload": _program_payload("section_not_in_this_well"),
        },
        headers=headers("engineer"),
    )
    assert wrong_scope.status_code == 404, wrong_scope.text


async def test_two_well_and_two_tenant_scope_isolation(fabric):
    alpha = fabric.alpha
    first_wellbore = alpha.wellbore_id
    first_section = alpha.section_id
    second_well_id = await fabric.add_well(alpha, "ALPHA-2-SYNTHETIC")
    second_wellbore = await fabric.add_wellbore(alpha, second_well_id, "Second synthetic bore")
    second_section = await fabric.add_section(alpha, second_wellbore, "Second synthetic section")

    created_ids: dict[str, str] = {}
    for well_id, wellbore_id, section_id, code in (
        (alpha.well_id, first_wellbore, first_section, "ALPHA-PROGRAM-1"),
        (second_well_id, second_wellbore, second_section, "ALPHA-PROGRAM-2"),
    ):
        response = await fabric.http.post(
            f"/api/v1/wells/{well_id}/engineering-products",
            json={
                "kind": "program",
                "title": f"Synthetic program for {code}",
                "code": code,
                "project_id": alpha.project_id,
                "wellbore_id": wellbore_id,
                "payload": _program_payload(section_id),
            },
            headers=alpha.headers,
        )
        assert response.status_code == 201, response.text
        created_ids[well_id] = response.json()["artifact"]["id"]

    for well_id, expected_id in created_ids.items():
        listed = await fabric.http.get(f"/api/v1/wells/{well_id}/engineering-products", headers=alpha.headers)
        assert listed.status_code == 200, listed.text
        assert listed.json()["total"] == 1
        assert [item["artifact"]["id"] for item in listed.json()["items"]] == [expected_id]

    mismatch = await fabric.http.post(
        f"/api/v1/wells/{alpha.well_id}/engineering-products",
        json={
            "kind": "program",
            "title": "Cross-well section reference must fail",
            "project_id": alpha.project_id,
            "wellbore_id": first_wellbore,
            "payload": _program_payload(second_section),
        },
        headers=alpha.headers,
    )
    assert mismatch.status_code == 422, mismatch.text

    bravo = await fabric.http.get(
        f"/api/v1/engineering-products/{created_ids[alpha.well_id]}",
        headers=fabric.bravo.headers,
    )
    assert bravo.status_code == 404, bravo.text
    cross_tenant_list = await fabric.http.get(
        f"/api/v1/wells/{alpha.well_id}/engineering-products",
        headers=fabric.bravo.headers,
    )
    assert cross_tenant_list.status_code == 404, cross_tenant_list.text

    async with fabric.session() as session:
        scoped = list(
            (
                await session.execute(
                    select(Artifact).where(
                        Artifact.id.in_(list(created_ids.values())),
                        Artifact.org_id == fabric.org_id(alpha),
                    )
                )
            ).scalars().all()
        )
        assert {item.well_id for item in scoped} == set(created_ids)
        assert all(item.org_id == fabric.org_id(alpha) for item in scoped)
