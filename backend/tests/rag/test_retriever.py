"""Retrieval: structured filters first, then lexical/hybrid ranking.

The acceptance criterion behind these tests is the mission's: asking about the 8½" section must
not return the 12¼" section's passages just because the tokens match.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest
from sqlalchemy import select

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
        "artifact": artifact,
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


async def test_broad_parent_document_allows_more_specific_chunk_scope(corpus, session):
    # A wellbore-level filing can legitimately contain a passage tagged to one section. Parent scope
    # fields are binding when present; an unset section on the parent is not a claim that the chunk has
    # no section.
    document = corpus["documents"]["intermediate"]
    document.section_id = None
    await session.flush()

    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(
            text="plastic viscosity",
            mode=RetrieveMode.LEXICAL,
            well_id=corpus["well"].id,
            section_id=corpus["intermediate"].id,
            limit=5,
        ),
    )
    assert result.hits
    assert all(hit.section_id == corpus["intermediate"].id for hit in result.hits)


async def test_project_scope_cannot_smuggle_a_child_from_another_project(corpus, session):
    foreign_project = Project(org_id=corpus["org"].id, name="RAG foreign project")
    session.add(foreign_project)
    await session.flush()
    foreign_well = Well(
        org_id=corpus["org"].id,
        project_id=foreign_project.id,
        name="RAG-FOREIGN-PROJECT",
    )
    session.add(foreign_well)
    await session.flush()

    broad_document = corpus["documents"]["program"]
    child = (
        await session.execute(
            select(DocumentChunk).where(DocumentChunk.document_id == broad_document.id)
        )
    ).scalar_one()
    child.text = "foreign-project-only sentinel must not enter the selected project"
    child.well_id = foreign_well.id
    await session.flush()

    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(
            text="foreign-project-only sentinel",
            mode=RetrieveMode.LEXICAL,
            project_id=corpus["project"].id,
            limit=5,
        ),
    )
    assert all(hit.chunk_id != child.id for hit in result.hits)
    assert all("foreign-project-only sentinel" not in hit.text for hit in result.hits)


async def test_quarantined_artifacts_are_excluded_from_hits_and_neighbour_expansion(corpus, session):
    query = RetrievalQuery(
        text="plastic viscosity",
        mode=RetrieveMode.LEXICAL,
        well_id=corpus["well"].id,
        limit=1,
    )
    before_quarantine = await retrieve(session, corpus["org"].id, query)
    assert before_quarantine.hits

    corpus["artifact"].is_quarantined = True
    await session.flush()

    after_quarantine = await retrieve(session, corpus["org"].id, query)
    assert after_quarantine.hits == []
    expanded = await expand_neighbours(
        session,
        before_quarantine.hits,
        radius=1,
        org_id=corpus["org"].id,
        query=query,
    )
    assert expanded == []


@pytest.mark.parametrize(
    "query",
    [
        RetrievalQuery(well_id=""),
        RetrievalQuery(project_id=""),
        RetrievalQuery(well_id=" ", project_id="project_nonempty"),
        RetrievalQuery(well_id=None, project_id="  "),
    ],
)
async def test_retrieval_rejects_blank_scope_even_when_the_other_scope_is_truthy(query, corpus, session):
    with pytest.raises(ValueError):
        await retrieve(session, corpus["org"].id, query)


@pytest.mark.parametrize("radius", [True, -1, 1.5, 3])
async def test_retrieval_rejects_invalid_neighbour_radius(radius, corpus, session):
    with pytest.raises(ValueError, match="include_neighbours"):
        await retrieve(
            session,
            corpus["org"].id,
            RetrievalQuery(well_id=corpus["well"].id, include_neighbours=radius),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("wellbore_id", " "),
        ("section_id", ""),
        ("operation_id", " "),
        ("formation_id", ""),
        ("doc_type", " "),
        ("chunk_kind", ""),
    ],
)
async def test_retrieval_rejects_blank_optional_scope_filters(corpus, session, field, value):
    query = RetrievalQuery(well_id=corpus["well"].id, **{field: value})
    with pytest.raises(ValueError, match=field):
        await retrieve(session, corpus["org"].id, query)


@pytest.mark.parametrize("min_score", [True, float("nan"), float("inf"), "0.2"])
async def test_retrieval_rejects_nonfinite_or_nonnumeric_scores(corpus, session, min_score):
    with pytest.raises(ValueError, match="min_score"):
        await retrieve(
            session,
            corpus["org"].id,
            RetrievalQuery(well_id=corpus["well"].id, min_score=min_score),
        )


async def test_retrieval_rejects_nonfinite_depth_and_reversed_period_windows(corpus, session):
    with pytest.raises(ValueError, match="depth_from_si"):
        await retrieve(
            session,
            corpus["org"].id,
            RetrievalQuery(well_id=corpus["well"].id, depth_from_si=float("nan")),
        )
    with pytest.raises(ValueError, match="period_from"):
        await retrieve(
            session,
            corpus["org"].id,
            RetrievalQuery(
                well_id=corpus["well"].id,
                period_from=dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
                period_to=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            ),
        )
    with pytest.raises(ValueError, match="datetime"):
        await retrieve(
            session,
            corpus["org"].id,
            RetrievalQuery(well_id=corpus["well"].id, period_from="2026-02-01"),
        )


async def test_retrieval_rejects_an_explicit_organization_scope_mismatch(corpus, session):
    with pytest.raises(ValueError, match="authenticated organization"):
        await retrieve(
            session,
            corpus["org"].id,
            RetrievalQuery(well_id=corpus["well"].id, org_id="org_attacker"),
        )


@pytest.mark.parametrize("radius", [True, -1, 1.0, 99])
async def test_neighbour_expansion_validates_radius_before_empty_hit_fast_path(radius, corpus, session):
    with pytest.raises(ValueError):
        await expand_neighbours(
            session,
            [],
            radius=radius,
            org_id=corpus["org"].id,
            query=RetrievalQuery(well_id=corpus["well"].id),
        )


async def test_neighbour_expansion_revalidates_the_full_query_scope_before_empty_hit_fast_path(corpus, session):
    with pytest.raises(ValueError, match="section_id"):
        await expand_neighbours(
            session,
            [],
            radius=0,
            org_id=corpus["org"].id,
            query=RetrievalQuery(well_id=corpus["well"].id, section_id=" "),
        )


async def test_neighbour_expansion_radius_zero_returns_authorized_hits_in_a_fresh_list(corpus, session):
    query = RetrievalQuery(
        text="plastic viscosity",
        mode=RetrieveMode.LEXICAL,
        well_id=corpus["well"].id,
        limit=1,
    )
    result = await retrieve(session, corpus["org"].id, query)
    assert result.hits

    expanded = await expand_neighbours(
        session,
        result.hits,
        radius=0,
        org_id=corpus["org"].id,
        query=query,
    )

    assert expanded is not result.hits
    assert [hit.chunk_id for hit in expanded] == [hit.chunk_id for hit in result.hits]
    assert all(hit.explain.get("reason", "").startswith("neighbour of") is False for hit in expanded)


async def test_filters_applied_reports_every_scope_and_content_filter(corpus, session):
    project_id = "project_nonexistent"
    wellbore_id = "wellbore_nonexistent"
    section_id = "section_nonexistent"
    operation_id = "operation_nonexistent"
    formation_id = "formation_nonexistent"
    doc_type = "survey_report"
    chunk_kind = "table_row"
    period_from = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    period_to = dt.datetime(2025, 1, 31, tzinfo=dt.UTC)
    query = RetrievalQuery(
        text="mud weight",
        well_id=corpus["well"].id,
        project_id=project_id,
        wellbore_id=wellbore_id,
        section_id=section_id,
        operation_id=operation_id,
        formation_id=formation_id,
        doc_type=doc_type,
        chunk_kind=chunk_kind,
        depth_from_si=100.0,
        depth_to_si=200.0,
        period_from=period_from,
        period_to=period_to,
    )

    result = await retrieve(session, corpus["org"].id, query)

    assert result.hits == []
    assert result.filters_applied == {
        "org_id": corpus["org"].id,
        "well_id": corpus["well"].id,
        "project_id": project_id,
        "wellbore_id": wellbore_id,
        "section_id": section_id,
        "operation_id": operation_id,
        "formation_id": formation_id,
        "doc_type": doc_type,
        "chunk_kind": chunk_kind,
        "depth_from_si": 100.0,
        "depth_to_si": 200.0,
        "period_from": period_from,
        "period_to": period_to,
    }


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


async def test_lexical_query_with_no_token_matches_returns_no_unrelated_chunks(corpus, session):
    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(
            text="unmatchedlexicaltoken",
            mode=RetrieveMode.LEXICAL,
            well_id=corpus["well"].id,
            limit=10,
        ),
    )

    assert result.mode_used == "lexical"
    assert result.hits == []
    assert any("no token matches" in note for note in result.notes)


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


async def test_retrieval_limits_are_bounded_before_database_work(corpus, session):
    for invalid in (0, -1, 101):
        with pytest.raises(ValueError, match="limit"):
            await retrieve(
                session,
                corpus["org"].id,
                RetrievalQuery(text="mud weight", mode="lexical", well_id=corpus["well"].id, limit=invalid),
            )


async def test_hybrid_rrf_does_not_return_unmatched_chunks_without_compatible_vectors(
    corpus, session, monkeypatch
):
    import hashlib

    from drillai.ai.providers import LLMCapability, ModelSpec, PrivacyClass, ProviderRegistry
    from drillai.db.models import DocumentChunk
    from drillai.rag.retriever import _embedding_space_id

    class OneVectorProvider:
        name = "one-vector-space"
        model_name = "one-vector-model"

        def supports(self, capability):
            return capability is LLMCapability.EMBEDDING

        def models(self):
            return [
                ModelSpec(
                    provider=self.name,
                    model=self.model_name,
                    capabilities=frozenset({LLMCapability.EMBEDDING}),
                    privacy=PrivacyClass.LOCAL,
                )
            ]

        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    provider = OneVectorProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    monkeypatch.setattr("drillai.ai.providers.provider_registry", lambda: registry)
    chunks = (await session.execute(select(DocumentChunk).order_by(DocumentChunk.created_at))).scalars().all()
    assert len(chunks) >= 2
    target = chunks[0]
    target.embedding = [1.0, 0.0]
    target.embedding_dim = 2
    target.embedding_model = _embedding_space_id(provider)
    target.embedding_content_hash = hashlib.sha256(target.text.encode("utf-8")).hexdigest()
    await session.flush()

    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(
            text="unmatchedlexicaltoken",
            mode=RetrieveMode.HYBRID,
            well_id=corpus["well"].id,
            limit=10,
        ),
    )

    assert result.mode_used == "hybrid"
    assert [hit.chunk_id for hit in result.hits] == [target.id]
    assert "lexical_rank" not in result.hits[0].explain


async def test_embedding_provider_failure_reports_the_real_fallback_without_logging_details(
    corpus, session, monkeypatch, caplog
):
    import hashlib

    from drillai.ai.providers import LLMCapability, ModelSpec, PrivacyClass, ProviderRegistry
    from drillai.db.models import DocumentChunk
    from drillai.rag.retriever import _embedding_space_id

    class FailingProvider:
        name = "failing-embedding-provider"
        model_name = "failing-embedding-model"

        def supports(self, capability):
            return capability is LLMCapability.EMBEDDING

        def models(self):
            return [
                ModelSpec(
                    provider=self.name,
                    model=self.model_name,
                    capabilities=frozenset({LLMCapability.EMBEDDING}),
                    privacy=PrivacyClass.LOCAL,
                )
            ]

        async def embed(self, texts):
            raise RuntimeError("credential=must-not-appear-in-logs")

    provider = FailingProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    monkeypatch.setattr("drillai.ai.providers.provider_registry", lambda: registry)
    chunks = (await session.execute(select(DocumentChunk))).scalars().all()
    for chunk in chunks:
        chunk.embedding = [1.0, 0.0]
        chunk.embedding_dim = 2
        chunk.embedding_model = _embedding_space_id(provider)
        chunk.embedding_content_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
    await session.flush()

    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(
            text="plastic viscosity",
            mode=RetrieveMode.HYBRID,
            well_id=corpus["well"].id,
            limit=3,
        ),
    )

    assert result.mode_used == "lexical"
    assert result.hits and "plastic viscosity" in result.hits[0].text
    assert any("provider could not encode the query" in note for note in result.notes)
    assert all("no embedding provider" not in note for note in result.notes)
    assert "credential=must-not-appear-in-logs" not in caplog.text


async def test_hybrid_stopword_only_query_reports_metadata_fallback(corpus, session):
    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(text="the and", mode="hybrid", well_id=corpus["well"].id, limit=3),
    )
    assert result.mode_used == "metadata"
    assert result.hits
    assert all(hit.explain.get("reason") == "metadata filter only, newest first" for hit in result.hits)
    assert any("no searchable lexical tokens" in note for note in result.notes)


async def test_metadata_ranking_keeps_newest_chunks_first_across_chunk_indexes(corpus, session):
    document = corpus["documents"]["program"]
    older = dt.datetime(2100, 1, 1, tzinfo=dt.UTC)
    chunks = [
        DocumentChunk(
            org_id=corpus["org"].id,
            document_id=document.id,
            chunk_index=index,
            page_number=index,
            text=f"metadata candidate {index}",
            kind="text",
            well_id=corpus["well"].id,
            wellbore_id=corpus["wellbore"].id,
            doc_type="program",
            created_at=older + dt.timedelta(days=day),
        )
        for index, day in ((20, 1), (1, 3), (10, 2))
    ]
    session.add_all(chunks)
    await session.flush()

    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(text="the and", mode=RetrieveMode.METADATA, well_id=corpus["well"].id, limit=3),
    )

    assert [hit.chunk_id for hit in result.hits] == [chunks[1].id, chunks[2].id, chunks[0].id]


async def test_retrieval_reports_when_the_recency_candidate_window_truncates(corpus, session):
    document = corpus["documents"]["program"]
    chunks = [
        DocumentChunk(
            org_id=corpus["org"].id,
            document_id=document.id,
            chunk_index=index + 100,
            page_number=index + 1,
            text=f"candidate-window filler {index}",
            kind="text",
            well_id=corpus["well"].id,
            wellbore_id=corpus["wellbore"].id,
            doc_type="program",
        )
        for index in range(197)
    ]
    session.add_all(chunks)
    await session.flush()
    matching_chunks = (
        await session.execute(select(DocumentChunk).where(DocumentChunk.well_id == corpus["well"].id))
    ).scalars().all()
    assert len(matching_chunks) == 201
    for index, chunk in enumerate(matching_chunks):
        chunk.created_at = dt.datetime(2100, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=index)

    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(text="the and", mode=RetrieveMode.METADATA, well_id=corpus["well"].id, limit=1),
    )

    assert len(result.hits) == 1
    assert any("newest 200 matching chunks" in note and "older matching chunks may be omitted" in note for note in result.notes)


async def test_hybrid_stopword_only_query_uses_semantic_ranking_when_vectors_match(corpus, session, monkeypatch):
    import hashlib

    from drillai.ai.providers import LLMCapability, ModelSpec, PrivacyClass, ProviderRegistry
    from drillai.db.models import DocumentChunk
    from drillai.rag.retriever import _embedding_space_id

    class QueryEmbeddingProvider:
        name = "stopword-query-space"
        model_name = "stopword-query-model"

        def supports(self, capability):
            return capability is LLMCapability.EMBEDDING

        def models(self):
            return [
                ModelSpec(
                    provider=self.name,
                    model=self.model_name,
                    capabilities=frozenset({LLMCapability.EMBEDDING}),
                    privacy=PrivacyClass.LOCAL,
                )
            ]

        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    provider = QueryEmbeddingProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    monkeypatch.setattr("drillai.ai.providers.provider_registry", lambda: registry)
    chunks = (await session.execute(select(DocumentChunk))).scalars().all()
    for chunk in chunks:
        chunk.embedding = [1.0, 0.0]
        chunk.embedding_dim = 2
        chunk.embedding_model = _embedding_space_id(provider)
        chunk.embedding_content_hash = hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
    await session.flush()

    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(text="the and", mode="hybrid", well_id=corpus["well"].id, limit=3),
    )
    assert result.mode_used == "semantic"
    assert result.hits
    assert all("semantic" in hit.explain for hit in result.hits)
    assert any("portable cosine similarity" in note for note in result.notes)


async def test_stale_embedding_content_fingerprint_is_rejected(corpus, session, monkeypatch):
    import hashlib

    from drillai.ai.providers import LLMCapability, ModelSpec, PrivacyClass, ProviderRegistry
    from drillai.db.models import DocumentChunk
    from drillai.rag.retriever import _embedding_space_id

    class QueryEmbeddingProvider:
        name = "stale-query-space"
        model_name = "stale-query-model"

        def supports(self, capability):
            return capability is LLMCapability.EMBEDDING

        def models(self):
            return [
                ModelSpec(
                    provider=self.name,
                    model=self.model_name,
                    capabilities=frozenset({LLMCapability.EMBEDDING}),
                    privacy=PrivacyClass.LOCAL,
                )
            ]

        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    provider = QueryEmbeddingProvider()
    registry = ProviderRegistry()
    registry.register(provider)
    monkeypatch.setattr("drillai.ai.providers.provider_registry", lambda: registry)
    chunks = (await session.execute(select(DocumentChunk))).scalars().all()
    for chunk in chunks:
        old_text = chunk.text
        chunk.embedding = [1.0, 0.0]
        chunk.embedding_dim = 2
        chunk.embedding_model = _embedding_space_id(provider)
        chunk.embedding_content_hash = hashlib.sha256(old_text.encode("utf-8")).hexdigest()
        chunk.text = old_text + " edited after embedding"
    await session.flush()

    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(text="plastic viscosity", mode="semantic", well_id=corpus["well"].id, limit=3),
    )
    assert result.mode_used == "lexical"
    assert result.hits and "plastic viscosity" in result.hits[0].text
    assert any("current chunk text fingerprint" in note for note in result.notes)


async def test_semantic_search_never_compares_equal_dimensions_from_different_models(
    corpus, session, monkeypatch
):
    """Equal dimensions and model labels still do not make different providers' spaces compatible."""
    from drillai.ai.providers import LLMCapability, ModelSpec, PrivacyClass, ProviderRegistry
    from drillai.db.models import DocumentChunk

    class QueryEmbeddingProvider:
        name = "query-space"
        model_name = "shared-model-name"

        def supports(self, capability):
            return capability is LLMCapability.EMBEDDING

        def models(self):
            return [
                ModelSpec(
                    provider=self.name,
                    model=self.model_name,
                    capabilities=frozenset({LLMCapability.EMBEDDING}),
                    privacy=PrivacyClass.LOCAL,
                )
            ]

        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    registry = ProviderRegistry()
    registry.register(QueryEmbeddingProvider())
    monkeypatch.setattr("drillai.ai.providers.provider_registry", lambda: registry)

    class OtherProviderSameModelName:
        name = "different-provider"
        model_name = "shared-model-name"

    from drillai.rag.retriever import _embedding_space_id

    chunks = (await session.execute(select(DocumentChunk))).scalars().all()
    assert chunks
    stored_space = _embedding_space_id(OtherProviderSameModelName())
    assert stored_space is not None
    for chunk in chunks:
        chunk.embedding = [1.0, 0.0]
        chunk.embedding_dim = 2
        chunk.embedding_model = stored_space

    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(text="plastic viscosity", mode="semantic", well_id=corpus["well"].id, limit=3),
    )
    assert result.mode_used == "lexical"
    assert result.hits and "plastic viscosity" in result.hits[0].text
    assert any("model" in note.lower() and "embedding" in note.lower() for note in result.notes)


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
    # An explicit interval requires positive depth evidence; untagged well-level text is not assumed
    # to belong to a narrower window.
    assert deep.hits
    for hit in deep.hits:
        assert hit.depth_from_si is not None or hit.depth_to_si is not None
        assert hit.depth_to_si is None or hit.depth_to_si >= 1000.0

    surface_doc = corpus["documents"]["surface"]
    surface_chunk = DocumentChunk(
        org_id=corpus["org"].id,
        document_id=surface_doc.id,
        chunk_index=3,
        text="February mud weight report with known period metadata.",
        kind="text",
        well_id=corpus["well"].id,
        wellbore_id=corpus["wellbore"].id,
        section_id=corpus["surface"].id,
        doc_type="ddr",
        period_start=dt.datetime(2026, 2, 2, tzinfo=dt.UTC),
    )
    session.add(surface_chunk)
    await session.flush()

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
    assert any(hit.chunk_id == surface_chunk.id for hit in windowed.hits)
    assert all(hit.period_start is not None for hit in windowed.hits)
    assert all(hit.period_start >= dt.datetime(2026, 2, 1, tzinfo=dt.UTC) for hit in windowed.hits)


async def test_explicit_windows_exclude_chunks_without_depth_or_period_metadata(corpus, session):
    program_doc = corpus["documents"]["program"]
    unscoped_depth = DocumentChunk(
        org_id=corpus["org"].id,
        document_id=program_doc.id,
        chunk_index=4,
        text="uniqueunscopeddepthtoken is present only in a depth-untagged chunk",
        kind="text",
        well_id=corpus["well"].id,
        wellbore_id=corpus["wellbore"].id,
        doc_type="program",
    )
    unscoped_period = DocumentChunk(
        org_id=corpus["org"].id,
        document_id=program_doc.id,
        chunk_index=5,
        text="uniqueunscopedperiodtoken is present only in an undated chunk",
        kind="text",
        well_id=corpus["well"].id,
        wellbore_id=corpus["wellbore"].id,
        doc_type="program",
    )
    session.add_all([unscoped_depth, unscoped_period])
    await session.flush()

    depth_result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(
            text="uniqueunscopeddepthtoken",
            mode=RetrieveMode.LEXICAL,
            well_id=corpus["well"].id,
            depth_from_si=100.0,
        ),
    )
    period_result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(
            text="uniqueunscopedperiodtoken",
            mode=RetrieveMode.LEXICAL,
            well_id=corpus["well"].id,
            period_from=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        ),
    )
    assert depth_result.hits == []
    assert period_result.hits == []


async def test_project_scope_sees_across_wells_in_the_project(corpus, session):
    result = await retrieve(
        session, corpus["org"].id, RetrievalQuery(text="mud weight", project_id=corpus["project"].id, mode="lexical")
    )
    assert len(result.hits) >= 2


async def test_neighbour_expansion_adds_adjacent_chunks_with_a_reason(corpus, session):
    query = RetrievalQuery(
        text="plastic viscosity",
        mode=RetrieveMode.LEXICAL,
        well_id=corpus["well"].id,
        limit=1,
    )
    result = await retrieve(session, corpus["org"].id, query)
    expanded = await expand_neighbours(
        session, result.hits, radius=1, org_id=corpus["org"].id, query=query
    )
    assert len(expanded) > len(result.hits)
    added = [hit for hit in expanded if hit.explain.get("reason", "").startswith("neighbour of")]
    assert added and added[0].score < result.hits[0].score  # a neighbour is never ranked above its hit


async def test_retrieve_applies_include_neighbours_and_reports_the_expansion(corpus, session):
    query = RetrievalQuery(
        text="plastic viscosity",
        mode=RetrieveMode.LEXICAL,
        well_id=corpus["well"].id,
        limit=1,
        include_neighbours=1,
    )

    result = await retrieve(session, corpus["org"].id, query)

    assert len(result.hits) > 1
    added = [hit for hit in result.hits if hit.explain.get("reason", "").startswith("neighbour of")]
    assert added
    assert all(hit.score < result.hits[0].score for hit in added)
    assert result.filters_applied["include_neighbours"] == 1
    assert any("adjacent chunk" in note for note in result.notes)


async def test_retrieval_rejects_a_chunk_whose_parent_document_is_another_organization(corpus, session):
    """Redundant tenant ids on a corrupt chunk row cannot override its parent document's tenant."""
    foreign_org = Organization(slug="rag-parent-foreign", name="Foreign Parent Org")
    session.add(foreign_org)
    await session.flush()
    artifact = (await session.execute(select(RawArtifact))).scalars().one()
    foreign_document = Document(
        org_id=foreign_org.id,
        raw_artifact_id=artifact.id,
        doc_type="ddr",
        title="foreign parent document",
        status="ingested",
    )
    session.add(foreign_document)
    await session.flush()
    hostile = DocumentChunk(
        org_id=corpus["org"].id,
        document_id=foreign_document.id,
        chunk_index=0,
        page_number=1,
        text="cross organization parent document secret",
        kind="text",
        well_id=corpus["well"].id,
        wellbore_id=corpus["wellbore"].id,
        section_id=corpus["intermediate"].id,
        doc_type="ddr",
    )
    session.add(hostile)
    await session.flush()

    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(
            text="cross organization parent document secret",
            mode="lexical",
            well_id=corpus["well"].id,
            limit=10,
        ),
    )
    assert all(hit.chunk_id != hostile.id for hit in result.hits)
    assert all("cross organization parent document secret" not in hit.text for hit in result.hits)


async def test_retrieval_rejects_a_chunk_whose_well_scope_conflicts_with_its_parent_document(
    corpus, session
):
    """A corrupt same-tenant child scope cannot refile another well's document into this well."""
    other_well = Well(
        org_id=corpus["org"].id,
        project_id=corpus["project"].id,
        name="RAG-OTHER",
    )
    session.add(other_well)
    await session.flush()
    artifact = (await session.execute(select(RawArtifact))).scalars().one()
    other_document = Document(
        org_id=corpus["org"].id,
        project_id=corpus["project"].id,
        well_id=other_well.id,
        raw_artifact_id=artifact.id,
        doc_type="ddr",
        title="other well report",
        status="ingested",
    )
    session.add(other_document)
    await session.flush()
    hostile = DocumentChunk(
        org_id=corpus["org"].id,
        document_id=other_document.id,
        chunk_index=0,
        page_number=1,
        text="scope-conflict private facts must not enter RAG-1",
        kind="text",
        well_id=corpus["well"].id,
        wellbore_id=corpus["wellbore"].id,
        section_id=corpus["intermediate"].id,
        doc_type="ddr",
    )
    session.add(hostile)
    await session.flush()

    result = await retrieve(
        session,
        corpus["org"].id,
        RetrievalQuery(
            text="scope-conflict private facts",
            mode="lexical",
            well_id=corpus["well"].id,
            limit=10,
        ),
    )
    assert all(hit.chunk_id != hostile.id for hit in result.hits)
    assert all("scope-conflict private facts" not in hit.text for hit in result.hits)


async def test_neighbour_expansion_does_not_cross_the_section_boundary(corpus, session):
    """Chunks sharing a document id but carrying a different section scope are not neighbours."""
    query = RetrievalQuery(
        text="plastic viscosity",
        mode=RetrieveMode.LEXICAL,
        well_id=corpus["well"].id,
        limit=1,
    )
    intermediate_doc = corpus["documents"]["intermediate"]
    other_section_chunk = DocumentChunk(
        org_id=corpus["org"].id,
        document_id=intermediate_doc.id,
        chunk_index=2,
        page_number=3,
        text="surface-section-only values must not join the 8-1/2 inch hit",
        kind="text",
        well_id=corpus["well"].id,
        wellbore_id=corpus["wellbore"].id,
        section_id=corpus["surface"].id,
        doc_type="ddr",
    )
    session.add(other_section_chunk)
    await session.flush()

    result = await retrieve(session, corpus["org"].id, query)
    expanded = await expand_neighbours(
        session, result.hits, radius=2, org_id=corpus["org"].id, query=query
    )
    assert all(hit.chunk_id != other_section_chunk.id for hit in expanded)
    assert all("surface-section-only" not in hit.text for hit in expanded)


async def test_neighbour_expansion_keeps_explicit_depth_filters(corpus, session):
    intermediate_doc = corpus["documents"]["intermediate"]
    outside_window = DocumentChunk(
        org_id=corpus["org"].id,
        document_id=intermediate_doc.id,
        chunk_index=2,
        page_number=3,
        text="must not appear: measurement at shallow depth",
        kind="text",
        well_id=corpus["well"].id,
        wellbore_id=corpus["wellbore"].id,
        section_id=corpus["intermediate"].id,
        doc_type="ddr",
        depth_from_si=0.0,
        depth_to_si=100.0,
    )
    session.add(outside_window)
    await session.flush()

    query = RetrievalQuery(
        text="plastic viscosity",
        mode="lexical",
        well_id=corpus["well"].id,
        depth_from_si=1000.0,
        depth_to_si=1200.0,
        limit=1,
    )
    result = await retrieve(session, corpus["org"].id, query)
    assert result.hits
    expanded = await expand_neighbours(
        session, result.hits, radius=2, org_id=corpus["org"].id, query=query
    )
    assert all(hit.chunk_id != outside_window.id for hit in expanded)
    assert all("must not appear" not in hit.text for hit in expanded)
    assert all(
        hit.depth_from_si is None or hit.depth_from_si <= query.depth_to_si
        for hit in expanded
    )
    assert all(
        hit.depth_to_si is None or hit.depth_to_si >= query.depth_from_si
        for hit in expanded
    )


async def test_neighbour_expansion_keeps_both_period_window_bounds(corpus, session):
    document = Document(
        org_id=corpus["org"].id,
        project_id=corpus["project"].id,
        well_id=corpus["well"].id,
        wellbore_id=corpus["wellbore"].id,
        section_id=corpus["intermediate"].id,
        doc_type="ddr",
        title="period-window neighbour test",
        status="ingested",
    )
    session.add(document)
    await session.flush()
    anchor = DocumentChunk(
        org_id=corpus["org"].id,
        document_id=document.id,
        chunk_index=0,
        page_number=1,
        text="neighbor-filter-anchor in a dated in-window chunk",
        kind="text",
        well_id=corpus["well"].id,
        wellbore_id=corpus["wellbore"].id,
        section_id=corpus["intermediate"].id,
        doc_type="ddr",
        period_start=dt.datetime(2026, 1, 15, tzinfo=dt.UTC),
    )
    before_window = DocumentChunk(
        org_id=corpus["org"].id,
        document_id=document.id,
        chunk_index=1,
        page_number=2,
        text="before-window private neighbour must stay out",
        kind="text",
        well_id=corpus["well"].id,
        wellbore_id=corpus["wellbore"].id,
        section_id=corpus["intermediate"].id,
        doc_type="ddr",
        period_start=dt.datetime(2025, 12, 31, tzinfo=dt.UTC),
    )
    after_window = DocumentChunk(
        org_id=corpus["org"].id,
        document_id=document.id,
        chunk_index=2,
        page_number=3,
        text="after-window private neighbour must stay out",
        kind="text",
        well_id=corpus["well"].id,
        wellbore_id=corpus["wellbore"].id,
        section_id=corpus["intermediate"].id,
        doc_type="ddr",
        period_start=dt.datetime(2026, 2, 2, tzinfo=dt.UTC),
    )
    session.add_all([anchor, before_window, after_window])
    await session.flush()

    query = RetrievalQuery(
        text="neighbor-filter-anchor",
        mode="lexical",
        well_id=corpus["well"].id,
        period_from=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        period_to=dt.datetime(2026, 2, 1, tzinfo=dt.UTC),
        limit=5,
        include_neighbours=2,
    )
    result = await retrieve(session, corpus["org"].id, query)
    assert [hit.chunk_id for hit in result.hits] == [anchor.id]

    expanded = await expand_neighbours(
        session, result.hits, radius=2, org_id=corpus["org"].id, query=query
    )
    returned_ids = {hit.chunk_id for hit in expanded}
    assert anchor.id in returned_ids
    assert before_window.id not in returned_ids
    assert after_window.id not in returned_ids
    assert all("private neighbour must stay out" not in hit.text for hit in expanded)


async def test_neighbour_expansion_does_not_cross_the_organization_boundary(corpus, session):
    """A corrupt cross-tenant chunk row cannot be disclosed by document-neighbour expansion."""
    query = RetrievalQuery(
        text="plastic viscosity",
        mode="lexical",
        well_id=corpus["well"].id,
        limit=1,
    )
    foreign_org = Organization(slug="rag-foreign", name="Foreign RAG Org")
    session.add(foreign_org)
    await session.flush()
    intermediate_doc = corpus["documents"]["intermediate"]
    hostile = DocumentChunk(
        org_id=foreign_org.id,
        document_id=intermediate_doc.id,
        chunk_index=2,
        page_number=3,
        text="foreign tenant secret neighbor must never be returned",
        kind="text",
        well_id=corpus["well"].id,
        wellbore_id=corpus["wellbore"].id,
        section_id=corpus["intermediate"].id,
        doc_type="ddr",
    )
    session.add(hostile)
    await session.flush()

    result = await retrieve(session, corpus["org"].id, query)
    assert result.hits
    expanded = await expand_neighbours(
        session, result.hits, radius=2, org_id=corpus["org"].id, query=query
    )
    assert all(hit.chunk_id != hostile.id for hit in expanded)
    assert all("foreign tenant secret" not in hit.text for hit in expanded)

    foreign_hit = replace(result.hits[0], org_id=foreign_org.id)
    assert await expand_neighbours(
        session, [foreign_hit], radius=0, org_id=corpus["org"].id, query=query
    ) == []


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
