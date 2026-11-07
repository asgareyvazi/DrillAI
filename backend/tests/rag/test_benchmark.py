"""Measured software metrics for the synthetic, lexical-only RAG smoke corpus."""

from __future__ import annotations

import json

import pytest

from drillai.rag.benchmark import DATASET_VERSION, evaluate


@pytest.mark.asyncio
async def test_versioned_synthetic_retrieval_benchmark_checks_scope_grounding_and_unknowns() -> None:
    report = await evaluate()

    assert report["dataset_classification"] == "synthetic"
    assert report["dataset_version"] == DATASET_VERSION == "drillai-rag-synthetic-v2"
    assert len(report["dataset_sha256"]) == 64
    assert report["retrieval_mode_requested"] == "lexical"
    assert report["query_count"] == 6
    assert report["positive_query_count"] == 4
    assert report["no_relevant_query_count"] == 2
    assert report["corpus_chunk_count"] == 13
    assert report["empty_result_cases"] == 2

    metrics = report["metrics"]
    assert metrics["micro_recall_at_k"] == 1.0
    assert metrics["macro_recall_at_k_positive_queries"] == 1.0
    assert metrics["mean_reciprocal_rank_positive_queries"] == 1.0
    assert metrics["scope_violations"] == 0
    assert metrics["forbidden_chunk_retrievals"] == 0
    assert metrics["citation_coverage"] == 1.0
    assert metrics["source_locator_coverage"] == 1.0

    by_id = {case["id"]: case for case in report["queries"]}
    windowed = by_id["intermediate-mud-properties-windowed"]
    assert windowed["filters_applied"]["depth_from_si"] == 1000.0
    assert windowed["filters_applied"]["period_from"] == "2026-03-15T00:00:00+00:00"
    assert windowed["retrieved_chunk_ids"][0] == "chk_benchmark_mud-properties"
    assert windowed["forbidden_retrieved_chunk_ids"] == []
    assert windowed["source_locator_gaps"] == 0

    conflicts = by_id["intermediate-conflicting-mud-values"]
    assert conflicts["retrieved_chunk_ids"][0] == "chk_benchmark_mud-conflict"
    assert "chk_benchmark_mud-properties" not in conflicts["must_not_retrieve_chunk_ids"]

    for case_id in ("unknown-depth-does-not-enter-window", "unknown-time-does-not-enter-period"):
        assert by_id[case_id]["retrieved_chunk_ids"] == []
        assert by_id[case_id]["relevant_retrieved"] == 0

    assert all(case["mode_used"] == "lexical" for case in report["queries"])
    assert all(case["scope_violations"] == 0 for case in report["queries"])
    assert all(case["forbidden_retrieved_chunk_ids"] == [] for case in report["queries"])
    assert all(case["citation_gaps"] == 0 for case in report["queries"])
    serialized = json.dumps(report, ensure_ascii=False)
    assert "Mud check:" not in serialized
    assert "not representative field or production evidence" in " ".join(report["limitations"])
    assert any("not independently adjudicated" in item for item in report["limitations"])
    assert any("created_at" in item for item in report["limitations"])
