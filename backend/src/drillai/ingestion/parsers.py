"""Format parsers: bytes in, a page/region model out.

Parsers are deliberately honest about what they can and cannot see:

* every parsed unit is a **page** with a **layout**;
* inside a page, content is expressed as **regions** — text blocks, tables and figures — each
  with an optional bounding box. Tables keep their grid (``rows``/``cols`` + ``cells`` with
  coordinates) because drilling data lives in tables and flattening them destroys meaning;
* page text is retained as well, because report readers (DDR header fields, narrative
  paragraphs) need the flow, and RAG needs something to index;
* nothing is invented: a parser that fails raises :class:`UnsupportedFormat` or
  :class:`ExtractionFailed` and the ingestion job records the failure.

Supported today (dependencies already in the project): PDF (``pypdf``), DOCX (``python-docx``),
XLSX/XLSM (``openpyxl``), CSV/TSV, and plain text/markdown. Scanned/image PDFs are detected
(no extractable text) and flagged ``ocr_required`` instead of silently returning an empty
document.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Any, BinaryIO, Protocol

from drillai.core.errors import ExtractionFailed, UnsupportedFormat

__all__ = [
    "PARSER_REGISTRY",
    "ParsedDocument",
    "ParsedPage",
    "ParsedRegion",
    "Parser",
    "detect_content_type",
    "parse_bytes",
    "parse_csv",
    "parse_docx",
    "parse_pdf",
    "parse_text",
    "parse_xlsx",
    "register_parser",
]

MAX_PAGES = 2000
MAX_BYTES = 64 * 1024 * 1024


@dataclass
class ParsedRegion:
    kind: str  # text | table | figure | header_footer
    order_index: int
    text: str | None = None
    bbox: dict[str, float] | None = None  # {x0, y0, x1, y1} in page points
    table: dict[str, Any] | None = None  # {rows, cols, cells: [[{row, col, text}]], header: [...]}
    confidence: float | None = None


@dataclass
class ParsedPage:
    page_number: int
    text: str = ""
    width_pt: float | None = None
    height_pt: float | None = None
    regions: list[ParsedRegion] = field(default_factory=list)
    has_tables: bool = False
    has_figures: bool = False
    layout: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedDocument:
    pages: list[ParsedPage]
    content_type: str
    parser: str
    parser_version: str
    metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages if page.text)


class Parser(Protocol):
    content_types: tuple[str, ...]
    name: str
    version: str

    def parse(self, data: bytes, *, filename: str) -> ParsedDocument:  # pragma: no cover - protocol
        ...


PARSER_REGISTRY: dict[str, Parser] = {}


def register_parser(parser: Parser, *, replace: bool = False) -> None:
    for content_type in parser.content_types:
        if content_type in PARSER_REGISTRY and not replace:
            raise ValueError(f"parser for {content_type!r} already registered")
        PARSER_REGISTRY[content_type] = parser


_EXTENSION_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".log": "text/plain",
}


def detect_content_type(filename: str, declared: str | None = None) -> str:
    """Trust the declared type only if it is supported; otherwise infer from the extension."""
    if declared and declared in PARSER_REGISTRY:
        return declared
    lowered = filename.lower()
    for extension, content_type in _EXTENSION_TYPES.items():
        if lowered.endswith(extension):
            return content_type
    return declared or "application/octet-stream"


def parse_bytes(data: bytes, *, filename: str, content_type: str | None = None) -> ParsedDocument:
    if not data:
        raise ExtractionFailed("file is empty")
    if len(data) > MAX_BYTES:
        raise ExtractionFailed(f"file is larger than the {MAX_BYTES // (1024 * 1024)} MiB ingestion limit")
    resolved = detect_content_type(filename, content_type)
    parser = PARSER_REGISTRY.get(resolved)
    if parser is None:
        raise UnsupportedFormat(
            f"no parser registered for {resolved!r}",
            details={"filename": filename, "supported": sorted(PARSER_REGISTRY)},
        )
    return parser.parse(data, filename=filename)


# --------------------------------------------------------------------------- text


class TextParser:
    content_types = ("text/plain", "text/markdown")
    name = "text"
    version = "1.0.0"

    def parse(self, data: bytes, *, filename: str) -> ParsedDocument:
        text = _decode(data)
        page = ParsedPage(
            page_number=1,
            text=text,
            regions=[ParsedRegion(kind="text", order_index=0, text=text, confidence=1.0)],
            layout={"source": "plain_text", "lines": text.count("\n") + 1},
        )
        return ParsedDocument(
            pages=[page], content_type="text/plain", parser=self.name, parser_version=self.version,
            metadata={"filename": filename},
        )


def parse_text(data: bytes, *, filename: str) -> ParsedDocument:
    return TextParser().parse(data, filename=filename)


# --------------------------------------------------------------------------- csv / tsv


class CsvParser:
    content_types = ("text/csv", "text/tab-separated-values")
    name = "csv"
    version = "1.0.0"

    def parse(self, data: bytes, *, filename: str) -> ParsedDocument:
        delimiter = "\t" if filename.lower().endswith(".tsv") else ","
        text = _decode(data)
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = list(reader)
        if not rows:
            raise ExtractionFailed("CSV file has no rows")
        header = rows[0]
        width = max(len(row) for row in rows)
        cells = [
            [{"row": row_index, "col": col_index, "text": value}
             for col_index, value in enumerate(row)]
            for row_index, row in enumerate(rows)
        ]
        table = {"rows": len(rows), "cols": width, "cells": cells, "header": header}
        page = ParsedPage(
            page_number=1,
            text="\n".join(delimiter.join(row) for row in rows),
            regions=[ParsedRegion(kind="table", order_index=0, table=table, confidence=1.0)],
            has_tables=True,
            layout={"source": "delimited", "delimiter": delimiter, "rows": len(rows)},
        )
        return ParsedDocument(
            pages=[page], content_type="text/csv", parser=self.name, parser_version=self.version,
            metadata={"filename": filename, "row_count": len(rows), "columns": header},
        )


def parse_csv(data: bytes, *, filename: str) -> ParsedDocument:
    return CsvParser().parse(data, filename=filename)


# --------------------------------------------------------------------------- docx


class DocxParser:
    content_types = ("application/vnd.openxmlformats-officedocument.wordprocessingml.document",)
    name = "docx"
    version = "1.0.0"

    def parse(self, data: bytes, *, filename: str) -> ParsedDocument:
        import docx  # python-docx

        try:
            document = docx.Document(io.BytesIO(data))
        except Exception as exc:
            raise ExtractionFailed(f"cannot read DOCX: {exc}") from exc

        regions: list[ParsedRegion] = []
        order = 0
        paragraphs = [paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip()]
        if paragraphs:
            regions.append(ParsedRegion(kind="text", order_index=order, text="\n".join(paragraphs), confidence=1.0))
            order += 1
        for table in document.tables:
            rows = [[cell.text.strip() for cell in row.cells] for row in table.rows]
            cells = [
                [{"row": row_index, "col": col_index, "text": value} for col_index, value in enumerate(row)]
                for row_index, row in enumerate(rows)
            ]
            regions.append(
                ParsedRegion(
                    kind="table",
                    order_index=order,
                    table={"rows": len(rows), "cols": max((len(r) for r in rows), default=0), "cells": cells,
                           "header": rows[0] if rows else []},
                    confidence=1.0,
                )
            )
            order += 1
        if not regions:
            raise ExtractionFailed("DOCX contains no readable text or tables")

        text = "\n".join(paragraphs)
        for region in regions:
            if region.kind == "table" and region.table:
                for row in region.table["cells"]:
                    text += "\n" + " | ".join(cell["text"] for cell in row)
        page = ParsedPage(
            page_number=1,
            text=text,
            regions=regions,
            has_tables=any(region.kind == "table" for region in regions),
            layout={"source": "docx", "tables": sum(1 for region in regions if region.kind == "table"), "paragraphs": len(paragraphs)},
        )
        return ParsedDocument(
            pages=[page], content_type=self.content_types[0], parser=self.name, parser_version=self.version,
            metadata={"filename": filename},
        )


def parse_docx(data: bytes, *, filename: str) -> ParsedDocument:
    return DocxParser().parse(data, filename=filename)


# --------------------------------------------------------------------------- xlsx


class XlsxParser:
    content_types = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",)
    name = "xlsx"
    version = "1.0.0"

    def parse(self, data: bytes, *, filename: str) -> ParsedDocument:
        import openpyxl

        try:
            workbook = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        except Exception as exc:
            raise ExtractionFailed(f"cannot read XLSX: {exc}") from exc

        pages: list[ParsedPage] = []
        warnings: list[str] = []
        for index, sheet in enumerate(workbook.worksheets, start=1):
            rows: list[list[str]] = []
            for row in sheet.iter_rows(values_only=True):
                if row is None:
                    continue
                values = ["" if value is None else str(value) for value in row]
                if any(value.strip() for value in values):
                    rows.append(values)
            if not rows:
                warnings.append(f"worksheet {sheet.title!r} is empty and was skipped")
                continue
            width = max(len(row) for row in rows)
            cells = [
                [{"row": row_index, "col": col_index, "text": value} for col_index, value in enumerate(row)]
                for row_index, row in enumerate(rows)
            ]
            table = {"rows": len(rows), "cols": width, "cells": cells, "header": rows[0], "sheet": sheet.title}
            text = "\n".join(" | ".join(row) for row in rows)
            pages.append(
                ParsedPage(
                    page_number=index,
                    text=f"# sheet: {sheet.title}\n{text}",
                    regions=[ParsedRegion(kind="table", order_index=0, table=table, confidence=1.0)],
                    has_tables=True,
                    layout={"source": "xlsx", "sheet": sheet.title, "rows": len(rows)},
                )
            )
        workbook.close()
        if not pages:
            raise ExtractionFailed("workbook contains no non-empty worksheets")
        return ParsedDocument(
            pages=pages, content_type=self.content_types[0], parser=self.name, parser_version=self.version,
            metadata={"filename": filename, "sheets": len(pages)}, warnings=warnings,
        )


def parse_xlsx(data: bytes, *, filename: str) -> ParsedDocument:
    return XlsxParser().parse(data, filename=filename)


# --------------------------------------------------------------------------- pdf


class PdfParser:
    content_types = ("application/pdf",)
    name = "pdf"
    version = "1.0.0"

    def parse(self, data: bytes, *, filename: str) -> ParsedDocument:
        import pypdf

        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
        except Exception as exc:
            raise ExtractionFailed(f"cannot read PDF: {exc}") from exc

        pages: list[ParsedPage] = []
        warnings: list[str] = []
        for index, page in enumerate(reader.pages[:MAX_PAGES], start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                text = ""
                warnings.append(f"page {index}: text extraction failed ({exc})")
            box = page.mediabox
            regions: list[ParsedRegion] = []
            if text.strip():
                regions.append(ParsedRegion(kind="text", order_index=0, text=text, confidence=0.9))
            pages.append(
                ParsedPage(
                    page_number=index,
                    text=text,
                    width_pt=float(box.width) if box else None,
                    height_pt=float(box.height) if box else None,
                    regions=regions,
                    layout={"source": "pdf", "extractor": "pypdf"},
                )
            )
        if len(reader.pages) > MAX_PAGES:
            warnings.append(f"document truncated to the first {MAX_PAGES} pages")
        if not any(page.text.strip() for page in pages):
            warnings.append(
                "no extractable text: the PDF is probably scanned (OCR required before extraction)"
            )
        return ParsedDocument(
            pages=pages, content_type="application/pdf", parser=self.name, parser_version=self.version,
            metadata={"filename": filename, "page_count": len(pages), "ocr_required": not any(p.text.strip() for p in pages)},
            warnings=warnings,
        )


def parse_pdf(data: bytes, *, filename: str) -> ParsedDocument:
    return PdfParser().parse(data, filename=filename)


# --------------------------------------------------------------------------- helpers


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def looks_like_scanned(document: ParsedDocument) -> bool:
    return bool(document.metadata.get("ocr_required"))


_DRILLING_HINTS = (
    "daily drilling report",
    "drilling report",
    "mud weight",
    "bit depth",
    "hole depth",
    "survey",
    "casing",
)


def guess_doc_type(document: ParsedDocument, filename: str) -> str:
    """Cheap deterministic classification used to pick extractors (LLM-free by design)."""
    haystack = f"{filename}\n{document.text[:6000]}".lower()
    if re.search(r"\bdaily (drilling )?report\b", haystack) or "ddr" in filename.lower():
        return "ddr"
    if "end of well" in haystack or "eowr" in filename.lower():
        return "eowr"
    if "drilling program" in haystack or "well program" in haystack or "programme" in haystack:
        return "program"
    if "procedure" in haystack:
        return "procedure"
    if any(hint in haystack for hint in _DRILLING_HINTS):
        return "drilling_document"
    return "other"


for _parser in (TextParser(), CsvParser(), DocxParser(), XlsxParser(), PdfParser()):
    register_parser(_parser)


def parser_catalogue() -> list[dict[str, Any]]:
    return [
        {"content_type": content_type, "parser": parser.name, "version": parser.version}
        for content_type, parser in sorted(PARSER_REGISTRY.items())
    ]


def stream_to_bytes(stream: BinaryIO) -> bytes:  # pragma: no cover - convenience
    return stream.read()
