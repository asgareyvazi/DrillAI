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


async def _create_approved_period(
    client,
    well_id: str,
    wellbore_id: str,
    *,
    report_date: str,
    report_number: str,
    period_start_at: str | None,
    period_end_at: str | None,
    shift_code: str = "day",
    approve: bool = True,
) -> tuple[str, str]:
    content = {"summary": "Synthetic API-test period record."}
    if period_start_at is not None:
        content["period_start_at"] = period_start_at
    if period_end_at is not None:
        content["period_end_at"] = period_end_at
    created = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json={
            "wellbore_id": wellbore_id,
            "report_date": report_date,
            "report_timezone": "UTC",
            "shift_code": shift_code,
            "report_number": report_number,
            "content": content,
        },
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["report"]["id"]
    revision_id = created.json()["revision"]["id"]
    if approve:
        submitted = await client.post(
            f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/submit",
            json={"expected_edit_version": 1, "reason": "Synthetic API-test submission."},
            headers=headers("engineer"),
        )
        assert submitted.status_code == 200, submitted.text
        approved = await client.post(
            f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/approve",
            json={"expected_edit_version": 2, "reason": "Synthetic API-test independent approval."},
            headers=headers("drilling_supervisor"),
        )
        assert approved.status_code == 200, approved.text
    return report_id, revision_id


async def _create_import_evidence(
    client,
    db,
    well_id: str,
    wellbore_id: str,
    *,
    record_payload: dict | None = None,
    record_type: str = "daily_summary",
) -> tuple[str, str]:
    from sqlalchemy import select

    from drillai.core.serialization import content_hash
    from drillai.db.models import Document, ExtractedRecord

    uploaded = await client.post(
        "/api/v1/documents",
        files={"file": ("import-source.txt", b"DAILY DRILLING REPORT\nSynthetic integration-test evidence.\n", "text/plain")},
        data={"well_id": well_id, "wellbore_id": wellbore_id, "doc_type": "ddr"},
        headers=headers("engineer"),
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["document"]["id"]
    async with db.session_factory() as session:
        document = (
            await session.execute(select(Document).where(Document.id == document_id))
        ).scalar_one()
        record_payload = record_payload if record_payload is not None else {
            "summary": "Extracted summary awaiting human review.",
            "operations_summary": "Extracted operations narrative awaiting human review.",
            "safety_notes": "Extracted safety narrative awaiting human review.",
            "next_period_plan": "Extracted next-period plan awaiting human review.",
            "environmental_notes": "Extracted environmental narrative awaiting human review.",
        }
        record = ExtractedRecord(
            org_id=document.org_id,
            document_id=document.id,
            project_id=document.project_id,
            well_id=document.well_id,
            wellbore_id=document.wellbore_id,
            record_type=record_type,
            payload_schema_key="test.daily_report_import",
            payload_schema_version=1,
            payload=record_payload,
            method="rule",
            validation_state="extracted",
            fingerprint=content_hash(record_payload),
        )
        session.add(record)
        await session.flush()
        record_id = record.id
        await session.commit()
    return document_id, record_id


@pytest.mark.asyncio
async def test_documentless_source_record_cannot_claim_a_document_for_report_evidence(client, db):
    from sqlalchemy import select

    from drillai.db.models import ExtractedRecord

    well_id, wellbore_id = await _create_well(client)
    document_id, record_id = await _create_import_evidence(client, db, well_id, wellbore_id)
    async with db.session_factory() as session:
        record = (
            await session.execute(select(ExtractedRecord).where(ExtractedRecord.id == record_id))
        ).scalar_one()
        record.document_id = None
        record.validation_state = "rule_validated"
        record.confidence = 0.99
        record.validation_rule = "synthetic-link-scope-test"
        await session.commit()

    payload = _report_payload(wellbore_id)
    payload["report_number"] = "DDR-DOCUMENTLESS-RECORD"
    payload["content"]["measurements"][0].update(
        {"source_document_id": document_id, "source_record_id": record_id}
    )
    response = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=payload,
        headers=headers("engineer"),
    )
    assert response.status_code == 422, response.text
    assert "source_record_id does not belong to source_document_id" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_report_header_identity_sources_require_human_reconciliation_and_fresh_evidence(client, db):
    from sqlalchemy import select

    from drillai.db.models import ExtractedRecord

    well_id, wellbore_id = await _create_well(client)

    async def create_header_import(
        report_date: str,
        report_number: str,
        *,
        source_report_date: str | None = None,
        source_report_number: str | None = None,
        record_type: str = "report_header",
        report_date_source_path: str = "payload.report_date",
        expected_status: int = 201,
    ) -> tuple[dict, str]:
        document_id, record_id = await _create_import_evidence(
            client,
            db,
            well_id,
            wellbore_id,
            record_type=record_type,
            record_payload={
                "well_name_text": "Synthetic report-header identity",
                "report_date": source_report_date or report_date,
                "report_number": source_report_number or report_number,
            },
        )
        imported = await client.post(
            f"/api/v1/wells/{well_id}/daily-reports/imports",
            json={
                "wellbore_id": wellbore_id,
                "report_date": report_date,
                "report_timezone": "UTC",
                "shift_code": "day",
                "report_number": report_number,
                "source_document_id": document_id,
                "source_record_id": record_id,
                "field_sources": [
                    {"field_key": "report_date", "source_record_id": record_id, "source_path": report_date_source_path},
                    {"field_key": "report_number", "source_record_id": record_id, "source_path": "payload.report_number"},
                ],
                "content": {
                    "source_document_id": document_id,
                    "source_record_id": record_id,
                    "summary": "Synthetic API-test report-header mapping.",
                },
            },
            headers=headers("engineer"),
        )
        assert imported.status_code == expected_status, imported.text
        return imported.json(), record_id

    wrong_path, _ = await create_header_import(
        "2026-11-04", "DDR-HEADER-PATH", report_date_source_path="payload.report_number", expected_status=422
    )
    assert "matching extracted header field" in wrong_path["error"]["message"]
    mismatched_identity, _ = await create_header_import(
        "2026-11-05", "DDR-HEADER-MISMATCH", source_report_date="2026-11-04", expected_status=422
    )
    assert "exactly match" in mismatched_identity["error"]["message"]
    incompatible_record, _ = await create_header_import(
        "2026-11-05", "DDR-HEADER-TYPE", record_type="daily_summary", expected_status=422
    )
    assert "not a compatible source" in incompatible_record["error"]["message"]

    imported, record_id = await create_header_import("2026-11-04", "DDR-HEADER-004")
    report_id = imported["report"]["id"]
    revision = imported["revision"]
    assert revision["status"] == "draft"
    assert revision["source_record_id"] == record_id
    assert revision["content_hash_verified"] is True
    identity_sources = [item for item in revision["reconciliation"] if item["target_kind"] == "report_identity"]
    assert {item["field_key"] for item in identity_sources} == {"report_date", "report_number"}
    assert all(item["source_record_id"] == record_id for item in identity_sources)
    assert all(item["proposal_matches_current"] is True for item in identity_sources)
    assert all(item["source_value_hash"] and item["proposed_value_hash"] for item in identity_sources)

    blocked_submit = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/submit",
        json={"expected_edit_version": 1, "reason": "Synthetic pre-reconciliation submission attempt."},
        headers=headers("engineer"),
    )
    assert blocked_submit.status_code == 422, blocked_submit.text
    assert blocked_submit.json()["error"]["details"]["target_count"] == 2

    accepted = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/reconcile",
        json={
            "expected_edit_version": 1,
            "reason": "Compared both immutable identity values with the cited report-header fields.",
            "decisions": [
                {"target_key": item["target_key"], "decision": "accepted"} for item in identity_sources
            ],
        },
        headers=headers("engineer"),
    )
    assert accepted.status_code == 200, accepted.text
    accepted_identity = [
        item for item in accepted.json()["revision"]["reconciliation"] if item["target_kind"] == "report_identity"
    ]
    assert {item["resolution_state"] for item in accepted_identity} == {"accepted"}
    assert accepted.json()["revision"]["content_hash_verified"] is True

    immutable_identity_correction = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/reconcile",
        json={
            "expected_edit_version": 2,
            "reason": "Synthetic attempt to change immutable report identity in place.",
            "decisions": [{"target_key": identity_sources[0]["target_key"], "decision": "corrected"}],
        },
        headers=headers("engineer"),
    )
    assert immutable_identity_correction.status_code == 422, immutable_identity_correction.text
    assert "identity is immutable" in immutable_identity_correction.json()["error"]["message"]

    submitted = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/submit",
        json={"expected_edit_version": 2, "reason": "Submit after source identity reconciliation."},
        headers=headers("engineer"),
    )
    assert submitted.status_code == 200, submitted.text

    rejected_import, _ = await create_header_import("2026-11-05", "DDR-HEADER-005")
    rejected_rows = [
        item for item in rejected_import["revision"]["reconciliation"] if item["target_kind"] == "report_identity"
    ]
    rejected_decision = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{rejected_import['report']['id']}/revisions/1/reconcile",
        json={
            "expected_edit_version": 1,
            "reason": "The synthetic identity proposals were reviewed and rejected.",
            "decisions": [{"target_key": item["target_key"], "decision": "rejected"} for item in rejected_rows],
        },
        headers=headers("engineer"),
    )
    assert rejected_decision.status_code == 200, rejected_decision.text
    assert {
        item["resolution_state"]
        for item in rejected_decision.json()["revision"]["reconciliation"]
        if item["target_kind"] == "report_identity"
    } == {"rejected"}
    rejected_submission = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{rejected_import['report']['id']}/revisions/1/submit",
        json={"expected_edit_version": 2, "reason": "Submit after explicit rejection of source identity proposals."},
        headers=headers("engineer"),
    )
    assert rejected_submission.status_code == 200, rejected_submission.text

    stale_import, stale_record_id = await create_header_import("2026-11-06", "DDR-HEADER-006")
    stale_report_id = stale_import["report"]["id"]
    async with db.session_factory() as session:
        source = (
            await session.execute(select(ExtractedRecord).where(ExtractedRecord.id == stale_record_id))
        ).scalar_one()
        source.review_version += 1
        await session.commit()

    stale_decision = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{stale_report_id}/revisions/1/reconcile",
        json={
            "expected_edit_version": 1,
            "reason": "Attempt to reconcile a header after its source review version changed.",
            "decisions": [
                {"target_key": item["target_key"], "decision": "accepted"}
                for item in stale_import["revision"]["reconciliation"]
                if item["target_kind"] == "report_identity"
            ],
        },
        headers=headers("engineer"),
    )
    assert stale_decision.status_code == 409, stale_decision.text
    assert "source evidence changed after import" in stale_decision.json()["error"]["message"]


@pytest.mark.asyncio
async def test_daily_report_activity_metrics_exclude_unknown_and_planned_duration(client, db):
    well_id, wellbore_id = await _create_well(client)
    payload = _report_payload(wellbore_id)
    payload["report_number"] = "DDR-METRICS-001"
    payload["content"]["activities"] = [
        {
            "activity_kind": "npt",
            "npt_category": "stuck_pipe",
            "title": "Reported stuck pipe",
            "duration_value": 2.0,
            "duration_unit": "h",
            "duration_basis": "reported",
        },
        {
            "activity_kind": "npt",
            "npt_category": "unclassified",
            "title": "Planned NPT allowance",
            "duration_value": 3.0,
            "duration_unit": "h",
            "duration_basis": "planned",
        },
        {
            "activity_kind": "npt",
            "npt_category": "unclassified",
            "title": "NPT duration not provided",
        },
        {
            "activity_kind": "operation",
            "operation_kind": "drilling",
            "title": "Drilling ahead",
            "duration_value": 8.5,
            "duration_unit": "h",
            "duration_basis": "reported",
        },
    ]
    created = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=payload,
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["report"]["id"]
    metrics = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/activity-metrics",
        headers=headers("viewer"),
    )
    assert metrics.status_code == 200, metrics.text
    result = metrics.json()
    assert result["revision_content_hash"] == created.json()["revision"]["content_hash"]
    assert result["revision_status"] == "draft"
    assert result["activity_count"] == 4
    assert result["npt_activity_count"] == 3
    assert result["npt_duration_included_count"] == 1
    assert result["npt_duration_excluded_count"] == 2
    assert result["npt_duration_excluded_by_basis"] == {"planned": 1, "inferred": 0, "unknown": 1}
    assert result["npt_duration_total"] == {"value": 7200.0, "unit": "s"}
    assert result["npt_duration_coverage"] == "partial"
    assert "not a measurement of physical downtime" in result["note"]

    no_known_duration = _report_payload(wellbore_id)
    no_known_duration["report_number"] = "DDR-METRICS-002"
    no_known_duration["content"]["activities"] = [payload["content"]["activities"][1]]
    second = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=no_known_duration,
        headers=headers("engineer"),
    )
    assert second.status_code == 201, second.text
    unavailable = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports/{second.json()['report']['id']}/revisions/1/activity-metrics",
        headers=headers("viewer"),
    )
    assert unavailable.status_code == 200, unavailable.text
    assert unavailable.json()["npt_duration_total"] == {"value": None, "unit": "s"}
    assert unavailable.json()["npt_duration_coverage"] == "unavailable"

    other_well_id, _ = await _create_well(client)
    out_of_scope = await client.get(
        f"/api/v1/wells/{other_well_id}/daily-reports/{report_id}/revisions/1/activity-metrics",
        headers=headers("viewer"),
    )
    assert out_of_scope.status_code == 404, out_of_scope.text

    from sqlalchemy import select

    from drillai.db.models import DailyDrillingReportRevision

    async with db.session_factory() as session:
        stored_revision = (
            await session.execute(
                select(DailyDrillingReportRevision).where(
                    DailyDrillingReportRevision.id == created.json()["revision"]["id"]
                )
            )
        ).scalar_one()
        stored_revision.content_hash = "0" * 64
        await session.commit()
    tampered = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/activity-metrics",
        headers=headers("viewer"),
    )
    assert tampered.status_code == 409, tampered.text
    assert "verified revision fingerprint" in tampered.json()["error"]["message"]


@pytest.mark.asyncio
async def test_daily_report_period_integrity_is_revision_bound_and_does_not_bridge_incomplete_periods(client, db):
    from sqlalchemy import select

    from drillai.db.models import DailyDrillingReportRevision

    well_id, wellbore_id = await _create_well(client)
    identities = [
        ("2026-04-01", "DDR-PERIOD-01", "2026-04-01T06:00:00Z", "2026-04-01T12:00:00Z", True),
        ("2026-04-02", "DDR-PERIOD-02", "2026-04-02T12:00:00Z", None, True),
        ("2026-04-03", "DDR-PERIOD-03", "2026-04-03T12:00:00Z", "2026-04-04T18:00:00Z", True),
        ("2026-04-04", "DDR-PERIOD-04", "2026-04-04T19:00:00Z", "2026-04-05T22:00:00Z", True),
        ("2026-04-05", "DDR-PERIOD-05", "2026-04-05T22:00:00Z", "2026-04-06T23:00:00Z", True),
        ("2026-04-06", "DDR-PERIOD-06", "2026-04-06T22:00:00Z", "2026-04-07T01:00:00Z", True),
        ("2026-04-07", "DDR-PERIOD-07", "2026-04-07T01:00:00Z", "2026-04-07T03:00:00Z", False),
    ]
    report_ids: dict[str, tuple[str, str]] = {}
    for report_date, report_number, start_at, end_at, approve in identities:
        report_ids[report_number] = await _create_approved_period(
            client,
            well_id,
            wellbore_id,
            report_date=report_date,
            report_number=report_number,
            period_start_at=start_at,
            period_end_at=end_at,
            approve=approve,
        )

    response = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports/period-integrity?wellbore_id={wellbore_id}&limit=20",
        headers=headers("viewer"),
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["total"] == 7
    assert result["truncated"] is False
    assert result["bounded_period_count"] == 5, [
        (item["report_number"], item["revision_status"], item["period_state"], item["revision_fingerprint_verified"])
        for item in result["items"]
    ]
    assert result["unassessed_period_count"] == 2
    assert result["unassessed_comparison_count"] == 3
    assert result["comparison_method"] == "approved_explicit_periods_by_report_date_and_start_v2"
    assert "are never skipped to bridge intervals" in result["note"]
    states = {item["report_number"]: item for item in result["items"]}
    assert states["DDR-PERIOD-02"]["period_state"] == "missing_end_bound"
    assert states["DDR-PERIOD-02"]["period_end_at"] is None
    assert states["DDR-PERIOD-06"]["revision_status"] == "approved"
    assert states["DDR-PERIOD-07"]["period_state"] == "no_approved_revision"
    assert all(
        item["revision_fingerprint_verified"] is True
        for item in result["items"]
        if item["revision_status"] == "approved"
    )
    relationships = {
        (item["previous_report_number"], item["next_report_number"]): item
        for item in result["comparisons"]
    }
    assert set(relationships) == {
        ("DDR-PERIOD-03", "DDR-PERIOD-04"),
        ("DDR-PERIOD-04", "DDR-PERIOD-05"),
        ("DDR-PERIOD-05", "DDR-PERIOD-06"),
    }
    assert relationships[("DDR-PERIOD-03", "DDR-PERIOD-04")]["relationship"] == "gap"
    assert relationships[("DDR-PERIOD-03", "DDR-PERIOD-04")]["gap_seconds"] == 3600.0
    assert relationships[("DDR-PERIOD-04", "DDR-PERIOD-05")]["relationship"] == "contiguous"
    assert relationships[("DDR-PERIOD-04", "DDR-PERIOD-05")]["gap_seconds"] is None
    assert relationships[("DDR-PERIOD-05", "DDR-PERIOD-06")]["relationship"] == "overlap"
    assert relationships[("DDR-PERIOD-05", "DDR-PERIOD-06")]["overlap_seconds"] == 3600.0
    assert {item["unit"] for item in result["comparisons"]} == {"s"}

    bounded_window = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports/period-integrity"
        f"?report_date_from=2026-04-01&report_date_to=2026-04-03&limit=20",
        headers=headers("viewer"),
    )
    assert bounded_window.status_code == 200, bounded_window.text
    assert bounded_window.json()["total"] == 3
    assert bounded_window.json()["comparisons"] == []
    assert bounded_window.json()["unassessed_comparison_count"] == 2

    recent = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports/period-integrity?wellbore_id={wellbore_id}&limit=3",
        headers=headers("viewer"),
    )
    assert recent.status_code == 200, recent.text
    assert recent.json()["truncated"] is True
    assert len(recent.json()["items"]) == 3
    assert recent.json()["comparisons"] == []

    async with db.session_factory() as session:
        tampered_revision = (
            await session.execute(
                select(DailyDrillingReportRevision).where(
                    DailyDrillingReportRevision.id == report_ids["DDR-PERIOD-04"][1]
                )
            )
        ).scalar_one()
        tampered_revision.content_hash = "0" * 64
        await session.commit()
    tampered_response = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports/period-integrity?limit=20",
        headers=headers("viewer"),
    )
    assert tampered_response.status_code == 200, tampered_response.text
    tampered_result = tampered_response.json()
    tampered_item = next(
        item for item in tampered_result["items"] if item["report_number"] == "DDR-PERIOD-04"
    )
    assert tampered_item["period_state"] == "fingerprint_mismatch"
    assert tampered_item["revision_fingerprint_verified"] is False
    assert tampered_item["period_start_at"] is None
    assert tampered_item["period_end_at"] is None
    assert {
        (item["previous_report_number"], item["next_report_number"])
        for item in tampered_result["comparisons"]
    } == {("DDR-PERIOD-05", "DDR-PERIOD-06")}
    assert tampered_result["unassessed_comparison_count"] == 5

    other_well_id, _ = await _create_well(client)
    out_of_scope = await client.get(
        f"/api/v1/wells/{other_well_id}/daily-reports/period-integrity?wellbore_id={wellbore_id}",
        headers=headers("viewer"),
    )
    assert out_of_scope.status_code == 422, out_of_scope.text

    shift_well_id, shift_wellbore_id = await _create_well(client)
    await _create_approved_period(
        client,
        shift_well_id,
        shift_wellbore_id,
        report_date="2026-05-01",
        report_number="DDR-SHIFT-EARLY",
        period_start_at="2026-05-01T06:00:00Z",
        period_end_at="2026-05-01T12:00:00Z",
        shift_code="day",
    )
    await _create_approved_period(
        client,
        shift_well_id,
        shift_wellbore_id,
        report_date="2026-05-01",
        report_number="DDR-SHIFT-LATE",
        period_start_at="2026-05-01T12:30:00Z",
        period_end_at="2026-05-01T20:00:00Z",
        shift_code="night",
    )
    await _create_approved_period(
        client,
        shift_well_id,
        shift_wellbore_id,
        report_date="2026-05-02",
        report_number="DDR-SHIFT-NEXT-DATE",
        period_start_at="2026-05-02T00:00:00Z",
        period_end_at="2026-05-02T06:00:00Z",
    )
    await _create_approved_period(
        client,
        shift_well_id,
        shift_wellbore_id,
        report_date="2026-05-03",
        report_number="DDR-SHIFT-UNAPPROVED",
        period_start_at="2026-05-03T06:00:00Z",
        period_end_at="2026-05-03T18:00:00Z",
        approve=False,
    )
    await _create_approved_period(
        client,
        shift_well_id,
        shift_wellbore_id,
        report_date="2026-05-04",
        report_number="DDR-SHIFT-AFTER-UNAPPROVED",
        period_start_at="2026-05-04T06:00:00Z",
        period_end_at="2026-05-04T18:00:00Z",
    )
    for shift_code, report_number, end_at in (
        ("day", "DDR-SHIFT-AMBIGUOUS-A", "2026-05-05T12:00:00Z"),
        ("night", "DDR-SHIFT-AMBIGUOUS-B", "2026-05-05T18:00:00Z"),
    ):
        await _create_approved_period(
            client,
            shift_well_id,
            shift_wellbore_id,
            report_date="2026-05-05",
            report_number=report_number,
            period_start_at="2026-05-05T06:00:00Z",
            period_end_at=end_at,
            shift_code=shift_code,
        )
    await _create_approved_period(
        client,
        shift_well_id,
        shift_wellbore_id,
        report_date="2026-05-06",
        report_number="DDR-SHIFT-ORDER-CONFLICT-A",
        period_start_at="2026-05-06T18:00:00Z",
        period_end_at="2026-05-07T01:00:00Z",
    )
    await _create_approved_period(
        client,
        shift_well_id,
        shift_wellbore_id,
        report_date="2026-05-07",
        report_number="DDR-SHIFT-ORDER-CONFLICT-B",
        period_start_at="2026-05-06T12:00:00Z",
        period_end_at="2026-05-07T00:00:00Z",
    )

    same_date_response = await client.get(
        f"/api/v1/wells/{shift_well_id}/daily-reports/period-integrity"
        f"?wellbore_id={shift_wellbore_id}&limit=20",
        headers=headers("viewer"),
    )
    assert same_date_response.status_code == 200, same_date_response.text
    same_date_result = same_date_response.json()
    assert same_date_result["total"] == 9
    assert same_date_result["bounded_period_count"] == 8
    assert same_date_result["unassessed_period_count"] == 1
    assert same_date_result["unassessed_comparison_count"] == 6
    same_date_comparisons = {
        (item["previous_report_number"], item["next_report_number"]): item
        for item in same_date_result["comparisons"]
    }
    assert [
        (item["previous_report_number"], item["next_report_number"])
        for item in same_date_result["comparisons"]
    ] == [
        ("DDR-SHIFT-EARLY", "DDR-SHIFT-LATE"),
        ("DDR-SHIFT-LATE", "DDR-SHIFT-NEXT-DATE"),
    ]
    assert set(same_date_comparisons) == {
        ("DDR-SHIFT-EARLY", "DDR-SHIFT-LATE"),
        ("DDR-SHIFT-LATE", "DDR-SHIFT-NEXT-DATE"),
    }
    assert not any("ORDER-CONFLICT" in item["previous_report_number"] or "ORDER-CONFLICT" in item["next_report_number"] for item in same_date_result["comparisons"])
    same_day_pair = same_date_comparisons[("DDR-SHIFT-EARLY", "DDR-SHIFT-LATE")]
    assert same_day_pair["previous_report_date"] == same_day_pair["next_report_date"] == "2026-05-01"
    assert same_day_pair["ordering_basis"] == "explicit_period_start_at_within_report_date_v1"
    assert same_day_pair["relationship"] == "gap"
    assert same_day_pair["gap_seconds"] == 1800.0
    cross_date_pair = same_date_comparisons[("DDR-SHIFT-LATE", "DDR-SHIFT-NEXT-DATE")]
    assert cross_date_pair["ordering_basis"] == "consecutive_report_dates_with_explicit_start_order_v1"
    assert cross_date_pair["gap_seconds"] == 14400.0
    truncated_same_date = await client.get(
        f"/api/v1/wells/{shift_well_id}/daily-reports/period-integrity"
        "?report_date_from=2026-05-04&report_date_to=2026-05-05&limit=2",
        headers=headers("viewer"),
    )
    assert truncated_same_date.status_code == 200, truncated_same_date.text
    assert truncated_same_date.json()["truncated"] is True
    assert truncated_same_date.json()["comparisons"] == []
    assert truncated_same_date.json()["unassessed_comparison_count"] == 1


@pytest.mark.asyncio
async def test_daily_report_activity_classification_preview_is_read_only_and_scope_checked(client, db):
    well_id, wellbore_id = await _create_well(client)
    source_text = "Lost circulation was reported while the crew prepared LCM for the source report."
    document_id, record_id = await _create_import_evidence(
        client,
        db,
        well_id,
        wellbore_id,
        record_payload={"activity": source_text},
    )

    preview = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/activity-classification-preview",
        json={
            "wellbore_id": wellbore_id,
            "source_document_id": document_id,
            "source_record_id": record_id,
        },
        headers=headers("viewer"),
    )
    assert preview.status_code == 200, preview.text
    suggestion = preview.json()
    assert suggestion["method"] == "deterministic_content_rules"
    assert suggestion["source_document_id"] == document_id
    assert suggestion["source_record_id"] == record_id
    assert suggestion["source_region_id"] is None
    assert suggestion["source_page_number"] is None
    assert suggestion["source_validation_state"] == "extracted"
    assert suggestion["source_review_version"] == 1
    assert suggestion["source_fingerprint"]
    assert suggestion["source_extraction_method"] == "rule"
    assert suggestion["source_text_truncated"] is False
    assert suggestion["operation"]["kind"] == "circulating"
    assert suggestion["operation"]["matched_text"] == "Lost circulation"
    assert suggestion["npt"]["category"] == "lost_circulation"
    assert suggestion["npt"]["code"] == "LOST_CIRC"
    assert suggestion["npt"]["matched_text"] == "Lost circulation"
    assert "not calibrated accuracy estimates" in suggestion["notice"]

    source_detail = await client.get(f"/api/v1/documents/{document_id}", headers=headers("viewer"))
    assert source_detail.status_code == 200, source_detail.text
    unchanged = next(row for row in source_detail.json()["records"] if row["id"] == record_id)
    assert unchanged["validation_state"] == "extracted"
    assert unchanged["payload"] == {"activity": source_text}

    long_document_id, long_record_id = await _create_import_evidence(
        client,
        db,
        well_id,
        wellbore_id,
        record_payload={"activity": "Drilling ahead. " * 2500},
    )
    bounded_preview = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/activity-classification-preview",
        json={
            "wellbore_id": wellbore_id,
            "source_document_id": long_document_id,
            "source_record_id": long_record_id,
        },
        headers=headers("engineer"),
    )
    assert bounded_preview.status_code == 200, bounded_preview.text
    assert bounded_preview.json()["source_text_truncated"] is True
    assert bounded_preview.json()["operation"]["kind"] == "drilling"

    operation_response = await client.post(
        "/api/v1/operations",
        json={
            "well_id": well_id,
            "wellbore_id": wellbore_id,
            "name": "Circulation response",
            "operation_class": "actual",
            "kind": "circulating",
            "status": "in_progress",
        },
        headers=headers("engineer"),
    )
    assert operation_response.status_code == 201, operation_response.text
    operation = operation_response.json()
    event_response = await client.post(
        "/api/v1/events",
        json={
            "well_id": well_id,
            "wellbore_id": wellbore_id,
            "operation_id": operation["id"],
            "title": "Circulation activity recorded",
            "kind": "observation",
        },
        headers=headers("engineer"),
    )
    assert event_response.status_code == 201, event_response.text
    event = event_response.json()
    linked_import = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/imports",
        json={
            "wellbore_id": wellbore_id,
            "report_date": "2026-11-04",
            "report_timezone": "UTC",
            "shift_code": "day",
            "report_number": "DDR-LINKED-ACTIVITY-001",
            "source_document_id": document_id,
            "source_record_id": record_id,
            "content": {
                "source_document_id": document_id,
                "source_record_id": record_id,
                "activities": [{
                    "activity_kind": "operation",
                    "operation_kind": "circulating",
                    "title": "Circulation response from source report",
                    "source_document_id": document_id,
                    "source_record_id": record_id,
                    "operation_id": operation["id"],
                    "event_id": event["id"],
                }],
            },
        },
        headers=headers("engineer"),
    )
    assert linked_import.status_code == 201, linked_import.text
    linked_activity = linked_import.json()["revision"]["activities"][0]
    assert linked_activity["operation_id"] == operation["id"]
    assert linked_activity["event_id"] == event["id"]
    assert linked_activity["resolution_state"] == "proposed"

    other_well_id, other_wellbore_id = await _create_well(client)
    cross_scope = await client.post(
        f"/api/v1/wells/{other_well_id}/daily-reports/activity-classification-preview",
        json={
            "wellbore_id": other_wellbore_id,
            "source_document_id": document_id,
            "source_record_id": record_id,
        },
        headers=headers("engineer"),
    )
    assert cross_scope.status_code == 422, cross_scope.text


@pytest.mark.asyncio
async def test_drilling_document_csv_extraction_flows_into_daily_report_reconciliation(client, db):
    well_id, wellbore_id = await _create_well(client)
    csv_bytes = (
        b"Activity Description,Start Time,End Time,Duration (h),Depth MD (ft)\n"
        b"Drill ahead,00:00,06:00,6.0,1500\n"
    )
    uploaded = await client.post(
        "/api/v1/documents",
        files={"file": ("shift-operations.csv", csv_bytes, "text/csv")},
        data={
            "well_id": well_id,
            "wellbore_id": wellbore_id,
            "doc_type": "drilling_document",
        },
        headers=headers("engineer"),
    )
    assert uploaded.status_code == 201, uploaded.text
    document = uploaded.json()["document"]
    assert document["doc_type"] == "drilling_document"

    listed = await client.get(
        "/api/v1/documents",
        params={"well_id": well_id, "doc_types": ["ddr", "drilling_document"], "limit": 500},
        headers=headers("engineer"),
    )
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()["items"]] == [document["id"]]
    conflicting_filters = await client.get(
        "/api/v1/documents",
        params={"well_id": well_id, "doc_type": "ddr", "doc_types": ["ddr"]},
        headers=headers("engineer"),
    )
    assert conflicting_filters.status_code == 422, conflicting_filters.text

    detail = await client.get(f"/api/v1/documents/{document['id']}", headers=headers("engineer"))
    assert detail.status_code == 200, detail.text
    operation_record = next(
        item for item in detail.json()["records"] if item["record_type"] == "operation_row"
    )
    assert operation_record["payload"]["activity description"] == "Drill ahead"
    assert operation_record["payload"]["duration_hours"] == 6.0
    assert operation_record["method_version"] == "1.2.0"

    evidence = await client.get(
        "/api/v1/evidence",
        params={"document_id": document["id"], "subject_id": operation_record["id"]},
        headers=headers("engineer"),
    )
    assert evidence.status_code == 200, evidence.text
    operation_evidence = next(item for item in evidence.json()["items"] if item["subject_id"] == operation_record["id"])
    assert operation_evidence["locator"]["source"]["format"] == "csv"
    assert operation_evidence["locator"]["source"]["kind"] == "table_row"
    assert operation_evidence["locator"]["field_locations"]["duration_hours"]["row"] == 2
    assert operation_evidence["locator"]["field_locations"]["duration_hours"]["column"] == 4

    imported = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/imports",
        json={
            "wellbore_id": wellbore_id,
            "report_date": "2026-11-04",
            "report_timezone": "UTC",
            "shift_code": "day",
            "report_number": "DDR-MULTIFORMAT-001",
            "source_document_id": document["id"],
            "source_record_id": operation_record["id"],
            "content": {
                "source_document_id": document["id"],
                "source_record_id": operation_record["id"],
                "operations_summary": "Extracted activity remains a proposal until reconciliation.",
                "activities": [{
                    "activity_kind": "operation",
                    "operation_kind": "drilling",
                    "title": "Drill ahead",
                    "duration_value": 6.0,
                    "duration_unit": "h",
                    "duration_basis": "reported",
                    "source_document_id": document["id"],
                    "source_record_id": operation_record["id"],
                }],
            },
        },
        headers=headers("engineer"),
    )
    assert imported.status_code == 201, imported.text
    imported_body = imported.json()
    assert imported_body["revision"]["status"] == "draft"
    assert imported_body["revision"]["activities"][0]["resolution_state"] == "proposed"
    proposal = next(row for row in imported_body["revision"]["reconciliation"] if row["target_kind"] == "activity")

    reviewed = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{imported_body['report']['id']}/revisions/1/reconcile",
        json={
            "expected_edit_version": 1,
            "reason": "Compared the extracted activity with its cited CSV row.",
            "decisions": [{"target_key": proposal["target_key"], "decision": "accepted"}],
        },
        headers=headers("engineer"),
    )
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["revision"]["activities"][0]["resolution_state"] == "accepted"
    assert reviewed.json()["revision"]["content_hash_verified"] is True


@pytest.mark.asyncio
async def test_imported_daily_report_reconciliation_measurements_and_correction(client, db):
    well_id, wellbore_id = await _create_well(client)
    document_id, record_id = await _create_import_evidence(client, db, well_id, wellbore_id)
    content = {
        "source_document_id": document_id,
        "source_record_id": record_id,
        "summary": "Extracted summary awaiting human review.",
        "operations_summary": "Time and depth are not inferred from ingestion metadata.",
        "activities": [
            {
                "activity_kind": "operation",
                "operation_kind": "drilling",
                "title": "Proposed drilling interval",
                "source_document_id": document_id,
                "source_record_id": record_id,
                "duration_value": 8.0,
                "duration_unit": "h",
                "duration_basis": "reported",
            }
        ],
        "measurements": [
            {
                "parameter_key": "mud_weight",
                "value": 9.2,
                "unit": "ppg",
                "value_basis": "reported",
                "source_document_id": document_id,
                "source_record_id": record_id,
            }
        ],
    }
    payload = {
        "wellbore_id": wellbore_id,
        "report_date": "2026-11-02",
        "report_timezone": "UTC",
        "shift_code": "day",
        "report_number": "DDR-IMPORT-01",
        "source_document_id": document_id,
        "source_record_id": record_id,
        "field_sources": [
            {"field_key": "summary", "source_record_id": record_id, "source_path": "payload.summary"}
        ],
        "content": content,
    }
    imported = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/imports",
        json=payload,
        headers={**headers("engineer"), "Idempotency-Key": "daily-report-import-reconcile-1"},
    )
    assert imported.status_code == 201, imported.text
    original = imported.json()
    report_id = original["report"]["id"]
    revision = original["revision"]
    assert revision["source_kind"] == "import"
    assert revision["status"] == "draft"
    assert revision["content_hash_verified"] is True
    assert {item["resolution_state"] for item in revision["activities"]} == {"proposed"}
    assert {item["resolution_state"] for item in revision["measurements"]} == {"proposed"}
    assert {item["resolution_state"] for item in revision["reconciliation"]} == {"proposed"}
    assert all(item["proposal_matches_current"] for item in revision["reconciliation"])
    assert len(revision["reconciliation"]) == 3
    activity_proposal = next(item for item in revision["reconciliation"] if item["target_kind"] == "activity")
    measurement_proposal = next(item for item in revision["reconciliation"] if item["target_kind"] == "measurement")
    assert activity_proposal["target_id"] == revision["activities"][0]["id"]
    assert measurement_proposal["target_id"] == revision["measurements"][0]["id"]
    assert all(
        item["target_id"] is None
        for item in revision["reconciliation"]
        if item["target_kind"] in {"revision_field", "report_identity"}
    )
    assert revision["measurements"][0]["observed_at"] is None
    assert revision["measurements"][0]["time_basis"] == "unknown"
    replayed_import = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/imports",
        json=payload,
        headers={**headers("engineer"), "Idempotency-Key": "daily-report-import-reconcile-1"},
    )
    assert replayed_import.status_code == 201, replayed_import.text
    assert replayed_import.json() == original
    denied_import = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/imports", json=payload, headers=headers("viewer")
    )
    assert denied_import.status_code == 403

    blocked = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/submit",
        json={"expected_edit_version": 1, "reason": "Attempt before reconciliation."},
        headers=headers("engineer"),
    )
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["error"]["details"]["target_count"] == 3

    target_keys = {item["target_kind"]: item["target_key"] for item in revision["reconciliation"]}
    denied_reconcile = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/reconcile",
        json={
            "expected_edit_version": 1,
            "reason": "Viewer is not authorized to resolve proposals.",
            "decisions": [{"target_key": next(iter(target_keys.values())), "decision": "accepted"}],
        },
        headers=headers("viewer"),
    )
    assert denied_reconcile.status_code == 403
    first_decision = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/reconcile",
        json={
            "expected_edit_version": 1,
            "reason": "Independently checked the imported values against the source document.",
            "decisions": [{"target_key": key, "decision": "accepted"} for key in target_keys.values()],
        },
        headers={**headers("engineer"), "Idempotency-Key": "daily-report-reconcile-1"},
    )
    assert first_decision.status_code == 200, first_decision.text
    assert first_decision.json()["revision"]["edit_version"] == 2
    assert {item["resolution_state"] for item in first_decision.json()["revision"]["reconciliation"]} == {"accepted"}
    assert first_decision.json()["revision"]["measurements"][0]["resolution_state"] == "accepted"
    stale_edit_version = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/reconcile",
        json={
            "expected_edit_version": 1,
            "reason": "A stale reconciliation writer must not overwrite decisions.",
            "decisions": [{"target_key": target_keys["revision_field"], "decision": "accepted"}],
        },
        headers=headers("engineer"),
    )
    assert stale_edit_version.status_code == 409
    assert stale_edit_version.json()["error"]["details"]["current_edit_version"] == 2

    corrected_content = copy.deepcopy(content)
    corrected_content["summary"] = "Human-corrected summary after checking the source."
    corrected_content["measurements"][0]["value"] = 9.3
    edited = await client.patch(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1",
        json={
            "expected_edit_version": 2,
            "reason": "Correct the narrative and mud-weight entry during source review.",
            "content": corrected_content,
        },
        headers={**headers("engineer"), "Idempotency-Key": "daily-report-import-edit-1"},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"]["edit_version"] == 3
    decisions_after_edit = {item["target_key"]: item["resolution_state"] for item in edited.json()["revision"]["reconciliation"]}
    assert decisions_after_edit[target_keys["revision_field"]] == "proposed"
    assert decisions_after_edit[target_keys["measurement"]] == "proposed"
    assert decisions_after_edit[target_keys["activity"]] == "accepted"
    proposal_match_by_key = {
        item["target_key"]: item["proposal_matches_current"] for item in edited.json()["revision"]["reconciliation"]
    }
    assert proposal_match_by_key[target_keys["revision_field"]] is False
    assert proposal_match_by_key[target_keys["measurement"]] is False
    assert proposal_match_by_key[target_keys["activity"]] is True
    assert {item["resolution_state"] for item in edited.json()["revision"]["measurements"]} == {"proposed"}
    incorrect_accept = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/reconcile",
        json={
            "expected_edit_version": 3,
            "reason": "An edited value cannot be accepted as unchanged source output.",
            "decisions": [{"target_key": target_keys["revision_field"], "decision": "accepted"}],
        },
        headers=headers("engineer"),
    )
    assert incorrect_accept.status_code == 422
    assert "mark it corrected" in incorrect_accept.json()["error"]["message"]

    final_decisions = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/reconcile",
        json={
            "expected_edit_version": 3,
            "reason": "Verified the corrected field and measurement against the cited source.",
            "decisions": [
                {"target_key": target_keys["revision_field"], "decision": "corrected"},
                {"target_key": target_keys["measurement"], "decision": "corrected"},
                {"target_key": target_keys["activity"], "decision": "accepted"},
            ],
        },
        headers={**headers("engineer"), "Idempotency-Key": "daily-report-reconcile-2"},
    )
    assert final_decisions.status_code == 200, final_decisions.text
    assert {item["resolution_state"] for item in final_decisions.json()["revision"]["reconciliation"]} == {"corrected", "accepted"}
    assert final_decisions.json()["revision"]["measurements"][0]["resolution_state"] == "corrected"
    assert final_decisions.json()["revision"]["measurements"][0]["source_value"] == 9.3

    submitted = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/submit",
        json={"expected_edit_version": 4, "reason": "Submit the fully reconciled import."},
        headers=headers("engineer"),
    )
    assert submitted.status_code == 200, submitted.text
    approved = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/approve",
        json={"expected_edit_version": 5, "reason": "Independent review of the imported report is complete."},
        headers=headers("drilling_supervisor"),
    )
    assert approved.status_code == 200, approved.text

    correction = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/corrections",
        json={"reason": "Create a follow-up correction with new source reconciliation proposals."},
        headers=headers("drilling_supervisor"),
    )
    assert correction.status_code == 201, correction.text
    corrected_revision = correction.json()["revision"]
    assert corrected_revision["source_kind"] == "import"
    assert corrected_revision["content_hash_verified"] is True
    assert len(corrected_revision["reconciliation"]) == 3
    assert {item["resolution_state"] for item in corrected_revision["reconciliation"]} == {"proposed"}
    assert {item["resolution_state"] for item in corrected_revision["measurements"]} == {"proposed"}

    # A direct database write cannot retarget a revision-one decision to a child owned by revision two,
    # even within the same tenant. The normal API never accepts target IDs, but the schema also protects
    # import/revision consistency if an internal caller or maintenance path bypasses the service.
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from drillai.db.models import DailyReportReconciliation

    for target_kind, target_column, foreign_target_id in (
        ("activity", "activity_target_id", corrected_revision["activities"][0]["id"]),
        ("measurement", "measurement_target_id", corrected_revision["measurements"][0]["id"]),
    ):
        async with db.session_factory() as session:
            decision = (
                await session.execute(
                    select(DailyReportReconciliation).where(
                        DailyReportReconciliation.revision_id == revision["id"],
                        DailyReportReconciliation.target_kind == target_kind,
                    )
                )
            ).scalar_one()
            setattr(decision, target_column, foreign_target_id)
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

    blocked_correction = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/2/submit",
        json={"expected_edit_version": 1, "reason": "Correction must be reconciled again."},
        headers=headers("engineer"),
    )
    assert blocked_correction.status_code == 422, blocked_correction.text
    correction_keys = [item["target_key"] for item in corrected_revision["reconciliation"]]
    reconciled_correction = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/2/reconcile",
        json={
            "expected_edit_version": 1,
            "reason": "Rechecked the corrected revision's source links.",
            "decisions": [{"target_key": key, "decision": "accepted"} for key in correction_keys],
        },
        headers=headers("drilling_supervisor"),
    )
    assert reconciled_correction.status_code == 200, reconciled_correction.text
    assert reconciled_correction.json()["revision"]["edit_version"] == 2


@pytest.mark.asyncio
async def test_import_supports_source_proposals_for_every_reconciliable_report_field(client, db):
    well_id, wellbore_id = await _create_well(client)
    document_id, record_id = await _create_import_evidence(client, db, well_id, wellbore_id)
    field_values = {
        "summary": "Extracted summary awaiting human review.",
        "operations_summary": "Extracted operations narrative awaiting human review.",
        "safety_notes": "Extracted safety narrative awaiting human review.",
        "next_period_plan": "Extracted next-period plan awaiting human review.",
        "environmental_notes": "Extracted environmental narrative awaiting human review.",
    }
    imported = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/imports",
        json={
            "wellbore_id": wellbore_id,
            "report_date": "2026-11-03",
            "report_timezone": "UTC",
            "shift_code": "day",
            "report_number": "DDR-ALL-FIELD-SOURCES",
            "source_document_id": document_id,
            "source_record_id": record_id,
            "field_sources": [
                {"field_key": field_key, "source_record_id": record_id, "source_path": f"payload.{field_key}"}
                for field_key in field_values
            ],
            "content": {
                "source_document_id": document_id,
                "source_record_id": record_id,
                **field_values,
            },
        },
        headers=headers("engineer"),
    )
    assert imported.status_code == 201, imported.text
    reconciliation = imported.json()["revision"]["reconciliation"]
    assert {item["field_key"] for item in reconciliation} == set(field_values)
    assert {item["source_path"] for item in reconciliation} == {
        f"payload.{field_key}" for field_key in field_values
    }
    assert {item["resolution_state"] for item in reconciliation} == {"proposed"}
    assert all(item["proposal_matches_current"] for item in reconciliation)


@pytest.mark.asyncio
async def test_duplicate_import_identity_returns_existing_report_without_mutating_or_creating(client, db):
    well_id, wellbore_id = await _create_well(client)
    document_id, record_id = await _create_import_evidence(client, db, well_id, wellbore_id)
    payload = {
        "wellbore_id": wellbore_id,
        "report_date": "2026-11-06",
        "report_timezone": "UTC",
        "shift_code": "day",
        "report_number": "DDR-IMPORT-IDEMPOTENT-CONFLICT",
        "source_document_id": document_id,
        "source_record_id": record_id,
        "field_sources": [
            {"field_key": "summary", "source_record_id": record_id, "source_path": "payload.summary"}
        ],
        "content": {
            "summary": "Extracted summary awaiting human review.",
            "source_document_id": document_id,
            "source_record_id": record_id,
        },
    }
    first = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/imports", json=payload, headers=headers("engineer")
    )
    assert first.status_code == 201, first.text
    original = first.json()

    conflicting_payload = copy.deepcopy(payload)
    conflicting_payload["content"]["summary"] = "A conflicting retry must never overwrite the existing revision."
    conflict = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/imports",
        json=conflicting_payload,
        headers=headers("engineer"),
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["details"]["report_id"] == original["report"]["id"]
    assert "no duplicate draft was created" in conflict.json()["error"]["message"]

    other_wellbore = await client.post(
        f"/api/v1/wells/{well_id}/wellbores",
        json={"name": "Sidetrack bore", "purpose": "sidetrack", "parent_wellbore_id": wellbore_id},
        headers=headers("well_manager"),
    )
    assert other_wellbore.status_code == 201, other_wellbore.text
    sidetrack_wellbore_id = other_wellbore.json()["id"]
    nonmatching_report_id, _ = await _create_approved_period(
        client,
        well_id,
        sidetrack_wellbore_id,
        report_date="2026-11-05",
        report_number="DDR-ANOTHER-REPORT",
        period_start_at="2026-11-05T00:00:00Z",
        period_end_at="2026-11-05T12:00:00Z",
        approve=False,
    )
    listing = await client.get(f"/api/v1/wells/{well_id}/daily-reports?limit=20", headers=headers("engineer"))
    assert listing.status_code == 200, listing.text
    assert listing.json()["total"] == 2
    assert {item["id"] for item in listing.json()["items"]} == {
        original["report"]["id"],
        nonmatching_report_id,
    }
    other_well_id, other_wellbore_id = await _create_well(client)
    other_scoped_report_id, _ = await _create_approved_period(
        client,
        other_well_id,
        other_wellbore_id,
        report_date="2026-11-04",
        report_number="DDR-IMPORT-IDEMPOTENT-CONFLICT",
        period_start_at="2026-11-04T00:00:00Z",
        period_end_at="2026-11-04T12:00:00Z",
        approve=False,
    )
    exact_number = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports",
        params={
            "report_number": "DDR-IMPORT-IDEMPOTENT-CONFLICT",
            "wellbore_id": wellbore_id,
            "limit": 20,
        },
        headers=headers("engineer"),
    )
    assert exact_number.status_code == 200, exact_number.text
    assert exact_number.json()["total"] == 1
    assert exact_number.json()["items"][0]["id"] == original["report"]["id"]
    other_wellbore_filter = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports",
        params={"wellbore_id": sidetrack_wellbore_id},
        headers=headers("engineer"),
    )
    assert other_wellbore_filter.status_code == 200, other_wellbore_filter.text
    assert other_wellbore_filter.json()["total"] == 1
    assert other_wellbore_filter.json()["items"][0]["id"] == nonmatching_report_id
    blank_wellbore = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports",
        params={"wellbore_id": " "},
        headers=headers("engineer"),
    )
    assert blank_wellbore.status_code == 422, blank_wellbore.text
    other_well_filter = await client.get(
        f"/api/v1/wells/{other_well_id}/daily-reports",
        params={"report_number": "DDR-IMPORT-IDEMPOTENT-CONFLICT"},
        headers=headers("engineer"),
    )
    assert other_well_filter.status_code == 200, other_well_filter.text
    assert other_well_filter.json()["total"] == 1
    assert other_well_filter.json()["items"][0]["id"] == other_scoped_report_id
    foreign_wellbore_filter = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports",
        params={"wellbore_id": other_wellbore_id},
        headers=headers("engineer"),
    )
    assert foreign_wellbore_filter.status_code == 200, foreign_wellbore_filter.text
    assert foreign_wellbore_filter.json()["items"] == []
    assert foreign_wellbore_filter.json()["total"] == 0
    case_mismatch = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports",
        params={"report_number": "ddr-import-idempotent-conflict"},
        headers=headers("engineer"),
    )
    assert case_mismatch.status_code == 200, case_mismatch.text
    assert case_mismatch.json()["items"] == []
    blank_number = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports",
        params={"report_number": " "},
        headers=headers("engineer"),
    )
    assert blank_number.status_code == 422, blank_number.text
    current = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports/{original['report']['id']}", headers=headers("engineer")
    )
    assert current.status_code == 200, current.text
    assert current.json()["latest_revision"]["summary"] == "Extracted summary awaiting human review."
    assert current.json()["latest_revision"]["content_hash_verified"] is True
    assert current.json()["latest_revision"]["edit_version"] == 1


@pytest.mark.asyncio
async def test_rejected_source_row_is_retained_but_cannot_make_a_blank_report_submittable(client, db):
    well_id, wellbore_id = await _create_well(client)
    document_id, record_id = await _create_import_evidence(client, db, well_id, wellbore_id)
    payload = {
        "wellbore_id": wellbore_id,
        "report_date": "2026-11-04",
        "report_timezone": "UTC",
        "shift_code": "24h",
        "report_number": "DDR-IMPORT-REJECT-ROW",
        "source_document_id": document_id,
        "source_record_id": record_id,
        "content": {
            "source_document_id": document_id,
            "source_record_id": record_id,
            "activities": [
                {
                    "activity_kind": "operation",
                    "operation_kind": "drilling",
                    "title": "Unsupported extracted row",
                    "source_document_id": document_id,
                    "source_record_id": record_id,
                }
            ],
            "measurements": [
                {
                    "parameter_key": "mud_weight",
                    "value": 9.2,
                    "unit": "ppg",
                    "value_basis": "reported",
                    "source_document_id": document_id,
                    "source_record_id": record_id,
                }
            ],
        },
    }
    imported = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/imports", json=payload, headers=headers("engineer")
    )
    assert imported.status_code == 201, imported.text
    revision = imported.json()["revision"]
    targets = {item["target_kind"]: item["target_key"] for item in revision["reconciliation"]}
    rejected = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{imported.json()['report']['id']}/revisions/1/reconcile",
        json={
            "expected_edit_version": 1,
            "reason": "The cited document does not support this proposed operation row.",
            "decisions": [
                {"target_key": targets["activity"], "decision": "rejected"},
                {"target_key": targets["measurement"], "decision": "rejected"},
            ],
        },
        headers=headers("engineer"),
    )
    assert rejected.status_code == 200, rejected.text
    after = rejected.json()["revision"]
    assert len(after["activities"]) == 1
    assert after["activities"][0]["resolution_state"] == "rejected"
    assert len(after["measurements"]) == 1
    assert after["measurements"][0]["resolution_state"] == "rejected"
    assert {item["target_key"]: item["resolution_state"] for item in after["reconciliation"]} == {
        targets["activity"]: "rejected",
        targets["measurement"]: "rejected",
    }
    changed_rejected_activity = await client.patch(
        f"/api/v1/wells/{well_id}/daily-reports/{imported.json()['report']['id']}/revisions/1",
        json={
            "expected_edit_version": 2,
            "reason": "Attempt to alter a rejected source activity.",
            "content": {
                "source_document_id": document_id,
                "source_record_id": record_id,
                "activities": [{
                    "activity_kind": "operation",
                    "operation_kind": "drilling",
                    "title": "Altered rejected proposal",
                    "source_document_id": document_id,
                    "source_record_id": record_id,
                }],
                "measurements": [{
                    "parameter_key": "mud_weight",
                    "value": 9.2,
                    "unit": "ppg",
                    "value_basis": "reported",
                    "source_document_id": document_id,
                    "source_record_id": record_id,
                }],
            },
        },
        headers=headers("engineer"),
    )
    assert changed_rejected_activity.status_code == 409, changed_rejected_activity.text
    assert "rejected source activity is immutable" in changed_rejected_activity.json()["error"]["message"]
    changed_rejected_measurement = await client.patch(
        f"/api/v1/wells/{well_id}/daily-reports/{imported.json()['report']['id']}/revisions/1",
        json={
            "expected_edit_version": 2,
            "reason": "Attempt to alter a rejected source measurement.",
            "content": {
                "source_document_id": document_id,
                "source_record_id": record_id,
                "activities": [{
                    "activity_kind": "operation",
                    "operation_kind": "drilling",
                    "title": "Unsupported extracted row",
                    "source_document_id": document_id,
                    "source_record_id": record_id,
                }],
                "measurements": [{
                    "parameter_key": "mud_weight",
                    "value": 9.3,
                    "unit": "ppg",
                    "value_basis": "reported",
                    "source_document_id": document_id,
                    "source_record_id": record_id,
                }],
            },
        },
        headers=headers("engineer"),
    )
    assert changed_rejected_measurement.status_code == 409, changed_rejected_measurement.text
    assert "rejected source measurement is immutable" in changed_rejected_measurement.json()["error"]["message"]
    edited = await client.patch(
        f"/api/v1/wells/{well_id}/daily-reports/{imported.json()['report']['id']}/revisions/1",
        json={
            "expected_edit_version": 2,
            "reason": "Keep the rejected source proposal in the audit-visible draft history.",
            "content": {"source_document_id": document_id, "source_record_id": record_id},
        },
        headers=headers("engineer"),
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"]["activities"][0]["resolution_state"] == "rejected"
    assert edited.json()["revision"]["measurements"][0]["resolution_state"] == "rejected"
    edited_states = {
        item["target_key"]: item["resolution_state"] for item in edited.json()["revision"]["reconciliation"]
    }
    assert edited_states == {targets["activity"]: "rejected", targets["measurement"]: "rejected"}
    blocked_blank_report = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{imported.json()['report']['id']}/revisions/1/submit",
        json={"expected_edit_version": 3, "reason": "Submit after rejecting unsupported extracted row."},
        headers=headers("engineer"),
    )
    assert blocked_blank_report.status_code == 422, blocked_blank_report.text
    assert "blank report cannot be submitted" in blocked_blank_report.json()["error"]["message"]


@pytest.mark.parametrize("stale_mode", ["review_version", "payload"])
@pytest.mark.asyncio
async def test_import_reconciliation_rejects_stale_extraction_evidence(client, db, stale_mode):
    from sqlalchemy import select

    from drillai.db.models import ExtractedRecord

    well_id, wellbore_id = await _create_well(client)
    document_id, record_id = await _create_import_evidence(client, db, well_id, wellbore_id)
    payload = {
        "wellbore_id": wellbore_id,
        "report_date": "2026-11-03",
        "report_timezone": "UTC",
        "shift_code": "night",
        "report_number": "DDR-IMPORT-STALE",
        "source_document_id": document_id,
        "field_sources": [
            {"field_key": "summary", "source_record_id": record_id, "source_path": "payload.summary"}
        ],
        "content": {
            "summary": "Extracted summary awaiting human review.",
            "source_record_id": record_id,
            "source_document_id": document_id,
        },
    }
    imported = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/imports", json=payload, headers=headers("engineer")
    )
    assert imported.status_code == 201, imported.text
    revision = imported.json()["revision"]
    field_key = revision["reconciliation"][0]["target_key"]
    async with db.session_factory() as session:
        record = (
            await session.execute(select(ExtractedRecord).where(ExtractedRecord.id == record_id))
        ).scalar_one()
        if stale_mode == "review_version":
            record.review_version += 1
        else:
            # The fingerprint is intentionally left untouched: a payload mutation without a review
            # version/fingerprint update must still be caught by the persisted source-value hash.
            record.payload = {"summary": "Changed extraction payload without a version bump."}
        await session.commit()

    stale = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{imported.json()['report']['id']}/revisions/1/reconcile",
        json={
            "expected_edit_version": 1,
            "reason": "Attempt to accept a stale source proposal.",
            "decisions": [{"target_key": field_key, "decision": "accepted"}],
        },
        headers=headers("engineer"),
    )
    assert stale.status_code == 409, stale.text
    assert "source evidence changed" in stale.json()["error"]["message"]


@pytest.mark.asyncio
async def test_reconciliation_refuses_a_revision_with_a_tampered_content_fingerprint(client, db):
    from sqlalchemy import select

    from drillai.db.models import DailyDrillingReportRevision

    well_id, wellbore_id = await _create_well(client)
    document_id, record_id = await _create_import_evidence(client, db, well_id, wellbore_id)
    imported = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/imports",
        json={
            "wellbore_id": wellbore_id,
            "report_date": "2026-11-05",
            "report_timezone": "UTC",
            "shift_code": "day",
            "report_number": "DDR-IMPORT-TAMPERED",
            "source_document_id": document_id,
            "source_record_id": record_id,
            "field_sources": [{"field_key": "summary", "source_record_id": record_id, "source_path": "payload.summary"}],
            "content": {
                "summary": "Source-derived summary",
                "source_document_id": document_id,
                "source_record_id": record_id,
            },
        },
        headers=headers("engineer"),
    )
    assert imported.status_code == 201, imported.text
    report_id = imported.json()["report"]["id"]
    target_key = imported.json()["revision"]["reconciliation"][0]["target_key"]
    async with db.session_factory() as session:
        revision = (
            await session.execute(select(DailyDrillingReportRevision).where(DailyDrillingReportRevision.id == imported.json()["revision"]["id"]))
        ).scalar_one()
        revision.summary = "Tampered without a new content fingerprint"
        await session.commit()

    denied = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/reconcile",
        json={
            "expected_edit_version": 1,
            "reason": "Attempt to accept a proposal on a tampered revision.",
            "decisions": [{"target_key": target_key, "decision": "accepted"}],
        },
        headers=headers("engineer"),
    )
    assert denied.status_code == 409, denied.text
    assert "does not match its stored revision fingerprint" in denied.json()["error"]["message"]
    detail = await client.get(f"/api/v1/wells/{well_id}/daily-reports/{report_id}", headers=headers("engineer"))
    assert detail.status_code == 200, detail.text
    assert detail.json()["latest_revision"]["edit_version"] == 1
    assert detail.json()["latest_revision"]["content_hash_verified"] is False
    assert detail.json()["latest_revision"]["reconciliation"][0]["resolution_state"] == "proposed"


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
    assert {"other", "npt", "operation"} <= set(parameter_catalogue.json()["activity_kinds"])
    assert "stuck_pipe" in parameter_catalogue.json()["npt_categories"]

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


@pytest.mark.asyncio
async def test_daily_report_immutable_revision_exports_are_safe_permissioned_and_audited(client, db):
    from io import BytesIO
    from zipfile import ZipFile

    from openpyxl import load_workbook
    from sqlalchemy import select

    from drillai.db.models import AuditLog, DailyDrillingReportRevision

    well_id, wellbore_id = await _create_well(client)
    payload = _report_payload(wellbore_id)
    payload["content"]["summary"] = 'حفاری پیشرفت کرد: =HYPERLINK("https://example.invalid")'
    payload["content"]["activities"][0]["description"] = "<script>alert('not markup')</script>"
    created = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=payload,
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["report"]["id"]
    revision_id = created.json()["revision"]["id"]
    export_url = f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/exports"

    # Drafts are never downloadable, and a read-only user cannot gain export rights from access to
    # the report/history screens.
    draft_export = await client.get(f"{export_url}/json", headers=headers("engineer"))
    assert draft_export.status_code == 409, draft_export.text

    submitted = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/submit",
        json={"expected_edit_version": 1, "reason": "Ready for independent review."},
        headers=headers("engineer"),
    )
    assert submitted.status_code == 200, submitted.text
    approved = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/approve",
        json={"expected_edit_version": 2, "reason": "Independent review complete."},
        headers=headers("drilling_supervisor"),
    )
    assert approved.status_code == 200, approved.text
    correction = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/corrections",
        json={"reason": "Open a new numbered correction without changing approved history."},
        headers=headers("drilling_supervisor"),
    )
    assert correction.status_code == 201, correction.text
    assert correction.json()["revision"]["revision_number"] == 2
    assert correction.json()["revision"]["status"] == "draft"
    latest_draft_export = await client.get(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/2/exports/json",
        headers=headers("engineer"),
    )
    assert latest_draft_export.status_code == 409, latest_draft_export.text

    denied = await client.get(f"{export_url}/json", headers=headers("viewer"))
    assert denied.status_code == 403, denied.text

    formats = {
        "json": "application/json",
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
    artifacts = {}
    for export_format, media_type in formats.items():
        response = await client.get(
            f"{export_url}/{export_format}?locale=fa",
            headers=headers("engineer"),
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith(media_type)
        assert response.headers["content-disposition"].startswith("attachment; filename=\"daily-report-revision-1.")
        assert response.headers["cache-control"] == "private, no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
        assert len(response.content) > 100
        artifacts[export_format] = response

    assert artifacts["json"].content.startswith(b"{")
    json_export = artifacts["json"].json()
    assert json_export["export_manifest"]["revision_id"] == revision_id
    assert json_export["export_manifest"]["revision_content_hash_verified"] is True
    assert "دلالت بر تأیید مهندسی بیشتر ندارد" in json_export["export_manifest"]["export_notice"]
    assert json_export["revision"]["summary"] == payload["content"]["summary"]
    assert artifacts["pdf"].content.startswith(b"%PDF-")

    with ZipFile(BytesIO(artifacts["docx"].content)) as archive:
        document_xml = archive.read("word/document.xml").decode("utf-8")
        assert "گزارش روزانه حفاری" in document_xml
        assert "حفاری پیشرفت کرد" in document_xml
        assert "حفاری" in document_xml and "وزن گل" in document_xml
        assert "<w:bidi" in document_xml and "<w:rtl" in document_xml
        # Narrative text is ordinary text, not injected document markup.
        assert "&lt;script&gt;" in document_xml
        assert "این خروجی به‌تنهایی دلالت بر تأیید مهندسی بیشتر ندارد" in document_xml

    workbook = load_workbook(BytesIO(artifacts["xlsx"].content), data_only=False)
    assert len(workbook.sheetnames) == 4
    assert all(workbook[sheet].sheet_view.rightToLeft is True for sheet in workbook.sheetnames)
    report_sheet = workbook[workbook.sheetnames[0]]
    report_labels = {row[0].value for row in report_sheet.iter_rows() if row[0].value is not None}
    assert {"قالب خروجی", "زبان خروجی", "یادداشت خروجی و بازبینی مهندسی"} <= report_labels
    assert "شناسه عملیات پیوندشده" in {
        cell.value for cell in workbook[workbook.sheetnames[1]][1]
    }
    assert "شناسه رویداد پیوندشده" in {
        cell.value for cell in workbook[workbook.sheetnames[1]][1]
    }
    assert "عمق استاندارد از (متر)" in {
        cell.value for cell in workbook[workbook.sheetnames[1]][1]
    }
    assert "عمق وابسته استاندارد (متر)" in {
        cell.value for cell in workbook[workbook.sheetnames[2]][1]
    }
    summary_cell = next(
        cell
        for row in workbook[workbook.sheetnames[0]].iter_rows()
        for cell in row
        if cell.value == payload["content"]["summary"]
    )
    assert summary_cell.data_type == "s"  # formula-like user text stays a literal string

    async with db.session_factory() as session:
        export_audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "daily_report.export",
                    AuditLog.resource_kind == "daily_report_revision",
                    AuditLog.resource_id == revision_id,
                )
            )
        ).scalars().all()
    assert len(export_audits) == len(formats)
    by_format = {row.details["format"]: row for row in export_audits}
    assert set(by_format) == set(formats)
    for export_format, row in by_format.items():
        assert row.permission_decision["required"] == "daily_report.export"
        assert row.permission_decision["granted"] is True
        assert row.details["report_id"] == report_id
        assert row.details["revision_id"] == revision_id
        assert row.details["revision_content_hash_verified"] is True
        assert row.details["locale"] == "fa"
        assert row.details["bytes"] == len(artifacts[export_format].content)
        assert row.details["artifact_sha256"] == artifacts[export_format].headers["etag"].strip('"')

    # A corrupt revision fingerprint fails closed before any artifact or success audit is returned.
    async with db.session_factory() as session:
        row = (
            await session.execute(
                select(DailyDrillingReportRevision).where(DailyDrillingReportRevision.id == revision_id)
            )
        ).scalar_one()
        row.content_hash = "0" * 64
        await session.commit()
    tampered = await client.get(f"{export_url}/json", headers=headers("engineer"))
    assert tampered.status_code == 409, tampered.text
    async with db.session_factory() as session:
        count = len(
            (
                await session.execute(
                    select(AuditLog.id).where(
                        AuditLog.action == "daily_report.export",
                        AuditLog.resource_kind == "daily_report_revision",
                        AuditLog.resource_id == revision_id,
                    )
                )
            ).all()
        )
    assert count == len(formats)


@pytest.mark.asyncio
async def test_daily_report_export_audit_failure_does_not_deliver_artifact(client, app, monkeypatch):
    import httpx

    from drillai.api.routers import daily_reports as daily_reports_router

    well_id, wellbore_id = await _create_well(client)
    created = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports",
        json=_report_payload(wellbore_id),
        headers=headers("engineer"),
    )
    assert created.status_code == 201, created.text
    report_id = created.json()["report"]["id"]
    submitted = await client.post(
        f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/submit",
        json={"expected_edit_version": 1, "reason": "Ready for independent review."},
        headers=headers("engineer"),
    )
    assert submitted.status_code == 200, submitted.text

    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("injected export-audit failure")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    with monkeypatch.context() as patch:
        patch.setattr(daily_reports_router, "record_audit", fail_audit)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as failing_client:
            failed = await failing_client.get(
                f"/api/v1/wells/{well_id}/daily-reports/{report_id}/revisions/1/exports/pdf",
                headers=headers("engineer"),
            )
    assert failed.status_code == 500
    assert failed.content != b"%PDF-"
    assert "injected export-audit failure" not in failed.text
