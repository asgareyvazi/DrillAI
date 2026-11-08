"""Deterministic, synthetic retrieval benchmark for the local RAG ranking contract.

This command evaluates the production lexical retrieval service in an isolated SQLite database. A
versioned, fictional corpus supplies exact development-fixture relevance labels, cross-scope distractors,
unknown depth/time metadata, a quarantined source, and explicit source locators. It reports lexical
Recall@k/Precision@k/MRR, exclusions, scope violations, citation coverage, and locator coverage only.

It makes no network calls and uses no embedding or LLM provider. Its relevance labels are hand-authored
software fixtures, not independent review or evidence of production-corpus, semantic, engineering, or
answer quality. An unknown depth or reporting period remains unknown and is excluded when a query asks
for a bounded depth/time window; record creation time is never substituted for a missing source period.

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

DATASET_VERSION = "drillai-rag-synthetic-v3"
TOP_K = 5
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
        "text": "mud check plastic viscosity 22 cp",
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
        "text": "mud check plastic viscosity 22 cp",
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


def _build_cp130_scale_fixtures() -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    """Create 100 unique positive retrieval labels plus 47 deterministic scope distractors."""
    rows: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    page_by_document: dict[str, int] = {}
    main_document_id = "doc_cp130_rag_generated"

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

    for index in range(100):
        sequence = index + 1
        sequence_text = f"{sequence:03d}"
        sample_day = dt.date(2026, 4, 1) + dt.timedelta(days=index)
        next_day = sample_day + dt.timedelta(days=1)
        period_start = dt.datetime.combine(sample_day, dt.time.min, tzinfo=dt.UTC).isoformat()
        period_end = dt.datetime.combine(next_day, dt.time.min, tzinfo=dt.UTC).isoformat()
        depth_start = 800.0 + (index % 20) * 50.0
        depth_end = depth_start + 100.0
        fixture_id = f"chk_cp130_item_{sequence_text}"
        add_row(
            fixture_id=fixture_id,
            document_id=main_document_id,
            scope="main",
            text=(
                f"Synthetic benchmark record CP130-ITEM-{sequence_text} parameter-{sequence_text} "
                f"value {100 + index}.25 canonical-SI; deterministic development-only fixture."
            ),
            depth_from_si=depth_start,
            depth_to_si=depth_end,
            period_start=period_start,
            period_end=period_end,
        )
        next_sequence = sequence % 100 + 1
        queries.append(
            {
                "id": f"cp130-unique-record-{sequence_text}",
                "text": f"CP130-ITEM-{sequence_text} parameter-{sequence_text}",
                "section": "intermediate",
                "doc_type": "benchmark",
                "depth_from_si": depth_start + 10.0,
                "depth_to_si": depth_end - 10.0,
                "period_from": period_start,
                "period_to": period_end,
                "relevant_chunk_ids": [fixture_id],
                "must_not_retrieve_chunk_ids": [
                    f"chk_cp130_item_{next_sequence:03d}",
                    "chk_cp130_dist_quarantine_001",
                    "chk_cp130_dist_other_well_001",
                    "chk_cp130_dist_other_tenant_001",
                ],
            }
        )

    # These 37 main-scope chunks challenge explicit depth/time filters and unknown-metadata handling.
    for index in range(20):
        sequence = index + 1
        day = dt.date(2026, 4, 1) + dt.timedelta(days=index)
        start = dt.datetime.combine(day, dt.time.min, tzinfo=dt.UTC).isoformat()
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=dt.UTC).isoformat()
        add_row(
            fixture_id=f"chk_cp130_dist_depth_{sequence:03d}",
            document_id=main_document_id,
            scope="main",
            text=f"Synthetic depth distractor CP130-DEPTH-DISTRACTOR-{sequence:03d}; not a target record.",
            depth_from_si=30_000.0 + index * 100.0,
            depth_to_si=30_050.0 + index * 100.0,
            period_start=start,
            period_end=end,
        )
    for index in range(10):
        sequence = index + 1
        add_row(
            fixture_id=f"chk_cp130_dist_time_{sequence:03d}",
            document_id=main_document_id,
            scope="main",
            text=f"Synthetic time distractor CP130-TIME-DISTRACTOR-{sequence:03d}; not a target record.",
            depth_from_si=800.0 + index * 50.0,
            depth_to_si=900.0 + index * 50.0,
            period_start=f"2099-01-{sequence:02d}T00:00:00+00:00",
            period_end=f"2099-01-{sequence:02d}T23:59:59+00:00",
        )
    for index in range(7):
        sequence = index + 1
        add_row(
            fixture_id=f"chk_cp130_dist_unknown_{sequence:03d}",
            document_id=main_document_id,
            scope="main",
            text=f"Synthetic unlocated distractor CP130-UNKNOWN-DISTRACTOR-{sequence:03d}; scope unknown.",
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
            fixture_id=f"chk_cp130_dist_quarantine_{sequence:03d}",
            document_id="doc_cp130_rag_quarantine",
            scope="main",
            text=f"Synthetic quarantined distractor CP130-QUARANTINE-{sequence:03d}; excluded by trust state.",
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
            fixture_id=f"chk_cp130_dist_other_well_{sequence:03d}",
            document_id="doc_cp130_rag_other_well",
            scope="other_well",
            text=f"Synthetic other-well distractor CP130-WELL-DISTRACTOR-{sequence:03d}.",
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
            fixture_id=f"chk_cp130_dist_other_tenant_{sequence:03d}",
            document_id="doc_cp130_rag_other_tenant",
            scope="other_tenant",
            text=f"Synthetic other-tenant distractor CP130-TENANT-DISTRACTOR-{sequence:03d}.",
            depth_from_si=800.0 + index * 50.0,
            depth_to_si=900.0 + index * 50.0,
            period_start=start,
            period_end=end,
        )
    return tuple(rows), tuple(queries)


_CP130_CORPUS, _CP130_QUERIES = _build_cp130_scale_fixtures()
_CORPUS = _CORPUS + _CP130_CORPUS
_QUERIES = _QUERIES + _CP130_QUERIES


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

            measured_queries: list[dict[str, Any]] = []
            total_relevant = total_retrieved_relevant = 0
            total_hits = total_forbidden_hits = scope_violations = 0
            citation_gaps = source_locator_gaps = 0
            reciprocal_ranks: list[float] = []
            recalls: list[float] = []
            precisions: list[float] = []
            for case in _QUERIES:
                section = main_scope["sections"].get(case.get("section"))
                period_from = _parse_utc(case.get("period_from"))
                period_to = _parse_utc(case.get("period_to"))
                result = await retrieve(
                    session,
                    main_org.id,
                    RetrievalQuery(
                        text=case["text"],
                        mode=RetrieveMode.LEXICAL,
                        well_id=main_well.id,
                        wellbore_id=main_wellbore.id,
                        section_id=section.id if section else None,
                        doc_type=case.get("doc_type"),
                        depth_from_si=case.get("depth_from_si"),
                        depth_to_si=case.get("depth_to_si"),
                        period_from=period_from,
                        period_to=period_to,
                        limit=TOP_K,
                    ),
                )
                retrieved_ids = [hit.chunk_id for hit in result.hits]
                relevant_ids = set(case["relevant_chunk_ids"])
                forbidden_ids = set(case.get("must_not_retrieve_chunk_ids", []))
                relevant_ranks = [
                    index for index, chunk_id in enumerate(retrieved_ids, start=1) if chunk_id in relevant_ids
                ]
                relevant_count = len(relevant_ids)
                retrieved_relevant_count = len(relevant_ranks)
                reciprocal_rank = 1.0 / relevant_ranks[0] if relevant_ranks else 0.0
                precision = retrieved_relevant_count / TOP_K
                recall = retrieved_relevant_count / relevant_count if relevant_count else 0.0
                forbidden_retrieved = sorted(forbidden_ids.intersection(retrieved_ids))
                case_scope_violations = sum(
                    hit.org_id != main_org.id
                    or hit.well_id != main_well.id
                    or hit.wellbore_id != main_wellbore.id
                    or (section is not None and hit.section_id != section.id)
                    or (case.get("doc_type") is not None and hit.doc_type != case["doc_type"])
                    or not _overlaps_depth(hit, case)
                    or not _overlaps_period(hit, case)
                    for hit in result.hits
                )
                case_citation_gaps = sum(not hit.citations for hit in result.hits)
                case_source_locator_gaps = sum(
                    not isinstance(hit.source_locator, dict)
                    or hit.source_locator.get("kind") != "synthetic_fixture"
                    or hit.source_locator.get("fixture_id") != hit.chunk_id
                    or hit.source_locator.get("page_number") != hit.page_number
                    for hit in result.hits
                )
                filters_applied = {
                    key: value.isoformat() if isinstance(value, dt.datetime) else value
                    for key, value in result.filters_applied.items()
                }
                measured_queries.append(
                    {
                        "id": case["id"],
                        "mode_used": result.mode_used,
                        "filters_applied": filters_applied,
                        "expected_relevant_chunk_ids": sorted(relevant_ids),
                        "retrieved_chunk_ids": retrieved_ids,
                        "must_not_retrieve_chunk_ids": sorted(forbidden_ids),
                        "forbidden_retrieved_chunk_ids": forbidden_retrieved,
                        "relevant_retrieved": retrieved_relevant_count,
                        "recall_at_k": recall,
                        "precision_at_k": precision,
                        "reciprocal_rank": reciprocal_rank,
                        "scope_violations": case_scope_violations,
                        "citation_gaps": case_citation_gaps,
                        "source_locator_gaps": case_source_locator_gaps,
                    }
                )
                total_relevant += relevant_count
                total_retrieved_relevant += retrieved_relevant_count
                if relevant_count:
                    reciprocal_ranks.append(reciprocal_rank)
                    recalls.append(recall)
                precisions.append(precision)
                total_hits += len(result.hits)
                total_forbidden_hits += len(forbidden_retrieved)
                scope_violations += case_scope_violations
                citation_gaps += case_citation_gaps
                source_locator_gaps += case_source_locator_gaps

            positive_queries = sum(bool(case["relevant_chunk_ids"]) for case in _QUERIES)
            empty_result_cases = sum(not case["retrieved_chunk_ids"] for case in measured_queries)
            query_count = len(_QUERIES)
            citation_coverage = (total_hits - citation_gaps) / total_hits if total_hits else 1.0
            source_locator_coverage = (total_hits - source_locator_gaps) / total_hits if total_hits else 1.0
            scope_profile = {
                "well_scoped_queries": query_count,
                "section_scoped_queries": sum(case.get("section") is not None for case in _QUERIES),
                "depth_window_queries": sum(
                    case.get("depth_from_si") is not None or case.get("depth_to_si") is not None
                    for case in _QUERIES
                ),
                "period_window_queries": sum(
                    case.get("period_from") is not None or case.get("period_to") is not None
                    for case in _QUERIES
                ),
                "scope_violations": scope_violations,
                "forbidden_chunk_retrievals": total_forbidden_hits,
                "quarantined_distractor_chunks": sum(bool(row.get("quarantined")) for row in _CORPUS),
                "cross_well_distractor_chunks": sum(row["scope"] == "other_well" for row in _CORPUS),
                "cross_tenant_distractor_chunks": sum(row["scope"] == "other_tenant" for row in _CORPUS),
            }
            citation_profile = {
                "retrieved_hits": total_hits,
                "citation_gaps": citation_gaps,
                "citation_coverage": citation_coverage,
                "source_locator_gaps": source_locator_gaps,
                "source_locator_coverage": source_locator_coverage,
            }
            return {
                "tool": "drillai-rag-benchmark-v3",
                "dataset_classification": "synthetic",
                "dataset_version": DATASET_VERSION,
                "dataset_sha256": _dataset_sha256(),
                "retrieval_mode_requested": RetrieveMode.LEXICAL.value,
                "top_k": TOP_K,
                "query_count": query_count,
                "positive_query_count": positive_queries,
                "no_relevant_query_count": query_count - positive_queries,
                "empty_result_cases": empty_result_cases,
                "corpus_chunk_count": len(_CORPUS),
                "scope_profile": scope_profile,
                "citation_profile": citation_profile,
                "metrics": {
                    "micro_recall_at_k": total_retrieved_relevant / total_relevant if total_relevant else 0.0,
                    "macro_recall_at_k_positive_queries": sum(recalls) / len(recalls) if recalls else 0.0,
                    "macro_precision_at_k_all_queries": sum(precisions) / query_count if query_count else 0.0,
                    "mean_reciprocal_rank_positive_queries": sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0.0,
                    "scope_violations": scope_violations,
                    "forbidden_chunk_retrievals": total_forbidden_hits,
                    "citation_coverage": (total_hits - citation_gaps) / total_hits if total_hits else 1.0,
                    "source_locator_coverage": (total_hits - source_locator_gaps) / total_hits if total_hits else 1.0,
                },
                "queries": measured_queries,
                "limitations": [
                    "The 100 CP130 scale queries and generated distractors are synthetic development fixtures, not field/vendor samples or a holdout.",
                    "Synthetic, hand-labelled development fixtures are not representative field or production evidence.",
                    "Lexical retrieval only; embeddings, hybrid ranking, and LLM answer quality are not evaluated.",
                    "Relevance labels are not independently adjudicated; metrics do not establish semantic correctness or engineering acceptance.",
                    "Depth windows use explicit canonical SI metadata; time windows use explicit source periods. Missing metadata is excluded, never inferred from text or created_at.",
                ],
            }
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate deterministic lexical retrieval on a synthetic development corpus.")
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
