"""The ingestion pipeline: raw file → document → pages → regions → chunks → records → evidence.

One method, :meth:`IngestionPipeline.ingest`, executes the whole chain and writes an
``IngestionJob`` row describing exactly what happened. The order matters:

1. **hash, then decide identity** — the same bytes are one blob and one raw artefact; whether they
   become a *new document* depends on the scope they are filed against, not on the bytes alone
   (``documents.identity``). The same report filed against two wells is two documents sharing one
   artefact, and re-filing it against the same well returns the document that already exists;
2. **parse** — format parser produces pages and regions (tables keep their grid and coordinates);
3. **extract and merge metadata** — deterministic extractors produce typed records (numbers, never
   prose), and any report date is merged before dependent rows are persisted;
4. **chunk** — retrievable passages carry the id of their region plus explicit depth and period
   metadata, so retrieval can filter by well, section, depth or date rather than relying on embeddings;
5. **link provenance** — every record carries its page *and its region* where the extractor could
   name one, every record gets an :class:`EvidenceLink` back to that source with the quote checked
   against the text it claims to quote, and the document is linked to the well it belongs to;
6. **record the job** — status, statistics, extractor versions, warnings and errors. A failed
   ingestion leaves the raw artefact and the job row behind (evidence is never lost), and the
   document is marked ``failed`` with the reason.

The status written onto the document is computed by ``documents.lifecycle.plan_document_status``
rather than chosen here, so "extracted" is never claimed while an extractor is known to have failed.

The pipeline is synchronous in-process. It is written so that a worker/queue can call the same
method later: no request-scoped state is used, and the caller owns the transaction.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import math
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import ensure_utc, parse_iso_datetime_utc, utc_now
from drillai.core.errors import ExtractionFailed, IngestionError, UnsupportedFormat, ValidationFailed
from drillai.core.logging import get_logger
from drillai.db.models import (
    Document,
    DocumentChunk,
    DocumentPage,
    DocumentRegion,
    EvidenceLink,
    ExtractedRecord,
    IngestionJob,
    RawArtifact,
)
from drillai.documents.identity import find_logical_document, logical_key
from drillai.documents.lifecycle import plan_document_status
from drillai.documents.quotes import verify_quote
from drillai.ingestion.extractors import ExtractedRecordDraft, extract, extractor_catalogue
from drillai.ingestion.parsers import ParsedDocument, guess_doc_type, parse_bytes
from drillai.ingestion.storage import BlobStore, compute_sha256
from drillai.observability.tracing import get_metrics, get_tracer

logger = get_logger(__name__)


def _is_named_unique_conflict(
    exc: IntegrityError, *, constraint_name: str, sqlite_columns: str
) -> bool:
    """Match a known unique index across PostgreSQL and SQLite error representations."""
    original = getattr(exc, "orig", exc)
    reported_name = getattr(original, "constraint_name", None)
    if reported_name is None:
        reported_name = getattr(getattr(original, "diag", None), "constraint_name", None)
    if reported_name == constraint_name:
        return True
    message = str(original).casefold()
    return constraint_name.casefold() in message or f"unique constraint failed: {sqlite_columns}" in message


def _is_raw_artifact_unique_conflict(exc: IntegrityError) -> bool:
    """Recognize the content-address uniqueness race, not arbitrary integrity failures."""
    return _is_named_unique_conflict(
        exc,
        constraint_name="uq_raw_artifacts_org_sha256",
        sqlite_columns="raw_artifacts.org_id, raw_artifacts.sha256",
    )


def _is_logical_document_unique_conflict(exc: IntegrityError) -> bool:
    """Recognize the logical-document uniqueness race, not arbitrary integrity failures."""
    return _is_named_unique_conflict(
        exc,
        constraint_name="uq_documents_org_logical_key",
        sqlite_columns="documents.org_id, documents.logical_key",
    )


def _prepare_identity_parse(
    data: bytes,
    *,
    filename: str,
    content_type: str | None,
    requested_doc_type: str | None,
) -> tuple[str, ParsedDocument | None, Exception | None]:
    """Resolve auto-classified identity before hashing the logical document key.

    Persisted keys use the document's final type, so an omitted/``other`` request must run the same
    deterministic parser classifier before deduplication. Keep the parse result (or failure) so this
    preflight does not parse successful uploads twice and parser failures can still be recorded.
    """
    if requested_doc_type not in (None, "", "other"):
        return requested_doc_type, None, None
    try:
        parsed = parse_bytes(data, filename=filename, content_type=content_type)
    except Exception as exc:
        # Defer the error until the normal failure-record path has created the durable job/document.
        return "other", None, exc
    return guess_doc_type(parsed, filename), parsed, None


__all__ = ["IngestionOutcome", "IngestionPipeline", "build_chunks", "normalize_text"]

#: Chunk sizing for retrieval. Overlap keeps a sentence that straddles a boundary retrievable.
CHUNK_TARGET_CHARS = 1200
CHUNK_OVERLAP_CHARS = 200
CHUNK_MIN_CHARS = 80

_WHITESPACE = re.compile(r"[ \t\u00a0]+")


def normalize_text(text: str) -> str:
    """Collapse horizontal whitespace but keep line *and paragraph* structure.

    Tables depend on line breaks and chunking depends on blank lines between paragraphs, so a
    naive "drop empty lines" pass would glue a whole report into one paragraph.
    """
    lines = [_WHITESPACE.sub(" ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    previous_blank = False
    for line in lines:
        if line:
            out.append(line)
            previous_blank = False
        elif not previous_blank:
            out.append("")
            previous_blank = True
    return "\n".join(out).strip("\n")


def _valid_si_depth(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(numeric) and numeric >= 0.0


@dataclass
class _ChunkDraft:
    text: str
    page_number: int
    region_id: str | None
    kind: str
    char_start: int
    char_end: int
    depth_from_si: float | None = None
    depth_to_si: float | None = None


def build_chunks(
    text: str,
    *,
    page_number: int,
    region_id: str | None,
    kind: str = "text",
    target_chars: int = CHUNK_TARGET_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
    depth_from_si: float | None = None,
    depth_to_si: float | None = None,
) -> list[_ChunkDraft]:
    """Split text into bounded, overlapping chunks while preferring paragraph/word boundaries.

    A single long paragraph used to bypass the target entirely because the previous chunker only
    split when there was already a buffer. A 64 MiB text upload could therefore become one 64 MiB
    retrieval result. The sliding window below bounds every chunk even when the source has no
    whitespace at all.
    """
    if target_chars < CHUNK_MIN_CHARS:
        raise ValueError(f"target_chars must be at least {CHUNK_MIN_CHARS}")
    if overlap_chars < 0 or overlap_chars >= target_chars:
        raise ValueError("overlap_chars must be non-negative and smaller than target_chars")
    for name, value in (("depth_from_si", depth_from_si), ("depth_to_si", depth_to_si)):
        if value is not None and not _valid_si_depth(value):
            raise ValueError(f"{name} must be a finite non-negative number")
    if depth_from_si is not None and depth_to_si is not None and depth_from_si > depth_to_si:
        raise ValueError("depth_from_si must not exceed depth_to_si")
    text = normalize_text(text)
    if len(text) < CHUNK_MIN_CHARS:
        return []

    chunks: list[_ChunkDraft] = []
    start = 0
    while start < len(text):
        end = min(start + target_chars, len(text))
        if end < len(text):
            floor = start + max(CHUNK_MIN_CHARS, target_chars // 2)
            boundaries = (
                text.rfind("\n\n", floor, end),
                text.rfind("\n", floor, end),
                text.rfind(" ", floor, end),
            )
            boundary = max(boundaries)
            if boundary > start:
                end = boundary

        raw_chunk = text[start:end]
        leading = len(raw_chunk) - len(raw_chunk.lstrip())
        trailing = len(raw_chunk.rstrip())
        chunk_text = raw_chunk.strip()
        chunk_start = start + leading
        chunk_end = start + trailing
        if len(chunk_text) >= CHUNK_MIN_CHARS:
            chunks.append(
                _ChunkDraft(
                    text=chunk_text,
                    page_number=page_number,
                    region_id=region_id,
                    kind=kind,
                    char_start=chunk_start,
                    char_end=chunk_end,
                    depth_from_si=depth_from_si,
                    depth_to_si=depth_to_si,
                )
            )
        if end >= len(text):
            break

        next_start = max(start + 1, end - overlap_chars)
        if next_start < end:
            next_whitespace = re.search(r"\s", text[next_start:end])
            if next_whitespace is not None:
                next_start += next_whitespace.end()
        start = next_start
    return chunks


@dataclass
class IngestionOutcome:
    job: IngestionJob
    document: Document | None
    raw_artifact: RawArtifact
    parsed: ParsedDocument | None = None
    draft_records: list[ExtractedRecordDraft] = field(default_factory=list)
    record_ids: list[str] = field(default_factory=list)
    evidence_link_ids: list[str] = field(default_factory=list)
    page_count: int = 0
    region_count: int = 0
    chunk_count: int = 0
    reused_existing: bool = False

    @property
    def stats(self) -> dict[str, int]:
        return {
            "pages": self.page_count,
            "regions": self.region_count,
            "chunks": self.chunk_count,
            "records": len(self.record_ids),
            "evidence_links": len(self.evidence_link_ids),
        }


class IngestionPipeline:
    """Executes the ingestion chain against one session."""

    def __init__(
        self,
        session: AsyncSession,
        store: BlobStore,
        *,
        org_id: str,
        extractor_version_map: dict[str, str] | None = None,
    ) -> None:
        self.session = session
        self.store = store
        self.org_id = org_id
        self.extractor_versions = extractor_version_map or {
            item["key"]: item["version"] for item in extractor_catalogue()
        }

    async def ingest(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str | None = None,
        project_id: str | None = None,
        well_id: str | None = None,
        wellbore_id: str | None = None,
        # Section scope matters: chunks and extracted records inherit it, which is what keeps an
        # 8-1/2" question from being answered with 12-1/4" content later on.
        section_id: str | None = None,
        operation_id: str | None = None,
        doc_type: str | None = None,
        title: str | None = None,
        #: The revision printed on the document. Part of the logical identity: a revised report is a
        #: new document rather than an overwrite of the one it revises.
        revision: str | None = None,
        period_start: dt.datetime | None = None,
        period_end: dt.datetime | None = None,
        trigger: str = "upload",
        triggered_by: str | None = None,
        trace_id: str | None = None,
        link_to_well: bool = True,
        commit: bool = False,
    ) -> IngestionOutcome:
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

        tracer = get_tracer()
        metrics = get_metrics()
        effective_trace_id = trace_id
        try:
            with tracer.span(
                "ingestion.run",
                attributes={"document.filename": filename, "well.id": well_id, "org.id": self.org_id},
            ) as span:
                effective_trace_id = trace_id or span.trace.trace_id
                outcome = await self._ingest_inner(
                    data,
                    filename=filename,
                    content_type=content_type,
                    project_id=project_id,
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                    section_id=section_id,
                    operation_id=operation_id,
                    doc_type=doc_type,
                    title=title,
                    revision=revision,
                    period_start=period_start,
                    period_end=period_end,
                    trigger=trigger,
                    triggered_by=triggered_by,
                    trace_id=effective_trace_id,
                    link_to_well=link_to_well,
                    commit=commit,
                )
                span.set_attributes(
                    {
                        "document.id": outcome.document.id if outcome.document else None,
                        "document.page_count": outcome.page_count,
                        "document.record_count": len(outcome.record_ids),
                        "document.status": outcome.job.status,
                    }
                )
                if outcome.job.error:
                    span.record_error(outcome.job.error)
            if commit:
                await self.session.commit()
        except IntegrityError as exc:
            if not commit:
                raise
            raw_artifact_conflict = _is_raw_artifact_unique_conflict(exc)
            logical_document_conflict = _is_logical_document_unique_conflict(exc)
            await self.session.rollback()
            if not (raw_artifact_conflict or logical_document_conflict):
                raise
            outcome = await self._recover_duplicate_upload(
                data,
                filename=filename,
                content_type=content_type,
                project_id=project_id,
                well_id=well_id,
                wellbore_id=wellbore_id,
                section_id=section_id,
                operation_id=operation_id,
                doc_type=doc_type,
                title=title,
                revision=revision,
                period_start=period_start,
                period_end=period_end,
                trigger=trigger,
                triggered_by=triggered_by,
                trace_id=effective_trace_id,
                link_to_well=link_to_well,
                raw_artifact_conflict=raw_artifact_conflict,
            )
            if outcome is None:
                raise exc
        metrics.counter("drillai.ingestion.jobs", "ingestion jobs").inc(1, status=outcome.job.status)
        if outcome.job.duration_ms:
            metrics.histogram("drillai.ingestion.duration_ms", "ingestion duration").observe(
                outcome.job.duration_ms, doc_type=outcome.document.doc_type if outcome.document else "unknown"
            )
        return outcome

    async def _recover_duplicate_upload(
        self,
        data: bytes,
        *,
        filename: str,
        content_type: str | None,
        project_id: str | None,
        well_id: str | None,
        wellbore_id: str | None,
        section_id: str | None,
        operation_id: str | None,
        doc_type: str | None,
        title: str | None,
        revision: str | None,
        period_start: dt.datetime | None,
        period_end: dt.datetime | None,
        trigger: str,
        triggered_by: str | None,
        trace_id: str | None,
        link_to_well: bool,
        raw_artifact_conflict: bool = False,
    ) -> IngestionOutcome | None:
        """Resolve a concurrent upload only after the database confirms its logical winner."""
        digest = compute_sha256(data)
        identity_doc_type, _parsed, _parse_error = _prepare_identity_parse(
            data,
            filename=filename,
            content_type=content_type,
            requested_doc_type=doc_type,
        )
        key = logical_key(
            content_sha256=digest,
            section_id=section_id,
            wellbore_id=wellbore_id,
            well_id=well_id,
            project_id=project_id,
            doc_type=identity_doc_type,
            revision=revision,
            operation_id=operation_id,
        )
        if key is None:
            return None
        existing_document = await find_logical_document(
            self.session, org_id=self.org_id, logical_key_value=key
        )
        if existing_document is not None and existing_document.raw_artifact_id:
            prior_artifact = await self.session.get(RawArtifact, existing_document.raw_artifact_id)
            if prior_artifact is not None and prior_artifact.is_quarantined:
                raise ValidationFailed("the uploaded content is quarantined and cannot be ingested")
        if existing_document is None:
            if not raw_artifact_conflict:
                return None
            # Identical bytes may legitimately be filed to another well, operation or revision. The
            # unique raw-artifact race is not a duplicate-document race: after rollback, the winning
            # artifact is visible, so rerun the requested logical filing against it.
            raw = (
                await self.session.execute(
                    select(RawArtifact).where(RawArtifact.org_id == self.org_id, RawArtifact.sha256 == digest)
                )
            ).scalar_one_or_none()
            if raw is None:
                return None
            if raw.is_quarantined:
                raise ValidationFailed("the uploaded content is quarantined and cannot be ingested")
            return await self.ingest(
                data,
                filename=filename,
                content_type=content_type,
                project_id=project_id,
                well_id=well_id,
                wellbore_id=wellbore_id,
                section_id=section_id,
                operation_id=operation_id,
                doc_type=identity_doc_type,
                title=title,
                revision=revision,
                period_start=period_start,
                period_end=period_end,
                trigger=trigger,
                triggered_by=triggered_by,
                trace_id=trace_id,
                link_to_well=link_to_well,
                commit=True,
            )
        if existing_document.status == "failed":
            # A committed parse failure has no reusable output. Use the regular failed-document retry
            # path after the winning transaction is visible; its artifact-count check prevents appending
            # to a partial document.
            return await self.ingest(
                data,
                filename=filename,
                content_type=content_type,
                project_id=project_id,
                well_id=well_id,
                wellbore_id=wellbore_id,
                section_id=section_id,
                operation_id=operation_id,
                doc_type=identity_doc_type,
                title=title,
                revision=revision,
                period_start=period_start,
                period_end=period_end,
                trigger=trigger,
                triggered_by=triggered_by,
                trace_id=trace_id,
                link_to_well=link_to_well,
                commit=True,
            )
        raw = await self.session.get(RawArtifact, existing_document.raw_artifact_id)
        if raw is None or raw.org_id != self.org_id:
            return None
        job = await self._record_job(
            raw=raw,
            document=existing_document,
            status="skipped_duplicate",
            started=utc_now(),
            stats={"deduplicated": 1, "concurrent_conflict": 1},
            warnings=["a concurrent upload filed the same logical document; the committed document was reused"],
            trigger=trigger,
            triggered_by=triggered_by,
            trace_id=trace_id,
            project_id=existing_document.project_id or project_id,
            well_id=existing_document.well_id or well_id,
        )
        await self.session.commit()
        return IngestionOutcome(job=job, document=existing_document, raw_artifact=raw, reused_existing=True)

    async def _ingest_inner(self, data: bytes, **kwargs: Any) -> IngestionOutcome:
        started = utc_now()
        filename: str = kwargs["filename"]
        content_type: str | None = kwargs.get("content_type")
        project_id = kwargs.get("project_id")
        well_id = kwargs.get("well_id")
        wellbore_id = kwargs.get("wellbore_id")
        section_id = kwargs.get("section_id")
        operation_id = kwargs.get("operation_id")
        trigger = kwargs.get("trigger", "upload")
        triggered_by = kwargs.get("triggered_by")
        trace_id = kwargs.get("trace_id")

        if not data:
            raise IngestionError("refusing to ingest an empty file", details={"filename": filename})

        digest = compute_sha256(data)
        existing = (
            await self.session.execute(
                select(RawArtifact).where(RawArtifact.org_id == self.org_id, RawArtifact.sha256 == digest).limit(1)
            )
        ).scalars().first()
        if existing is not None and existing.is_quarantined:
            raise ValidationFailed("the uploaded content is quarantined and cannot be ingested")
        identity_doc_type, parsed_for_identity, deferred_parse_error = _prepare_identity_parse(
            data,
            filename=filename,
            content_type=content_type,
            requested_doc_type=kwargs.get("doc_type"),
        )

        # The bytes are one artefact; the *document* is only a repeat if the scope, type and revision
        # match too. Keying the repeat on the bytes alone returned another well's document — see
        # `documents.identity`, which explains the four cases this has to tell apart.
        fingerprint = logical_key(
            content_sha256=digest,
            section_id=section_id,
            wellbore_id=wellbore_id,
            well_id=well_id,
            project_id=project_id,
            doc_type=identity_doc_type,
            revision=kwargs.get("revision"),
            operation_id=operation_id,
        )
        retry_document: Document | None = None
        if existing is not None and fingerprint is not None:
            existing_document = await find_logical_document(
                self.session, org_id=self.org_id, logical_key_value=fingerprint
            )
            if existing_document is not None and existing_document.status == "failed":
                # A parse failure occurs before structure/record persistence. Re-run only when that
                # invariant is true; never append a second copy of a partially persisted document.
                artifact_counts = []
                for model in (
                    DocumentPage,
                    DocumentRegion,
                    DocumentChunk,
                    ExtractedRecord,
                    EvidenceLink,
                ):
                    artifact_counts.append(
                        (
                            await self.session.execute(
                                select(func.count()).select_from(model).where(model.document_id == existing_document.id)
                            )
                        ).scalar_one()
                    )
                if not any(artifact_counts):
                    retry_document = existing_document
                else:
                    job = await self._record_job(
                        raw=existing,
                        document=existing_document,
                        status="skipped_duplicate",
                        started=started,
                        stats={"deduplicated": 1, "retry_refused_due_to_persisted_artifacts": 1},
                        warnings=[
                            "prior attempt failed after source artifacts were persisted; automatic retry "
                            "was refused to prevent duplicate pages, chunks or records"
                        ],
                        trigger=trigger,
                        triggered_by=triggered_by,
                        trace_id=trace_id,
                        project_id=project_id,
                        well_id=well_id,
                    )
                    return IngestionOutcome(
                        job=job, document=existing_document, raw_artifact=existing, reused_existing=True
                    )
            elif existing_document is not None:
                job = await self._record_job(
                    raw=existing,
                    document=existing_document,
                    status="skipped_duplicate",
                    started=started,
                    stats={"deduplicated": 1},
                    warnings=[
                        "identical content was already filed against this scope; "
                        "no new document was created"
                    ],
                    trigger=trigger,
                    triggered_by=triggered_by,
                    trace_id=trace_id,
                    project_id=project_id,
                    well_id=well_id,
                )
                return IngestionOutcome(
                    job=job, document=existing_document, raw_artifact=existing, reused_existing=True
                )

        blob = self.store.put(data, content_type=content_type or "application/octet-stream")
        raw = existing or RawArtifact(
            org_id=self.org_id,
            blob_key=blob.key,
            sha256=blob.sha256,
            byte_size=blob.byte_size,
            content_type=content_type or "application/octet-stream",
            original_filename=filename,
            source_kind=trigger,
            uploaded_by=triggered_by,
            attributes={"content_length": len(data)},
        )
        if existing is None:
            self.session.add(raw)
            await self.session.flush()

        if retry_document is None:
            document = Document(
                org_id=self.org_id,
                project_id=project_id,
                well_id=well_id,
                wellbore_id=wellbore_id,
                section_id=section_id,
                operation_id=operation_id,
                raw_artifact_id=raw.id,
                doc_type=identity_doc_type,
                title=kwargs.get("title") or filename,
                revision=kwargs.get("revision"),
                period_start=kwargs.get("period_start"),
                period_end=kwargs.get("period_end"),
                # A document that is being read is *extracting* — the canonical name for that state.
                status="extracting",
                logical_key=fingerprint,
                checksum=digest,
                language=None,
            )
            self.session.add(document)
            await self.session.flush()
        else:
            document = retry_document
            document.status = "extracting"
            document.extraction_summary = None
            document.checksum = digest

        job = await self._record_job(
            raw=raw,
            document=document,
            status="running",
            started=started,
            stats={"retry_of_failed_document": 1} if retry_document is not None else {},
            warnings=(
                ["retrying parse of a previously failed document"]
                if retry_document is not None
                else []
            ),
            trigger=trigger,
            triggered_by=triggered_by,
            trace_id=trace_id,
            project_id=project_id,
            well_id=well_id,
        )

        try:
            if deferred_parse_error is not None:
                raise deferred_parse_error
            parsed = parsed_for_identity or parse_bytes(
                data, filename=filename, content_type=content_type
            )
        except (UnsupportedFormat, ExtractionFailed) as exc:
            document.status = "failed"
            document.extraction_summary = {"error": str(exc)}
            await self._finish_job(job, started, status="failed", stats={}, warnings=[], error=str(exc))
            if kwargs.get("commit"):
                await self.session.commit()
            raise
        except Exception as exc:
            document.status = "failed"
            document.extraction_summary = {"error": str(exc)}
            await self._finish_job(job, started, status="failed", stats={}, warnings=[], error=str(exc))
            if kwargs.get("commit"):
                await self.session.commit()
            raise IngestionError(f"parsing failed: {exc}", details={"filename": filename}) from exc

        doc_type = document.doc_type if document.doc_type != "other" else guess_doc_type(parsed, filename)
        document.doc_type = doc_type

        pages, regions, region_ids, page_ids = await self._persist_structure(document, parsed)
        drafts, extraction_warnings = extract(parsed, doc_type=doc_type)
        # Header dates and explicit upload windows are document metadata, so merge them before the
        # chunks and records inherit their temporal bounds.
        await self._merge_document_metadata(document, parsed, drafts)
        depth_by_region = self._depth_bounds_by_region(drafts)
        chunk_count = await self._persist_chunks(
            document,
            parsed,
            doc_type=doc_type,
            region_ids=region_ids,
            depth_by_region=depth_by_region,
        )
        record_ids, evidence_ids = await self._persist_records(
            document,
            drafts,
            parsed=parsed,
            region_ids=region_ids,
            page_ids=page_ids,
            link_to_well=kwargs.get("link_to_well", True),
        )

        warnings = [*parsed.warnings, *extraction_warnings]
        if parsed.metadata.get("ocr_required"):
            document.ocr_required = True
            warnings.append("one or more pages have no extractable text: OCR is required for those pages")
        # An extractor that raised is counted, not merely warned about: "one of four extractors
        # failed" is the difference between a complete document and a partial one, and the number is
        # what makes the partial status checkable rather than a judgement call.
        extractor_failures = sum(1 for warning in extraction_warnings if " failed: " in warning)
        outcome = IngestionOutcome(
            job=job,
            document=document,
            raw_artifact=raw,
            parsed=parsed,
            draft_records=drafts,
            record_ids=record_ids,
            evidence_link_ids=evidence_ids,
            page_count=pages,
            region_count=regions,
            chunk_count=chunk_count,
        )
        document.status = plan_document_status(
            parsed=True,
            has_records=bool(record_ids),
            has_chunks=bool(chunk_count),
            warnings=warnings,
            extractor_failures=extractor_failures,
            ocr_required=bool(parsed.metadata.get("ocr_required")),
        )
        document.extraction_summary = {
            **outcome.stats,
            "extractors": self.extractor_versions,
            "doc_type": doc_type,
            # The reconciliation block: a caller can check that the document's status follows from
            # these numbers instead of taking it on trust.
            "extractor_failures": extractor_failures,
            "warnings": len(warnings),
            "region_linked_records": sum(1 for record in drafts if record.region_index is not None),
        }
        # The job's own outcome is separate from the document's state: partial extraction is a
        # *successful* run that produced an incomplete record, and calling it "failed" would lose
        # that the pipeline did exactly what it was asked.
        await self._finish_job(
            job,
            started,
            status="partial" if document.status == "partially_extracted" else "succeeded",
            stats=outcome.stats,
            warnings=warnings,
            error=None,
        )
        return outcome

    # ------------------------------------------------------------------ steps

    async def _persist_structure(
        self, document: Document, parsed: ParsedDocument
    ) -> tuple[int, int, dict[tuple[int, int], str], dict[int, str]]:
        """Write pages and regions, returning the map that lets a value point back at its source.

        The map is keyed by ``(page_number, order_index)`` because that is what the parsers and
        extractors speak in: an :class:`ExtractedRecordDraft` carries the region's order index on its
        page, and the database needs the region's primary key. Building the translation here — where
        the region rows are actually created — is what stops the pipeline from having to look a region
        up again later, and is what was missing when ``region_id`` was written as ``NULL``.
        """
        page_ids: dict[int, str] = {}
        region_ids: dict[tuple[int, int], str] = {}
        page_text: dict[int, str] = {}
        self._region_text: dict[str, str] = {}
        self._region_text_basis: dict[str, str] = {}
        region_count = 0
        for page in parsed.pages:
            row = DocumentPage(
                org_id=self.org_id,
                document_id=document.id,
                page_number=page.page_number,
                width_pt=page.width_pt,
                height_pt=page.height_pt,
                text=page.text,
                char_count=len(page.text),
                has_tables=page.has_tables,
                has_figures=page.has_figures,
                ocr_applied=False,
                layout=page.layout,
            )
            self.session.add(row)
            await self.session.flush()
            page_ids[page.page_number] = row.id
            page_text[page.page_number] = page.text or ""
            for region in page.regions:
                region_row = DocumentRegion(
                    org_id=self.org_id,
                    document_id=document.id,
                    page_id=row.id,
                    page_number=page.page_number,
                    kind=region.kind,
                    order_index=region.order_index,
                    text=region.text,
                    bbox=region.bbox,
                    table=region.table,
                    confidence=region.confidence,
                    attributes=region.source_locator,
                    extractor=parsed.parser,
                    extractor_version=parsed.parser_version,
                )
                self.session.add(region_row)
                region_count += 1
                await self.session.flush()
                region_ids[(page.page_number, region.order_index)] = region_row.id
                if region.text is not None:
                    self._region_text[region_row.id] = region.text
                    self._region_text_basis[region_row.id] = "region_text"
                elif region.table is not None:
                    self._region_text[region_row.id] = _table_to_text(region.table)
                    self._region_text_basis[region_row.id] = "region_table"
                else:
                    # A non-text region with neither text nor a table has no verifiable quote source.
                    self._region_text[region_row.id] = ""
                    self._region_text_basis[region_row.id] = "region_unavailable"
        document.page_count = len(parsed.pages)
        document.has_tables = any(page.has_tables for page in parsed.pages)
        document.has_figures = any(page.has_figures for page in parsed.pages)
        await self.session.flush()
        # The page texts travel with the map so quote verification does not re-read the pages it just
        # wrote.
        self._page_text = page_text
        return len(parsed.pages), region_count, region_ids, page_ids

    @staticmethod
    def _depth_bounds_by_region(
        drafts: list[ExtractedRecordDraft],
    ) -> dict[tuple[int, int], tuple[float, float]]:
        """Return conservative MD ranges from explicitly SI-labelled extraction fields only.

        A generic ``depth`` value is deliberately ignored. It is not evidence that a number is
        measured depth, and raw values in ``parameter_set`` may still be in field units.
        """
        values_by_region: dict[tuple[int, int], list[float]] = {}
        for draft in drafts:
            if draft.region_index is None:
                continue
            value = draft.depth_md_si
            if value is None:
                payload = draft.payload or {}
                value = payload.get("depth_md_si")
                if value is None:
                    value = payload.get("md_si")
            if not _valid_si_depth(value):
                continue
            values_by_region.setdefault((draft.page_number, draft.region_index), []).append(float(value))
        return {
            key: (min(values), max(values))
            for key, values in values_by_region.items()
            if values
        }

    async def _persist_chunks(
        self,
        document: Document,
        parsed: ParsedDocument,
        *,
        doc_type: str,
        region_ids: dict[tuple[int, int], str],
        depth_by_region: dict[tuple[int, int], tuple[float, float]],
    ) -> int:
        """One chunk per region, carrying that region's id.

        Every chunk here is built from exactly one region — the loop is over regions — so the linkage
        is unambiguous and there is no need for a many-to-many model: a chunk that spanned two regions
        would have to answer "which one?", and today it does not span two. When a chunker that merges
        regions arrives, the primary region is the honest answer and this is where that decision will
        live.
        """
        chunk_index = 0
        document_has_period = document.period_start is not None or document.period_end is not None
        chunk_period_start = document.period_start if document_has_period else document.issue_date
        chunk_period_end = document.period_end if document_has_period else document.issue_date
        for page in parsed.pages:
            region_by_index = {region.order_index: region for region in page.regions}
            for region in page.regions:
                if region.kind not in {"text", "table"} or not (region.text or region.table):
                    continue
                text = region.text or _table_to_text(region.table)
                source_region_id = region_ids.get((page.page_number, region.order_index))
                depth_bounds = depth_by_region.get((page.page_number, region.order_index))
                for draft in build_chunks(
                    text,
                    page_number=page.page_number,
                    region_id=source_region_id,
                    kind=region.kind,
                    depth_from_si=depth_bounds[0] if depth_bounds is not None else None,
                    depth_to_si=depth_bounds[1] if depth_bounds is not None else None,
                ):
                    row = DocumentChunk(
                        org_id=self.org_id,
                        document_id=document.id,
                        region_id=source_region_id,
                        page_number=page.page_number,
                        chunk_index=chunk_index,
                        text=draft.text,
                        token_estimate=max(1, len(draft.text) // 4),
                        char_start=draft.char_start,
                        char_end=draft.char_end,
                        depth_from_si=draft.depth_from_si,
                        depth_to_si=draft.depth_to_si,
                        kind=region.kind,
                        well_id=document.well_id,
                        wellbore_id=document.wellbore_id,
                        section_id=document.section_id,
                        operation_id=document.operation_id,
                        doc_type=doc_type,
                        period_start=chunk_period_start,
                        period_end=chunk_period_end,
                        attributes={
                            "region_order": region.order_index,
                            "page_regions": len(region_by_index),
                            "source_locator": region.source_locator,
                        },
                    )
                    self.session.add(row)
                    chunk_index += 1
        await self.session.flush()
        return chunk_index

    async def _persist_records(
        self,
        document: Document,
        drafts: list[ExtractedRecordDraft],
        *,
        parsed: ParsedDocument,
        region_ids: dict[tuple[int, int], str],
        page_ids: dict[int, str],
        link_to_well: bool,
    ) -> tuple[list[str], list[str]]:
        """Persist extracted records and the evidence link that anchors each one to its source.

        Where the extractor named a region, the record and its evidence link carry that region's id.
        Where it could not — some extractors read the page as a whole rather than a region — the record
        is marked ``region_unknown`` instead of being given a plausible-looking region it did not come
        from. A fabricated region is worse than an absent one: it survives review, because it looks
        exactly like a real one.
        """
        ids: list[str] = []
        evidence_ids: list[str] = []
        for draft in drafts:
            fingerprint = _record_fingerprint(document.id, draft)
            region_id = (
                region_ids.get((draft.page_number, draft.region_index))
                if draft.region_index is not None
                else None
            )
            record = ExtractedRecord(
                org_id=self.org_id,
                document_id=document.id,
                region_id=region_id,
                region_unknown=region_id is None,
                page_number=draft.page_number,
                project_id=document.project_id,
                well_id=document.well_id,
                wellbore_id=document.wellbore_id,
                section_id=document.section_id,
                operation_id=document.operation_id,
                record_type=draft.record_type,
                payload_schema_key=draft.payload_schema_key,
                payload_schema_version=draft.payload_schema_version,
                payload=draft.payload,
                observed_at=draft.observed_at or document.period_start or document.issue_date,
                depth_md_si=draft.depth_md_si,
                depth_tvd_si=draft.depth_tvd_si,
                method=draft.method,
                method_version=draft.method_version,
                confidence=draft.confidence,
                # "A machine wrote this and nothing has checked it." The old value here was
                # `unvalidated`, which appears in no vocabulary — see `documents.lifecycle`.
                validation_state="extracted",
                fingerprint=fingerprint,
                unit_context=draft.unit_context,
                quality_flags=draft.quality_flags,
            )
            self.session.add(record)
            await self.session.flush()
            ids.append(record.id)
            if link_to_well or document.well_id:
                excerpt = (draft.excerpt or "")[:2000] or None
                region_text, page_text, region_basis = self._quote_sources(region_id, draft.page_number)
                verified, quote_check = verify_quote(
                    excerpt=excerpt,
                    region_text=region_text,
                    page_text=page_text,
                    region_basis=region_basis,
                )
                page_id = page_ids.get(draft.page_number)
                if region_id is not None:
                    evidence_kind, evidence_id = "document_region", region_id
                elif page_id is not None:
                    evidence_kind, evidence_id = "document_page", page_id
                else:
                    # A malformed extractor locator must not make a page edge point at a blob id.
                    evidence_kind, evidence_id = "document", document.id
                field_locations = draft.field_locations or {}
                all_field_locators = [
                    locator
                    for locator in field_locations.values()
                    if isinstance(locator, dict)
                ]
                has_cell_locator = any(
                    locator.get("cell")
                    or (
                        locator.get("format") in {"csv", "tsv", "xlsx", "docx"}
                        and locator.get("column") is not None
                    )
                    for locator in all_field_locators
                )
                if has_cell_locator:
                    precision = "cell"
                elif draft.source_locator.get("kind") == "table_row":
                    precision = "row"
                elif region_id is not None:
                    precision = "region"
                elif page_id is not None:
                    precision = "page"
                else:
                    precision = "document"
                link = EvidenceLink(
                    org_id=self.org_id,
                    subject_kind=f"extracted_record.{draft.record_type}",
                    subject_id=record.id,
                    evidence_kind=evidence_kind,
                    evidence_id=evidence_id,
                    document_id=document.id,
                    region_id=region_id,
                    page_number=draft.page_number if page_id is not None else None,
                    well_id=document.well_id,
                    locator={
                        "page": draft.page_number,
                        "page_id": page_id,
                        "region_id": region_id,
                        "region_order": draft.region_index,
                        "parser": parsed.parser,
                        "record_type": draft.record_type,
                        "source": draft.source_locator,
                        "field_locations": field_locations,
                        "precision": precision,
                        "quote_verification_basis": quote_check,
                    },
                    excerpt=excerpt,
                    confidence=draft.confidence,
                    relevance=1.0,
                    weight=1.0,
                    method=draft.method,
                    quote_verified=verified,
                    quote_check=quote_check,
                )
                self.session.add(link)
                await self.session.flush()
                evidence_ids.append(link.id)
        await self.session.flush()
        return ids, evidence_ids

    def _quote_sources(
        self, region_id: str | None, page_number: int
    ) -> tuple[str | None, str | None, str]:
        """Return the exact region source when available, otherwise the page source.

        Table text is rendered from the parser's original cell matrix, not inferred from the page's
        CSV/XLSX presentation. The source basis is stored with the result so a cell/row quote is not
        reported as page-verified when it was checked against structured table cells.
        """
        page_text = getattr(self, "_page_text", {}).get(page_number)
        if region_id is None:
            return None, page_text, "page_text"
        region_text = getattr(self, "_region_text", {}).get(region_id, "")
        region_basis = getattr(self, "_region_text_basis", {}).get(region_id, "region_unavailable")
        return region_text, page_text, region_basis

    async def _merge_document_metadata(
        self, document: Document, parsed: ParsedDocument, drafts: list[ExtractedRecordDraft]
    ) -> None:
        for draft in drafts:
            if draft.record_type != "report_header":
                continue
            payload = draft.payload
            if payload.get("well_name_text") and not document.well_name_text:
                document.well_name_text = payload["well_name_text"]
            if payload.get("rig_name_text") and not document.rig_name_text:
                document.rig_name_text = payload["rig_name_text"]
            raw_report_date = payload.get("report_date")
            if raw_report_date and document.issue_date is None:
                with contextlib.suppress(ValueError, TypeError):  # malformed dates must not lose page text
                    document.issue_date = parse_iso_datetime_utc(
                        raw_report_date, assume_utc_for_naive=True
                    )
            # Only a document explicitly classified as a daily drilling report gets a one-day
            # reporting interval from its issue date. An explicit partial or complete upload window
            # is authoritative; never fill its missing endpoint from a potentially conflicting header.
            if (
                document.doc_type == "ddr"
                and document.issue_date is not None
                and document.period_start is None
                and document.period_end is None
            ):
                document.period_start = document.issue_date
                document.period_end = document.issue_date + dt.timedelta(days=1)
        # ``classification`` is the data-sensitivity label (internal/confidential); parser
        # provenance belongs with the rest of the extraction summary.
        document.extraction_summary = {
            **(document.extraction_summary or {}),
            "parser": parsed.parser,
            "parser_version": parsed.parser_version,
            "doc_type": document.doc_type,
            "record_types": sorted({draft.record_type for draft in drafts}),
        }

    async def _record_job(
        self,
        *,
        raw: RawArtifact,
        document: Document | None,
        status: str,
        started: dt.datetime,
        stats: dict[str, Any],
        warnings: list[str],
        trigger: str,
        triggered_by: str | None,
        trace_id: str | None,
        project_id: str | None,
        well_id: str | None,
    ) -> IngestionJob:
        job = IngestionJob(
            org_id=self.org_id,
            raw_artifact_id=raw.id,
            document_id=document.id if document else None,
            project_id=project_id,
            well_id=well_id,
            status=status,
            trigger=trigger,
            pipeline={"parser_registry": True, "extractors": self.extractor_versions},
            stats=stats,
            extractor_versions=self.extractor_versions,
            attempts=1,
            started_at=started,
            finished_at=utc_now() if status != "running" else None,
            triggered_by=triggered_by,
            trace_id=trace_id,
            warnings=warnings,
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def _finish_job(
        self,
        job: IngestionJob,
        started: dt.datetime,
        *,
        status: str,
        stats: dict[str, Any],
        warnings: list[str],
        error: str | None,
    ) -> None:
        finished = utc_now()
        job.status = status
        job.stats = stats
        job.warnings = warnings
        job.error = error
        job.finished_at = finished
        job.duration_ms = int((finished - started).total_seconds() * 1000)
        await self.session.flush()


def _table_to_text(table: dict[str, Any] | None) -> str:
    if not table:
        return ""
    rows = table.get("cells") or []
    return "\n".join(" | ".join(cell.get("text", "") for cell in row) for row in rows)


def _record_fingerprint(document_id: str, draft: ExtractedRecordDraft) -> str:
    import hashlib
    import json

    identity = json.dumps(
        {
            "type": draft.record_type,
            "payload": draft.payload,
            "region_index": draft.region_index,
            "method": draft.method,
            "method_version": draft.method_version,
            "observed_at": draft.observed_at,
            "depth_md_si": draft.depth_md_si,
            "depth_tvd_si": draft.depth_tvd_si,
            "unit_context": draft.unit_context,
            "quality_flags": draft.quality_flags,
            "excerpt": draft.excerpt,
            "payload_schema_key": draft.payload_schema_key,
            "payload_schema_version": draft.payload_schema_version,
            "source_locator": draft.source_locator,
            "field_locations": draft.field_locations,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(f"{document_id}:{draft.page_number}:{identity}".encode()).hexdigest()[:64]


async def count_document_records(session: AsyncSession, document_id: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).scalar_one()
    )
