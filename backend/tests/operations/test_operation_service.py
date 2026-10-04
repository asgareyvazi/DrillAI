"""Operations as first-class records: creation, corrections, transitions, sequencing, tenancy.

Each test corresponds to a rule the platform must not be able to break, and most of them are rules that
*were* breakable: there was no way to create an operation through the API at all before CP8, so the only
operations in any database were the ones a DDR parser happened to produce, with no correction path and
no sequencing check.

The rules asserted here:

* an operation always resolves to a hole (wellbore) and a well — not to a well alone;
* a plan may not carry an actual start, because a plan is not history;
* a correction needs the version it corrects and a reason, and both are recorded;
* the sequence is an invariant: no self-reference, no predecessor on another bore, no cycles;
* nothing is deleted — an abandonment is a transition with a reason;
* every mutation writes an audit entry naming what changed;
* a second tenant cannot see, create against, or link to the first tenant's rows.
"""

from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import select
from tests.fixtures.fabric import Fabric

from drillai.db.models import AuditLog


def _scope(fabric: Fabric, tenant, **overrides) -> dict:
    """The scope fields for a tenant's tree, with the wellbore by default."""
    base = {
        "project_id": tenant.project_id,
        "well_id": tenant.well_id,
        "wellbore_id": tenant.wellbore_id,
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


async def _create(fabric: Fabric, tenant, **payload) -> dict:
    response = await fabric.http.post(
        "/api/v1/operations",
        json={"name": "Drilling ahead", "kind": "drilling", **_scope(fabric, tenant), **payload},
        headers=tenant.headers,
    )
    return response


# --------------------------------------------------------------------------- creation


async def test_create_an_operation(fabric: Fabric) -> None:
    response = await _create(fabric, fabric.alpha, sequence=1)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["id"].startswith("opr_")
    assert body["well_id"] == fabric.alpha.well_id
    assert body["wellbore_id"] == fabric.alpha.wellbore_id
    assert body["project_id"] == fabric.alpha.project_id, "the project was derived from the well"
    assert body["status"] == "completed", "an actual operation with no stated status is finished work"
    assert body["operation_class"] == "actual"
    assert body["is_planned"] is False
    assert body["source_kind"] == "manual"
    assert body["allowed_transitions"] == []


async def test_a_planned_operation_starts_planned_and_may_move(fabric: Fabric) -> None:
    response = await _create(fabric, fabric.alpha, operation_class="plan", sequence=1)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "planned"
    assert body["is_planned"] is True
    assert set(body["allowed_transitions"]) == {"ready", "in_progress", "cancelled"}


async def test_a_plan_may_not_carry_an_actual_start(fabric: Fabric) -> None:
    """§19/§20: planned data must never be recorded as if it had happened."""
    response = await _create(
        fabric,
        fabric.alpha,
        operation_class="plan",
        sequence=1,
        actual_start="2026-03-15T06:00:00+00:00",
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert "may not carry actual_start" in error["message"]
    assert error["details"]["field"] == "actual_start"


async def test_an_actual_operation_may_carry_the_plan_it_executed(fabric: Fabric) -> None:
    """The asymmetry is deliberate: comparing actual against plan is the point of having both."""
    response = await _create(
        fabric,
        fabric.alpha,
        sequence=1,
        planned_start="2026-03-15T06:00:00+00:00",
        planned_end="2026-03-15T14:00:00+00:00",
        actual_start="2026-03-15T06:30:00+00:00",
        actual_end="2026-03-15T15:00:00+00:00",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["planned_start"].startswith("2026-03-15T06:00")
    assert body["actual_start"].startswith("2026-03-15T06:30")


async def test_an_operation_must_name_a_wellbore(fabric: Fabric) -> None:
    """A well alone is not a place an operation happens."""
    response = await fabric.http.post(
        "/api/v1/operations",
        json={
            "name": "Drilling ahead",
            "project_id": fabric.alpha.project_id,
            "well_id": fabric.alpha.well_id,
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert "must name a wellbore" in response.json()["error"]["message"]


async def test_unknown_vocabulary_values_are_refused_with_the_list(fabric: Fabric) -> None:
    response = await _create(fabric, fabric.alpha, sequence=1, kind="not_a_kind")
    assert response.status_code == 422, response.text
    details = response.json()["error"]["details"]
    assert details["field"] == "kind"
    assert "drilling" in details["allowed"]


async def test_sequence_defaults_to_one_past_the_highest(fabric: Fabric) -> None:
    first = await _create(fabric, fabric.alpha)
    second = await _create(fabric, fabric.alpha)
    assert first.json()["sequence"] == 1
    assert second.json()["sequence"] == 2


async def test_creation_is_idempotent_under_an_idempotency_key(fabric: Fabric) -> None:
    """The same key twice is one operation; a different key is a second one."""
    payload = {"name": "Drilling ahead", "kind": "drilling", **_scope(fabric, fabric.alpha)}
    headers = {**fabric.alpha.headers, "Idempotency-Key": "op-key-1"}
    first = await fabric.http.post("/api/v1/operations", json=payload, headers=headers)
    second = await fabric.http.post("/api/v1/operations", json=payload, headers=headers)
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    third = await fabric.http.post(
        "/api/v1/operations",
        json=payload,
        headers={**fabric.alpha.headers, "Idempotency-Key": "op-key-2"},
    )
    assert third.json()["id"] != first.json()["id"]


async def test_the_same_key_with_a_different_body_is_a_conflict(fabric: Fabric) -> None:
    headers = {**fabric.alpha.headers, "Idempotency-Key": "op-key-3"}
    first = await fabric.http.post(
        "/api/v1/operations",
        json={"name": "Drilling ahead", "kind": "drilling", **_scope(fabric, fabric.alpha)},
        headers=headers,
    )
    assert first.status_code == 201, first.text
    second = await fabric.http.post(
        "/api/v1/operations",
        json={"name": "Reaming", "kind": "reaming", **_scope(fabric, fabric.alpha)},
        headers=headers,
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["retryable"] is False


# --------------------------------------------------------------------------- corrections


async def test_a_correction_records_what_changed(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()
    response = await fabric.http.patch(
        f"/api/v1/operations/{created['id']}",
        json={
            "expected_updated_at": created["updated_at"],
            "reason": "duration was transcribed as 8.5 h; the report says 9.5 h",
            "changes": {"actual_duration_hours": 9.5},
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["actual_duration_hours"] == 9.5
    assert body["changes"] == [
        {"field": "actual_duration_hours", "before": None, "after": 9.5}
    ]


async def test_a_correction_without_the_version_is_refused(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()
    response = await fabric.http.patch(
        f"/api/v1/operations/{created['id']}",
        json={"reason": "no version", "changes": {"actual_duration_hours": 9.5}},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text


async def test_a_stale_version_is_a_conflict_not_a_silent_overwrite(fabric: Fabric) -> None:
    """Two people editing the same operation: the second is told, not obeyed."""
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()
    stale = created["updated_at"]

    first = await fabric.http.patch(
        f"/api/v1/operations/{created['id']}",
        json={
            "expected_updated_at": stale,
            "reason": "first correction",
            "changes": {"actual_duration_hours": 9.5},
        },
        headers=fabric.alpha.headers,
    )
    assert first.status_code == 200, first.text

    second = await fabric.http.patch(
        f"/api/v1/operations/{created['id']}",
        json={
            "expected_updated_at": stale,
            "reason": "second correction from a stale screen",
            "changes": {"remarks": "should not land"},
        },
        headers=fabric.alpha.headers,
    )
    assert second.status_code == 409, second.text
    error = second.json()["error"]
    assert error["details"]["current_updated_at"] != error["details"]["expected_updated_at"]
    assert error["retryable"] is False

    current = await fabric.http.get(
        f"/api/v1/operations/{created['id']}", headers=fabric.alpha.headers
    )
    assert current.json()["remarks"] is None, "the stale write landed"


async def test_a_correction_needs_a_reason(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()
    response = await fabric.http.patch(
        f"/api/v1/operations/{created['id']}",
        json={"expected_updated_at": created["updated_at"], "changes": {"remarks": "x"}},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text


async def test_a_field_outside_the_owned_set_is_named_not_dropped(fabric: Fabric) -> None:
    """§7: a caller that sends a field it may not set is told *which* field, not silently ignored."""
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()
    response = await fabric.http.patch(
        f"/api/v1/operations/{created['id']}",
        json={
            "expected_updated_at": created["updated_at"],
            "reason": "promote the plan",
            "changes": {"operation_class": "actual"},
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    details = response.json()["error"]["details"]
    assert details["fields"] == ["operation_class"]
    assert details["owned_by"] == {"operation_class": "the record's class"}
    assert "status" not in details["allowed"], "status is not an editable field"


async def test_a_completed_operation_can_be_corrected_and_the_ledger_keeps_both_sides(
    fabric: Fabric,
) -> None:
    """The case §28 is about: a transcription mistake in a finished operation.

    Refusing the correction would push the fix outside the platform; allowing it without the version,
    the reason and the before/after record would make it a silent overwrite. All three are required,
    and the test checks the ledger rather than the response alone.
    """
    created = (await _create(fabric, fabric.alpha, sequence=1, actual_duration_hours=8.5)).json()
    assert created["status"] == "completed"
    response = await fabric.http.patch(
        f"/api/v1/operations/{created['id']}",
        json={
            "expected_updated_at": created["updated_at"],
            "reason": "the report says 09:30, not 08:30",
            "changes": {"actual_duration_hours": 9.5},
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    actions = await _audit_actions(fabric, created["id"])
    assert actions[-1][0] == "operation.update"
    assert actions[-1][1]["changes"] == [
        {"field": "actual_duration_hours", "before": 8.5, "after": 9.5}
    ]
    assert actions[-1][1]["reason"] == "the report says 09:30, not 08:30"


async def test_a_cancelled_operation_is_closed_history(fabric: Fabric) -> None:
    """Cancelled means the decision was made and recorded; the numbers stop moving."""
    created = (await _create(fabric, fabric.alpha, operation_class="plan", sequence=1)).json()
    cancelled = await fabric.http.post(
        f"/api/v1/operations/{created['id']}/transition",
        json={
            "status": "cancelled",
            "expected_updated_at": created["updated_at"],
            "reason": "the section was drilled with a different programme",
        },
        headers=fabric.alpha.headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    response = await fabric.http.patch(
        f"/api/v1/operations/{created['id']}",
        json={
            "expected_updated_at": cancelled.json()["updated_at"],
            "reason": "revising an abandoned plan",
            "changes": {"remarks": "x"},
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 409, response.text
    assert "cancelled" in response.json()["error"]["message"]


async def test_a_planned_operation_may_be_corrected(fabric: Fabric) -> None:
    """A plan is a proposal, so it stays editable after it is stored."""
    created = (await _create(fabric, fabric.alpha, operation_class="plan", sequence=1)).json()
    response = await fabric.http.patch(
        f"/api/v1/operations/{created['id']}",
        json={
            "expected_updated_at": created["updated_at"],
            "reason": "the programme was revised",
            "changes": {"planned_duration_hours": 12.0},
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["planned_duration_hours"] == 12.0


# --------------------------------------------------------------------------- transitions


async def test_a_transition_moves_the_status_and_records_the_reason(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha, operation_class="plan", sequence=1)).json()
    response = await fabric.http.post(
        f"/api/v1/operations/{created['id']}/transition",
        json={
            "status": "in_progress",
            "expected_updated_at": created["updated_at"],
            "reason": "picked up at 06:00",
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "in_progress"
    assert body["phase"] == "executing"
    assert set(body["allowed_transitions"]) == {"completed", "suspended", "cancelled"}


async def test_an_illegal_transition_names_what_is_allowed(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()  # completed
    response = await fabric.http.post(
        f"/api/v1/operations/{created['id']}/transition",
        json={"status": "planned", "expected_updated_at": created["updated_at"]},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    details = response.json()["error"]["details"]
    assert details["from"] == "completed"
    assert details["allowed"] == []


async def test_cancelling_requires_a_reason(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha, operation_class="plan", sequence=1)).json()
    response = await fabric.http.post(
        f"/api/v1/operations/{created['id']}/transition",
        json={"status": "cancelled", "expected_updated_at": created["updated_at"]},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert "requires a reason" in response.json()["error"]["message"]


async def test_a_cancelled_operation_still_exists(fabric: Fabric) -> None:
    """§27: no delete of actual history. Cancelling is a state, not a removal."""
    created = (await _create(fabric, fabric.alpha, operation_class="plan", sequence=1)).json()
    cancelled = await fabric.http.post(
        f"/api/v1/operations/{created['id']}/transition",
        json={
            "status": "cancelled",
            "expected_updated_at": created["updated_at"],
            "reason": "the hole was abandoned before this step",
        },
        headers=fabric.alpha.headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    still_there = await fabric.http.get(
        f"/api/v1/operations/{created['id']}", headers=fabric.alpha.headers
    )
    assert still_there.status_code == 200
    assert still_there.json()["status"] == "cancelled"
    assert "reason" in (await _audit_actions(fabric, created["id"]))[-1][1]


async def test_there_is_no_delete_route(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()
    response = await fabric.http.delete(
        f"/api/v1/operations/{created['id']}", headers=fabric.alpha.headers
    )
    assert response.status_code == 405, "operations must not be deletable"


# --------------------------------------------------------------------------- sequencing


async def test_a_predecessor_must_be_on_the_same_wellbore(fabric: Fabric) -> None:
    other_well = await fabric.add_well(fabric.alpha, "ALPHA-SEQ")
    other_bore = await fabric.add_wellbore(fabric.alpha, other_well, "Other bore")
    first = (await _create(fabric, fabric.alpha, sequence=1)).json()
    response = await fabric.http.post(
        "/api/v1/operations",
        json={
            "name": "Next",
            "kind": "drilling",
            "project_id": fabric.alpha.project_id,
            "well_id": other_well,
            "wellbore_id": other_bore,
            "predecessor_operation_id": first["id"],
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert "different wellbore" in response.json()["error"]["message"]


async def test_an_operation_may_not_follow_itself(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()
    response = await fabric.http.post(
        f"/api/v1/operations/{created['id']}/predecessor",
        json={"predecessor_operation_id": created["id"]},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert "cannot follow itself" in response.json()["error"]["message"]


async def test_a_cycle_is_refused(fabric: Fabric) -> None:
    """A → B → C, then C is made to follow A: the chain would close on itself."""
    first = (await _create(fabric, fabric.alpha, sequence=1)).json()
    second = (
        await _create(fabric, fabric.alpha, sequence=2, predecessor_operation_id=first["id"])
    ).json()
    third = (
        await _create(fabric, fabric.alpha, sequence=3, predecessor_operation_id=second["id"])
    ).json()

    response = await fabric.http.post(
        f"/api/v1/operations/{first['id']}/predecessor",
        json={"predecessor_operation_id": third["id"]},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert "cycle" in response.json()["error"]["message"]


async def test_a_missing_predecessor_is_not_found(fabric: Fabric) -> None:
    response = await fabric.http.post(
        "/api/v1/operations",
        json={
            "name": "Next",
            "kind": "drilling",
            **_scope(fabric, fabric.alpha),
            "predecessor_operation_id": "opr_does_not_exist",
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 404, response.text


async def test_linking_a_predecessor_can_be_cleared(fabric: Fabric) -> None:
    first = (await _create(fabric, fabric.alpha, sequence=1)).json()
    second = (
        await _create(fabric, fabric.alpha, sequence=2, predecessor_operation_id=first["id"])
    ).json()
    assert second["predecessor_operation_id"] == first["id"]
    response = await fabric.http.post(
        f"/api/v1/operations/{second['id']}/predecessor",
        json={"predecessor_operation_id": None},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["predecessor_operation_id"] is None


# --------------------------------------------------------------------------- documents and reads


async def test_linking_a_document_of_another_well_is_refused(fabric: Fabric) -> None:
    other_well = await fabric.add_well(fabric.alpha, "ALPHA-DOC")
    document = await fabric.upload(fabric.alpha, b"another well's report", well_id=other_well)
    assert document.status_code == 201, document.text
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()

    response = await fabric.http.post(
        f"/api/v1/operations/{created['id']}/document",
        json={"document_id": document.json()["document"]["id"]},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert "different well" in response.json()["error"]["message"]


async def test_linking_a_document_of_the_same_well_is_recorded(fabric: Fabric) -> None:
    document = await fabric.upload(
        fabric.alpha, b"this well's report", well_id=fabric.alpha.well_id
    )
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()
    response = await fabric.http.post(
        f"/api/v1/operations/{created['id']}/document",
        json={"document_id": document.json()["document"]["id"]},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["source_document_id"] == document.json()["document"]["id"]


async def test_listing_filters_and_counts_in_the_database(fabric: Fabric) -> None:
    for index in range(5):
        await _create(fabric, fabric.alpha, sequence=index + 1)
    await _create(fabric, fabric.alpha, sequence=6, operation_class="plan", kind="reaming")

    response = await fabric.http.get(
        "/api/v1/operations",
        params={"well_id": fabric.alpha.well_id, "operation_class": "actual"},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 5, "the count is over the filtered set, before paging"
    assert len(body["items"]) == 5
    assert [row["sequence"] for row in body["items"]] == [1, 2, 3, 4, 5]

    paged = await fabric.http.get(
        "/api/v1/operations",
        params={"well_id": fabric.alpha.well_id, "limit": 2, "offset": 1},
        headers=fabric.alpha.headers,
    )
    assert [row["sequence"] for row in paged.json()["items"]] == [2, 3]
    assert paged.json()["total"] == 6


async def test_an_operation_carries_its_events_and_evidence(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()
    event = await fabric.http.post(
        "/api/v1/events",
        json={
            "title": "Stuck pipe at 2410 m",
            "kind": "stuck_pipe",
            "well_id": fabric.alpha.well_id,
            "operation_id": created["id"],
            "severity": "high",
            "occurred_at": "2026-03-15T07:00:00+00:00",
        },
        headers=fabric.alpha.headers,
    )
    assert event.status_code == 201, event.text
    detail = await fabric.http.get(
        f"/api/v1/operations/{created['id']}", headers=fabric.alpha.headers
    )
    assert detail.status_code == 200
    body = detail.json()
    assert [row["id"] for row in body["events"]] == [event.json()["id"]]
    assert body["evidence_links"] == []


# --------------------------------------------------------------------------- tenancy


async def test_another_tenant_cannot_read_an_operation(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()
    response = await fabric.http.get(
        f"/api/v1/operations/{created['id']}", headers=fabric.bravo.headers
    )
    assert response.status_code == 404, response.text


async def test_another_tenant_cannot_create_against_a_foreign_scope(fabric: Fabric) -> None:
    response = await fabric.http.post(
        "/api/v1/operations",
        json={
            "name": "Hostile",
            "kind": "drilling",
            "well_id": fabric.alpha.well_id,
            "wellbore_id": fabric.alpha.wellbore_id,
        },
        headers=fabric.bravo.headers,
    )
    assert response.status_code in {403, 404}, response.text


async def test_a_listing_never_includes_another_tenants_rows(fabric: Fabric) -> None:
    await _create(fabric, fabric.alpha, sequence=1)
    await _create(fabric, fabric.bravo, sequence=1)
    alpha = await fabric.http.get("/api/v1/operations", headers=fabric.alpha.headers)
    bravo = await fabric.http.get("/api/v1/operations", headers=fabric.bravo.headers)
    assert alpha.json()["total"] == 1 and bravo.json()["total"] == 1
    assert alpha.json()["items"][0]["id"] != bravo.json()["items"][0]["id"]


# --------------------------------------------------------------------------- audit


async def test_every_mutation_writes_an_audit_entry(fabric: Fabric) -> None:
    created = (await _create(fabric, fabric.alpha, sequence=1)).json()
    await fabric.http.patch(
        f"/api/v1/operations/{created['id']}",
        json={
            "expected_updated_at": created["updated_at"],
            "reason": "corrected",
            "changes": {"npt_hours": 1.5},
        },
        headers=fabric.alpha.headers,
    )
    actions = await _audit_actions(fabric, created["id"])
    assert [action for action, _details in actions] == ["operation.create", "operation.update"]
    update_details = actions[-1][1]
    assert update_details["reason"] == "corrected"
    assert update_details["changes"] == [{"field": "npt_hours", "before": None, "after": 1.5}]


async def _audit_actions(fabric: Fabric, subject_id: str) -> list[tuple[str, dict]]:
    async with fabric.session() as session:
        rows = (
            await session.execute(
                select(AuditLog)
                .where(AuditLog.resource_id == subject_id)
                .order_by(AuditLog.occurred_at.asc())
            )
        ).scalars().all()
    return [(row.action, row.details or {}) for row in rows]


async def test_an_audit_entry_is_not_written_for_a_refused_operation(fabric: Fabric) -> None:
    """A refused write must leave no ledger entry claiming something happened."""
    before = await _count_audit(fabric, "operation.create")
    response = await _create(fabric, fabric.alpha, sequence=1, kind="not_a_kind")
    assert response.status_code == 422
    assert await _count_audit(fabric, "operation.create") == before


async def _count_audit(fabric: Fabric, action: str) -> int:
    async with fabric.session() as session:
        return len(
            (await session.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()
        )


@pytest.mark.parametrize("field", ["project_id", "well_id", "wellbore_id", "section_id"])
async def test_a_scope_field_of_another_tenant_never_resolves(fabric: Fabric, field: str) -> None:
    response = await fabric.http.post(
        "/api/v1/operations",
        json={
            "name": "Hostile",
            "kind": "drilling",
            "well_id": fabric.bravo.well_id,
            "wellbore_id": fabric.bravo.wellbore_id,
            field: getattr(fabric.bravo, field),
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code in {403, 404}, response.text


def test_operation_rows_are_never_deleted_by_the_service() -> None:
    """A structural guard: no method in the service removes an operation.

    The API test above proves the *route* does not exist; this proves the layer underneath it did not
    quietly keep one.
    """
    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src"
        / "drillai"
        / "operations"
        / "service.py"
    ).read_text()
    assert "session.delete" not in source
    assert "DELETE FROM" not in source
