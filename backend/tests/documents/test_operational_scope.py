"""Document scope: the eight shapes, the disagreements, and the tenant boundary.

Every finding these tests cover was reproduced against the running application before the resolver
existed, and each test names the behaviour it replaces:

* an upload naming another organization's ``wellbore_id`` returned **201** and stored a document bound
  to that other tenant's hole (``test_cross_organization_*``);
* an upload naming another organization's ``section_id`` did the same;
* a ``well_id`` that disagreed with the ``wellbore_id`` beside it was accepted, producing a document
  attached to one tenant's well and another tenant's bore.

The scope is now resolved once, against the caller's organization, before the upload is read.
"""

from __future__ import annotations

import pytest
from tests.documents.conftest import DDR_BYTES, Fabric, Tenant


async def test_project_only_scope(fabric: Fabric) -> None:
    response = await fabric.upload(
        fabric.alpha, b"project-level filing", project_id=fabric.alpha.project_id
    )
    assert response.status_code == 201, response.text
    scope = response.json()["scope"]
    assert scope["project_id"] == fabric.alpha.project_id
    assert scope["well_id"] is None
    assert scope["supplied"] == ["project_id"]
    assert scope["derived"] == []


async def test_well_only_scope_does_not_invent_a_wellbore(fabric: Fabric) -> None:
    """A well may have more than one bore, so "this is about the well" stays at the well.

    This is the case the fixture asserted wrongly at first: it expected the well's *only* wellbore to
    be filled in. Deriving downward would mean the platform choosing a hole, and a well with a sidetrack
    would file the document against whichever bore happened to be listed first.
    """
    response = await fabric.upload(fabric.alpha, b"well-level filing", well_id=fabric.alpha.well_id)
    assert response.status_code == 201, response.text
    scope = response.json()["scope"]
    assert scope["well_id"] == fabric.alpha.well_id
    assert scope["wellbore_id"] is None
    assert scope["section_id"] is None
    # The project *is* derived: it is above the well, and there is exactly one of it.
    assert scope["project_id"] == fabric.alpha.project_id
    assert scope["derived"] == ["project_id"]
    assert "well_id → project_id" in scope["chain"]


async def test_well_and_wellbore_scope(fabric: Fabric) -> None:
    response = await fabric.upload(
        fabric.alpha,
        b"wellbore-level filing",
        well_id=fabric.alpha.well_id,
        wellbore_id=fabric.alpha.wellbore_id,
    )
    assert response.status_code == 201, response.text
    scope = response.json()["scope"]
    assert scope["well_id"] == fabric.alpha.well_id
    assert scope["wellbore_id"] == fabric.alpha.wellbore_id
    assert scope["supplied"] == ["well_id", "wellbore_id"]
    assert scope["derived"] == ["project_id"]


async def test_well_and_section_scope_derives_the_wellbore_between_them(fabric: Fabric) -> None:
    """A section implies its wellbore, so the caller need not send both."""
    response = await fabric.upload(
        fabric.alpha,
        b"section-level filing",
        well_id=fabric.alpha.well_id,
        section_id=fabric.alpha.section_id,
    )
    assert response.status_code == 201, response.text
    scope = response.json()["scope"]
    assert scope["section_id"] == fabric.alpha.section_id
    assert scope["wellbore_id"] == fabric.alpha.wellbore_id
    assert "section_id → wellbore_id" in scope["chain"]
    assert scope["supplied"] == ["well_id", "section_id"]
    assert set(scope["derived"]) == {"wellbore_id", "project_id"}


async def test_wellbore_only_scope(fabric: Fabric) -> None:
    response = await fabric.upload(fabric.alpha, b"bore-only filing", wellbore_id=fabric.alpha.wellbore_id)
    assert response.status_code == 201, response.text
    scope = response.json()["scope"]
    assert scope["well_id"] == fabric.alpha.well_id
    assert scope["project_id"] == fabric.alpha.project_id
    assert scope["supplied"] == ["wellbore_id"]
    assert "wellbore_id → well_id" in scope["chain"]


async def test_section_only_scope(fabric: Fabric) -> None:
    response = await fabric.upload(fabric.alpha, b"section-only filing", section_id=fabric.alpha.section_id)
    assert response.status_code == 201, response.text
    scope = response.json()["scope"]
    assert scope["section_id"] == fabric.alpha.section_id
    assert scope["wellbore_id"] == fabric.alpha.wellbore_id
    assert scope["well_id"] == fabric.alpha.well_id
    assert scope["project_id"] == fabric.alpha.project_id
    assert scope["supplied"] == ["section_id"]


async def test_full_scope_round_trips_unchanged(fabric: Fabric) -> None:
    response = await fabric.upload(
        fabric.alpha,
        b"fully scoped filing",
        project_id=fabric.alpha.project_id,
        well_id=fabric.alpha.well_id,
        wellbore_id=fabric.alpha.wellbore_id,
        section_id=fabric.alpha.section_id,
    )
    assert response.status_code == 201, response.text
    scope = response.json()["scope"]
    assert scope["derived"] == [], "nothing should have been derived from a complete scope"
    assert scope["notes"] == []
    assert scope["wellbore_id"] == fabric.alpha.wellbore_id


async def test_no_scope_is_allowed_and_recorded_as_such(fabric: Fabric) -> None:
    """An unscoped upload is a filing with no home yet — not an error, and not a silent well."""
    response = await fabric.upload(fabric.alpha, DDR_BYTES, filename="ddr.txt", doc_type="ddr")
    assert response.status_code == 201, response.text
    scope = response.json()["scope"]
    assert scope["supplied"] == []
    assert scope["derived"] == []
    assert scope["well_id"] is None


# --------------------------------------------------------------------------- disagreements


def _error(response) -> dict:
    """The one error envelope every endpoint answers with (see ``api/app.py``)."""
    body = response.json()
    assert "error" in body, body
    return body["error"]


async def test_wellbore_from_another_well_is_refused(fabric: Fabric) -> None:
    """Both references exist and belong to this tenant — they simply contradict each other."""
    other_well = await fabric.add_well(fabric.alpha, "ALPHA-2")
    response = await fabric.upload(
        fabric.alpha,
        b"contradictory scope",
        well_id=other_well,
        wellbore_id=fabric.alpha.wellbore_id,
    )
    assert response.status_code == 422, response.text
    error = _error(response)
    assert error["code"] == "platform.validation_failed"
    assert "wellbore_id does not belong to the supplied well_id" in error["message"]
    assert error["details"]["wellbore_well_id"] == fabric.alpha.well_id


async def test_section_from_another_wellbore_is_refused(fabric: Fabric) -> None:
    """A section and a bore that both exist in this tenant, but on different wells."""
    other_well = await fabric.add_well(fabric.alpha, "ALPHA-4")
    other_wellbore = await fabric.add_wellbore(fabric.alpha, other_well, "Second bore")
    response = await fabric.upload(
        fabric.alpha,
        b"section on the wrong bore",
        wellbore_id=other_wellbore,
        section_id=fabric.alpha.section_id,
    )
    assert response.status_code == 422, response.text
    assert "section_id does not belong to the supplied wellbore_id" in _error(response)["message"]


async def test_well_from_another_project_is_refused(fabric: Fabric) -> None:
    second_project = await fabric.http.post(
        "/api/v1/projects", json={"name": "alpha second project"}, headers=fabric.alpha.headers
    )
    assert second_project.status_code == 201, second_project.text
    response = await fabric.upload(
        fabric.alpha,
        b"well outside the named project",
        project_id=second_project.json()["id"],
        well_id=fabric.alpha.well_id,
    )
    assert response.status_code == 422, response.text
    assert "well_id does not belong to the supplied project_id" in _error(response)["message"]


async def test_unknown_reference_is_not_found_not_forbidden(fabric: Fabric) -> None:
    """A guessed identifier must not be confirmed by the shape of the error."""
    response = await fabric.upload(fabric.alpha, b"guessed scope", well_id="wel_does_not_exist")
    assert response.status_code == 404, response.text
    error = _error(response)
    assert error["code"] == "platform.not_found"
    assert "not found" in error["message"]


# --------------------------------------------------------------------------- tenant boundary


@pytest.mark.parametrize("field", ["wellbore_id", "section_id", "well_id", "project_id"])
async def test_cross_organization_reference_is_refused(fabric: Fabric, field: str) -> None:
    """FINDING A/A2: these were all accepted with 201 before the resolver.

    The upload also has to fail *before* storing anything: a refused scope must leave no artefact, no
    document and no job behind. That is asserted here rather than assumed, because a document row
    written for a rejected scope is a cross-tenant leak even if the response says 404.
    """
    response = await fabric.upload(
        fabric.alpha, b"cross-org attempt", **{field: getattr(fabric.bravo, field)}
    )
    assert response.status_code in {403, 404}, response.text

    # Nothing of bravo's was named in a stored document.
    listing = await fabric.http.get(
        "/api/v1/documents", params={"well_id": fabric.bravo.well_id}, headers=fabric.alpha.headers
    )
    assert listing.status_code == 200
    assert listing.json()["items"] == []


async def test_cross_organization_upload_leaves_no_artefact_behind(fabric: Fabric) -> None:
    payload = b"bytes that must not be stored under a rejected scope"
    refused = await fabric.upload(fabric.alpha, payload, wellbore_id=fabric.bravo.wellbore_id)
    assert refused.status_code in {403, 404}, refused.text

    # The same bytes for the caller's *own* well are a first upload, not a duplicate of anything: if
    # the rejected request had stored the artefact, this would come back as a reuse of a document the
    # caller cannot see.
    accepted = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["scope"]["reused_existing"] is False


async def test_bravo_cannot_read_alpha_document(fabric: Fabric) -> None:
    created = await fabric.upload(fabric.alpha, b"alpha private", well_id=fabric.alpha.well_id)
    assert created.status_code == 201, created.text
    document_id = created.json()["document"]["id"]

    assert (
        await fabric.http.get(f"/api/v1/documents/{document_id}", headers=fabric.bravo.headers)
    ).status_code == 404
    assert (
        await fabric.http.get(
            f"/api/v1/documents/{document_id}/provenance", headers=fabric.bravo.headers
        )
    ).status_code == 404


async def test_operation_only_scope_derives_the_well(fabric: Fabric) -> None:
    """The deepest reference in §8: an operation implies its well, bore and section.

    An operation is created directly rather than through an endpoint that does not exist yet, because
    this test is about the resolver reading the row it is handed, not about how the row got there.
    """
    operation_id = await _create_operation(fabric, fabric.alpha)
    response = await fabric.upload(fabric.alpha, b"operation filing", operation_id=operation_id)
    assert response.status_code == 201, response.text
    scope = response.json()["scope"]
    assert scope["operation_id"] == operation_id
    assert scope["well_id"] == fabric.alpha.well_id
    assert scope["wellbore_id"] == fabric.alpha.wellbore_id
    assert scope["section_id"] == fabric.alpha.section_id
    assert "operation_id → well_id" in scope["chain"]


async def test_operation_from_another_well_is_refused(fabric: Fabric) -> None:
    """The operation and the named well are each real; they are simply not the same well."""
    other_well = await fabric.add_well(fabric.alpha, "ALPHA-3")
    operation_id = await _create_operation(fabric, fabric.alpha)
    response = await fabric.upload(
        fabric.alpha, b"operation on the wrong well", well_id=other_well, operation_id=operation_id
    )
    assert response.status_code == 422, response.text
    assert "operation_id does not belong to the supplied well_id" in _error(response)["message"]


async def test_cross_organization_operation_is_refused(fabric: Fabric) -> None:
    operation_id = await _create_operation(fabric, fabric.bravo)
    response = await fabric.upload(fabric.alpha, b"bravo operation", operation_id=operation_id)
    assert response.status_code in {403, 404}, response.text


async def _create_operation(fabric: Fabric, tenant: Tenant) -> str:
    """Insert one operation for a tenant, bypassing the API that does not serve them yet.

    The row is written through the application's own session factory, so it carries exactly the columns
    the model declares — no hand-written INSERT that could omit a NOT NULL field such as
    ``wellbore_id``, which operations always have.
    """
    from drillai.core.ids import new_id
    from drillai.db.models import Operation

    async with fabric.session() as session:
        operation = Operation(
            id=new_id("opr"),
            org_id=fabric.org_id(tenant),
            project_id=tenant.project_id,
            well_id=tenant.well_id,
            wellbore_id=tenant.wellbore_id,
            section_id=tenant.section_id,
            operation_class="actual",
            sequence=1,
            kind="drilling_ahead",
            phase="drilling",
            status="completed",
            name="Drilling ahead",
            source_kind="manual",
        )
        session.add(operation)
        await session.commit()
        return operation.id
