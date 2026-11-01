"""End-to-end API contract for governed daily report revisions and review separation."""

from __future__ import annotations

import asyncio
import copy

import pytest
from tests.api.conftest import headers


async def _create_well(client):
    manager = headers("well_manager")
    project_response = await client.post("/api/v1/projects", json={"name": "DDR test project"}, headers=manager)
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["id"]
    well_response = await client.post(
        "/api/v1/wells",
        json={
            "project_id": project_id,
            "name": "DDR test well",
            "well_type": "development_producer",
        },
        headers=manager,
    )
    assert well_response.status_code == 201, well_response.text
    well_id = well_response.json()["id"]
    bore_response = await client.post(
        f"/api/v1/wells/{well_id}/wellbores",
        json={"name": "Main bore", "purpose": "original", "planned_td_md_si": 3200.0},
        headers=manager,
    )
    assert bore_response.status_code == 201, bore_response.text
    return well_id, bore_response.json()["id"]


def _report_payload(wellbore_id: str) -> dict:
    return {
        "wellbore_id": wellbore_id,
        "report_date": "2026-03-15",
        "report_timezone": "America/Denver",
        "shift_code": "day",
        "report_number": "DDR-007",
        "content": {
            "period_start_at": "2026-03-15T06:00:00-06:00",
            "period_end_at": "2026-03-16T06:00:00-06:00",
            "summary": "Drilled ahead and confirmed the morning mud check.",
            "operations_summary": "Drilled 150 m; no event time was inferred from the report period.",
            "activities": [
                {
                    "activity_kind": "operation",
                    "operation_kind": "drilling",
                    "title": "Drilling ahead",
                    "start_at": "2026-03-15T07:00:00-06:00",
                    "start_time_basis": "reported",
                    "duration_value": 8.5,
                    "duration_unit": "h",
                    "duration_basis": "reported",
                    "depth_from_value": 2300.0,
                    "depth_from_unit": "m",
                    "depth_to_value": 2450.0,
                    "depth_to_unit": "m",
                    "depth_reference": "md",
                    "depth_basis": "reported",
                    "cause_basis": "recorded",
                }
            ],
            "measurements": [
                {
                    "parameter_key": "mud_weight",
                    "value": 9.2,
                    "unit": "ppg",
                    "value_basis": "reported",
                    "observed_at": "2026-03-15T11:30:00-06:00",
                    "time_basis": "reported",
                    "depth_value": 2412.0,
                    "depth_unit": "ft",
                    "depth_reference": "md",
                    "depth_basis": "reported",
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_daily_report_manual_revision_submit_approve_and_correct(client, db):
    well_id, wellbore_id = await _create_well(client)
    payload = _report_payload(wellbore_id)
    create_headers = {**headers("engineer"), "Idempotency-Key": "daily-report-create-1"}
    created = await client.post(f"/api/v1/wells/{well_id}/daily-reports", json=payload, headers=create_headers)
    assert created.status_code == 201, created.text
    first = created.json()
    report_id = first["report"]["id"]
    assert first["report"]["report_date"] == "2026-03-15"
    assert first["report"]["report_timezone"] == "America/Denver"
    assert first["report"]["latest_revision_number"] == 1
    assert first["revision"]["status"] == "draft"
    assert first["revision"]["content_hash_verified"] is True
    assert first["revision"]["activities"][0]["start_time_basis"] == "reported"
    assert first["revision"]["activities"][0]["duration_seconds"] == pytest.approx(8.5 * 3600)
    measurement = first["revision"]["measurements"][0]
    assert measurement["source_value"] == 9.2
    assert measurement["source_unit"] == "ppg"
    assert measurement["accepted_input_value"] == 9.2
    assert measurement["accepted_input_unit"] == "ppg"
    assert measurement["canonical_unit"] == "kg/m3"
    assert measurement["value_si"] == pytest.approx(9.2 * 119.82642731689663)
    assert measurement["depth_reference"] == "md"
    assert measurement["depth_basis"] == "reported"
    assert measurement["time_basis"] == "reported"

    # A retry of the exact request returns the response instead of creating a duplicate identity.
    replay = await client.post(f"/api/v1/wells/{well_id}/daily-reports", json=payload, headers=create_headers)
    assert replay.status_code == 201, replay.text
    assert replay.json() == first
    reused_key = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json={**payload, "report_number": "DDR-DIFFERENT-PAYLOAD"},
        headers=create_headers,
    )
    assert reused_key.status_code == 409
    assert reused_key.json()["error"]["code"] == "platform.conflict"

    collision = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=payload,
        headers=headers("engineer"),
    )
    assert collision.status_code == 409, collision.text
    assert collision.json()["error"]["code"] == "platform.conflict"

    read = await client.get(f"/api/v1/wells/{well_id}/daily-reports/{report_id}", headers=headers("engineer"))
    assert read.status_code == 200, read.text
    assert read.json()["latest_revision"]["content_hash_verified"] is True
    draft_list = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports?status=draft",
        headers=headers("viewer"),
    )
    assert draft_list.status_code == 200, draft_list.text
    assert draft_list.json()["total"] == 1
    assert draft_list.json()["items"][0]["id"] == report_id
    parameter_catalogue = await client.get("/api/v1/daily-report-parameters", headers=headers("viewer"))
    assert parameter_catalogue.status_code == 200, parameter_catalogue.text
    catalogue = {item["key"]: item for item in parameter_catalogue.json()["parameters"]}
    assert set(catalogue) >= {"depth_md", "mud_weight"}
    assert "ppg" in catalogue["mud_weight"]["accepted_units"]
    assert "kg/m3" in catalogue["mud_weight"]["accepted_units"]

    edited_payload = copy.deepcopy(payload["content"])
    edited_payload["summary"] = "Reviewed manually; the entered source values remain visible."
    edit_body = {
        "expected_edit_version": 1,
        "reason": "Correct the narrative before submission.",
        "content": edited_payload,
    }
    edit_headers = {**headers("engineer"), "Idempotency-Key": "daily-report-edit-1"}
    edited = await client.patch(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1",
        json=edit_body,
        headers=edit_headers,
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"]["edit_version"] == 2
    assert edited.json()["revision"]["summary"] == edited_payload["summary"]
    assert edited.json()["revision"]["content_hash_verified"] is True
    replayed_edit = await client.patch(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1",
        json=edit_body,
        headers=edit_headers,
    )
    assert replayed_edit.json() == edited.json()

    stale = await client.patch(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1",
        json={**edit_body, "reason": "A stale writer must not overwrite the report."},
        headers=headers("engineer"),
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["details"]["current_edit_version"] == 2

    transition_body = {"expected_edit_version": 2, "reason": "Shift lead reviewed the draft."}
    submitted = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/submit",
        json=transition_body,
        headers=headers("engineer"),
    )
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["revision"]["status"] == "submitted"
    assert submitted.json()["revision"]["edit_version"] == 3

    # The author cannot decide their own revision; an engineer also has no L4 review permission.
    denied = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/approve",
        json={"expected_edit_version": 3, "reason": "Not authorized."},
        headers=headers("engineer"),
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "security.permission_denied"

    approved = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/approve",
        json={"expected_edit_version": 3, "reason": "Independent review complete."},
        headers=headers("drilling_supervisor"),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["revision"]["status"] == "approved"
    assert approved.json()["revision"]["decided_by"] != approved.json()["revision"]["submitted_by"]
    assert approved.json()["report"]["latest_approved_revision_number"] == 1

    history = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/history", headers=headers("viewer")
    )
    assert history.status_code == 200, history.text
    assert history.json()["total"] == 1
    assert history.json()["items"][0]["status"] == "approved"

    correction = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/corrections",
        json={"reason": "Create a separately numbered correction."},
        headers=headers("drilling_supervisor"),
    )
    assert correction.status_code == 201, correction.text
    assert correction.json()["report"]["latest_revision_number"] == 2
    assert correction.json()["revision"]["revision_number"] == 2
    assert correction.json()["revision"]["status"] == "draft"
    assert correction.json()["revision"]["supersedes_revision_id"] == first["revision"]["id"]
    assert correction.json()["revision"]["activities"][0]["title"] == "Drilling ahead"

    # The approved historical revision remains readable and unchanged after a correction is opened.
    old_revision = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1", headers=headers("viewer")
    )
    assert old_revision.status_code == 200, old_revision.text
    assert old_revision.json()["revision"]["status"] == "approved"
    assert old_revision.json()["revision"]["summary"] == edited_payload["summary"]

    denied_write = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json={**payload, "report_number": "DDR-008"},
        headers=headers("viewer"),
    )
    assert denied_write.status_code == 403

    from sqlalchemy import select

    from drillai.db.models import AuditLog, ChangeRecord, OutboxEvent

    async with db.session_factory() as session:
        audit_rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.resource_kind == "daily_drilling_report",
                    AuditLog.resource_id == report_id,
                )
            )
        ).scalars().all()
        actions = {row.action for row in audit_rows}
        assert {
            "daily_report.create",
            "daily_report.edit",
            "daily_report.submit",
            "daily_report.approve",
            "daily_report.revise",
        } <= actions
        approval = next(row for row in audit_rows if row.action == "daily_report.approve")
        assert approval.action_level == "L4"
        assert approval.permission_decision["required"] == "daily_report.approve"
        changes = (
            await session.execute(
                select(ChangeRecord).where(
                    ChangeRecord.subject_kind == "daily_drilling_report",
                    ChangeRecord.subject_id == report_id,
                )
            )
        ).scalars().all()
        assert len(changes) >= 5
        outbox = (
            await session.execute(
                select(OutboxEvent).where(
                    OutboxEvent.event_type == "daily_report.changed",
                    OutboxEvent.subject_id == report_id,
                )
            )
        ).scalars().all()
        assert len(outbox) >= 5


@pytest.mark.asyncio
async def test_daily_report_reject_then_new_numbered_revision_and_validation(client, db):
    well_id, wellbore_id = await _create_well(client)
    payload = _report_payload(wellbore_id)
    created = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=payload,
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["report"]["id"]
    submitted = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/submit",
        json={"expected_edit_version": 1, "reason": "Ready for review."},
        headers=headers("engineer"),
    )
    assert submitted.status_code == 200, submitted.text

    rejected = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/reject",
        json={"expected_edit_version": 2, "reason": "Clarify the source for the daily mud check."},
        headers=headers("drilling_supervisor"),
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["revision"]["status"] == "rejected"
    assert rejected.json()["revision"]["decision_reason"] == "Clarify the source for the daily mud check."

    from sqlalchemy import select

    from drillai.db.models import AuditLog

    async with db.session_factory() as session:
        rejection_audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "daily_report.reject",
                    AuditLog.resource_id == report_id,
                )
            )
        ).scalar_one()
    assert rejection_audit.permission_decision["required"] == "daily_report.reject"

    correction = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/corrections",
        json={"reason": "Respond to review comments."},
        headers=headers("engineer"),
    )
    assert correction.status_code == 201, correction.text
    assert correction.json()["revision"]["revision_number"] == 2
    assert correction.json()["revision"]["status"] == "draft"

    bad_period = copy.deepcopy(payload)
    bad_period["report_number"] = "DDR-BAD-PERIOD"
    bad_period["content"]["period_start_at"] = "2026-03-16T06:00:00-06:00"
    bad_period["content"]["period_end_at"] = "2026-03-15T06:00:00-06:00"
    response = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=bad_period,
        headers=headers("engineer"),
    )
    assert response.status_code == 422

    naive_time = copy.deepcopy(payload)
    naive_time["report_number"] = "DDR-NAIVE-TIME"
    naive_time["content"]["activities"][0]["start_at"] = "2026-03-15T07:00:00"
    response = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=naive_time,
        headers=headers("engineer"),
    )
    assert response.status_code == 422

    invalid_zone = copy.deepcopy(payload)
    invalid_zone["report_number"] = "DDR-INVALID-ZONE"
    invalid_zone["report_timezone"] = "Mars/Orem"
    response = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=invalid_zone,
        headers=headers("engineer"),
    )
    assert response.status_code == 422

    wrong_unit = copy.deepcopy(payload)
    wrong_unit["report_number"] = "DDR-WRONG-UNIT"
    wrong_unit["content"]["measurements"][0]["unit"] = "m"
    response = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=wrong_unit,
        headers=headers("engineer"),
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_daily_report_author_cannot_approve_own_revision(client):
    well_id, wellbore_id = await _create_well(client)
    payload = _report_payload(wellbore_id)
    created = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=payload,
        headers=headers("drilling_supervisor"),
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["report"]["id"]
    submitted = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/submit",
        json={"expected_edit_version": 1, "reason": "Submit for independent review."},
        headers=headers("drilling_supervisor"),
    )
    assert submitted.status_code == 200, submitted.text

    self_decision = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/approve",
        json={"expected_edit_version": 2, "reason": "The author cannot approve their own submission."},
        headers=headers("drilling_supervisor"),
    )
    assert self_decision.status_code == 403, self_decision.text
    assert "submitter" in self_decision.json()["error"]["message"]

    independent = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/approve",
        json={"expected_edit_version": 2, "reason": "Independent supervisor review."},
        headers=headers("well_manager"),
    )
    assert independent.status_code == 200, independent.text
    assert independent.json()["revision"]["submitted_by"] != independent.json()["revision"]["decided_by"]


@pytest.mark.asyncio
async def test_daily_report_never_infers_activity_time_from_period_end(client):
    well_id, wellbore_id = await _create_well(client)
    response = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json={
            "wellbore_id": wellbore_id,
            "report_date": "2026-05-04",
            "report_timezone": "America/Denver",
            "shift_code": "night",
            "content": {
                "period_end_at": "2026-05-05T06:00:00-06:00",
                "activities": [
                    {"activity_kind": "other", "title": "Waiting on the next instruction."}
                ],
            },
        },
        headers=headers("engineer"),
    )
    assert response.status_code == 201, response.text
    activity = response.json()["revision"]["activities"][0]
    assert activity["start_at"] is None
    assert activity["end_at"] is None
    assert activity["start_time_basis"] == "unknown"
    assert activity["end_time_basis"] == "unknown"


@pytest.mark.asyncio
async def test_daily_report_concurrent_draft_updates_have_one_winner(client):
    well_id, wellbore_id = await _create_well(client)
    created = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=_report_payload(wellbore_id),
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["report"]["id"]
    content_a = copy.deepcopy(_report_payload(wellbore_id)["content"])
    content_b = copy.deepcopy(content_a)
    content_a["summary"] = "Concurrent edit A."
    content_b["summary"] = "Concurrent edit B."

    async def update(content: dict, key: str):
        return await client.patch(
            f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1",
            json={
                "expected_edit_version": 1,
                "reason": f"Concurrent edit {key}.",
                "content": content,
            },
            headers={**headers("engineer"), "Idempotency-Key": f"daily-report-race-{key}"},
        )

    first, second = await asyncio.gather(update(content_a, "a"), update(content_b, "b"))
    assert sorted((first.status_code, second.status_code)) == [200, 409], (first.text, second.text)
    winner = first if first.status_code == 200 else second
    assert winner.json()["revision"]["edit_version"] == 2
    assert winner.json()["revision"]["content_hash_verified"] is True


@pytest.mark.asyncio
async def test_daily_report_withdraw_is_audited_and_can_be_corrected(client):
    well_id, wellbore_id = await _create_well(client)
    created = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=_report_payload(wellbore_id),
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["report"]["id"]
    withdrawn = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/withdraw",
        json={"expected_edit_version": 1, "reason": "The shift opened this draft in error."},
        headers=headers("engineer"),
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["revision"]["status"] == "withdrawn"
    corrected = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/corrections",
        json={"reason": "Create a fresh draft after withdrawal."},
        headers=headers("engineer"),
    )
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["revision"]["revision_number"] == 2
    assert corrected.json()["revision"]["supersedes_revision_id"] == created.json()["revision"]["id"]


@pytest.mark.asyncio
async def test_daily_report_failure_rolls_back_mutation_and_idempotency_key(client, app, monkeypatch):
    well_id, wellbore_id = await _create_well(client)
    payload = _report_payload(wellbore_id)
    payload["report_number"] = "DDR-AUDIT-ROLLBACK"
    retry_headers = {**headers("engineer"), "Idempotency-Key": "daily-report-audit-outage"}

    from drillai.drilling import daily_reports as daily_report_module

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("injected audit-store failure")

    import httpx

    with monkeypatch.context() as patch:
        patch.setattr(daily_report_module, "record_audit", fail_audit)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as failing_client:
            failed = await failing_client.post(
                f"/api/v1/wells/{well_id}/daily-reports",
                json=payload,
                headers=retry_headers,
            )
    assert failed.status_code == 500, failed.text
    assert "Traceback" not in failed.text
    assert "injected audit-store failure" not in failed.text

    # The report, revision and in-progress idempotency reservation shared a transaction; retrying
    # after the injected failure is allowed to create exactly one complete record.
    retried = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=payload,
        headers=retry_headers,
    )
    assert retried.status_code == 201, retried.text
    assert retried.json()["revision"]["status"] == "draft"
