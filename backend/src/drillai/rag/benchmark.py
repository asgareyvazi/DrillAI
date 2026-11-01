"""Deterministic, synthetic retrieval benchmark for the local RAG ranking contract.

This command evaluates the production retrieval service in a fresh SQLite database using a small,
versioned corpus whose expected relevant chunks are declared below. It measures lexical retrieval
only: Recall@k, Precision@k, MRR, scope leakage, and citation coverage. It makes no network calls,
uses no embedding or LLM provider, and is not evidence of production-corpus relevance or answer quality.

Run from ``backend/``::

    .venv/bin/python -m drillai.rag.benchmark
    .venv/bin/python -m drillai.rag.benchmark --output /tmp/rag-benchmark.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from drillai.db.base import Base
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
from drillai.rag.retriever import RetrievalQuery, RetrieveMode, retrieve

DATASET_VERSION = "drillai-rag-synthetic-v1"
TOP_K = 5

# Stable IDs let an evaluation report be compared across runs without retaining database-generated IDs.
_CORPUS = (
    {
        "id": "chk_benchmark_mud-properties",
        "document_id": "doc_benchmark_ddr-intermediate",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 1,
        "text": "Mud check: mud weight 12.4 ppg and plastic viscosity 22 cp in the 8-1/2 inch section.",
        "depth_from_si": 800.0,
        "depth_to_si": 1500.0,
    },
    {
        "id": "chk_benchmark_torque-drag",
        "document_id": "doc_benchmark_ddr-intermediate",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 2,
        "text": "Torque and drag stayed within the drilling program limits at 1300 m measured depth.",
        "depth_from_si": 800.0,
        "depth_to_si": 1500.0,
    },
    {
        "id": "chk_benchmark_surface-shoe",
        "document_id": "doc_benchmark_ddr-surface",
        "section": "surface",
        "doc_type": "ddr",
        "page_number": 3,
        "text": "Surface casing shoe fracture pressure was recorded at 18.2 MPa.",
        "depth_from_si": 0.0,
        "depth_to_si": 800.0,
    },
    {
        "id": "chk_benchmark_intermediate-shoe",
        "document_id": "doc_benchmark_ddr-intermediate",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 4,
        "text": "Intermediate casing shoe fracture pressure was recorded at 24.0 MPa.",
        "depth_from_si": 800.0,
        "depth_to_si": 1500.0,
    },
    {
        "id": "chk_benchmark_program-mud",
        "document_id": "doc_benchmark_program",
        "section": None,
        "doc_type": "program",
        "page_number": 5,
        "text": "The planned drilling program uses mud weight between 12.2 and 12.8 ppg.",
        "depth_from_si": None,
        "depth_to_si": None,
    },
)

_QUERIES = (
    {
        "id": "intermediate-mud-properties",
        "text": "mud check plastic viscosity 22 cp",
        "section": "intermediate",
        "relevant_chunk_ids": ["chk_benchmark_mud-properties"],
    },
    {
        "id": "surface-shoe-pressure",
        "text": "surface casing shoe fracture pressure",
        "section": "surface",
        "relevant_chunk_ids": ["chk_benchmark_surface-shoe"],
    },
    {
        "id": "intermediate-torque-drag",
        "text": "torque and drag at 1300 m measured depth",
        "section": "intermediate",
        "relevant_chunk_ids": ["chk_benchmark_torque-drag"],
    },
)


def _dataset_sha256() -> str:
    encoded = json.dumps(
        {"version": DATASET_VERSION, "top_k": TOP_K, "corpus": _CORPUS, "queries": _QUERIES},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def evaluate() -> dict[str, Any]:
    """Seed an isolated database and run the fixed retrieval cases through ``retrieve``."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            org = Organization(id="org_rag_benchmark", slug="rag-benchmark", name="RAG benchmark")
            project = Project(id="prj_rag_benchmark", org_id=org.id, name="Benchmark project")
            well = Well(id="wel_rag_benchmark", org_id=org.id, project_id=project.id, name="Benchmark well")
            wellbore = Wellbore(id="wbr_rag_benchmark", org_id=org.id, well_id=well.id, name="Main")
            surface = WellSection(
                id="sec_rag_surface",
                org_id=org.id,
                wellbore_id=wellbore.id,
                sequence=1,
                name="Surface",
                kind="surface",
                status="drilled",
            )
            intermediate = WellSection(
                id="sec_rag_intermediate",
                org_id=org.id,
                wellbore_id=wellbore.id,
                sequence=2,
                name="Intermediate",
                kind="intermediate",
                status="drilling",
            )
            artifact = RawArtifact(
                id="raw_rag_benchmark",
                org_id=org.id,
                sha256=hashlib.sha256(b"synthetic-rag-benchmark").hexdigest(),
                byte_size=24,
                blob_key="benchmarks/rag/synthetic",
                content_type="application/pdf",
                source_kind="upload",
                retention_class="standard",
                scan_status="clean",
            )
            session.add_all([org, project, well, wellbore, surface, intermediate, artifact])
            await session.flush()

            section_by_key = {"surface": surface, "intermediate": intermediate}
            documents: dict[str, Document] = {}
            for corpus_row in _CORPUS:
                document_id = corpus_row["document_id"]
                if document_id not in documents:
                    section = section_by_key.get(corpus_row["section"])
                    document = Document(
                        id=document_id,
                        org_id=org.id,
                        project_id=project.id,
                        well_id=well.id,
                        wellbore_id=wellbore.id,
                        section_id=section.id if section else None,
                        raw_artifact_id=artifact.id,
                        doc_type=corpus_row["doc_type"],
                        title=f"Synthetic benchmark {corpus_row['doc_type']}",
                        status="ingested",
                    )
                    documents[document_id] = document
                    session.add(document)
            await session.flush()

            for corpus_row in _CORPUS:
                section = section_by_key.get(corpus_row["section"])
                session.add(
                    DocumentChunk(
                        id=corpus_row["id"],
                        org_id=org.id,
                        document_id=corpus_row["document_id"],
                        chunk_index=corpus_row["page_number"] - 1,
                        page_number=corpus_row["page_number"],
                        text=corpus_row["text"],
                        kind="text",
                        well_id=well.id,
                        wellbore_id=wellbore.id,
                        section_id=section.id if section else None,
                        doc_type=corpus_row["doc_type"],
                        depth_from_si=corpus_row["depth_from_si"],
                        depth_to_si=corpus_row["depth_to_si"],
                        token_estimate=len(corpus_row["text"]) // 4,
                    )
                )
            await session.flush()

            measured_queries: list[dict[str, Any]] = []
            total_relevant = total_retrieved_relevant = 0
            reciprocal_ranks: list[float] = []
            precisions: list[float] = []
            scope_violations = citation_gaps = total_hits = 0
            for case in _QUERIES:
                section = section_by_key[case["section"]]
                result = await retrieve(
                    session,
                    org.id,
                    RetrievalQuery(
                        text=case["text"],
                        mode=RetrieveMode.LEXICAL,
                        well_id=well.id,
                        section_id=section.id,
                        limit=TOP_K,
                    ),
                )
                retrieved_ids = [hit.chunk_id for hit in result.hits]
                relevant_ids = set(case["relevant_chunk_ids"])
                relevant_ranks = [index for index, chunk_id in enumerate(retrieved_ids, start=1) if chunk_id in relevant_ids]
                relevant_count = len(relevant_ids)
                retrieved_relevant_count = len(relevant_ranks)
                reciprocal_rank = 1.0 / relevant_ranks[0] if relevant_ranks else 0.0
                precision = retrieved_relevant_count / TOP_K
                recall = retrieved_relevant_count / relevant_count if relevant_count else 0.0
                case_scope_violations = sum(
                    hit.well_id != well.id or hit.section_id != section.id for hit in result.hits
                )
                case_citation_gaps = sum(not hit.citations for hit in result.hits)
                measured_queries.append(
                    {
                        "id": case["id"],
                        "mode_used": result.mode_used,
                        "expected_relevant_chunk_ids": sorted(relevant_ids),
                        "retrieved_chunk_ids": retrieved_ids,
                        "relevant_retrieved": retrieved_relevant_count,
                        "recall_at_k": recall,
                        "precision_at_k": precision,
                        "reciprocal_rank": reciprocal_rank,
                        "scope_violations": case_scope_violations,
                        "citation_gaps": case_citation_gaps,
                    }
                )
                total_relevant += relevant_count
                total_retrieved_relevant += retrieved_relevant_count
                reciprocal_ranks.append(reciprocal_rank)
                precisions.append(precision)
                scope_violations += case_scope_violations
                citation_gaps += case_citation_gaps
                total_hits += len(result.hits)

            query_count = len(_QUERIES)
            return {
                "tool": "drillai-rag-benchmark-v1",
                "dataset_version": DATASET_VERSION,
                "dataset_sha256": _dataset_sha256(),
                "retrieval_mode_requested": RetrieveMode.LEXICAL.value,
                "top_k": TOP_K,
                "query_count": query_count,
                "corpus_chunk_count": len(_CORPUS),
                "metrics": {
                    "micro_recall_at_k": total_retrieved_relevant / total_relevant if total_relevant else 0.0,
                    "macro_recall_at_k": sum(item["recall_at_k"] for item in measured_queries) / query_count,
                    "macro_precision_at_k": sum(precisions) / query_count,
                    "mean_reciprocal_rank": sum(reciprocal_ranks) / query_count,
                    "scope_violations": scope_violations,
                    "citation_coverage": (total_hits - citation_gaps) / total_hits if total_hits else 1.0,
                },
                "queries": measured_queries,
                "limitations": [
                    "Synthetic, versioned smoke corpus; not representative production evidence.",
                    "Lexical retrieval only; embeddings, hybrid ranking and LLM answer quality are not evaluated.",
                    "Metrics do not establish semantic correctness, engineering approval, or field performance.",
                ],
            }
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate deterministic DrillAI lexical retrieval on a synthetic corpus.")
    parser.add_argument("--output", type=Path, help="write JSON to this path instead of stdout")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(evaluate())
        serialized = json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(serialized, encoding="utf-8")
        else:
            sys.stdout.write(serialized)
    except Exception as exc:
        sys.stderr.write(f"RAG benchmark failed: {type(exc).__name__}: {str(exc)[:500]}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
