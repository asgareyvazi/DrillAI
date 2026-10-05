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

from drillai.db.models import DocumentRegion, ExtractedRecord


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

        document_id = await _document(fabric, b"alpha report: mud weight 9.2 ppg\n", filename="alpha.txt")
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
