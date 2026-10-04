"""Events as first-class records: lifecycle, NPT accounting, cause provenance, tenancy.

The distinctions this suite protects are the ones the platform was previously unable to express:

* an **event** (what happened) is not an **NPT category** (how it is accounted for) — a milestone has no
  NPT, and a category can exist with no hours;
* a **recorded** cause is not an **inferred** one, and neither is the same as **unknown** — the DDR
  promotion used to write the report's own text into ``root_cause``, which read as a diagnosis;
* an event attributed to an **operation** inherits that operation's bore and section, so the operation's
  own event list is complete.

Everything is asserted over HTTP against the real application, with two organizations.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from tests.fixtures.fabric import Fabric

from drillai.db.models import AuditLog

DDR_RECORD = {
    "title": "Kick while drilling the 12-1/4\" section",
    "kind": "kick",
    "severity": "high",
    "occurred_at": "2026-03-15T07:15:00+00:00",
}


async def _create(fabric: Fabric, tenant, **payload) -> dict:
    response = await fabric.http.post(
        "/api/v1/events",
        json={"well_id": tenant.well_id, **DDR_RECORD, **payload},
        headers=tenant.headers,
    )
    return response


async def test_create_an_event(fabric: Fabric) -> None:
    response = await _create(fabric, fabric.alpha)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"].startswith("evn_")
    assert body["well_id"] == fabric.alpha.well_id
    assert body["status"] == "open"
    assert body["kind"] == "kick"
    assert body["is_npt"] is False, "an incident is not automatically an NPT charge"
    assert body["npt_category"] is None
    assert body["cause_basis"] == "unknown"
    assert body["classification_source"] == "recorded"
    assert body["source_kind"] == "manual"
    assert set(body["allowed_transitions"]) == {"acknowledged", "investigating", "closed", "cancelled"}


async def test_an_event_must_resolve_to_a_well(fabric: Fabric) -> None:
    response = await fabric.http.post(
        "/api/v1/events",
        json={**DDR_RECORD, "project_id": fabric.alpha.project_id},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert "must resolve to a well" in response.json()["error"]["message"]


async def test_events_are_listed_in_chronological_order(fabric: Fabric) -> None:
    for offset, hour in enumerate(["09:00", "07:00", "08:00"]):
        response = await _create(
            fabric,
            fabric.alpha,
            title=f"Observation {offset}",
            kind="observation",
            occurred_at=f"2026-03-15T{hour}:00+00:00",
        )
        assert response.status_code == 201, response.text
    listing = await fabric.http.get(
        "/api/v1/events", params={"well_id": fabric.alpha.well_id}, headers=fabric.alpha.headers
    )
    assert listing.status_code == 200
    times = [row["occurred_at"] for row in listing.json()["items"]]
    assert times == sorted(times)
    assert listing.json()["total"] == 3


# --------------------------------------------------------------------------- kind is not NPT


async def test_a_milestone_is_an_event_with_no_npt_accounting(fabric: Fabric) -> None:
    response = await _create(
        fabric, fabric.alpha, kind="milestone", title="Section TD reached", severity="low"
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["kind"] == "milestone"
    assert body["is_npt"] is False
    assert body["npt_category"] is None
    assert body["npt_hours"] is None


async def test_npt_is_charged_with_hours_and_a_category(fabric: Fabric) -> None:
    response = await _create(
        fabric,
        fabric.alpha,
        title="Waiting on weather",
        kind="weather_downtime",
        npt_category="weather",
        npt_hours=6.0,
        severity="medium",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["is_npt"] is True
    assert body["npt_hours"] == 6.0
    assert body["npt_category"] == "weather"


async def test_a_category_without_hours_is_not_charged(fabric: Fabric) -> None:
    """The category says how the loss *would* be booked; zero hours means there is no loss yet."""
    response = await _create(
        fabric,
        fabric.alpha,
        title="Reamer tight spot",
        kind="tight_hole",
        npt_category="hole_problem",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["npt_category"] == "hole_problem"
    assert body["is_npt"] is False


async def test_an_unknown_npt_category_is_refused_with_the_list(fabric: Fabric) -> None:
    response = await _create(fabric, fabric.alpha, npt_category="not_a_category")
    assert response.status_code == 422, response.text
    details = response.json()["error"]["details"]
    assert details["field"] == "npt_category"
    assert "weather" in details["allowed"]


async def test_the_route_does_not_map_kind_to_category(fabric: Fabric) -> None:
    """A stuck pipe is a ``stuck_pipe`` event; its category is not derived from its kind."""
    response = await _create(fabric, fabric.alpha, kind="stuck_pipe", title="Stuck at 2410 m")
    assert response.status_code == 201, response.text
    assert response.json()["npt_category"] is None


# --------------------------------------------------------------------------- cause provenance


async def test_a_cause_with_no_basis_is_unknown_and_not_written_as_a_diagnosis(fabric: Fabric) -> None:
    """The DDR-promotion defect, as a rule: text is not a cause.

    A report saying "The string parted at 2410 m" describes the event. Storing that sentence as
    ``root_cause`` made it read as the report's diagnosis. Unless the source states a cause, the field
    stays empty and ``cause_basis`` says nobody has established one.
    """
    response = await _create(
        fabric,
        fabric.alpha,
        title="Stuck pipe",
        kind="stuck_pipe",
        description="The string parted at 2410 m while reaming.",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["description"].startswith("The string parted")
    assert body["root_cause"] is None
    assert body["cause_basis"] == "unknown"


async def test_a_cause_asserted_from_a_manual_report_must_come_from_the_reporter(fabric: Fabric) -> None:
    """``recorded`` is available to a person reporting what they saw, and to a document."""
    response = await _create(
        fabric,
        fabric.alpha,
        title="Lost circulation",
        kind="lost_circulation",
        root_cause="formation fractured at 2380 m when the mud weight was raised",
        cause_basis="recorded",
        source_kind="manual",
    )
    assert response.status_code == 201, response.text
    assert response.json()["cause_basis"] == "recorded"


async def test_a_cause_may_not_be_relabelled_recorded_by_an_unattributed_source(fabric: Fabric) -> None:
    """A connector reading is not a person's statement, so it cannot claim ``recorded``."""
    response = await _create(
        fabric,
        fabric.alpha,
        title="Lost circulation",
        kind="lost_circulation",
        root_cause="the formation took the mud",
        cause_basis="recorded",
        source_kind="connector",
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert "recorded" in error["message"]
    assert error["details"]["hint"].startswith("use cause_basis='inferred'")


async def test_an_inferred_cause_must_cite_its_evidence(fabric: Fabric) -> None:
    """§31/§32: an inference with no citation is indistinguishable from a guess."""
    response = await _create(
        fabric,
        fabric.alpha,
        title="Vibration event",
        kind="vibration",
        root_cause="inferred BHA whirl from the vibration signature",
        cause_basis="inferred",
    )
    assert response.status_code == 422, response.text
    assert "must cite the evidence" in response.json()["error"]["message"]


async def test_an_inferred_cause_with_evidence_is_accepted_and_labelled(fabric: Fabric) -> None:
    document = await fabric.upload(fabric.alpha, b"vibration log", well_id=fabric.alpha.well_id)
    document_id = document.json()["document"]["id"]
    response = await _create(
        fabric,
        fabric.alpha,
        title="Vibration event",
        kind="vibration",
        severity="medium",
        root_cause="inferred BHA whirl from the vibration signature",
        cause_basis="inferred",
        evidence_ref=document_id,
        source_kind="system",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["cause_basis"] == "inferred"
    assert body["evidence_ref"] == document_id
    assert body["classification_source"] == "recorded"


async def test_a_later_correction_may_downgrade_a_recorded_cause(fabric: Fabric) -> None:
    """Moving away from ``recorded`` is allowed: a claim can be withdrawn, not manufactured."""
    created = (
        await _create(
            fabric,
            fabric.alpha,
            title="Lost circulation",
            kind="lost_circulation",
            root_cause="formation fractured",
            cause_basis="recorded",
            source_kind="manual",
        )
    ).json()
    response = await fabric.http.patch(
        f"/api/v1/events/{created['id']}",
        json={
            "expected_updated_at": created["updated_at"],
            "reason": "the same cause is claimed in two reports; treat it as unconfirmed",
            "changes": {"cause_basis": "unknown"},
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["cause_basis"] == "unknown"


async def test_a_promoted_event_is_marked_as_derived_not_recorded(fabric: Fabric) -> None:
    """A classification the platform derived may not present itself as the report's own statement."""
    response = await _create(
        fabric,
        fabric.alpha,
        title="Automatic classification",
        kind="kick",
        classification_source="derived",
        source_kind="ddr_promotion",
    )
    assert response.status_code == 201, response.text
    assert response.json()["classification_source"] == "derived"
    assert response.json()["source_kind"] == "ddr_promotion"


# --------------------------------------------------------------------------- lifecycle


async def test_the_lifecycle_moves_forward_only(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha)).json()
    investigating = await fabric.http.post(
        f"/api/v1/events/{created['id']}/transition",
        json={"status": "investigating", "expected_updated_at": created["updated_at"]},
        headers=fabric.alpha.headers,
    )
    assert investigating.status_code == 200, investigating.text
    closed = await fabric.http.post(
        f"/api/v1/events/{created['id']}/transition",
        json={
            "status": "closed",
            "expected_updated_at": investigating.json()["updated_at"],
            "reason": "string recovered; 6 h lost",
        },
        headers=fabric.alpha.headers,
    )
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["status"] == "closed"
    assert body["ended_at"] is not None
    assert body["allowed_transitions"] == []

    reopened = await fabric.http.post(
        f"/api/v1/events/{created['id']}/transition",
        json={"status": "open", "expected_updated_at": body["updated_at"]},
        headers=fabric.alpha.headers,
    )
    assert reopened.status_code == 422, reopened.text
    assert reopened.json()["error"]["details"]["allowed"] == []


async def test_closing_requires_a_reason(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha)).json()
    response = await fabric.http.post(
        f"/api/v1/events/{created['id']}/transition",
        json={"status": "closed", "expected_updated_at": created["updated_at"]},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert "requires a reason" in response.json()["error"]["message"]


async def test_a_cancelled_event_keeps_its_row_and_its_reason(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha)).json()
    cancelled = await fabric.http.post(
        f"/api/v1/events/{created['id']}/transition",
        json={
            "status": "cancelled",
            "expected_updated_at": created["updated_at"],
            "reason": "raised against the wrong well; the event belongs to ALPHA-2",
        },
        headers=fabric.alpha.headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    still_there = await fabric.http.get(f"/api/v1/events/{created['id']}", headers=fabric.alpha.headers)
    assert still_there.status_code == 200
    async with fabric.session() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.resource_id == created["id"]))
        ).scalars().all()
    assert [row.action for row in rows] == ["event.create", "event.transition"]
    assert "wrong well" in rows[-1].details["reason"]


async def test_there_is_no_delete_route(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha)).json()
    response = await fabric.http.delete(
        f"/api/v1/events/{created['id']}", headers=fabric.alpha.headers
    )
    assert response.status_code == 405


# --------------------------------------------------------------------------- corrections


async def test_a_correction_records_before_and_after(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha, severity="low")).json()
    response = await fabric.http.patch(
        f"/api/v1/events/{created['id']}",
        json={
            "expected_updated_at": created["updated_at"],
            "reason": "the supervisor classified it as high after the review",
            "changes": {"severity": "high"},
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["severity"] == "high"
    assert response.json()["changes"] == [{"field": "severity", "before": "low", "after": "high"}]


async def test_a_stale_correction_is_a_conflict(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha)).json()
    first = await fabric.http.patch(
        f"/api/v1/events/{created['id']}",
        json={
            "expected_updated_at": created["updated_at"],
            "reason": "first",
            "changes": {"severity": "medium"},
        },
        headers=fabric.alpha.headers,
    )
    assert first.status_code == 200, first.text
    second = await fabric.http.patch(
        f"/api/v1/events/{created['id']}",
        json={
            "expected_updated_at": created["updated_at"],
            "reason": "second, from a stale screen",
            "changes": {"severity": "critical"},
        },
        headers=fabric.alpha.headers,
    )
    assert second.status_code == 409, second.text
    current = await fabric.http.get(f"/api/v1/events/{created['id']}", headers=fabric.alpha.headers)
    assert current.json()["severity"] == "medium"


async def test_an_event_status_is_not_an_editable_field(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha)).json()
    response = await fabric.http.patch(
        f"/api/v1/events/{created['id']}",
        json={
            "expected_updated_at": created["updated_at"],
            "reason": "trying to skip the lifecycle",
            "changes": {"status": "closed"},
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["fields"] == ["status"]


# --------------------------------------------------------------------------- operation attribution


async def test_an_event_inherits_its_operation_bore_and_section(fabric: Fabric) -> None:
    """The operation is the anchor: its own event list must contain the event."""
    operation = await fabric.http.post(
        "/api/v1/operations",
        json={
            "name": "Drilling ahead",
            "kind": "drilling",
            "well_id": fabric.alpha.well_id,
            "wellbore_id": fabric.alpha.wellbore_id,
            "section_id": fabric.alpha.section_id,
        },
        headers=fabric.alpha.headers,
    )
    assert operation.status_code == 201, operation.text
    response = await fabric.http.post(
        "/api/v1/events",
        json={
            "title": "Stuck pipe",
            "kind": "stuck_pipe",
            "well_id": fabric.alpha.well_id,
            "operation_id": operation.json()["id"],
            "occurred_at": "2026-03-15T08:00:00+00:00",
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["operation_id"] == operation.json()["id"]
    assert body["wellbore_id"] == fabric.alpha.wellbore_id
    assert body["section_id"] == fabric.alpha.section_id

    detail = await fabric.http.get(
        f"/api/v1/operations/{operation.json()['id']}", headers=fabric.alpha.headers
    )
    assert [row["id"] for row in detail.json()["events"]] == [body["id"]]


async def test_an_operation_on_another_bore_is_refused(fabric: Fabric) -> None:
    """The event says one bore, the operation it names is on another: that is a contradiction.

    Both rows exist and belong to this tenant. Accepting the combination would put the event in the
    operation's list while filing it against a different hole, so the operation's own view of what
    happened to it would be wrong.
    """
    other_well = await fabric.add_well(fabric.alpha, "ALPHA-EV")
    other_bore = await fabric.add_wellbore(fabric.alpha, other_well, "Other bore")
    operation = await fabric.http.post(
        "/api/v1/operations",
        json={
            "name": "Drilling ahead",
            "kind": "drilling",
            "well_id": other_well,
            "wellbore_id": other_bore,
        },
        headers=fabric.alpha.headers,
    )
    assert operation.status_code == 201, operation.text
    response = await fabric.http.post(
        "/api/v1/events",
        json={
            "title": "Stuck pipe",
            "kind": "stuck_pipe",
            "well_id": fabric.alpha.well_id,
            "wellbore_id": fabric.alpha.wellbore_id,
            "operation_id": operation.json()["id"],
            "occurred_at": "2026-03-15T08:00:00+00:00",
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert "different wellbore" in response.json()["error"]["message"]


async def test_an_unknown_operation_is_not_found(fabric: Fabric) -> None:
    response = await _create(fabric, fabric.alpha, operation_id="opr_does_not_exist")
    assert response.status_code == 404, response.text


# --------------------------------------------------------------------------- documents and tenancy


async def test_linking_the_document_an_event_was_reported_in(fabric: Fabric) -> None:
    document = await fabric.upload(
        fabric.alpha, b"the report that describes the kick", well_id=fabric.alpha.well_id
    )
    document_id = document.json()["document"]["id"]
    created = (await _create(fabric, fabric.alpha)).json()
    response = await fabric.http.post(
        f"/api/v1/events/{created['id']}/document",
        json={"document_id": document_id},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["source_document_id"] == document_id


async def test_linking_another_wells_document_is_refused(fabric: Fabric) -> None:
    other_well = await fabric.add_well(fabric.alpha, "ALPHA-EV2")
    document = await fabric.upload(fabric.alpha, b"a different well's report", well_id=other_well)
    created = (await _create(fabric, fabric.alpha)).json()
    response = await fabric.http.post(
        f"/api/v1/events/{created['id']}/document",
        json={"document_id": document.json()["document"]["id"]},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert "different well" in response.json()["error"]["message"]


async def test_linking_another_tenants_document_is_not_found(fabric: Fabric) -> None:
    document = await fabric.upload(
        fabric.bravo, b"bravo's report", well_id=fabric.bravo.well_id
    )
    created = (await _create(fabric, fabric.alpha)).json()
    response = await fabric.http.post(
        f"/api/v1/events/{created['id']}/document",
        json={"document_id": document.json()["document"]["id"]},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 404, response.text


async def test_another_tenant_cannot_read_or_list_an_event(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha)).json()
    assert (
        await fabric.http.get(f"/api/v1/events/{created['id']}", headers=fabric.bravo.headers)
    ).status_code == 404
    listing = await fabric.http.get(
        "/api/v1/events", params={"well_id": fabric.alpha.well_id}, headers=fabric.bravo.headers
    )
    assert listing.status_code == 200
    assert listing.json()["items"] == []


@pytest.mark.parametrize("field", ["well_id", "wellbore_id", "project_id"])
async def test_a_scope_reference_of_another_tenant_never_resolves(fabric: Fabric, field: str) -> None:
    response = await fabric.http.post(
        "/api/v1/events",
        json={
            "title": "Hostile",
            "kind": "observation",
            "well_id": fabric.bravo.well_id,
            field: getattr(fabric.bravo, field),
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code in {403, 404}, response.text


async def test_the_well_listing_for_one_well_excludes_other_wells(fabric: Fabric) -> None:
    other_well = await fabric.add_well(fabric.alpha, "ALPHA-OTHER")
    await _create(fabric, fabric.alpha, title="On well one")
    response = await fabric.http.post(
        "/api/v1/events",
        json={
            "title": "On well two",
            "kind": "observation",
            "well_id": other_well,
            "occurred_at": "2026-03-15T10:00:00+00:00",
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 201, response.text
    listing = await fabric.http.get(
        "/api/v1/events", params={"well_id": fabric.alpha.well_id}, headers=fabric.alpha.headers
    )
    assert [row["title"] for row in listing.json()["items"]] == ["On well one"]


async def test_events_are_never_deleted_by_the_service() -> None:
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[2] / "src" / "drillai" / "operations" / "events.py"
    ).read_text()
    assert "session.delete" not in source
    assert "DELETE FROM" not in source
