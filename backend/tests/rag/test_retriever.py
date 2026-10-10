"""Retrieval: structured filters first, then lexical/hybrid ranking.

The acceptance criterion behind these tests is the mission's: asking about the 8½" section must
not return the 12¼" section's passages just because the tokens match.
"""

from __future__ import annotations

import datetime as dt

import pytest

from drillai.db.models import (
    Document,
    DocumentChunk,
    Organization,
    Project,
    RawArtifact,
    Well,
    Wellbore,
    WellSection,
)
from drillai.rag.retriever import RetrievalQuery, RetrieveMode, expand_neighbours, retrieve


@pytest.fixture
async def corpus(session):
    org = Organization(slug="rag", name="RAG Org")
    session.add(org)
    await session.flush()
    project = Project(org_id=org.id, name="RAG project")
    session.add(project)
    await session.flush()
    well = Well(org_id=org.id, project_id=project.id, name="RAG-1")
    session.add(well)
    await session.flush()
    wellbore = Wellbore(org_id=org.id, well_id=well.id, name="Main")
    session.add(wellbore)
    await session.flush()
    surface = WellSection(
        org_id=org.id, wellbore_id=wellbore.id, sequence=1, name='12-1/4" section', kind="surface", status="drilled"
    )
    intermediate = WellSection(
        org_id=org.id, wellbore_id=wellbore.id, sequence=2, name='8-1/2" section', kind="intermediate", status="drilling"
    )
    session.add_all([surface, intermediate])
    await session.flush()
    artifact = RawArtifact(
        org_id=org.id,
        sha256="b" * 64,
        byte_size=10,
        blob_key="raw/bb/y",
        content_type="application/pdf",
        source_kind="upload",
        retention_class="standard",
        scan_status="clean",
    )
    session.add(artifact)
    await session.flush()

    async def add_document(section, doc_type: str, texts: list[str], **kwargs):
        document = Document(
            org_id=org.id,
            project_id=project.id,
            well_id=well.id,
            wellbore_id=wellbore.id,
            section_id=section.id if section else None,
            raw_artifact_id=artifact.id,
            doc_type=doc_type,
            title=f"{doc_type} {section.name if section else 'well'}",
            status="ingested",
        )
        session.add(document)
        await session.flush()
        for index, text in enumerate(texts):
            session.add(
                DocumentChunk(
                    org_id=org.id,
                    document_id=document.id,
                    chunk_index=index,
                    page_number=index + 1,
                    text=text,
                    kind="text",
                    well_id=well.id,
                    wellbore_id=wellbore.id,
                    section_id=section.id if section else None,
                    doc_type=doc_type,
                    depth_from_si=kwargs.get("depth_from_si"),
                    depth_to_si=kwargs.get("depth_to_si"),
                    period_start=kwargs.get("period_start"),
                    period_end=kwargs.get("period_end"),
                    token_estimate=len(text) // 4,
                )
            )
        await session.flush()
        return document

    intermediate_doc = await add_document(
        intermediate,
        "ddr",
        [
            "The 8-1/2 inch section was drilled with a mud weight of 12.4 ppg and a plastic viscosity of 22 cp.",
            "Torque and drag in the 8-1/2 inch hole stayed within the drilling program limits at 1300 m MD.",
        ],
        depth_from_si=800.0,
        depth_to_si=1500.0,
        period_start=dt.datetime(2026, 1, 5, tzinfo=dt.UTC),
    )
    surface_doc = await add_document(
        surface,
        "ddr",
        ["The 12-1/4 inch surface section used 9.2 ppg mud and a 1.5 degree inclination."],
        depth_from_si=0.0,
        depth_to_si=800.0,
    )
    program_doc = await add_document(
        None,
        "program",
        ["The drilling program states the 8-1/2 inch section will use a mud weight of 12.2 to 12.8 ppg."],
    )
    return {
        "org": org,
        "project": project,
        "well": well,
        "wellbore": wellbore,
        "surface": surface,
        "intermediate": intermediate,
        "documents": {"intermediate": intermediate_doc, "surface": surface_doc, "program": program_doc},
    }


async def test_retrieval_must_be_scoped(corpus, session):
    with pytest.raises(ValueError):
        await retrieve(session, corpus["org"].id, RetrievalQuery(text="mud weight"))


async def test_section_filter_keeps_the_answer_in_the_right_hole_section(corpus, session):
    """The 8½" question must not be answered with the 12¼" section's passages."""
    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(
            text="8-1/2 inch section mud weight",
            mode=RetrieveMode.HYBRID,
            well_id=corpus["well"].id,
            section_id=corpus["intermediate"].id,
            limit=10,
        ),
    )
    assert result.hits
    assert all(hit.section_id == corpus["intermediate"].id for hit in result.hits)
    assert all("12-1/4" not in hit.text for hit in result.hits)
    assert result.filters_applied["section_id"] == corpus["intermediate"].id


async def test_lexical_ranking_prefers_the_relevant_passage(corpus, session):
    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(text="plastic viscosity", mode=RetrieveMode.LEXICAL, well_id=corpus["well"].id, limit=3),
    )
    assert result.hits
    assert "plastic viscosity" in result.hits[0].text
    assert result.hits[0].explain.get("bm25") is not None
    assert result.hits[0].citations  # a hit is always citable


async def test_hybrid_falls_back_to_lexical_and_says_so(corpus, session):
    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(text="mud weight", mode=RetrieveMode.HYBRID, well_id=corpus["well"].id),
    )
    # No embedding provider is configured in the test environment, so the mode used is reported
    # as lexical and a note explains why. It never pretends a vector search happened.
    assert result.mode_used == "lexical"
    assert any("vector search unavailable" in note for note in result.notes)


async def test_metadata_mode_ignores_relevance_and_returns_the_newest(corpus, session):
    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(mode=RetrieveMode.METADATA, well_id=corpus["well"].id, doc_type="program"),
    )
    assert len(result.hits) == 1
    assert result.hits[0].doc_type == "program"
    assert result.hits[0].explain["reason"].startswith("metadata filter only")


async def test_depth_and_period_filters_are_applied(corpus, session):
    deep = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(text="mud", mode=RetrieveMode.LEXICAL, well_id=corpus["well"].id, depth_from_si=1000.0),
    )
    # A hit is either inside the window or carries no depth at all (well-level text is not dropped
    # just because it cannot be placed in depth); nothing that ends above the window is returned.
    for hit in deep.hits:
        assert hit.depth_to_si is None or hit.depth_to_si >= 1000.0

    windowed = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(
            text="mud",
            mode=RetrieveMode.LEXICAL,
            well_id=corpus["well"].id,
            period_from=dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
        ),
    )
    # the January chunk is outside the window (a single-ended period is treated as a point
    # in time); chunks with no dates at all stay eligible
    assert all(hit.period_start is None or hit.period_start >= dt.datetime(2026, 2, 1, tzinfo=dt.UTC) for hit in windowed.hits)


async def test_project_scope_sees_across_wells_in_the_project(corpus, session):
    result = await retrieve(
        session, corpus["org"].id, RetrievalQuery(text="mud weight", project_id=corpus["project"].id, mode="lexical")
    )
    assert len(result.hits) >= 2


async def test_neighbour_expansion_adds_adjacent_chunks_with_a_reason(corpus, session):
    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(text="plastic viscosity", mode=RetrieveMode.LEXICAL, well_id=corpus["well"].id, limit=1),
    )
    expanded = await expand_neighbours(session, result.hits, radius=1)
    assert len(expanded) > len(result.hits)
    added = [hit for hit in expanded if hit.explain.get("reason", "").startswith("neighbour of")]
    assert added and added[0].score < result.hits[0].score  # a neighbour is never ranked above its hit


async def test_min_score_filters_weak_matches(corpus, session):
    all_hits = await retrieve(
        session, corpus["org"].id, RetrievalQuery(text="mud weight", mode=RetrieveMode.LEXICAL, well_id=corpus["well"].id)
    )
    filtered = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(
            text="mud weight",
            mode=RetrieveMode.LEXICAL,
            well_id=corpus["well"].id,
            min_score=(all_hits.hits[0].score + all_hits.hits[-1].score) / 2,
        ),
    )
    assert len(filtered.hits) < len(all_hits.hits)
