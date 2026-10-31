"""Operational reads must not turn stale provenance into current engineering input.

The rows remain in storage for audit; these tests exercise the ordinary API/read models and require
quarantined, rejected, superseded, low-confidence, cross-tenant, and scope-conflicting provenance to
fail closed across operations, events, state, timeline, and NPT.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.fixtures.fabric import Fabric

from drillai.db.models import Document, Event, ExtractedRecord, Operation, RawArtifact
from drillai.drilling.state import WellStateService


async def _source(
    fabric: Fabric,
    *,
    content: bytes = b"provenance fixture for operational read tests",
) -> tuple[str, str]:
    uploaded = await fabric.upload(
        fabric.alpha,
        content,
        filename="source.txt",
        well_id=fabric.alpha.well_id,
        wellbore_id=fabric.alpha.wellbore_id,
        section_id=fabric.alpha.section_id,
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["document"]["id"]
    async with fabric.session() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        record = ExtractedRecord(
            org_id=fabric.org_id(fabric.alpha),
            document_id=document.id,
            project_id=fabric.alpha.project_id,
            well_id=fabric.alpha.well_id,
            wellbore_id=fabric.alpha.wellbore_id,
            section_id=fabric.alpha.section_id,
            record_type="operation_row",
            payload_schema_key="extracted.operation_row",
            payload={"activity": "Drilling ahead"},
            method="rule",
            confidence=0.95,
            validation_state="rule_validated",
            validation_rule="test_fixture",
        )
        session.add(record)
        await session.commit()
        record_id = record.id
    return document_id, record_id


async def _promote_operation_and_event(
    fabric: Fabric, document_id: str, record_id: str
) -> tuple[str, str]:
    operation_response = await fabric.http.post(
        "/api/v1/operations",
        json={
            "project_id": fabric.alpha.project_id,
            "well_id": fabric.alpha.well_id,
            "wellbore_id": fabric.alpha.wellbore_id,
            "section_id": fabric.alpha.section_id,
            "name": "Drilling ahead",
            "sequence": 1,
            "operation_class": "actual",
            "npt_hours": 2.0,
            "is_productive": False,
            "source_kind": "ddr_promotion",
            "source_document_id": document_id,
            "source_record_id": record_id,
        },
        headers=fabric.alpha.headers,
    )
    assert operation_response.status_code == 201, operation_response.text
    operation_id = operation_response.json()["id"]

    event_response = await fabric.http.post(
        "/api/v1/events",
        json={
            "project_id": fabric.alpha.project_id,
            "well_id": fabric.alpha.well_id,
            "wellbore_id": fabric.alpha.wellbore_id,
            "section_id": fabric.alpha.section_id,
            "operation_id": operation_id,
            "title": "Two hours of weather downtime",
            "kind": "weather_downtime",
            "severity": "medium",
            "occurred_at": "2026-04-01T08:00:00+00:00",
            "duration_hours": 2.0,
            "npt_hours": 2.0,
            "npt_category": "weather",
            "is_npt": True,
            "source_kind": "ddr_promotion",
            "source_document_id": document_id,
            "source_record_id": record_id,
        },
        headers=fabric.alpha.headers,
    )
    assert event_response.status_code == 201, event_response.text
    return operation_id, event_response.json()["id"]


async def _assert_operational_reads_exclude(fabric: Fabric, operation_id: str, event_id: str) -> None:
    operation_list = await fabric.http.get(
        "/api/v1/operations",
        params={"well_id": fabric.alpha.well_id},
        headers=fabric.alpha.headers,
    )
    assert operation_list.status_code == 200, operation_list.text
    assert operation_list.json()["total"] == 0
    operation_detail = await fabric.http.get(
        f"/api/v1/operations/{operation_id}", headers=fabric.alpha.headers
    )
    assert operation_detail.status_code == 404, operation_detail.text

    event_list = await fabric.http.get(
        "/api/v1/events",
        params={"well_id": fabric.alpha.well_id},
        headers=fabric.alpha.headers,
    )
    assert event_list.status_code == 200, event_list.text
    assert event_list.json()["total"] == 0
    event_detail = await fabric.http.get(f"/api/v1/events/{event_id}", headers=fabric.alpha.headers)
    assert event_detail.status_code == 404, event_detail.text

    async with fabric.session() as session:
        window = await WellStateService(session, fabric.org_id(fabric.alpha)).operation_window_for(
            fabric.alpha.well_id
        )
    assert window.current is None
    assert window.next_operation is None

    state_response = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/state", headers=fabric.alpha.headers
    )
    assert state_response.status_code == 200, state_response.text
    assert state_response.json()["state"]["counts"]["operations"] == 0
    assert state_response.json()["state"]["counts"]["events"] == 0
    assert state_response.json()["state"]["npt"]["event_count"] == 0

    timeline = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/timeline",
        params={"kinds": ["operation", "event"]},
        headers=fabric.alpha.headers,
    )
    assert timeline.status_code == 200, timeline.text
    assert timeline.json()["entries"] == []

    for basis in ("events", "operations"):
        npt = await fabric.http.get(
            f"/api/v1/wells/{fabric.alpha.well_id}/npt",
            params={"basis": basis, "include_offsets": "false"},
            headers=fabric.alpha.headers,
        )
        assert npt.status_code == 200, npt.text
        assert npt.json()["npt"]["total_hours"] == 0.0
        assert npt.json()["npt"]["cases"] == []


@pytest.mark.parametrize(
    "invalidation",
    [
        "rejected",
        "superseded",
        "low_confidence",
        "confidence_above_max",
        "human_confidence_above_max",
        "quarantined",
    ],
)
async def test_ineligible_source_is_hidden_from_operational_read_models(
    fabric: Fabric, invalidation: str
) -> None:
    document_id, record_id = await _source(fabric)
    operation_id, event_id = await _promote_operation_and_event(fabric, document_id, record_id)

    async with fabric.session() as session:
        if invalidation == "quarantined":
            document = await session.get(Document, document_id)
            assert document is not None and document.raw_artifact_id is not None
            artifact = await session.get(RawArtifact, document.raw_artifact_id)
            assert artifact is not None
            artifact.is_quarantined = True
        else:
            record = await session.get(ExtractedRecord, record_id)
            assert record is not None
            if invalidation == "low_confidence":
                record.confidence = 0.59
            elif invalidation in {"confidence_above_max", "human_confidence_above_max"}:
                record.confidence = 1.01
                if invalidation == "human_confidence_above_max":
                    record.validation_state = "human_validated"
            else:
                record.validation_state = invalidation
        await session.commit()

    # These are retained in the database; only ordinary engineering/read paths fail closed.
    async with fabric.session() as session:
        assert await session.get(Operation, operation_id) is not None
        assert await session.get(Event, event_id) is not None
        assert await session.get(ExtractedRecord, record_id) is not None

    await _assert_operational_reads_exclude(fabric, operation_id, event_id)


async def test_operation_event_list_excludes_an_ineligible_event_but_keeps_the_valid_operation(
    fabric: Fabric,
) -> None:
    operation_document_id, operation_record_id = await _source(fabric)
    operation_id, event_id = await _promote_operation_and_event(
        fabric, operation_document_id, operation_record_id
    )
    event_document_id, event_record_id = await _source(
        fabric, content=b"separate source record for event-only invalidation"
    )

    async with fabric.session() as session:
        event = await session.get(Event, event_id)
        record = await session.get(ExtractedRecord, event_record_id)
        assert event is not None and record is not None
        event.source_document_id = event_document_id
        event.source_record_id = event_record_id
        record.validation_state = "rejected"
        await session.commit()

    operation_detail = await fabric.http.get(
        f"/api/v1/operations/{operation_id}", headers=fabric.alpha.headers
    )
    assert operation_detail.status_code == 200, operation_detail.text
    assert operation_detail.json()["events"] == []
    assert (
        await fabric.http.get(f"/api/v1/events/{event_id}", headers=fabric.alpha.headers)
    ).status_code == 404
    event_list = await fabric.http.get(
        "/api/v1/events",
        params={"well_id": fabric.alpha.well_id},
        headers=fabric.alpha.headers,
    )
    assert event_list.status_code == 200, event_list.text
    assert event_list.json()["total"] == 0

    state = (
        await fabric.http.get(
            f"/api/v1/wells/{fabric.alpha.well_id}/state", headers=fabric.alpha.headers
        )
    ).json()["state"]
    assert state["counts"]["operations"] == 1
    assert state["counts"]["events"] == 0
    timeline = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/timeline",
        params={"kinds": ["operation", "event"]},
        headers=fabric.alpha.headers,
    )
    assert [entry["kind"] for entry in timeline.json()["entries"]] == ["operation"]


@pytest.mark.parametrize("scope_damage", ["cross_tenant_document", "document_scope", "record_scope"])
async def test_broken_tenant_or_scope_binding_is_hidden_from_operational_reads(
    fabric: Fabric, scope_damage: str
) -> None:
    document_id, record_id = await _source(fabric)
    operation_id, event_id = await _promote_operation_and_event(fabric, document_id, record_id)
    if scope_damage == "cross_tenant_document":
        foreign = await fabric.upload(
            fabric.bravo,
            b"a different tenant document",
            well_id=fabric.bravo.well_id,
            wellbore_id=fabric.bravo.wellbore_id,
            section_id=fabric.bravo.section_id,
        )
        assert foreign.status_code == 201, foreign.text
        damaged_document_id = foreign.json()["document"]["id"]

    async with fabric.session() as session:
        operation = await session.get(Operation, operation_id)
        event = await session.get(Event, event_id)
        assert operation is not None and event is not None
        if scope_damage == "cross_tenant_document":
            operation.source_document_id = damaged_document_id
            event.source_document_id = damaged_document_id
        elif scope_damage == "document_scope":
            document = await session.get(Document, document_id)
            assert document is not None
            document.section_id = fabric.bravo.section_id
        else:
            record = await session.get(ExtractedRecord, record_id)
            assert record is not None
            record.section_id = fabric.bravo.section_id
        await session.commit()

    await _assert_operational_reads_exclude(fabric, operation_id, event_id)


async def test_human_validation_can_override_a_low_extraction_confidence(fabric: Fabric) -> None:
    document_id, record_id = await _source(fabric)
    async with fabric.session() as session:
        record = await session.get(ExtractedRecord, record_id)
        assert record is not None
        record.confidence = 0.2
        record.validation_state = "human_validated"
        await session.commit()

    operation = await fabric.http.post(
        "/api/v1/operations",
        json={
            "project_id": fabric.alpha.project_id,
            "well_id": fabric.alpha.well_id,
            "wellbore_id": fabric.alpha.wellbore_id,
            "section_id": fabric.alpha.section_id,
            "name": "human-reviewed operation",
            "source_kind": "ddr_promotion",
            "source_document_id": document_id,
            "source_record_id": record_id,
        },
        headers=fabric.alpha.headers,
    )
    assert operation.status_code == 201, operation.text
    listed = await fabric.http.get(
        "/api/v1/operations",
        params={"well_id": fabric.alpha.well_id},
        headers=fabric.alpha.headers,
    )
    assert listed.status_code == 200, listed.text
    assert [row["id"] for row in listed.json()["items"]] == [operation.json()["id"]]


async def test_human_validation_does_not_override_out_of_range_confidence(fabric: Fabric) -> None:
    document_id, record_id = await _source(fabric)
    async with fabric.session() as session:
        record = await session.get(ExtractedRecord, record_id)
        assert record is not None
        record.confidence = 1.01
        record.validation_state = "human_validated"
        await session.commit()

    operation = await fabric.http.post(
        "/api/v1/operations",
        json={
            "project_id": fabric.alpha.project_id,
            "well_id": fabric.alpha.well_id,
            "wellbore_id": fabric.alpha.wellbore_id,
            "section_id": fabric.alpha.section_id,
            "name": "must not link an invalid confidence score",
            "source_kind": "ddr_promotion",
            "source_document_id": document_id,
            "source_record_id": record_id,
        },
        headers=fabric.alpha.headers,
    )
    assert operation.status_code == 422, operation.text

    async with fabric.session() as session:
        assert (
            await session.execute(
                select(Operation).where(Operation.source_record_id == record_id)
            )
        ).scalars().all() == []


async def test_source_record_below_confidence_floor_cannot_be_linked(fabric: Fabric) -> None:
    document_id, record_id = await _source(fabric)
    async with fabric.session() as session:
        record = await session.get(ExtractedRecord, record_id)
        assert record is not None
        record.confidence = 0.59
        await session.commit()

    payload = {
        "project_id": fabric.alpha.project_id,
        "well_id": fabric.alpha.well_id,
        "wellbore_id": fabric.alpha.wellbore_id,
        "section_id": fabric.alpha.section_id,
        "source_kind": "ddr_promotion",
        "source_document_id": document_id,
        "source_record_id": record_id,
    }
    operation = await fabric.http.post(
        "/api/v1/operations",
        json={"name": "must be reviewed", **payload},
        headers=fabric.alpha.headers,
    )
    assert operation.status_code == 422, operation.text
    assert "human validation is required" in operation.json()["error"]["message"]

    event = await fabric.http.post(
        "/api/v1/events",
        json={
            "title": "must be reviewed",
            "kind": "observation",
            "occurred_at": "2026-04-01T08:00:00+00:00",
            **payload,
        },
        headers=fabric.alpha.headers,
    )
    assert event.status_code == 422, event.text
    assert "human validation is required" in event.json()["error"]["message"]

    operations = await fabric.http.get(
        "/api/v1/operations", params={"well_id": fabric.alpha.well_id}, headers=fabric.alpha.headers
    )
    events = await fabric.http.get(
        "/api/v1/events", params={"well_id": fabric.alpha.well_id}, headers=fabric.alpha.headers
    )
    assert operations.json()["total"] == 0
    assert events.json()["total"] == 0
