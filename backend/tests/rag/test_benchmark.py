"""Measured software metrics for the synthetic multi-mode RAG development benchmark."""

from __future__ import annotations

import json

import pytest

from drillai.rag.benchmark import DATASET_VERSION, _dataset_sha256, evaluate


@pytest.mark.asyncio
async def test_versioned_synthetic_retrieval_benchmark_scales_modes_scope_and_unknowns(monkeypatch) -> None:
    from drillai.ai.providers import LLMCapability

    class ForbiddenNetworkEmbeddingProvider:
        name = "benchmark-must-not-call-embedding-provider"

        def supports(self, capability):
            return capability is LLMCapability.EMBEDDING

        async def embed(self, texts):
            raise AssertionError("offline benchmark must not request embeddings")

    class Registry:
        def providers(self):
            return {"sentinel": ForbiddenNetworkEmbeddingProvider()}

    monkeypatch.setattr("drillai.ai.providers.provider_registry", lambda: Registry())
    report = await evaluate()

    assert report["tool"] == "drillai-rag-benchmark-v4"
    assert report["dataset_classification"] == "synthetic"
    assert report["dataset_version"] == DATASET_VERSION == "drillai-rag-synthetic-v4"
    assert report["dataset_sha256"] == _dataset_sha256()
    assert len(report["dataset_sha256"]) == 64
    assert report["retrieval_mode_requested"] == "lexical"
    assert set(report["retrieval_modes_requested"]) == {"metadata", "lexical", "semantic", "hybrid"}
    assert report["query_count"] == 278
    assert report["distinct_query_text_count"] == 278
    assert report["distinct_query_text_count"] >= 250
    assert report["positive_query_count"] == 260
    assert report["no_relevant_query_count"] == 18
    assert report["corpus_chunk_count"] == 508
    assert report["corpus_chunk_count"] >= 500
    assert report["empty_result_cases"] == 18

    scope = report["scope_profile"]
    assert scope == {
        "retrieval_mode": "lexical",
        "well_scoped_queries": 278,
        "distinct_query_text_count": 278,
        "section_scoped_queries": 277,
        "depth_window_queries": 274,
        "period_window_queries": 275,
        "scope_violations": 0,
        "forbidden_chunk_retrievals": 0,
        "quarantined_distractor_chunks": 4,
        "cross_well_distractor_chunks": 4,
        "cross_tenant_distractor_chunks": 5,
        "corpus_chunks_by_scope": {"main": 499, "other_well": 4, "other_tenant": 5},
    }
    assert report["citation_profile"]["retrieval_mode"] == "lexical"
    assert report["citation_profile"]["retrieved_hits"] == 695
    assert report["citation_profile"]["citation_gaps"] == 0
    assert report["citation_profile"]["citation_coverage"] == 1.0
    assert report["citation_profile"]["source_locator_gaps"] == 0
    assert report["citation_profile"]["source_locator_coverage"] == 1.0

    profiles = report["mode_profiles"]
    assert set(profiles) == {"metadata", "lexical", "semantic", "hybrid"}
    lexical = profiles["lexical"]
    assert lexical["status"] == "measured"
    assert lexical["requested_query_count"] == lexical["measured_query_count"] == 278
    assert lexical["mode_used_counts"] == {"lexical": 278}
    metrics = lexical["metrics"]
    assert metrics["micro_recall_at_k"] == 1.0
    assert metrics["macro_recall_at_k_positive_queries"] == 1.0
    assert metrics["mean_reciprocal_rank_positive_queries"] == 1.0
    assert metrics["hit_rate_at_k_positive_queries"] == 1.0
    assert metrics["macro_precision_at_k_all_queries"] == pytest.approx(0.18705035971223094)
    assert metrics["macro_ndcg_at_k_all_queries"] == pytest.approx(0.935251798561151)
    assert metrics["no_relevant_false_positive_query_count"] == 0
    assert metrics["no_relevant_false_positive_rate"] == 0.0
    assert metrics["retrieved_hit_count"] == 695
    assert metrics["retrieved_nonrelevant_hit_count"] == 435
    assert metrics["scope_violations"] == 0
    assert metrics["forbidden_chunk_retrievals"] == 0
    assert metrics["citation_coverage"] == 1.0
    assert metrics["source_locator_coverage"] == 1.0

    metadata = profiles["metadata"]
    assert metadata["status"] == "measured"
    assert metadata["requested_query_count"] == metadata["measured_query_count"] == 278
    assert metadata["mode_used_counts"] == {"metadata": 278}
    assert metadata["metrics"]["mean_reciprocal_rank_positive_queries"] < metrics[
        "mean_reciprocal_rank_positive_queries"
    ]
    assert metadata["metrics"]["scope_violations"] == 0
    assert metadata["metrics"]["citation_coverage"] == 1.0

    semantic = profiles["semantic"]
    hybrid = profiles["hybrid"]
    for mode, profile in (("semantic", semantic), ("hybrid", hybrid)):
        assert profile["status"] == "not_evaluated"
        assert profile["requested_mode"] == mode
        assert profile["requested_query_count"] == 278
        assert profile["measured_query_count"] == 0
        assert profile["metrics"] is None
        assert profile["probe_mode_used"] == "lexical"
        fallback = " ".join(profile["fallback_notes"])
        assert any(reason in fallback for reason in (
            "no stored chunk embeddings",
            "no embedding provider is configured",
        ))
    assert report["embedding_evaluation"]["authentic_stored_embedding_count"] == 0
    assert report["embedding_evaluation"]["synthetic_embedding_count"] == 0
    assert report["embedding_evaluation"]["provider_embedding_calls"] == 0

    by_id = {case["id"]: case for case in report["queries"]}
    assert len(by_id) == 278
    assert len({case["query_text_sha256"] for case in report["queries"]}) == 278
    windowed = by_id["intermediate-mud-properties-windowed"]
    assert windowed["filters_applied"]["depth_from_si"] == 1000.0
    assert windowed["filters_applied"]["period_from"] == "2026-03-15T00:00:00+00:00"
    assert windowed["retrieved_chunk_ids"][0] == "chk_benchmark_mud-properties"
    assert windowed["forbidden_retrieved_chunk_ids"] == []
    assert windowed["source_locator_gaps"] == 0

    conflicts = by_id["intermediate-conflicting-mud-values"]
    assert conflicts["retrieved_chunk_ids"][0] == "chk_benchmark_mud-conflict"

    generated = [case for case in report["queries"] if case["id"].startswith("cp139-unique-record-")]
    assert len(generated) == 256
    assert all(case["retrieved_chunk_ids"][0] == case["expected_relevant_chunk_ids"][0] for case in generated)
    assert all(case["scope_violations"] == 0 for case in generated)
    assert all(case["forbidden_retrieved_chunk_ids"] == [] for case in generated)
    assert all(case["citation_gaps"] == 0 and case["source_locator_gaps"] == 0 for case in generated)

    strata = lexical["stratified_metrics"]
    assert {key: strata[key]["query_count"] for key in (
        "identifier_lookup", "record_phrase", "parameter_phrase", "canonical_value", "no_match"
    )} == {
        "identifier_lookup": 64,
        "record_phrase": 64,
        "parameter_phrase": 64,
        "canonical_value": 64,
        "no_match": 16,
    }
    no_match = [case for case in report["queries"] if case["id"].startswith("cp139-no-match-")]
    assert len(no_match) == 16
    assert all(case["retrieved_chunk_ids"] == [] for case in no_match)
    for case_id in ("unknown-depth-does-not-enter-window", "unknown-time-does-not-enter-period"):
        assert by_id[case_id]["retrieved_chunk_ids"] == []

    serialized = json.dumps(report, ensure_ascii=False)
    assert "Synthetic benchmark record" not in serialized
    assert "Mud check:" not in serialized
    assert any("semantic and hybrid modes" in item.lower() for item in report["limitations"])
    assert any("not independently adjudicated" in item for item in report["limitations"])
    assert any("created_at" in item for item in report["limitations"])

    repeated = await evaluate()
    assert repeated["dataset_sha256"] == report["dataset_sha256"]
    for mode in ("lexical", "metadata"):
        assert repeated["mode_profiles"][mode]["metrics"] == profiles[mode]["metrics"]
        assert repeated["mode_profiles"][mode]["stratified_metrics"] == profiles[mode]["stratified_metrics"]
        if mode == "lexical":
            assert repeated["queries"] == report["queries"]
        else:
            assert repeated["mode_profiles"][mode]["queries"] == profiles[mode]["queries"]
