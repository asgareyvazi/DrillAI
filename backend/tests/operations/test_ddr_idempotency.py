"""DDR promotion: identity, idempotency, partial runs, bounded scans, and an honest dry run.

The promotion path is the one place where a document becomes *domain rows* — operations, events,
surveys and twin revisions. Everything a reviewer needs to defend those rows lives here:

* **record authority** — each promoted row names the document it came from, the extracted record it
  was read from, and the fingerprint that identifies the pair, so a row can always be traced back to
  the sentence it was read from;
* **idempotency without whole-well scans** — reprocessing a report links what already exists and
  creates the rest, and the lookup that decides this reads the document's own rows only;
* **partial-run reconciliation** — a run interrupted half way is completed rather than duplicated,
  which is tested by reconstructing the interrupted state rather than by simulating one;
* **an honest dry run** — it reports the same numbers a real run produces and writes nothing at all,
  including no ledger entry.

The fixture is :data:`tests.fixtures.synthetic_ddr.SYNTHETIC_DDR_CSV`, which declares itself
synthetic in its own text and is read by the real extractors.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from tests.fixtures.fabric import Fabric
from tests.fixtures.synthetic_ddr import SYNTHETIC_DDR_CSV, SYNTHETIC_PROGRAM_TEXT

from drillai.db.models import AuditLog, Event, ExtractedRecord, Operation
from drillai.drilling.ddr import DdrProcessor


async def _upload_report(
    fabric: Fabric,
    tenant=None,
    payload: bytes = SYNTHETIC_DDR_CSV,
    filename: str = "ddr.csv",
    content_type: str = "text/csv",
    doc_type: str = "ddr",
    **fields,
) -> dict:
    tenant = tenant or fabric.alpha
    response = await fabric.upload(
        tenant,
        payload,
        filename=filename,
        content_type=content_type,
        well_id=tenant.well_id,
        wellbore_id=tenant.wellbore_id,
        section_id=tenant.section_id,
        doc_type=doc_type,
        **fields,
    )
    assert response.status_code == 201, response.text
    return response.json()["document"]


async def _process(fabric: Fabric, document_id: str, tenant=None, **payload) -> dict:
    tenant = tenant or fabric.alpha
    response = await fabric.http.post(
        f"/api/v1/documents/{document_id}/process",
        json=payload,
        headers=tenant.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["processing"]


async def _counts(fabric: Fabric) -> tuple[int, int]:
    async with fabric.session() as session:
        operations = (await session.execute(select(func.count()).select_from(Operation))).scalar_one()
        events = (await session.execute(select(func.count()).select_from(Event))).scalar_one()
    return int(operations), int(events)


async def test_a_report_promotes_into_traceable_operations(fabric: Fabric) -> None:
    document = await _upload_report(fabric)
    report = await _process(fabric, document["id"])

    assert report["doc_type"] == "ddr"
    assert report["dry_run"] is False
    assert len(report["operations_created"]) == 5
    assert report["operations_hours"] == pytest.approx(18.5)

    listing = await fabric.http.get(
        "/api/v1/operations", params={"well_id": fabric.alpha.well_id}, headers=fabric.alpha.headers
    )
    operations = listing.json()["items"]
    assert [row["name"] for row in operations] == [
        "Drilling ahead 8-1/2 in section",
        "Made connection",
        "Stuck pipe while POOH",
        "Lost circulation",
        "Circulating and conditioning mud",
    ]
    for row in operations:
        # Record authority: the row says which document and which extracted record it came from, and
        # which rule promoted it.
        assert row["source_document_id"] == document["id"]
        assert row["source_record_id"]
        assert row["source_kind"] == "ddr_promotion"
        assert len(row["promotion_fingerprint"]) == 32
        assert row["data_quality"] == "extracted"
        assert row["operation_class"] == "actual"
    # The shift is reconstructed as a chain: each row follows the one before it.
    assert [row["sequence"] for row in operations] == [1, 2, 3, 4, 5]
    assert operations[1]["predecessor_operation_id"] == operations[0]["id"]
    assert operations[4]["predecessor_operation_id"] == operations[3]["id"]
    assert operations[0]["predecessor_operation_id"] is None
    assert all(row["wellbore_id"] == fabric.alpha.wellbore_id for row in operations)
    assert all(row["section_id"] == fabric.alpha.section_id for row in operations)


async def test_every_promoted_row_has_a_distinct_fingerprint(fabric: Fabric) -> None:
    """The fingerprint identifies *(document, record)* — two rows of one report never share one."""
    document = await _upload_report(fabric)
    await _process(fabric, document["id"])
    listing = await fabric.http.get(
        "/api/v1/operations", params={"well_id": fabric.alpha.well_id}, headers=fabric.alpha.headers
    )
    fingerprints = [row["promotion_fingerprint"] for row in listing.json()["items"]]
    assert len(set(fingerprints)) == len(fingerprints) == 5
    assert all(len(value) == 32 for value in fingerprints)

    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.document_id == document["id"],
                    ExtractedRecord.record_type == "operation_row",
                )
            )
        ).scalars().all()
        # The column is the production rule applied to the real rows, not a value copied around.
        expected = {DdrProcessor._fingerprint(document["id"], record) for record in records}
    assert set(fingerprints) == expected


async def test_reprocessing_a_report_creates_nothing_and_links_instead(fabric: Fabric) -> None:
    document = await _upload_report(fabric)
    first = await _process(fabric, document["id"])
    before = await _counts(fabric)

    second = await _process(fabric, document["id"])
    assert second["operations_created"] == []
    assert second["events_created"] == []
    assert len(second["operations_linked"]) == 5
    assert len(second["records_promoted"]) == 2, "the non-operation records are still re-read"
    # Counts are of *created* rows: a reprocess that reported the linked rows as created would make
    # the caller believe the well had grown.
    assert second["counts"]["operations"] == 0
    assert second["counts"]["operations_linked"] == 5
    assert await _counts(fabric) == before
    assert first["operations_created"] != second["operations_created"]


async def test_a_dry_run_reports_the_truth_and_writes_nothing(fabric: Fabric) -> None:
    """§36: the preview must predict the run, and leave no trace of having been made."""
    document = await _upload_report(fabric)
    preview = await _process(fabric, document["id"], dry_run=True)

    assert preview["dry_run"] is True
    assert preview["counts"]["operations"] == 5
    assert preview["counts"]["events"] == 2
    assert preview["npt_hours_classified"] == pytest.approx(8.5)
    # The preview reports the hours the operations it says it would create actually carry: a preview
    # that claims five operations and no time is the opposite of a forecast.
    assert preview["operations_hours"] == pytest.approx(18.5)
    assert preview["twin_aspects"], "a preview that hides the twin effect is not a preview"
    assert await _counts(fabric) == (0, 0)

    async with fabric.session() as session:
        audit = (
            await session.execute(select(AuditLog).where(AuditLog.action == "document.process"))
        ).scalars().all()
    assert audit == [], "a dry run must not leave a ledger entry claiming a promotion happened"

    real = await _process(fabric, document["id"])
    assert real["counts"]["operations"] == preview["counts"]["operations"]
    assert real["counts"]["events"] == preview["counts"]["events"]
    assert real["npt_hours_classified"] == pytest.approx(preview["npt_hours_classified"])


async def test_a_half_promoted_report_is_completed_not_duplicated(fabric: Fabric) -> None:
    """An interrupted run leaves some rows behind; the next run finishes the job.

    The interrupted state is reconstructed by writing the row the first record would have produced —
    the state a crashed run leaves is exactly this, and the alternative (deleting rows afterwards)
    would be deleting real history to set up a test.
    """
    document = await _upload_report(fabric)
    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord)
                .where(
                    ExtractedRecord.document_id == document["id"],
                    ExtractedRecord.record_type == "operation_row",
                )
                .order_by(ExtractedRecord.id)
            )
        ).scalars().all()
        first = records[0]
        session.add(
            Operation(
                id="opr_interrupted",
                org_id=fabric.org_id(fabric.alpha),
                project_id=fabric.alpha.project_id,
                well_id=fabric.alpha.well_id,
                wellbore_id=fabric.alpha.wellbore_id,
                section_id=fabric.alpha.section_id,
                operation_class="actual",
                sequence=1,
                name="Drilling ahead 8-1/2 in section",
                kind="drilling",
                phase="completed",
                status="completed",
                source="report",
                source_kind="ddr_promotion",
                source_document_id=document["id"],
                source_record_id=first.id,
                promotion_fingerprint=DdrProcessor._fingerprint(document["id"], first),
                data_quality="extracted",
            )
        )
        await session.commit()

    report = await _process(fabric, document["id"])
    assert report["operations_created"], "the missing rows must still be created"
    assert len(report["operations_linked"]) == 1, "the row that already existed is linked, not redone"
    assert len(report["operations_created"]) == 4
    assert await _counts(fabric) == (5, 2)

    listing = await fabric.http.get(
        "/api/v1/operations", params={"well_id": fabric.alpha.well_id}, headers=fabric.alpha.headers
    )
    assert len(listing.json()["items"]) == 5
    assert len({row["name"] for row in listing.json()["items"]}) == 5


class _RowCountingSession:
    """A session proxy that records how many rows each statement returned.

    Only ``execute`` is forwarded: the measured call (``_existing_fingerprints``) uses exactly that,
    and a wider proxy would hide which statement was actually being counted.
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        self.row_counts: list[int] = []

    async def execute(self, statement: Any) -> Any:
        rows = (await self._session.execute(statement)).all()
        self.row_counts.append(len(rows))

        class _Result:
            @staticmethod
            def all() -> list:
                return rows

        return _Result()


async def test_the_existing_row_lookup_reads_only_this_documents_rows(fabric: Fabric) -> None:
    """§34: the idempotency check must not grow with the well's history.

    Two hundred unrelated operations are written to the same well first. A lookup scoped to the well
    would return 205 rows; a lookup scoped to the document returns the five it created, whatever the
    well has accumulated.
    """
    document = await _upload_report(fabric)
    await _process(fabric, document["id"])

    # A second report from the same well: its rows share the well but not the document, which is the
    # distinction the fingerprint and the lookup are built on.
    other_well_document = await _upload_report(
        fabric, payload=SYNTHETIC_DDR_CSV.replace(b"RPT-NF12-014", b"RPT-NF12-015"), filename="other.csv"
    )
    assert other_well_document["id"] != document["id"]

    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord)
                .where(
                    ExtractedRecord.document_id == document["id"],
                    ExtractedRecord.record_type == "operation_row",
                )
                .order_by(ExtractedRecord.id)
            )
        ).scalars().all()
        for index in range(200):
            session.add(
                Operation(
                    id=f"opr_unrelated_{index:03d}",
                    org_id=fabric.org_id(fabric.alpha),
                    project_id=fabric.alpha.project_id,
                    well_id=fabric.alpha.well_id,
                    wellbore_id=fabric.alpha.wellbore_id,
                    operation_class="plan",
                    sequence=1000 + index,
                    name=f"Unrelated operation {index}",
                    kind="other",
                    status="planned",
                )
            )
        await session.commit()

        counting = _RowCountingSession(session)
        processor = DdrProcessor(counting, fabric.org_id(fabric.alpha))
        found = await processor._existing_fingerprints(Operation, document["id"])
        expected = {DdrProcessor._fingerprint(document["id"], record) for record in records}

    assert found == expected
    assert counting.row_counts == [5], "the lookup read rows that cannot belong to this document"

    # And the behaviour it drives is unchanged by the history around it.
    report = await _process(fabric, document["id"])
    assert report["operations_created"] == []
    assert len(report["operations_linked"]) == 5


async def test_a_promoted_event_attributes_the_loss_without_asserting_a_cause(fabric: Fabric) -> None:
    document = await _upload_report(fabric)
    report = await _process(fabric, document["id"])
    assert len(report["events_created"]) == 2

    listing = await fabric.http.get(
        "/api/v1/events", params={"well_id": fabric.alpha.well_id}, headers=fabric.alpha.headers
    )
    events = listing.json()["items"]
    assert all(event["source_document_id"] == document["id"] for event in events)
    assert all(event["source_record_id"] for event in events)
    assert all(event["source_kind"] == "ddr_promotion" for event in events)
    assert all(event["cause_basis"] == "unknown" for event in events)
    assert all(event["root_cause"] is None for event in events)
    assert all(event["classification_source"] == "derived" for event in events)

    stuck_pipe = next(event for event in events if event["npt_category"] == "stuck_pipe")
    assert stuck_pipe["is_npt"] is True
    assert stuck_pipe["npt_hours"] == pytest.approx(6.0)
    # The report's own sentence is kept as the description — the text is not lost, it is just not
    # presented as a diagnosis.
    assert "Stuck pipe at 2408 m" in stuck_pipe["description"]
    # The event is attributed to the operation it was reported alongside, which is where the lost
    # hours are charged.
    operations = (
        await fabric.http.get(
            "/api/v1/operations", params={"well_id": fabric.alpha.well_id}, headers=fabric.alpha.headers
        )
    ).json()["items"]
    stuck_operation = next(row for row in operations if row["name"] == "Stuck pipe while POOH")
    assert stuck_pipe["operation_id"] == stuck_operation["id"]
    assert stuck_pipe["section_id"] == fabric.alpha.section_id
    # The operation's own accounting has to agree with the event that explains it.
    assert stuck_operation["npt_hours"] == pytest.approx(6.0)
    assert stuck_operation["is_productive"] is False


async def test_promotion_writes_one_ledger_entry_for_the_run(fabric: Fabric) -> None:
    document = await _upload_report(fabric)
    await _process(fabric, document["id"])
    async with fabric.session() as session:
        rows = (
            await session.execute(select(AuditLog).where(AuditLog.action == "document.process"))
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.resource_kind == "document"
    assert row.resource_id == document["id"]
    assert row.well_id == fabric.alpha.well_id
    assert row.actor_id == "usr_alpha", "the ledger must name the person who authorised the run"
    assert row.after == {} and row.before == {}
    assert row.details["operations_created"] == 5
    assert row.details["events_created"] == 2
    assert row.details["promotion_rule"] == "promotion_rules_v1"


async def test_a_document_that_is_not_a_ddr_is_refused_with_a_reason(fabric: Fabric) -> None:
    document = await _upload_report(
        fabric, payload=SYNTHETIC_PROGRAM_TEXT, filename="program.txt", doc_type="other"
    )
    report = await _process(fabric, document["id"])
    assert report["operations_created"] == []
    assert report["warnings"] and "not a daily drilling report" in report["warnings"][0]
    assert await _counts(fabric) == (0, 0)


async def test_a_scanned_report_says_it_has_nothing_to_promote(fabric: Fabric) -> None:
    """No extracted records is a fact about the document, not an empty result."""
    from tests.fixtures.documents_bytes import scanned_pdf

    document = await _upload_report(fabric, payload=scanned_pdf(), filename="scan.pdf", content_type="application/pdf")
    assert document["status"] == "partially_extracted"
    report = await _process(fabric, document["id"])
    assert report["records_promoted"] == []
    assert any("no extracted records" in warning for warning in report["warnings"])
    assert await _counts(fabric) == (0, 0)


async def test_a_document_of_another_tenant_cannot_be_promoted(fabric: Fabric) -> None:
    document = await _upload_report(fabric, tenant=fabric.bravo)
    response = await fabric.http.post(
        f"/api/v1/documents/{document['id']}/process", json={}, headers=fabric.alpha.headers
    )
    assert response.status_code == 404, response.text


async def test_another_tenants_rows_are_not_consulted_when_looking_for_duplicates(
    fabric: Fabric,
) -> None:
    """The same report filed by two organizations promotes twice, into each organization's well."""
    alpha_document = await _upload_report(fabric, tenant=fabric.alpha)
    bravo_document = await _upload_report(fabric, tenant=fabric.bravo)
    assert alpha_document["id"] != bravo_document["id"]

    alpha_report = await _process(fabric, alpha_document["id"], tenant=fabric.alpha)
    bravo_report = await _process(fabric, bravo_document["id"], tenant=fabric.bravo)
    assert len(alpha_report["operations_created"]) == 5
    assert len(bravo_report["operations_created"]) == 5

    for tenant, document in ((fabric.alpha, alpha_document), (fabric.bravo, bravo_document)):
        listing = await fabric.http.get(
            "/api/v1/operations", params={"well_id": tenant.well_id}, headers=tenant.headers
        )
        items = listing.json()["items"]
        assert len(items) == 5, f"{tenant.slug} sees {len(items)} operations"
        assert all(row["source_document_id"] == document["id"] for row in items)
    counts = await _counts(fabric)
    assert counts == (10, 4)
