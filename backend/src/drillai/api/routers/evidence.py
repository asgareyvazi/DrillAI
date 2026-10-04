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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, get_db, require
from drillai.api.serializers import evidence_out
from drillai.core.errors import NotFound
from drillai.db.models import Document, DocumentRegion, EvidenceLink, ExtractedRecord

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
    stmt = select(EvidenceLink).where(EvidenceLink.org_id == auth.org_id)
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
    stmt = select(EvidenceLink).where(EvidenceLink.org_id == auth.org_id)
    if well_id:
        stmt = stmt.where(EvidenceLink.well_id == well_id)
    if document_id:
        stmt = stmt.where(EvidenceLink.document_id == document_id)
    rows = (await session.execute(stmt)).scalars().all()
    confidences = [row.confidence for row in rows if row.confidence is not None]
    by_kind: dict[str, int] = {}
    for row in rows:
        by_kind[row.evidence_kind] = by_kind.get(row.evidence_kind, 0) + 1
    return {
        "link_count": len(rows),
        "verified_quotes": sum(1 for row in rows if row.quote_verified),
        "documents_referenced": len({row.document_id for row in rows if row.document_id}),
        "mean_confidence": round(sum(confidences) / len(confidences), 4) if confidences else None,
        "by_kind": by_kind,
        "limitations": (
            [
                "confidence is the extraction confidence recorded at ingestion time, not a human "
                "assessment of correctness",
                "quote verification is mechanical (text match against the source region)",
            ]
            if rows
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
            select(EvidenceLink).where(EvidenceLink.id == evidence_id, EvidenceLink.org_id == auth.org_id)
        )
    ).scalar_one_or_none()
    if link is None:
        raise NotFound(f"evidence link {evidence_id!r} not found")
    payload: dict[str, Any] = {"evidence": evidence_out(link)}
    if link.document_id:
        # Every traversal below is organization-scoped, and the scoping matters even though the link
        # itself was already checked. The link names its subjects by **identifier**, and an identifier
        # in a row the caller can read is not proof that the row it points at is the caller's: a link
        # whose `region_id` was written from another tenant's document would have been followed here
        # without complaint, and the excerpt handed to the caller. The whole traversal was unscoped
        # before CP8; only the first lookup was not.
        document = (
            await session.execute(
                select(Document).where(Document.id == link.document_id, Document.org_id == auth.org_id)
            )
        ).scalar_one_or_none()
        payload["document"] = (
            {
                "id": document.id,
                "title": document.title,
                "doc_type": document.doc_type,
                "revision": document.revision,
                "status": document.status,
                "page_count": document.page_count,
            }
            if document is not None
            else None
        )
        if link.region_id:
            region = (
                await session.execute(
                    select(DocumentRegion).where(
                        DocumentRegion.id == link.region_id,
                        DocumentRegion.org_id == auth.org_id,
                        # …and it must belong to the document the link names. A region of another
                        # document in the same organization is a different kind of wrong: the excerpt
                        # would be real, and from somewhere else.
                        DocumentRegion.document_id == link.document_id,
                    )
                )
            ).scalar_one_or_none()
            payload["region"] = (
                {
                    "id": region.id,
                    "page_number": region.page_number,
                    "kind": region.kind,
                    "bbox": region.bbox,
                    "text": (region.text or "")[:4000],
                    "table": region.table,
                    "extractor": region.extractor,
                    "extractor_version": region.extractor_version,
                }
                if region is not None
                else None
            )
        if link.subject_kind.startswith("extracted_record"):
            record = (
                await session.execute(
                    select(ExtractedRecord).where(
                        ExtractedRecord.id == link.subject_id,
                        ExtractedRecord.org_id == auth.org_id,
                        ExtractedRecord.document_id == link.document_id,
                    )
                )
            ).scalar_one_or_none()
            payload["subject_record"] = (
                {
                    "id": record.id,
                    "record_type": record.record_type,
                    "payload": record.payload,
                    "method": record.method,
                    "confidence": record.confidence,
                    "validation_state": record.validation_state,
                }
                if record is not None
                else None
            )
    return payload
