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
    DOC_TYPES,
    Document,
    DocumentChunk,
    DocumentRegion,
    EvidenceLink,
    ExtractedRecord,
    IngestionJob,
)
from drillai.documents.scope import DocumentScope, DocumentScopeResolver
from drillai.security.actions import authorize

router = APIRouter(tags=["documents"])


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
    operation_id: Annotated[str | None, Form()] = None,
    doc_type: Annotated[str | None, Form()] = None,
    revision: Annotated[str | None, Form()] = None,
    title: Annotated[str | None, Form()] = None,
    period_start: Annotated[dt.datetime | None, Form()] = None,
    period_end: Annotated[dt.datetime | None, Form()] = None,
) -> dict[str, Any]:
    """Ingest one uploaded file into the data fabric.

    The scope (project / well / wellbore / section / operation) is resolved once, against the caller's
    organization, before anything is read from the upload — so a scope the caller may not use is
    refused before the file is stored, and a scope whose parts disagree is refused rather than
    corrected. The resolved scope is written onto the document and inherited by every chunk and
    extracted record, which is what allows retrieval and context assembly to be scoped to a hole
    section rather than to a well as a whole.

    The same bytes uploaded against a *different* scope produce a second logical document sharing one
    stored artefact: a report filed against the wrong well has to be correctable. The same bytes
    against the same scope at the same revision return the document that already exists.
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
    if doc_type is not None and doc_type not in DOC_TYPES:
        raise ValidationFailed(
            "doc_type is not a recognised document type",
            details={"field": "doc_type", "value": doc_type, "allowed": sorted(DOC_TYPES)},
        )

    # One resolver, one place where the hierarchy is checked. It filters every lookup by the caller's
    # organization and refuses a scope whose parts disagree — neither of which the inline version did
    # for wellbore and section, which is how a tenant could attach a document to another tenant's
    # hole (reproduced against the running API before this was written).
    scope = await DocumentScopeResolver(session, auth.org_id or "").resolve(
        DocumentScope(
            project_id=project_id,
            well_id=well_id,
            wellbore_id=wellbore_id,
            section_id=section_id,
            operation_id=operation_id,
        )
    )

    from drillai.ingestion.pipeline import IngestionPipeline

    pipeline = IngestionPipeline(session, get_blob_store(request), org_id=auth.org_id or "")
    outcome = await pipeline.ingest(
        data,
        filename=file.filename or "upload.bin",
        content_type=file.content_type,
        project_id=scope.project_id,
        well_id=scope.well_id,
        wellbore_id=scope.wellbore_id,
        section_id=scope.section_id,
        operation_id=scope.operation_id,
        doc_type=doc_type,
        title=title,
        revision=revision,
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
        # What the platform attached this document to, and which parts of that the caller supplied.
        # A client that named only a section gets a document with a wellbore, a well and a project it
        # never mentioned, and can see that this is what happened.
        "scope": {
            **scope.as_dict(),
            "supplied": list(scope.supplied),
            "derived": list(scope.derived),
            "chain": list(scope.chain),
            "notes": list(scope.notes),
            "reused_existing": outcome.reused_existing,
        },
    }


@router.get("/documents/{document_id}", summary="Document detail: pages, chunks, records, evidence")
async def get_document(
    document_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("document.read"))],
    include_chunks: bool = Query(default=True),
    include_records: bool = Query(default=True),
    include_regions: bool = Query(default=True),
    chunk_limit: int = Query(default=50, ge=1, le=500),
    region_limit: int = Query(default=100, ge=1, le=500),
    record_limit: int = Query(default=200, ge=1, le=1000),
    record_offset: int = Query(default=0, ge=0),
    record_after: Annotated[
        str | None, Query(description="keyset position: pass back next_record_cursor")
    ] = None,
    evidence_limit: int = Query(default=200, ge=1, le=1000),
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
    if include_regions:
        region_rows = (
            await session.execute(
                select(DocumentRegion)
                .where(DocumentRegion.document_id == document_id)
                .order_by(DocumentRegion.page_number, DocumentRegion.id)
                .limit(region_limit)
            )
        ).scalars().all()
        payload["regions"] = [
            {
                "id": region.id,
                "page_number": region.page_number,
                "kind": region.kind,
                "bbox": region.bbox,
                # Bounded like the provenance endpoint: a region's text is evidence, not a payload.
                "text": (region.text or "")[:2000],
                "attributes": region.attributes,
            }
            for region in region_rows
        ]
        payload["regions_returned"] = len(region_rows)
        payload["regions_truncated"] = regions > len(region_rows)
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
                # The chunk→region link, which is what makes the text of a chunk traceable to a place
                # on a page. It was populated by nothing before this, so every chunk pointed at null.
                "region_id": chunk.region_id,
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
        record_total = (
            await session.execute(
                select(func.count())
                .select_from(ExtractedRecord)
                .where(ExtractedRecord.document_id == document_id)
            )
        ).scalar_one()
        statement = (
            select(ExtractedRecord)
            .where(ExtractedRecord.document_id == document_id)
            # Ordered by id, which is time-sortable, so the page is stable and the cursor can be a
            # record id rather than a computed offset that shifts when rows are written.
            .order_by(ExtractedRecord.id)
            .limit(record_limit)
        )
        if record_after:
            await _require_record_cursor(session, document_id, record_after)
            statement = statement.where(ExtractedRecord.id > record_after)
        else:
            statement = statement.offset(record_offset)
        records = (await session.execute(statement)).scalars().all()
        payload["records"] = [extracted_record_out(row) for row in records]
        payload["record_count"] = record_total
        payload["records_returned"] = len(records)
        payload["records_truncated"] = record_total > record_offset + len(records)
        # The cursor is the last id on this page: pass it back as ``record_after`` for the next one.
        payload["next_record_cursor"] = records[-1].id if len(records) == record_limit else None
    evidence_total = (
        await session.execute(
            select(func.count()).select_from(EvidenceLink).where(EvidenceLink.document_id == document_id)
        )
    ).scalar_one()
    links = (
        await session.execute(
            select(EvidenceLink)
            .where(EvidenceLink.document_id == document_id)
            .order_by(EvidenceLink.id)
            .limit(evidence_limit)
        )
    ).scalars().all()
    payload["evidence_links"] = [evidence_out(row) for row in links]
    payload["evidence_count"] = evidence_total
    payload["evidence_truncated"] = evidence_total > len(links)
    payload["limits"] = {
        "chunks": chunk_limit,
        "regions": region_limit,
        "records": record_limit,
        "evidence": evidence_limit,
    }
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
    limit: int = Query(default=200, ge=1, le=1000),
    cursor: Annotated[
        str | None,
        Query(description="keyset position: pass back next_cursor from the previous page"),
    ] = None,
) -> dict[str, Any]:
    """The provenance chain for one document: what was extracted, by which method, from where.

    This is the "fact → document → page → region → extraction method → confidence → validation"
    chain in one response, so a reviewer never has to reconstruct it from five endpoints. The chain
    is a page of extracted records in id order — the same order the document detail lists them — and
    each page resolves only the regions it references.
    """
    document = (
        await session.execute(
            select(Document).where(Document.id == document_id, Document.org_id == auth.org_id)
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFound(f"document {document_id!r} not found")
    total = (
        await session.execute(
            select(func.count())
            .select_from(ExtractedRecord)
            .where(ExtractedRecord.document_id == document_id)
        )
    ).scalar_one()
    if cursor:
        await _require_record_cursor(session, document_id, cursor)
    statement = (
        select(ExtractedRecord)
        .where(ExtractedRecord.document_id == document_id)
        .order_by(ExtractedRecord.id)
        .limit(limit)
    )
    if cursor:
        statement = statement.where(ExtractedRecord.id > cursor)
    records = list((await session.execute(statement)).scalars().all())
    wanted = {record.region_id for record in records if record.region_id}
    regions = (
        list(
            (
                await session.execute(
                    select(DocumentRegion).where(DocumentRegion.id.in_(wanted))
                )
            ).scalars().all()
        )
        if wanted
        else []
    )
    # The region must belong to this document; a record pointing at another document's region would
    # otherwise leak that region's text through this response.
    region_index = {
        region.id: region for region in regions if region.document_id == document_id
    }
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
        "regions_returned": len(region_index),
        "record_count": total,
        "records_returned": len(records),
        "next_cursor": records[-1].id if len(records) == limit else None,
        "chain_truncated": total > len(records),
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
    return Response(
        content=data,
        media_type=artifact.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": _content_disposition(artifact.original_filename, document.id)
        },
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


async def _require_record_cursor(session: AsyncSession, document_id: str, cursor: str) -> None:
    """A cursor must name a record of *this* document, or it is refused.

    An opaque string compared with ``>`` is not a cursor: whether it returns the first page, the last
    page or everything depends on how it happens to sort. Refusing it means a client that passes the
    wrong token is told so, instead of being handed a page that looks like a valid answer.
    """
    found = (
        await session.execute(
            select(ExtractedRecord.id).where(
                ExtractedRecord.id == cursor, ExtractedRecord.document_id == document_id
            )
        )
    ).scalar_one_or_none()
    if found is None:
        raise ValidationFailed(
            "cursor does not name an extracted record of this document",
            details={"document_id": document_id, "hint": "pass back next_cursor unchanged"},
        )


_HEADER_UNSAFE = {chr(code) for code in range(0x20)} | {"\x7f", '"', "\\"}


def _content_disposition(filename: str | None, fallback: str) -> str:
    """A ``Content-Disposition`` that cannot be turned into a second header or an unending string.

    The uploaded filename is attacker-controlled. Interpolating it directly let a name containing a
    quote end the quoted string and start header parameters, and a newline could split the response
    into a second header — response splitting. Control characters and quotes are dropped from the
    ASCII form, the name is bounded, and the original is carried in the RFC 5987 ``filename*`` form
    (percent-encoded) so a non-ASCII name still arrives intact.
    """
    from urllib.parse import quote

    name = (filename or "").strip() or f"{fallback}.bin"
    safe = "".join(character for character in name if character not in _HEADER_UNSAFE)
    safe = safe.replace(";", "_")[:150] or f"{fallback}.bin"
    encoded = quote(name, safe="")
    return f"inline; filename=\"{safe}\"; filename*=UTF-8''{encoded}"


def _job_payload(outcome: Any) -> dict[str, Any]:
    return {
        **ingestion_job_out(outcome.job),
        "page_count": outcome.page_count,
        "stats": outcome.job.stats or {},
    }
