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

import asyncio
import datetime as dt
from typing import Any

import pytest
from sqlalchemy import func, select
from tests.fixtures.fabric import Fabric
from tests.fixtures.synthetic_ddr import SYNTHETIC_DDR_CSV, SYNTHETIC_PROGRAM_TEXT

from drillai.db.models import (
    AuditLog,
    Document,
    Event,
    ExtractedRecord,
    Operation,
    Trajectory,
    TrajectoryStation,
)
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


async def test_zero_measured_depth_keeps_priority_on_create_and_reconciliation(fabric: Fabric) -> None:
    """A zero-metre MD is present data, not a false-y request to use an older fallback."""
    document = await _upload_report(fabric)
    async with fabric.session() as session:
        record = (
            await session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.document_id == document["id"],
                    ExtractedRecord.record_type == "operation_row",
                )
            )
        ).scalars().all()
        stuck_pipe = next(
            row for row in record if (row.payload or {}).get("activity") == "Stuck pipe while POOH"
        )
        stuck_pipe_id = stuck_pipe.id
        stuck_pipe.payload = {**(stuck_pipe.payload or {}), "depth_md_si": 0.0}
        # Preserve a non-zero lower-priority extracted-field fallback to expose an `or` regression.
        stuck_pipe.depth_md_si = 2408.0
        await session.commit()

    first_report = await _process(fabric, document["id"])
    async with fabric.session() as session:
        event = (
            await session.execute(
                select(Event).where(Event.source_record_id == stuck_pipe_id)
            )
        ).scalar_one()
        event_id = event.id
        assert event_id in first_report["events_created"]
        operation = await session.get(Operation, event.operation_id)
        assert operation is not None
        assert operation.depth_to_md_si == 0.0
        assert event.depth_md_si == 0.0

        # A source retry must preserve the same zero-valued precedence when reconciling an existing
        # derived event, rather than refreshing it from the record's non-zero fallback.
        event.depth_md_si = 2408.0
        await session.commit()

    second_report = await _process(fabric, document["id"])
    assert event_id in second_report["events_updated"]
    async with fabric.session() as session:
        event = (
            await session.execute(
                select(Event).where(Event.source_record_id == stuck_pipe.id)
            )
        ).scalar_one()
        assert event.depth_md_si == 0.0


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
    assert len(second["records_promoted"]) == 1, "only confidence-eligible non-operation records are promoted again"
    assert second["records_needing_review"], "low-confidence parameter records must remain under review"
    # Counts are of *created* rows: a reprocess that reported the linked rows as created would make
    # the caller believe the well had grown.
    assert second["counts"]["operations"] == 0
    assert second["counts"]["operations_linked"] == 5
    assert await _counts(fabric) == before
    assert first["operations_created"] != second["operations_created"]


async def test_concurrent_retries_of_one_document_do_not_duplicate_promotions(fabric: Fabric) -> None:
    document = await _upload_report(fabric)
    responses = await asyncio.gather(
        fabric.http.post(
            f"/api/v1/documents/{document['id']}/process", json={}, headers=fabric.alpha.headers
        ),
        fabric.http.post(
            f"/api/v1/documents/{document['id']}/process", json={}, headers=fabric.alpha.headers
        ),
    )
    assert [response.status_code for response in responses] == [200, 200], [
        response.text for response in responses
    ]
    reports = [response.json()["processing"] for response in responses]
    assert sum(len(report["operations_created"]) for report in reports) == 5
    assert sum(len(report["operations_linked"]) for report in reports) == 5
    assert sum(len(report["events_created"]) for report in reports) == 2
    assert sum(report["counts"]["events_updated"] for report in reports) == 0
    assert await _counts(fabric) == (5, 2)


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
        first_name = "Drilling ahead 8-1/2 in section"
        first = next(
            (record for record in records if (record.payload or {}).get("activity") == first_name), None
        )
        assert first is not None, "the synthetic DDR must contain its first drilling operation"
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
                name=first_name,
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
    items = listing.json()["items"]
    assert len(items) == 5
    assert len({row["name"] for row in items}) == 5
    first_row = next(row for row in items if row["name"] == first_name)
    second_row = next(row for row in items if row["name"] == "Made connection")
    assert second_row["predecessor_operation_id"] == first_row["id"], (
        "retry must reconnect the missing part of the original operation chain"
    )


class _RowCountingSession:
    """A session proxy that records how many rows each statement returned.

    Only ``execute`` is forwarded: the measured call (``_existing_operations_for_document``) uses
    exactly that, and a wider proxy would hide which statement was actually being counted.
    """

    def __init__(self, session: Any) -> None:
        self._session = session
        self.row_counts: list[int] = []

    async def execute(self, statement: Any) -> Any:
        rows = (await self._session.execute(statement)).scalars().all()
        self.row_counts.append(len(rows))

        class _Result:
            @staticmethod
            def scalars() -> _Result:
                return _Result()

            @staticmethod
            def all() -> list:
                return rows

        return _Result()


async def test_failed_promotion_rolls_back_operations_events_and_run_audit(
    fabric: Fabric, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = await _upload_report(fabric)

    async def fail_after_operations(*_args, **_kwargs):
        raise RuntimeError("injected failure after operation flush")

    monkeypatch.setattr(DdrProcessor, "_promote_npt_events", fail_after_operations)
    with pytest.raises(RuntimeError, match="injected failure"):
        async with fabric.app.state.database.session() as session:
            await DdrProcessor(
                session, fabric.org_id(fabric.alpha), actor_id="transaction-test"
            ).process(document["id"])

    assert await _counts(fabric) == (0, 0)
    async with fabric.session() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "document.process",
                    AuditLog.resource_id == document["id"],
                )
            )
        ).scalars().all()
        operations = (
            await session.execute(
                select(Operation).where(Operation.source_document_id == document["id"])
            )
        ).scalars().all()
    assert audit == []
    assert operations == []


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

        first_operation = (
            await session.execute(
                select(Operation).where(
                    Operation.source_document_id == document["id"],
                    Operation.source_record_id == records[0].id,
                )
            )
        ).scalar_one()
        legacy_fingerprint = DdrProcessor._fingerprint(document["id"], records[0])
        first_operation.promotion_fingerprint = None
        first_operation.attributes = {"promotion_fingerprint": legacy_fingerprint}
        await session.flush()

        document_row = (
            await session.execute(select(Document).where(Document.id == document["id"]))
        ).scalar_one()
        counting = _RowCountingSession(session)
        processor = DdrProcessor(counting, fabric.org_id(fabric.alpha))
        existing = await processor._existing_operations_for_document(document_row)
        found = {
            fingerprint
            for operation in existing
            for fingerprint in (
                operation.promotion_fingerprint,
                (operation.attributes or {}).get("promotion_fingerprint"),
            )
            if fingerprint
        }
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


async def test_retry_refreshes_an_open_derived_event_after_record_reclassification(
    fabric: Fabric,
) -> None:
    document = await _upload_report(fabric)
    first = await _process(fabric, document["id"])
    assert first["events_created"]

    async with fabric.session() as session:
        record = (
            await session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.document_id == document["id"],
                    ExtractedRecord.record_type == "operation_row",
                )
            )
        ).scalars().all()
        target = next(row for row in record if (row.payload or {}).get("activity") == "Lost circulation")
        following_record = next(
            row for row in record if (row.payload or {}).get("activity") == "Circulating and conditioning mud"
        )
        event = (
            await session.execute(
                select(Event).where(
                    Event.org_id == fabric.org_id(fabric.alpha),
                    Event.source_record_id == target.id,
                    Event.source_kind == "ddr_promotion",
                )
            )
        ).scalar_one()
        operation = (
            await session.execute(
                select(Operation).where(
                    Operation.org_id == fabric.org_id(fabric.alpha),
                    Operation.source_document_id == document["id"],
                    Operation.source_record_id == target.id,
                )
            )
        ).scalar_one()
        following_operation = (
            await session.execute(
                select(Operation).where(
                    Operation.org_id == fabric.org_id(fabric.alpha),
                    Operation.source_document_id == document["id"],
                    Operation.source_record_id == following_record.id,
                )
            )
        ).scalar_one()
        assert event.category == "lost_circulation"
        target.payload = {
            **target.payload,
            "activity": "Stuck pipe while POOH",
            "description": "Stuck pipe while pulling out of hole; jarring attempted.",
            "duration_hours": 3.5,
        }
        await session.commit()
        event_id = event.id
        operation_id = operation.id
        record_id = target.id
        following_operation_id = following_operation.id
        following_operation_start = following_operation.actual_start

    preview = await _process(fabric, document["id"], dry_run=True)
    assert preview["events_updated"] == [event_id]
    assert preview["operations_updated"] == [operation_id, following_operation_id]
    assert preview["counts"]["events_updated"] == 1
    async with fabric.session() as session:
        unchanged = (await session.execute(select(Event).where(Event.id == event_id))).scalar_one()
        assert unchanged.category == "lost_circulation", "dry-run must not write the reclassification"
        assert unchanged.npt_hours == pytest.approx(2.5)

    second = await _process(fabric, document["id"])
    assert second["events_created"] == []
    assert second["events_updated"] == [event_id]
    assert second["operations_updated"] == [operation_id, following_operation_id]
    assert second["counts"]["events_updated"] == 1
    async with fabric.session() as session:
        refreshed = (await session.execute(select(Event).where(Event.id == event_id))).scalar_one()
        assert refreshed.category == "stuck_pipe"
        assert refreshed.npt_category == "stuck_pipe"
        assert refreshed.npt_code == "STUCK_PIPE"
        assert refreshed.classification_source == "derived"
        assert refreshed.source_record_id == record_id
        assert "Stuck pipe while pulling out" in refreshed.description
        assert refreshed.npt_hours == pytest.approx(3.5)
        assert refreshed.duration_hours == pytest.approx(3.5)
        assert refreshed.is_npt is True
        operation = (await session.execute(select(Operation).where(Operation.id == refreshed.operation_id))).scalar_one()
        following = (
            await session.execute(select(Operation).where(Operation.id == following_operation_id))
        ).scalar_one()
        assert following_operation_start is not None
        assert following.actual_start == following_operation_start + dt.timedelta(hours=1)
        assert operation.id == operation_id
        assert operation.name == "Stuck pipe while POOH"
        assert operation.kind == "jarring"
        assert operation.actual_duration_hours == pytest.approx(3.5)
        assert operation.npt_hours == pytest.approx(3.5)
        assert operation.is_productive is False
        operation_audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "operation.update",
                    AuditLog.resource_id == operation_id,
                )
            )
        ).scalars().all()
        audits = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "event.update",
                    AuditLog.resource_id == event_id,
                )
            )
        ).scalars().all()
    assert len(audits) == 1
    assert audits[0].details["reason"] == "DDR source record was reclassified on retry"
    assert audits[0].before["category"] == "lost_circulation"
    assert audits[0].after["category"] == "stuck_pipe"
    assert audits[0].before["ended_at"] == "2026-03-15T17:30:00+00:00"
    assert audits[0].after["ended_at"] == "2026-03-15T18:30:00+00:00"
    assert audits[0].details["operation_npt_rollup"] == {
        "operation_id": operation_id,
        "before": 2.5,
        "after": 3.5,
    }
    assert audits[0].details["audit_metadata"]["source"] == "ddr_reconciliation"
    assert len(operation_audits) == 1
    assert operation_audits[0].details["audit_metadata"] == {
        "source": "ddr_reconciliation",
        "source_document_id": document["id"],
        "source_record_id": record_id,
    }

    third = await _process(fabric, document["id"])
    assert third["operations_updated"] == []
    assert third["events_updated"] == []


async def test_retry_preserves_a_human_operation_edit_even_if_reason_matches_system_reason(
    fabric: Fabric,
) -> None:
    document = await _upload_report(fabric)
    await _process(fabric, document["id"])
    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.document_id == document["id"],
                    ExtractedRecord.record_type == "operation_row",
                )
            )
        ).scalars().all()
        record = next(
            item for item in records if (item.payload or {}).get("activity") == "Drilling ahead 8-1/2 in section"
        )
        operation = (
            await session.execute(
                select(Operation).where(
                    Operation.source_document_id == document["id"],
                    Operation.source_record_id == record.id,
                )
            )
        ).scalar_one()
        operation_id, record_id = operation.id, record.id
        operation_version = operation.updated_at.isoformat()

    human_edit = await fabric.http.patch(
        f"/api/v1/operations/{operation_id}",
        json={
            "expected_updated_at": operation_version,
            # Matching a system message must not turn a user edit into a system edit.
            "reason": "DDR source record changed on retry",
            "changes": {"name": "Human-verified operation description"},
        },
        headers=fabric.alpha.headers,
    )
    assert human_edit.status_code == 200, human_edit.text

    async with fabric.session() as session:
        record = (await session.execute(select(ExtractedRecord).where(ExtractedRecord.id == record_id))).scalar_one()
        record.payload = {
            **record.payload,
            "activity": "Reclassified drilling activity",
            "description": "Updated source description; the operator's correction remains authoritative.",
            "duration_hours": 9.5,
        }
        await session.commit()

    preview = await _process(fabric, document["id"], dry_run=True)
    assert operation_id not in preview["operations_updated"]
    assert any("person-authored update" in warning for warning in preview["warnings"])
    async with fabric.session() as session:
        unchanged = (await session.execute(select(Operation).where(Operation.id == operation_id))).scalar_one()
        assert unchanged.name == "Human-verified operation description"
        assert unchanged.actual_duration_hours == pytest.approx(8.5)

    retried = await _process(fabric, document["id"])
    assert operation_id not in retried["operations_updated"]
    async with fabric.session() as session:
        unchanged = (await session.execute(select(Operation).where(Operation.id == operation_id))).scalar_one()
        assert unchanged.name == "Human-verified operation description"
        assert unchanged.actual_duration_hours == pytest.approx(8.5)


async def test_retry_preserves_a_human_event_edit_while_the_event_remains_open_and_derived(
    fabric: Fabric,
) -> None:
    document = await _upload_report(fabric)
    await _process(fabric, document["id"])
    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.document_id == document["id"],
                    ExtractedRecord.record_type == "operation_row",
                )
            )
        ).scalars().all()
        record = next(item for item in records if (item.payload or {}).get("activity") == "Lost circulation")
        event = (
            await session.execute(
                select(Event).where(
                    Event.source_document_id == document["id"],
                    Event.source_record_id == record.id,
                )
            )
        ).scalar_one()
        event_id, record_id = event.id, record.id
        event_version = event.updated_at.isoformat()

    human_edit = await fabric.http.patch(
        f"/api/v1/events/{event_id}",
        json={
            "expected_updated_at": event_version,
            "reason": "DDR source record was reclassified on retry",
            "changes": {"description": "Human-verified incident description."},
        },
        headers=fabric.alpha.headers,
    )
    assert human_edit.status_code == 200, human_edit.text
    assert human_edit.json()["status"] == "open"
    assert human_edit.json()["classification_source"] == "derived"

    async with fabric.session() as session:
        record = (await session.execute(select(ExtractedRecord).where(ExtractedRecord.id == record_id))).scalar_one()
        record.payload = {
            **record.payload,
            "activity": "Stuck pipe while POOH",
            "description": "Stuck pipe while pulling out of hole; jarring attempted.",
            "duration_hours": 3.5,
        }
        await session.commit()

    preview = await _process(fabric, document["id"], dry_run=True)
    assert event_id not in preview["events_updated"]
    assert any("person-authored edit" in warning for warning in preview["warnings"])
    retried = await _process(fabric, document["id"])
    assert event_id not in retried["events_updated"]
    async with fabric.session() as session:
        retained = (await session.execute(select(Event).where(Event.id == event_id))).scalar_one()
        assert retained.description == "Human-verified incident description."
        assert retained.category == "lost_circulation"
        assert retained.npt_hours == pytest.approx(2.5)
        assert retained.status == "open"
        assert retained.classification_source == "derived"


async def test_invalid_report_duration_is_reviewed_and_not_promoted(fabric: Fabric) -> None:
    document = await _upload_report(fabric)
    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.document_id == document["id"],
                    ExtractedRecord.record_type == "operation_row",
                )
            )
        ).scalars().all()
        target = next(
            record
            for record in records
            if (record.payload or {}).get("activity") == "Drilling ahead 8-1/2 in section"
        )
        target.payload = {**target.payload, "duration_hours": -0.5}
        record_id = target.id
        await session.commit()

    report = await _process(fabric, document["id"])
    rejected = next(row for row in report["not_promoted"] if row.get("record_id") == record_id)
    assert "invalid duration field(s) duration_hours" in rejected["reason"]
    assert record_id in report["records_needing_review"]
    assert len(report["operations_created"]) == 4
    async with fabric.session() as session:
        record = (await session.execute(select(ExtractedRecord).where(ExtractedRecord.id == record_id))).scalar_one()
        operation = (
            await session.execute(
                select(Operation).where(Operation.source_record_id == record_id)
            )
        ).scalar_one_or_none()
        event = (
            await session.execute(select(Event).where(Event.source_record_id == record_id))
        ).scalar_one_or_none()
    assert record.promoted_to_id is None
    assert operation is None
    assert event is None


@pytest.mark.parametrize("state", ["rejected", "superseded", "low_confidence", "invalid_duration"])
async def test_ineligible_source_keeps_prior_promotions_visible_for_review(
    fabric: Fabric, state: str
) -> None:
    document = await _upload_report(fabric)
    await _process(fabric, document["id"])

    async with fabric.session() as session:
        row = (
            await session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.document_id == document["id"],
                    ExtractedRecord.record_type == "operation_row",
                )
            )
        ).scalars().all()
        record = next(item for item in row if (item.payload or {}).get("activity") == "Lost circulation")
        if state == "low_confidence":
            record.confidence = 0.59
        elif state != "invalid_duration":
            record.validation_state = state
        record.payload = {
            **record.payload,
            "activity": "Stuck pipe while POOH",
            "description": "Reclassified source wording must not overwrite an ineligible promotion.",
            "duration_hours": 100.0 if state == "invalid_duration" else 3.5,
        }
        operation = (
            await session.execute(
                select(Operation).where(
                    Operation.org_id == fabric.org_id(fabric.alpha),
                    Operation.source_document_id == document["id"],
                    Operation.source_record_id == record.id,
                )
            )
        ).scalar_one()
        event = (
            await session.execute(
                select(Event).where(
                    Event.org_id == fabric.org_id(fabric.alpha),
                    Event.source_record_id == record.id,
                    Event.source_kind == "ddr_promotion",
                )
            )
        ).scalar_one()
        event_id = event.id
        operation_id = operation.id
        record_id = record.id
        await session.commit()

    report = await _process(fabric, document["id"])
    assert report["events_created"] == []
    assert event_id not in report["events_updated"]
    assert operation_id not in report["operations_updated"]
    assert any(row.get("record_id") == record_id for row in report["not_promoted"])
    assert any(
        "source records that no longer pass the validation-state, confidence, or duration gates" in warning
        for warning in report["warnings"]
    )
    async with fabric.session() as session:
        retained = (await session.execute(select(Event).where(Event.id == event_id))).scalar_one()
        retained_operation = (await session.execute(select(Operation).where(Operation.id == operation_id))).scalar_one()
        assert retained.source_record_id == record_id
        assert retained.status == "open"
        assert retained.category == "lost_circulation"
        assert retained.npt_hours == pytest.approx(2.5)
        assert retained_operation.name == "Lost circulation"
        assert retained_operation.actual_duration_hours == pytest.approx(2.5)


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


async def test_incomplete_survey_rows_are_reviewed_and_never_promoted(fabric: Fabric) -> None:
    from drillai.db.models import EvidenceLink

    payloads = (
        (
            b"MD (m),Inclination (deg)\n500,12\n",
            "survey-missing-azimuth.csv",
        ),
        (
            b"MD (m),Inclination (deg),Azimuth (deg)\n600,NaN,Infinity\n",
            "survey-invalid-angles.csv",
        ),
    )
    for payload, filename in payloads:
        document = await _upload_report(
            fabric, payload=payload, filename=filename, content_type="text/csv", doc_type="ddr"
        )
        report = await _process(fabric, document["id"])

        async with fabric.session() as session:
            record = (
                await session.execute(
                    select(ExtractedRecord).where(
                        ExtractedRecord.document_id == document["id"],
                        ExtractedRecord.record_type == "survey_station",
                    )
                )
            ).scalar_one()
            stations = (
                await session.execute(
                    select(TrajectoryStation).where(TrajectoryStation.evidence_ref == record.id)
                )
            ).scalars().all()
            links = (
                await session.execute(
                    select(EvidenceLink).where(
                        EvidenceLink.subject_kind == "trajectory_station",
                        EvidenceLink.evidence_id == record.id,
                    )
                )
            ).scalars().all()
            trajectories = (
                await session.execute(
                    select(Trajectory).where(Trajectory.source_document_id == document["id"])
                )
            ).scalars().all()

        assert report["counts"]["trajectory_stations"] == 0
        assert record.id in report["records_needing_review"]
        assert any(row.get("record_id") == record.id for row in report["not_promoted"])
        assert stations == [] and links == [] and trajectories == []
        assert record.promoted_to_id is None


async def test_survey_retry_reuses_the_existing_station_and_evidence_link(fabric: Fabric) -> None:
    from drillai.db.models import EvidenceLink

    document = await _upload_report(
        fabric,
        payload=b"MD (m),Inclination (deg),Azimuth (deg)\n500,12,180\n",
        filename="survey-valid.csv",
        content_type="text/csv",
        doc_type="ddr",
    )
    first = await _process(fabric, document["id"])
    assert first["counts"]["trajectory_stations"] == 1

    second = await _process(fabric, document["id"])
    assert second["counts"]["trajectory_stations"] == 0
    assert any("reused 1 existing survey station" in warning for warning in second["warnings"])

    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.document_id == document["id"],
                    ExtractedRecord.record_type == "survey_station",
                )
            )
        ).scalars().all()
        stations = (
            await session.execute(
                select(TrajectoryStation).where(TrajectoryStation.evidence_ref == records[0].id)
            )
        ).scalars().all()
        links = (
            await session.execute(
                select(EvidenceLink).where(
                    EvidenceLink.subject_kind == "trajectory_station",
                    EvidenceLink.evidence_id == records[0].id,
                )
            )
        ).scalars().all()

    assert len(records) == len(stations) == len(links) == 1
    assert records[0].promoted_to_id == stations[0].id
    assert records[0].promoted_to_kind == "trajectory_station"
    assert stations[0].inclination_deg == pytest.approx(12.0)
    assert stations[0].azimuth_deg == pytest.approx(180.0)


async def test_survey_stations_from_two_documents_share_a_wellbore_trajectory_with_per_station_source(
    fabric: Fabric,
) -> None:
    """The trajectory has one anchor document; each appended station retains its own source."""
    from drillai.db.models import EvidenceLink

    def survey_report(report_number: str, report_date: str, md: int, inclination: int, azimuth: int) -> bytes:
        return (
            "Daily Drilling Report - SYNTHETIC TEST DATA - Well NF-12\n"
            f"Report No,{report_number}\n"
            f"Date,{report_date}\n"
            "Well,NF-12\n"
            "Rig,RIG-SYNTH-7\n"
            "Section,8-1/2 in\n"
            "MD (m),Inclination (deg),Azimuth (deg)\n"
            f"{md},{inclination},{azimuth}\n"
        ).encode()

    first_document = await _upload_report(
        fabric,
        payload=survey_report("RPT-SURVEY-101", "2026-03-15", 500, 12, 180),
        filename="survey-101.csv",
    )
    second_document = await _upload_report(
        fabric,
        payload=survey_report("RPT-SURVEY-102", "2026-03-16", 600, 13, 181),
        filename="survey-102.csv",
    )
    first = await _process(fabric, first_document["id"])
    assert first["counts"]["trajectory_stations"] == 1

    async with fabric.session() as session:
        first_trajectory = (
            await session.execute(
                select(Trajectory).where(
                    Trajectory.org_id == fabric.org_id(fabric.alpha),
                    Trajectory.wellbore_id == fabric.alpha.wellbore_id,
                    Trajectory.kind == "actual",
                    Trajectory.is_current.is_(True),
                )
            )
        ).scalar_one()
        trajectory_id = first_trajectory.id
        assert first_trajectory.source_document_id == first_document["id"]

    second = await _process(fabric, second_document["id"])
    assert second["counts"]["trajectory_stations"] == 1

    async with fabric.session() as session:
        trajectory = (await session.execute(select(Trajectory).where(Trajectory.id == trajectory_id))).scalar_one()
        stations = (
            await session.execute(
                select(TrajectoryStation)
                .where(TrajectoryStation.trajectory_id == trajectory.id)
                .order_by(TrajectoryStation.md_si)
            )
        ).scalars().all()
        records = (
            await session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.id.in_([station.evidence_ref for station in stations])
                )
            )
        ).scalars().all()
        links = (
            await session.execute(
                select(EvidenceLink).where(
                    EvidenceLink.subject_kind == "trajectory_station",
                    EvidenceLink.subject_id.in_([station.id for station in stations]),
                )
            )
        ).scalars().all()

    record_document_by_id = {record.id: record.document_id for record in records}
    station_document_by_id = {
        station.id: record_document_by_id[station.evidence_ref]
        for station in stations
        if station.evidence_ref in record_document_by_id
    }
    link_document_by_id = {link.subject_id: link.document_id for link in links}
    assert trajectory.source_document_id == first_document["id"]
    assert [station.md_si for station in stations] == [500.0, 600.0]
    assert set(station_document_by_id.values()) == {first_document["id"], second_document["id"]}
    assert link_document_by_id == station_document_by_id


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


async def test_rejected_and_superseded_records_never_enter_the_promotion_path(fabric: Fabric) -> None:
    document = await _upload_report(fabric)
    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document["id"])
            )
        ).scalars().all()
        assert records
        blocked_ids = set()
        for index, record in enumerate(records):
            record.validation_state = "rejected" if index % 2 == 0 else "superseded"
            blocked_ids.add(record.id)
        await session.commit()

    report = await _process(fabric, document["id"])
    assert report["operations_created"] == []
    assert report["events_created"] == []
    assert report["records_promoted"] == []
    assert report["twin_aspects"] == []
    assert report["counts"]["trajectory_stations"] == 0
    assert {row["record_id"] for row in report["not_promoted"]} == blocked_ids
    assert all("not eligible for promotion" in row["reason"] for row in report["not_promoted"])
    assert any("no extracted records passed validation-state and confidence gates" in warning for warning in report["warnings"])
    assert await _counts(fabric) == (0, 0)


async def test_every_promotion_path_observes_the_confidence_threshold(fabric: Fabric) -> None:
    document = await _upload_report(fabric)
    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document["id"])
            )
        ).scalars().all()
        assert records
        for record in records:
            record.confidence = 0.59
        expected_ids = {record.id for record in records}
        await session.commit()

    report = await _process(fabric, document["id"])
    assert report["operations_created"] == []
    assert report["events_created"] == []
    assert report["records_promoted"] == []
    assert report["twin_aspects"] == []
    assert set(report["records_needing_review"]) == expected_ids
    assert {row["record_id"] for row in report["not_promoted"]} == expected_ids
    assert all("below promotion threshold" in row["reason"] for row in report["not_promoted"])
    assert await _counts(fabric) == (0, 0)


async def test_non_finite_extraction_confidence_never_enters_promotion(fabric: Fabric) -> None:
    document = await _upload_report(fabric)
    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document["id"])
            )
        ).scalars().all()
        assert records
        for index, record in enumerate(records):
            record.confidence = float("nan") if index % 2 == 0 else float("inf")
        expected_ids = {record.id for record in records}
        await session.commit()

    report = await _process(fabric, document["id"])
    assert report["operations_created"] == []
    assert report["events_created"] == []
    assert report["records_promoted"] == []
    assert report["twin_aspects"] == []
    assert set(report["records_needing_review"]) == expected_ids
    assert {row["record_id"] for row in report["not_promoted"]} == expected_ids
    assert all(
        "confidence" in row["reason"]
        and (
            "confidence is missing or non-finite" in row["reason"]
            or "expected a finite value from 0 to 1" in row["reason"]
        )
        for row in report["not_promoted"]
    )
    assert await _counts(fabric) == (0, 0)


@pytest.mark.parametrize("validation_state", ["extracted", "human_validated"])
async def test_out_of_range_confidence_never_enters_promotion(
    fabric: Fabric, validation_state: str
) -> None:
    document = await _upload_report(fabric)
    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document["id"])
            )
        ).scalars().all()
        assert records
        for record in records:
            record.validation_state = validation_state
            record.confidence = 1.01
        expected_ids = {record.id for record in records}
        await session.commit()

    report = await _process(fabric, document["id"])
    assert report["operations_created"] == []
    assert report["events_created"] == []
    assert report["records_promoted"] == []
    assert report["twin_aspects"] == []
    assert set(report["records_needing_review"]) == expected_ids
    assert {row["record_id"] for row in report["not_promoted"]} == expected_ids
    assert all("expected a finite value from 0 to 1" in row["reason"] for row in report["not_promoted"])
    assert await _counts(fabric) == (0, 0)


async def test_human_validation_overrides_the_floor_without_being_downgraded(fabric: Fabric) -> None:
    document = await _upload_report(fabric)
    async with fabric.session() as session:
        records = (
            await session.execute(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document["id"])
            )
        ).scalars().all()
        assert records
        for index, record in enumerate(records):
            record.validation_state = "human_validated"
            record.confidence = None if index == 0 else 0.2
        await session.commit()

    report = await _process(fabric, document["id"])
    assert len(report["operations_created"]) == 5
    assert len(report["events_created"]) == 2
    assert report["records_needing_review"] == []
    async with fabric.session() as session:
        records_after = (
            await session.execute(
                select(ExtractedRecord).where(ExtractedRecord.document_id == document["id"])
            )
        ).scalars().all()
    assert records_after
    assert all(record.validation_state == "human_validated" for record in records_after)


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
