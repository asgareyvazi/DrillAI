from __future__ import annotations

import json
from io import BytesIO

import arabic_reshaper
import pytest
from openpyxl import load_workbook
from pypdf import PdfReader

from drillai.drilling.structured_report_exports import (
    StructuredReportExportError,
    render_structured_report_export,
)

REPORT = {
    "kind": "management_summary",
    "title": "گزارش نمونه — NF-12",
    "well_id": "wel_synthetic",
    "generated_at": "2026-10-20T12:00:00+00:00",
    "period_from": None,
    "period_to": None,
    "metadata": {"synthetic": True, "well": {"name": "NF-12"}},
    "sections": [
        {
            "key": "facts",
            "title": "Recorded state",
            "kind": "facts",
            "items": [
                {
                    "metric": "measured_depth",
                    "value": 3200.0,
                    "unit": "m",
                    "source": "recorded source row",
                    "formula_like": "=1+2",
                    "large_identifier": 9_007_199_254_740_993,
                    "precision_sensitive": 123_456_789_012_345.67,
                    "user_markup": "<script>alert(1)</script>",
                }
            ],
            "note": "Synthetic facts only.",
            "count": 1,
        },
        {
            "key": "calculations",
            "title": "Engineering calculations",
            "kind": "calculations",
            "items": [
                {
                    "engine_key": "trajectory.minimum_curvature",
                    "engine_version": "2.0.0",
                    "inputs": {"station_count": 2},
                    "outputs": {"tvd_si": 2989.3},
                    "units": {
                        "output_fields": {
                            "tvd_si": {"status": "declared", "canonical_unit": "m"}
                        }
                    },
                    "inputs_hash": "synthetic-input-hash",
                }
            ],
            "note": "Persisted result and unit contract.",
            "count": 1,
        },
        {
            "key": "evidence",
            "title": "Source evidence",
            "kind": "evidence",
            "items": [{"document_id": "doc_synthetic", "page_number": 1, "excerpt": "برگهٔ منبع"}],
            "note": None,
            "count": 1,
        },
        {
            "key": "recommendations",
            "title": "Recommendations and decisions",
            "kind": "recommendations",
            "items": [{"status": "proposed", "statement": "فقط برای بازبینی انسانی", "action_level": "L1"}],
            "note": "No approval is implied.",
            "count": 1,
        },
        {
            "key": "assumptions",
            "title": "Assumptions and limitations",
            "kind": "assumptions",
            "items": [{"key": "time_basis", "statement": "زمان فقط از رکورد منبع"}],
            "note": None,
            "count": 1,
        },
    ],
    "section_kinds": ["assumptions", "calculations", "evidence", "facts", "recommendations"],
    "is_renderable": True,
    "rendering_note": "JSON, PDF and XLSX exports preserve these section kinds.",
}


@pytest.mark.parametrize("export_format", ["json", "pdf", "xlsx"])
def test_structured_report_exports_are_real_files_and_preserve_section_contract(export_format: str):
    artifact = render_structured_report_export(REPORT, export_format=export_format, locale="fa")
    assert len(artifact) > 100

    if export_format == "json":
        assert json.loads(artifact)["report"] == REPORT
        return

    if export_format == "pdf":
        assert artifact.startswith(b"%PDF-")
        reader = PdfReader(BytesIO(artifact))
        assert len(reader.pages) >= 1
        extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        # The actual embedded Persian-capable PDF font and bidi renderer are exercised, not stubbed.
        expected_title = arabic_reshaper.reshape("گزارش نمونه")
        assert expected_title in extracted
        assert "3200" in extracted
        assert "2989.3" in extracted
        assert "doc_synthetic" in extracted
        assert "L1" in extracted
        assert "time_basis" in extracted
        assert "<script>alert(1)</script>" in extracted
        return

    workbook = load_workbook(BytesIO(artifact), data_only=False)
    assert workbook.sheetnames == ["نمای کلی", "واقعیت‌ها", "محاسبات", "شواهد", "توصیه‌ها", "فرض‌ها"]
    assert all(workbook[sheet].sheet_view.rightToLeft is True for sheet in workbook.sheetnames)

    overview = workbook["نمای کلی"]
    overview_values = [cell.value for row in overview.iter_rows() for cell in row]
    assert "گزارش نمونه — NF-12" in overview_values
    assert {"facts", "calculations", "evidence", "recommendations", "assumptions"} <= set(overview_values)

    facts = workbook["واقعیت‌ها"]
    fact_headers = {cell.value: cell.column for cell in facts[3]}
    formula_cell = facts.cell(row=5, column=fact_headers["data.formula_like"])
    assert formula_cell.value == "=1+2"
    assert formula_cell.data_type == "s"  # user text cannot become an Excel formula
    markup_cell = facts.cell(row=5, column=fact_headers["data.user_markup"])
    assert markup_cell.value == "<script>alert(1)</script>"
    assert markup_cell.data_type == "s"

    calculations = workbook["محاسبات"]
    calc_headers = {cell.value: cell.column for cell in calculations[3]}
    unit_cell = calculations.cell(row=5, column=calc_headers["data.units"])
    unit_contract = json.loads(unit_cell.value)
    assert unit_contract["output_fields"]["tvd_si"]["canonical_unit"] == "m"
    assert calculations.cell(row=5, column=calc_headers["data.outputs"]).value == '{"tvd_si":2989.3}'
    evidence = workbook["شواهد"]
    evidence_headers = {cell.value: cell.column for cell in evidence[3]}
    assert evidence.cell(row=5, column=evidence_headers["data.document_id"]).value == "doc_synthetic"
    recommendation = workbook["توصیه‌ها"]
    recommendation_headers = {cell.value: cell.column for cell in recommendation[3]}
    assert recommendation.cell(row=5, column=recommendation_headers["data.action_level"]).value == "L1"
    assumptions = workbook["فرض‌ها"]
    assumption_headers = {cell.value: cell.column for cell in assumptions[3]}
    assert assumptions.cell(row=5, column=assumption_headers["data.key"]).value == "time_basis"


def test_structured_report_xlsx_keeps_ltr_direction_and_typed_numeric_values():
    workbook = load_workbook(
        BytesIO(render_structured_report_export(REPORT, export_format="xlsx", locale="en")),
        data_only=False,
    )
    assert all(workbook[sheet].sheet_view.rightToLeft is False for sheet in workbook.sheetnames)
    facts = workbook["Facts"]
    headers = {cell.value: cell.column for cell in facts[3]}
    assert facts.cell(row=5, column=headers["data.value"]).value == 3200.0
    assert facts.cell(row=5, column=headers["data.value"]).data_type == "n"
    assert facts.cell(row=5, column=headers["data.formula_like"]).data_type == "s"
    large_identifier = facts.cell(row=5, column=headers["data.large_identifier"])
    assert large_identifier.value == "9007199254740993"
    assert large_identifier.data_type == "s"
    precise_float = facts.cell(row=5, column=headers["data.precision_sensitive"])
    assert precise_float.value == repr(REPORT["sections"][0]["items"][0]["precision_sensitive"])
    assert precise_float.data_type == "s"
    assert facts.cell(row=3, column=1).alignment.horizontal == "left"


def test_pdf_row_limit_stops_consuming_fields_incrementally(monkeypatch):
    import drillai.drilling.structured_report_exports as exports

    monkeypatch.setattr(exports, "MAX_PDF_RENDER_ROWS", 2)
    consumed = 0

    def many_fields(*_args):
        nonlocal consumed
        for index in range(10_000):
            consumed += 1
            yield f"/field/{index}", "synthetic"

    monkeypatch.setattr(exports, "_flatten_pdf_fields", many_fields)
    report = {
        **REPORT,
        "sections": [{**REPORT["sections"][0], "items": [{}], "count": 1}],
    }
    with pytest.raises(StructuredReportExportError, match="too many PDF rows"):
        render_structured_report_export(report, export_format="pdf")
    assert consumed == 3  # the first over-limit field rejects before the rest of the iterator is read


def test_xlsx_rejects_oversized_cells_instead_of_truncating_report_values():
    long_item = {**REPORT["sections"][0]["items"][0], "too_long": "x" * 32_768}
    report = {
        **REPORT,
        "sections": [{**REPORT["sections"][0], "items": [long_item], "count": 1}],
    }
    with pytest.raises(StructuredReportExportError, match="32,767-character cell limit"):
        render_structured_report_export(report, export_format="xlsx")


def test_structured_report_export_rejects_invalid_kinds_counts_locales_and_nonfinite_values():
    with pytest.raises(StructuredReportExportError, match="section has an unknown kind"):
        invalid_kind = {**REPORT, "sections": [{**REPORT["sections"][0], "kind": "mixed"}]}
        render_structured_report_export(invalid_kind, export_format="pdf")

    with pytest.raises(StructuredReportExportError, match="count does not match"):
        invalid_count = {**REPORT, "sections": [{**REPORT["sections"][0], "count": 2}]}
        render_structured_report_export(invalid_count, export_format="xlsx")

    with pytest.raises(StructuredReportExportError, match="non-finite"):
        invalid_number = {**REPORT, "metadata": {"bad": float("nan")}}
        render_structured_report_export(invalid_number, export_format="json")

    with pytest.raises(StructuredReportExportError, match="locale"):
        render_structured_report_export(REPORT, export_format="pdf", locale="ar")
