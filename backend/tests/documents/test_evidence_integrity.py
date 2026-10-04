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

from sqlalchemy import func, select
from tests.documents.conftest import DDR_BYTES, Fabric

from drillai.db.models import (
    Document,
    DocumentChunk,
    DocumentPage,
    DocumentRegion,
    EvidenceLink,
    ExtractedRecord,
)
from drillai.documents.quotes import excerpt_occurs_in, normalize_for_match, verify_quote

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
        regions = {
            region.id: region.text or ""
            for region in (
                await session.execute(
                    select(DocumentRegion).where(DocumentRegion.document_id == document_id)
                )
            )
            .scalars()
            .all()
        }
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
        region_text = regions.get(link.region_id or "", "")
        page_text = page_texts.get(link.page_number or -1, "")
        verified, basis = verify_quote(
            excerpt=link.excerpt,
            region_text=region_text or None,
            page_text=page_text or None,
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
    assert response.status_code == 200, response.text
    body = response.json()
    # The link itself is bravo's and is returned. What must not be returned is anything it points at.
    assert body["evidence"]["id"] == "evd_intruder_probe"
    assert body.get("document") is None, "another tenant's document was disclosed through a link"
    assert body.get("region") is None, "another tenant's region text was disclosed through a link"
    assert body.get("subject_record") is None, "another tenant's record was disclosed through a link"


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
