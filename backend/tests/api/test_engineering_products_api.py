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


async def test_program_overlapping_hole_sections_are_blocked_before_submission(client):
    import copy

    project_id, well_id, wellbore_id, section_id = await _create_well(client, name="Overlapping sections")
    payload = _program_payload(section_id)
    payload["sections"][0]["planned_md_to"] = _quantity(1000, "m")
    second = copy.deepcopy(payload["sections"][0])
    second.update(sequence=2, name="Overlapping section", section_id=None,
                  planned_md_from=_quantity(900, "m"), planned_md_to=_quantity(2000, "m"))
    payload["sections"].append(second)
    created = await client.post(
        f"/api/v1/wells/{well_id}/engineering-products",
        json={"kind": "program", "title": "Synthetic overlap draft", "project_id": project_id,
              "wellbore_id": wellbore_id, "payload": payload},
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    artifact, version = created.json()["artifact"], created.json()["version"]
    path = f"/api/v1/engineering-products/{artifact['id']}/versions/{version['id']}"
    validated = await client.post(path + "/validate", headers=headers("engineer"))
    assert validated.status_code == 200, validated.text
    validation = validated.json()["validation"]
    assert validation["state"] == "blocked"
    assert any(item["code"] == "section_depth_overlap" for item in validation["findings"])
    rejected = await client.post(path + "/submit", headers=headers("drilling_supervisor"))
    assert rejected.status_code == 422, rejected.text


async def test_program_cannot_submit_with_outdated_validation_ruleset(client, db):
    from drillai.db.models import ArtifactVersion

    project_id, well_id, wellbore_id, section_id = await _create_well(client, name="Stale ruleset")
    created = await client.post(
        f"/api/v1/wells/{well_id}/engineering-products",
        json={"kind": "program", "title": "Ruleset test", "project_id": project_id,
              "wellbore_id": wellbore_id, "payload": _program_payload(section_id)},
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    artifact, version = created.json()["artifact"], created.json()["version"]
    path = f"/api/v1/engineering-products/{artifact['id']}/versions/{version['id']}"
    validated = await client.post(path + "/validate", headers=headers("engineer"))
    assert validated.status_code == 200, validated.text
    async with db.session() as session:
        stored = (await session.execute(select(ArtifactVersion).where(ArtifactVersion.id == version["id"]))).scalar_one()
        summary = dict(stored.validation_summary)
        summary["validation"] = {**summary["validation"], "ruleset": "drillai.program-structure.v0"}
        stored.validation_summary = summary
    rejected = await client.post(path + "/submit", headers=headers("drilling_supervisor"))
    assert rejected.status_code == 409, rejected.text
    refreshed = await client.post(path + "/validate", headers=headers("engineer"))
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["validation"]["ruleset"] == "drillai.program-structure.v2"
    accepted = await client.post(path + "/submit", headers=headers("drilling_supervisor"))
    assert accepted.status_code == 202, accepted.text


async def test_in_review_program_rejects_outdated_rules_until_revalidated(client, db):
    from drillai.db.models import ArtifactVersion

    project_id, well_id, wellbore_id, section_id = await _create_well(client, name="Stale review")
    created = await client.post(
        f"/api/v1/wells/{well_id}/engineering-products",
        json={"kind": "program", "title": "Ruleset review test", "project_id": project_id,
              "wellbore_id": wellbore_id, "payload": _program_payload(section_id)},
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    artifact, version = created.json()["artifact"], created.json()["version"]
    path = f"/api/v1/engineering-products/{artifact['id']}/versions/{version['id']}"
    assert (await client.post(path + "/validate", headers=headers("engineer"))).status_code == 200
    submitted = await client.post(path + "/submit", headers=headers("drilling_supervisor"))
    assert submitted.status_code == 202
    old_approval_id = submitted.json()["approval_request"]["id"]
    async with db.session() as session:
        stored = (await session.execute(select(ArtifactVersion).where(ArtifactVersion.id == version["id"]))).scalar_one()
        summary = dict(stored.validation_summary)
        summary["validation"] = {**summary["validation"], "ruleset": "drillai.program-structure.v1"}
        stored.validation_summary = summary
    review_body = {"notes": "Synthetic review only, not field acceptance."}
    rejected = await client.post(path + "/review", headers=headers("well_manager"), json=review_body)
    assert rejected.status_code == 409, rejected.text
    withdrawn = await client.post(path + "/withdraw", headers=headers("drilling_supervisor"),
                                  json={"reason": "Validation rules changed; restart independent review."})
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["version"]["state"] == "draft"
    assert withdrawn.json()["approval_request"]["status"] == "cancelled"
    assert withdrawn.json()["approval_request"]["id"] == old_approval_id
    revalidated = await client.post(path + "/validate", headers=headers("engineer"))
    assert revalidated.status_code == 200, revalidated.text
    assert revalidated.json()["validation"]["ruleset"] == "drillai.program-structure.v2"
    resubmitted = await client.post(path + "/submit", headers=headers("drilling_supervisor"))
    assert resubmitted.status_code == 202, resubmitted.text
    assert resubmitted.json()["approval_request"]["id"] != old_approval_id
    detail = await client.get(path, headers=headers("engineer"))
    assert detail.status_code == 200, detail.text
    history = detail.json()["approval_history"]
    assert {item["id"] for item in history} == {old_approval_id, resubmitted.json()["approval_request"]["id"]}
    assert next(item for item in history if item["id"] == old_approval_id)["status"] == "cancelled"
    reviewed = await client.post(path + "/review", headers=headers("well_manager"), json=review_body)
    assert reviewed.status_code == 200, reviewed.text


async def test_withdrawal_preserves_review_history_and_requires_new_authorization(client, db):
    from drillai.db.models import ApprovalRequest

    project_id, well_id, wellbore_id, section_id = await _create_well(client, name="Withdraw review")
    created = await client.post(
        f"/api/v1/wells/{well_id}/engineering-products",
        json={"kind": "program", "title": "Withdrawal history", "project_id": project_id,
              "wellbore_id": wellbore_id, "payload": _program_payload(section_id)},
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    artifact, version = created.json()["artifact"], created.json()["version"]
    path = f"/api/v1/engineering-products/{artifact['id']}/versions/{version['id']}"
    assert (await client.post(path + "/validate", headers=headers("engineer"))).status_code == 200
    submitted = await client.post(path + "/submit", headers=headers("drilling_supervisor"))
    assert submitted.status_code == 202, submitted.text
    old_id = submitted.json()["approval_request"]["id"]
    reviewed = await client.post(path + "/review", headers=headers("well_manager"),
                                 json={"notes": "Synthetic review to retain in history."})
    assert reviewed.status_code == 200, reviewed.text
    forbidden = await client.post(path + "/withdraw", headers=headers("engineer"), json={"reason": "No permission."})
    assert forbidden.status_code == 403, forbidden.text
    withdrawn = await client.post(path + "/withdraw", headers=headers("drilling_supervisor"),
                                  json={"reason": "Restart review after revised validation."})
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["version"]["reviewed_by"] is None
    assert withdrawn.json()["version"]["validation_summary"].get("validation") is None
    async with db.session() as session:
        old = (await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == old_id))).scalar_one()
        assert old.status == "cancelled"
        assert old.request_payload["review"]["notes"] == "Synthetic review to retain in history."
        assert old.request_payload["withdrawal"]["reason"] == "Restart review after revised validation."
        assert old.request_payload["withdrawn_validation"]["ruleset"] == "drillai.program-structure.v2"
        audit = (await session.execute(select(AuditLog).where(
            AuditLog.org_id == artifact["org_id"], AuditLog.resource_id == version["id"],
            AuditLog.action == "artifact.withdraw",
        ))).scalars().all()
        assert len(audit) == 1
    repeated = await client.post(path + "/withdraw", headers=headers("drilling_supervisor"), json={"reason": "Repeat"})
    assert repeated.status_code == 409
    resubmit_without_validation = await client.post(path + "/submit", headers=headers("drilling_supervisor"))
    assert resubmit_without_validation.status_code == 409
    old_decision = await client.post(path + "/decision", headers=headers("well_manager,drilling_supervisor"),
                                     json={"approval_id": old_id, "decision": "approved", "note": "Stale"})
    assert old_decision.status_code == 409


async def _submitted_program_for_transition_test(client):
    project_id, well_id, wellbore_id, section_id = await _create_well(client, name="Concurrent review")
    created = await client.post(
        f"/api/v1/wells/{well_id}/engineering-products",
        json={"kind": "program", "title": "Synthetic concurrent review", "project_id": project_id,
              "wellbore_id": wellbore_id, "payload": _program_payload(section_id)},
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    artifact, version = created.json()["artifact"], created.json()["version"]
    path = f"/api/v1/engineering-products/{artifact['id']}/versions/{version['id']}"
    assert (await client.post(path + "/validate", headers=headers("engineer"))).status_code == 200
    submitted = await client.post(path + "/submit", headers=headers("drilling_supervisor"))
    assert submitted.status_code == 202, submitted.text
    return artifact, version, path, submitted.json()["approval_request"]["id"]


@pytest.mark.parametrize("competitor", ["decision", "review", "withdraw"])
async def test_competing_withdrawal_transitions_keep_one_consistent_record(client, db, competitor):
    from drillai.db.models import ApprovalRequest, ArtifactVersion

    artifact, version, path, approval_id = await _submitted_program_for_transition_test(client)
    if competitor == "decision":
        reviewed = await client.post(path + "/review", headers=headers("well_manager"),
                                     json={"notes": "Independent synthetic review."})
        assert reviewed.status_code == 200, reviewed.text

    async def withdraw():
        return await client.post(path + "/withdraw", headers=headers("drilling_supervisor"),
                                 json={"reason": "Concurrent synthetic withdrawal."})

    async def competing():
        if competitor == "decision":
            return await client.post(path + "/decision", headers=headers("well_manager,drilling_supervisor"),
                                     json={"approval_id": approval_id, "decision": "approved",
                                           "note": "Synthetic decision only."})
        if competitor == "review":
            return await client.post(path + "/review", headers=headers("well_manager"),
                                     json={"notes": "Concurrent independent synthetic review."})
        return await withdraw()

    first, second = await asyncio.gather(withdraw(), competing())
    async with db.session() as session:
        actual_artifact = (await session.execute(select(Artifact).where(Artifact.id == artifact["id"]))).scalar_one()
        actual_version = (await session.execute(select(ArtifactVersion).where(ArtifactVersion.id == version["id"]))).scalar_one()
        approval = (await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))).scalar_one()
        terminal = list((await session.execute(select(AuditLog).where(
            AuditLog.org_id == artifact["org_id"], AuditLog.resource_id == version["id"],
            AuditLog.action.in_(["artifact.withdraw", "artifact.approve"]),
        ))).scalars().all())
        assert len(terminal) == 1
        if approval.status == "cancelled":
            assert first.status_code == 200 or second.status_code == 200
            assert actual_artifact.status == actual_version.state == "draft"
            assert actual_version.reviewed_by is None
            assert terminal[0].action == "artifact.withdraw"
            assert approval.request_payload["withdrawal"]["reason"] == "Concurrent synthetic withdrawal."
            if competitor == "review" and second.status_code == 200:
                assert approval.request_payload["review"]["notes"] == "Concurrent independent synthetic review."
        else:
            assert competitor == "decision"
            assert approval.status == actual_artifact.status == actual_version.state == "approved"
            assert terminal[0].action == "artifact.approve"
        if competitor != "review":
            assert sorted([first.status_code, second.status_code]) in ([200, 404], [200, 409])
        else:
            assert first.status_code == 200 and second.status_code in {200, 409}


async def test_withdrawal_rolls_back_every_change_if_audit_fails(client, db, monkeypatch):
    from drillai.api.routers import engineering_products
    from drillai.db.models import ApprovalRequest, ArtifactVersion

    artifact, version, path, approval_id = await _submitted_program_for_transition_test(client)
    reviewed = await client.post(path + "/review", headers=headers("well_manager"),
                                 json={"notes": "Review retained after failed transaction."})
    assert reviewed.status_code == 200
    original_audit = engineering_products._audit

    async def fail_audit(*args, **kwargs):
        if kwargs.get("action") == "artifact.withdraw":
            raise RuntimeError("injected audit storage failure")
        await original_audit(*args, **kwargs)

    monkeypatch.setattr(engineering_products, "_audit", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit storage failure"):
        await client.post(path + "/withdraw", headers=headers("drilling_supervisor"),
                          json={"reason": "Synthetic transaction rollback."})
    async with db.session() as session:
        current_artifact = (await session.execute(select(Artifact).where(Artifact.id == artifact["id"]))).scalar_one()
        current_version = (await session.execute(select(ArtifactVersion).where(ArtifactVersion.id == version["id"]))).scalar_one()
        approval = (await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))).scalar_one()
        assert current_artifact.status == current_version.state == "in_review"
        assert current_version.reviewed_by is not None
        assert approval.status == "pending"
        assert approval.request_payload["review"]["notes"] == "Review retained after failed transaction."
        assert "withdrawal" not in approval.request_payload
        audit = (await session.execute(select(AuditLog).where(
            AuditLog.org_id == artifact["org_id"], AuditLog.resource_id == version["id"],
            AuditLog.action == "artifact.withdraw",
        ))).scalars().all()
        assert audit == []
    monkeypatch.setattr(engineering_products, "_audit", original_audit)
    retried = await client.post(path + "/withdraw", headers=headers("drilling_supervisor"),
                                json={"reason": "Synthetic retry after rollback."})
    assert retried.status_code == 200, retried.text


async def test_withdrawal_preserves_review_committed_after_approval_read(client, db, monkeypatch):
    """Force a SQLite race at a real read boundary; do not replace persistence or transition code."""
    from contextvars import ContextVar

    from drillai.db.models import ApprovalRequest
    from drillai.engineering.service import EngineeringProductService

    _artifact, _version, path, approval_id = await _submitted_program_for_transition_test(client)
    original = EngineeringProductService._pending_approval
    approval_read = asyncio.Event()
    review_committed = asyncio.Event()
    is_withdrawal = ContextVar("is_withdrawal", default=False)

    async def interleaved(self, version_id, *, for_update=False):
        approval = await original(self, version_id, for_update=for_update)
        if is_withdrawal.get():
            approval_read.set()
            await asyncio.wait_for(review_committed.wait(), timeout=10)
        return approval

    monkeypatch.setattr(EngineeringProductService, "_pending_approval", interleaved)

    async def withdraw_request():
        token = is_withdrawal.set(True)
        try:
            return await client.post(
                path + "/withdraw", headers=headers("drilling_supervisor"), json={"reason": "Race withdrawal."},
            )
        finally:
            is_withdrawal.reset(token)

    withdrawal = asyncio.create_task(withdraw_request())
    try:
        await asyncio.wait_for(approval_read.wait(), timeout=10)
        reviewed = await client.post(path + "/review", headers=headers("well_manager"),
                                     json={"notes": "Review committed during withdrawal."})
        assert reviewed.status_code == 200, reviewed.text
    finally:
        review_committed.set()
    withdrawn = await withdrawal
    assert withdrawn.status_code == 409, withdrawn.text
    async with db.session() as session:
        approval = (await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))).scalar_one()
        assert approval.status == "pending"
        assert approval.request_payload["review"]["notes"] == "Review committed during withdrawal."
        assert "withdrawal" not in approval.request_payload
    retried = await client.post(path + "/withdraw", headers=headers("drilling_supervisor"),
                                json={"reason": "Retry after concurrent review."})
    assert retried.status_code == 200, retried.text
    async with db.session() as session:
        approval = (await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))).scalar_one()
        assert approval.status == "cancelled"
        assert approval.request_payload["review"]["notes"] == "Review committed during withdrawal."
        assert approval.request_payload["withdrawal"]["reason"] == "Retry after concurrent review."


@pytest.mark.postgres
async def test_postgres_review_then_competing_withdrawal_preserves_history(postgres_url, monkeypatch):
    """Exercise row locks with separate real HTTP transactions on PostgreSQL."""
    from contextvars import ContextVar

    import httpx

    from drillai.api.app import _warm_registries, create_app
    from drillai.core.config import Settings
    from drillai.db.models import ApprovalRequest
    from drillai.engineering.service import EngineeringProductService

    settings = Settings(_env_file=None, environment="test", auth_enabled=False,
                        database_url=postgres_url, blob_backend="memory", scheduler_enabled=False)
    app = create_app(settings)
    app.state.catalogues = _warm_registries()
    await app.state.database.create_all()
    reading = asyncio.Event()
    withdraw_attempted = asyncio.Event()
    release_review = asyncio.Event()
    is_review = ContextVar("pg_review", default=False)
    is_withdraw = ContextVar("pg_withdraw", default=False)
    original_pending = EngineeringProductService._pending_approval
    original_get = EngineeringProductService.get

    async def hold_review(self, version_id, *, for_update=False):
        approval = await original_pending(self, version_id, for_update=for_update)
        if is_review.get():
            reading.set()
            await asyncio.wait_for(release_review.wait(), timeout=15)
        return approval

    async def observe_withdraw(self, artifact_id, *, for_update=False):
        if is_withdraw.get():
            withdraw_attempted.set()
        return await original_get(self, artifact_id, for_update=for_update)

    async def scoped_post(http, context, url, **kwargs):
        token = context.set(True)
        try:
            return await http.post(url, **kwargs)
        finally:
            context.reset(token)

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            _artifact, _version, path, approval_id = await _submitted_program_for_transition_test(http)
            monkeypatch.setattr(EngineeringProductService, "_pending_approval", hold_review)
            monkeypatch.setattr(EngineeringProductService, "get", observe_withdraw)
            review_task = asyncio.create_task(scoped_post(
                http, is_review, path + "/review", headers=headers("well_manager"),
                json={"notes": "PostgreSQL concurrent review."},
            ))
            withdraw_task = None
            try:
                await asyncio.wait_for(reading.wait(), timeout=15)
                withdraw_task = asyncio.create_task(scoped_post(
                    http, is_withdraw, path + "/withdraw", headers=headers("drilling_supervisor"),
                    json={"reason": "PostgreSQL concurrent withdrawal."},
                ))
                await asyncio.wait_for(withdraw_attempted.wait(), timeout=15)
            finally:
                release_review.set()
            review_response = await review_task
            assert review_response.status_code == 200, review_response.text
            assert withdraw_task is not None
            withdrawal = await withdraw_task
            assert withdrawal.status_code == 200, withdrawal.text
            async with app.state.database.session() as session:
                approval = (await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))).scalar_one()
                assert approval.status == "cancelled"
                assert approval.request_payload["review"]["notes"] == "PostgreSQL concurrent review."
                assert approval.request_payload["withdrawal"]["reason"] == "PostgreSQL concurrent withdrawal."
    finally:
        release_review.set()
        await app.state.database.dispose()
