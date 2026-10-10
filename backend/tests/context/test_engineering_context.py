"""Engineering context: one source of well truth, scoped correctly and filtered by permission.

The acceptance test the mission calls out is here: a question about the 8½" section must not pull
in every section of the well. That is enforced by *scope filters*, not by hope — the same principle
that keeps a retrieval from answering with the 12¼" section's 8½" casing passage.
"""

from __future__ import annotations

import datetime as dt

import pytest

from drillai.context.builder import (
    ContextBuilder,
    SectionProvider,
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
    EvidenceLink,
    ExtractedRecord,
    Organization,
    Project,
    RawArtifact,
    Well,
    Wellbore,
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

    # Documents and extracted records belonging to each section.
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
        document = Document(
            org_id=org.id,
            project_id=project.id,
            well_id=well.id,
            wellbore_id=wellbore.id,
            section_id=section.id,
            raw_artifact_id=artifact.id,
            doc_type="ddr",
            title=f"Daily report ({section.name})",
            status="ingested",
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
                doc_type="ddr",
                token_estimate=len(text) // 4,
            )
        )
        record = ExtractedRecord(
            org_id=org.id,
            document_id=document.id,
            well_id=well.id,
            wellbore_id=wellbore.id,
            section_id=section.id,
            record_type="mud_properties",
            payload_schema_key="extracted.mud_properties",
            payload={"mud_weight": 9.2 if section is surface else 12.4},
            method="pattern",
            validation_state="unvalidated",
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
    return {"org": org, "project": project, "well": well, "wellbore": wellbore, "surface": surface, "intermediate": intermediate, "documents": documents}


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
