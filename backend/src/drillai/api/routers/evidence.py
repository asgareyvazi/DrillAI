"""Evidence and provenance traversal.

Evidence is the platform's answer to "why should I believe this?". Every link records what it
supports (``subject_kind``/``subject_id``), what supports it (document, page, region, excerpt),
how it was produced (method, confidence) and whether the quoted text was verified against the
source. These endpoints expose that graph read-only; evidence is written by the ingestion
pipeline, not by clients — a caller cannot mint its own justification through the API.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, get_db, require
from drillai.api.serializers import bounded_json_preview, evidence_out
from drillai.core.errors import NotFound
from drillai.db.models import (
    Document,
    DocumentPage,
    DocumentRegion,
    EvidenceLink,
    ExtractedRecord,
    RawArtifact,
)
from drillai.documents.access import (
    document_not_quarantined_condition,
    evidence_document_not_quarantined_condition,
    extracted_record_scope_conditions,
)

router = APIRouter(tags=["evidence"])


@router.get("/evidence", summary="Query evidence links")
async def list_evidence(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("evidence.read"))],
    subject_kind: OptionalFilter = None,
    subject_id: OptionalFilter = None,
    document_id: OptionalFilter = None,
    well_id: OptionalFilter = None,
    evidence_kind: OptionalFilter = None,
    min_confidence: Annotated[float | None, Query(ge=0, le=1)] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    stmt = (
        select(EvidenceLink)
        .outerjoin(Document, Document.id == EvidenceLink.document_id)
        .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
        .where(EvidenceLink.org_id == auth.org_id, evidence_document_not_quarantined_condition())
    )
    if subject_kind:
        stmt = stmt.where(EvidenceLink.subject_kind == subject_kind)
    if subject_id:
        stmt = stmt.where(EvidenceLink.subject_id == subject_id)
    if document_id:
        stmt = stmt.where(EvidenceLink.document_id == document_id)
    if well_id:
        stmt = stmt.where(EvidenceLink.well_id == well_id)
    if evidence_kind:
        stmt = stmt.where(EvidenceLink.evidence_kind == evidence_kind)
    if min_confidence is not None:
        stmt = stmt.where(EvidenceLink.confidence >= min_confidence)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await session.execute(stmt.order_by(EvidenceLink.created_at.desc()).limit(limit).offset(offset))
    ).scalars().all()
    return {"items": [evidence_out(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/evidence/summary", summary="Evidence coverage for a well or document")
async def evidence_summary(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("evidence.read"))],
    well_id: OptionalFilter = None,
    document_id: OptionalFilter = None,
) -> dict[str, Any]:
    """Coverage in counts and quality terms.

    "How much of this well is actually backed by evidence, and how much of it was verified?" is a
    first-class question in an engineering platform; answering it with a count of links and a mean
    confidence is deliberately modest, but it is not a fabricated quality score.
    """
    filters = [
        EvidenceLink.org_id == auth.org_id,
        evidence_document_not_quarantined_condition(),
    ]
    if well_id:
        filters.append(EvidenceLink.well_id == well_id)
    if document_id:
        filters.append(EvidenceLink.document_id == document_id)
    aggregate = (
        await session.execute(
            select(
                func.count(EvidenceLink.id),
                func.coalesce(
                    func.sum(case((EvidenceLink.quote_verified.is_(True), 1), else_=0)), 0
                ),
                func.count(func.distinct(EvidenceLink.document_id)),
                func.avg(
                    case(
                        (
                            and_(
                                EvidenceLink.confidence.is_not(None),
                                EvidenceLink.confidence >= 0.0,
                                EvidenceLink.confidence <= 1.0,
                            ),
                            EvidenceLink.confidence,
                        ),
                        else_=None,
                    )
                ),
            )
            .select_from(EvidenceLink)
            .outerjoin(Document, Document.id == EvidenceLink.document_id)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(*filters)
        )
    ).one()
    link_count, verified_quotes, documents_referenced, mean_confidence = aggregate
    kind_rows = (
        await session.execute(
            select(EvidenceLink.evidence_kind, func.count(EvidenceLink.id))
            .select_from(EvidenceLink)
            .outerjoin(Document, Document.id == EvidenceLink.document_id)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(*filters)
            .group_by(EvidenceLink.evidence_kind)
        )
    ).all()
    by_kind = {kind: int(count) for kind, count in kind_rows}
    return {
        "link_count": int(link_count),
        "verified_quotes": int(verified_quotes),
        "documents_referenced": int(documents_referenced),
        "mean_confidence": round(float(mean_confidence), 4) if mean_confidence is not None else None,
        "by_kind": by_kind,
        "limitations": (
            [
                "confidence is the extraction confidence recorded at ingestion time, not a human "
                "assessment of correctness; invalid confidence values are excluded from the mean",
                "quote verification is mechanical (text match against the source region)",
            ]
            if link_count
            else ["no evidence links recorded in this scope"]
        ),
    }


@router.get("/evidence/{evidence_id}", summary="One evidence link, with its source excerpt")
async def get_evidence(
    evidence_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("evidence.read"))],
) -> dict[str, Any]:
    link = (
        await session.execute(
            select(EvidenceLink)
            .outerjoin(Document, Document.id == EvidenceLink.document_id)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(
                EvidenceLink.id == evidence_id,
                EvidenceLink.org_id == auth.org_id,
                evidence_document_not_quarantined_condition(),
            )
        )
    ).scalar_one_or_none()
    if link is None:
        raise NotFound(f"evidence link {evidence_id!r} not found")
    payload: dict[str, Any] = {}
    document = None
    if link.document_id is not None:
        # Check the parent again for every traversal. A quarantine change between the initial link
        # lookup and a child lookup must not turn a stale authorization into returned source content.
        document = (
            await session.execute(
                select(Document)
                .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
                .where(
                    Document.id == link.document_id,
                    Document.org_id == auth.org_id,
                    document_not_quarantined_condition(),
                )
            )
        ).scalar_one_or_none()
        if document is None:
            raise NotFound(f"evidence link {evidence_id!r} not found")
        payload["document"] = {
            "id": document.id,
            "title": document.title,
            "doc_type": document.doc_type,
            "revision": document.revision,
            "status": document.status,
            "page_count": document.page_count,
        }

    if link.evidence_kind == "document" and (
        document is None or link.evidence_id != document.id
    ):
        raise NotFound(f"evidence link {evidence_id!r} not found")

    if link.evidence_kind == "document_page" and link.region_id is not None:
        raise NotFound(f"evidence link {evidence_id!r} not found")

    if link.region_id is not None:
        if document is None or (
            link.evidence_kind == "document_region" and link.evidence_id != link.region_id
        ):
            raise NotFound(f"evidence link {evidence_id!r} not found")
        region = (
            await session.execute(
                select(DocumentRegion)
                .join(Document, Document.id == DocumentRegion.document_id)
                .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
                .where(
                    DocumentRegion.id == link.region_id,
                    DocumentRegion.org_id == auth.org_id,
                    DocumentRegion.document_id == link.document_id,
                    Document.org_id == auth.org_id,
                    document_not_quarantined_condition(),
                )
            )
        ).scalar_one_or_none()
        if region is None or (
            link.page_number is not None and region.page_number != link.page_number
        ):
            raise NotFound(f"evidence link {evidence_id!r} not found")
        table_preview, table_truncated = bounded_json_preview(
            region.table,
            max_bytes=128 * 1024,
            max_depth=8,
            max_items=100,
            max_string_chars=2048,
            max_nodes=4096,
        )
        bbox_preview, bbox_truncated = bounded_json_preview(
            region.bbox, max_bytes=2048, max_depth=4, max_items=32, max_string_chars=256, max_nodes=128
        )
        region_text = region.text or ""
        payload["region"] = {
            "id": region.id,
            "page_number": region.page_number,
            "kind": region.kind,
            "bbox": bbox_preview,
            "bbox_truncated": bbox_truncated,
            "text": region_text[:4000],
            "text_truncated": len(region_text) > 4000,
            "table": table_preview,
            "table_truncated": table_truncated,
            "extractor": region.extractor,
            "extractor_version": region.extractor_version,
        }
    elif link.evidence_kind == "document_region":
        raise NotFound(f"evidence link {evidence_id!r} not found")

    if link.evidence_kind == "document_page":
        if document is None:
            raise NotFound(f"evidence link {evidence_id!r} not found")
        page = (
            await session.execute(
                select(DocumentPage)
                .join(Document, Document.id == DocumentPage.document_id)
                .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
                .where(
                    DocumentPage.id == link.evidence_id,
                    DocumentPage.document_id == link.document_id,
                    DocumentPage.org_id == auth.org_id,
                    Document.org_id == auth.org_id,
                    document_not_quarantined_condition(),
                )
            )
        ).scalar_one_or_none()
        if page is None or (link.page_number is not None and page.page_number != link.page_number):
            raise NotFound(f"evidence link {evidence_id!r} not found")
        page_text = page.text or ""
        payload["page"] = {
            "id": page.id,
            "page_number": page.page_number,
            "text": page_text[:4000],
            "text_truncated": len(page_text) > 4000,
        }

    if link.subject_kind.startswith("extracted_record"):
        if document is None:
            raise NotFound(f"evidence link {evidence_id!r} not found")
        record = (
            await session.execute(
                select(ExtractedRecord)
                .join(Document, Document.id == ExtractedRecord.document_id)
                .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
                .where(
                    ExtractedRecord.id == link.subject_id,
                    ExtractedRecord.org_id == auth.org_id,
                    ExtractedRecord.document_id == link.document_id,
                    Document.org_id == auth.org_id,
                    document_not_quarantined_condition(),
                    *extracted_record_scope_conditions(),
                )
            )
        ).scalar_one_or_none()
        if record is None:
            raise NotFound(f"evidence link {evidence_id!r} not found")
        if (
            record.region_id != link.region_id
            or (link.page_number is not None and record.page_number != link.page_number)
            or link.evidence_kind not in {"document_region", "document_page", "document"}
            or (
                link.evidence_kind == "document"
                and (link.evidence_id != document.id or link.region_id is not None)
            )
        ):
            raise NotFound(f"evidence link {evidence_id!r} not found")
        record_payload, record_payload_truncated = bounded_json_preview(record.payload)
        payload["subject_record"] = {
            "id": record.id,
            "record_type": record.record_type,
            "payload": record_payload,
            "payload_truncated": record_payload_truncated,
            "method": record.method,
            "confidence": record.confidence,
            "validation_state": record.validation_state,
        }

    if link.document_id is not None:
        still_available = (
            await session.execute(
                select(Document.id)
                .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
                .where(
                    Document.id == link.document_id,
                    Document.org_id == auth.org_id,
                    document_not_quarantined_condition(),
                )
            )
        ).scalar_one_or_none()
        if still_available is None:
            raise NotFound(f"evidence link {evidence_id!r} not found")

    payload["evidence"] = evidence_out(link)
    return payload
