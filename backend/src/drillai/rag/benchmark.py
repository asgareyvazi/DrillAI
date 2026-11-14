"""Deterministic, synthetic multi-mode retrieval benchmark for the local RAG ranking contract.

This command measures metadata and lexical retrieval separately in an isolated SQLite database and
records semantic/hybrid availability without conflating lexical fallback with vector quality. A
versioned fictional corpus supplies fixture relevance labels, near-miss and cross-scope distractors,
unknown depth/time metadata, quarantined sources, and explicit source locators. It reports top-k
ranking, no-relevant-query, scope, citation, locator, and per-query-stratum metrics.

It makes no network calls and creates no embeddings or LLM/provider responses. Semantic and hybrid
scores remain not evaluated without authentic stored vectors. Relevance labels are development
fixtures, not independent review or evidence of production-corpus, semantic, engineering, or answer
quality. An unknown depth or reporting period remains unknown; record creation time is never substituted
for a missing source period.

Run from ``backend/``::

    .venv/bin/python -m drillai.rag.benchmark
    .venv/bin/python -m drillai.rag.benchmark --output /tmp/rag-benchmark.json
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import math
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

DATASET_VERSION = "drillai-rag-synthetic-v4"
TOP_K = 5
SCALE_POSITIVE_QUERY_COUNT = 256
SCALE_NEAR_MISS_CHUNK_COUNT = 192
SCALE_NO_MATCH_QUERY_COUNT = 16
_MAIN_ORG_ID = "org_rag_benchmark"
_MAIN_PROJECT_ID = "prj_rag_benchmark"
_MAIN_WELL_ID = "wel_rag_benchmark"
_MAIN_WELLBORE_ID = "wbr_rag_benchmark"
_MAIN_SURFACE_SECTION_ID = "sec_rag_surface"
_MAIN_INTERMEDIATE_SECTION_ID = "sec_rag_intermediate"

# Every text and relevance label is synthetic and version-pinned by _dataset_sha256().
# Source locators are deliberately carried through DocumentChunk.attributes into retrieval hits.
_CORPUS = (
    {
        "id": "chk_benchmark_mud-properties",
        "document_id": "doc_benchmark_ddr-intermediate",
        "scope": "main",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 1,
        "text": "Mud check: mud weight 12.4 ppg and plastic viscosity 22 cp in the 8-1/2 inch section.",
        "depth_from_si": 800.0,
        "depth_to_si": 1500.0,
        "period_start": "2026-03-15T00:00:00+00:00",
        "period_end": "2026-03-16T00:00:00+00:00",
    },
    {
        "id": "chk_benchmark_torque-drag",
        "document_id": "doc_benchmark_ddr-intermediate",
        "scope": "main",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 2,
        "text": "Torque and drag stayed within the drilling program limits at 1300 m measured depth.",
        "depth_from_si": 800.0,
        "depth_to_si": 1500.0,
        "period_start": "2026-03-15T00:00:00+00:00",
        "period_end": "2026-03-16T00:00:00+00:00",
    },
    {
        "id": "chk_benchmark_surface-shoe",
        "document_id": "doc_benchmark_ddr-surface",
        "scope": "main",
        "section": "surface",
        "doc_type": "ddr",
        "page_number": 3,
        "text": "Surface casing shoe fracture pressure was recorded at 18.2 MPa.",
        "depth_from_si": 0.0,
        "depth_to_si": 800.0,
        "period_start": "2026-03-14T00:00:00+00:00",
        "period_end": "2026-03-15T00:00:00+00:00",
    },
    {
        "id": "chk_benchmark_intermediate-shoe",
        "document_id": "doc_benchmark_ddr-intermediate",
        "scope": "main",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 4,
        "text": "Intermediate casing shoe fracture pressure was recorded at 24.0 MPa.",
        "depth_from_si": 800.0,
        "depth_to_si": 1500.0,
        "period_start": "2026-03-14T00:00:00+00:00",
        "period_end": "2026-03-15T00:00:00+00:00",
    },
    {
        "id": "chk_benchmark_program-mud",
        "document_id": "doc_benchmark_program",
        "scope": "main",
        "section": None,
        "doc_type": "program",
        "page_number": 5,
        "text": "The planned drilling program uses mud weight between 12.2 and 12.8 ppg.",
        "depth_from_si": None,
        "depth_to_si": None,
        "period_start": None,
        "period_end": None,
    },
    {
        "id": "chk_benchmark_mud-conflict",
        "document_id": "doc_benchmark_ddr-intermediate",
        "scope": "main",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 6,
        "text": "Review note: two source values for mud weight, 12.4 ppg and 12.5 ppg, remain conflicting and unreconciled.",
        "depth_from_si": 800.0,
        "depth_to_si": 1500.0,
        "period_start": "2026-03-15T00:00:00+00:00",
        "period_end": "2026-03-16T00:00:00+00:00",
    },
    {
        "id": "chk_benchmark_mud-unknown-scope",
        "document_id": "doc_benchmark_ddr-intermediate",
        "scope": "main",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 7,
        "text": "An unlocated mud check repeats plastic viscosity 22 cp with mud weight 12.4 ppg; depth and reporting time are unknown.",
        "depth_from_si": None,
        "depth_to_si": None,
        "period_start": None,
        "period_end": None,
    },
    {
        "id": "chk_benchmark_mud-outside-depth",
        "document_id": "doc_benchmark_ddr-intermediate",
        "scope": "main",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 8,
        "text": "Mud check: mud weight 12.4 ppg and plastic viscosity 22 cp at 1900 m measured depth.",
        "depth_from_si": 1800.0,
        "depth_to_si": 2000.0,
        "period_start": "2026-03-15T00:00:00+00:00",
        "period_end": "2026-03-16T00:00:00+00:00",
    },
    {
        "id": "chk_benchmark_mud-outside-period",
        "document_id": "doc_benchmark_ddr-intermediate",
        "scope": "main",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 9,
        "text": "Mud check: mud weight 12.4 ppg and plastic viscosity 22 cp at 1150 m measured depth.",
        "depth_from_si": 1100.0,
        "depth_to_si": 1200.0,
        "period_start": "2026-03-17T00:00:00+00:00",
        "period_end": "2026-03-18T00:00:00+00:00",
    },
    {
        "id": "chk_benchmark_mud-quarantined",
        "document_id": "doc_benchmark_ddr-quarantined",
        "scope": "main",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 10,
        "text": "Mud check: mud weight 12.4 ppg and plastic viscosity 22 cp from a quarantined synthetic artifact.",
        "depth_from_si": 1100.0,
        "depth_to_si": 1200.0,
        "period_start": "2026-03-15T00:00:00+00:00",
        "period_end": "2026-03-16T00:00:00+00:00",
        "quarantined": True,
    },
    {
        "id": "chk_benchmark_mud-other-well",
        "document_id": "doc_benchmark_ddr-other-well",
        "scope": "other_well",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 1,
        "text": "Mud check: mud weight 12.4 ppg and plastic viscosity 22 cp in another synthetic well.",
        "depth_from_si": 800.0,
        "depth_to_si": 1500.0,
        "period_start": "2026-03-15T00:00:00+00:00",
        "period_end": "2026-03-16T00:00:00+00:00",
    },
    {
        "id": "chk_benchmark_mud-other-tenant",
        "document_id": "doc_benchmark_ddr-other-tenant",
        "scope": "other_tenant",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 1,
        "text": "Mud check: mud weight 12.4 ppg and plastic viscosity 22 cp in another synthetic tenant.",
        "depth_from_si": 800.0,
        "depth_to_si": 1500.0,
        "period_start": "2026-03-15T00:00:00+00:00",
        "period_end": "2026-03-16T00:00:00+00:00",
    },
    {
        "id": "chk_benchmark_surface-text-wrong-section",
        "document_id": "doc_benchmark_ddr-intermediate",
        "scope": "main",
        "section": "intermediate",
        "doc_type": "ddr",
        "page_number": 11,
        "text": "Surface casing shoe fracture pressure was recorded at 18.2 MPa in a different section.",
        "depth_from_si": 800.0,
        "depth_to_si": 1500.0,
        "period_start": "2026-03-14T00:00:00+00:00",
        "period_end": "2026-03-15T00:00:00+00:00",
    },
)

_QUERIES = (
    {
        "id": "intermediate-mud-properties-windowed",
        "text": "mud check plastic viscosity 22 cp",
        "section": "intermediate",
        "depth_from_si": 1000.0,
        "depth_to_si": 1400.0,
        "period_from": "2026-03-15T00:00:00+00:00",
        "period_to": "2026-03-16T00:00:00+00:00",
        "relevant_chunk_ids": ["chk_benchmark_mud-properties"],
        "must_not_retrieve_chunk_ids": [
            "chk_benchmark_mud-unknown-scope",
            "chk_benchmark_mud-outside-depth",
            "chk_benchmark_mud-outside-period",
            "chk_benchmark_mud-quarantined",
            "chk_benchmark_mud-other-well",
            "chk_benchmark_mud-other-tenant",
        ],
    },
    {
        "id": "intermediate-conflicting-mud-values",
        "text": "unresolved conflicting mud weight values 12.4 ppg 12.5 ppg",
        "section": "intermediate",
        "period_from": "2026-03-15T00:00:00+00:00",
        "period_to": "2026-03-16T00:00:00+00:00",
        "relevant_chunk_ids": ["chk_benchmark_mud-conflict"],
        "must_not_retrieve_chunk_ids": ["chk_benchmark_mud-quarantined", "chk_benchmark_mud-other-well", "chk_benchmark_mud-other-tenant"],
    },
    {
        "id": "surface-shoe-pressure",
        "text": "surface casing shoe fracture pressure",
        "section": "surface",
        "relevant_chunk_ids": ["chk_benchmark_surface-shoe"],
        "must_not_retrieve_chunk_ids": [
            "chk_benchmark_intermediate-shoe",
            "chk_benchmark_surface-text-wrong-section",
            "chk_benchmark_mud-other-well",
            "chk_benchmark_mud-other-tenant",
        ],
    },
    {
        "id": "planned-program-mud-window",
        "text": "planned drilling program mud weight window 12.2 12.8 ppg",
        "doc_type": "program",
        "relevant_chunk_ids": ["chk_benchmark_program-mud"],
        "must_not_retrieve_chunk_ids": ["chk_benchmark_mud-properties", "chk_benchmark_mud-conflict"],
    },
    {
        "id": "unknown-depth-does-not-enter-window",
        "text": "mud check plastic viscosity 22 cp inside an explicit depth window",
        "section": "intermediate",
        "depth_from_si": 2200.0,
        "depth_to_si": 2300.0,
        "relevant_chunk_ids": [],
        "must_not_retrieve_chunk_ids": [
            "chk_benchmark_mud-unknown-scope",
            "chk_benchmark_mud-properties",
            "chk_benchmark_mud-outside-depth",
            "chk_benchmark_mud-outside-period",
        ],
    },
    {
        "id": "unknown-time-does-not-enter-period",
        "text": "mud check plastic viscosity 22 cp inside an explicit reporting period",
        "section": "intermediate",
        "period_from": "2026-03-18T00:00:01+00:00",
        "period_to": "2026-03-19T00:00:00+00:00",
        "relevant_chunk_ids": [],
        "must_not_retrieve_chunk_ids": [
            "chk_benchmark_mud-unknown-scope",
            "chk_benchmark_mud-properties",
            "chk_benchmark_mud-outside-depth",
            "chk_benchmark_mud-outside-period",
        ],
    },
)


def _build_cp139_scale_fixtures() -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Build 508 versioned synthetic chunks and 278 distinct retrieval questions."""
    rows: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    page_by_document: dict[str, int] = {}
    main_document_id = "doc_cp139_rag_generated"

    def add_row(
        *,
        fixture_id: str,
        document_id: str,
        scope: str,
        text: str,
        depth_from_si: float | None,
        depth_to_si: float | None,
        period_start: str | None,
        period_end: str | None,
        quarantined: bool = False,
    ) -> None:
        page_by_document[document_id] = page_by_document.get(document_id, 0) + 1
        rows.append(
            {
                "id": fixture_id,
                "document_id": document_id,
                "scope": scope,
                "section": "intermediate",
                "doc_type": "benchmark",
                "page_number": page_by_document[document_id],
                "text": text,
                "depth_from_si": depth_from_si,
                "depth_to_si": depth_to_si,
                "period_start": period_start,
                "period_end": period_end,
                **({"quarantined": True} if quarantined else {}),
            }
        )

    query_phrases = (
        ("identifier_lookup", "CP139-ITEM-{sequence} parameter-{sequence}"),
        ("record_phrase", "synthetic drilling record CP139-ITEM-{sequence} parameter-{sequence}"),
        ("parameter_phrase", "locate parameter-{sequence} on record CP139-ITEM-{sequence}"),
        ("canonical_value", "canonical-SI value for CP139-ITEM-{sequence} parameter-{sequence}"),
    )
    for index in range(SCALE_POSITIVE_QUERY_COUNT):
        sequence = index + 1
        sequence_text = f"{sequence:03d}"
        sample_day = dt.date(2026, 4, 1) + dt.timedelta(days=index)
        next_day = sample_day + dt.timedelta(days=1)
        period_start = dt.datetime.combine(sample_day, dt.time.min, tzinfo=dt.UTC).isoformat()
        period_end = dt.datetime.combine(next_day, dt.time.min, tzinfo=dt.UTC).isoformat()
        query_period_end = (dt.datetime.combine(next_day, dt.time.min, tzinfo=dt.UTC) - dt.timedelta(microseconds=1)).isoformat()
        depth_start = 800.0 + (index % 20) * 50.0
        depth_end = depth_start + 100.0
        fixture_id = f"chk_cp139_item_{sequence_text}"
        add_row(
            fixture_id=fixture_id,
            document_id=main_document_id,
            scope="main",
            text=(
                f"Synthetic benchmark record CP139-ITEM-{sequence_text} parameter-{sequence_text} "
                f"value {100 + index}.25 canonical-SI; deterministic development-only fixture."
            ),
            depth_from_si=depth_start,
            depth_to_si=depth_end,
            period_start=period_start,
            period_end=period_end,
        )
        next_sequence = sequence % SCALE_POSITIVE_QUERY_COUNT + 1
        stratum, query_template = query_phrases[index % len(query_phrases)]
        queries.append(
            {
                "id": f"cp139-unique-record-{sequence_text}",
                "stratum": stratum,
                "text": query_template.format(sequence=sequence_text),
                "section": "intermediate",
                "doc_type": "benchmark",
                "depth_from_si": depth_start + 10.0,
                "depth_to_si": depth_end - 10.0,
                "period_from": period_start,
                "period_to": query_period_end,
                "relevant_chunk_ids": [fixture_id],
                "must_not_retrieve_chunk_ids": [
                    f"chk_cp139_item_{next_sequence:03d}",
                    "chk_cp139_dist_quarantine_001",
                    "chk_cp139_dist_other_well_001",
                    "chk_cp139_dist_other_tenant_001",
                ],
            }
        )

    for index in range(SCALE_NO_MATCH_QUERY_COUNT):
        sequence = index + 1
        sequence_text = f"{sequence:03d}"
        queries.append(
            {
                "id": f"cp139-no-match-{sequence_text}",
                "stratum": "no_match",
                "text": f"nohitmarker-139-{sequence_text} qzv-miss-{sequence_text}",
                "section": "intermediate",
                "doc_type": "benchmark",
                "depth_from_si": 800.0 + (index % 20) * 50.0,
                "depth_to_si": 900.0 + (index % 20) * 50.0,
                "period_from": "2026-04-01T00:00:00+00:00",
                "period_to": "2026-04-02T00:00:00+00:00",
                "relevant_chunk_ids": [],
                "must_not_retrieve_chunk_ids": [],
            }
        )

    # Hard negatives share a parameter identifier and well/depth/time window with one target, but
    # not that target's unique record identifier. They test ranking rather than access-control filters.
    for index in range(SCALE_NEAR_MISS_CHUNK_COUNT):
        target_index = index % SCALE_POSITIVE_QUERY_COUNT
        target_sequence = target_index + 1
        sequence_text = f"{index + 1:03d}"
        sample_day = dt.date(2026, 4, 1) + dt.timedelta(days=target_index)
        next_day = sample_day + dt.timedelta(days=1)
        depth_start = 800.0 + (target_index % 20) * 50.0
        add_row(
            fixture_id=f"chk_cp139_near_miss_{sequence_text}",
            document_id=main_document_id,
            scope="main",
            text=(
                f"Synthetic benchmark near-match CP139-DECOY-{sequence_text} parameter-{target_sequence:03d} "
                "context-only placeholder; this is not the requested CP139 item."
            ),
            depth_from_si=depth_start,
            depth_to_si=depth_start + 100.0,
            period_start=dt.datetime.combine(sample_day, dt.time.min, tzinfo=dt.UTC).isoformat(),
            period_end=dt.datetime.combine(next_day, dt.time.min, tzinfo=dt.UTC).isoformat(),
        )

    # These 37 main-scope chunks challenge explicit depth/time filters and unknown-metadata handling.
    for index in range(20):
        sequence = index + 1
        day = dt.date(2026, 4, 1) + dt.timedelta(days=index)
        start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC).isoformat()
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC).isoformat()
        add_row(
            fixture_id=f"chk_cp139_dist_depth_{sequence:03d}",
            document_id=main_document_id,
            scope="main",
            text=f"Synthetic depth distractor CP139-DEPTH-DISTRACTOR-{sequence:03d}; not a target record.",
            depth_from_si=30_000.0 + index * 100.0,
            depth_to_si=30_050.0 + index * 100.0,
            period_start=start,
            period_end=end,
        )
    for index in range(10):
        sequence = index + 1
        add_row(
            fixture_id=f"chk_cp139_dist_time_{sequence:03d}",
            document_id=main_document_id,
            scope="main",
            text=f"Synthetic time distractor CP139-TIME-DISTRACTOR-{sequence:03d}; not a target record.",
            depth_from_si=800.0 + index * 50.0,
            depth_to_si=900.0 + index * 50.0,
            period_start=f"2099-01-{sequence:02d}T00:00:00+00:00",
            period_end=f"2099-01-{sequence:02d}T23:59:59+00:00",
        )
    for index in range(7):
        sequence = index + 1
        add_row(
            fixture_id=f"chk_cp139_dist_unknown_{sequence:03d}",
            document_id=main_document_id,
            scope="main",
            text=f"Synthetic unlocated distractor CP139-UNKNOWN-DISTRACTOR-{sequence:03d}; scope unknown.",
            depth_from_si=None,
            depth_to_si=None,
            period_start=None,
            period_end=None,
        )

    # A few deliberately in-window negatives verify quarantine and cross-well/tenant isolation.
    for index in range(3):
        sequence = index + 1
        day = dt.date(2026, 4, 1) + dt.timedelta(days=index)
        start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC).isoformat()
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC).isoformat()
        add_row(
            fixture_id=f"chk_cp139_dist_quarantine_{sequence:03d}",
            document_id="doc_cp139_rag_quarantine",
            scope="main",
            text=f"Synthetic quarantined distractor CP139-QUARANTINE-{sequence:03d}; excluded by trust state.",
            depth_from_si=800.0 + index * 50.0,
            depth_to_si=900.0 + index * 50.0,
            period_start=start,
            period_end=end,
            quarantined=True,
        )
    for index in range(3):
        sequence = index + 1
        day = dt.date(2026, 4, 1) + dt.timedelta(days=index)
        start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC).isoformat()
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC).isoformat()
        add_row(
            fixture_id=f"chk_cp139_dist_other_well_{sequence:03d}",
            document_id="doc_cp139_rag_other_well",
            scope="other_well",
            text=f"Synthetic other-well distractor CP139-WELL-DISTRACTOR-{sequence:03d}.",
            depth_from_si=800.0 + index * 50.0,
            depth_to_si=900.0 + index * 50.0,
            period_start=start,
            period_end=end,
        )
    for index in range(4):
        sequence = index + 1
        day = dt.date(2026, 4, 1) + dt.timedelta(days=index)
        start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC).isoformat()
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC).isoformat()
        add_row(
            fixture_id=f"chk_cp139_dist_other_tenant_{sequence:03d}",
            document_id="doc_cp139_rag_other_tenant",
            scope="other_tenant",
            text=f"Synthetic other-tenant distractor CP139-TENANT-DISTRACTOR-{sequence:03d}.",
            depth_from_si=800.0 + index * 50.0,
            depth_to_si=900.0 + index * 50.0,
            period_start=start,
            period_end=end,
        )
    return tuple(rows), tuple(queries)


_CP139_CORPUS, _CP139_QUERIES = _build_cp139_scale_fixtures()
_CORPUS = _CORPUS + _CP139_CORPUS
_QUERIES = _QUERIES + _CP139_QUERIES


def _parse_utc(value: str | None) -> dt.datetime | None:
    if value is None:
        return None
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("synthetic period values must be explicitly timezone-aware")
    return parsed


def _dataset_sha256() -> str:
    encoded = json.dumps(
        {"version": DATASET_VERSION, "top_k": TOP_K, "corpus": _CORPUS, "queries": _QUERIES},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _overlaps_depth(hit, query: dict[str, Any]) -> bool:
    if query.get("depth_from_si") is None and query.get("depth_to_si") is None:
        return True
    start = hit.depth_from_si if hit.depth_from_si is not None else hit.depth_to_si
    end = hit.depth_to_si if hit.depth_to_si is not None else hit.depth_from_si
    if start is None or end is None:
        return False
    return not (
        (query.get("depth_to_si") is not None and start > query["depth_to_si"])
        or (query.get("depth_from_si") is not None and end < query["depth_from_si"])
    )


def _overlaps_period(hit, query: dict[str, Any]) -> bool:
    if query.get("period_from") is None and query.get("period_to") is None:
        return True
    start = hit.period_start if hit.period_start is not None else hit.period_end
    end = hit.period_end if hit.period_end is not None else hit.period_start
    if start is None or end is None:
        return False
    period_from = _parse_utc(query.get("period_from"))
    period_to = _parse_utc(query.get("period_to"))
    return not ((period_to is not None and start > period_to) or (period_from is not None and end < period_from))


async def _measure_query_mode(
    session,
    *,
    org_id: str,
    well_id: str,
    wellbore_id: str,
    sections: dict[str, WellSection],
    mode: RetrieveMode,
) -> list[dict[str, Any]]:
    """Measure one ranking mode over the fixed query set without mixing its results with another mode."""
    measured: list[dict[str, Any]] = []
    for case in _QUERIES:
        section = sections.get(case.get("section"))
        result = await retrieve(
            session,
            org_id,
            RetrievalQuery(
                text=case["text"],
                mode=mode,
                well_id=well_id,
                wellbore_id=wellbore_id,
                section_id=section.id if section else None,
                doc_type=case.get("doc_type"),
                depth_from_si=case.get("depth_from_si"),
                depth_to_si=case.get("depth_to_si"),
                period_from=_parse_utc(case.get("period_from")),
                period_to=_parse_utc(case.get("period_to")),
                limit=TOP_K,
            ),
        )
        retrieved_ids = [hit.chunk_id for hit in result.hits]
        relevant_ids = set(case["relevant_chunk_ids"])
        forbidden_ids = set(case.get("must_not_retrieve_chunk_ids", []))
        relevant_ranks = [
            rank for rank, chunk_id in enumerate(retrieved_ids, start=1) if chunk_id in relevant_ids
        ]
        recall = len(relevant_ranks) / len(relevant_ids) if relevant_ids else 0.0
        precision = len(relevant_ranks) / TOP_K
        scope_violations = sum(
            hit.org_id != org_id
            or hit.well_id != well_id
            or hit.wellbore_id != wellbore_id
            or (section is not None and hit.section_id != section.id)
            or (case.get("doc_type") is not None and hit.doc_type != case["doc_type"])
            or not _overlaps_depth(hit, case)
            or not _overlaps_period(hit, case)
            for hit in result.hits
        )
        citation_gaps = sum(not hit.citations for hit in result.hits)
        locator_gaps = sum(
            not isinstance(hit.source_locator, dict)
            or hit.source_locator.get("kind") != "synthetic_fixture"
            or hit.source_locator.get("fixture_id") != hit.chunk_id
            or hit.source_locator.get("page_number") != hit.page_number
            for hit in result.hits
        )
        measured.append(
            {
                "id": case["id"],
                "stratum": case.get("stratum", "baseline"),
                "query_text_sha256": hashlib.sha256(case["text"].encode("utf-8")).hexdigest(),
                "mode_used": result.mode_used,
                "filters_applied": {
                    key: value.isoformat() if isinstance(value, dt.datetime) else value
                    for key, value in result.filters_applied.items()
                },
                "expected_relevant_chunk_ids": sorted(relevant_ids),
                "retrieved_chunk_ids": retrieved_ids,
                "must_not_retrieve_chunk_ids": sorted(forbidden_ids),
                "forbidden_retrieved_chunk_ids": sorted(forbidden_ids.intersection(retrieved_ids)),
                "relevant_retrieved": len(relevant_ranks),
                "relevant_ranks": relevant_ranks,
                "recall_at_k": recall,
                "precision_at_k": precision,
                "reciprocal_rank": 1.0 / relevant_ranks[0] if relevant_ranks else 0.0,
                "scope_violations": scope_violations,
                "citation_gaps": citation_gaps,
                "source_locator_gaps": locator_gaps,
            }
        )
    return measured


def _retrieval_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate standard top-k ranking, abstention, provenance, and scope measurements."""
    positive = [row for row in rows if row["expected_relevant_chunk_ids"]]
    no_relevant = [row for row in rows if not row["expected_relevant_chunk_ids"]]
    relevant_total = sum(len(row["expected_relevant_chunk_ids"]) for row in positive)
    retrieved_relevant_total = sum(row["relevant_retrieved"] for row in rows)
    hit_count = sum(len(row["retrieved_chunk_ids"]) for row in rows)
    citation_gaps = sum(row["citation_gaps"] for row in rows)
    locator_gaps = sum(row["source_locator_gaps"] for row in rows)
    false_positive_queries = sum(bool(row["retrieved_chunk_ids"]) for row in no_relevant)
    ndcg_values = []
    for row in rows:
        relevant = set(row["expected_relevant_chunk_ids"])
        ranks = [rank for rank, chunk_id in enumerate(row["retrieved_chunk_ids"], start=1) if chunk_id in relevant]
        dcg = math.fsum(1.0 / math.log2(rank + 1) for rank in ranks)
        ideal_count = min(len(relevant), TOP_K)
        ideal_dcg = math.fsum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
        ndcg_values.append(dcg / ideal_dcg if ideal_dcg else 0.0)
    return {
        "micro_recall_at_k": retrieved_relevant_total / relevant_total if relevant_total else 0.0,
        "macro_recall_at_k_positive_queries": (
            sum(row["recall_at_k"] for row in positive) / len(positive) if positive else 0.0
        ),
        "macro_precision_at_k_all_queries": (
            sum(row["precision_at_k"] for row in rows) / len(rows) if rows else 0.0
        ),
        "mean_reciprocal_rank_positive_queries": (
            sum(row["reciprocal_rank"] for row in positive) / len(positive) if positive else 0.0
        ),
        "hit_rate_at_k_positive_queries": (
            sum(bool(row["relevant_retrieved"]) for row in positive) / len(positive) if positive else 0.0
        ),
        "macro_ndcg_at_k_all_queries": sum(ndcg_values) / len(ndcg_values) if ndcg_values else 0.0,
        "positive_query_count": len(positive),
        "no_relevant_query_count": len(no_relevant),
        "empty_result_count": sum(not row["retrieved_chunk_ids"] for row in rows),
        "no_relevant_false_positive_query_count": false_positive_queries,
        "no_relevant_false_positive_rate": (
            false_positive_queries / len(no_relevant) if no_relevant else 0.0
        ),
        "retrieved_nonrelevant_hit_count": sum(
            len(row["retrieved_chunk_ids"]) - row["relevant_retrieved"] for row in rows
        ),
        "retrieved_hit_count": hit_count,
        "scope_violations": sum(row["scope_violations"] for row in rows),
        "forbidden_chunk_retrievals": sum(len(row["forbidden_retrieved_chunk_ids"]) for row in rows),
        "citation_gaps": citation_gaps,
        "citation_coverage": (hit_count - citation_gaps) / hit_count if hit_count else 1.0,
        "source_locator_gaps": locator_gaps,
        "source_locator_coverage": (hit_count - locator_gaps) / hit_count if hit_count else 1.0,
    }


def _stratified_metrics(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    strata: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        strata.setdefault(row["stratum"], []).append(row)
    return {
        stratum: {"query_count": len(cases), **_retrieval_metrics(cases)}
        for stratum, cases in sorted(strata.items())
    }


async def evaluate() -> dict[str, Any]:
    """Seed an isolated database and run fixed synthetic cases through ``retrieve``."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory() as session:
            main_org = Organization(id=_MAIN_ORG_ID, slug="rag-benchmark", name="Synthetic RAG benchmark")
            main_project = Project(id=_MAIN_PROJECT_ID, org_id=main_org.id, name="Synthetic benchmark project")
            main_well = Well(id=_MAIN_WELL_ID, org_id=main_org.id, project_id=main_project.id, name="Synthetic well")
            main_wellbore = Wellbore(
                id=_MAIN_WELLBORE_ID,
                org_id=main_org.id,
                well_id=main_well.id,
                name="Synthetic main bore",
            )
            main_surface = WellSection(
                id=_MAIN_SURFACE_SECTION_ID,
                org_id=main_org.id,
                wellbore_id=main_wellbore.id,
                sequence=1,
                name="Synthetic surface section",
                kind="surface",
                status="drilled",
            )
            main_intermediate = WellSection(
                id=_MAIN_INTERMEDIATE_SECTION_ID,
                org_id=main_org.id,
                wellbore_id=main_wellbore.id,
                sequence=2,
                name="Synthetic intermediate section",
                kind="intermediate",
                status="drilling",
            )

            other_well = Well(id="wel_rag_other", org_id=main_org.id, project_id=main_project.id, name="Other synthetic well")
            other_wellbore = Wellbore(
                id="wbr_rag_other", org_id=main_org.id, well_id=other_well.id, name="Other synthetic bore"
            )
            other_intermediate = WellSection(
                id="sec_rag_other_intermediate",
                org_id=main_org.id,
                wellbore_id=other_wellbore.id,
                sequence=1,
                name="Other synthetic intermediate section",
                kind="intermediate",
                status="drilling",
            )

            other_org = Organization(id="org_rag_other", slug="rag-other-benchmark", name="Other synthetic tenant")
            other_project = Project(id="prj_rag_other", org_id=other_org.id, name="Other synthetic project")
            other_tenant_well = Well(
                id="wel_rag_other_tenant", org_id=other_org.id, project_id=other_project.id, name="Other tenant synthetic well"
            )
            other_tenant_wellbore = Wellbore(
                id="wbr_rag_other_tenant",
                org_id=other_org.id,
                well_id=other_tenant_well.id,
                name="Other tenant synthetic bore",
            )
            other_tenant_intermediate = WellSection(
                id="sec_rag_other_tenant_intermediate",
                org_id=other_org.id,
                wellbore_id=other_tenant_wellbore.id,
                sequence=1,
                name="Other tenant synthetic section",
                kind="intermediate",
                status="drilling",
            )

            main_scope = {
                "org": main_org,
                "project": main_project,
                "well": main_well,
                "wellbore": main_wellbore,
                "sections": {"surface": main_surface, "intermediate": main_intermediate},
                "clean_artifact_id": "raw_rag_benchmark",
            }
            other_well_scope = {
                "org": main_org,
                "project": main_project,
                "well": other_well,
                "wellbore": other_wellbore,
                "sections": {"intermediate": other_intermediate},
                "clean_artifact_id": "raw_rag_benchmark",
            }
            other_tenant_scope = {
                "org": other_org,
                "project": other_project,
                "well": other_tenant_well,
                "wellbore": other_tenant_wellbore,
                "sections": {"intermediate": other_tenant_intermediate},
                "clean_artifact_id": "raw_rag_other_tenant",
            }
            scope_by_key = {
                "main": main_scope,
                "other_well": other_well_scope,
                "other_tenant": other_tenant_scope,
            }

            clean_bytes = b"fictional synthetic retrieval fixture"
            quarantine_bytes = b"fictional quarantined retrieval fixture"
            other_tenant_bytes = b"fictional other-tenant retrieval fixture"
            artifacts = [
                RawArtifact(
                    id="raw_rag_benchmark",
                    org_id=main_org.id,
                    sha256=hashlib.sha256(clean_bytes).hexdigest(),
                    byte_size=len(clean_bytes),
                    blob_key="benchmarks/rag/synthetic-clean",
                    content_type="application/pdf",
                    source_kind="upload",
                    retention_class="standard",
                    scan_status="clean",
                    is_quarantined=False,
                ),
                RawArtifact(
                    id="raw_rag_benchmark_quarantined",
                    org_id=main_org.id,
                    sha256=hashlib.sha256(quarantine_bytes).hexdigest(),
                    byte_size=len(quarantine_bytes),
                    blob_key="benchmarks/rag/synthetic-quarantined",
                    content_type="application/pdf",
                    source_kind="upload",
                    retention_class="standard",
                    scan_status="quarantined",
                    is_quarantined=True,
                ),
                RawArtifact(
                    id="raw_rag_other_tenant",
                    org_id=other_org.id,
                    sha256=hashlib.sha256(other_tenant_bytes).hexdigest(),
                    byte_size=len(other_tenant_bytes),
                    blob_key="benchmarks/rag/other-tenant-synthetic",
                    content_type="application/pdf",
                    source_kind="upload",
                    retention_class="standard",
                    scan_status="clean",
                    is_quarantined=False,
                ),
            ]
            main_scope["quarantined_artifact_id"] = "raw_rag_benchmark_quarantined"
            session.add_all(
                [
                    main_org,
                    main_project,
                    main_well,
                    main_wellbore,
                    main_surface,
                    main_intermediate,
                    other_well,
                    other_wellbore,
                    other_intermediate,
                    other_org,
                    other_project,
                    other_tenant_well,
                    other_tenant_wellbore,
                    other_tenant_intermediate,
                    *artifacts,
                ]
            )
            await session.flush()

            documents: dict[str, Document] = {}
            for corpus_row in _CORPUS:
                scope = scope_by_key[corpus_row["scope"]]
                section = scope["sections"].get(corpus_row["section"])
                document_id = corpus_row["document_id"]
                if document_id in documents:
                    continue
                artifact_id = (
                    scope["quarantined_artifact_id"]
                    if corpus_row.get("quarantined")
                    else scope["clean_artifact_id"]
                )
                document = Document(
                    id=document_id,
                    org_id=scope["org"].id,
                    project_id=scope["project"].id,
                    well_id=scope["well"].id,
                    wellbore_id=scope["wellbore"].id,
                    section_id=section.id if section else None,
                    raw_artifact_id=artifact_id,
                    doc_type=corpus_row["doc_type"],
                    title=f"Synthetic benchmark {corpus_row['doc_type']}",
                    status="ingested",
                )
                documents[document_id] = document
                session.add(document)
            await session.flush()

            section_by_scope: dict[str, dict[str, WellSection]] = {
                scope_key: scope["sections"] for scope_key, scope in scope_by_key.items()
            }
            for corpus_row in _CORPUS:
                scope = scope_by_key[corpus_row["scope"]]
                section = section_by_scope[corpus_row["scope"]].get(corpus_row["section"])
                source_locator = {
                    "kind": "synthetic_fixture",
                    "fixture_id": corpus_row["id"],
                    "page_number": corpus_row["page_number"],
                }
                session.add(
                    DocumentChunk(
                        id=corpus_row["id"],
                        org_id=scope["org"].id,
                        document_id=corpus_row["document_id"],
                        chunk_index=corpus_row["page_number"] - 1,
                        page_number=corpus_row["page_number"],
                        text=corpus_row["text"],
                        kind="text",
                        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
                        + dt.timedelta(seconds=corpus_row["page_number"]),
                        well_id=scope["well"].id,
                        wellbore_id=scope["wellbore"].id,
                        section_id=section.id if section else None,
                        doc_type=corpus_row["doc_type"],
                        depth_from_si=corpus_row["depth_from_si"],
                        depth_to_si=corpus_row["depth_to_si"],
                        period_start=_parse_utc(corpus_row["period_start"]),
                        period_end=_parse_utc(corpus_row["period_end"]),
                        token_estimate=len(corpus_row["text"]) // 4,
                        attributes={"source_locator": source_locator},
                    )
                )
            await session.flush()

            query_count = len(_QUERIES)
            positive_queries = sum(bool(case["relevant_chunk_ids"]) for case in _QUERIES)
            distinct_query_text_count = len({case["text"] for case in _QUERIES})
            measured_queries = await _measure_query_mode(
                session,
                org_id=main_org.id,
                well_id=main_well.id,
                wellbore_id=main_wellbore.id,
                sections=main_scope["sections"],
                mode=RetrieveMode.LEXICAL,
            )
            metadata_queries = await _measure_query_mode(
                session,
                org_id=main_org.id,
                well_id=main_well.id,
                wellbore_id=main_wellbore.id,
                sections=main_scope["sections"],
                mode=RetrieveMode.METADATA,
            )
            metrics = _retrieval_metrics(measured_queries)
            metadata_metrics = _retrieval_metrics(metadata_queries)

            async def optional_mode_profile(mode: RetrieveMode) -> dict[str, Any]:
                """Probe vector modes without fabricating/storing vectors or making provider calls."""
                case = next(item for item in _QUERIES if item["relevant_chunk_ids"])
                section = main_scope["sections"].get(case.get("section"))
                result = await retrieve(
                    session,
                    main_org.id,
                    RetrievalQuery(
                        text=case["text"],
                        mode=mode,
                        well_id=main_well.id,
                        wellbore_id=main_wellbore.id,
                        section_id=section.id if section else None,
                        doc_type=case.get("doc_type"),
                        depth_from_si=case.get("depth_from_si"),
                        depth_to_si=case.get("depth_to_si"),
                        period_from=_parse_utc(case.get("period_from")),
                        period_to=_parse_utc(case.get("period_to")),
                        limit=TOP_K,
                    ),
                )
                if result.mode_used != mode.value:
                    return {
                        "status": "not_evaluated",
                        "requested_mode": mode.value,
                        "requested_query_count": query_count,
                        "measured_query_count": 0,
                        "probe_query_id": case["id"],
                        "probe_mode_used": result.mode_used,
                        "fallback_notes": result.notes,
                        "metrics": None,
                        "stratified_metrics": None,
                        "reason": (
                            "Authentic stored chunk embeddings are unavailable; semantic/hybrid ranking was "
                            "not scored. The probe records the product fallback separately. No vectors were "
                            "fabricated and no embedding provider was called by this benchmark."
                        ),
                    }
                optional_queries = await _measure_query_mode(
                    session,
                    org_id=main_org.id,
                    well_id=main_well.id,
                    wellbore_id=main_wellbore.id,
                    sections=main_scope["sections"],
                    mode=mode,
                )
                return {
                    "status": "measured",
                    "requested_mode": mode.value,
                    "requested_query_count": query_count,
                    "measured_query_count": len(optional_queries),
                    "mode_used_counts": {mode.value: len(optional_queries)},
                    "metrics": _retrieval_metrics(optional_queries),
                    "stratified_metrics": _stratified_metrics(optional_queries),
                    "queries": optional_queries,
                }

            lexical_mode_profile = {
                "status": "measured",
                "requested_mode": RetrieveMode.LEXICAL.value,
                "requested_query_count": query_count,
                "measured_query_count": len(measured_queries),
                "mode_used_counts": {RetrieveMode.LEXICAL.value: len(measured_queries)},
                "metrics": metrics,
                "stratified_metrics": _stratified_metrics(measured_queries),
            }
            metadata_mode_profile = {
                "status": "measured",
                "requested_mode": RetrieveMode.METADATA.value,
                "requested_query_count": query_count,
                "measured_query_count": len(metadata_queries),
                "mode_used_counts": {RetrieveMode.METADATA.value: len(metadata_queries)},
                "metrics": metadata_metrics,
                "stratified_metrics": _stratified_metrics(metadata_queries),
                "queries": metadata_queries,
            }
            semantic_mode_profile = await optional_mode_profile(RetrieveMode.SEMANTIC)
            hybrid_mode_profile = await optional_mode_profile(RetrieveMode.HYBRID)

            scope_profile = {
                "retrieval_mode": RetrieveMode.LEXICAL.value,
                "well_scoped_queries": query_count,
                "distinct_query_text_count": distinct_query_text_count,
                "section_scoped_queries": sum(case.get("section") is not None for case in _QUERIES),
                "depth_window_queries": sum(
                    case.get("depth_from_si") is not None or case.get("depth_to_si") is not None
                    for case in _QUERIES
                ),
                "period_window_queries": sum(
                    case.get("period_from") is not None or case.get("period_to") is not None
                    for case in _QUERIES
                ),
                "scope_violations": metrics["scope_violations"],
                "forbidden_chunk_retrievals": metrics["forbidden_chunk_retrievals"],
                "quarantined_distractor_chunks": sum(bool(row.get("quarantined")) for row in _CORPUS),
                "cross_well_distractor_chunks": sum(row["scope"] == "other_well" for row in _CORPUS),
                "cross_tenant_distractor_chunks": sum(row["scope"] == "other_tenant" for row in _CORPUS),
                "corpus_chunks_by_scope": {
                    scope_key: sum(row["scope"] == scope_key for row in _CORPUS)
                    for scope_key in ("main", "other_well", "other_tenant")
                },
            }
            citation_profile = {
                "retrieval_mode": RetrieveMode.LEXICAL.value,
                "retrieved_hits": metrics["retrieved_hit_count"],
                "citation_gaps": metrics["citation_gaps"],
                "citation_coverage": metrics["citation_coverage"],
                "source_locator_gaps": metrics["source_locator_gaps"],
                "source_locator_coverage": metrics["source_locator_coverage"],
            }
            mode_profiles = {
                "metadata": metadata_mode_profile,
                "lexical": lexical_mode_profile,
                "semantic": semantic_mode_profile,
                "hybrid": hybrid_mode_profile,
            }
            return {
                "tool": "drillai-rag-benchmark-v4",
                "benchmark_disposition": "lexical_and_metadata_measured; semantic_and_hybrid_not_evaluated_without_authentic_embeddings",
                "dataset_classification": "synthetic",
                "dataset_version": DATASET_VERSION,
                "dataset_sha256": _dataset_sha256(),
                "retrieval_mode_requested": RetrieveMode.LEXICAL.value,
                "retrieval_modes_requested": [mode.value for mode in RetrieveMode],
                "mode_profiles": mode_profiles,
                "embedding_evaluation": {
                    "authentic_stored_embedding_count": sum(
                        row.get("embedding") is not None for row in _CORPUS
                    ),
                    "synthetic_embedding_count": 0,
                    "provider_embedding_calls": 0,
                    "note": (
                        "The benchmark inserts text and metadata only. Semantic/hybrid probes stop at the "
                        "missing-stored-embeddings check; no vectors are fabricated and no provider is called."
                    ),
                },
                "top_k": TOP_K,
                "query_count": query_count,
                "distinct_query_text_count": distinct_query_text_count,
                "positive_query_count": positive_queries,
                "no_relevant_query_count": query_count - positive_queries,
                "empty_result_cases": metrics["empty_result_count"],
                "corpus_chunk_count": len(_CORPUS),
                "scope_profile": scope_profile,
                "citation_profile": citation_profile,
                "metrics": metrics,
                "queries": measured_queries,
                "limitations": [
                    "The 256 generated positive queries, 16 no-match queries, 192 same-scope near-miss chunks, and remaining scope distractors are deterministic synthetic development fixtures, not field/vendor samples or a holdout.",
                    "Lexical and metadata modes are measured separately. Semantic and hybrid modes are requested and probed but are not scored because the benchmark has no authentic stored embeddings; lexical fallback is never counted as semantic quality.",
                    "No embedding vectors are generated for this corpus and no embedding/LLM provider is called by the benchmark.",
                    "Relevance labels are fixture-authored, not independently adjudicated; metrics do not establish semantic correctness, answer quality, or engineering acceptance.",
                    "Depth windows use explicit canonical SI metadata; time windows use explicit source periods. Missing metadata is excluded, never inferred from text or created_at.",
                ],
            }
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure distinct retrieval modes on a synthetic development corpus.")
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
