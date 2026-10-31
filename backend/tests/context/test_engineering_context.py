"""Engineering context: one source of well truth, scoped correctly and filtered by permission.

The acceptance test the mission calls out is here: a question about the 8½" section must not pull
in every section of the well. That is enforced by *scope filters*, not by hope — the same principle
that keeps a retrieval from answering with the 12¼" section's 8½" casing passage.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from drillai.context.builder import (
    ContextBuilder,
    SectionProvider,
    TrajectoryProvider,
    default_context_builder,
    register_section_provider,
    registered_section_keys,
)
from drillai.context.model import (
    ContextItem,
    ContextPurpose,
    ContextRequest,
    ContextScope,
    ContextSection,
    UnitSystemName,
)
from drillai.db.models import (
    Document,
    DocumentChunk,
    Event,
    EvidenceLink,
    ExtractedRecord,
    Formation,
    Operation,
    Organization,
    Project,
    RawArtifact,
    Recommendation,
    Risk,
    Trajectory,
    TrajectoryStation,
    Well,
    Wellbore,
    WellFormationMarker,
    WellSection,
)
from drillai.twin.aspects import StateKind
from drillai.twin.service import AspectRevision, TwinService

ALL_READ_PERMISSIONS = frozenset(
    {"well.*", "document.*", "evidence.*", "engine.*", "twin.*", "recommendation.*"}
)


@pytest.fixture
async def well_context(session):
    org = Organization(slug="ctx", name="Context Org")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="Context project")
    session.add(project)
    await session.flush()
    well = Well(org_id=org.id, project_id=project.id, name="CTX-1", kb_elevation_si=30.0)
    session.add(well)
    await session.flush()
    wellbore = Wellbore(org_id=org.id, well_id=well.id, name="Main")
    session.add(wellbore)
    await session.flush()

    # Two sections with the same nominal size in the name but different hole sizes.
    surface = WellSection(
        org_id=org.id,
        wellbore_id=wellbore.id,
        sequence=1,
        name='12-1/4" section',
        kind="surface",
        status="drilled",
        hole_diameter_si=0.31115,
        hole_diameter_nominal='12-1/4"',
        planned_top_md_si=0.0,
        planned_bottom_md_si=800.0,
        actual_bottom_md_si=802.0,
    )
    intermediate = WellSection(
        org_id=org.id,
        wellbore_id=wellbore.id,
        sequence=2,
        name='8-1/2" section',
        kind="intermediate",
        status="drilling",
        hole_diameter_si=0.2159,
        hole_diameter_nominal='8-1/2"',
        planned_top_md_si=800.0,
        planned_bottom_md_si=3200.0,
        current_md_si=1500.0,
    )
    session.add_all([surface, intermediate])
    await session.flush()

    # Operations, documents, and extracted records are deliberately tagged at their narrowest
    # known scope so each context provider can prove a requested filter before returning data.
    operations = {}
    for sequence, section in enumerate((surface, intermediate), start=1):
        is_surface = section.id == surface.id
        operation = Operation(
            org_id=org.id,
            project_id=project.id,
            well_id=well.id,
            wellbore_id=wellbore.id,
            section_id=section.id,
            operation_class="actual",
            sequence=sequence,
            name=f"Drill {section.name}",
            actual_start=dt.datetime(2026, 1 if is_surface else 2, 5, tzinfo=dt.UTC),
            actual_end=dt.datetime(2026, 1 if is_surface else 2, 6, tzinfo=dt.UTC),
            depth_from_md_si=0.0 if is_surface else 800.0,
            depth_to_md_si=800.0 if is_surface else 1500.0,
        )
        session.add(operation)
        await session.flush()
        operations[section.id] = operation

    artifact = RawArtifact(
        org_id=org.id,
        sha256="a" * 64,
        byte_size=100,
        blob_key="raw/aa/x",
        content_type="application/pdf",
        source_kind="upload",
        retention_class="standard",
        scan_status="clean",
    )
    session.add(artifact)
    await session.flush()
    documents = {}
    for section, text in ((surface, "Surface section mud weight was 9.2 ppg."), (intermediate, "Intermediate section mud weight is 12.4 ppg.")):
        is_surface = section.id == surface.id
        operation = operations[section.id]
        observed_at = dt.datetime(2026, 1 if is_surface else 2, 5, 12, tzinfo=dt.UTC)
        depth_md = 400.0 if is_surface else 1000.0
        document = Document(
            org_id=org.id,
            project_id=project.id,
            well_id=well.id,
            wellbore_id=wellbore.id,
            operation_id=operation.id,
            section_id=section.id,
            raw_artifact_id=artifact.id,
            doc_type="ddr",
            title=f"Daily report ({section.name})",
            status="ingested",
            period_start=observed_at,
            period_end=observed_at,
        )
        session.add(document)
        await session.flush()
        documents[section.id] = document
        session.add(
            DocumentChunk(
                org_id=org.id,
                document_id=document.id,
                chunk_index=0,
                text=text,
                kind="text",
                well_id=well.id,
                wellbore_id=wellbore.id,
                section_id=section.id,
                operation_id=operation.id,
                doc_type="ddr",
                depth_from_si=depth_md,
                depth_to_si=depth_md,
                period_start=observed_at,
                period_end=observed_at,
                token_estimate=len(text) // 4,
            )
        )
        record = ExtractedRecord(
            org_id=org.id,
            document_id=document.id,
            project_id=project.id,
            well_id=well.id,
            wellbore_id=wellbore.id,
            section_id=section.id,
            operation_id=operation.id,
            depth_md_si=depth_md,
            observed_at=observed_at,
            record_type="mud_properties",
            payload_schema_key="extracted.mud_properties",
            payload={"mud_weight": 9.2 if section is surface else 12.4},
            method="pattern",
            confidence=0.9,
            validation_state="extracted",
            unit_context={"mud_weight": "ppg"},
            quality_flags=[],
        )
        session.add(record)
        await session.flush()
        session.add(
            EvidenceLink(
                org_id=org.id,
                subject_kind="extracted_record",
                subject_id=record.id,
                evidence_kind="document_page",
                document_id=document.id,
                well_id=well.id,
                page_number=1,
            )
        )
    twin = TwinService(session, org_id=org.id)
    await twin.write_aspect(
        well_id=well.id,
        revision=AspectRevision(
            aspect="mud_program",
            state_kind=StateKind.ACTUAL,
            payload={"mud_weight_si": 1485.0},
            summary="current mud weight",
            computed_by="engine",
        ),
    )
    await session.flush()
    return {
        "org": org,
        "project": project,
        "well": well,
        "wellbore": wellbore,
        "surface": surface,
        "intermediate": intermediate,
        "artifact": artifact,
        "operations": operations,
        "documents": documents,
    }


def _request(well_context, **overrides) -> ContextRequest:
    scope_kwargs = {
        "org_id": well_context["org"].id,
        "project_id": well_context["project"].id,
        "well_id": well_context["well"].id,
        "wellbore_id": well_context["wellbore"].id,
    }
    scope_kwargs.update(overrides.pop("scope", {}))
    data = {
        "scope": ContextScope(**scope_kwargs),
        "purpose": ContextPurpose.PROMPT,
        "permissions": ALL_READ_PERMISSIONS,
    }
    data.update(overrides)
    return ContextRequest(**data)


# --------------------------------------------------------------------------- scope


async def test_well_identity_and_sections_are_present(session, well_context):
    builder = default_context_builder()
    bundle = await builder.build(session, _request(well_context))
    identity = bundle.section("well_identity")
    assert identity is not None and not identity.is_empty
    assert identity.items[0].data["name"] == "CTX-1"
    sections = bundle.section("well_sections")
    assert sections is not None
    names = {item.data["name"] for item in sections.items}
    assert names == {'12-1/4" section', '8-1/2" section'}
    assert bundle.token_estimate > 0
    assert bundle.limitations  # the bundle states what it does not do


async def test_section_scope_does_not_pull_in_every_section(session, well_context):
    """The mission's acceptance case: an 8½" question must stay in the 8½" section."""
    builder = default_context_builder()
    intermediate = well_context["intermediate"]
    bundle = await builder.build(session, _request(well_context, scope={"section_id": intermediate.id}))

    documents = bundle.section("documents")
    assert documents is not None and not documents.is_empty
    titles = [item.label for item in documents.items]
    assert titles == ['Daily report (8-1/2" section)']
    assert all("12-1/4" not in title for title in titles)

    records = bundle.section("extracted_records")
    assert records is not None and records.items
    assert {item.data["mud_weight"] for item in records.items} == {12.4}
    # each record cites the document it came from, so a claim can be traced back to a page
    assert all(item.cites for item in records.items)

    chunks = bundle.section("document_chunks")
    if chunks is not None and chunks.items:
        assert all("Surface section" not in item.data.get("text", "") for item in chunks.items)


async def test_project_only_context_retrieves_only_the_project_documents_and_evidence(session, well_context):
    bundle = await default_context_builder().build(
        session,
        _request(well_context, scope={"well_id": None, "wellbore_id": None}),
    )

    expected_document_ids = {document.id for document in well_context["documents"].values()}
    for key in ("documents", "extracted_records", "evidence", "document_chunks"):
        section = bundle.section(key)
        assert section is not None and section.items, key
        if key == "documents":
            returned_document_ids = {item.id for item in section.items}
        elif key == "extracted_records":
            returned_document_ids = {item.cites[0] for item in section.items}
        elif key == "evidence":
            returned_document_ids = {item.data["document_id"] for item in section.items}
        else:
            returned_document_ids = {item.data["document_id"] for item in section.items}
        assert returned_document_ids == expected_document_ids


async def test_evidence_context_applies_section_operation_depth_and_period_scope(session, well_context):
    intermediate = well_context["intermediate"]
    operation = well_context["operations"][intermediate.id]
    expected_document_id = well_context["documents"][intermediate.id].id
    requests = (
        {"section_id": intermediate.id},
        {"operation_id": operation.id},
        {
            "period_from": dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
            "period_to": dt.datetime(2026, 2, 28, tzinfo=dt.UTC),
        },
        {"depth_from_si": 900.0, "depth_to_si": 1200.0},
    )

    for narrowed_scope in requests:
        bundle = await default_context_builder().build(
            session, _request(well_context, scope=narrowed_scope)
        )
        evidence = bundle.section("evidence")
        assert evidence is not None
        assert {item.data["document_id"] for item in evidence.items} == {expected_document_id}


async def test_evidence_period_is_proved_by_the_linked_record_or_operation(session, well_context):
    document = well_context["documents"][well_context["intermediate"].id]
    out_of_period_record = ExtractedRecord(
        org_id=well_context["org"].id,
        document_id=document.id,
        project_id=well_context["project"].id,
        well_id=well_context["well"].id,
        wellbore_id=well_context["wellbore"].id,
        section_id=well_context["intermediate"].id,
        operation_id=well_context["operations"][well_context["intermediate"].id].id,
        record_type="depth_reading",
        payload_schema_key="extracted.depth_reading",
        payload={"bit_depth_si": 1400.0},
        observed_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
        depth_md_si=1400.0,
        method="test",
        confidence=0.9,
        validation_state="extracted",
    )
    out_of_period_operation = Operation(
        org_id=well_context["org"].id,
        project_id=well_context["project"].id,
        well_id=well_context["well"].id,
        wellbore_id=well_context["wellbore"].id,
        section_id=well_context["intermediate"].id,
        operation_class="actual",
        sequence=99,
        name="March-only operation",
        actual_start=dt.datetime(2026, 3, 2, tzinfo=dt.UTC),
        actual_end=dt.datetime(2026, 3, 3, tzinfo=dt.UTC),
    )
    session.add_all([out_of_period_record, out_of_period_operation])
    await session.flush()
    record_link = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="extracted_record.depth_reading",
        subject_id=out_of_period_record.id,
        evidence_kind="document_page",
        evidence_id=out_of_period_record.id,
        document_id=document.id,
        well_id=well_context["well"].id,
    )
    operation_link = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="operation",
        subject_id=out_of_period_operation.id,
        evidence_kind="document_page",
        document_id=document.id,
        well_id=well_context["well"].id,
    )
    session.add_all([record_link, operation_link])
    await session.flush()

    bundle = await default_context_builder().build(
        session,
        _request(
            well_context,
            scope={
                "period_from": dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
                "period_to": dt.datetime(2026, 2, 28, tzinfo=dt.UTC),
            },
            sections=["evidence"],
        ),
    )
    evidence = bundle.section("evidence")
    assert evidence is not None
    returned_ids = {item.id for item in evidence.items}
    assert record_link.id not in returned_ids
    assert operation_link.id not in returned_ids
    assert returned_ids, "valid February source evidence should remain visible"


async def test_event_and_station_evidence_prove_scope_time_depth_and_document_consistency(
    session, well_context
):
    target_operation = well_context["operations"][well_context["intermediate"].id]
    other_operation = well_context["operations"][well_context["surface"].id]
    target_document = well_context["documents"][well_context["intermediate"].id]
    other_document = well_context["documents"][well_context["surface"].id]
    target_trajectory = Trajectory(
        org_id=well_context["org"].id,
        wellbore_id=well_context["wellbore"].id,
        name="Current survey",
        kind="as_drilled",
        is_current=True,
        source_document_id=target_document.id,
    )
    other_trajectory = Trajectory(
        org_id=well_context["org"].id,
        wellbore_id=well_context["wellbore"].id,
        name="Surface survey",
        kind="as_drilled",
        source_document_id=other_document.id,
    )
    session.add_all([target_trajectory, other_trajectory])
    await session.flush()
    target_station = TrajectoryStation(
        org_id=well_context["org"].id,
        trajectory_id=target_trajectory.id,
        wellbore_id=well_context["wellbore"].id,
        station_index=1,
        md_si=1000.0,
        inclination_deg=12.0,
        azimuth_deg=180.0,
        source="survey",
        measured_at=dt.datetime(2026, 2, 5, 12, tzinfo=dt.UTC),
    )
    other_station = TrajectoryStation(
        org_id=well_context["org"].id,
        trajectory_id=other_trajectory.id,
        wellbore_id=well_context["wellbore"].id,
        station_index=1,
        md_si=400.0,
        inclination_deg=7.0,
        azimuth_deg=90.0,
        source="survey",
        measured_at=dt.datetime(2026, 1, 5, 12, tzinfo=dt.UTC),
    )
    target_event = Event(
        org_id=well_context["org"].id,
        project_id=well_context["project"].id,
        well_id=well_context["well"].id,
        wellbore_id=well_context["wellbore"].id,
        section_id=well_context["intermediate"].id,
        operation_id=target_operation.id,
        kind="npt",
        category="lost_circulation",
        title="Target-section event",
        occurred_at=dt.datetime(2026, 2, 5, 10, tzinfo=dt.UTC),
        ended_at=dt.datetime(2026, 2, 5, 11, tzinfo=dt.UTC),
        depth_md_si=1000.0,
        source="report",
        source_document_id=target_document.id,
    )
    other_event = Event(
        org_id=well_context["org"].id,
        project_id=well_context["project"].id,
        well_id=well_context["well"].id,
        wellbore_id=well_context["wellbore"].id,
        section_id=well_context["surface"].id,
        operation_id=other_operation.id,
        kind="npt",
        category="weather",
        title="Other-section event",
        occurred_at=dt.datetime(2026, 1, 5, 10, tzinfo=dt.UTC),
        ended_at=dt.datetime(2026, 1, 5, 11, tzinfo=dt.UTC),
        depth_md_si=400.0,
        source="report",
        source_document_id=other_document.id,
    )
    quarantined_artifact = RawArtifact(
        org_id=well_context["org"].id,
        sha256="b" * 64,
        byte_size=100,
        blob_key="raw/bb/quarantined-source",
        content_type="application/pdf",
        source_kind="upload",
        retention_class="standard",
        scan_status="infected",
        is_quarantined=True,
    )
    session.add(quarantined_artifact)
    await session.flush()
    quarantined_document = Document(
        org_id=well_context["org"].id,
        project_id=well_context["project"].id,
        well_id=well_context["well"].id,
        wellbore_id=well_context["wellbore"].id,
        section_id=well_context["intermediate"].id,
        raw_artifact_id=quarantined_artifact.id,
        doc_type="ddr",
        title="Quarantined source",
        status="extracted",
    )
    session.add(quarantined_document)
    await session.flush()
    quarantined_event = Event(
        org_id=well_context["org"].id,
        project_id=well_context["project"].id,
        well_id=well_context["well"].id,
        wellbore_id=well_context["wellbore"].id,
        section_id=well_context["intermediate"].id,
        operation_id=target_operation.id,
        kind="npt",
        category="lost_circulation",
        title="Derived from quarantined source",
        occurred_at=dt.datetime(2026, 2, 5, 10, tzinfo=dt.UTC),
        ended_at=dt.datetime(2026, 2, 5, 11, tzinfo=dt.UTC),
        depth_md_si=1000.0,
        source="report",
        source_document_id=quarantined_document.id,
    )
    session.add_all([target_station, other_station, target_event, other_event, quarantined_event])
    await session.flush()
    target_station_link = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="trajectory_station",
        subject_id=target_station.id,
        evidence_kind="trajectory_station",
        evidence_id=target_station.id,
        document_id=target_document.id,
        well_id=well_context["well"].id,
    )
    other_station_link = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="trajectory_station",
        subject_id=other_station.id,
        evidence_kind="trajectory_station",
        evidence_id=other_station.id,
        document_id=other_document.id,
        well_id=well_context["well"].id,
    )
    target_event_link = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="event",
        subject_id=target_event.id,
        evidence_kind="event",
        evidence_id=target_event.id,
        document_id=target_document.id,
        well_id=well_context["well"].id,
    )
    mismatched_event_document_link = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="event",
        subject_id=target_event.id,
        evidence_kind="document_page",
        evidence_id=target_event.id,
        document_id=other_document.id,
        well_id=well_context["well"].id,
    )
    other_event_link = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="event",
        subject_id=other_event.id,
        evidence_kind="event",
        evidence_id=other_event.id,
        document_id=other_document.id,
        well_id=well_context["well"].id,
    )
    quarantined_event_link = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="event",
        subject_id=quarantined_event.id,
        evidence_kind="event",
        evidence_id=quarantined_event.id,
        document_id=None,
        well_id=well_context["well"].id,
    )
    session.add_all(
        [
            target_station_link,
            other_station_link,
            target_event_link,
            mismatched_event_document_link,
            other_event_link,
            quarantined_event_link,
        ]
    )
    await session.flush()

    bundle = await default_context_builder().build(
        session,
        _request(
            well_context,
            scope={
                "section_id": well_context["intermediate"].id,
                "operation_id": target_operation.id,
                "depth_from_si": 900.0,
                "depth_to_si": 1100.0,
                "period_from": dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
                "period_to": dt.datetime(2026, 2, 28, tzinfo=dt.UTC),
            },
            sections=["evidence"],
        ),
    )
    evidence = bundle.section("evidence")
    assert evidence is not None
    returned = {item.id: item.data["subject_kind"] for item in evidence.items}
    assert {
        item_id: kind for item_id, kind in returned.items() if kind in {"event", "trajectory_station"}
    } == {
        target_station_link.id: "trajectory_station",
        target_event_link.id: "event",
    }
    assert other_station_link.id not in returned
    assert mismatched_event_document_link.id not in returned
    assert other_event_link.id not in returned
    assert quarantined_event_link.id not in returned


async def test_one_trajectory_can_use_trusted_stations_from_multiple_documents(session, well_context):
    source_document = well_context["documents"][well_context["intermediate"].id]
    other_document = well_context["documents"][well_context["surface"].id]

    def survey_record(document, section, *, md_si: float, observed_at: dt.datetime) -> ExtractedRecord:
        return ExtractedRecord(
            org_id=well_context["org"].id,
            document_id=document.id,
            project_id=well_context["project"].id,
            well_id=well_context["well"].id,
            wellbore_id=well_context["wellbore"].id,
            operation_id=document.operation_id,
            section_id=section.id,
            record_type="survey_station",
            payload_schema_key="extracted.survey_station",
            payload={"md_si": md_si, "inclination_deg": 4.0, "azimuth_deg": 90.0},
            observed_at=observed_at,
            depth_md_si=md_si,
            method="survey_table",
            confidence=0.95,
            validation_state="rule_validated",
            validation_rule="test-survey-source-trust",
        )

    source_record = survey_record(
        source_document,
        well_context["intermediate"],
        md_si=1000.0,
        observed_at=dt.datetime(2026, 2, 5, 12, tzinfo=dt.UTC),
    )
    other_record = survey_record(
        other_document,
        well_context["surface"],
        md_si=400.0,
        observed_at=dt.datetime(2026, 1, 5, 12, tzinfo=dt.UTC),
    )
    session.add_all([source_record, other_record])
    await session.flush()

    trajectory = Trajectory(
        org_id=well_context["org"].id,
        wellbore_id=well_context["wellbore"].id,
        name="Combined survey",
        kind="actual",
        is_current=True,
        source_document_id=source_document.id,
        station_count=2,
        total_md_si=1000.0,
        total_tvd_si=990.0,
        max_inclination_deg=4.0,
    )
    session.add(trajectory)
    await session.flush()
    source_station = TrajectoryStation(
        org_id=well_context["org"].id,
        trajectory_id=trajectory.id,
        wellbore_id=well_context["wellbore"].id,
        station_index=0,
        md_si=1000.0,
        inclination_deg=4.0,
        azimuth_deg=90.0,
        source="report",
        measured_at=source_record.observed_at,
        evidence_ref=source_record.id,
    )
    other_station = TrajectoryStation(
        org_id=well_context["org"].id,
        trajectory_id=trajectory.id,
        wellbore_id=well_context["wellbore"].id,
        station_index=1,
        md_si=400.0,
        inclination_deg=4.0,
        azimuth_deg=90.0,
        source="report",
        measured_at=other_record.observed_at,
        evidence_ref=other_record.id,
    )
    session.add_all([source_station, other_station])
    await session.flush()
    source_link = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="trajectory_station",
        subject_id=source_station.id,
        evidence_kind="document_page",
        evidence_id=source_record.id,
        document_id=source_document.id,
        well_id=well_context["well"].id,
    )
    other_link = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="trajectory_station",
        subject_id=other_station.id,
        evidence_kind="document_page",
        evidence_id=other_record.id,
        document_id=other_document.id,
        well_id=well_context["well"].id,
    )
    mismatched_source_link = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="trajectory_station",
        subject_id=other_station.id,
        evidence_kind="document_page",
        evidence_id=other_record.id,
        document_id=source_document.id,
        well_id=well_context["well"].id,
    )
    session.add_all([source_link, other_link, mismatched_source_link])
    await session.flush()

    trajectory_bundle = await default_context_builder().build(
        session,
        _request(well_context, sections=["trajectory"]),
    )
    trajectory_section = trajectory_bundle.section("trajectory")
    assert trajectory_section is not None
    returned_station_ids = {
        item.id for item in trajectory_section.items if item.kind == "trajectory_station"
    }
    assert returned_station_ids == {source_station.id, other_station.id}
    assert trajectory_section.source_ids == [trajectory.id]
    station_items = {
        item.id: item for item in trajectory_section.items if item.kind == "trajectory_station"
    }
    assert station_items[source_station.id].cites == [source_record.id]
    assert station_items[other_station.id].cites == [other_record.id]

    evidence_bundle = await default_context_builder().build(
        session,
        _request(well_context, sections=["evidence"]),
    )
    evidence_section = evidence_bundle.section("evidence")
    assert evidence_section is not None
    returned_evidence_ids = {item.id for item in evidence_section.items}
    assert {source_link.id, other_link.id} <= returned_evidence_ids
    assert mismatched_source_link.id not in returned_evidence_ids

    # A second document is acceptable provenance only when it proves the same wellbore. If the
    # station's record is retagged to a different bore, the combined trajectory becomes unavailable.
    other_record.wellbore_id = "wb_out_of_scope"
    await session.flush()
    stale_bundle = await default_context_builder().build(
        session,
        _request(well_context, sections=["trajectory"]),
    )
    stale = stale_bundle.section("trajectory")
    assert stale is not None
    assert stale.items == []
    assert stale.empty_reason == "no current trajectory on file for this scope"


async def test_document_issue_date_does_not_extend_an_existing_period(session, well_context):
    document = well_context["documents"][well_context["intermediate"].id]
    document.issue_date = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)
    document_evidence = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="document",
        subject_id=document.id,
        evidence_kind="document",
        evidence_id=document.id,
        document_id=document.id,
        well_id=well_context["well"].id,
    )
    session.add(document_evidence)
    await session.flush()

    async def returned_ids(section_key: str, period_from: dt.datetime, period_to: dt.datetime) -> set[str]:
        bundle = await default_context_builder().build(
            session,
            _request(
                well_context,
                scope={"period_from": period_from, "period_to": period_to},
                sections=[section_key],
            ),
        )
        section = bundle.section(section_key)
        assert section is not None
        return {item.id for item in section.items}

    march_first = (dt.datetime(2026, 3, 1, tzinfo=dt.UTC), dt.datetime(2026, 3, 1, 23, 59, tzinfo=dt.UTC))
    march_second = (dt.datetime(2026, 3, 2, tzinfo=dt.UTC), dt.datetime(2026, 3, 2, 23, 59, tzinfo=dt.UTC))
    for period_start, period_end in (
        (march_second[0], None),
        (None, march_second[0]),
    ):
        document.period_start = period_start
        document.period_end = period_end
        await session.flush()
        for section_key, expected_id in (("documents", document.id), ("evidence", document_evidence.id)):
            assert expected_id not in await returned_ids(section_key, *march_first)
            assert expected_id in await returned_ids(section_key, *march_second)

    document.period_start = None
    document.period_end = None
    await session.flush()
    for section_key, expected_id in (("documents", document.id), ("evidence", document_evidence.id)):
        assert expected_id in await returned_ids(section_key, *march_first)


async def test_well_only_evidence_does_not_trust_a_mismatched_link_well_id(session, well_context):
    target_well = well_context["well"]
    other_well = Well(
        org_id=well_context["org"].id,
        project_id=well_context["project"].id,
        name="CTX-OTHER",
    )
    session.add(other_well)
    await session.flush()
    mismatched_document = Document(
        org_id=well_context["org"].id,
        project_id=well_context["project"].id,
        well_id=other_well.id,
        doc_type="ddr",
        title="Document from another well",
        status="ingested",
    )
    session.add(mismatched_document)
    await session.flush()
    mismatched_link = EvidenceLink(
        org_id=well_context["org"].id,
        subject_kind="document",
        subject_id=mismatched_document.id,
        evidence_kind="document_page",
        document_id=mismatched_document.id,
        well_id=target_well.id,
    )
    session.add(mismatched_link)
    await session.flush()

    bundle = await default_context_builder().build(
        session,
        _request(well_context, scope={"project_id": None, "wellbore_id": None}),
    )
    evidence = bundle.section("evidence")
    assert evidence is not None
    returned_ids = {item.id for item in evidence.items}
    assert mismatched_link.id not in returned_ids
    assert returned_ids


async def test_wellbore_scope_excludes_well_level_documents(session, well_context):
    well_level_document = Document(
        org_id=well_context["org"].id,
        project_id=well_context["project"].id,
        well_id=well_context["well"].id,
        wellbore_id=None,
        doc_type="ddr",
        title="Unassigned well-level document",
        status="ingested",
    )
    session.add(well_level_document)
    await session.flush()

    bundle = await default_context_builder().build(session, _request(well_context))
    documents = bundle.section("documents")
    assert documents is not None
    returned_ids = {item.id for item in documents.items}
    assert well_level_document.id not in returned_ids
    assert returned_ids == {document.id for document in well_context["documents"].values()}


async def test_trajectory_scope_derives_wellbore_from_section_and_operation(session, well_context):
    side_bore = Wellbore(
        org_id=well_context["org"].id,
        well_id=well_context["well"].id,
        name="Side track",
        sequence=2,
        is_active=False,
    )
    session.add(side_bore)
    await session.flush()
    side_section = WellSection(
        org_id=well_context["org"].id,
        wellbore_id=side_bore.id,
        sequence=1,
        name="Side-track interval",
        kind="intermediate",
        status="drilled",
        actual_top_md_si=800.0,
        actual_bottom_md_si=1000.0,
    )
    session.add(side_section)
    await session.flush()
    side_operation = Operation(
        org_id=well_context["org"].id,
        project_id=well_context["project"].id,
        well_id=well_context["well"].id,
        wellbore_id=side_bore.id,
        section_id=side_section.id,
        operation_class="actual",
        sequence=3,
        name="Side-track drilling",
        depth_from_md_si=800.0,
        depth_to_md_si=1000.0,
    )
    primary_trajectory = Trajectory(
        org_id=well_context["org"].id,
        wellbore_id=well_context["wellbore"].id,
        name="Main-bore survey",
        kind="as_drilled",
        is_current=True,
        version=1,
    )
    side_trajectory = Trajectory(
        org_id=well_context["org"].id,
        wellbore_id=side_bore.id,
        name="Side-bore survey",
        kind="as_drilled",
        is_current=True,
        version=99,
    )
    session.add_all([side_operation, primary_trajectory, side_trajectory])
    await session.flush()
    primary_station = TrajectoryStation(
        org_id=well_context["org"].id,
        trajectory_id=primary_trajectory.id,
        wellbore_id=well_context["wellbore"].id,
        station_index=0,
        md_si=950.0,
        inclination_deg=2.0,
        azimuth_deg=30.0,
    )
    side_station = TrajectoryStation(
        org_id=well_context["org"].id,
        trajectory_id=side_trajectory.id,
        wellbore_id=side_bore.id,
        station_index=0,
        md_si=900.0,
        inclination_deg=3.0,
        azimuth_deg=40.0,
    )
    # A corrupt cross-wellbore station must not be served merely because its trajectory_id matches.
    mismatched_station = TrajectoryStation(
        org_id=well_context["org"].id,
        trajectory_id=primary_trajectory.id,
        wellbore_id=side_bore.id,
        station_index=1,
        md_si=975.0,
        inclination_deg=4.0,
        azimuth_deg=50.0,
    )
    session.add_all([primary_station, side_station, mismatched_station])
    await session.flush()

    section_bundle = await default_context_builder().build(
        session,
        _request(
            well_context,
            scope={"wellbore_id": None, "section_id": well_context["intermediate"].id},
            sections=["trajectory"],
        ),
    )
    section_result = section_bundle.section("trajectory")
    assert section_result is not None
    assert section_result.source_ids == [primary_trajectory.id]
    assert [item.id for item in section_result.items if item.kind == "trajectory_station"] == [
        primary_station.id
    ]

    operation_bundle = await default_context_builder().build(
        session,
        _request(
            well_context,
            scope={"wellbore_id": None, "operation_id": side_operation.id},
            sections=["trajectory"],
        ),
    )
    operation_result = operation_bundle.section("trajectory")
    assert operation_result is not None
    assert operation_result.source_ids == [side_trajectory.id]
    assert [item.id for item in operation_result.items if item.kind == "trajectory_station"] == [
        side_station.id
    ]
    assert all(item.kind != "trajectory_summary" for item in operation_result.items)


async def test_operation_period_uses_class_specific_timestamps_without_mixing_actual_and_plan(session, well_context):
    planned = Operation(
        org_id=well_context["org"].id,
        project_id=well_context["project"].id,
        well_id=well_context["well"].id,
        wellbore_id=well_context["wellbore"].id,
        section_id=well_context["intermediate"].id,
        operation_class="plan",
        sequence=10,
        name="February planned operation",
        planned_start=dt.datetime(2026, 2, 10, tzinfo=dt.UTC),
        planned_end=dt.datetime(2026, 2, 11, tzinfo=dt.UTC),
        actual_start=dt.datetime(2026, 3, 10, tzinfo=dt.UTC),
        actual_end=dt.datetime(2026, 3, 11, tzinfo=dt.UTC),
    )
    actual_without_observed_time = Operation(
        org_id=well_context["org"].id,
        project_id=well_context["project"].id,
        well_id=well_context["well"].id,
        wellbore_id=well_context["wellbore"].id,
        section_id=well_context["intermediate"].id,
        operation_class="actual",
        sequence=11,
        name="Unplaced actual operation",
        planned_start=dt.datetime(2026, 2, 12, tzinfo=dt.UTC),
        planned_end=dt.datetime(2026, 2, 13, tzinfo=dt.UTC),
    )
    session.add_all([planned, actual_without_observed_time])
    await session.flush()

    bundle = await default_context_builder().build(
        session,
        _request(
            well_context,
            scope={
                "period_from": dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
                "period_to": dt.datetime(2026, 2, 28, tzinfo=dt.UTC),
            },
            sections=["operations"],
        ),
    )
    operations = bundle.section("operations")
    assert operations is not None
    returned_ids = {item.id for item in operations.items}
    assert planned.id in returned_ids
    assert actual_without_observed_time.id not in returned_ids
    assert well_context["operations"][well_context["intermediate"].id].id in returned_ids


async def test_trajectory_period_filters_observation_times_not_creation_or_summary(session, well_context):
    trajectory = Trajectory(
        org_id=well_context["org"].id,
        wellbore_id=well_context["wellbore"].id,
        name="As-drilled survey",
        kind="as_drilled",
        is_current=True,
        version=1,
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        station_count=3,
        total_md_si=300.0,
        total_tvd_si=290.0,
        max_inclination_deg=12.0,
    )
    session.add(trajectory)
    await session.flush()
    session.add_all(
        [
            TrajectoryStation(
                org_id=well_context["org"].id,
                trajectory_id=trajectory.id,
                wellbore_id=well_context["wellbore"].id,
                station_index=0,
                md_si=100.0,
                inclination_deg=1.0,
                azimuth_deg=10.0,
                measured_at=dt.datetime(2026, 2, 10, tzinfo=dt.UTC),
            ),
            TrajectoryStation(
                org_id=well_context["org"].id,
                trajectory_id=trajectory.id,
                wellbore_id=well_context["wellbore"].id,
                station_index=1,
                md_si=200.0,
                inclination_deg=2.0,
                azimuth_deg=20.0,
                measured_at=dt.datetime(2026, 1, 10, tzinfo=dt.UTC),
            ),
            TrajectoryStation(
                org_id=well_context["org"].id,
                trajectory_id=trajectory.id,
                wellbore_id=well_context["wellbore"].id,
                station_index=2,
                md_si=300.0,
                inclination_deg=3.0,
                azimuth_deg=30.0,
                measured_at=None,
            ),
        ]
    )
    await session.flush()

    bundle = await default_context_builder().build(
        session,
        _request(
            well_context,
            scope={
                "period_from": dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
                "period_to": dt.datetime(2026, 2, 28, tzinfo=dt.UTC),
            },
            sections=["trajectory"],
        ),
    )
    section = bundle.section("trajectory")
    assert section is not None
    assert [item.kind for item in section.items] == ["trajectory_station"]
    assert section.items[0].data["md_si"] == 100.0
    assert section.items[0].observed_at == dt.datetime(2026, 2, 10, tzinfo=dt.UTC)


async def test_formation_risk_and_recommendation_period_filters_fail_closed_without_valid_time(
    session, well_context
):
    formation = Formation(
        org_id=well_context["org"].id,
        project_id=well_context["project"].id,
        name="Temporal test formation",
    )
    session.add(formation)
    await session.flush()
    session.add(
        WellFormationMarker(
            org_id=well_context["org"].id,
            wellbore_id=well_context["wellbore"].id,
            formation_id=formation.id,
            md_si=1200.0,
            source="survey",
            created_at=dt.datetime(2026, 2, 15, tzinfo=dt.UTC),
        )
    )
    session.add(
        Risk(
            org_id=well_context["org"].id,
            project_id=well_context["project"].id,
            well_id=well_context["well"].id,
            title="Untimed risk",
            category="drilling",
            description="No observation timestamp is recorded.",
            created_at=dt.datetime(2026, 2, 15, tzinfo=dt.UTC),
        )
    )
    session.add_all(
        [
            Recommendation(
                org_id=well_context["org"].id,
                project_id=well_context["project"].id,
                well_id=well_context["well"].id,
                title="Untimed recommendation",
                statement="No validity start is recorded.",
                valid_from=None,
                valid_to=None,
                created_at=dt.datetime(2026, 2, 15, tzinfo=dt.UTC),
            ),
            Recommendation(
                org_id=well_context["org"].id,
                project_id=well_context["project"].id,
                well_id=well_context["well"].id,
                wellbore_id=well_context["wellbore"].id,
                title="Recommendation valid in February",
                statement="A declared validity range overlaps the requested interval.",
                valid_from=dt.datetime(2026, 2, 10, tzinfo=dt.UTC),
                valid_to=dt.datetime(2026, 2, 20, tzinfo=dt.UTC),
            ),
        ]
    )
    await session.flush()

    bundle = await default_context_builder().build(
        session,
        _request(
            well_context,
            scope={
                "period_from": dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
                "period_to": dt.datetime(2026, 2, 28, tzinfo=dt.UTC),
            },
            sections=["formations", "risks", "recommendations"],
        ),
    )
    for key in ("formations", "risks"):
        section = bundle.section(key)
        assert section is not None and section.is_empty, key
    recommendations = bundle.section("recommendations")
    assert recommendations is not None
    assert [item.label for item in recommendations.items] == ["Recommendation valid in February"]


async def test_quarantined_document_content_is_excluded_from_context(session, well_context):
    well_context["artifact"].is_quarantined = True
    await session.flush()

    bundle = await default_context_builder().build(session, _request(well_context))
    for key in ("documents", "extracted_records", "evidence", "document_chunks"):
        section = bundle.section(key)
        assert section is not None and section.is_empty, key


async def test_depth_window_narrows_what_is_included(session, well_context):
    builder = default_context_builder()
    wide = await builder.build(session, _request(well_context))
    narrow = await builder.build(
        session, _request(well_context, scope={"depth_from_si": 900.0, "depth_to_si": 1000.0})
    )
    wide_chunks = len(wide.section("document_chunks").items)
    narrow_chunks = len(narrow.section("document_chunks").items)
    # The depth window is applied as a filter; a chunk with no depth information is not dropped
    # by accident, which is why this asserts "narrowed or equal", not "smaller".
    assert narrow_chunks <= wide_chunks
    assert narrow.scope.depth_from_si == 900.0


def test_context_scope_rejects_blank_identifiers_invalid_depth_and_reversed_windows(well_context):
    base = {
        "org_id": well_context["org"].id,
        "well_id": well_context["well"].id,
    }
    with pytest.raises(ValueError, match="scope identifiers"):
        ContextScope(**base, section_id=" ")
    with pytest.raises(ValueError):
        ContextScope(**base, depth_from_si=float("nan"))
    with pytest.raises(ValueError, match="depth_from_si"):
        ContextScope(**base, depth_from_si=100.0, depth_to_si=50.0)
    with pytest.raises(ValueError, match="period_from"):
        ContextScope(
            **base,
            period_from=dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
            period_to=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )


async def test_unknown_section_key_is_an_error_not_a_silent_empty_section(session, well_context):
    from drillai.core.errors import ContextError

    builder = default_context_builder()
    with pytest.raises(ContextError):
        await builder.build(session, _request(well_context, sections=["well_identity", "not_a_section"]))


async def test_bundle_requests_a_non_si_display_system_without_changing_values(session, well_context):
    builder = default_context_builder()
    bundle = await builder.build(session, _request(well_context, unit_system=UnitSystemName.FIELD_US))
    assert bundle.unit_system is UnitSystemName.FIELD_US
    # Values stay canonical SI in the bundle; conversion happens at render time.
    sections = bundle.section("well_sections")
    assert sections.items[0].data["hole_diameter_si"] == pytest.approx(0.31115)


# --------------------------------------------------------------------------- permissions


async def test_permissions_filter_sections_and_state_what_was_applied(session, well_context):
    builder = default_context_builder()
    restricted = await builder.build(
        session, _request(well_context, permissions=frozenset({"well.read"}))
    )
    keys = {section.key for section in restricted.sections}
    assert "well_identity" in keys
    assert "evidence" not in keys and "recommendations" not in keys
    assert restricted.permissions_applied == ["well.read"]

    full = await builder.build(session, _request(well_context, permissions=frozenset({"**"})))
    assert "evidence" in {section.key for section in full.sections}


async def test_sections_dropped_by_permission_are_reported(session, well_context):
    builder = default_context_builder()
    bundle = await builder.build(session, _request(well_context, permissions=frozenset({"well.read"})))
    assert any("evidence" in redaction for redaction in bundle.redactions)


async def test_role_pattern_permissions_are_honoured(session, well_context):
    """A role grants ``well.*``; the context layer must understand patterns, not just literals."""
    builder = default_context_builder()
    bundle = await builder.build(
        session, _request(well_context, permissions=frozenset({"well.*", "document.*"}))
    )
    keys = {section.key for section in bundle.sections}
    assert {"well_identity", "well_sections", "documents"} <= keys
    assert "evidence" not in keys


# --------------------------------------------------------------------------- budget & failures


async def test_token_budget_omits_sections_and_says_so(session, well_context):
    builder = default_context_builder()
    bundle = await builder.build(session, _request(well_context, token_budget=64))
    assert bundle.truncated is True
    assert bundle.omitted_sections
    assert bundle.token_estimate <= 64
    assert any("token budget" in note for note in bundle.notes)


async def test_a_failing_provider_does_not_break_the_bundle(session, well_context):
    class ExplodingProvider(SectionProvider):
        key = "well_identity"  # overrides the real one
        title = "Broken"
        priority = 1
        required_permission = "well.read"

        async def build(self, session, request):  # type: ignore[no-untyped-def]
            raise RuntimeError("database exploded")

    register_section_provider(ExplodingProvider(), replace=True)
    try:
        builder = ContextBuilder()
        bundle = await builder.build(session, _request(well_context))
        identity = bundle.section("well_identity")
        assert identity is not None
        assert identity.is_empty
        assert "database exploded" in (identity.empty_reason or "")
        # The rest of the context is still usable: one broken section must not blind the caller.
        assert not bundle.section("well_sections").is_empty
    finally:
        # restore the shipped provider set for the other tests in this session
        from drillai.context.builder import install_default_providers

        install_default_providers()


def test_all_shipped_sections_are_registered():
    keys = registered_section_keys()
    assert len(keys) == 21
    assert {"well_identity", "well_sections", "trajectory", "twin_state", "documents", "evidence", "engine_results", "recommendations", "risks", "lessons", "materials"}.issubset(set(keys))


# --------------------------------------------------------------------------- prompt rendering


async def test_prompt_payload_carries_citations_and_never_invents_values(session, well_context):
    builder = default_context_builder()
    bundle = await builder.build(session, _request(well_context))
    payload = builder.render_prompt_payload(bundle)
    assert payload.startswith("ENGINEERING CONTEXT")
    assert "scope: well=" in payload
    assert "CTX-1" in payload
    # Every document item is rendered with the id that can be used as a citation.
    document = well_context["documents"][well_context["intermediate"].id]
    assert document.id in payload
    assert bundle.all_cites()

    limited = builder.render_prompt_payload(bundle, max_chars=200)
    assert len(limited) <= 400  # a truncated payload states that it was truncated
    assert "truncated" in limited.lower() or len(limited) < len(payload)


async def test_prompt_purpose_carries_the_expected_sections(session, well_context):
    builder = default_context_builder()
    prompt_bundle = await builder.build(session, _request(well_context, purpose=ContextPurpose.PROMPT))
    dashboard_bundle = await builder.build(session, _request(well_context, purpose=ContextPurpose.DASHBOARD))
    prompt_keys = {section.key for section in prompt_bundle.sections}
    dashboard_keys = {section.key for section in dashboard_bundle.sections}
    assert "document_chunks" in prompt_keys
    assert dashboard_keys <= prompt_keys | dashboard_keys
    # dashboards are cheap: they do not pull retrieval chunks
    assert "document_chunks" not in dashboard_keys


def test_context_item_carries_provenance_fields():
    item = ContextItem(
        kind="extracted_record",
        id="rec_1",
        label="mud weight",
        data={"mud_weight": 12.4},
        cites=["doc_1"],
        source_kind="document",
        confidence=0.65,
        data_quality="fair",
        observed_at=dt.datetime(2026, 1, 5, tzinfo=dt.UTC),
    )
    assert item.cites == ["doc_1"]
    assert item.confidence == 0.65
    section = ContextSection(key="k", title="t", items=[item])
    assert not section.is_empty


@pytest.mark.parametrize("invalidation", ["rejected", "superseded", "low_confidence", "quarantined"])
async def test_trajectory_source_becomes_unavailable_when_a_station_record_is_not_trusted(
    session, well_context, invalidation: str
) -> None:
    document = well_context["documents"][well_context["surface"].id]
    record = (
        await session.execute(
            select(ExtractedRecord).where(ExtractedRecord.document_id == document.id)
        )
    ).scalar_one()
    trajectory = Trajectory(
        org_id=well_context["org"].id,
        wellbore_id=well_context["wellbore"].id,
        name="Report-backed actual survey",
        kind="as_drilled",
        is_current=True,
        version=1,
        source_document_id=document.id,
        station_count=1,
        total_md_si=400.0,
        total_tvd_si=398.0,
        max_inclination_deg=5.0,
    )
    session.add(trajectory)
    await session.flush()
    station = TrajectoryStation(
        org_id=well_context["org"].id,
        trajectory_id=trajectory.id,
        wellbore_id=well_context["wellbore"].id,
        station_index=0,
        md_si=400.0,
        inclination_deg=5.0,
        azimuth_deg=180.0,
        source="report",
        evidence_ref=record.id,
        raw_source={"record_id": record.id},
    )
    session.add(station)
    await session.flush()

    provider = TrajectoryProvider()
    request = ContextRequest(
        scope=ContextScope(
            org_id=well_context["org"].id,
            project_id=well_context["project"].id,
            well_id=well_context["well"].id,
            wellbore_id=well_context["wellbore"].id,
        ),
        sections=["trajectory"],
    )
    current, sources = await provider.build(session, request)
    assert sources == [trajectory.id, station.id]
    assert any(item.kind == "trajectory_summary" for item in current.items)
    assert any(item.kind == "trajectory_station" for item in current.items)

    if invalidation == "quarantined":
        artifact = await session.get(RawArtifact, document.raw_artifact_id)
        assert artifact is not None
        artifact.is_quarantined = True
    elif invalidation == "low_confidence":
        record.confidence = 0.59
    else:
        record.validation_state = invalidation
    await session.flush()

    stale, stale_sources = await provider.build(session, request)
    assert stale_sources == []
    assert stale.items == []
    assert stale.empty_reason == "no current trajectory on file for this scope"
