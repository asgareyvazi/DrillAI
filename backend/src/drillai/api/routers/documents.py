"""Documents and ingestion.

Upload is synchronous in this MVP on purpose: the ingestion chain is in-process and the caller
needs the outcome (status, extracted record count, evidence links) to decide what to do next. The
job row is written either way, so a future queue-backed ingest changes latency, not the contract.

The response of an upload is the *ingestion record*, never a bare "ok": document id, job id,
status, stats, warnings and the trace id that ties it to the observability stream.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, Header, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, get_blob_store, get_db, require
from drillai.api.serializers import (
    bounded_json_preview,
    document_out,
    evidence_out,
    extracted_record_out,
    ingestion_job_out,
)
from drillai.core.audit import record_audit
from drillai.core.clock import ensure_utc, utc_now
from drillai.core.config import get_settings
from drillai.core.errors import Conflict, NotFound, ValidationFailed
from drillai.core.idempotency import complete, replay_or_reserve
from drillai.db.models import (
    DOC_TYPES,
    Document,
    DocumentChunk,
    DocumentRegion,
    EvidenceLink,
    ExtractedRecord,
    IngestionJob,
    RawArtifact,
)
from drillai.documents.access import document_not_quarantined_condition
from drillai.documents.lifecycle import VALIDATION_TRANSITIONS
from drillai.documents.scope import DocumentScope, DocumentScopeResolver
from drillai.security.actions import authorize

router = APIRouter(tags=["documents"])


class ExtractedRecordReviewChanges(BaseModel):
    """Only evidence-derived values can be corrected; identity and provenance are immutable."""

    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any] | None = None
    observed_at: dt.datetime | None = None
    depth_md_si: float | None = None
    depth_tvd_si: float | None = None

    @field_validator("observed_at")
    @classmethod
    def observed_at_must_be_aware(cls, value: dt.datetime | None) -> dt.datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("observed_at must include a timezone offset")
        return value

    @field_validator("depth_md_si", "depth_tvd_si")
    @classmethod
    def depths_must_be_finite_nonnegative(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("SI depths must be finite and non-negative")
        return value


class ExtractedRecordReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1, strict=True)
    decision: Literal["validate", "reject"]
    reason: str = Field(min_length=1, max_length=2000)
    changes: ExtractedRecordReviewChanges = Field(default_factory=ExtractedRecordReviewChanges)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value.strip()


def _review_payload_hash(payload: dict[str, Any]) -> str:
    """Hash, but never copy, potentially sensitive extracted data into the governance audit."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _validate_review_value_shape(original: Any, corrected: Any, *, path: str) -> None:
    """Keep corrections within the extracted JSON shape when no schema registry exists yet."""
    if original is None:
        if isinstance(corrected, (dict, list)):
            raise ValidationFailed(f"corrected {path} must remain a scalar because its stored type is unknown")
        return
    if isinstance(original, bool):
        if not isinstance(corrected, bool):
            raise ValidationFailed(f"corrected {path} must remain boolean")
        return
    if isinstance(original, (int, float)):
        if not _is_finite_number(corrected):
            raise ValidationFailed(f"corrected {path} must remain a finite number")
        return
    if isinstance(original, str):
        if not isinstance(corrected, str):
            raise ValidationFailed(f"corrected {path} must remain text")
        return
    if isinstance(original, dict):
        if not isinstance(corrected, dict) or set(corrected) != set(original):
            raise ValidationFailed(f"corrected {path} must preserve the stored object fields")
        for key, old_value in original.items():
            _validate_review_value_shape(old_value, corrected[key], path=f"{path}.{key}")
        return
    if isinstance(original, list):
        if not isinstance(corrected, list) or len(corrected) != len(original):
            raise ValidationFailed(f"corrected {path} must preserve the stored list shape")
        for index, (old_value, new_value) in enumerate(zip(original, corrected, strict=True)):
            _validate_review_value_shape(old_value, new_value, path=f"{path}[{index}]")
        return
    raise ValidationFailed(f"stored {path} uses an unsupported correction type")


def _validate_review_payload_shape(
    original: dict[str, Any], corrected: dict[str, Any], *, record_type: str
) -> None:
    allowed_additions = (
        {"azimuth_reference", "azimuth_reference_id", "datum", "grid_correction_deg"}
        if record_type == "survey_station"
        else set()
    )
    missing = set(original) - set(corrected)
    added = set(corrected) - set(original)
    if missing or added - allowed_additions:
        raise ValidationFailed("corrected payload must preserve stored fields; only documented survey metadata may be added")
    for key, old_value in original.items():
        _validate_review_value_shape(old_value, corrected[key], path=f"payload.{key}")
    for key in added:
        _validate_review_value_shape(None, corrected[key], path=f"payload.{key}")


def _validate_review_domain_payload(record_type: str, payload: dict[str, Any]) -> None:
    if record_type == "survey_station":
        md = payload.get("md_si")
        if md is not None and (not _is_finite_number(md) or md < 0):
            raise ValidationFailed("survey station md_si must be a finite non-negative number")
        inclination = payload.get("inclination_deg")
        if inclination is not None and (
            not _is_finite_number(inclination) or not 0 <= inclination <= 180
        ):
            raise ValidationFailed("survey station inclination_deg must be between 0 and 180 degrees")
        azimuth = payload.get("azimuth_deg")
        if azimuth is not None and (not _is_finite_number(azimuth) or not 0 <= azimuth < 360):
            raise ValidationFailed("survey station azimuth_deg must be within [0, 360)")
        reference = payload.get("azimuth_reference", "unknown")
        if not isinstance(reference, str) or reference not in {"grid", "true", "magnetic", "unknown"}:
            raise ValidationFailed("survey station azimuth_reference must be grid, true, magnetic, or unknown")
        reference_id = payload.get("azimuth_reference_id")
        if reference_id is not None and (
            not isinstance(reference_id, str)
            or not reference_id.strip()
            or reference_id != reference_id.strip()
            or len(reference_id) > 120
            or not reference_id.isprintable()
        ):
            raise ValidationFailed("survey station azimuth_reference_id must be printable text up to 120 characters")
        if reference != "unknown" and reference_id is None:
            raise ValidationFailed("a known survey azimuth_reference requires azimuth_reference_id")
        datum = payload.get("datum", "unknown")
        if not isinstance(datum, str) or datum not in {"rkb", "msl", "gl", "cf", "derrick_floor", "unknown"}:
            raise ValidationFailed("survey station datum must be rkb, msl, gl, cf, derrick_floor, or unknown")
        correction = payload.get("grid_correction_deg")
        if correction is not None:
            if not _is_finite_number(correction) or not -360 <= correction <= 360:
                raise ValidationFailed("survey station grid_correction_deg must be finite and between -360 and 360")
            if reference == "unknown":
                raise ValidationFailed("grid_correction_deg requires a known source azimuth_reference")
    elif record_type == "depth_reading":
        for field_name, value in payload.items():
            if field_name.endswith("_si") and value is not None and (
                not _is_finite_number(value) or value < 0
            ):
                raise ValidationFailed(f"depth reading {field_name} must be finite and non-negative")


def _region_metadata_out(region: DocumentRegion) -> dict[str, Any]:
    bbox, bbox_truncated = bounded_json_preview(
        region.bbox, max_bytes=2048, max_depth=4, max_items=32, max_string_chars=256, max_nodes=128
    )
    attributes, attributes_truncated = bounded_json_preview(
        region.attributes, max_bytes=4096, max_depth=6, max_items=64, max_string_chars=512, max_nodes=512
    )
    return {
        "bbox": bbox,
        "bbox_truncated": bbox_truncated,
        "attributes": attributes,
        "attributes_truncated": attributes_truncated,
    }


async def _read_upload_bounded(file: UploadFile, max_bytes: int) -> bytes:
    """Read an upload incrementally and stop as soon as it exceeds the configured limit."""
    if max_bytes < 1:
        raise RuntimeError("max_upload_bytes must be positive")
    data = bytearray()
    while True:
        chunk = await file.read(min(64 * 1024, max_bytes - len(data) + 1))
        if not chunk:
            return bytes(data)
        if len(data) + len(chunk) > max_bytes:
            raise ValidationFailed(
                "upload exceeds the configured limit",
                details={"bytes": len(data) + len(chunk), "limit": max_bytes},
            )
        data.extend(chunk)


async def _require_available_document(session: AsyncSession, org_id: str, document_id: str) -> None:
    """Recheck quarantine after multi-query traversals before returning document-derived content."""
    available = (
        await session.execute(
            select(Document.id)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(
                Document.id == document_id,
                Document.org_id == org_id,
                document_not_quarantined_condition(),
            )
        )
    ).scalar_one_or_none()
    if available is None:
        raise NotFound(f"document {document_id!r} not found")


@router.get("/documents", summary="List documents")
async def list_documents(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("document.read"))],
    well_id: OptionalFilter = None,
    project_id: OptionalFilter = None,
    doc_type: OptionalFilter = None,
    doc_types: Annotated[list[str] | None, Query(description="Repeat to match any of these document types.")] = None,
    status: OptionalFilter = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    stmt = (
        select(Document)
        .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
        .where(Document.org_id == auth.org_id, document_not_quarantined_condition())
    )
    if well_id:
        stmt = stmt.where(Document.well_id == well_id)
    if project_id:
        stmt = stmt.where(Document.project_id == project_id)
    if doc_types is not None:
        if doc_type is not None:
            raise ValidationFailed("use either doc_type or repeated doc_types filters, not both")
        if not doc_types or len(doc_types) > 32 or any(not value.strip() for value in doc_types):
            raise ValidationFailed("doc_types must contain between 1 and 32 non-empty document types")
        requested_types = set(doc_types)
        invalid_types = sorted(requested_types.difference(DOC_TYPES))
        if invalid_types:
            raise ValidationFailed(
                "doc_types contains an unrecognised document type",
                details={"invalid": invalid_types, "allowed": sorted(DOC_TYPES)},
            )
        stmt = stmt.where(Document.doc_type.in_(requested_types))
    elif doc_type:
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
    file: Annotated[
        UploadFile,
        File(description="PDF, DOCX, XLSX/XLSM, CSV/TSV, TXT, Markdown or LOG report"),
    ],
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
    try:
        period_start = ensure_utc(period_start) if period_start is not None else None
        period_end = ensure_utc(period_end) if period_end is not None else None
    except ValueError as exc:
        raise ValidationFailed(
            "period_start and period_end must include a timezone offset",
            details={"reason": str(exc)},
        ) from exc
    if period_start is not None and period_end is not None and period_end < period_start:
        raise ValidationFailed("period_end must not precede period_start")
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

    settings = get_settings()
    from drillai.ingestion.parsers import MAX_BYTES

    # The parser's cap is an independent hard ceiling. Honour a stricter deployment setting while
    # avoiding the old path that buffered up to 200 MiB only to have the parser reject after the read.
    data = await _read_upload_bounded(file, min(settings.max_upload_bytes, MAX_BYTES))
    if not data:
        raise ValidationFailed("the uploaded file is empty")

    from drillai.ingestion.ocr import TesseractOcrProvider
    from drillai.ingestion.pipeline import IngestionPipeline

    ocr_provider = (
        TesseractOcrProvider(
            languages=settings.ocr_languages,
            executable=settings.ocr_executable,
            timeout_seconds=settings.ocr_timeout_seconds,
            max_concurrency=settings.ocr_max_concurrency,
        )
        if settings.ocr_enabled
        else None
    )
    pipeline = IngestionPipeline(
        session,
        get_blob_store(request),
        org_id=auth.org_id or "",
        ocr_provider=ocr_provider,
        ocr_max_pages=settings.ocr_max_pages,
        ocr_dpi=settings.ocr_dpi,
        ocr_max_page_pixels=settings.ocr_max_page_pixels,
    )
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
        # Persist the job and failed document even when the parser raises: get_db rolls back
        # unhandled request exceptions, so the pipeline must commit this failure record itself.
        commit=True,
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
    job_limit: int = Query(default=50, ge=1, le=500),
    job_offset: int = Query(default=0, ge=0),
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
            select(Document)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(
                Document.id == document_id,
                Document.org_id == auth.org_id,
                document_not_quarantined_condition(),
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFound(f"document {document_id!r} not found")

    payload: dict[str, Any] = {"document": document_out(document)}
    jobs_total = (
        await session.execute(
            select(func.count())
            .select_from(IngestionJob)
            .where(IngestionJob.document_id == document_id, IngestionJob.org_id == auth.org_id)
        )
    ).scalar_one()
    fetched_jobs = (
        await session.execute(
            select(IngestionJob)
            .where(IngestionJob.document_id == document_id, IngestionJob.org_id == auth.org_id)
            .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc())
            .limit(job_limit + 1)
            .offset(job_offset)
        )
    ).scalars().all()
    jobs_truncated = len(fetched_jobs) > job_limit
    jobs = fetched_jobs[:job_limit]
    payload["ingestion_jobs"] = [ingestion_job_out(row) for row in jobs]
    payload["ingestion_job_count"] = jobs_total
    payload["ingestion_jobs_returned"] = len(jobs)
    payload["ingestion_jobs_truncated"] = jobs_truncated
    payload["next_ingestion_job_offset"] = job_offset + len(jobs) if jobs_truncated else None
    payload["ingestion_jobs_limit"] = job_limit
    payload["ingestion_jobs_offset"] = job_offset
    regions = (
        await session.execute(
            select(func.count())
            .select_from(DocumentRegion)
            .where(DocumentRegion.document_id == document_id, DocumentRegion.org_id == auth.org_id)
        )
    ).scalar_one()
    payload["region_count"] = regions
    if include_regions:
        region_rows = (
            await session.execute(
                select(DocumentRegion)
                .where(DocumentRegion.document_id == document_id, DocumentRegion.org_id == auth.org_id)
                .order_by(DocumentRegion.page_number, DocumentRegion.id)
                .limit(region_limit)
            )
        ).scalars().all()
        payload["regions"] = [
            {
                "id": region.id,
                "page_number": region.page_number,
                "kind": region.kind,
                **_region_metadata_out(region),
                # Bounded like the provenance endpoint: a region's text is evidence, not a payload.
                "text": (region.text or "")[:2000],
                "text_truncated": len(region.text or "") > 2000,
            }
            for region in region_rows
        ]
        payload["regions_returned"] = len(region_rows)
        payload["regions_truncated"] = regions > len(region_rows)
    if include_chunks:
        chunks = (
            await session.execute(
                select(DocumentChunk)
                .where(DocumentChunk.document_id == document_id, DocumentChunk.org_id == auth.org_id)
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
                .where(ExtractedRecord.document_id == document_id, ExtractedRecord.org_id == auth.org_id)
            )
        ).scalar_one()
        statement = (
            select(ExtractedRecord)
            .where(ExtractedRecord.document_id == document_id, ExtractedRecord.org_id == auth.org_id)
            # Ordered by id, which is time-sortable, so the page is stable and the cursor can be a
            # record id rather than a computed offset that shifts when rows are written.
            .order_by(ExtractedRecord.id)
            .limit(record_limit + 1)
        )
        if record_after:
            await _require_record_cursor(session, auth.org_id, document_id, record_after)
            statement = statement.where(ExtractedRecord.id > record_after)
        else:
            statement = statement.offset(record_offset)
        fetched_records = (await session.execute(statement)).scalars().all()
        has_more_records = len(fetched_records) > record_limit
        records = fetched_records[:record_limit]
        payload["records"] = [extracted_record_out(row) for row in records]
        payload["record_count"] = record_total
        payload["records_returned"] = len(records)
        payload["records_truncated"] = has_more_records
        # The cursor is present only when another page exists, including keyset continuations.
        payload["next_record_cursor"] = records[-1].id if has_more_records and records else None
    evidence_total = (
        await session.execute(
            select(func.count())
            .select_from(EvidenceLink)
            .where(EvidenceLink.document_id == document_id, EvidenceLink.org_id == auth.org_id)
        )
    ).scalar_one()
    links = (
        await session.execute(
            select(EvidenceLink)
            .where(EvidenceLink.document_id == document_id, EvidenceLink.org_id == auth.org_id)
            .order_by(EvidenceLink.id)
            .limit(evidence_limit)
        )
    ).scalars().all()
    payload["evidence_links"] = [evidence_out(row) for row in links]
    payload["evidence_count"] = evidence_total
    payload["evidence_truncated"] = evidence_total > len(links)
    payload["limits"] = {
        "chunks": chunk_limit,
        "jobs": job_limit,
        "regions": region_limit,
        "records": record_limit,
        "evidence": evidence_limit,
    }
    await _require_available_document(session, auth.org_id, document_id)
    return payload


@router.get(
    "/documents/{document_id}/records/{record_id}",
    summary="Read one extracted record for review",
)
async def extracted_record_for_review(
    document_id: str,
    record_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("document.read"))],
) -> dict[str, Any]:
    """Return a larger, still-bounded payload preview for human review without weakening tenant scope."""
    row = (
        await session.execute(
            select(ExtractedRecord)
            .join(Document, Document.id == ExtractedRecord.document_id)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(
                ExtractedRecord.id == record_id,
                ExtractedRecord.document_id == document_id,
                ExtractedRecord.org_id == auth.org_id,
                Document.id == document_id,
                Document.org_id == auth.org_id,
                document_not_quarantined_condition(),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(f"extracted record {record_id!r} not found")
    return extracted_record_out(row, payload_max_bytes=64 * 1024)


@router.patch(
    "/documents/{document_id}/records/{record_id}/review",
    summary="Correct, validate or reject one extracted record",
)
async def review_extracted_record(
    document_id: str,
    record_id: str,
    payload: ExtractedRecordReviewRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("document.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    """Versioned human review; source identity and provenance cannot be rewritten by this route."""
    authorize(
        auth.principal,
        "document.record_review",
        context={"document_id": document_id, "record_id": record_id},
    )
    org_id = auth.org_id or ""
    scope = f"document.record_review:{document_id}:{record_id}"
    # Preserve omitted-vs-explicit-null distinctions: a cleared payload is invalid and must not replay
    # a prior successful review that omitted the payload field.
    request_body = payload.model_dump(mode="json", exclude_unset=True)
    if (
        replayed := await replay_or_reserve(
            session,
            org_id=org_id,
            key=idempotency_key,
            scope=scope,
            payload=request_body,
        )
    ) is not None:
        return replayed

    document = (
        await session.execute(
            select(Document)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(
                Document.id == document_id,
                Document.org_id == org_id,
                document_not_quarantined_condition(),
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFound(f"document {document_id!r} not found")
    if document.status == "archived":
        raise Conflict("an archived document is read-only")

    record = (
        await session.execute(
            select(ExtractedRecord)
            .where(
                ExtractedRecord.id == record_id,
                ExtractedRecord.document_id == document_id,
                ExtractedRecord.org_id == org_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if record is None:
        raise NotFound(f"extracted record {record_id!r} not found")
    if record.review_version != payload.expected_version:
        raise Conflict(
            "the extracted record changed since it was opened; reload before reviewing",
            details={"expected_version": payload.expected_version, "current_version": record.review_version},
        )

    next_state = "human_validated" if payload.decision == "validate" else "rejected"
    allowed_states = VALIDATION_TRANSITIONS.get(record.validation_state, frozenset())
    reaffirming_human_review = (
        payload.decision == "validate"
        and record.validation_state == "human_validated"
        and next_state == "human_validated"
    )
    if next_state not in allowed_states and not reaffirming_human_review:
        raise Conflict(
            f"an extracted record in state {record.validation_state!r} cannot move to {next_state!r}",
            details={"current_state": record.validation_state, "requested_state": next_state},
        )

    changed_fields = set(payload.changes.model_fields_set)
    update_values: dict[str, Any] = {
        "validation_state": next_state,
        "validation_notes": payload.reason,
        "review_version": payload.expected_version + 1,
    }
    corrected_payload = dict(record.payload or {})
    if "payload" in changed_fields:
        if payload.changes.payload is None:
            raise ValidationFailed("a corrected payload must be a JSON object, not null")
        corrected_payload = payload.changes.payload
        _validate_review_payload_shape(
            dict(record.payload or {}), corrected_payload, record_type=record.record_type
        )
        _validate_review_domain_payload(record.record_type, corrected_payload)
        try:
            encoded_payload = json.dumps(
                corrected_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, RecursionError) as exc:
            raise ValidationFailed("corrected payload must contain finite JSON values") from exc
        if len(encoded_payload) > 64 * 1024:
            raise ValidationFailed("corrected payload exceeds the 64 KiB review limit")
        update_values["payload"] = corrected_payload

        # The canonical SI columns are consumed by state/KPI code separately from the raw JSON.
        # Keep them synchronized for the two record types whose payload contract names SI depth.
        if record.record_type == "survey_station":
            depth_fields = {"md_si": "depth_md_si", "tvd_si": "depth_tvd_si"}
        elif record.record_type == "depth_reading":
            depth_fields = {"depth_md_si": "depth_md_si", "depth_tvd_si": "depth_tvd_si"}
        else:
            depth_fields = {}
        for payload_key, column_key in depth_fields.items():
            if payload_key not in corrected_payload:
                continue
            value = corrected_payload[payload_key]
            if value is not None and (not _is_finite_number(value) or value < 0):
                raise ValidationFailed(
                    f"corrected {payload_key} must be null or a finite non-negative SI value"
                )
            update_values[column_key] = float(value) if value is not None else None
            basis_field = f"{column_key.removesuffix('_si')}_basis"
            update_values[basis_field] = "operator_entered" if value is not None else "unknown"

    basis_fields = {
        "observed_at": "observed_at_basis",
        "depth_md_si": "depth_md_basis",
        "depth_tvd_si": "depth_tvd_basis",
    }
    for field_name, basis_field in basis_fields.items():
        if field_name not in changed_fields:
            continue
        value = getattr(payload.changes, field_name)
        if field_name == "observed_at" and value is not None:
            value = ensure_utc(value)
        if (
            field_name in {"depth_md_si", "depth_tvd_si"}
            and value is not None
            and (not math.isfinite(value) or value < 0)
        ):
            raise ValidationFailed(f"{field_name} must be a finite non-negative SI value")
        update_values[field_name] = value
        update_values[basis_field] = "operator_entered" if value is not None else "unknown"

    if payload.decision == "validate":
        update_values["validated_by"] = auth.principal.id
        update_values["validated_at"] = utc_now()
    else:
        update_values["validated_by"] = None
        update_values["validated_at"] = None

    previous_validation_notes = record.validation_notes or ""
    before = {
        "validation_state": record.validation_state,
        "review_version": record.review_version,
        "validation_notes": previous_validation_notes[:512],
        "validation_notes_truncated": len(previous_validation_notes) > 512,
        "payload_sha256": _review_payload_hash(record.payload or {}),
        "observed_at": record.observed_at.isoformat() if record.observed_at else None,
        "observed_at_basis": record.observed_at_basis,
        "depth_md_si": record.depth_md_si,
        "depth_md_basis": record.depth_md_basis,
        "depth_tvd_si": record.depth_tvd_si,
        "depth_tvd_basis": record.depth_tvd_basis,
    }
    result = await session.execute(
        update(ExtractedRecord)
        .where(
            ExtractedRecord.id == record_id,
            ExtractedRecord.document_id == document_id,
            ExtractedRecord.org_id == org_id,
            ExtractedRecord.review_version == payload.expected_version,
        )
        .values(**update_values)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        current_version = await session.scalar(
            select(ExtractedRecord.review_version).where(
                ExtractedRecord.id == record_id,
                ExtractedRecord.document_id == document_id,
                ExtractedRecord.org_id == org_id,
            )
        )
        if current_version is None:
            raise NotFound(f"extracted record {record_id!r} not found")
        raise Conflict(
            "the extracted record changed during review; reload before retrying",
            details={"expected_version": payload.expected_version, "current_version": current_version},
        )

    await session.refresh(record)
    after = {
        "validation_state": record.validation_state,
        "review_version": record.review_version,
        "payload_sha256": _review_payload_hash(record.payload or {}),
        "observed_at": record.observed_at.isoformat() if record.observed_at else None,
        "observed_at_basis": record.observed_at_basis,
        "depth_md_si": record.depth_md_si,
        "depth_md_basis": record.depth_md_basis,
        "depth_tvd_si": record.depth_tvd_si,
        "depth_tvd_basis": record.depth_tvd_basis,
    }
    await record_audit(
        session,
        org_id=org_id,
        action="document.record_review",
        resource_kind="extracted_record",
        resource_id=record.id,
        principal=auth.principal,
        permission="document.review",
        request_id=getattr(request.state, "request_id", None),
        project_id=record.project_id,
        well_id=record.well_id,
        before=before,
        after=after,
        details={
            "document_id": document_id,
            "decision": payload.decision,
            "changed_fields": sorted(changed_fields),
            "reason": payload.reason[:512],
            "reason_truncated": len(payload.reason) > 512,
        },
    )
    response = extracted_record_out(record)
    await complete(
        session,
        org_id=org_id,
        key=idempotency_key,
        scope=scope,
        response=response,
    )
    return response


@router.get("/documents/{document_id}/ingestion", summary="Ingestion jobs for a document")
async def document_ingestion(
    document_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("document.read"))],
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    document = (
        await session.execute(
            select(Document)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(
                Document.id == document_id,
                Document.org_id == auth.org_id,
                document_not_quarantined_condition(),
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFound(f"document {document_id!r} not found")
    total = (
        await session.execute(
            select(func.count())
            .select_from(IngestionJob)
            .where(IngestionJob.document_id == document_id, IngestionJob.org_id == auth.org_id)
        )
    ).scalar_one()
    fetched = (
        await session.execute(
            select(IngestionJob)
            .where(IngestionJob.document_id == document_id, IngestionJob.org_id == auth.org_id)
            .order_by(IngestionJob.created_at.desc(), IngestionJob.id.desc())
            .limit(limit + 1)
            .offset(offset)
        )
    ).scalars().all()
    truncated = len(fetched) > limit
    rows = fetched[:limit]
    await _require_available_document(session, auth.org_id, document_id)
    return {
        "document_id": document_id,
        "document_status": document.status,
        "items": [ingestion_job_out(row) for row in rows],
        "total": total,
        "returned": len(rows),
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
        "next_offset": offset + len(rows) if truncated else None,
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
            select(Document)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(
                Document.id == document_id,
                Document.org_id == auth.org_id,
                document_not_quarantined_condition(),
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFound(f"document {document_id!r} not found")
    total = (
        await session.execute(
            select(func.count())
            .select_from(ExtractedRecord)
            .where(ExtractedRecord.document_id == document_id, ExtractedRecord.org_id == auth.org_id)
        )
    ).scalar_one()
    if cursor:
        await _require_record_cursor(session, auth.org_id, document_id, cursor)
    statement = (
        select(ExtractedRecord)
        .where(ExtractedRecord.document_id == document_id, ExtractedRecord.org_id == auth.org_id)
        .order_by(ExtractedRecord.id)
        .limit(limit + 1)
    )
    if cursor:
        statement = statement.where(ExtractedRecord.id > cursor)
    fetched_records = list((await session.execute(statement)).scalars().all())
    has_more_records = len(fetched_records) > limit
    records = fetched_records[:limit]
    wanted = {record.region_id for record in records if record.region_id}
    regions = (
        list(
            (
                await session.execute(
                    select(DocumentRegion).where(
                        DocumentRegion.id.in_(wanted),
                        DocumentRegion.document_id == document_id,
                        DocumentRegion.org_id == auth.org_id,
                    )
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
                        **_region_metadata_out(region),
                        "text": (region.text or "")[:2000],
                        "text_truncated": len(region.text or "") > 2000,
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
    extraction_summary, extraction_summary_truncated = bounded_json_preview(
        document.extraction_summary or {}, max_bytes=8 * 1024, max_items=64, max_string_chars=2048
    )
    payload = {
        "document_id": document_id,
        "extraction_summary": extraction_summary,
        "extraction_summary_truncated": extraction_summary_truncated,
        "chain": chain,
        "regions_returned": len(region_index),
        "record_count": total,
        "records_returned": len(records),
        "next_cursor": records[-1].id if has_more_records and records else None,
        "chain_truncated": has_more_records,
    }
    await _require_available_document(session, auth.org_id, document_id)
    return payload


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

    document = (
        await session.execute(
            select(Document)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(
                Document.id == document_id,
                Document.org_id == auth.org_id,
                document_not_quarantined_condition(),
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise NotFound(f"document {document_id!r} not found")
    if not document.raw_artifact_id:
        raise NotFound("this document has no stored original file")
    artifact = (
        await session.execute(
            select(RawArtifact).where(
                RawArtifact.id == document.raw_artifact_id,
                RawArtifact.org_id == auth.org_id,
            )
        )
    ).scalar_one_or_none()
    if artifact is None:
        raise NotFound("the stored original for this document is missing")
    if artifact.is_quarantined:
        # The original may be retained for investigation, but it must not be served as ordinary evidence.
        raise NotFound("the stored original is not available")
    store = get_blob_store(request)
    if not store.exists(artifact.blob_key):
        raise NotFound("the stored original is no longer available in the blob backend")
    data = store.get(artifact.blob_key)
    await _require_available_document(session, auth.org_id, document_id)
    still_available = (
        await session.execute(
            select(RawArtifact.id).where(
                RawArtifact.id == artifact.id,
                RawArtifact.org_id == auth.org_id,
                RawArtifact.is_quarantined.is_(False),
            )
        )
    ).scalar_one_or_none()
    if still_available is None:
        raise NotFound("the stored original is not available")
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
            select(Document)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(
                Document.id == document_id,
                Document.org_id == auth.org_id,
                document_not_quarantined_condition(),
            )
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


async def _require_record_cursor(
    session: AsyncSession, org_id: str, document_id: str, cursor: str
) -> None:
    """A cursor must name a record of *this* document, or it is refused.

    An opaque string compared with ``>`` is not a cursor: whether it returns the first page, the last
    page or everything depends on how it happens to sort. Refusing it means a client that passes the
    wrong token is told so, instead of being handed a page that looks like a valid answer.
    """
    found = (
        await session.execute(
            select(ExtractedRecord.id).where(
                ExtractedRecord.id == cursor,
                ExtractedRecord.document_id == document_id,
                ExtractedRecord.org_id == org_id,
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
    # Response headers use Latin-1 on the ASGI wire; keep the fallback strictly printable ASCII and
    # leave the full UTF-8 filename to the percent-encoded filename* parameter below.
    safe = "".join(
        character
        for character in name
        if 0x20 <= ord(character) < 0x7F and character not in _HEADER_UNSAFE
    )
    safe = safe.replace(";", "_")[:150] or f"{fallback}.bin"
    encoded = quote(name, safe="")
    return f"inline; filename=\"{safe}\"; filename*=UTF-8''{encoded}"


def _job_payload(outcome: Any) -> dict[str, Any]:
    return {
        **ingestion_job_out(outcome.job),
        "page_count": outcome.page_count,
    }
