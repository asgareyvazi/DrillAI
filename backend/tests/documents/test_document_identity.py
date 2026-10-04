"""Document identity: raw bytes are not the same thing as a logical document.

The defect these tests are built around was reproduced before the fix: uploading the *same bytes* to a
second well returned the **first** well's document, because deduplication keyed on the artefact alone.
A daily report filed against the wrong well could not be corrected — the platform would hand back the
document attached to the other well and report success.

The four cases §15–18 require, each asserted separately because each has a different answer:

===============================================  ==========================================
same bytes, same scope, same revision           one logical document (idempotent)
same bytes, different scope                      two documents sharing one artefact
same bytes, same scope, different revision       two documents sharing one artefact
different bytes, same document number            two documents, two artefacts
===============================================  ==========================================
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
from tests.documents.conftest import DDR_BYTES, Fabric

from drillai.documents.identity import logical_key, scope_token

# --------------------------------------------------------------------------- the key itself


def test_key_is_content_derived_and_deterministic() -> None:
    base = {
        "content_sha256": "a" * 64,
        "well_id": "wel_1",
        "doc_type": "ddr",
        "revision": "A",
    }
    first = logical_key(**base)
    assert first == logical_key(**base)
    assert first is not None and len(first) == 64


def test_key_ignores_presentation_but_not_identity() -> None:
    """Case-only differences in a revision are the same revision; a different well is not."""
    base = {"content_sha256": "a" * 64, "well_id": "wel_1", "revision": "B"}
    assert logical_key(**base) == logical_key(**{**base, "revision": "b"})
    assert logical_key(**base) == logical_key(**{**base, "revision": " B "})

    # Every part of the key changes it when it is the reference that decides the scope.
    unscoped = {"content_sha256": "a" * 64, "doc_type": "ddr", "revision": "B"}
    assert logical_key(**unscoped) != logical_key(**unscoped, well_id="wel_1")
    for field, other in (
        ("wellbore_id", "wlb_1"),
        ("section_id", "sec_1"),
        ("well_id", "wel_2"),
    ):
        assert logical_key(**base) != logical_key(**{**base, field: other}), field
    assert logical_key(**unscoped) != logical_key(**unscoped, project_id="prj_1")
    for field, other in (("doc_type", "mud_report"), ("revision", "C"), ("content_sha256", "b" * 64)):
        assert logical_key(**base) != logical_key(**{**base, field: other}), field


def test_a_less_specific_reference_beside_a_more_specific_one_does_not_split_the_key() -> None:
    """The scope is *where the document is*, not the set of fields the caller happened to send.

    A client that sends only ``section_id`` and one that sends the section's wellbore, well and project
    as well are filing the same document in the same place, and the resolver derives the rest. If the
    key counted references instead of resolving them, the two would produce two documents.
    """
    content = {"content_sha256": "a" * 64}
    section_only = logical_key(**content, section_id="sec_1")
    redundant = logical_key(
        **content,
        section_id="sec_1",
        wellbore_id="wlb_1",
        well_id="wel_1",
        project_id="prj_1",
    )
    assert section_only == redundant


def test_a_section_filing_is_not_the_same_document_as_a_wellbore_filing() -> None:
    """The scope token needed ``section_id`` too — the same defect one level down.

    Filing the same bytes against a section and then against the wellbore it sits in produced one
    document, so the section-level filing became a wellbore-level one. Caught while writing the first
    version of these tests, which is why the scope tuple now covers every scope field the model stores.
    """
    common = {"content_sha256": "e" * 64, "well_id": "wel_1"}
    by_section = logical_key(**common, section_id="sec_1", wellbore_id="wlb_1")
    by_wellbore = logical_key(**common, wellbore_id="wlb_1")
    assert by_section != by_wellbore


def test_no_bytes_means_no_identity() -> None:
    """Without bytes there is nothing to identify; the caller must not get a key for nothing."""
    assert logical_key(content_sha256=None, well_id="wel_1") is None
    assert logical_key(content_sha256="", well_id="wel_1") is None


def test_scope_token_takes_the_most_specific_reference() -> None:
    """Order of the arguments is irrelevant; order of *specificity* decides the token."""
    assert scope_token(well_id="wel_1", wellbore_id="wlb_1") == "wlb_1"
    assert scope_token(wellbore_id="wlb_1", well_id="wel_1") == "wlb_1"
    # Most specific wins, all the way down the hierarchy.
    assert scope_token(section_id="sec_1", wellbore_id="wlb_1", well_id="wel_1") == "sec_1"
    assert scope_token(well_id="wel_1", project_id="prj_1") == "wel_1"
    # No scope named is a real answer, and an empty string can never collide with an identifier.
    assert scope_token() == ""
    assert scope_token(project_id="") == ""


def _migration_module():
    """Load the migration that backfills ``logical_key``, exactly as Alembic would."""
    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "b8f2a6d31c04_operational_provenance.py"
    )
    spec = importlib.util.spec_from_file_location("_cp8_migration", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_migration_key_copy_agrees_with_the_application() -> None:
    """§60/§92: the migration carries a frozen copy of the key; it must not drift.

    A migration cannot import application code — the code is free to change and the migration's output
    must not. So the copy is deliberate, and this test is what keeps the copy honest: same inputs, same
    key, forever.
    """
    frozen = _migration_module().document_logical_key
    # The migration's copy takes every argument positionally-mandated (no defaults), so each case names
    # all seven. That is deliberate: a default added on one side and not the other would be a silent
    # divergence, and this test would not notice it.
    cases = [
        {
            "content_sha256": "c" * 64,
            "section_id": None,
            "well_id": "wel_1",
            "wellbore_id": None,
            "project_id": "prj_1",
            "doc_type": None,
            "revision": None,
        },
        {
            "content_sha256": "d" * 64,
            "section_id": "sec_7",
            "well_id": None,
            "wellbore_id": "wlb_9",
            "project_id": None,
            "doc_type": "ddr",
            "revision": "Rev-B",
        },
        {
            "content_sha256": "f" * 64,
            "section_id": None,
            "well_id": None,
            "wellbore_id": None,
            "project_id": None,
            "doc_type": " Mud_Report ",
            "revision": " A ",
        },
        {
            "content_sha256": "g" * 64,
            "section_id": None,
            "well_id": None,
            "wellbore_id": None,
            "project_id": None,
            "doc_type": None,
            "revision": None,
        },
        {
            "content_sha256": None,
            "section_id": None,
            "well_id": "wel_1",
            "wellbore_id": None,
            "project_id": None,
            "doc_type": None,
            "revision": None,
        },
    ]
    for case in cases:
        assert frozen(**case) == logical_key(**case), case


# --------------------------------------------------------------------------- the four cases


async def test_same_bytes_same_scope_is_idempotent(fabric: Fabric) -> None:
    payload = b"identical DDR bytes, filed once"
    first = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    second = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    assert first.status_code == second.status_code == 201
    assert second.json()["document"]["id"] == first.json()["document"]["id"]
    assert second.json()["scope"]["reused_existing"] is True
    assert second.json()["job"]["status"] == "skipped_duplicate"


async def test_same_bytes_different_scope_is_a_second_document(fabric: Fabric) -> None:
    """FINDING D: this returned the first well's document, so a misfiled report was uncorrectable."""
    payload = b"identical DDR bytes, filed against two wells"
    other_well = await fabric.add_well(fabric.alpha, "ALPHA-9")

    first = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    second = await fabric.upload(fabric.alpha, payload, well_id=other_well)
    assert first.status_code == second.status_code == 201

    first_body, second_body = first.json(), second.json()
    assert second_body["document"]["id"] != first_body["document"]["id"]
    assert second_body["document"]["well_id"] == other_well
    assert second_body["scope"]["reused_existing"] is False
    # One artefact, two documents: §17 explicitly forbids duplicating the bytes for a scope change.
    assert second_body["document"]["raw_artifact_id"] == first_body["document"]["raw_artifact_id"]
    assert second_body["document"]["logical_key"] != first_body["document"]["logical_key"]


async def test_same_bytes_same_scope_different_revision_is_a_second_document(fabric: Fabric) -> None:
    """A reissue is a different logical document, and the first one is not overwritten."""
    payload = b"identical DDR bytes, revision A then revision B"
    first = await fabric.upload(
        fabric.alpha, payload, well_id=fabric.alpha.well_id, revision="A", title="DDR rev A"
    )
    second = await fabric.upload(
        fabric.alpha, payload, well_id=fabric.alpha.well_id, revision="B", title="DDR rev B"
    )
    assert first.status_code == second.status_code == 201
    assert second.json()["document"]["id"] != first.json()["document"]["id"]
    assert second.json()["document"]["revision"] == "B"

    # Both are still readable: the revision did not replace its predecessor.
    listing = await fabric.http.get(
        "/api/v1/documents", params={"well_id": fabric.alpha.well_id}, headers=fabric.alpha.headers
    )
    assert listing.status_code == 200
    ids = {row["id"] for row in listing.json()["items"]}
    assert {first.json()["document"]["id"], second.json()["document"]["id"]} <= ids


async def test_different_bytes_same_document_number_are_two_documents(fabric: Fabric) -> None:
    """Identity is content-derived; a human's document number is metadata, not identity.

    Two documents numbered ``DDR-001`` with different bytes is a real filing mistake, and collapsing
    them would delete one of them. Both are kept, and both keep their own artefact.
    """
    first = await fabric.upload(
        fabric.alpha, DDR_BYTES, well_id=fabric.alpha.well_id, filename="one.txt"
    )
    second = await fabric.upload(
        fabric.alpha, DDR_BYTES + b"\nADDITIONAL NOTE: pump pressure 120 bar\n",
        well_id=fabric.alpha.well_id, filename="two.txt",
    )
    assert first.status_code == second.status_code == 201
    assert second.json()["document"]["id"] != first.json()["document"]["id"]
    assert second.json()["document"]["raw_artifact_id"] != first.json()["document"]["raw_artifact_id"]


async def test_a_cross_scope_upload_does_not_reuse_another_tenants_bytes(fabric: Fabric) -> None:
    """The dedup search is org-scoped: bravo's identical bytes are not alpha's document."""
    payload = b"a file both tenants happen to have"
    alpha = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    bravo = await fabric.upload(fabric.bravo, payload, well_id=fabric.bravo.well_id)
    assert alpha.status_code == bravo.status_code == 201
    assert bravo.json()["scope"]["reused_existing"] is False
    assert bravo.json()["document"]["id"] != alpha.json()["document"]["id"]
    # Two organizations, two artefacts as well: an artefact row is tenant-owned, so reusing one across
    # organizations would leak the fact that the other tenant already had those bytes.
    assert bravo.json()["document"]["raw_artifact_id"] != alpha.json()["document"]["raw_artifact_id"]


@pytest.mark.parametrize("field", ["title", "doc_type", "period_start"])
async def test_metadata_differences_do_not_change_identity(fabric: Fabric, field: str) -> None:
    """§18: a rename or reclassification is not a new document.

    ``doc_type`` is part of the *key*, so changing it is a reclassification and deliberately produces a
    new logical document while keeping one artefact; a title is not part of the key and must not.
    """
    payload = f"payload for the {field} case".encode()
    first = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id)
    assert first.status_code == 201, first.text
    extra: dict[str, str] = {}
    if field == "period_start":
        extra["period_start"] = "2026-03-01T00:00:00"
    elif field == "doc_type":
        # A different recognised type: a reclassification, which is a different logical document
        # sharing the same bytes.
        extra["doc_type"] = "mud_report"
    else:
        extra[field] = "renamed or reclassified"
    second = await fabric.upload(fabric.alpha, payload, well_id=fabric.alpha.well_id, **extra)
    assert second.status_code == 201, second.text

    if field == "doc_type":
        assert second.json()["document"]["id"] != first.json()["document"]["id"]
        assert (
            second.json()["document"]["raw_artifact_id"]
            == first.json()["document"]["raw_artifact_id"]
        )
    else:
        assert second.json()["document"]["id"] == first.json()["document"]["id"]
        assert second.json()["scope"]["reused_existing"] is True
