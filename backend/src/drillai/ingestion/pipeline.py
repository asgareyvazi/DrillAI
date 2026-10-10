"""The ingestion pipeline: raw file → document → pages → regions → chunks → records → evidence.

One method, :meth:`IngestionPipeline.ingest`, executes the whole chain and writes an
``IngestionJob`` row describing exactly what happened. The order matters:

1. **hash, then decide identity** — the same bytes are one blob and one raw artefact; whether they
   become a *new document* depends on the scope they are filed against, not on the bytes alone
   (``documents.identity``). The same report filed against two wells is two documents sharing one
   artefact, and re-filing it against the same well returns the document that already exists;
2. **parse** — format parser produces pages and regions (tables keep their grid and coordinates);
3. **chunk** — retrievable passages carrying the id of the region they came from, plus coordinates,
   depth and period metadata, so retrieval can filter by well, section, depth or date rather than
   relying on embeddings alone;
4. **extract** — deterministic extractors produce typed records (numbers, never prose);
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
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import utc_now
from drillai.core.errors import ExtractionFailed, IngestionError, UnsupportedFormat
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
) -> list[_ChunkDraft]:
    """Split text on paragraph boundaries into overlapping, retrievable chunks."""
    text = normalize_text(text)
    if len(text) < CHUNK_MIN_CHARS:
        return []
    paragraphs = [paragraph for paragraph in text.split("\n\n") if paragraph.strip()]
    chunks: list[_ChunkDraft] = []
    buffer = ""
    start = 0
    cursor = 0
    for paragraph in paragraphs:
        candidate = f"{buffer}\n\n{paragraph}" if buffer else paragraph
        if buffer and len(candidate) > target_chars:
            chunks.append(
                _ChunkDraft(text=buffer, page_number=page_number, region_id=region_id, kind=kind, char_start=start, char_end=start + len(buffer))
            )
            overlap = buffer[-overlap_chars:] if overlap_chars else ""
            buffer = f"{overlap}\n\n{paragraph}" if overlap else paragraph
            start = max(0, cursor - len(overlap)) if overlap else cursor
        else:
            buffer = candidate
        cursor += len(paragraph) + 2
    if buffer.strip():
        chunks.append(
            _ChunkDraft(text=buffer, page_number=page_number, region_id=region_id, kind=kind, char_start=start, char_end=start + len(buffer))
        )
    return [chunk for chunk in chunks if len(chunk.text) >= CHUNK_MIN_CHARS]


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
        tracer = get_tracer()
        metrics = get_metrics()
        with tracer.span(
            "ingestion.run",
            attributes={"document.filename": filename, "well.id": well_id, "org.id": self.org_id},
        ) as span:
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
                trace_id=trace_id or span.trace.trace_id,
                link_to_well=link_to_well,
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
        metrics.counter("drillai.ingestion.jobs", "ingestion jobs").inc(1, status=outcome.job.status)
        if outcome.job.duration_ms:
            metrics.histogram("drillai.ingestion.duration_ms", "ingestion duration").observe(
                outcome.job.duration_ms, doc_type=outcome.document.doc_type if outcome.document else "unknown"
            )
        if commit:
            await self.session.commit()
        return outcome

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

        # The bytes are one artefact; the *document* is only a repeat if the scope, type and revision
        # match too. Keying the repeat on the bytes alone returned another well's document — see
        # `documents.identity`, which explains the four cases this has to tell apart.
        fingerprint = logical_key(
            content_sha256=digest,
            section_id=section_id,
            wellbore_id=wellbore_id,
            well_id=well_id,
            project_id=project_id,
            doc_type=kwargs.get("doc_type"),
            revision=kwargs.get("revision"),
        )
        if existing is not None and fingerprint is not None:
            existing_document = await find_logical_document(
                self.session, org_id=self.org_id, logical_key_value=fingerprint
            )
            if existing_document is not None:
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

        document = Document(
            org_id=self.org_id,
            project_id=project_id,
            well_id=well_id,
            wellbore_id=wellbore_id,
            section_id=section_id,
            operation_id=operation_id,
            raw_artifact_id=raw.id,
            doc_type=kwargs.get("doc_type") or "other",
            title=kwargs.get("title") or filename,
            revision=kwargs.get("revision"),
            period_start=kwargs.get("period_start"),
            period_end=kwargs.get("period_end"),
            # A document that is being read is *extracting* — the canonical name for that state. The
            # old value here was `parsing`, which the model's own vocabulary did not contain.
            status="extracting",
            logical_key=fingerprint,
            checksum=digest,
            language=None,
        )
        self.session.add(document)
        await self.session.flush()

        job = await self._record_job(
            raw=raw,
            document=document,
            status="running",
            started=started,
            stats={},
            warnings=[],
            trigger=trigger,
            triggered_by=triggered_by,
            trace_id=trace_id,
            project_id=project_id,
            well_id=well_id,
        )

        try:
            parsed = parse_bytes(data, filename=filename, content_type=content_type)
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
            raise IngestionError(f"parsing failed: {exc}", details={"filename": filename}) from exc

        doc_type = document.doc_type if document.doc_type != "other" else guess_doc_type(parsed, filename)
        document.doc_type = doc_type

        pages, regions, region_ids = await self._persist_structure(document, parsed)
        chunk_count = await self._persist_chunks(
            document, parsed, doc_type=doc_type, region_ids=region_ids
        )
        drafts, extraction_warnings = extract(parsed, doc_type=doc_type)
        record_ids, evidence_ids = await self._persist_records(
            document,
            drafts,
            parsed=parsed,
            region_ids=region_ids,
            link_to_well=kwargs.get("link_to_well", True),
        )
        await self._merge_document_metadata(document, parsed, drafts)

        warnings = [*parsed.warnings, *extraction_warnings]
        if parsed.metadata.get("ocr_required"):
            document.ocr_required = True
            warnings.append("document has no extractable text: OCR is required before extraction can run")
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
    ) -> tuple[int, int, dict[tuple[int, int], str]]:
        """Write pages and regions, returning the map that lets a value point back at its source.

        The map is keyed by ``(page_number, order_index)`` because that is what the parsers and
        extractors speak in: an :class:`ExtractedRecordDraft` carries the region's order index on its
        page, and the database needs the region's primary key. Building the translation here — where
        the region rows are actually created — is what stops the pipeline from having to look a region
        up again later, and is what was missing when ``region_id`` was written as ``NULL``.
        """
        page_rows: dict[int, DocumentPage] = {}
        region_ids: dict[tuple[int, int], str] = {}
        page_text: dict[int, str] = {}
        self._region_text: dict[str, str] = {}
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
            page_rows[page.page_number] = row
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
                    extractor=parsed.parser,
                    extractor_version=parsed.parser_version,
                )
                self.session.add(region_row)
                region_count += 1
                await self.session.flush()
                region_ids[(page.page_number, region.order_index)] = region_row.id
                if region.text is not None:
                    self._region_text[region_row.id] = region.text
        document.page_count = len(parsed.pages)
        document.has_tables = any(page.has_tables for page in parsed.pages)
        document.has_figures = any(page.has_figures for page in parsed.pages)
        await self.session.flush()
        # The page texts travel with the map so quote verification does not re-read the pages it just
        # wrote.
        self._page_text = page_text
        return len(parsed.pages), region_count, region_ids

    async def _persist_chunks(
        self,
        document: Document,
        parsed: ParsedDocument,
        *,
        doc_type: str,
        region_ids: dict[tuple[int, int], str],
    ) -> int:
        """One chunk per region, carrying that region's id.

        Every chunk here is built from exactly one region — the loop is over regions — so the linkage
        is unambiguous and there is no need for a many-to-many model: a chunk that spanned two regions
        would have to answer "which one?", and today it does not span two. When a chunker that merges
        regions arrives, the primary region is the honest answer and this is where that decision will
        live.
        """
        chunk_index = 0
        for page in parsed.pages:
            region_by_index = {region.order_index: region for region in page.regions}
            for region in page.regions:
                if region.kind not in {"text", "table"} or not (region.text or region.table):
                    continue
                text = region.text or _table_to_text(region.table)
                source_region_id = region_ids.get((page.page_number, region.order_index))
                for draft in build_chunks(
                    text, page_number=page.page_number, region_id=source_region_id, kind=region.kind
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
                        kind=region.kind,
                        well_id=document.well_id,
                        wellbore_id=document.wellbore_id,
                        section_id=document.section_id,
                        operation_id=document.operation_id,
                        doc_type=doc_type,
                        period_start=document.period_start,
                        period_end=document.period_end,
                        attributes={"region_order": region.order_index, "page_regions": len(region_by_index)},
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
                observed_at=draft.observed_at or document.period_start,
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
                region_text, page_text = self._quote_sources(region_id, draft.page_number)
                verified, quote_check = verify_quote(
                    excerpt=excerpt, region_text=region_text, page_text=page_text
                )
                link = EvidenceLink(
                        org_id=self.org_id,
                        subject_kind=f"extracted_record.{draft.record_type}",
                        subject_id=record.id,
                        evidence_kind="document_region" if region_id else "document_page",
                        evidence_id=region_id or document.raw_artifact_id or document.id,
                        document_id=document.id,
                        region_id=region_id,
                        page_number=draft.page_number,
                        well_id=document.well_id,
                        locator={
                            "page": draft.page_number,
                            "region_id": region_id,
                            "region_order": draft.region_index,
                            "parser": parsed.parser,
                            "record_type": draft.record_type,
                            # Says whether the quote was checked against the region or only against
                            # the page, so a reader never has to infer the precision of the locator.
                            "precision": "region" if region_id else "page",
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

    def _quote_sources(self, region_id: str | None, page_number: int) -> tuple[str | None, str | None]:
        """The texts an excerpt is checked against: the region first, the page as the fallback.

        Both are the strings this same request just wrote, held on the pipeline instance rather than
        re-read from the database, so verification costs no extra query. The region text is returned
        as ``None`` when there is no region, which is what makes the verifier fall back to the page and
        record that it did.
        """
        page_text = getattr(self, "_page_text", {}).get(page_number)
        if region_id is None:
            return None, page_text
        region_text = getattr(self, "_region_text", {}).get(region_id)
        return region_text, page_text

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
            if payload.get("report_date") and document.issue_date is None:
                with contextlib.suppress(ValueError):  # a malformed date must not lose the page text
                    document.issue_date = dt.datetime.fromisoformat(payload["report_date"]).replace(tzinfo=dt.UTC)
            # A daily report covers one day: its report date *is* the reporting period. Deriving
            # the period here means every downstream consumer (operations, timeline, twin
            # validity, NPT attribution) has an unambiguous date without re-parsing the header.
            if document.issue_date is not None:
                if document.period_start is None:
                    document.period_start = document.issue_date
                if document.period_end is None:
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

    payload = json.dumps({"type": draft.record_type, "payload": draft.payload}, sort_keys=True, default=str)
    return hashlib.sha256(f"{document_id}:{draft.page_number}:{payload}".encode()).hexdigest()[:64]


async def count_document_records(session: AsyncSession, document_id: str) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(ExtractedRecord).where(ExtractedRecord.document_id == document_id)
            )
        ).scalar_one()
    )
