"""The provenance chain: bounded pages, deterministic order, and no traversal out of the tenant.

The chain answers one question — *where did this value come from?* — and the answers that matter are
about what happens at its edges:

* the page is bounded and the cursor continues it exactly, because a document with a thousand
  extracted records must not be answered with a thousand records in one response;
* the same request twice returns the same bytes, so a reviewer can compare two runs;
* a record whose region belongs to another document resolves to nothing rather than leaking that
  region's text — the traversal is checked, not assumed;
* the served filename cannot become a header of its own.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from tests.fixtures.fabric import DDR_BYTES, Fabric
from tests.fixtures.synthetic_ddr import SYNTHETIC_DDR_CSV

from drillai.db.models import (
    Document,
    DocumentChunk,
    DocumentRegion,
    EvidenceLink,
    ExtractedRecord,
    IngestionJob,
    RawArtifact,
)


async def _document(
    fabric: Fabric,
    payload: bytes = SYNTHETIC_DDR_CSV,
    filename: str = "ddr.csv",
    content_type: str = "text/csv",
    **fields,
) -> str:
    response = await fabric.upload(
        fabric.alpha,
        payload,
        filename=filename,
        content_type=content_type,
        well_id=fabric.alpha.well_id,
        wellbore_id=fabric.alpha.wellbore_id,
        doc_type="ddr",
        **fields,
    )
    assert response.status_code == 201, response.text
    return response.json()["document"]["id"]


async def _provenance(fabric: Fabric, document_id: str, **params) -> dict:
    response = await fabric.http.get(
        f"/api/v1/documents/{document_id}/provenance",
        params=params or None,
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_every_record_resolves_to_the_region_and_page_it_came_from(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    body = await _provenance(fabric, document_id)
    assert body["record_count"] == body["records_returned"] > 0
    assert body["chain_truncated"] is False
    assert body["next_cursor"] is None
    for link in body["chain"]:
        assert link["document"]["id"] == document_id
        record = link["record"]
        assert record["method"] and record["method_version"]
        if record["region_id"] is None:
            # A value with no place on the page says so rather than pointing at a plausible region.
            assert record["region_unknown"] is True
            assert link["region"] is None
        else:
            assert link["region"]["id"] == record["region_id"]
            assert link["region"]["page_number"] >= 1


async def test_the_chain_is_the_same_chain_twice(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    first = await _provenance(fabric, document_id)
    second = await _provenance(fabric, document_id)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


async def test_the_chain_is_paged_by_cursor_and_the_pages_join_exactly(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    whole = await _provenance(fabric, document_id, limit=1000)
    assert whole["record_count"] > 3, "the fixture must produce enough records to page"

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await _provenance(fabric, document_id, limit=3, cursor=cursor)
        pages += 1
        seen.extend(link["record"]["id"] for link in page["chain"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 20, "the cursor never reached the end"
    assert pages > 1, "a three-row page must not have returned everything"
    assert seen == [link["record"]["id"] for link in whole["chain"]]
    assert len(set(seen)) == len(seen)


async def test_ingestion_jobs_are_bounded_and_offset_pageable(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    duplicate_jobs = []
    for _ in range(2):
        response = await fabric.upload(
            fabric.alpha,
            SYNTHETIC_DDR_CSV,
            filename="ddr.csv",
            content_type="text/csv",
            well_id=fabric.alpha.well_id,
            wellbore_id=fabric.alpha.wellbore_id,
            doc_type="ddr",
        )
        assert response.status_code == 201, response.text
        assert response.json()["document"]["id"] == document_id
        duplicate_jobs.append(response.json()["job"]["id"])

    first_detail = await fabric.http.get(
        f"/api/v1/documents/{document_id}",
        params={"job_limit": 1, "job_offset": 0},
        headers=fabric.alpha.headers,
    )
    assert first_detail.status_code == 200, first_detail.text
    first_body = first_detail.json()
    assert first_body["ingestion_job_count"] == 3
    assert first_body["ingestion_jobs_returned"] == 1
    assert first_body["ingestion_jobs_truncated"] is True
    assert first_body["next_ingestion_job_offset"] == 1

    second_detail = await fabric.http.get(
        f"/api/v1/documents/{document_id}",
        params={"job_limit": 1, "job_offset": 1},
        headers=fabric.alpha.headers,
    )
    assert second_detail.status_code == 200, second_detail.text
    assert second_detail.json()["ingestion_jobs_returned"] == 1
    assert first_body["ingestion_jobs"][0]["id"] != second_detail.json()["ingestion_jobs"][0]["id"]

    first_page = await fabric.http.get(
        f"/api/v1/documents/{document_id}/ingestion",
        params={"limit": 1, "offset": 0},
        headers=fabric.alpha.headers,
    )
    second_page = await fabric.http.get(
        f"/api/v1/documents/{document_id}/ingestion",
        params={"limit": 1, "offset": 1},
        headers=fabric.alpha.headers,
    )
    assert first_page.status_code == second_page.status_code == 200
    assert first_page.json()["total"] == second_page.json()["total"] == 3
    assert first_page.json()["truncated"] is True
    assert first_page.json()["next_offset"] == 1
    assert first_page.json()["items"][0]["id"] != second_page.json()["items"][0]["id"]
    assert len(set(duplicate_jobs)) == 2


async def test_evidence_summary_aggregates_without_trusting_invalid_scores(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    async with fabric.session() as session:
        links = (
            await session.execute(
                select(EvidenceLink).where(EvidenceLink.document_id == document_id)
            )
        ).scalars().all()
        assert links
        for link in links:
            link.confidence = 1.5
        await session.commit()

    response = await fabric.http.get(
        "/api/v1/evidence/summary",
        params={"document_id": document_id},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    summary = response.json()
    assert summary["link_count"] == len(links)
    assert summary["mean_confidence"] is None
    assert sum(summary["by_kind"].values()) == summary["link_count"]
    assert any("invalid confidence values are excluded" in note for note in summary["limitations"])


async def test_an_unknown_cursor_is_refused_rather_than_ignored(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    response = await fabric.http.get(
        f"/api/v1/documents/{document_id}/provenance",
        params={"cursor": "not-a-record-id"},
        headers=fabric.alpha.headers,
    )
    # A string that names nothing is refused. Comparing it as if it were a position would return
    # whichever page it happens to sort before — an answer that looks valid and is not.
    assert response.status_code == 422, response.text
    assert "does not name an extracted record" in response.json()["error"]["message"]


async def test_a_region_of_another_document_is_never_resolved_through_a_record(
    fabric: Fabric,
) -> None:
    """The record's ``region_id`` is data; the resolution is a traversal and is checked.

    A record whose region belongs to a *different* document is exactly the case a naive join serves
    happily — it would answer with another document's page text. The region must resolve to nothing.
    """
    foreign_document = await fabric.upload(
        fabric.bravo,
        DDR_BYTES,
        filename="bravo.txt",
        well_id=fabric.bravo.well_id,
        doc_type="ddr",
    )
    assert foreign_document.status_code == 201, foreign_document.text
    foreign_document_id = foreign_document.json()["document"]["id"]

    async with fabric.session() as session:
        foreign_region = (
            await session.execute(
                select(DocumentRegion).where(DocumentRegion.document_id == foreign_document_id)
            )
        ).scalars().first()
        assert foreign_region is not None, "the fixture must have produced a region"

        document_id = await _document(
            fabric,
            b"alpha report: mud weight 9.2 ppg\n",
            filename="alpha.txt",
            content_type="text/plain",
        )
        record = (
            await session.execute(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).scalars().first()
        assert record is not None
        record_id = record.id
        hostile_region_text = foreign_region.text
        record.region_id = foreign_region.id
        await session.commit()

    body = await _provenance(fabric, document_id)
    link = next(item for item in body["chain"] if item["record"]["id"] == record_id)
    assert link["region"] is None, "another document's region text was served through this chain"
    assert body["regions_returned"] == 0
    assert hostile_region_text not in json.dumps(body)


async def test_region_text_is_bounded_in_the_chain(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    body = await _provenance(fabric, document_id)
    for link in body["chain"]:
        if link["region"] is not None:
            assert len(link["region"]["text"]) <= 2000


async def test_the_document_detail_pages_records_and_says_what_it_cut(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    detail = (
        await fabric.http.get(f"/api/v1/documents/{document_id}", headers=fabric.alpha.headers)
    ).json()
    assert detail["record_count"] > 2
    assert detail["records_returned"] == len(detail["records"])
    assert detail["records_truncated"] is False

    first = (
        await fabric.http.get(
            f"/api/v1/documents/{document_id}",
            params={"record_limit": 2, "chunk_limit": 1, "region_limit": 1, "evidence_limit": 1},
            headers=fabric.alpha.headers,
        )
    ).json()
    assert len(first["records"]) == 2
    assert first["records_truncated"] is True
    assert first["next_record_cursor"] == first["records"][-1]["id"]
    assert first["evidence_count"] >= 1
    assert first["evidence_truncated"] is True
    assert first["limits"]["records"] == 2

    second = (
        await fabric.http.get(
            f"/api/v1/documents/{document_id}",
            params={"record_limit": 2, "record_after": first["next_record_cursor"]},
            headers=fabric.alpha.headers,
        )
    ).json()
    assert second["records"], "the keyset continuation returned nothing"
    assert {row["id"] for row in second["records"]} & {row["id"] for row in first["records"]} == set()


async def test_evidence_links_are_bounded_and_counted(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    detail = (
        await fabric.http.get(
            f"/api/v1/documents/{document_id}",
            params={"evidence_limit": 1},
            headers=fabric.alpha.headers,
        )
    ).json()
    assert detail["evidence_count"] > 1
    assert len(detail["evidence_links"]) == 1
    assert detail["evidence_truncated"] is True


async def test_the_other_tenant_sees_none_of_it(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    for url in (
        f"/api/v1/documents/{document_id}",
        f"/api/v1/documents/{document_id}/provenance",
        f"/api/v1/documents/{document_id}/file",
    ):
        assert (await fabric.http.get(url, headers=fabric.bravo.headers)).status_code == 404


async def test_document_views_filter_corrupt_cross_tenant_child_rows(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    foreign_org_id = fabric.org_id(fabric.bravo)
    secret = "cross-tenant-child-secret-should-not-leave-the-database"
    async with fabric.session() as session:
        foreign_region = DocumentRegion(
            org_id=foreign_org_id,
            document_id=document_id,
            page_number=1,
            kind="text",
            order_index=900,
            text=secret,
            attributes={"probe": secret},
        )
        session.add(foreign_region)
        await session.flush()
        session.add_all(
            [
                DocumentChunk(
                    org_id=foreign_org_id,
                    document_id=document_id,
                    chunk_index=900,
                    page_number=1,
                    text=secret,
                    kind="text",
                ),
                ExtractedRecord(
                    org_id=foreign_org_id,
                    document_id=document_id,
                    region_id=foreign_region.id,
                    record_type="tenant_secret",
                    payload_schema_key="test.tenant_secret",
                    payload={"value": secret},
                    page_number=1,
                ),
                EvidenceLink(
                    org_id=foreign_org_id,
                    subject_kind="test",
                    subject_id="tenant_secret",
                    evidence_kind="document_region",
                    document_id=document_id,
                    region_id=foreign_region.id,
                    excerpt=secret,
                    locator={"secret": secret},
                ),
                IngestionJob(
                    org_id=foreign_org_id,
                    document_id=document_id,
                    status="failed",
                    error=secret,
                    warnings=[secret],
                ),
            ]
        )
        await session.commit()

    detail_response = await fabric.http.get(
        f"/api/v1/documents/{document_id}", headers=fabric.alpha.headers
    )
    provenance_response = await fabric.http.get(
        f"/api/v1/documents/{document_id}/provenance", headers=fabric.alpha.headers
    )
    ingestion_response = await fabric.http.get(
        f"/api/v1/documents/{document_id}/ingestion", headers=fabric.alpha.headers
    )
    assert detail_response.status_code == 200, detail_response.text
    assert provenance_response.status_code == 200, provenance_response.text
    assert ingestion_response.status_code == 200, ingestion_response.text
    for body in (detail_response.json(), provenance_response.json(), ingestion_response.json()):
        assert secret not in json.dumps(body)
    detail = detail_response.json()
    assert all(chunk["text"] != secret for chunk in detail["chunks"])
    assert all(record["record_type"] != "tenant_secret" for record in detail["records"])
    assert all(job["error"] != secret for job in detail["ingestion_jobs"])
    assert all(job["error"] != secret for job in ingestion_response.json()["items"])
    assert all(link["excerpt"] != secret for link in detail["evidence_links"])


async def test_quarantined_raw_artifact_is_not_served_as_a_document(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    async with fabric.session() as session:
        document = await session.get(Document, document_id)
        assert document is not None and document.raw_artifact_id is not None
        artifact = await session.get(RawArtifact, document.raw_artifact_id)
        assert artifact is not None
        evidence_id = await session.scalar(
            select(EvidenceLink.id).where(EvidenceLink.document_id == document_id)
        )
        assert evidence_id is not None
        artifact.is_quarantined = True
        await session.commit()

    blocked_retry = await fabric.upload(
        fabric.alpha,
        SYNTHETIC_DDR_CSV,
        filename="ddr.csv",
        content_type="text/csv",
        well_id=fabric.alpha.well_id,
        wellbore_id=fabric.alpha.wellbore_id,
        doc_type="ddr",
    )
    assert blocked_retry.status_code == 422, blocked_retry.text

    for suffix in ("", "/ingestion", "/provenance", "/file", "/permissions"):
        response = await fabric.http.get(
            f"/api/v1/documents/{document_id}{suffix}", headers=fabric.alpha.headers
        )
        assert response.status_code == 404, f"{suffix}: {response.text}"
    listing = await fabric.http.get("/api/v1/documents", headers=fabric.alpha.headers)
    assert listing.status_code == 200, listing.text
    assert document_id not in {item["id"] for item in listing.json()["items"]}

    evidence_list = await fabric.http.get(
        "/api/v1/evidence", params={"document_id": document_id}, headers=fabric.alpha.headers
    )
    evidence_summary = await fabric.http.get(
        "/api/v1/evidence/summary", params={"document_id": document_id}, headers=fabric.alpha.headers
    )
    evidence_detail = await fabric.http.get(
        f"/api/v1/evidence/{evidence_id}", headers=fabric.alpha.headers
    )
    assert evidence_list.status_code == 200 and evidence_list.json()["total"] == 0
    assert evidence_summary.status_code == 200 and evidence_summary.json()["link_count"] == 0
    assert evidence_detail.status_code == 404, evidence_detail.text


async def test_document_download_rejects_a_cross_tenant_raw_artifact_pointer(fabric: Fabric) -> None:
    alpha_upload = await fabric.upload(
        fabric.alpha,
        b"alpha-owned document bytes that must remain private",
        filename="alpha.txt",
        well_id=fabric.alpha.well_id,
    )
    bravo_upload = await fabric.upload(
        fabric.bravo,
        b"bravo-only private source bytes",
        filename="bravo.txt",
        well_id=fabric.bravo.well_id,
    )
    assert alpha_upload.status_code == 201, alpha_upload.text
    assert bravo_upload.status_code == 201, bravo_upload.text
    alpha_document_id = alpha_upload.json()["document"]["id"]
    bravo_document_id = bravo_upload.json()["document"]["id"]

    async with fabric.session() as session:
        alpha_document = await session.get(Document, alpha_document_id)
        bravo_document = await session.get(Document, bravo_document_id)
        assert alpha_document is not None and bravo_document is not None
        foreign_artifact = await session.get(RawArtifact, bravo_document.raw_artifact_id)
        assert foreign_artifact is not None and foreign_artifact.org_id == fabric.org_id(fabric.bravo)
        alpha_document.raw_artifact_id = foreign_artifact.id
        await session.commit()

    response = await fabric.http.get(
        f"/api/v1/documents/{alpha_document_id}/file", headers=fabric.alpha.headers
    )
    assert response.status_code == 404, response.text
    assert b"bravo-only private source bytes" not in response.content


async def test_exactly_full_final_pages_do_not_advertise_a_nonexistent_cursor(fabric: Fabric) -> None:
    document_id = await _document(fabric)
    whole = await _provenance(fabric, document_id, limit=1000)
    assert whole["record_count"] > 1
    first_id = whole["chain"][0]["record"]["id"]
    tail = await _provenance(
        fabric,
        document_id,
        limit=whole["record_count"] - 1,
        cursor=first_id,
    )
    assert len(tail["chain"]) == whole["record_count"] - 1
    assert tail["chain_truncated"] is False
    assert tail["next_cursor"] is None

    detail = (
        await fabric.http.get(f"/api/v1/documents/{document_id}", headers=fabric.alpha.headers)
    ).json()
    first_detail = await fabric.http.get(
        f"/api/v1/documents/{document_id}",
        params={"record_limit": 1},
        headers=fabric.alpha.headers,
    )
    assert first_detail.status_code == 200, first_detail.text
    detail_cursor = first_detail.json()["next_record_cursor"]
    assert detail_cursor is not None
    detail_tail = await fabric.http.get(
        f"/api/v1/documents/{document_id}",
        params={"record_limit": detail["record_count"] - 1, "record_after": detail_cursor},
        headers=fabric.alpha.headers,
    )
    assert detail_tail.status_code == 200, detail_tail.text
    tail_body = detail_tail.json()
    assert tail_body["records_returned"] == detail["record_count"] - 1
    assert tail_body["records_truncated"] is False
    assert tail_body["next_record_cursor"] is None


async def test_a_hostile_filename_cannot_become_a_response_header(fabric: Fabric) -> None:
    """The upload's filename is attacker-controlled text that ends up in a header.

    A quote ends the quoted filename parameter, and a control character can end the header line
    entirely. Both are removed from the ASCII form; the original travels percent-encoded.
    """
    hostile = 'q1"\r\nX-Injected: yes; attachment=eve.txt'
    uploaded = await fabric.upload(
        fabric.alpha,
        b"mud weight 9.2 ppg\n",
        filename=hostile,
        well_id=fabric.alpha.well_id,
    )
    assert uploaded.status_code == 201, uploaded.text
    document_id = uploaded.json()["document"]["id"]

    response = await fabric.http.get(
        f"/api/v1/documents/{document_id}/file", headers=fabric.alpha.headers
    )
    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    assert "\r" not in disposition and "\n" not in disposition
    # The ASCII form is what a client that ignores `filename*` reads, so the characters that could
    # close it early or start a new parameter must not survive in it.
    ascii_form = disposition.split('filename="', 1)[1].split('"', 1)[0]
    assert '"' not in ascii_form and ";" not in ascii_form
    assert response.headers.get("x-injected") is None
    assert "filename*=UTF-8''" in disposition
    assert response.content == b"mud weight 9.2 ppg\n"


async def test_unicode_filename_download_has_an_ascii_fallback(fabric: Fabric) -> None:
    from urllib.parse import quote

    filename = "report-🛢.pdf"
    uploaded = await fabric.upload(
        fabric.alpha,
        b"Unicode filename report content",
        filename=filename,
        well_id=fabric.alpha.well_id,
    )
    assert uploaded.status_code == 201, uploaded.text

    response = await fabric.http.get(
        f"/api/v1/documents/{uploaded.json()['document']['id']}/file",
        headers=fabric.alpha.headers,
    )

    assert response.status_code == 200, response.text
    disposition = response.headers["content-disposition"]
    ascii_form = disposition.split('filename="', 1)[1].split('"', 1)[0]
    assert all(0x20 <= ord(character) < 0x7F for character in ascii_form)
    assert f"filename*=UTF-8''{quote(filename, safe='')}" in disposition
    assert response.content == b"Unicode filename report content"
