"""Ingestion: parse → structure → chunks → records → evidence → job record.

The fixtures build real files in memory (a hand-written PDF, a python-docx document, an
openpyxl workbook, delimited text) so the parsers are exercised for real rather than mocked.
"""

from __future__ import annotations

import io

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from drillai.core.errors import IngestionError, UnsupportedFormat
from drillai.db.models import (
    Base,
    Document,
    DocumentChunk,
    DocumentPage,
    EvidenceLink,
    ExtractedRecord,
    IngestionJob,
    Organization,
    RawArtifact,
    Well,
)
from drillai.ingestion.parsers import parse_bytes
from drillai.ingestion.pipeline import IngestionPipeline, build_chunks, normalize_text

# --------------------------------------------------------------------------- fixtures


def minimal_pdf(lines: list[str] | None = None) -> bytes:
    """A valid, uncompressed one-page PDF (pypdf extracts the text)."""
    body = lines or [
        "Daily Drilling Report",
        "Well: NF-12",
        "Rig: Rig 42",
        "Mud Weight: 9.2 ppg",
        "Plastic Viscosity: 18 cp",
        "Bit Depth: 1500 m",
        "Hole Depth: 1502 m",
    ]
    content = ("BT /F1 12 Tf 72 720 Td 14 TL\n" + "\n".join(f"({line}) Tj T*" for line in body) + "\nET").encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_position = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_position}\n%%EOF\n".encode()
    return bytes(out)


def scanned_pdf() -> bytes:
    """A PDF whose page has no text operators (stands in for a scan)."""
    content = b"0.5 0.5 0.5 rg 100 100 200 200 re f"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_position = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode() + b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_position}\n%%EOF\n".encode()
    return bytes(out)


def docx_bytes() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("Drilling Program - NF-12", level=1)
    document.add_paragraph("Well: NF-12")
    document.add_paragraph("Rig: Rig 42")
    table = document.add_table(rows=3, cols=4)
    header = ["MD (m)", "Inclination (deg)", "Azimuth (deg)", "Toolface"]
    for index, value in enumerate(header):
        table.rows[0].cells[index].text = value
    rows = [["0", "0.0", "0.0", "-"], ["500", "1.5", "45.0", "20"]]
    for row_index, values in enumerate(rows, start=1):
        for column, value in enumerate(values):
            table.rows[row_index].cells[column].text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_bytes() -> bytes:
    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Mud Check"
    rows = [
        ["Time", "Mud Weight (ppg)", "Funnel Viscosity (s)", "PV (cp)", "YP (lb/100ft2)"],
        ["06:00", 9.2, 45, 18, 12],
        ["12:00", 9.4, 48, 20, 14],
    ]
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as sess:
        yield sess
    await engine.dispose()


@pytest.fixture
async def context(session: AsyncSession, tmp_path):
    org = Organization(slug="ing", name="Ingestion Co")
    session.add(org)
    await session.flush()
    from drillai.db.models import Project

    project = Project(org_id=org.id, name="Development")
    session.add(project)
    await session.flush()
    well = Well(org_id=org.id, project_id=project.id, name="NF-12")
    session.add(well)
    await session.flush()
    from drillai.ingestion.storage import LocalBlobStore

    store = LocalBlobStore(tmp_path / "blobs")
    pipeline = IngestionPipeline(session, store, org_id=org.id)
    return {"org": org, "project": project, "well": well, "store": store, "pipeline": pipeline}


# --------------------------------------------------------------------------- parsers


def test_pdf_parser_extracts_pages_and_text():
    parsed = parse_bytes(minimal_pdf(), filename="ddr.pdf")
    assert parsed.parser == "pdf"
    assert len(parsed.pages) == 1
    assert "Daily Drilling Report" in parsed.pages[0].text
    assert parsed.pages[0].width_pt == pytest.approx(612)


def test_scanned_pdf_is_flagged_not_silently_empty():
    parsed = parse_bytes(scanned_pdf(), filename="scan.pdf")
    assert parsed.metadata["ocr_required"] is True
    assert any("scanned" in warning for warning in parsed.warnings)


def test_docx_and_xlsx_and_csv_parsers():
    docx_doc = parse_bytes(docx_bytes(), filename="program.docx")
    assert any(region.kind == "table" for region in docx_doc.pages[0].regions)

    xlsx_doc = parse_bytes(xlsx_bytes(), filename="mud.xlsx")
    assert xlsx_doc.pages[0].regions[0].table["rows"] == 3

    csv_doc = parse_bytes(b"md,incl,azi\n0,0,0\n500,1.5,45\n", filename="survey.csv")
    assert csv_doc.pages[0].regions[0].table["header"] == ["md", "incl", "azi"]


def test_unsupported_format_is_rejected_explicitly():
    with pytest.raises(UnsupportedFormat):
        parse_bytes(b"\x00\x01binary", filename="mystery.bin", content_type="application/octet-stream")


def test_chunking_respects_boundaries_and_overlap():
    text = "\n\n".join(f"Paragraph {index} " + "word " * 160 for index in range(4))
    chunks = build_chunks(text, page_number=1, region_id=None)
    assert len(chunks) >= 2
    assert all(len(chunk.text) >= 80 for chunk in chunks)
    assert all(chunk.char_start >= 0 for chunk in chunks)
    assert normalize_text("a   b\t\tc") == "a b c"


# --------------------------------------------------------------------------- pipeline


async def test_pdf_ingestion_creates_full_provenance_chain(session, context):
    pipeline = context["pipeline"]
    outcome = await pipeline.ingest(
        minimal_pdf(),
        filename="ddr-2026-01-05.pdf",
        content_type="application/pdf",
        project_id=context["project"].id,
        well_id=context["well"].id,
        triggered_by="usr_1",
    )
    document = outcome.document
    assert document is not None
    assert outcome.job.status == "succeeded"
    assert document.status == "ingested"
    assert document.doc_type == "ddr"
    assert document.well_name_text == "NF-12"
    assert document.rig_name_text == "Rig 42"
    assert document.page_count == 1
    assert outcome.stats["regions"] >= 1
    assert outcome.chunk_count >= 1

    job = (await session.execute(select(IngestionJob))).scalars().one()
    assert job.id == outcome.job.id
    assert job.document_id == document.id
    assert job.duration_ms is not None
    assert job.pipeline["extractors"]["report_header"] == "1.0.0"

    pages = (await session.execute(select(DocumentPage))).scalars().all()
    assert [page.page_number for page in pages] == [1]

    chunks = (await session.execute(select(DocumentChunk))).scalars().all()
    assert all(chunk.well_id == context["well"].id for chunk in chunks)
    assert all(chunk.doc_type == "ddr" for chunk in chunks)

    records = (await session.execute(select(ExtractedRecord))).scalars().all()
    types = {record.record_type for record in records}
    assert "report_header" in types
    assert "mud_properties" in types
    assert "depth_reading" in types
    mud = next(record for record in records if record.record_type == "mud_properties")
    assert mud.payload["mud_weight"] == pytest.approx(9.2)
    assert mud.unit_context.get("mud_weight") == "ppg"
    assert mud.validation_state == "unvalidated"  # extraction is never auto-validated
    depth = next(record for record in records if record.record_type == "depth_reading")
    assert depth.payload["bit_depth_si"] == pytest.approx(1500.0)

    links = (await session.execute(select(EvidenceLink))).scalars().all()
    assert len(links) == len(records)
    assert all(link.document_id == document.id for link in links)
    assert all(link.locator.get("page") == 1 for link in links)


async def test_identical_upload_is_deduplicated(session, context):
    pipeline = context["pipeline"]
    first = await pipeline.ingest(minimal_pdf(), filename="ddr.pdf", project_id=context["project"].id)
    second = await pipeline.ingest(minimal_pdf(), filename="ddr-copy.pdf", project_id=context["project"].id)
    assert second.reused_existing is True
    assert second.job.status == "skipped_duplicate"
    assert second.document is not None and second.document.id == first.document.id
    assert len((await session.execute(select(RawArtifact))).scalars().all()) == 1
    assert len((await session.execute(select(Document))).scalars().all()) == 1


async def test_csv_survey_rows_become_records_with_depths(session, context):
    csv = b"MD (m),Inclination (deg),Azimuth (deg)\n0,0.0,0.0\n500,1.5,45.0\n1000,3.0,90.0\n"
    outcome = await context["pipeline"].ingest(
        csv,
        filename="survey.csv",
        content_type="text/csv",
        well_id=context["well"].id,
        project_id=context["project"].id,
    )
    records = (
        await session.execute(select(ExtractedRecord).where(ExtractedRecord.record_type == "survey_station"))
    ).scalars().all()
    assert [record.depth_md_si for record in records] == [500.0, 1000.0]
    assert records[0].payload["inclination_deg"] == pytest.approx(1.5)
    assert outcome.stats["records"] == len(outcome.record_ids)


async def test_docx_program_table_is_ingested(session, context):
    outcome = await context["pipeline"].ingest(
        docx_bytes(),
        filename="program.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        well_id=context["well"].id,
        project_id=context["project"].id,
    )
    assert outcome.document is not None
    assert outcome.document.has_tables is True
    records = (
        await session.execute(select(ExtractedRecord).where(ExtractedRecord.record_type == "survey_station"))
    ).scalars().all()
    assert {record.payload["md_si"] for record in records} == {500.0}


async def test_scanned_document_records_ocr_requirement(session, context):
    outcome = await context["pipeline"].ingest(
        scanned_pdf(), filename="scan.pdf", content_type="application/pdf", well_id=context["well"].id
    )
    assert outcome.document is not None
    assert outcome.document.ocr_required is True
    assert any("OCR" in warning for warning in outcome.job.warnings)
    assert outcome.document.status in {"parsed", "ingested"}


async def test_empty_file_is_refused(session, context):
    with pytest.raises(IngestionError):
        await context["pipeline"].ingest(b"", filename="empty.pdf")


async def test_failed_ingestion_still_records_evidence_and_job(session, context):
    """A corrupted PDF: the raw artefact and a failed job remain, nothing is silently dropped."""
    from drillai.core.errors import ExtractionFailed

    with pytest.raises(ExtractionFailed):
        await context["pipeline"].ingest(
            b"%PDF-1.4\nthis is not a real pdf",
            filename="broken.pdf",
            content_type="application/pdf",
            well_id=context["well"].id,
        )
    assert len((await session.execute(select(RawArtifact))).scalars().all()) == 1
    job = (await session.execute(select(IngestionJob))).scalars().one()
    assert job.status == "failed"
    assert job.error
    document = (await session.execute(select(Document))).scalars().one()
    assert document.status == "failed"


async def test_blob_is_content_addressed(session, context):
    store = context["store"]
    outcome = await context["pipeline"].ingest(minimal_pdf(), filename="ddr.pdf")
    assert outcome.raw_artifact.blob_key == f"raw/{outcome.raw_artifact.sha256[:2]}/{outcome.raw_artifact.sha256[2:]}"
    assert store.exists(outcome.raw_artifact.blob_key)
    assert store.get(outcome.raw_artifact.blob_key) == minimal_pdf()
