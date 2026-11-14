"""CP140 claim citations, explicit freshness, scope filtering, and fail-closed abstention tests."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import event

from drillai.core.ids import new_id
from drillai.db.models import (
    Document,
    EvidenceLink,
    Organization,
    Project,
    TimeSeries,
    TimeSeriesPoint,
    Well,
    Wellbore,
)
from drillai.drilling.advisor import AdvisorAnswer, EvidenceItem, Fact, OperationsAdvisor
from drillai.telemetry.identity import scope_token
from drillai.telemetry.units import CANONICAL_UNIT


async def _create_well(session, *, slug: str, name: str = "CP140-SYNTHETIC-01"):
    org = Organization(slug=slug, name=f"{slug} synthetic organization")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name=f"{slug} synthetic project")
    session.add(project)
    await session.flush()
    well = Well(org_id=org.id, project_id=project.id, name=name)
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
    return org, project, well, wellbore


async def test_every_positive_advisor_claim_has_a_scoped_source_and_unknowns_remain_explicit(session):
    org, project, well, wellbore = await _create_well(session, slug="cp140-claim-contract")

    answer = await OperationsAdvisor(session, org.id).ask(well.id, "where_are_we")
    payload = answer.to_dict()

    grounding = payload["grounding"]
    assert grounding["contract"] == "advisor-grounding-v1"
    assert grounding["scope"] == {
        "organization_id": org.id,
        "project_id": project.id,
        "well_id": well.id,
        "wellbore_id": wellbore.id,
    }
    positive_claims = (
        payload["facts"]
        + payload["calculations"]
        + payload["inference"]
        + payload["recommendation"]
    )
    assert positive_claims
    assert all(item["citations"] for item in positive_claims)
    assert grounding["claim_count"] == grounding["cited_claim_count"]
    assert grounding["uncited_claim_count"] == 0
    assert grounding["withheld_claim_count"] == 0

    for fact in payload["facts"]:
        assert fact["grounding"]["status"] == "cited"
        assert fact["citations"][0]["scope"]["organization_id"] == org.id
        assert fact["citations"][0]["scope"]["well_id"] == well.id
    assert any(item["key"] == "npt.none" for item in payload["unknown"])
    current_operation = next(item for item in payload["inference"] if item["key"] == "operation.current")
    assert current_operation["citations"][0]["kind"] == "scoped_query"
    assert "does not establish physical field absence" in current_operation["citations"][0]["note"]
    assert "generated_at is answer assembly time" in " ".join(payload["limitations"])


async def test_stale_measurement_is_cited_by_point_id_and_never_relabeled_as_current(session):
    org, _project, well, wellbore = await _create_well(session, slug="cp140-stale-measurement")
    now = dt.datetime.now(tz=dt.UTC)
    observed_at = now - dt.timedelta(hours=1)
    series = TimeSeries(
        id=new_id("tms"),
        org_id=org.id,
        well_id=well.id,
        wellbore_id=wellbore.id,
        scope_token=scope_token(well_id=well.id, wellbore_id=wellbore.id),
        channel_key="wob",
        name="Weight on bit",
        dimension="force",
        unit=CANONICAL_UNIT["force"],
        source="manual",
        is_realtime=True,
    )
    point = TimeSeriesPoint(
        id=new_id("tsp"),
        series_id=series.id,
        ts=observed_at,
        received_at=now,
        value=80_000.0,
        quality="good",
        source_point_id="cp140-synthetic-source-point-1",
        source_ref="synthetic source connector",
        dedup_key="cp140-synthetic-point-key-1",
        src_value=18.0,
        src_unit="klbf",
    )
    session.add_all([series, point])
    await session.flush()

    answer = await OperationsAdvisor(session, org.id).ask(well.id, "performance")
    fact = next(item.to_dict() for item in answer.facts if item.key == "measured.wob")
    citation = fact["citations"][0]
    freshness = fact["grounding"]["freshness"]

    assert citation["kind"] == "measurement_point"
    assert citation["source_id"] == point.id
    assert citation["evidence_ref"] == point.id
    assert citation["scope"]["well_id"] == well.id
    assert citation["scope"]["wellbore_id"] == wellbore.id
    assert freshness["status"] == "stale"
    assert freshness["as_of"] == observed_at.isoformat()
    assert freshness["age_seconds"] > 300
    assert fact["provenance"]["observed_at_basis"] == "source_timestamp"
    assert answer.grounding["freshness"]["stale_measurement_claim_count"] == 1
    assert answer.grounding["freshness"]["generated_at_is_not_source_time"] is True


async def test_advisor_evidence_citations_exclude_same_tenant_other_well_and_keep_quote_limits(session):
    org, project, well, wellbore = await _create_well(session, slug="cp140-evidence-scope", name="CP140-SYNTHETIC-OWNER")
    other_well = Well(org_id=org.id, project_id=project.id, name="CP140-SYNTHETIC-OTHER")
    session.add(other_well)
    await session.flush()
    owner_document = Document(
        org_id=org.id,
        project_id=project.id,
        well_id=well.id,
        wellbore_id=wellbore.id,
        doc_type="ddr",
        title="SYNTHETIC owner DDR",
        status="extracted",
    )
    other_document = Document(
        org_id=org.id,
        project_id=project.id,
        well_id=other_well.id,
        doc_type="ddr",
        title="SYNTHETIC other-well DDR",
        status="extracted",
    )
    unscoped_document = Document(
        org_id=org.id,
        doc_type="ddr",
        title="SYNTHETIC unscoped organization document",
        status="extracted",
    )
    session.add_all([owner_document, other_document, unscoped_document])
    await session.flush()
    session.add_all(
        [
            EvidenceLink(
                org_id=org.id,
                subject_kind="operation",
                subject_id="opr_cp140_claim",
                evidence_kind="document_region",
                evidence_id="reg_cp140_owner",
                document_id=owner_document.id,
                region_id="reg_cp140_owner",
                page_number=1,
                well_id=well.id,
                locator={"page": 1, "region": "reg_cp140_owner", "precision": "region"},
                excerpt="SYNTHETIC owner source excerpt",
                confidence=0.91,
                method="synthetic_test",
                quote_verified=True,
                quote_check="region_text",
            ),
            EvidenceLink(
                org_id=org.id,
                subject_kind="operation",
                subject_id="opr_cp140_claim",
                evidence_kind="document_region",
                evidence_id="reg_cp140_other",
                document_id=other_document.id,
                region_id="reg_cp140_other",
                page_number=2,
                well_id=well.id,
                locator={"page": 2, "region": "reg_cp140_other", "precision": "region"},
                excerpt="SYNTHETIC other-well source excerpt must not leak",
                confidence=1.0,
                method="synthetic_test",
                quote_verified=True,
                quote_check="region_text",
            ),
            EvidenceLink(
                org_id=org.id,
                subject_kind="operation",
                subject_id="opr_cp140_claim",
                evidence_kind="document_region",
                evidence_id="reg_cp140_unscoped",
                document_id=unscoped_document.id,
                region_id="reg_cp140_unscoped",
                page_number=3,
                excerpt="SYNTHETIC unscoped organization evidence must not leak",
                confidence=0.95,
                method="synthetic_test",
                quote_verified=True,
                quote_check="region_text",
            ),
        ]
    )
    await session.flush()

    statements: list[str] = []

    def record_select(_connection, _cursor, statement, _parameters, _context, _executemany) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    engine = session.get_bind()
    advisor = OperationsAdvisor(session, org.id)
    event.listen(engine, "before_cursor_execute", record_select)
    try:
        citations = await advisor._evidence_for(
            ["opr_cp140_claim"], well_id=well.id, project_id=project.id
        )
    finally:
        event.remove(engine, "before_cursor_execute", record_select)

    assert len(citations) == 1
    citation = citations[0].to_dict()
    assert citation["document_id"] == owner_document.id
    assert citation["well_id"] == well.id
    assert citation["wellbore_id"] == wellbore.id
    assert citation["quote_verified"] is True
    assert citation["quote_check"] == "region_text"
    assert citation["locator"]["precision"] == "region"
    assert "other-well source excerpt" not in str(citation)
    assert "unscoped organization evidence" not in str(citation)
    assert len(statements) == 1


def test_citation_attachments_cannot_cross_the_claims_wellbore_or_section_scope(session):
    answer = AdvisorAnswer(
        question="current_operation",
        well_id="wel_requested",
        generated_at=dt.datetime(2026, 10, 20, tzinfo=dt.UTC),
        facts=[
            Fact(
                key="operation.current",
                statement="Recorded current operation",
                source="operations",
                source_id="opr_requested",
                provenance={
                    "project_id": "prj_requested",
                    "well_id": "wel_requested",
                    "wellbore_id": "wbr_requested",
                    "section_id": "sec_requested",
                },
            )
        ],
        evidence=[
            EvidenceItem(
                id="evd_requested_scope",
                kind="document_region",
                document_id="doc_requested_scope",
                document_title="SYNTHETIC requested-scope source",
                page_number=1,
                excerpt="SYNTHETIC relevant evidence",
                method="synthetic_test",
                confidence=0.9,
                subject_id="opr_requested",
                evidence_id="rgn_requested_scope",
                well_id="wel_requested",
                project_id="prj_requested",
                wellbore_id="wbr_requested",
                section_id="sec_requested",
                operation_id="opr_requested",
            ),
            EvidenceItem(
                id="evd_other_bore",
                kind="document_region",
                document_id="doc_other_bore",
                document_title="SYNTHETIC other-bore source",
                page_number=2,
                excerpt="Must not support requested-bore claim",
                method="synthetic_test",
                confidence=1.0,
                subject_id="opr_requested",
                evidence_id="rgn_other_bore",
                well_id="wel_requested",
                project_id="prj_requested",
                wellbore_id="wbr_other",
                section_id="sec_requested",
                operation_id="opr_requested",
            ),
            EvidenceItem(
                id="evd_other_section",
                kind="document_region",
                document_id="doc_other_section",
                document_title="SYNTHETIC other-section source",
                page_number=3,
                excerpt="Must not support requested-section claim",
                method="synthetic_test",
                confidence=1.0,
                subject_id="opr_requested",
                evidence_id="rgn_other_section",
                well_id="wel_requested",
                project_id="prj_requested",
                wellbore_id="wbr_requested",
                section_id="sec_other",
                operation_id="opr_requested",
            ),
        ],
    )

    OperationsAdvisor(session, "org_requested")._ground_answer(
        answer,
        scope={
            "project_id": "prj_requested",
            "wellbore_id": "wbr_requested",
        },
    )

    citations = answer.facts[0].citations
    assert [item["citation_id"] for item in citations] == [
        "source:operation.current:database_record:opr_requested",
        "evidence:evd_requested_scope",
    ]
    assert citations[1]["scope"]["wellbore_id"] == "wbr_requested"
    assert citations[1]["scope"]["section_id"] == "sec_requested"


def test_recorded_fact_scope_conflict_is_withheld_and_counted_as_an_abstention(session):
    answer = AdvisorAnswer(
        question="performance",
        well_id="wel_requested",
        generated_at=dt.datetime(2026, 10, 20, tzinfo=dt.UTC),
        facts=[
            Fact(
                key="fact.other_well",
                statement="Cross-scope fact",
                source="wells",
                source_id="wel_other",
                provenance={"project_id": "prj_requested", "well_id": "wel_other"},
            )
        ],
    )

    OperationsAdvisor(session, "org_requested")._ground_answer(
        answer,
        scope={"project_id": "prj_requested", "wellbore_id": None},
    )

    assert answer.facts == []
    assert answer.grounding["withheld_claim_count"] == 1
    assert answer.unknown[0]["kind"] == "scope_conflict"
    assert "scope conflicts" in answer.unknown[0]["statement"]


def test_source_free_claim_is_withheld_and_counted_as_an_abstention(session):
    answer = AdvisorAnswer(
        question="performance",
        well_id="wel_cp140_test",
        generated_at=dt.datetime(2026, 10, 20, tzinfo=dt.UTC),
        facts=[Fact(key="unsupported", statement="Synthetic unsupported claim")],
    )
    OperationsAdvisor(session, "org_cp140_test")._ground_answer(
        answer,
        scope={"project_id": "prj_cp140_test", "wellbore_id": None},
    )

    assert answer.facts == []
    assert answer.grounding["withheld_claim_count"] == 1
    assert answer.grounding["uncited_claim_count"] == 0
    assert answer.unknown[0]["kind"] == "missing_evidence"
    assert answer.unknown[0]["key"] == "grounding.withheld.unsupported"
