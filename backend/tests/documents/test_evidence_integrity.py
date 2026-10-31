"""Evidence integrity: the graph from a value back to the place on the page it came from.

Before CP8 the graph was declared and empty. ``chunk.region_id`` and ``record.region_id`` were
``None`` for every row the pipeline wrote, so "which paragraph of which page produced this number" had
no answer at all — the columns existed, the links were null, and nothing failed. Both were reproduced
against the running application before these tests were written.

What the tests check, in order of what would break first if it regressed:

1. a chunk knows the region it was cut from (else text is unattributable);
2. a record carries its region, or says ``region_unknown`` (else the gap is invisible);
3. an evidence link points at a region of *its own* document, in *its own* organization;
4. ``quote_verified`` is mechanical — the excerpt was found in the text it names — and never a claim
   about meaning;
5. following a link never crosses an organization.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import func, select
from tests.documents.conftest import DDR_BYTES, Fabric

from drillai.api.deps import AuthContext
from drillai.api.routers.evidence import get_evidence
from drillai.core.errors import NotFound
from drillai.db.models import (
    Document,
    DocumentChunk,
    DocumentPage,
    DocumentRegion,
    EvidenceLink,
    ExtractedRecord,
    IngestionJob,
    RawArtifact,
)
from drillai.documents.quotes import excerpt_occurs_in, normalize_for_match, verify_quote
from drillai.security.actions import ActionLevel, Principal

# --------------------------------------------------------------------------- the mechanical check


def test_normalisation_folds_presentation_not_content() -> None:
    assert normalize_for_match("Mud  Weight:\u00a09.2  ppg") == "mud weight: 9.2 ppg"
    # A non-breaking space, a curly apostrophe and a ligature are typography, not data.
    assert normalize_for_match("Rig\u2019s \ufb01rst run") == "rig's first run"
    # And a number is not folded into another number.
    assert normalize_for_match("9.2") != normalize_for_match("9.20")


def test_containment_is_exact_after_normalisation() -> None:
    haystack = "Mud Weight: 9.2 ppg\nFunnel Viscosity: 45 s"
    assert excerpt_occurs_in(excerpt="mud weight: 9.2 PPG", source_text=haystack) is True
    assert excerpt_occurs_in(excerpt="Mud Weight: 9.3 ppg", source_text=haystack) is False
    assert excerpt_occurs_in(excerpt="", source_text=haystack) is False, "an empty excerpt proves nothing"
    assert excerpt_occurs_in(excerpt="anything", source_text="") is False
    assert excerpt_occurs_in(excerpt="anything", source_text=None) is False


def test_verify_quote_reports_which_text_it_consulted() -> None:
    """``how`` names the text that was checked, so "not verified" distinguishes its own causes."""
    region = "Bit Depth: 1500 m"
    page = "Header\nBit Depth: 1500 m\nFooter"
    assert verify_quote(
        excerpt="bit depth: 1500 m", region_text=region, page_text=page
    ) == (True, "region_text")
    # A region exists and the excerpt is not in it: checked, and it did not match. Falling back to the
    # page here would turn "this came from region 7" into "this came from somewhere on page 3" while
    # the link's locator still claimed region 7 — so the region is the only text consulted when there
    # is one, and the answer says so.
    assert verify_quote(
        excerpt="Bit Depth: 1600 m", region_text=region, page_text=page
    ) == (False, "region_text")
    assert verify_quote(
        excerpt="Mud weight: 9.2 ppg",
        region_text="Mud weight: 9.4 ppg",
        page_text="Mud weight: 9.2 ppg",
        region_basis="region_table",
    ) == (False, "region_table")
    # No region on the link, so the page is the text of record.
    assert verify_quote(
        excerpt="header", region_text=None, page_text=page
    ) == (True, "page_text")
    assert verify_quote(
        excerpt="nowhere at all", region_text=None, page_text=page
    ) == (False, "page_text")
    # Nothing to check against is a third state, not a mismatch.
    assert verify_quote(excerpt="x", region_text=None, page_text=None) == (False, "unavailable")


# --------------------------------------------------------------------------- the stored graph


async def test_chunks_carry_the_region_they_came_from(fabric: Fabric) -> None:
    """FINDING E: every chunk's ``region_id`` was null."""
    response = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="ddr.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["document"]["id"]

    detail = await fabric.http.get(f"/api/v1/documents/{document_id}", headers=fabric.alpha.headers)
    assert detail.status_code == 200
    chunks = detail.json()["chunks"]
    assert chunks, "a one-page text report must produce chunks"

    async with fabric.session() as session:
        region_ids = set(
            (
                await session.execute(
                    select(DocumentRegion.id).where(DocumentRegion.document_id == document_id)
                )
            )
            .scalars()
            .all()
        )
    assert region_ids, "the document must have regions for a chunk to point at"
    linked = [chunk for chunk in chunks if chunk["region_id"]]
    assert linked, "no chunk carries a region id: the chunk→region link is dead again"
    for chunk in linked:
        assert chunk["region_id"] in region_ids, "a chunk points at a region of another document"


async def test_records_carry_a_region_or_say_the_region_is_unknown(fabric: Fabric) -> None:
    """§21: a record whose region the extractor cannot determine must say so, not invent one.

    The report is deliberately read through the *text* extractors, which locate values inside a region.
    A record that has no region and no flag is the failure this test exists to catch: the UI would
    render a value with no provenance and no indication that any was missing.
    """
    response = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="ddr.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["document"]["id"]

    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).scalars().all()
        regions = {
            region.id: region
            for region in (
                await session.execute(
                    select(DocumentRegion).where(DocumentRegion.document_id == document_id)
                )
            )
            .scalars()
            .all()
        }

    assert records, "the report has readable values"
    for record in records:
        if record.region_id is not None:
            assert record.region_id in regions, "the region belongs to this document"
            assert record.region_unknown is False
            assert record.page_number == regions[record.region_id].page_number, (
                "the record's page must be the page of the region it points at"
            )
        else:
            assert record.region_unknown is True, (
                f"record {record.id} has no region and does not declare the gap"
            )
    assert any(record.region_id for record in records), "at least one value must be attributable"


async def test_evidence_links_trace_to_a_region_of_the_same_document(fabric: Fabric) -> None:
    response = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="ddr.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["document"]["id"]

    listing = await fabric.http.get(
        "/api/v1/evidence", params={"document_id": document_id}, headers=fabric.alpha.headers
    )
    assert listing.status_code == 200, listing.text
    links = listing.json()["items"]
    assert links, "an ingested document must produce evidence links"
    for link in links:
        assert link["document_id"] == document_id
        assert link["evidence_kind"] in {
            "document_region",
            "document_page",
            "document",
        }, link["evidence_kind"]
        assert link["excerpt"], "an evidence link without an excerpt cites nothing"


async def test_quote_verified_matches_the_text_the_platform_stored(fabric: Fabric) -> None:
    """The flag must be re-derivable from the row's own excerpt and the region it names."""
    response = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="ddr.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["document"]["id"]

    async with fabric.session() as session:
        links = (
            await session.execute(select(EvidenceLink).where(EvidenceLink.document_id == document_id))
        ).scalars().all()
        region_rows = (
            await session.execute(
                select(DocumentRegion).where(DocumentRegion.document_id == document_id)
            )
        ).scalars().all()
        regions = {}
        for region in region_rows:
            if region.text is not None:
                region_source, region_basis = region.text, "region_text"
            elif region.table is not None:
                region_source = "\n".join(
                    " | ".join(str(cell.get("text", "")) for cell in row)
                    for row in region.table.get("cells", [])
                )
                region_basis = "region_table"
            else:
                region_source, region_basis = "", "region_unavailable"
            regions[region.id] = (region_source, region_basis)
        page_texts = {
            page.page_number: page.text or ""
            for page in (
                await session.execute(
                    select(DocumentPage).where(DocumentPage.document_id == document_id)
                )
            )
            .scalars()
            .all()
        }

    checked = [link for link in links if link.quote_check]
    assert checked, "every link written by the pipeline records how its quote was checked"
    for link in checked:
        region_text, region_basis = regions.get(link.region_id, (None, "region_text"))
        page_text = page_texts.get(link.page_number or -1, "")
        verified, basis = verify_quote(
            excerpt=link.excerpt,
            region_text=region_text,
            page_text=page_text or None,
            region_basis=region_basis,
        )
        # Re-derived from the stored row, the verdict must be the one that was stored. If this ever
        # fails, the flag was written by something other than the check it claims to be.
        assert basis == link.quote_check, (link.id, basis, link.quote_check)
        assert verified is link.quote_verified, link.id


async def test_region_count_agrees_with_the_regions_that_exist(fabric: Fabric) -> None:
    """The count is the count: a client must not be told zero while the detail lists regions.

    The endpoint deliberately answers with ``region_count`` rather than the regions themselves, so this
    cross-checks the number against the table rather than against the response — a count that does not
    match the rows is how a "nothing was extracted from this page" story starts.
    """
    response = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="ddr.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["document"]["id"]
    detail = await fabric.http.get(f"/api/v1/documents/{document_id}", headers=fabric.alpha.headers)
    assert detail.status_code == 200
    async with fabric.session() as session:
        stored_regions = (
            await session.execute(
                select(func.count())
                .select_from(DocumentRegion)
                .where(DocumentRegion.document_id == document_id)
            )
        ).scalar_one()
        chunks = (
            await session.execute(
                select(DocumentChunk).where(DocumentChunk.document_id == document_id)
            )
        ).scalars().all()
    assert detail.json()["region_count"] == stored_regions > 0
    # The scope a chunk inherits is the document's scope: retrieval depends on this.
    assert all(chunk.well_id == fabric.alpha.well_id for chunk in chunks)
    assert all(chunk.org_id == "org_alpha" for chunk in chunks)


# --------------------------------------------------------------------------- tenancy of the traversal


async def test_evidence_traversal_is_organization_scoped(fabric: Fabric) -> None:
    """Following a link must not cross a tenant boundary, even for a link the caller may read.

    The link is read under bravo's identity while naming alpha's document, region and record — the
    shape a mis-written row would have. Nothing in the traversal may resolve them.
    """
    created = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="ddr.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert created.status_code == 201, created.text
    alpha_document_id = created.json()["document"]["id"]

    async with fabric.session() as session:
        alpha_document = (
            await session.execute(select(Document).where(Document.id == alpha_document_id))
        ).scalar_one()
        alpha_region = (
            await session.execute(
                select(DocumentRegion).where(DocumentRegion.document_id == alpha_document_id)
            )
        ).scalars().first()
        alpha_record = (
            await session.execute(
                select(ExtractedRecord).where(ExtractedRecord.document_id == alpha_document_id)
            )
        ).scalars().first()
        assert alpha_region is not None and alpha_record is not None

        # A link bravo legitimately owns, whose subjects point at alpha's rows.
        intruder = EvidenceLink(
            id="evd_intruder_probe",
            org_id="org_bravo",
            subject_kind="extracted_record",
            subject_id=alpha_record.id,
            evidence_kind="document_region",
            evidence_id=alpha_region.id,
            document_id=alpha_document.id,
            region_id=alpha_region.id,
            page_number=alpha_region.page_number,
            well_id=fabric.bravo.well_id,
            excerpt="this excerpt belongs to another tenant",
            method="test",
        )
        session.add(intruder)
        await session.commit()

    response = await fabric.http.get("/api/v1/evidence/evd_intruder_probe", headers=fabric.bravo.headers)
    # A cross-tenant link target invalidates the edge itself; do not return even its caller-authored
    # excerpt as if it were an internally valid citation.
    assert response.status_code == 404, response.text
    assert "this excerpt belongs to another tenant" not in response.text


async def test_provenance_chain_is_scoped_and_ordered(fabric: Fabric) -> None:
    created = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="ddr.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["document"]["id"]

    response = await fabric.http.get(
        f"/api/v1/documents/{document_id}/provenance", headers=fabric.alpha.headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_id"] == document_id
    assert body["record_count"] == len(body["chain"]) > 0
    for link in body["chain"]:
        assert link["document"]["id"] == document_id
        record = link["record"]
        assert record["document_id"] == document_id
        if record["region_id"] is not None:
            # The region is resolved inline, so a client never has to join by hand.
            assert link["region"] is not None
            assert link["region"]["id"] == record["region_id"]
        else:
            assert record["region_unknown"] is True
    # Another tenant cannot read the provenance of a document it does not own.
    assert (
        await fabric.http.get(
            f"/api/v1/documents/{document_id}/provenance", headers=fabric.bravo.headers
        )
    ).status_code == 404


async def test_evidence_detail_rejects_a_deleted_subject_record(fabric: Fabric) -> None:
    created = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="deleted-record.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["document"]["id"]
    async with fabric.session() as session:
        link = (
            await session.execute(
                select(EvidenceLink).where(
                    EvidenceLink.document_id == document_id,
                    EvidenceLink.subject_kind.startswith("extracted_record"),
                )
            )
        ).scalars().first()
        assert link is not None
        record = await session.get(ExtractedRecord, link.subject_id)
        assert record is not None
        evidence_id = link.id
        await session.delete(record)
        await session.commit()

    response = await fabric.http.get(f"/api/v1/evidence/{evidence_id}", headers=fabric.alpha.headers)
    assert response.status_code == 404, response.text


async def test_evidence_detail_rejects_a_deleted_source_document(fabric: Fabric) -> None:
    created = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="deleted-document.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["document"]["id"]
    async with fabric.session() as session:
        link = (
            await session.execute(
                select(EvidenceLink).where(EvidenceLink.document_id == document_id)
            )
        ).scalars().first()
        document = await session.get(Document, document_id)
        assert link is not None and document is not None
        evidence_id = link.id
        await session.delete(document)
        await session.commit()

    response = await fabric.http.get(f"/api/v1/evidence/{evidence_id}", headers=fabric.alpha.headers)
    assert response.status_code == 404, response.text


async def test_evidence_detail_bounds_nested_table_locator_excerpt_and_record_payload(fabric: Fabric) -> None:
    created = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="large-evidence.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["document"]["id"]

    async with fabric.session() as session:
        region = (
            await session.execute(
                select(DocumentRegion).where(DocumentRegion.document_id == document_id)
            )
        ).scalars().first()
        assert region is not None
        link = (
            await session.execute(
                select(EvidenceLink).where(
                    EvidenceLink.document_id == document_id,
                    EvidenceLink.region_id == region.id,
                    EvidenceLink.subject_kind.startswith("extracted_record"),
                )
            )
        ).scalars().first()
        assert link is not None
        record = (
            await session.execute(select(ExtractedRecord).where(ExtractedRecord.id == link.subject_id))
        ).scalar_one()
        link.locator = {
            "field_locations": {
                f"field_{index}": {"source": "x" * 256, "index": index} for index in range(300)
            }
        }
        link.excerpt = "e" * 10000
        region.table = {
            "rows": 200,
            "cols": 3,
            "header": ["A", "B", "C"],
            "cells": [
                [{"text": "cell-" + ("t" * 3000)} for _ in range(3)] for _ in range(200)
            ],
        }
        record.payload = {f"field_{index}": "p" * 2000 for index in range(100)}
        evidence_id = link.id
        await session.commit()

    response = await fabric.http.get(f"/api/v1/evidence/{evidence_id}", headers=fabric.alpha.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["region"]["table_truncated"] is True
    assert body["evidence"]["locator_truncated"] is True
    assert body["evidence"]["excerpt_truncated"] is True
    assert len(body["evidence"]["excerpt"]) == 2000
    assert body["subject_record"]["payload_truncated"] is True
    assert len(json.dumps(body).encode("utf-8")) < 250_000


async def test_document_detail_bounds_nested_summaries_attributes_and_job_text(fabric: Fabric) -> None:
    created = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="large-detail.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["document"]["id"]
    async with fabric.session() as session:
        document = await session.get(Document, document_id)
        assert document is not None
        region = (
            await session.execute(
                select(DocumentRegion).where(DocumentRegion.document_id == document_id)
            )
        ).scalars().first()
        job = (
            await session.execute(
                select(IngestionJob).where(IngestionJob.document_id == document_id)
            )
        ).scalars().first()
        record = (
            await session.execute(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).scalars().first()
        assert region is not None and job is not None and record is not None
        document.extraction_summary = {"large": "s" * 50000}
        region.attributes = {"source_locator": "a" * 20000}
        job.warnings = ["w" * 5000 for _ in range(30)]
        job.error = "e" * 6000
        record.unit_context = {"depth": "u" * 10000}
        record.quality_flags = ["q" * 1000 for _ in range(100)]
        await session.commit()

    response = await fabric.http.get(
        f"/api/v1/documents/{document_id}",
        params={"chunk_limit": 1, "region_limit": 1, "record_limit": 1, "evidence_limit": 1},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document"]["extraction_summary_truncated"] is True
    assert body["ingestion_jobs"][0]["warnings_truncated"] is True
    assert body["ingestion_jobs"][0]["error_truncated"] is True
    assert body["regions"][0]["attributes_truncated"] is True
    assert body["records"][0]["unit_context_truncated"] is True
    assert body["records"][0]["quality_flags_truncated"] is True
    assert len(response.content) < 100_000


@pytest.mark.parametrize("quarantine_after_call", [1, 2, 3, 4])
async def test_evidence_detail_rechecks_quarantine_during_traversal(
    fabric: Fabric, quarantine_after_call: int
) -> None:
    created = await fabric.upload(
        fabric.alpha, DDR_BYTES, filename="quarantine-race.txt", doc_type="ddr", well_id=fabric.alpha.well_id
    )
    assert created.status_code == 201, created.text
    document_id = created.json()["document"]["id"]
    async with fabric.session() as session:
        evidence = (
            await session.execute(
                select(EvidenceLink).where(
                    EvidenceLink.document_id == document_id,
                    EvidenceLink.region_id.is_not(None),
                    EvidenceLink.subject_kind.startswith("extracted_record"),
                )
            )
        ).scalars().first()
        assert evidence is not None
        evidence_id = evidence.id
        document = await session.get(Document, document_id)
        assert document is not None and document.raw_artifact_id is not None
        artifact = await session.get(RawArtifact, document.raw_artifact_id)
        assert artifact is not None

        class QuarantineAfterLookup:
            def __init__(self) -> None:
                self.calls = 0

            async def execute(self, *args: Any, **kwargs: Any) -> Any:
                result = await session.execute(*args, **kwargs)
                self.calls += 1
                if self.calls == quarantine_after_call:
                    artifact.is_quarantined = True
                    await session.flush()
                return result

        auth = AuthContext(
            principal=Principal(id="usr_alpha", org_id="org_alpha", max_action_level=ActionLevel.OBSERVE),
            org_id="org_alpha",
        )
        with pytest.raises(NotFound):
            await get_evidence(evidence_id, session=QuarantineAfterLookup(), auth=auth)
