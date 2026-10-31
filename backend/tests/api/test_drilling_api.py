"""The drilling intelligence surface, exercised over HTTP.

These tests cover the endpoints the product's workspaces call: the drilling state of a well, the
merged timeline, the NPT summary, DDR promotion, the operations advisor, the reporting contract, the
change-impact analysis and parameter optimisation. They assert on *real* computed values and on the
platform's refusal paths (uncomputable objectives, unknown ports), because those refusals are part of
the product contract rather than error handling.
"""

from __future__ import annotations

import datetime as dt
import io

import pytest
from sqlalchemy import select
from tests.api.conftest import headers

from drillai.db.models import Document, ExtractedRecord, Operation, WellSection

pytestmark = pytest.mark.asyncio

SYNTHETIC_DDR = (
    "Daily Drilling Report - SYNTHETIC TEST DATA - Well NF-12\n"
    "Report No,RPT-NF12-014\n"
    "Date,2026-03-15\n"
    "Well,NF-12\n"
    "Rig,RIG-SYNTH-7\n"
    "Section,8-1/2 in\n"
    "Operations,Activity,Code,Duration (h),Depth (m),Description\n"
    "1,Drilling ahead 8-1/2 in section,DRL,8.5,2410,Drilling ahead from 2350 m to 2410 m at 120 rpm\n"
    "2,Made connection,CONN,0.5,2410,Connection at 2410 m\n"
    "3,Stuck pipe while POOH,STUCK,6.0,2408,Stuck pipe at 2408 m; worked free after 6 hours\n"
    "4,Lost circulation,LC,2.5,2412,Lost circulation 15 bbl/hr; LCM pill pumped\n"
    "Drilling Parameters,Value\n"
    "WOB (klbf),18.5\n"
    "RPM (rpm),120\n"
    "Flow Rate (l/min),2100\n"
    "Mud Weight (ppg),12.4\n"
    "Depth (m),2412\n"
)

HYDRAULICS_ELEMENTS = [
    {
        "kind": "drillpipe",
        "name": "dp",
        "from_depth_si": 0.0,
        "to_depth_si": 2400.0,
        "od_si": 0.127,
        "id_si": 0.1086,
    },
    {"kind": "hole", "name": "hole", "from_depth_si": 0.0, "to_depth_si": 2400.0, "od_si": 0.2159},
]

HYDRAULICS_INPUTS = {
    "elements": HYDRAULICS_ELEMENTS,
    "bit_depth_si": 2400.0,
    "flow_rate_si": 0.035,
    "mud_weight_si": 1240.0,
    "plastic_viscosity_si": 0.018,
    "yield_point_si": 8.0,
    "bit_diameter_si": 0.2159,
    "tfa_si": 0.00045,
    "nozzle_count": 3,
    "pore_pressure_gradient_si": 12500.0,
    "fracture_gradient_si": 15500.0,
}


async def _well_with_report(
    client,
    *,
    process: bool = True,
    period_start: str | None = None,
    period_end: str | None = None,
) -> dict[str, str]:
    """Create a project/well/wellbore/section and promote one synthetic DDR into it."""
    project = (
        await client.post("/api/v1/projects", json={"name": "Drilling API"}, headers=headers())
    ).json()
    well = (
        await client.post(
            "/api/v1/wells",
            json={"project_id": project["id"], "name": "NF-12", "well_type": "development_producer"},
            headers=headers(),
        )
    ).json()
    wellbore = (
        await client.post(
            f"/api/v1/wells/{well['id']}/wellbores",
            json={"name": "Main bore", "planned_td_md_si": 3200.0},
            headers=headers(),
        )
    ).json()
    section = (
        await client.post(
            f"/api/v1/wellbores/{wellbore['id']}/sections",
            json={
                "sequence": 3,
                "name": '8-1/2" section',
                "hole_diameter_si": 0.2159,
                "planned_top_md_si": 2400.0,
                "planned_bottom_md_si": 3200.0,
                # A section that was drilled records how far it was drilled. `is_planned_only` used to
                # be sent here instead, which made the record claim a hole depth nobody had written
                # down; it is derived from these two values now.
                "actual_top_md_si": 2400.0,
                "actual_bottom_md_si": 3200.0,
            },
            headers=headers(),
        )
    ).json()
    upload_data = {
        "well_id": well["id"],
        "wellbore_id": wellbore["id"],
        "section_id": section["id"],
        "doc_type": "ddr",
    }
    if period_start is not None:
        upload_data["period_start"] = period_start
    if period_end is not None:
        upload_data["period_end"] = period_end
    upload = await client.post(
        "/api/v1/documents",
        files={"file": ("ddr.csv", io.BytesIO(SYNTHETIC_DDR.encode()), "text/csv")},
        data=upload_data,
        headers=headers(),
    )
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["document"]["id"]
    if not process:
        return {
            "well_id": well["id"],
            "wellbore_id": wellbore["id"],
            "section_id": section["id"],
            "document_id": document_id,
            "processing": None,
        }
    processed = await client.post(
        f"/api/v1/documents/{document_id}/process", json={}, headers=headers()
    )
    assert processed.status_code == 200, processed.text
    return {
        "well_id": well["id"],
        "wellbore_id": wellbore["id"],
        "section_id": section["id"],
        "document_id": document_id,
        "processing": processed.json()["processing"],
    }


async def test_ddr_promotion_produces_operations_events_and_state(client):
    ids = await _well_with_report(client)
    report = ids["processing"]
    assert report["doc_type"] == "ddr"
    assert report["dry_run"] is False
    assert len(report["operations_created"]) >= 4
    assert len(report["events_created"]) >= 2
    assert len(report["records_promoted"]) == 5  # four operations and one mud-properties revision
    assert report["npt_hours_classified"] == pytest.approx(8.5, abs=0.01)
    assert report["operations_hours"] == pytest.approx(17.5, abs=0.01)
    assert report["twin_aspects"], "eligible mud properties must update the well twin"
    # The generic parameter_set extractor reports 0.55 confidence, below the 0.6 promotion gate.
    # It remains available for review rather than being silently admitted as measured well state.
    assert len(report["records_needing_review"]) == 1
    not_promoted_by_type = {item["record_type"]: item for item in report["not_promoted"]}
    assert set(not_promoted_by_type) == {"parameter_set", "report_header"}
    parameter_set = not_promoted_by_type["parameter_set"]
    assert "below promotion threshold 0.6" in parameter_set["reason"]
    assert parameter_set["record_id"] == report["records_needing_review"][0]
    assert "header metadata is merged into the document" in not_promoted_by_type["report_header"]["reason"]
    assert isinstance(report["warnings"], list)
    assert report["counts"]["events"] == len(report["events_created"])
    assert report["counts"]["events_updated"] == len(report["events_updated"]) == 0

    state = (await client.get(f"/api/v1/wells/{ids['well_id']}/state", headers=headers())).json()[
        "state"
    ]
    assert state["well"]["id"] == ids["well_id"]
    assert state["counts"]["operations"] >= 4
    assert state["counts"]["npt_events"] >= 2
    assert all(item["source"] != "extracted_record:parameter_set" for item in state["measured"]), (
        "the low-confidence parameter set remains reviewable on the document but is not a trusted telemetry fallback"
    )
    # The report's generic `Depth (m)` is not a defensible MD field and its parameter set is below
    # the promotion threshold; the stored section endpoint remains the reported source of progress.
    assert state["progress"]["current_md_si"] == pytest.approx(3200.0)
    assert state["progress"]["current_md_source"] == "well_sections.actual_bottom_md_si"
    assert state["npt"]["total_hours"] == pytest.approx(8.5, abs=0.01)
    assert state["twin"]["aspect_count"] >= 1
    assert state["documents"]["documents"] == 1
    # Missing capability is reported, never silently rendered as zero.
    assert state["missing"], "the state bundle must declare what it does not have"
    assert all(item["how_to_supply"] for item in state["missing"])


async def test_ddr_date_replays_ordered_durations_into_a_utc_operation_timeline(client, db):
    ids = await _well_with_report(client)
    listing = await client.get(
        "/api/v1/operations",
        params={"well_id": ids["well_id"]},
        headers=headers(),
    )
    assert listing.status_code == 200, listing.text
    operations = listing.json()["items"]
    assert [item["actual_start"] for item in operations] == [
        "2026-03-15T00:00:00+00:00",
        "2026-03-15T08:30:00+00:00",
        "2026-03-15T09:00:00+00:00",
        "2026-03-15T15:00:00+00:00",
    ]
    assert [item["actual_end"] for item in operations] == [
        "2026-03-15T08:30:00+00:00",
        "2026-03-15T09:00:00+00:00",
        "2026-03-15T15:00:00+00:00",
        "2026-03-15T17:30:00+00:00",
    ]

    async with db.session() as session:
        promoted = (
            await session.execute(
                select(Operation).where(
                    Operation.source_document_id == ids["document_id"]
                ).order_by(Operation.sequence)
            )
        ).scalars().all()
    assert [row.attributes["time_basis"] for row in promoted] == [
        "report_date",
        "replayed_from_report_duration",
        "replayed_from_report_duration",
        "replayed_from_report_duration",
    ]


async def test_ddr_period_start_precedes_issue_date_and_is_normalized_to_utc(client, db):
    explicit_period_start = dt.datetime(
        2026, 3, 15, 6, tzinfo=dt.timezone(dt.timedelta(hours=2))
    )
    ids = await _well_with_report(
        client,
        process=False,
        period_start=explicit_period_start.isoformat(),
        period_end=(explicit_period_start + dt.timedelta(days=1)).isoformat(),
    )
    async with db.session() as session:
        document = (
            await session.execute(select(Document).where(Document.id == ids["document_id"]))
        ).scalar_one()
        assert document.period_start == dt.datetime(2026, 3, 15, 4, tzinfo=dt.UTC)
        assert document.issue_date == dt.datetime(2026, 3, 15, tzinfo=dt.UTC)

    processed = await client.post(
        f"/api/v1/documents/{ids['document_id']}/process", json={}, headers=headers()
    )
    assert processed.status_code == 200, processed.text
    listing = await client.get(
        "/api/v1/operations",
        params={"well_id": ids["well_id"]},
        headers=headers(),
    )
    operations = listing.json()["items"]
    assert [item["actual_start"] for item in operations] == [
        "2026-03-15T04:00:00+00:00",
        "2026-03-15T12:30:00+00:00",
        "2026-03-15T13:00:00+00:00",
        "2026-03-15T19:00:00+00:00",
    ]
    async with db.session() as session:
        promoted = (
            await session.execute(
                select(Operation).where(
                    Operation.source_document_id == ids["document_id"]
                ).order_by(Operation.sequence)
            )
        ).scalars().all()
    assert promoted[0].actual_start == dt.datetime(2026, 3, 15, 4, tzinfo=dt.UTC)
    assert promoted[0].attributes["time_basis"] == "report_date"


async def test_generic_depth_parameter_is_not_reinterpreted_as_measured_depth(client, db):
    """A confident generic Depth value must not overwrite the recorded as-drilled MD field."""
    ids = await _well_with_report(client, process=False)
    async with db.session() as session:
        record = (
            await session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.document_id == ids["document_id"],
                    ExtractedRecord.record_type == "parameter_set",
                )
            )
        ).scalar_one()
        assert (record.payload or {}).get("depth") == pytest.approx(2412.0)
        # Raising confidence makes this an explicit test of field semantics rather than the separate
        # promotion-confidence gate. A generic label still cannot claim this is measured depth.
        record.confidence = 0.95
        await session.commit()

    processing = await client.post(
        f"/api/v1/documents/{ids['document_id']}/process", json={}, headers=headers()
    )
    assert processing.status_code == 200, processing.text
    state = (
        await client.get(f"/api/v1/wells/{ids['well_id']}/state", headers=headers())
    ).json()["state"]
    assert state["progress"]["current_md_si"] == pytest.approx(3200.0)
    assert state["progress"]["current_md_source"] == "well_sections.actual_bottom_md_si"
    async with db.session() as session:
        section = await session.get(WellSection, ids["section_id"])
        assert section is not None
        assert section.current_md_si is None
        assert section.actual_bottom_md_si == pytest.approx(3200.0)


async def test_explicit_measured_depth_parameter_is_converted_to_si(client, db):
    """An explicit MD field in feet is converted before it can update measured progress."""
    ids = await _well_with_report(client, process=False)
    async with db.session() as session:
        record = (
            await session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.document_id == ids["document_id"],
                    ExtractedRecord.record_type == "parameter_set",
                )
            )
        ).scalar_one()
        record.payload = {**record.payload, "measured_depth": 12000.0}
        record.unit_context = {**(record.unit_context or {}), "measured_depth": "ft"}
        record.confidence = 0.95
        await session.commit()

    processed = await client.post(
        f"/api/v1/documents/{ids['document_id']}/process", json={}, headers=headers()
    )
    assert processed.status_code == 200, processed.text
    state = (
        await client.get(f"/api/v1/wells/{ids['well_id']}/state", headers=headers())
    ).json()["state"]
    assert state["progress"]["current_md_si"] == pytest.approx(3657.6)
    assert state["progress"]["current_md_source"] == "well_sections.current_md_si"
    async with db.session() as session:
        section = await session.get(WellSection, ids["section_id"])
        record = (
            await session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.document_id == ids["document_id"],
                    ExtractedRecord.record_type == "parameter_set",
                )
            )
        ).scalar_one()
        assert section is not None
        assert section.current_md_si == pytest.approx(3657.6)
        assert record.unit_context["measured_depth"] == "ft", "source units remain auditable"


async def test_reprocessing_is_idempotent(client):
    """Re-running promotion must not duplicate operations, events or twin revisions."""
    ids = await _well_with_report(client)
    before = (await client.get(f"/api/v1/wells/{ids['well_id']}/state", headers=headers())).json()[
        "state"
    ]
    second = (
        await client.post(
            f"/api/v1/documents/{ids['document_id']}/process", json={}, headers=headers()
        )
    ).json()["processing"]
    assert second["operations_created"] == []
    assert second["events_created"] == []
    assert second["events_updated"] == []
    after = (await client.get(f"/api/v1/wells/{ids['well_id']}/state", headers=headers())).json()[
        "state"
    ]
    assert after["counts"] == before["counts"]
    assert after["twin"]["aspect_count"] == before["twin"]["aspect_count"]
    # The parameters are re-attached as evidence to the *same* twin revision: the twin service
    # recognises an identical payload by content hash instead of appending a duplicate revision.
    first_mud = next(
        item for item in ids["processing"]["twin_aspects"] if item["aspect"] == "mud_program"
    )
    second_mud = next(item for item in second["twin_aspects"] if item["aspect"] == "mud_program")
    assert second_mud["id"] == first_mud["id"]
    assert second["records_promoted"], "re-attaching provenance to the same revision is reported"


async def test_dry_run_predicts_without_writing(client):
    """A dry run must be a prediction: same analysis, no writes, and the real run must agree."""
    ids = await _well_with_report(client, process=False)
    before = (
        await client.get(f"/api/v1/wells/{ids['well_id']}/state", headers=headers())
    ).json()["state"]
    dry = (
        await client.post(
            f"/api/v1/documents/{ids['document_id']}/process",
            json={"dry_run": True},
            headers=headers(),
        )
    ).json()["processing"]
    after = (
        await client.get(f"/api/v1/wells/{ids['well_id']}/state", headers=headers())
    ).json()["state"]
    assert dry["dry_run"] is True
    # A dry run reports what *would* happen: the pending rows carry identifiers but nothing is
    # written, so no record is marked promoted and the well state is byte-for-byte unchanged.
    assert len(dry["operations_created"]) >= 4
    assert len(dry["events_created"]) >= 2
    assert dry["npt_hours_classified"] == pytest.approx(8.5, abs=0.01)
    assert dry["records_promoted"] == []
    assert any(item.get("dry_run") for item in dry["twin_aspects"])
    assert after["counts"] == before["counts"]
    assert after["twin"]["aspect_count"] == before["twin"]["aspect_count"]
    assert after["progress"]["current_md_si"] == before["progress"]["current_md_si"]

    # The prediction must match what the real run then does — otherwise the preview is theatre.
    real = (
        await client.post(f"/api/v1/documents/{ids['document_id']}/process", json={}, headers=headers())
    ).json()["processing"]
    assert len(real["operations_created"]) == len(dry["operations_created"])
    assert len(real["events_created"]) == len(dry["events_created"])
    assert real["npt_hours_classified"] == dry["npt_hours_classified"]
    assert len(set(real["records_promoted"])) == len(real["records_promoted"])
    assert len(real["records_promoted"]) >= len(dry["operations_created"])
    # Every record the real run claims to have promoted must actually carry its promotion target.
    detail = (
        await client.get(f"/api/v1/documents/{ids['document_id']}", headers=headers())
    ).json()
    promoted_rows = {row["id"]: row for row in detail["records"] if row.get("promoted_to_id")}
    assert set(real["records_promoted"]) <= set(promoted_rows)
    assert {row["promoted_to_kind"] for row in promoted_rows.values()} >= {"operation"}


async def test_timeline_merges_sources_and_rejects_unknown_kinds(client):
    ids = await _well_with_report(client)
    timeline = (
        await client.get(f"/api/v1/wells/{ids['well_id']}/timeline", headers=headers())
    ).json()
    assert timeline["count"] == len(timeline["entries"])
    assert timeline["count"] >= 4
    kinds = {entry["kind"] for entry in timeline["entries"]}
    assert {"operation", "event", "document"} <= kinds
    assert set(kinds) <= set(timeline["kinds_available"])
    operation = next(entry for entry in timeline["entries"] if entry["kind"] == "operation")
    assert operation["duration_hours"] is not None
    # The source table says only `Depth`, not MD; the parser must not invent a depth reference.
    assert operation["depth_md_si"] is None
    npt_entry = next(entry for entry in timeline["entries"] if entry["npt"] is True)
    assert npt_entry["npt_hours"] > 0

    bad = await client.get(
        f"/api/v1/wells/{ids['well_id']}/timeline",
        params={"kinds": "not_a_kind"},
        headers=headers(),
    )
    assert bad.status_code == 422
    error = bad.json()["error"]
    assert error["code"] == "platform.validation_failed"
    assert error["details"]["unknown"] == ["not_a_kind"]
    assert set(error["details"]["known"]) == set(timeline["kinds_available"])


async def test_npt_pareto_is_ordered_and_self_consistent(client):
    ids = await _well_with_report(client)
    npt = (await client.get(f"/api/v1/wells/{ids['well_id']}/npt", headers=headers())).json()["npt"]
    assert npt["total_hours"] == pytest.approx(8.5, abs=0.01)
    assert npt["event_count"] == 2
    assert npt["basis"] == "events"
    by_category = npt["by_category"]
    assert by_category[0]["key"] == "stuck_pipe"
    assert by_category[0]["hours"] == pytest.approx(6.0, abs=0.01)
    assert by_category[1]["key"] == "lost_circulation"
    assert by_category[1]["hours"] == pytest.approx(2.5, abs=0.01)
    # A Pareto chart that does not cumulate is a lie; check the running total.
    hours = [row["hours"] for row in by_category]
    assert hours == sorted(hours, reverse=True)
    assert by_category[-1]["cumulative_percent"] == pytest.approx(100.0, abs=0.01)
    assert by_category[0]["cumulative_percent"] == pytest.approx(by_category[0]["percent_of_total"])
    assert sum(row["hours"] for row in by_category) == pytest.approx(npt["total_hours"], abs=0.01)
    assert npt["cases"]
    case = npt["cases"][0]
    assert case["category"] == "stuck_pipe"
    # The NPT *code* must be the catalogue code, not the category: the code is what carries the
    # subcategory and the controllability judgement.
    assert case["code"] == "STUCK_PIPE"
    assert case["subcategory"] == "pipe_stuck"
    assert case["operator_controllable"] is True
    # The generic `Depth (m)` source is not a measured-depth locator, so the event stays unplaced.
    assert case["depth_md_si"] is None
    assert case["classification_source"] == "recorded"
    assert case["document_id"] == ids["document_id"]
    assert {row["key"] for row in npt["by_code"]} == {"STUCK_PIPE", "LOST_CIRC"}
    assert by_category[0]["operator_controllable"] is True
    # The controllability split is exhaustive and never silently folds "unknown" into "zero".
    split = (
        npt["controllable_hours"]
        + npt["uncontrollable_hours"]
        + npt["unknown_controllability_hours"]
    )
    assert split == pytest.approx(npt["total_hours"], abs=0.01)
    assert npt["controllable_hours"] == pytest.approx(8.5, abs=0.01)
    assert npt["unknown_controllability_hours"] == pytest.approx(0.0, abs=0.01)
    assert npt["notes"], "the summary must state how NPT was counted"


async def test_advisor_separates_facts_calculations_and_unknowns(client):
    ids = await _well_with_report(client)
    catalogue = (await client.get("/api/v1/advisor/questions", headers=headers())).json()
    assert any(item["key"] == "where_are_we" for item in catalogue["questions"])

    answer = (
        await client.post(
            f"/api/v1/wells/{ids['well_id']}/advisor",
            json={"question": "where_are_we"},
            headers=headers(),
        )
    ).json()["answer"]
    assert answer["facts"], "the advisor must ground its answer in recorded facts"
    assert answer["question_text"] == next(
        item["description"] for item in catalogue["questions"] if item["key"] == "where_are_we"
    )
    assert isinstance(answer["sources"], list)
    assert answer["sections"]["facts"] == len(answer["facts"])
    assert answer["sections"]["unknown"] == len(answer["unknown"])
    assert answer["unknown"], "the advisor must say what it does not know"
    # Without a configured provider there is no narrative — the computed sections are unaffected.
    assert answer["narrative"] is None
    assert answer["limitations"]
    assert answer["context_used"]["well_id"] == ids["well_id"]

    bad = await client.post(
        f"/api/v1/wells/{ids['well_id']}/advisor",
        json={"question": "will_the_well_make_money"},
        headers=headers(),
    )
    assert bad.status_code == 422, "a client typo is a validation error, not a server fault"
    error = bad.json()["error"]
    assert error["code"] == "platform.validation_failed"
    assert error["details"]["unknown"] == ["will_the_well_make_money"]
    assert error["details"]["known"] == sorted(
        item["key"] for item in catalogue["questions"]
    )


async def test_reports_keep_evidence_apart_from_calculations(client):
    ids = await _well_with_report(client)
    kinds = (await client.get("/api/v1/reports/kinds", headers=headers())).json()
    assert {item["key"] for item in kinds["kinds"]} >= {
        "daily_drilling",
        "npt",
        "drilling_performance",
        "management_summary",
        "end_of_well",
    }
    assert all(item["description"] for item in kinds["kinds"])

    report = (
        await client.get(
            f"/api/v1/wells/{ids['well_id']}/reports/daily_drilling", headers=headers()
        )
    ).json()["report"]
    assert report["kind"] == "daily_drilling"
    assert report["sections"]
    assert set(report["section_kinds"]) <= {
        "facts",
        "calculations",
        "evidence",
        "recommendations",
        "assumptions",
    }
    for section in report["sections"]:
        assert section["kind"] in report["section_kinds"]
        # An empty section is still present with count 0, so a client can tell "nothing recorded"
        # apart from "the backend did not answer".
        assert section["count"] == len(section["items"])
        assert section["title"]
    assert report["metadata"]["well"]["id"] == ids["well_id"]
    assert report["is_renderable"] is True
    # The payload is the contract; document rendering is explicitly out of scope.
    assert report["rendering_note"]

    unknown = await client.get(
        f"/api/v1/wells/{ids['well_id']}/reports/not_a_report", headers=headers()
    )
    assert unknown.status_code == 422
    assert unknown.json()["error"]["details"]["known"] == [
        item["key"] for item in sorted(kinds["kinds"], key=lambda row: row["key"])
    ]


async def test_optimisation_returns_constrained_candidates_and_a_reasoned_recommendation(client):
    ids = await _well_with_report(client)
    objectives = (
        await client.get("/api/v1/engineering/optimisation/objectives", headers=headers())
    ).json()
    assert {item["key"] for item in objectives["computable"]} >= {
        "ecd_margin_si",
        "spp_utilisation",
    }
    engine_keys = {
        row["key"]
        for row in (await client.get("/api/v1/registry/engines", headers=headers())).json()["items"]
    }
    assert all(
        item["source"].split(":")[0] in engine_keys for item in objectives["computable"]
    ), "every objective must name the engine whose output it reads"
    assert "rop" in objectives["not_evaluated"] and "bit_wear" in objectives["not_evaluated"]

    response = await client.post(
        f"/api/v1/wells/{ids['well_id']}/engineering/optimise",
        json={
            "section_id": ids["section_id"],
            "parameters": [
                {"name": "flow_rate_si", "min_si": 0.025, "max_si": 0.045, "steps": 4, "unit": "m3/s"}
            ],
            "hydraulics_inputs": HYDRAULICS_INPUTS,
            "limits": {"spp_si": 25.0e6},
            "objectives": [
                {"key": "ecd_margin_si", "sense": "maximize"},
                {"key": "spp_utilisation", "sense": "minimize"},
            ],
            "samples": 4,
        },
        headers=headers(),
    )
    assert response.status_code == 200, response.text
    optimisation = response.json()["optimisation"]
    explanation = optimisation["explanation"]
    assert explanation["status"] == "succeeded"
    assert explanation["candidates_evaluated"] == len(explanation["candidates"]) >= 4
    assert explanation["feasible_count"] == explanation["candidates_evaluated"]
    assert explanation["pareto_count"] >= 1
    assert explanation["recommended"] is not None
    recommended = explanation["recommended"]
    assert recommended["rank"] == 1
    assert recommended["objectives"], "the recommendation must carry computed objectives"
    assert recommended["engine_run_ids"], "a candidate must cite the engine runs behind it"
    assert explanation["engine_run_ids"] == recommended["engine_run_ids"]
    assert explanation["provenance"]["persisted_candidates"] == [recommended["label"]]
    assert explanation["provenance"]["evaluated_in_memory"]
    assert explanation["provenance"]["determinism_check"]
    assert explanation["why_not"], "the other candidates must be explained, not discarded"
    assert all(row["reason"] for row in explanation["why_not"])
    assert explanation["sensitivity"]["ecd_margin_si"]["spread"] > 0
    assert explanation["engine_keys"], "the result must name the engines that produced it"
    assert explanation["objective_sources"]["ecd_margin_si"].startswith("hydraulics.laminar:")
    assert explanation["constraints"] == [{"name": "spp_si", "value": 25.0e6}]
    assert optimisation["engine"]["sweep"] == "optimization.sweep"
    assert optimisation["engine"]["evaluation"] == "hydraulics.laminar"
    assert optimisation["not_evaluated"], "uncomputable objectives must be declared, not omitted"

    recommendations = (
        await client.get(f"/api/v1/wells/{ids['well_id']}/recommendations", headers=headers())
    ).json()["items"]
    stored = next(row for row in recommendations if row["id"] == optimisation["recommendation_id"])
    assert stored["engine_run_ids"] == recommended["engine_run_ids"], (
        "a recommendation must cite exactly the engine runs its candidate rests on"
    )
    engine_runs = (
        await client.get(f"/api/v1/wells/{ids['well_id']}/engine-runs", headers=headers())
    ).json()["items"]
    cited = next(row for row in engine_runs if row["id"] == stored["engine_run_ids"][0])
    assert cited["engine_key"] == "hydraulics.laminar"
    assert cited["engine_version"]
    assert cited["status"] == "succeeded"
    assert cited["inputs_hash"] and cited["outputs_hash"]
    assert cited["triggered_by"] == "optimisation"
    assert stored["why_not"]
    assert stored["constraints_applied"] is not None
    assert stored["action_level"]

    replay = (
        await client.get(
            f"/api/v1/engineering/optimisation/{optimisation['optimization_run_id']}",
            headers=headers(),
        )
    ).json()["explanation"]
    assert replay["recommended"]["id"] == recommended["id"]
    assert replay["recommended"]["engine_run_ids"] == recommended["engine_run_ids"]


async def test_optimisation_refuses_objectives_no_engine_computes(client):
    ids = await _well_with_report(client)
    response = await client.post(
        f"/api/v1/wells/{ids['well_id']}/engineering/optimise",
        json={
            "parameters": [
                {"name": "flow_rate_si", "min_si": 0.03, "max_si": 0.04, "steps": 2, "unit": "m3/s"}
            ],
            "hydraulics_inputs": HYDRAULICS_INPUTS,
            "objectives": [{"key": "rate_of_penetration_si"}],
            "samples": 2,
        },
        headers=headers(),
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "platform.validation_failed"
    assert "objectives that no engine computes" in body["error"]["message"]
    assert body["error"]["retryable"] is False


async def test_dependency_graph_and_impact_report(client):
    ids = await _well_with_report(client)
    graph = (await client.get("/api/v1/engineering/dependencies", headers=headers())).json()["graph"]
    assert len(graph["nodes"]) >= 10
    assert graph["edges"]
    produced = set(graph["ports"]["produced"])
    consumed = set(graph["ports"]["consumed"])
    assert graph["ports"]["known"] >= len(produced | consumed)
    assert graph["ports"]["declared_but_unused"] == sorted(
        set(graph["ports"]["declared_but_unused"])
    )
    assert len(graph["ports"]["declared_but_unused"]) == graph["ports"]["known"] - len(
        produced | consumed
    )
    assert "mud.properties" in graph["ports"]["consumed"]
    assert graph["producers"]["hydraulics.profile"]
    assert "registry" in graph["source"]

    impact = (
        await client.get(
            f"/api/v1/wells/{ids['well_id']}/engineering/impact",
            params={"ports": ["mud.properties"]},
            headers=headers(),
        )
    ).json()["impact"]
    assert impact["changed_ports"] == ["mud.properties"]
    assert impact["unknown_ports"] == []
    affected = next(row for row in impact["impacts"] if row["port"] == "mud.properties")
    assert "hydraulics.laminar" in affected["affected_engines"]
    assert "hydraulics.profile" in affected["stale_produced_ports"]
    assert "hydraulics.laminar" in affected["never_run"]
    assert impact["stale_ports"], "downstream artifacts must be marked stale, not silently kept"
    assert impact["note"]

    unknown = (
        await client.get(
            f"/api/v1/wells/{ids['well_id']}/engineering/impact",
            params={"ports": ["not.a.port"]},
            headers=headers(),
        )
    ).json()["impact"]
    assert unknown["unknown_ports"] == ["not.a.port"]
    assert unknown["unknown_port_note"], "an unknown port must be explained, not ignored"


async def test_identity_reports_permissions_without_deciding_them(client):
    engineer = (await client.get("/api/v1/platform/identity", headers=headers("engineer"))).json()
    assert engineer["role_keys"] == ["engineer"]
    assert "workflow.draft" in engineer["permissions"]
    assert "workflow.publish" not in engineer["permissions"]
    assert engineer["max_action_level"] == "L2"
    assert engineer["auth_enabled"] is False
    assert engineer["identity_source"] == "development_header"
    assert len(engineer["available_roles"]) >= 8
    assert [role["key"] for role in engineer["roles"]] == ["engineer"]
    supervisor = next(
        role for role in engineer["available_roles"] if role["key"] == "drilling_supervisor"
    )
    assert "workflow.*" in supervisor["permissions"]
    assert supervisor["max_action_level"] == "L4"

    viewer = (await client.get("/api/v1/platform/identity", headers=headers("viewer"))).json()
    assert viewer["max_action_level"] == "L0"
    assert viewer["permissions"] == sorted(viewer["permissions"])
    # No credential material may appear in the payload.
    payload = str(viewer).lower()
    for forbidden in ("token", "password", "secret", "hash"):
        assert forbidden not in payload


async def test_identity_refuses_an_unknown_role(client):
    response = await client.get(
        "/api/v1/platform/identity", headers={"X-Dev-Roles": "not_a_catalogued_role"}
    )
    assert response.status_code in (401, 403)
