"""Measured metrics for the versioned, offline lexical RAG smoke corpus."""

from __future__ import annotations

import pytest

from drillai.rag.benchmark import DATASET_VERSION, evaluate


@pytest.mark.asyncio
async def test_versioned_synthetic_retrieval_benchmark_reports_expected_metrics() -> None:
    report = await evaluate()

    assert report["dataset_version"] == DATASET_VERSION == "drillai-rag-synthetic-v1"
    assert len(report["dataset_sha256"]) == 64
    assert report["retrieval_mode_requested"] == "lexical"
    assert report["query_count"] == 3
    assert report["corpus_chunk_count"] == 5
    assert report["metrics"]["micro_recall_at_k"] == 1.0
    assert report["metrics"]["macro_recall_at_k"] == 1.0
    assert report["metrics"]["mean_reciprocal_rank"] == 1.0
    assert report["metrics"]["scope_violations"] == 0
    assert report["metrics"]["citation_coverage"] == 1.0
    assert all(case["mode_used"] == "lexical" for case in report["queries"])
    assert all(case["scope_violations"] == 0 for case in report["queries"])
    assert all(case["citation_gaps"] == 0 for case in report["queries"])
    assert any("not representative production evidence" in item for item in report["limitations"])
