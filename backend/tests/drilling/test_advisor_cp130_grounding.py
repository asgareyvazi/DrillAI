"""CP130 Advisor grounding and explicit-abstention regressions on isolated synthetic records."""

from __future__ import annotations

import datetime as dt

from drillai.db.models import (
    Document,
    ExtractedRecord,
    Operation,
    Organization,
    Project,
    Well,
    Wellbore,
)
from drillai.drilling.advisor import OperationsAdvisor


async def test_advisor_abstains_on_rejected_parameter_values_and_exposes_missing_measures(session):
    org = Organization(slug="cp130-advisor-untrusted", name="CP130 synthetic Advisor grounding")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="CP130 synthetic project")
    session.add(project)
    await session.flush()
    well = Well(org_id=org.id, project_id=project.id, name="CP130-SYNTHETIC-ADVISOR-01")
    session.add(well)
    await session.flush()
    wellbore = Wellbore(
        org_id=org.id,
        well_id=well.id,
        name="Synthetic main bore",
        sequence=1,
        is_active=True,
    )
    session.add(wellbore)
    await session.flush()
    document = Document(
        org_id=org.id,
        project_id=project.id,
        well_id=well.id,
        wellbore_id=wellbore.id,
        doc_type="ddr",
        title="SYNTHETIC TEST DATA — rejected parameter source",
        status="extracted",
        classification="synthetic",
    )
    session.add(document)
    await session.flush()
    rejected = ExtractedRecord(
        org_id=org.id,
        document_id=document.id,
        project_id=project.id,
        well_id=well.id,
        wellbore_id=wellbore.id,
        record_type="parameter_set",
        payload_schema_key="daily_drilling.parameter_set",
        payload={"wob": 999.0, "rop": 88.0},
        observed_at=None,
        observed_at_basis="unknown",
        depth_md_basis="unknown",
        depth_tvd_basis="unknown",
        method="synthetic_fixture",
        confidence=0.99,
        validation_state="rejected",
        unit_context={"wob": "klbf", "rop": "m/h"},
        quality_flags=["unit_inferred"],
        is_demo_fixture=True,
    )
    session.add(rejected)
    await session.flush()

    answer = await OperationsAdvisor(session, org.id).ask(well.id, "performance")
    payload = answer.to_dict()
    fact_keys = {fact["key"] for fact in payload["facts"]}
    unknown_by_key = {item["key"]: item for item in payload["unknown"]}

    assert "measured.wob" not in fact_keys
    assert "measured.rop" not in fact_keys
    assert "kpi.wob" in unknown_by_key
    assert "kpi.rop" in unknown_by_key
    assert "999.0" not in str(payload["facts"])
    assert "88.0" not in str(payload["facts"])
    assert rejected.id not in payload["sources"]
    assert payload["narrative"] is None
    assert payload["unknown"]


async def test_advisor_reports_tied_current_operations_as_unknown_without_selecting_one(session):
    org = Organization(slug="cp130-advisor-ambiguous", name="CP130 synthetic Advisor abstention")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="CP130 synthetic project")
    session.add(project)
    await session.flush()
    well = Well(org_id=org.id, project_id=project.id, name="CP130-SYNTHETIC-ADVISOR-02")
    session.add(well)
    await session.flush()
    wellbore = Wellbore(
        org_id=org.id,
        well_id=well.id,
        name="Synthetic main bore",
        sequence=1,
        is_active=True,
    )
    session.add(wellbore)
    await session.flush()
    start = dt.datetime(2026, 4, 1, 8, 0, tzinfo=dt.UTC)
    candidates = [
        Operation(
            org_id=org.id,
            project_id=project.id,
            well_id=well.id,
            wellbore_id=wellbore.id,
            operation_class="actual",
            sequence=sequence,
            name=f"Synthetic competing operation {sequence}",
            kind="drilling",
            phase="drilling",
            status="in_progress",
            actual_start=start,
            actual_start_basis="source_record",
            actual_end=None,
            actual_end_basis="unknown",
            actual_duration_hours=None,
            actual_duration_basis="unknown",
            source="synthetic_fixture",
            source_kind="manual",
            is_demo_fixture=True,
        )
        for sequence in (1, 2)
    ]
    session.add_all(candidates)
    await session.flush()

    answer = await OperationsAdvisor(session, org.id).ask(well.id, "current_operation")
    payload = answer.to_dict()
    facts = {item["key"]: item for item in payload["facts"]}
    ambiguous = next(item for item in payload["unknown"] if item["key"] == "operation.current.ambiguous")

    assert "operation.current" not in facts
    assert "operation.latest" not in facts
    assert ambiguous["kind"] == "conflict"
    assert set(ambiguous["candidate_ids"]) == {operation.id for operation in candidates}
    assert "does not establish which one is current" in ambiguous["statement"]
    assert payload["narrative"] is None
