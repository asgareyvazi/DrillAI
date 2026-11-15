"""Deterministic PDF/XLSX/JSON renderers for generic structured reports.

The API authorizes and audits exports; this module only validates a report payload and renders its
already-computed content. It performs no database reads, calculations, localization of source facts,
or network access. Facts, calculations, evidence, recommendations and assumptions remain separate
in the document layout and workbook tabs.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import threading
from collections.abc import Iterator
from importlib.resources import as_file, files
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

import arabic_reshaper
from bidi.algorithm import get_display
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

REPORT_EXPORT_MEDIA_TYPES = {
    "json": "application/json",
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
REPORT_SECTION_KINDS = ("facts", "calculations", "evidence", "recommendations", "assumptions")
MAX_REPORT_EXPORT_BYTES = 20 * 1024 * 1024
MAX_REPORT_SECTIONS = 500
MAX_REPORT_ITEMS = 10_000
MAX_XLSX_CELL_CHARS = 32_767
MAX_XLSX_FIELDS_PER_KIND = 1_000
MAX_PDF_VALUE_CHARS = 3_000
MAX_PDF_RENDER_ROWS = 100_000

_FONT_LOCK = threading.Lock()
_FONT_REGULAR = "DrillAI-Structured"
_FONT_BOLD = "DrillAI-Structured-Bold"
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")


class StructuredReportExportError(ValueError):
    """Raised when a structured report cannot be represented safely as an export."""


_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "overview": "Report overview",
        "report_title": "Report title",
        "report_kind": "Report kind",
        "well_id": "Well ID",
        "generated_at": "Generated at",
        "period_from": "Period from",
        "period_to": "Period to",
        "section_inventory": "Report sections",
        "section_key": "Section key",
        "section_title": "Section title",
        "section_kind": "Section kind",
        "section_count": "Item count",
        "section_note": "Section note",
        "record_type": "Record type",
        "item_index": "Item number",
        "no_sections": "No sections of this kind are present in this report.",
        "no_items": "No entries were recorded for this section.",
        "not_recorded": "Not recorded",
        "facts": "Facts",
        "calculations": "Calculations",
        "evidence": "Evidence",
        "recommendations": "Recommendations",
        "assumptions": "Assumptions",
        "sheet_overview": "Overview",
        "sheet_facts": "Facts",
        "sheet_calculations": "Calculations",
        "sheet_evidence": "Evidence",
        "sheet_recommendations": "Recommendations",
        "sheet_assumptions": "Assumptions",
        "record_section": "Section",
        "record_item": "Item",
        "record_empty": "Empty category",
        "page": "Page",
    },
    "fa": {
        "overview": "نمای کلی گزارش",
        "report_title": "عنوان گزارش",
        "report_kind": "نوع گزارش",
        "well_id": "شناسه چاه",
        "generated_at": "زمان ایجاد",
        "period_from": "شروع دوره",
        "period_to": "پایان دوره",
        "section_inventory": "بخش‌های گزارش",
        "section_key": "کلید بخش",
        "section_title": "عنوان بخش",
        "section_kind": "نوع بخش",
        "section_count": "تعداد موارد",
        "section_note": "یادداشت بخش",
        "record_type": "نوع ردیف",
        "item_index": "شماره مورد",
        "no_sections": "در این گزارش بخشی از این نوع وجود ندارد.",
        "no_items": "موردی برای این بخش ثبت نشده است.",
        "not_recorded": "ثبت نشده",
        "facts": "واقعیت‌ها",
        "calculations": "محاسبات",
        "evidence": "شواهد",
        "recommendations": "توصیه‌ها",
        "assumptions": "فرض‌ها و محدودیت‌ها",
        "sheet_overview": "نمای کلی",
        "sheet_facts": "واقعیت‌ها",
        "sheet_calculations": "محاسبات",
        "sheet_evidence": "شواهد",
        "sheet_recommendations": "توصیه‌ها",
        "sheet_assumptions": "فرض‌ها",
        "record_section": "بخش",
        "record_item": "مورد",
        "record_empty": "دستهٔ خالی",
        "page": "صفحه",
    },
}


def _clean_text(value: str) -> str:
    return _CONTROL_CHARS.sub("�", value)


def _normalize(value: Any) -> Any:
    """Convert supported report values to finite JSON-safe values without guessing units or types."""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise StructuredReportExportError("non-finite numbers cannot be exported")
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise StructuredReportExportError("report object keys must be strings")
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    raise StructuredReportExportError(f"unsupported report value type: {type(value).__name__}")


def _json_text(value: Any, *, pretty: bool = False) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise StructuredReportExportError("report contains a value that cannot be represented as finite JSON") from exc


def _validate_report(report: Any) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    if not isinstance(report, dict):
        raise StructuredReportExportError("report payload must be an object")
    normalized = _normalize(report)
    if not isinstance(normalized, dict):
        raise StructuredReportExportError("report payload must be an object")
    for key in ("kind", "title", "well_id"):
        if not isinstance(normalized.get(key), str) or not normalized[key].strip():
            raise StructuredReportExportError(f"report {key} must be a non-empty string")
    sections = normalized.get("sections")
    if not isinstance(sections, list):
        raise StructuredReportExportError("report sections must be a list")
    if len(sections) > MAX_REPORT_SECTIONS:
        raise StructuredReportExportError("report has too many sections to export")
    total_items = 0
    for section in sections:
        if not isinstance(section, dict):
            raise StructuredReportExportError("each report section must be an object")
        if section.get("kind") not in REPORT_SECTION_KINDS:
            raise StructuredReportExportError("report section has an unknown kind")
        if not isinstance(section.get("key"), str) or not isinstance(section.get("title"), str):
            raise StructuredReportExportError("report sections require string keys and titles")
        items = section.get("items")
        if not isinstance(items, list):
            raise StructuredReportExportError("report section items must be a list")
        if "count" in section and section["count"] != len(items):
            raise StructuredReportExportError("report section count does not match its item list")
        total_items += len(items)
        if total_items > MAX_REPORT_ITEMS:
            raise StructuredReportExportError("report has too many items to export")
    if "metadata" in normalized and not isinstance(normalized["metadata"], dict):
        raise StructuredReportExportError("report metadata must be an object")
    if len(_json_text({"report": normalized}).encode("utf-8")) > MAX_REPORT_EXPORT_BYTES:
        raise StructuredReportExportError("report export exceeds the configured 20 MiB limit")
    return normalized, sections, total_items


def _display_text(value: str, locale: str) -> str:
    text = _clean_text(value)
    if locale == "fa" or _ARABIC_SCRIPT.search(text):
        try:
            return get_display(arabic_reshaper.reshape(text), base_dir="R")
        except (TypeError, ValueError):
            return text
    return text


def _pdf_value(value: Any, locale: str) -> str:
    if value is None:
        return _LABELS[locale]["not_recorded"]
    if isinstance(value, dict | list):
        return _json_text(value, pretty=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _flatten_pdf_fields(value: Any, path: str, locale: str) -> Iterator[tuple[str, str]]:
    """Yield readable JSON-pointer paths and bounded leaf text for pagination-safe PDF tables."""
    if isinstance(value, dict):
        if not value:
            yield path, "{}"
            return
        for key in sorted(value):
            escaped_key = key.replace("~", "~0").replace("/", "~1")
            yield from _flatten_pdf_fields(value[key], f"{path}/{escaped_key}", locale)
        return
    if isinstance(value, list):
        if not value:
            yield path, "[]"
            return
        for index, item in enumerate(value):
            yield from _flatten_pdf_fields(item, f"{path}/{index}", locale)
        return
    text = _pdf_value(value, locale)
    if len(text) <= MAX_PDF_VALUE_CHARS:
        yield path, text
        return
    parts = [text[index:index + MAX_PDF_VALUE_CHARS] for index in range(0, len(text), MAX_PDF_VALUE_CHARS)]
    for index, part in enumerate(parts, 1):
        yield f"{path} (part {index}/{len(parts)})", part


def _require_pdf_fonts() -> None:
    if _FONT_REGULAR in pdfmetrics.getRegisteredFontNames() and _FONT_BOLD in pdfmetrics.getRegisteredFontNames():
        return
    with _FONT_LOCK:
        names = pdfmetrics.getRegisteredFontNames()
        if _FONT_REGULAR in names and _FONT_BOLD in names:
            return
        regular = files("drillai").joinpath("assets", "fonts", "DejaVuSans.ttf")
        bold = files("drillai").joinpath("assets", "fonts", "DejaVuSans-Bold.ttf")
        with as_file(regular) as regular_path, as_file(bold) as bold_path:
            if _FONT_REGULAR not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(_FONT_REGULAR, str(regular_path)))
            if _FONT_BOLD not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(_FONT_BOLD, str(bold_path)))


def _render_pdf(report: dict[str, Any], sections: list[dict[str, Any]], locale: str) -> bytes:
    _require_pdf_fonts()
    labels = _LABELS[locale]
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=17 * mm,
        bottomMargin=17 * mm,
        title=f"DrillAI {report['kind']} {report['well_id']}",
        author="DrillAI",
        subject="Structured well report export",
        allowSplitting=1,
    )
    align = TA_RIGHT if locale == "fa" else TA_LEFT
    base = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "StructuredReportTitle", parent=base["Title"], fontName=_FONT_BOLD, fontSize=16,
        leading=21, alignment=align, textColor=colors.HexColor("#153448"), spaceAfter=8,
    )
    category_style = ParagraphStyle(
        "StructuredReportCategory", parent=base["Heading1"], fontName=_FONT_BOLD, fontSize=12,
        leading=16, alignment=align, textColor=colors.HexColor("#153448"), spaceBefore=12,
        spaceAfter=5, keepWithNext=True,
    )
    section_style = ParagraphStyle(
        "StructuredReportSection", parent=base["Heading2"], fontName=_FONT_BOLD, fontSize=9.5,
        leading=13, alignment=align, textColor=colors.HexColor("#334e5c"), spaceBefore=7,
        spaceAfter=4, keepWithNext=True,
    )
    body_style = ParagraphStyle(
        "StructuredReportBody", parent=base["BodyText"], fontName=_FONT_REGULAR, fontSize=7.6,
        leading=10.2, alignment=align, wordWrap="CJK", allowWidows=0, allowOrphans=0,
    )
    label_style = ParagraphStyle(
        "StructuredReportLabel", parent=body_style, fontName=_FONT_BOLD,
        textColor=colors.HexColor("#334e5c"),
    )

    def paragraph(value: Any, style: ParagraphStyle = body_style) -> Paragraph:
        text = _display_text(_pdf_value(value, locale), locale)
        safe_markup = escape(text).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br/>")
        return Paragraph(safe_markup, style)

    def key_value_table(rows: list[tuple[str, Any]]) -> Table:
        rendered: list[list[Paragraph]] = []
        for key, value in rows:
            if locale == "fa":
                rendered.append([paragraph(value), paragraph(key, label_style)])
            else:
                rendered.append([paragraph(key, label_style), paragraph(value)])
        if locale == "fa":
            widths = [document.width * 0.70, document.width * 0.30]
            shaded_column = 1
        else:
            widths = [document.width * 0.30, document.width * 0.70]
            shaded_column = 0
        table = Table(
            rendered,
            colWidths=widths,
            hAlign="RIGHT" if locale == "fa" else "LEFT",
            splitInRow=1,
        )
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#d8e0e4")),
            ("BACKGROUND", (shaded_column, 0), (shaded_column, -1), colors.HexColor("#f3f6f7")),
        ]))
        return table

    overview = [
        (labels["report_kind"], report["kind"]),
        (labels["well_id"], report["well_id"]),
        (labels["generated_at"], report.get("generated_at")),
        (labels["period_from"], report.get("period_from")),
        (labels["period_to"], report.get("period_to")),
    ]
    story: list[Any] = [paragraph(report["title"], title_style), key_value_table(overview), Spacer(1, 5)]
    pdf_render_rows = 0

    for kind in REPORT_SECTION_KINDS:
        kind_sections = [section for section in sections if section["kind"] == kind]
        story.append(paragraph(labels[kind], category_style))
        if not kind_sections:
            story.append(paragraph(labels["no_sections"]))
            continue
        for section in kind_sections:
            story.append(paragraph(f"{section['title']} — {section['key']}", section_style))
            section_rows = [
                (labels["section_kind"], labels[kind]),
                (labels["section_count"], len(section["items"])),
            ]
            if section.get("note"):
                section_rows.append((labels["section_note"], section["note"]))
            story.append(key_value_table(section_rows))
            if not section["items"]:
                story.append(paragraph(labels["no_items"]))
                continue
            for item_index, item in enumerate(section["items"], 1):
                story.append(paragraph(f"{labels['item_index']} {item_index}", label_style))
                fields: list[tuple[str, str]] = []
                for field in _flatten_pdf_fields(item, "data", locale):
                    fields.append(field)
                    if pdf_render_rows + len(fields) > MAX_PDF_RENDER_ROWS:
                        raise StructuredReportExportError("report contains too many PDF rows to render safely")
                pdf_render_rows += len(fields)
                story.append(key_value_table(fields))
                story.append(Spacer(1, 3))

    def footer(canvas, page_document) -> None:
        canvas.saveState()
        canvas.setFont(_FONT_REGULAR, 7)
        text = _display_text(f"{report['kind']} · {labels['page']} {page_document.page}", locale)
        canvas.drawRightString(A4[0] - 16 * mm, 9 * mm, text)
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def _excel_cell(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        value = _json_text(value)
    elif isinstance(value, str):
        value = _clean_text(value)
    elif isinstance(value, int) and not isinstance(value, bool) and abs(value) > 999_999_999_999_999:
        # Excel only preserves 15 significant numeric digits. Keep a large identifier exact as text.
        value = str(value)
    elif isinstance(value, float):
        mantissa = repr(value).lower().split("e", 1)[0].lstrip("+-")
        significant_digits = mantissa.replace(".", "").lstrip("0").rstrip("0")
        if len(significant_digits or "0") > 15:
            # Preserve a round-trippable decimal string rather than letting Excel silently round it.
            value = repr(value)
    elif isinstance(value, bool | int):
        pass
    else:
        value = _json_text(value)
    if isinstance(value, str) and len(value) > MAX_XLSX_CELL_CHARS:
        raise StructuredReportExportError("a report field exceeds Excel's 32,767-character cell limit")
    return value


def _append_row(worksheet, values: list[Any]) -> None:
    prepared = [_excel_cell(value) for value in values]
    worksheet.append(prepared)
    for cell in worksheet[worksheet.max_row]:
        if isinstance(cell.value, str):
            # User-authored values beginning with '=' are literal report text, never formulas.
            cell.data_type = "s"


def _style_sheet(worksheet, locale: str, *, header_row: int | None = None) -> None:
    worksheet.sheet_view.rightToLeft = locale == "fa"
    worksheet.sheet_view.showGridLines = False
    worksheet.freeze_panes = f"A{header_row + 1}" if header_row is not None else "A2"
    worksheet.page_setup.fitToWidth = 1
    worksheet.page_setup.fitToHeight = 0
    worksheet.sheet_properties.pageSetUpPr.fitToPage = True
    worksheet.page_setup.orientation = "landscape"
    alignment = "right" if locale == "fa" else "left"
    thin = Side(style="thin", color="D8E0E4")
    if header_row is not None:
        for cell in worksheet[header_row]:
            cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="153448")
            cell.alignment = Alignment(horizontal=alignment, vertical="top", wrap_text=True)
            cell.border = Border(bottom=thin)
    for row in worksheet.iter_rows(min_row=(header_row or 1) + 1):
        for cell in row:
            cell.font = Font(name="Arial", size=9)
            cell.alignment = Alignment(horizontal=alignment, vertical="top", wrap_text=True)
            cell.border = Border(bottom=thin)
    for column in worksheet.columns:
        letter = get_column_letter(column[0].column)
        values = [len(str(cell.value)) for cell in column if cell.value is not None]
        worksheet.column_dimensions[letter].width = min(54, max(14, max(values, default=0) + 2))


def _render_xlsx(report: dict[str, Any], sections: list[dict[str, Any]], locale: str) -> bytes:
    labels = _LABELS[locale]
    workbook = Workbook()
    overview = workbook.active
    overview.title = labels["sheet_overview"][:31]
    _append_row(overview, [labels["overview"], ""])
    for key, label in (
        ("title", "report_title"),
        ("kind", "report_kind"),
        ("well_id", "well_id"),
        ("generated_at", "generated_at"),
        ("period_from", "period_from"),
        ("period_to", "period_to"),
    ):
        _append_row(overview, [labels[label], report.get(key)])
    _append_row(overview, [""])
    section_header_row = overview.max_row + 1
    _append_row(overview, [
        labels["section_key"], labels["section_title"], labels["section_kind"],
        labels["section_count"], labels["section_note"],
    ])
    for section in sections:
        _append_row(overview, [
            section["key"], section["title"], labels[section["kind"]],
            len(section["items"]), section.get("note"),
        ])
    _style_sheet(overview, locale, header_row=section_header_row)

    for kind in REPORT_SECTION_KINDS:
        sheet = workbook.create_sheet(labels[f"sheet_{kind}"][:31])
        kind_sections = [section for section in sections if section["kind"] == kind]
        field_names = sorted({
            key
            for section in kind_sections
            for item in section["items"]
            if isinstance(item, dict)
            for key in item
        })
        if any(not isinstance(key, str) for section in kind_sections for item in section["items"] if isinstance(item, dict) for key in item):
            raise StructuredReportExportError("report item keys must be strings")
        if any(not isinstance(item, dict) for section in kind_sections for item in section["items"]):
            field_names = sorted({*field_names, "item_json"})
        if len(field_names) > MAX_XLSX_FIELDS_PER_KIND:
            raise StructuredReportExportError("a report category contains too many distinct fields for Excel")
        _append_row(sheet, [labels[kind]])
        _append_row(sheet, [""])
        header_row = sheet.max_row + 1
        headers = [
            labels["record_type"], labels["section_key"], labels["section_title"],
            labels["section_note"], labels["item_index"],
            *[f"data.{field}" for field in field_names],
        ]
        _append_row(sheet, headers)
        if not kind_sections:
            _append_row(sheet, [labels["record_empty"], "", "", labels["no_sections"], "", *([""] * len(field_names))])
        for section in kind_sections:
            common = [section["key"], section["title"], section.get("note")]
            _append_row(sheet, [labels["record_section"], *common, "", *([""] * len(field_names))])
            for item_index, item in enumerate(section["items"], 1):
                if isinstance(item, dict):
                    values = [item.get(field) for field in field_names]
                else:
                    values = [_json_text(item) if field == "item_json" else None for field in field_names]
                _append_row(sheet, [labels["record_item"], *common, item_index, *values])
        _style_sheet(sheet, locale, header_row=header_row)
        sheet.auto_filter.ref = f"A{header_row}:{get_column_letter(sheet.max_column)}{sheet.max_row}"

    workbook.properties.title = f"{report['kind']} — {report['well_id']}"
    workbook.properties.subject = "Structured report; sections remain separated by kind"
    workbook.properties.creator = "DrillAI"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def render_structured_report_export(
    report: dict[str, Any],
    *,
    export_format: str,
    locale: str = "en",
) -> bytes:
    """Render a tenant-scoped structured report into JSON, PDF or XLSX bytes.

    The input is already built by ``ReportingService``. It is normalized, size-limited and validated
    before any renderer can truncate or reinterpret it. The returned bytes are deterministic for a
    fixed report and locale (timestamps in the report remain the caller's recorded values).
    """
    if export_format not in REPORT_EXPORT_MEDIA_TYPES:
        raise StructuredReportExportError("unsupported report export format")
    if locale not in _LABELS:
        raise StructuredReportExportError("unsupported report export locale")
    try:
        normalized, sections, _item_count = _validate_report(report)
        if export_format == "json":
            artifact = _json_text({"report": normalized}).encode("utf-8")
        elif export_format == "pdf":
            artifact = _render_pdf(normalized, sections, locale)
        else:
            artifact = _render_xlsx(normalized, sections, locale)
    except StructuredReportExportError:
        raise
    except (TypeError, ValueError, RecursionError, OverflowError) as exc:
        raise StructuredReportExportError("report could not be rendered safely") from exc
    if len(artifact) > MAX_REPORT_EXPORT_BYTES:
        raise StructuredReportExportError("report export exceeds the configured 20 MiB limit")
    return artifact
