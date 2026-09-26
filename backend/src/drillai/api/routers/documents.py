"""Documents and ingestion.

Upload is synchronous in this MVP on purpose: the ingestion chain is in-process and the caller
needs the outcome (status, extracted record count, evidence links) to decide what to do next. The
job row is written either way, so a future queue-backed ingest changes latency, not the contract.

The response of an upload is the *ingestion record*, never a bare "ok": document id, job id,
status, stats, warnings and the trace id that ties it to the observability stream.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, get_blob_store, get_db, require
from drillai.api.serializers import (
    document_out,
    evidence_out,
    extracted_record_out,
    ingestion_job_out,
)
from drillai.core.config import get_settings
from drillai.core.errors import NotFound, ValidationFailed
from drillai.db.models import (
    Document,
    DocumentChunk,
    DocumentRegion,
    EvidenceLink,
    ExtractedRecord,
    IngestionJob,
    Well,
    Wellbore,
    WellSection,
)
from drillai.security.actions import authorize

router = APIRouter(tags=["documents"])

#: Extraction methods a record can come from; kept in one place so the detail view can explain it.
EXTRACTION_METHODS = ("parser", "table", "figure", "regex", "layout", "manual", "import")


@router.get("/documents", summary="List documents")
async def list_documents(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("document.read"))],
    well_id: OptionalFilter = None,
    project_id: OptionalFilter = None,
    doc_type: OptionalFilter = None,
    status: OptionalFilter = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    stmt = select(Document).where(Document.org_id == auth.org_id)
    if well_id:
        stmt = stmt.where(Document.well_id == well_id)
    if project_id:
        stmt = stmt.where(Document.project_id == project_id)
    if doc_type:
        stmt = stmt.where(Document.doc_type == doc_type)
    if status:
        stmt = stmt.where(Document.status == status)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await session.execute(stmt.order_by(Document.created_at.desc()).limit(limit).offset(offset))
    ).scalars().all()
    return {"items": [document_out(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.post("/documents", summary="Upload a document and ingest it", status_code=201)
async def upload_document(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("document.read"))],
    file: Annotated[UploadFile, File(description="PDF, DOCX, XLSX, CSV or text report")],
    well_id: Annotated[str | None, Form()] = None,
    wellbore_id: Annotated[str | None, Form()] = None,
    section_id: Annotated[str | None, Form()] = None,
    project_id: Annotated[str | None, Form()] = None,
    doc_type: Annotated[str | None, Form()] = None,
    title: Annotated[str | None, Form()] = None,
    period_start: Annotated[dt.datetime | None, Form()] = None,
    period_end: Annotated[dt.datetime | None, Form()] = None,
) -> dict[str, Any]:
    """Ingest one uploaded file into the data fabric.

    The scope (well / wellbore / section) is written onto the document *and* inherited by every
    chunk and extracted record, which is what allows later retrieval and context assembly to be
    scoped to a hole section rather than to a well as a whole.
    """
    authorize(auth.principal, "document.ingest")
    if period_start and period_end and period_end < period_start:
        raise ValidationFailed("period_end must not precede period_start")
    settings = get_settings()
    data = await file.read()
    if not data:
        raise ValidationFailed("the uploaded file is empty")
    if len(data) > settings.max_upload_bytes:
        raise ValidationFailed(
            "upload exceeds the configured limit",
            details={"bytes": len(data), "limit": settings.max_upload_bytes},
        )
    if well_id is not None:
        well = (
            await session.execute(
                select(Well).where(Well.id == well_id, Well.org_id == auth.org_id)
            )
        ).scalar_one_or_none()
        if well is None:
            raise NotFound(f"well {well_id!r} not found")
        project_id = project_id or well.project_id
    if wellbore_id is not None:
        wellbore = (
            await session.execute(select(Wellbore).where(Wellbore.id == wellbore_id))
        ).scalar_one_or_none()
        if wellbore is None:
            raise NotFound(f"wellbore {wellbore_id!r} not found")
        well_id = well_id or wellbore.well_id
        project_id = project_id or (
            await session.execute(select(Well.project_id).where(Well.id == wellbore.well_id))
        ).scalar_one_or_none()
    if section_id is not None:
        section = (
            await session.execute(select(WellSection).where(WellSection.id == section_id))
        ).scalar_one_or_none()
        if section is None:
            raise NotFound(f"section {section_id!r} not found")
        wellbore_id = wellbore_id or section.wellbore_id
        if wellbore_id != section.wellbore_id:
            raise ValidationFailed(
                "section_id does not belong to the supplied wellbore_id",
                details={"section_id": section_id, "wellbore_id": wellbore_id},
            )
        if well_id is None:
            well_id = (
                await session.execute(select(Wellbore.well_id).where(Wellbore.id == section.wellbore_id))
            ).scalar_one_or_none()

    from drillai.ingestion.pipeline import IngestionPipeline

    pipeline = IngestionPipeline(session, get_blob_store(request), org_id=auth.org_id or "")
    outcome = await pipeline.ingest(
        data,
        filename=file.filename or "upload.bin",
        content_type=file.content_type,
        project_id=project_id,
        well_id=well_id,
        wellbore_id=wellbore_id,
        section_id=section_id,
        doc_type=doc_type,
        title=title,
        period_start=period_start,
        period_end=period_end,
        trigger="upload",
        triggered_by=auth.principal.id,
    )
    return {
        "document": document_out(outcome.document),
        "job": _job_payload(outcome),
        "record_ids": list(outcome.record_ids),
        "evidence_link_ids": list(outcome.evidence_link_ids),
    }


@router.get("/documents/{document_id}", summary="Document detail: pages, chunks, records, evidence")
async def get_document(
    document_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("document.read"))],
    include_chunks: bool = Query(default=True),
    include_records: bool = Query(default=True),
    chunk_limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    document = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.org_id == auth.org_id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFound(f"document {document_id!r} not found")

    payload: dict[str, Any] = {"document": document_out(document)}
    jobs = (
        await session.execute(
            select(IngestionJob).where(IngestionJob.document_id == document_id).order_by(IngestionJob.created_at.desc())
        )
    ).scalars().all()
    payload["ingestion_jobs"] = [ingestion_job_out(row) for row in jobs]
    regions = (
        await session.execute(
            select(func.count()).select_from(DocumentRegion).where(DocumentRegion.document_id == document_id)
        )
    ).scalar_one()
    payload["region_count"] = regions
    if include_chunks:
        chunks = (
            await session.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.chunk_index)
                .limit(chunk_limit)
            )
        ).scalars().all()
        payload["chunks"] = [
            {
                "id": chunk.id,
                "chunk_index": chunk.chunk_index,
                "page_number": chunk.page_number,
                "kind": chunk.kind,
                "text": chunk.text,
                "token_estimate": chunk.token_estimate,
                "section_id": chunk.section_id,
                "depth_from_si": chunk.depth_from_si,
                "depth_to_si": chunk.depth_to_si,
            }
            for chunk in chunks
        ]
    if include_records:
        records = (
            await session.execute(
                select(ExtractedRecord)
                .where(ExtractedRecord.document_id == document_id)
                .order_by(ExtractedRecord.created_at)
            )
        ).scalars().all()
        payload["records"] = [extracted_record_out(row) for row in records]
    links = (
        await session.execute(
            select(EvidenceLink)
            .where(EvidenceLink.document_id == document_id)
            .order_by(EvidenceLink.created_at)
            .limit(200)
        )
    ).scalars().all()
    payload["evidence_links"] = [evidence_out(row) for row in links]
    return payload


@router.get("/documents/{document_id}/ingestion", summary="Ingestion jobs for a document")
async def document_ingestion(
    document_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("document.read"))],
) -> dict[str, Any]:
    document = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.org_id == auth.org_id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFound(f"document {document_id!r} not found")
    rows = (
        await session.execute(
            select(IngestionJob).where(IngestionJob.document_id == document_id).order_by(IngestionJob.created_at.desc())
        )
    ).scalars().all()
    return {
        "document_id": document_id,
        "document_status": document.status,
        "items": [ingestion_job_out(row) for row in rows],
        "total": len(rows),
    }


@router.get("/documents/{document_id}/provenance", summary="Provenance: page → region → extraction")
async def document_provenance(
    document_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("evidence.read"))],
) -> dict[str, Any]:
    """The provenance chain for one document: what was extracted, by which method, from where.

    This is the "fact → document → page → region → extraction method → confidence → validation"
    chain in one response, so a reviewer never has to reconstruct it from five endpoints.
    """
    document = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.org_id == auth.org_id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFound(f"document {document_id!r} not found")
    records = (
        await session.execute(
            select(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
        )
    ).scalars().all()
    regions = (
        await session.execute(
            select(DocumentRegion).where(DocumentRegion.document_id == document_id).limit(500)
        )
    ).scalars().all()
    region_index = {region.id: region for region in regions}
    chain = []
    for record in records:
        region = region_index.get(record.region_id) if record.region_id else None
        chain.append(
            {
                "record": extracted_record_out(record),
                "region": (
                    {
                        "id": region.id,
                        "page_number": region.page_number,
                        "kind": region.kind,
                        "bbox": region.bbox,
                        "text": (region.text or "")[:2000],
                        "attributes": region.attributes,
                    }
                    if region is not None
                    else None
                ),
                "document": {
                    "id": document.id,
                    "title": document.title,
                    "doc_type": document.doc_type,
                    "revision": document.revision,
                },
            }
        )
    return {
        "document_id": document_id,
        "extraction_summary": document.extraction_summary or {},
        "chain": chain,
        "region_count": len(regions),
        "record_count": len(records),
    }


@router.get("/documents/{document_id}/file", summary="Download the original upload")
async def download_document(
    document_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("document.read"))],
) -> Any:
    """Return the stored original bytes.

    Serving the original (rather than only the text) is what lets a reviewer check an extraction
    against the page it came from. Permission-wise this is the same read gate as the metadata: the
    document *is* the evidence.
    """
    from fastapi.responses import Response

    from drillai.db.models import RawArtifact

    document = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.org_id == auth.org_id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFound(f"document {document_id!r} not found")
    if not document.raw_artifact_id:
        raise NotFound("this document has no stored original file")
    artifact = (
        await session.execute(select(RawArtifact).where(RawArtifact.id == document.raw_artifact_id))
    ).scalar_one_or_none()
    if artifact is None:
        raise NotFound("the stored original for this document is missing")
    store = get_blob_store(request)
    if not store.exists(artifact.blob_key):
        raise NotFound("the stored original is no longer available in the blob backend")
    data = store.get(artifact.blob_key)
    filename = artifact.original_filename or f"{document.id}.bin"
    return Response(
        content=data,
        media_type=artifact.content_type or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@router.get("/documents/{document_id}/permissions", summary="What the caller may see in this document")
async def document_permissions(
    document_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("document.read"))],
) -> dict[str, Any]:
    """Explain the caller's access in terms of permissions rather than a boolean.

    A 403 with no explanation is a support ticket; this endpoint lets a UI say *why* a control is
    disabled ("your role holds document.read but not artifact.approve").
    """
    document = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.org_id == auth.org_id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFound(f"document {document_id!r} not found")
    relevant = ["document.read", "document.write", "evidence.read", "action:artifact.approve", "action:export.create"]
    return {
        "document_id": document_id,
        "role_keys": list(auth.principal.role_keys),
        "action_ceiling": auth.principal.max_action_level.value,
        "can": {permission: auth.principal.has_permission(permission) for permission in relevant},
        "note": "permissions are patterns; the answer above uses the same matcher as the action gate",
        "permission_count": len(auth.principal.permissions),
    }


def _job_payload(outcome: Any) -> dict[str, Any]:
    return {
        **ingestion_job_out(outcome.job),
        "page_count": outcome.page_count,
        "stats": outcome.job.stats or {},
    }
