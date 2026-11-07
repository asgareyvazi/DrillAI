"""HTTP integration tests for reviewed-source imports and immutable trajectory revisions."""

from __future__ import annotations

import csv
import io

import pytest
from sqlalchemy import select
from tests.api.conftest import headers

from drillai.db.models import Document, EvidenceLink, ExtractedRecord, Trajectory, TrajectoryStation

pytestmark = pytest.mark.asyncio


async def _create_well(client, *, name: str = "Synthetic survey well") -> tuple[str, str, str]:
    project = await client.post("/api/v1/projects", json={"name": f"{name} project"}, headers=headers())
    assert project.status_code == 201, project.text
    well = await client.post(
        "/api/v1/wells",
        json={"project_id": project.json()["id"], "name": name, "well_type": "development_producer"},
        headers=headers(),
    )
    assert well.status_code == 201, well.text
    well_id = well.json()["id"]
    bore = await client.post(
        f"/api/v1/wells/{well_id}/wellbores",
        json={"name": "Main bore", "planned_td_md_si": 3000.0},
        headers=headers(),
    )
    assert bore.status_code == 201, bore.text
    return project.json()["id"], well_id, bore.json()["id"]


async def _upload_survey_report(client, well_id: str, wellbore_id: str) -> str:
    uploaded = await client.post(
        "/api/v1/documents",
        files={"file": ("synthetic-survey.txt", b"SYNTHETIC TEST DATA - reviewed survey source.\n", "text/plain")},
        data={"well_id": well_id, "wellbore_id": wellbore_id, "doc_type": "ddr"},
        headers=headers(),
    )
    assert uploaded.status_code == 201, uploaded.text
    return uploaded.json()["document"]["id"]


def _manual_payload(wellbore_id: str, *, name: str = "Test actual survey") -> dict:
    return {
        "wellbore_id": wellbore_id,
        "name": name,
        "kind": "actual",
        "expected_current_version": 0,
        "reason": "Synthetic API-test entry; not field-validated.",
        "datum": "unknown",
        "stations": [
            {
                "md_value": 0.0,
                "md_unit": "m",
                "inclination_value": 0.0,
                "inclination_unit": "deg",
                "azimuth_value": 0.0,
                "azimuth_unit": "deg",
                "azimuth_reference": "true",
                "azimuth_reference_id": "SYNTHETIC-TRUE-NORTH",
                "source": "manual-test",
            },
            {
                "md_value": 100.0,
                "md_unit": "ft",
                "inclination_value": 5.0,
                "inclination_unit": "deg",
                "azimuth_value": 12.0,
                "azimuth_unit": "deg",
                "azimuth_reference": "true",
                "azimuth_reference_id": "SYNTHETIC-TRUE-NORTH",
                "source": "manual-test",
            },
        ],
    }


def _source_payload(
    *, md_si: float, md_source_value: float, inclination: float, azimuth: float, md_source_unit: str | None = "ft"
) -> dict:
    return {
        "md_si": md_si,
        "inclination_deg": inclination,
        "azimuth_deg": azimuth,
        "md_source_value": md_source_value,
        "md_source_unit": md_source_unit,
        "unit_provenance": {
            "md": "source_header" if md_source_unit else "inferred_default",
            "inclination": "source_header",
            "azimuth": "source_header",
        },
        "inclination_source_value": inclination,
        "inclination_source_unit": "deg",
        "azimuth_source_value": azimuth,
        "azimuth_source_unit": "deg",
        "azimuth_reference": "true",
        "azimuth_reference_id": "SYNTHETIC-TRUE-NORTH",
        "datum": "rkb",
        "source": "synthetic-gyro",
    }


async def _seed_extracted_survey_records(db, document_id: str) -> list[str]:
    async with db.session() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        rows = [
            ExtractedRecord(
                org_id=document.org_id,
                document_id=document.id,
                project_id=document.project_id,
                well_id=document.well_id,
                wellbore_id=document.wellbore_id,
                page_number=index + 1,
                record_type="survey_station",
                payload_schema_key="extracted.survey_station",
                payload_schema_version=1,
                payload=payload,
                method="synthetic-fixture",
                method_version="test-1",
                confidence=0.42,
                validation_state="extracted",
                review_version=1,
                observed_at=None,
                observed_at_basis="unknown",
                depth_md_si=None,
                depth_md_basis="unknown",
                quality_flags=["synthetic_test_data", "unit_inferred"],
                unit_context={"md_si": "m", "inclination_deg": "deg", "azimuth_deg": "deg"},
            )
            for index, payload in enumerate(
                (
                    _source_payload(md_si=0.0, md_source_value=0.0, inclination=0.0, azimuth=0.0, md_source_unit=None),
                    _source_payload(md_si=30.48, md_source_value=100.0, inclination=5.0, azimuth=12.0),
                )
            )
        ]
        session.add_all(rows)
        await session.flush()
        return [row.id for row in rows]


async def _review_record(client, document_id: str, record_id: str, *, payload: dict | None = None) -> dict:
    body: dict = {
        "expected_version": 1,
        "decision": "validate",
        "reason": "Synthetic integration-test review against the source record.",
        "changes": {"unit_inference_reviewed": True},
    }
    if payload is not None:
        body["changes"]["payload"] = payload
    response = await client.patch(
        f"/api/v1/documents/{document_id}/records/{record_id}/review",
        json=body,
        headers=headers("engineer", **{"Idempotency-Key": f"survey-review-{record_id}"}),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_manual_calculation_creates_immutable_revision_and_compare_and_swap_history(client, db):
    _project_id, well_id, wellbore_id = await _create_well(client)
    payload = _manual_payload(wellbore_id)
    key = "survey-manual-create-replay"
    first = await client.post(
        f"/api/v1/wells/{well_id}/surveys",
        json=payload,
        headers=headers("engineer", **{"Idempotency-Key": key}),
    )
    assert first.status_code == 201, first.text
    created = first.json()
    trajectory = created["trajectory"]
    assert trajectory["version"] == 1
    assert trajectory["is_current"] is True
    assert trajectory["calculation_state"] == "formula_derived"
    assert trajectory["engineering_acceptance"] == "not_established"
    assert trajectory["datum"] == "unknown"
    assert trajectory["datum_provenance"] == "engine_input"
    assert created["stations"][1]["md_si"] == pytest.approx(30.48)
    assert created["stations"][1]["raw_source"]["source_values"]["md_value"] == 100.0
    assert created["stations"][1]["raw_source"]["source_values"]["md_unit"] == "ft"
    assert created["stations"][1]["measured_at"] is None
    assert created["stations"][1]["measured_at_provenance"] == "unknown"

    replay = await client.post(
        f"/api/v1/wells/{well_id}/surveys",
        json=payload,
        headers=headers("engineer", **{"Idempotency-Key": key}),
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["trajectory"]["id"] == trajectory["id"]

    corrected_payload = {
        **payload,
        "expected_current_version": 1,
        "base_trajectory_id": trajectory["id"],
        "reason": "Synthetic correction of the second entered inclination.",
        "stations": [
            {**payload["stations"][0], "source_station_id": created["stations"][0]["id"]},
            {
                **payload["stations"][1],
                "inclination_value": 6.0,
                "source_station_id": created["stations"][1]["id"],
            },
        ],
    }
    revised = await client.post(
        f"/api/v1/wells/{well_id}/surveys",
        json=corrected_payload,
        headers=headers("engineer", **{"Idempotency-Key": "survey-manual-revision-2"}),
    )
    assert revised.status_code == 201, revised.text
    revised_trajectory = revised.json()["trajectory"]
    assert revised_trajectory["version"] == 2
    assert revised_trajectory["is_current"] is True
    assert revised.json()["stations"][1]["inclination_deg"] == pytest.approx(6.0)
    assert revised.json()["stations"][1]["raw_source"]["revision_input"]["source_values"]["inclination_value"] == 6.0

    stale = await client.post(
        f"/api/v1/wells/{well_id}/surveys",
        json=corrected_payload,
        headers=headers("engineer", **{"Idempotency-Key": "survey-stale-revision"}),
    )
    assert stale.status_code == 409, stale.text

    async with db.session() as session:
        revisions = (
            await session.execute(
                select(Trajectory).where(Trajectory.wellbore_id == wellbore_id).order_by(Trajectory.version)
            )
        ).scalars().all()
        original_stations = (
            await session.execute(
                select(TrajectoryStation).where(TrajectoryStation.trajectory_id == trajectory["id"])
            )
        ).scalars().all()
    assert [(row.version, row.is_current) for row in revisions] == [(1, False), (2, True)]
    assert len(original_stations) == 2
    assert original_stations[1].inclination_deg != pytest.approx(6.0)

    comparison = await client.get(
        f"/api/v1/wells/{well_id}/surveys/{revised_trajectory['id']}/compare/{trajectory['id']}",
        headers=headers("viewer"),
    )
    assert comparison.status_code == 200, comparison.text
    assert comparison.json()["comparison_basis"].startswith("exact canonical measured-depth equality")
    assert comparison.json()["matched_count"] == 2
    assert comparison.json()["items"][1]["delta"]["inclination_deg"] == pytest.approx(1.0)

    export = await client.get(
        f"/api/v1/wells/{well_id}/surveys/{revised_trajectory['id']}/export.csv",
        headers=headers("engineer"),
    )
    assert export.status_code == 200, export.text
    assert export.headers["content-type"].startswith("text/csv")
    assert export.headers["cache-control"] == "private, no-store"
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert len(rows) == 2
    assert rows[1]["source_md_value [source-recorded unit]"] == "100.0"
    assert rows[1]["source_md_unit [source-recorded unit]"] == "ft"
    assert rows[1]["trajectory_version [1]"] == "2"


async def test_import_rejects_unreviewed_records_then_preserves_review_provenance_and_csv(client, db):
    _project_id, well_id, wellbore_id = await _create_well(client, name="Synthetic imported survey well")
    document_id = await _upload_survey_report(client, well_id, wellbore_id)
    record_ids = await _seed_extracted_survey_records(db, document_id)
    payload = {
        "wellbore_id": wellbore_id,
        "source_document_id": document_id,
        "selected_record_ids": record_ids,
        "name": "Synthetic imported actual survey",
        "kind": "actual",
        "expected_current_version": 0,
        "reason": "Synthetic API-test import after human record review.",
        "datum": "rkb",
    }

    blocked = await client.post(
        f"/api/v1/wells/{well_id}/surveys/imports",
        json=payload,
        headers=headers("engineer", **{"Idempotency-Key": "survey-import-before-review"}),
    )
    assert blocked.status_code == 409, blocked.text
    assert "unreviewed" in blocked.json()["error"]["message"]

    before = await client.get(
        f"/api/v1/documents/{document_id}/records/{record_ids[0]}", headers=headers("engineer")
    )
    assert before.status_code == 200, before.text
    altered_snapshot = dict(before.json()["payload"])
    altered_snapshot["md_source_unit"] = "m"
    immutable = await client.patch(
        f"/api/v1/documents/{document_id}/records/{record_ids[0]}/review",
        json={
            "expected_version": 1,
            "decision": "validate",
            "reason": "Attempt to alter the source unit must be rejected.",
            "changes": {"payload": altered_snapshot, "unit_inference_reviewed": True},
        },
        headers=headers("engineer", **{"Idempotency-Key": "survey-review-source-snapshot-immutable"}),
    )
    assert immutable.status_code == 422, immutable.text
    assert "source values and source units are immutable" in immutable.json()["error"]["message"]

    corrected_payload = dict(before.json()["payload"])
    corrected_payload["inclination_deg"] = 0.5
    reviewed_first = await _review_record(client, document_id, record_ids[0], payload=corrected_payload)
    reviewed_second = await _review_record(client, document_id, record_ids[1])
    assert reviewed_first["unit_inference_reviewed"] is True
    assert reviewed_second["unit_inference_reviewed"] is True
    assert reviewed_first["validation_state"] == "human_validated"
    assert reviewed_first["payload"]["md_source_value"] == 0.0
    assert reviewed_first["payload"]["md_source_unit"] is None
    assert reviewed_first["payload"]["unit_provenance"]["md"] == "inferred_default"
    assert reviewed_first["payload"]["inclination_deg"] == pytest.approx(0.5)
    assert reviewed_second["review_version"] == 2

    imported = await client.post(
        f"/api/v1/wells/{well_id}/surveys/imports",
        json=payload,
        headers=headers("engineer", **{"Idempotency-Key": "survey-import-after-review"}),
    )
    assert imported.status_code == 201, imported.text
    trajectory = imported.json()["trajectory"]
    assert trajectory["version"] == 1
    assert trajectory["source_document_id"] == document_id
    assert trajectory["datum"] == "rkb"
    assert trajectory["datum_provenance"] == "source_document"
    first_station, second_station = imported.json()["stations"]
    assert first_station["measured_at"] is None
    assert first_station["measured_at_provenance"] == "unknown"
    assert first_station["evidence_ref"] == record_ids[0]
    assert first_station["raw_source"]["human_review"]["review_version"] == 2
    assert first_station["raw_source"]["record_payload"]["md_source_value"] == 0.0
    assert first_station["raw_source"]["record_payload"]["md_source_unit"] is None
    assert first_station["raw_source"]["record_payload"]["unit_provenance"]["md"] == "inferred_default"
    assert first_station["raw_source"]["source_values"]["md_source_unit"] is None
    assert first_station["raw_source"]["conversion_metadata"]["md"]["status"] == "not_available"
    assert "_unit_inference_review" not in first_station["raw_source"]["record_payload"]
    assert second_station["md_si"] == pytest.approx(30.48)
    assert second_station["raw_source"]["record_payload"]["md_source_value"] == 100.0
    assert second_station["raw_source"]["record_payload"]["md_source_unit"] == "ft"
    assert second_station["raw_source"]["conversion_metadata"]["md"]["factor"] == pytest.approx(0.3048)

    async with db.session() as session:
        evidence = (
            await session.execute(
                select(EvidenceLink).where(
                    EvidenceLink.subject_kind == "trajectory_station",
                    EvidenceLink.subject_id == first_station["id"],
                )
            )
        ).scalar_one()
        assert evidence.document_id == document_id
        assert evidence.evidence_id == record_ids[0]
        assert evidence.quote_verified is False
        assert "_unit_inference_review" not in (evidence.excerpt or "")

    export = await client.get(
        f"/api/v1/wells/{well_id}/surveys/{trajectory['id']}/export.csv", headers=headers("engineer")
    )
    assert export.status_code == 200, export.text
    csv_rows = list(csv.DictReader(io.StringIO(export.text)))
    assert csv_rows[0]["source_md_value [source-recorded unit]"] == "0.0"
    assert csv_rows[0]["source_md_unit [source-recorded unit]"] == ""
    assert csv_rows[1]["source_md_value [source-recorded unit]"] == "100.0"
    assert csv_rows[1]["source_md_unit [source-recorded unit]"] == "ft"
    assert csv_rows[1]["source_record_id [identifier]"] == record_ids[1]
    assert csv_rows[1]["engineering_acceptance [status]"] == "not_established"

    focused = await client.get(
        f"/api/v1/documents/{document_id}/records/{record_ids[0]}", headers=headers("engineer")
    )
    assert focused.status_code == 200, focused.text
    changed_review = dict(focused.json()["payload"])
    changed_review["inclination_deg"] = 0.75
    re_reviewed = await client.patch(
        f"/api/v1/documents/{document_id}/records/{record_ids[0]}/review",
        json={
            "expected_version": focused.json()["review_version"],
            "decision": "validate",
            "reason": "A later synthetic review changes the accepted source value.",
            "changes": {"payload": changed_review, "unit_inference_reviewed": True},
        },
        headers=headers("engineer", **{"Idempotency-Key": "survey-source-rereview-after-import"}),
    )
    assert re_reviewed.status_code == 200, re_reviewed.text

    stale_listing = await client.get(f"/api/v1/wells/{well_id}/surveys", headers=headers("engineer"))
    assert stale_listing.status_code == 200, stale_listing.text
    assert stale_listing.json()["total"] == 0
    unavailable_detail = await client.get(
        f"/api/v1/wells/{well_id}/surveys/{trajectory['id']}", headers=headers("engineer")
    )
    assert unavailable_detail.status_code == 404, unavailable_detail.text

    revision_stations = []
    for station in imported.json()["stations"]:
        accepted = station["raw_source"]["calculation"]["accepted_engine_input"]
        revision_stations.append(
            {
                "md_value": accepted["md_si"],
                "md_unit": "m",
                "inclination_value": accepted["inclination_deg"],
                "inclination_unit": "deg",
                "azimuth_value": accepted["azimuth_deg"],
                "azimuth_unit": "deg",
                "azimuth_reference": accepted["azimuth_reference"],
                "azimuth_reference_id": accepted["azimuth_reference_id"],
                "source": accepted["source"],
                "source_station_id": station["id"],
            }
        )
    stale_source_revision = await client.post(
        f"/api/v1/wells/{well_id}/surveys",
        json={
            "wellbore_id": wellbore_id,
            "name": trajectory["name"],
            "kind": trajectory["kind"],
            "expected_current_version": 1,
            "base_trajectory_id": trajectory["id"],
            "reason": "Must not silently inherit a station after source re-review.",
            "datum": "rkb",
            "stations": revision_stations,
        },
        headers=headers("engineer", **{"Idempotency-Key": "survey-revision-after-source-rereview"}),
    )
    assert stale_source_revision.status_code == 409, stale_source_revision.text
    assert "source record changed after this trajectory revision" in stale_source_revision.json()["error"]["message"]


async def test_survey_read_rbac_is_read_only_for_viewers(client):
    _project_id, well_id, wellbore_id = await _create_well(client, name="Synthetic RBAC survey well")
    payload = _manual_payload(wellbore_id, name="Viewer-readable synthetic trajectory")
    created = await client.post(
        f"/api/v1/wells/{well_id}/surveys", json=payload, headers=headers("engineer")
    )
    assert created.status_code == 201, created.text
    trajectory_id = created.json()["trajectory"]["id"]

    listing = await client.get(f"/api/v1/wells/{well_id}/surveys", headers=headers("viewer"))
    assert listing.status_code == 200, listing.text
    assert any(item["id"] == trajectory_id for item in listing.json()["items"])
    detail = await client.get(f"/api/v1/wells/{well_id}/surveys/{trajectory_id}", headers=headers("viewer"))
    assert detail.status_code == 200, detail.text
    assert detail.json()["trajectory"]["id"] == trajectory_id

    denied = await client.post(
        f"/api/v1/wells/{well_id}/surveys",
        json={**payload, "name": "Viewer must not write"},
        headers=headers("viewer"),
    )
    assert denied.status_code == 403, denied.text
