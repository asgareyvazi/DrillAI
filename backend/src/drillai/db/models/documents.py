"""Data fabric: raw artefacts, documents, multimodal regions, extracted records,
evidence links and derivation edges.

The fabric deliberately separates five layers, because collapsing them is what makes
document systems unusable for engineering:

1. **RawArtifact** — the bytes exactly as received (sha256-addressed). Immutable.
2. **Document / DocumentPage / DocumentRegion** — a *multimodal* representation: text,
   tables, figures, charts, schematics and their page coordinates. ``PDF → text`` alone
   destroys the table that holds the mud weights.
3. **DocumentChunk** — retrieval units with the engineering context tags (well, section,
   operation, depth, time) needed for context-aware retrieval.
4. **ExtractedRecord** — structured engineering facts of a declared type, each with
   method, version, confidence and validation state.
5. **EvidenceLink / Derivation** — the provenance graph: every fact can be traced back to
   a document region, a calculation or an assumption, and every derived value records its
   inputs.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, Float, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from drillai.db.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    IntType,
    JsonType,
    OrgScopedMixin,
    TextType,
    TimestampMixin,
    UtcDateTime,
)

DOC_TYPES = (
    "ddr",                  # daily drilling report
    "well_program",
    "drilling_program",
    "end_of_well_report",
    "service_report",
    "mud_report",
    "mud_program",
    "cement_report",
    "bit_record",
    "bha_record",
    "survey_report",
    "direction_report",
    "well_plan",
    "completion_program",
    "procedure",
    "standard",
    "casings_report",
    "drilling_parameter_record",
    "daily_log",
    "eowr",
    "invoice",
    "contract",
    "fax_or_letter",
    "image_or_scan",
    "other",
)
DOC_STATUSES = ("uploaded", "extracting", "extracted", "partially_extracted", "failed", "validated", "archived")
REGION_KINDS = (
    "text",
    "paragraph",
    "heading",
    "table",
    "table_row",
    "table_cell",
    "figure",
    "chart",
    "image",
    "diagram",
    "schematic",
    "form_field",
    "signature",
    "header",
    "footer",
    "caption",
    "handwriting",
)
RECORD_TYPES = (
    "drilling_parameter",
    "mud_check",
    "survey",
    "bit_record",
    "bha_run",
    "casing_tally",
    "cement_job",
    "operation_span",
    "npt_span",
    "gas_reading",
    "well_test",
    "safety_event",
    "cost_line",
    "inventory_usage",
    "formation_marker",
    "pressure_measurement",
    "daily_summary",
    "general",
)
EXTRACTION_METHODS = ("rule", "template", "table_parse", "layout_model", "llm", "ocr", "manual", "connector", "import")
VALIDATION_STATES = ("extracted", "rule_validated", "human_validated", "rejected", "superseded")

EVIDENCE_KINDS = (
    "document_region",
    "document_page",
    "document",
    "extracted_record",
    "engine_run",
    "calculation",
    "time_series_point",
    "time_series_window",
    "trajectory_station",
    "offset_well",
    "offset_comparison",
    "procedure",
    "standard",
    "lesson",
    "risk",
    "event",
    "operation",
    "inventory_item",
    "certification",
    "integration_message",
    "assumption",
    "manual_entry",
    "external_source",
)


class RawArtifact(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Immutable raw bytes with content addressing (never overwritten)."""

    __tablename__ = "raw_artifacts"
    id_prefix = "raw"

    blob_key: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    byte_size: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    content_type: Mapped[str] = mapped_column(String(160), default="application/octet-stream", nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(400))
    source_kind: Mapped[str] = mapped_column(String(40), default="upload", nullable=False, index=True)
    source_ref: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    uploaded_by: Mapped[str | None] = mapped_column(String(64))
    retention_class: Mapped[str] = mapped_column(String(40), default="standard", nullable=False)
    is_quarantined: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    scan_status: Mapped[str] = mapped_column(String(24), default="not_scanned", nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Document(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A logical document (one DDR, one well program) linked to its raw artefact."""

    __tablename__ = "documents"
    id_prefix = "doc"

    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    raw_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("raw_artifacts.id", ondelete="SET NULL"), index=True
    )
    doc_type: Mapped[str] = mapped_column(String(40), default="other", nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(400), nullable=False, index=True)
    document_number: Mapped[str | None] = mapped_column(String(120), index=True)
    revision: Mapped[str | None] = mapped_column(String(40))
    issue_date: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    period_start: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    period_end: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    author_org: Mapped[str | None] = mapped_column(String(200))
    author_name: Mapped[str | None] = mapped_column(String(200))
    well_name_text: Mapped[str | None] = mapped_column(
        String(200), comment="well name as printed, used for entity resolution and provenance"
    )
    rig_name_text: Mapped[str | None] = mapped_column(String(200))
    language: Mapped[str | None] = mapped_column(String(16))
    page_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="uploaded", nullable=False, index=True)
    extraction_summary: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    source_units: Mapped[dict] = mapped_column(
        JsonType, default=dict, nullable=False, comment="unit system detected per quantity family"
    )
    classification: Mapped[str] = mapped_column(String(32), default="internal", nullable=False)
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), index=True)
    has_tables: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_figures: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ocr_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_demo_fixture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)

    pages: Mapped[list[DocumentPage]] = relationship(
        back_populates="document", cascade="all, delete-orphan", order_by="DocumentPage.page_number"
    )


class DocumentPage(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    __tablename__ = "document_pages"
    id_prefix = "dpg"

    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_number: Mapped[int] = mapped_column(IntType, nullable=False)
    width_pt: Mapped[float | None] = mapped_column(Float)
    height_pt: Mapped[float | None] = mapped_column(Float)
    rotation: Mapped[int | None] = mapped_column(IntType)
    text: Mapped[str | None] = mapped_column(TextType)
    char_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    has_tables: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_figures: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    ocr_applied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    layout: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    image_ref: Mapped[str | None] = mapped_column(String(400), comment="blob key of rendered page image")
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)

    document: Mapped[Document] = relationship(back_populates="pages")


class DocumentRegion(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """A spatial region on a page: paragraph, table, figure, chart, schematic, form field.

    ``bbox`` is ``{"x0","y0","x1","y1"}`` in PDF points with the origin at the top-left,
    plus normalized coordinates. Tables additionally carry their parsed matrix in
    ``table`` so that cell-level evidence is possible ("the 9.2 ppg came from row 4,
    column 3 of the mud check table on page 7").
    """

    __tablename__ = "document_regions"
    id_prefix = "rgn"

    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    page_id: Mapped[str | None] = mapped_column(ForeignKey("document_pages.id", ondelete="CASCADE"), index=True)
    page_number: Mapped[int | None] = mapped_column(IntType, index=True)
    parent_region_id: Mapped[str | None] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="text", nullable=False, index=True)
    order_index: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    text: Mapped[str | None] = mapped_column(TextType)
    bbox: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    table: Mapped[dict | None] = mapped_column(JsonType)
    confidence: Mapped[float | None] = mapped_column(Float)
    extractor: Mapped[str | None] = mapped_column(String(80))
    extractor_version: Mapped[str | None] = mapped_column(String(40))
    checksum: Mapped[str | None] = mapped_column(String(64), index=True)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class DocumentChunk(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Retrieval unit with context tags and an embedding reference.

    The context tags (``well_id``, ``section_id``, ``operation_id``, ``doc_type``,
    ``depth_*``, ``period_*``) are what allow a query about "the 8½ inch section" to
    retrieve *this well's* 8½ inch content instead of every document containing "8½".
    """

    __tablename__ = "document_chunks"
    id_prefix = "chk"
    __table_args__ = (
        Index("ix_document_chunks_document_index", "document_id", "chunk_index"),
    )

    document_id: Mapped[str] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    region_id: Mapped[str | None] = mapped_column(String(64), index=True)
    page_number: Mapped[int | None] = mapped_column(IntType)
    chunk_index: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    text: Mapped[str] = mapped_column(TextType, nullable=False)
    token_estimate: Mapped[int | None] = mapped_column(IntType)
    char_start: Mapped[int | None] = mapped_column(IntType)
    char_end: Mapped[int | None] = mapped_column(IntType)
    kind: Mapped[str] = mapped_column(String(32), default="text", nullable=False)
    # --- retrieval context tags ---
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    doc_type: Mapped[str | None] = mapped_column(String(40), index=True)
    depth_from_si: Mapped[float | None] = mapped_column(Float)
    depth_to_si: Mapped[float | None] = mapped_column(Float)
    period_start: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    period_end: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    formation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    service_category: Mapped[str | None] = mapped_column(String(60), index=True)
    # --- embedding (JSON for the portable backend; pgvector keeps vectors in its own table) ---
    embedding: Mapped[list | None] = mapped_column(JsonType)
    embedding_model: Mapped[str | None] = mapped_column(String(120))
    embedding_dim: Mapped[int | None] = mapped_column(IntType)
    embedding_content_hash: Mapped[str | None] = mapped_column(String(64))
    lexical: Mapped[str | None] = mapped_column(TextType, comment="normalised text for lexical scoring")
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class ExtractedRecord(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """A structured engineering record extracted from evidence.

    ``payload`` is validated against the schema named by ``payload_schema_key``.
    ``validation_state`` distinguishes machine extraction from human confirmation —
    a recommendation built on ``extracted`` (not ``human_validated``) data must say so.
    """

    __tablename__ = "extracted_records"
    id_prefix = "rec"

    document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    region_id: Mapped[str | None] = mapped_column(String(64), index=True)
    page_number: Mapped[int | None] = mapped_column(IntType)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    record_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    payload_schema_key: Mapped[str] = mapped_column(String(120), nullable=False)
    payload_schema_version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    observed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    depth_md_si: Mapped[float | None] = mapped_column(Float)
    depth_tvd_si: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(40), default="rule", nullable=False)
    method_version: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[float | None] = mapped_column(Float)
    validation_state: Mapped[str] = mapped_column(String(32), default="extracted", nullable=False, index=True)
    validated_by: Mapped[str | None] = mapped_column(String(64))
    validated_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    validation_notes: Mapped[str | None] = mapped_column(TextType)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    unit_context: Mapped[dict] = mapped_column(
        JsonType, default=dict, nullable=False, comment="original units per field for replay/QA"
    )
    quality_flags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    engine_run_id: Mapped[str | None] = mapped_column(String(64))
    promoted_to_kind: Mapped[str | None] = mapped_column(String(40), comment="entity created from this record")
    promoted_to_id: Mapped[str | None] = mapped_column(String(64))
    is_demo_fixture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class EvidenceLink(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """An evidence edge: ``subject`` is supported by ``evidence``.

    The platform's "Why?" answer is a traversal of these edges. Confidence and relevance
    are stored per edge because the same document may be strong evidence for one claim
    and weak for another.
    """

    __tablename__ = "evidence_links"
    id_prefix = "evd"

    subject_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evidence_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    evidence_id: Mapped[str | None] = mapped_column(String(64), index=True)
    document_id: Mapped[str | None] = mapped_column(String(64), index=True)
    region_id: Mapped[str | None] = mapped_column(String(64), index=True)
    page_number: Mapped[int | None] = mapped_column(IntType)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    locator: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    excerpt: Mapped[str | None] = mapped_column(TextType)
    confidence: Mapped[float | None] = mapped_column(Float)
    relevance: Mapped[float | None] = mapped_column(Float)
    weight: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str | None] = mapped_column(String(40))
    note: Mapped[str | None] = mapped_column(TextType)
    quote_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="excerpt was verified to occur in the source region"
    )
    created_by: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Derivation(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """A dependency edge between artefacts: ``output`` was computed from ``input``.

    This is the substrate for change-impact analysis: when a trajectory changes, the
    platform walks derivation edges forward to find every affected engine result,
    recommendation and report section.
    """

    __tablename__ = "derivations"
    id_prefix = "drv"

    output_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    output_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    input_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    input_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), default="input", nullable=False)
    engine_key: Mapped[str | None] = mapped_column(String(80), index=True)
    engine_version: Mapped[str | None] = mapped_column(String(40))
    input_hash: Mapped[str | None] = mapped_column(String(64))
    output_hash: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(TextType)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class IngestionJob(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Pipeline run for one uploaded artefact: storage → extraction → records → index."""

    __tablename__ = "ingestion_jobs"
    id_prefix = "job"

    raw_artifact_id: Mapped[str | None] = mapped_column(String(64), index=True)
    document_id: Mapped[str | None] = mapped_column(String(64), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="upload", nullable=False)
    pipeline: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    stats: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    extractor_versions: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    attempts: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    started_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    duration_ms: Mapped[int | None] = mapped_column(IntType)
    error: Mapped[str | None] = mapped_column(TextType)
    triggered_by: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64))
    warnings: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
