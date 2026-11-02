"""HTTP integration tests for versioned, audited extracted-record review."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.api.conftest import headers

if TYPE_CHECKING:
    from tests.fixtures.fabric import Fabric

pytestmark = pytest.mark.asyncio


async def _upload_depth_record(client, *, name: str = "Review well") -> tuple[str, str, dict]:
    project_response = await client.post(
        "/api/v1/projects",
        json={"name": f"{name} project", "operator": "Test operator"},
        headers=headers(),
    )
    assert project_response.status_code == 201, project_response.text
    project_id = project_response.json()["id"]
    well_response = await client.post(
        "/api/v1/wells",
        json={"project_id": project_id, "name": name, "well_type": "development_producer"},
        headers=headers(),
    )
    assert well_response.status_code == 201, well_response.text
    well_id = well_response.json()["id"]
    upload = await client.post(
        "/api/v1/documents",
        headers=headers(),
        files={"file": ("depth.txt", b"Bit depth: 1000 m\n", "text/plain")},
        data={"well_id": well_id, "doc_type": "ddr"},
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["document"]["id"]
    detail = await client.get(f"/api/v1/documents/{document_id}", headers=headers())
    assert detail.status_code == 200, detail.text
    record = next(
        (item for item in detail.json()["records"] if item["record_type"] == "depth_reading"),
        None,
    )
    assert record is not None, detail.json()["records"]
    return well_id, document_id, record


async def _upload_survey_record(client, *, survey_csv: bytes | None = None) -> tuple[str, str, dict]:
    project_response = await client.post(
        "/api/v1/projects",
        json={"name": "Survey review project", "operator": "Test operator"},
        headers=headers(),
    )
    assert project_response.status_code == 201, project_response.text
    well_response = await client.post(
        "/api/v1/wells",
        json={
            "project_id": project_response.json()["id"],
            "name": "Survey review well",
            "well_type": "development_producer",
        },
        headers=headers(),
    )
    assert well_response.status_code == 201, well_response.text
    well_id = well_response.json()["id"]
    upload = await client.post(
        "/api/v1/documents",
        headers=headers(),
        files={
            "file": (
                "survey.csv",
                survey_csv or b"MD (m),Inclination (deg),Azimuth (deg)\n500,12,180\n",
                "text/csv",
            )
        },
        data={"well_id": well_id, "doc_type": "ddr"},
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["document"]["id"]
    detail = await client.get(f"/api/v1/documents/{document_id}", headers=headers())
    assert detail.status_code == 200, detail.text
    record = next(
        item for item in detail.json()["records"] if item["record_type"] == "survey_station"
    )
    return well_id, document_id, record


def _review_body(record: dict, *, depth: float, reason: str = "Checked against the source page") -> dict:
    return {
        "expected_version": record["review_version"],
        "decision": "validate",
        "reason": reason,
        "changes": {
            "payload": {"bit_depth_si": depth},
            "depth_md_si": depth,
        },
    }


async def test_focused_record_read_is_tenant_and_document_scoped(fabric: Fabric):
    uploaded = await fabric.upload(
        fabric.alpha,
        b"Bit depth: 1000 m\n",
        filename="review-depth.txt",
        content_type="text/plain",
        well_id=fabric.alpha.well_id,
        doc_type="ddr",
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["document"]["id"]
    detail = await fabric.http.get(f"/api/v1/documents/{document_id}", headers=fabric.alpha.headers)
    assert detail.status_code == 200, detail.text
    record = next(row for row in detail.json()["records"] if row["record_type"] == "depth_reading")
    alpha_read = await fabric.http.get(
        f"/api/v1/documents/{document_id}/records/{record['id']}", headers=fabric.alpha.headers
    )
    assert alpha_read.status_code == 200, alpha_read.text

    bravo_cross_tenant = await fabric.http.get(
        f"/api/v1/documents/{document_id}/records/{record['id']}", headers=fabric.bravo.headers
    )
    assert bravo_cross_tenant.status_code == 404, bravo_cross_tenant.text
    other_document = await fabric.upload(
        fabric.alpha,
        b"Bit depth: 1100 m\n",
        filename="other-review-depth.txt",
        content_type="text/plain",
        well_id=fabric.alpha.well_id,
        doc_type="ddr",
    )
    assert other_document.status_code == 201, other_document.text
    cross_document = await fabric.http.get(
        f"/api/v1/documents/{other_document.json()['document']['id']}/records/{record['id']}",
        headers=fabric.alpha.headers,
    )
    assert cross_document.status_code == 404, cross_document.text


async def test_review_corrects_validates_audits_and_replays_idempotently(client, db):
    from sqlalchemy import select

    from drillai.db.models import ExtractedRecord

    well_id, document_id, record = await _upload_depth_record(client)
    prior_note = "Earlier validator flagged the source unit for manual confirmation."
    async with db.session() as session:
        row = (
            await session.execute(select(ExtractedRecord).where(ExtractedRecord.id == record["id"]))
        ).scalar_one()
        row.validation_notes = prior_note
        await session.commit()
    assert record["review_version"] == 1
    assert record["validation_state"] == "extracted"
    assert record["payload"]["bit_depth_si"] == 1000.0

    body = _review_body(record, depth=1001.5)
    request_headers = headers("engineer", **{"Idempotency-Key": "review-depth-record-1"})
    response = await client.patch(
        f"/api/v1/documents/{document_id}/records/{record['id']}/review",
        headers=request_headers,
        json=body,
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["review_version"] == 2
    assert updated["validation_state"] == "human_validated"
    assert updated["payload"]["bit_depth_si"] == 1001.5
    assert updated["depth_md_si"] == 1001.5
    assert updated["validated_by"]
    assert updated["validated_at"]

    replay = await client.patch(
        f"/api/v1/documents/{document_id}/records/{record['id']}/review",
        headers=request_headers,
        json=body,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == updated

    audit = await client.get(f"/api/v1/wells/{well_id}/audit-log", headers=headers())
    assert audit.status_code == 200, audit.text
    review_entries = [item for item in audit.json()["items"] if item["action"] == "document.record_review"]
    assert len(review_entries) == 1
    assert review_entries[0]["details"]["decision"] == "validate"
    assert review_entries[0]["details"]["changed_fields"] == ["depth_md_si", "payload"]
    assert "payload_sha256" in review_entries[0]["before"]
    assert review_entries[0]["before"]["validation_notes"] == prior_note
    assert review_entries[0]["before"]["validation_notes_truncated"] is False
    assert "payload_sha256" in review_entries[0]["after"]
    assert "bit_depth_si" not in str(review_entries[0]["after"])


async def test_survey_review_validates_frame_identity_datum_correction_and_audits(client):
    _well_id, document_id, record = await _upload_survey_record(client)
    assert record["payload"]["azimuth_reference"] == "unknown"
    assert record["payload"]["datum"] == "unknown"

    url = f"/api/v1/documents/{document_id}/records/{record['id']}/review"
    incomplete = dict(record["payload"])
    incomplete.update({"azimuth_reference": "grid", "datum": "rkb"})
    missing_id = await client.patch(
        url,
        headers=headers("engineer", **{"Idempotency-Key": "survey-review-missing-frame-id"}),
        json={
            "expected_version": 1,
            "decision": "validate",
            "reason": "Reference type was checked but the frame identity was not available",
            "changes": {"payload": incomplete},
        },
    )
    assert missing_id.status_code == 422, missing_id.text
    assert "requires azimuth_reference_id" in missing_id.json()["error"]["message"]

    invalid_correction = {
        **incomplete,
        "azimuth_reference_id": "EPSG:32631",
        "grid_correction_deg": 361.0,
    }
    invalid = await client.patch(
        url,
        headers=headers("engineer", **{"Idempotency-Key": "survey-review-invalid-correction"}),
        json={
            "expected_version": 1,
            "decision": "validate",
            "reason": "Check correction limits",
            "changes": {"payload": invalid_correction},
        },
    )
    assert invalid.status_code == 422, invalid.text
    assert "grid_correction_deg" in invalid.json()["error"]["message"]

    corrected = {**incomplete, "azimuth_reference_id": "EPSG:32631", "grid_correction_deg": 2.5}
    response = await client.patch(
        url,
        headers=headers("engineer", **{"Idempotency-Key": "survey-review-valid-frame"}),
        json={
            "expected_version": 1,
            "decision": "validate",
            "reason": "Source page declares EPSG:32631 and RKB",
            "changes": {"payload": corrected},
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["payload"]["azimuth_reference"] == "grid"
    assert updated["payload"]["azimuth_reference_id"] == "EPSG:32631"
    assert updated["payload"]["grid_correction_deg"] == pytest.approx(2.5)
    assert updated["payload"]["datum"] == "rkb"
    assert updated["validation_state"] == "human_validated"
    assert updated["review_version"] == 2

    audit = await client.get(f"/api/v1/wells/{_well_id}/audit-log", headers=headers())
    assert audit.status_code == 200, audit.text
    review_entries = [item for item in audit.json()["items"] if item["action"] == "document.record_review"]
    assert len(review_entries) == 1
    assert review_entries[0]["details"]["changed_fields"] == ["payload"]
    assert "payload_sha256" in review_entries[0]["after"]
    assert "EPSG:32631" not in str(review_entries[0]["after"])


async def test_human_validated_record_can_receive_a_versioned_correction(client):
    _well_id, document_id, record = await _upload_depth_record(client)
    url = f"/api/v1/documents/{document_id}/records/{record['id']}/review"
    first = await client.patch(
        url,
        headers=headers("engineer", **{"Idempotency-Key": "review-initial-validation"}),
        json=_review_body(record, depth=1001.0),
    )
    assert first.status_code == 200, first.text
    assert first.json()["review_version"] == 2

    corrected = await client.patch(
        url,
        headers=headers("engineer", **{"Idempotency-Key": "review-human-correction"}),
        json={
            "expected_version": 2,
            "decision": "validate",
            "reason": "A second reviewer verified the exact transcription",
            "changes": {"payload": {"bit_depth_si": 1002.0}, "depth_md_si": 1002.0},
        },
    )
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["review_version"] == 3
    assert corrected.json()["validation_state"] == "human_validated"
    assert corrected.json()["payload"]["bit_depth_si"] == 1002.0
    assert corrected.json()["depth_md_si"] == 1002.0


async def test_rejection_is_audited_and_rejected_record_cannot_be_revalidated(client):
    well_id, document_id, record = await _upload_depth_record(client)
    url = f"/api/v1/documents/{document_id}/records/{record['id']}/review"
    rejected = await client.patch(
        url,
        headers=headers("engineer", **{"Idempotency-Key": "review-reject-depth-record"}),
        json={
            "expected_version": 1,
            "decision": "reject",
            "reason": "The extracted value is not supported by the source page",
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["validation_state"] == "rejected"
    assert rejected.json()["review_version"] == 2
    assert rejected.json()["validated_by"] is None
    assert rejected.json()["validated_at"] is None

    reopened = await client.patch(
        url,
        headers=headers("engineer", **{"Idempotency-Key": "review-reopen-rejected-record"}),
        json={
            "expected_version": 2,
            "decision": "validate",
            "reason": "A rejected record cannot be silently promoted again",
        },
    )
    assert reopened.status_code == 409, reopened.text
    assert "cannot move" in reopened.json()["error"]["message"]

    audit = await client.get(f"/api/v1/wells/{well_id}/audit-log", headers=headers())
    assert audit.status_code == 200, audit.text
    reviews = [item for item in audit.json()["items"] if item["action"] == "document.record_review"]
    assert len(reviews) == 1
    assert reviews[0]["details"]["decision"] == "reject"


async def test_record_review_read_returns_a_bounded_full_payload_when_default_preview_truncates(client, db):
    from sqlalchemy import select

    from drillai.db.models import ExtractedRecord

    _well_id, document_id, record = await _upload_depth_record(client)
    source_note = "source-layout-evidence-" * 500
    async with db.session() as session:
        row = (
            await session.execute(select(ExtractedRecord).where(ExtractedRecord.id == record["id"]))
        ).scalar_one()
        row.payload = {**row.payload, "source_note": source_note}

    detail = await client.get(f"/api/v1/documents/{document_id}", headers=headers())
    assert detail.status_code == 200, detail.text
    preview = next(item for item in detail.json()["records"] if item["id"] == record["id"])
    assert preview["payload_truncated"] is True

    focused = await client.get(
        f"/api/v1/documents/{document_id}/records/{record['id']}",
        headers=headers(),
    )
    assert focused.status_code == 200, focused.text
    full_preview = focused.json()
    assert full_preview["payload_truncated"] is False
    assert full_preview["payload"]["source_note"] == source_note


async def test_review_can_clear_observed_time_and_canonical_depth_explicitly(client, db):
    from datetime import UTC, datetime

    from sqlalchemy import select

    from drillai.db.models import ExtractedRecord

    _well_id, document_id, record = await _upload_depth_record(client)
    existing_time = datetime(2026, 10, 31, 12, 30, tzinfo=UTC)
    async with db.session() as session:
        row = (
            await session.execute(select(ExtractedRecord).where(ExtractedRecord.id == record["id"]))
        ).scalar_one()
        row.observed_at = existing_time
        row.depth_md_si = 999.0
        await session.commit()

    response = await client.patch(
        f"/api/v1/documents/{document_id}/records/{record['id']}/review",
        headers=headers("engineer", **{"Idempotency-Key": "review-explicit-clears"}),
        json={
            "expected_version": 1,
            "decision": "validate",
            "reason": "Remove unsupported canonical metadata after source review",
            "changes": {"observed_at": None, "depth_md_si": None},
        },
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["observed_at"] is None
    assert updated["depth_md_si"] is None
    assert updated["payload"]["bit_depth_si"] == 1000.0
    assert updated["review_version"] == 2


async def test_review_idempotency_distinguishes_omitted_payload_from_explicit_null(client):
    _well_id, document_id, record = await _upload_depth_record(client)
    url = f"/api/v1/documents/{document_id}/records/{record['id']}/review"
    request_headers = headers("engineer", **{"Idempotency-Key": "review-null-distinction"})
    omitted_payload = {
        "expected_version": 1,
        "decision": "validate",
        "reason": "Reviewed without changing the extracted payload",
    }
    first = await client.patch(url, headers=request_headers, json=omitted_payload)
    assert first.status_code == 200, first.text

    explicit_null = await client.patch(
        url,
        headers=request_headers,
        json={**omitted_payload, "changes": {"payload": None}},
    )
    assert explicit_null.status_code == 409, explicit_null.text
    assert "different request body" in explicit_null.json()["error"]["message"]


async def test_review_rejects_stale_versions_unprivileged_roles_and_cross_document_ids(client):
    _well_id, document_id, record = await _upload_depth_record(client)
    body = _review_body(record, depth=1002.0)
    url = f"/api/v1/documents/{document_id}/records/{record['id']}/review"
    reviewed = await client.patch(
        url,
        headers=headers("engineer", **{"Idempotency-Key": "review-first-version"}),
        json=body,
    )
    assert reviewed.status_code == 200, reviewed.text

    stale = await client.patch(
        url,
        headers=headers("engineer", **{"Idempotency-Key": "review-stale-version"}),
        json=body,
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["details"]["current_version"] == 2

    forbidden = await client.patch(
        url,
        headers=headers("viewer", **{"Idempotency-Key": "review-viewer-denied"}),
        json={**body, "expected_version": 2},
    )
    assert forbidden.status_code == 403, forbidden.text

    _other_well, other_document_id, _other_record = await _upload_depth_record(client, name="Other well")
    mismatched = await client.patch(
        f"/api/v1/documents/{other_document_id}/records/{record['id']}/review",
        headers=headers("engineer", **{"Idempotency-Key": "review-cross-document"}),
        json={**body, "expected_version": 2},
    )
    assert mismatched.status_code == 404, mismatched.text


async def test_two_reviewers_cannot_both_commit_the_same_record_version(client):
    _well_id, document_id, record = await _upload_depth_record(client)
    url = f"/api/v1/documents/{document_id}/records/{record['id']}/review"

    async def submit(depth: float, key: str):
        return await client.patch(
            url,
            headers=headers("engineer", **{"Idempotency-Key": key}),
            json=_review_body(record, depth=depth, reason=f"Reviewer proposed {depth} m"),
        )

    first, second = await asyncio.gather(
        submit(1003.0, "review-concurrent-one"),
        submit(1004.0, "review-concurrent-two"),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409], (first.text, second.text)
    winner = first if first.status_code == 200 else second
    loser = second if winner is first else first
    assert winner.json()["review_version"] == 2
    assert loser.json()["error"]["details"]["current_version"] == 2


async def test_review_rejects_invalid_payload_and_archived_document(client, db):
    from sqlalchemy import update

    from drillai.db.models import Document

    _well_id, document_id, record = await _upload_depth_record(client)
    url = f"/api/v1/documents/{document_id}/records/{record['id']}/review"
    invalid = await client.patch(
        url,
        headers=headers("engineer", **{"Idempotency-Key": "review-invalid-null-payload"}),
        json={
            "expected_version": 1,
            "decision": "validate",
            "reason": "Cannot clear a payload object",
            "changes": {"payload": None},
        },
    )
    assert invalid.status_code == 422, invalid.text

    oversized_number = await client.patch(
        url,
        headers=headers("engineer", **{"Idempotency-Key": "review-nonfinite-conversion"}),
        json={
            "expected_version": 1,
            "decision": "validate",
            "reason": "Reject values that cannot be represented as finite SI numbers",
            "changes": {"payload": {"bit_depth_si": 10**1000}},
        },
    )
    assert oversized_number.status_code == 422, oversized_number.text
    assert "finite number" in oversized_number.json()["error"]["message"]

    # Set up the terminal state directly because no public document-lifecycle route exists yet; the
    # review itself still goes through the real API and must refuse to mutate archived evidence.
    async with db.session() as session:
        await session.execute(update(Document).where(Document.id == document_id).values(status="archived"))
    read_only = await client.patch(
        url,
        headers=headers("engineer", **{"Idempotency-Key": "review-archived-document"}),
        json=_review_body(record, depth=1005.0),
    )
    assert read_only.status_code == 409, read_only.text
