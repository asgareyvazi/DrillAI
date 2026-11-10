"""In-memory synthetic OCR quality check for the deployed PDFium/Tesseract ingestion path.

The fixture corpus is versioned in ``data/synthetic_ddr_ocr_quality_v3.json``. Each varied page is
rendered as an image-only PDF, passed through the same ``parse_bytes`` and PDFium/Tesseract adapters
used by uploads, and compared with generated ground truth. English and Persian cases run only when
the configured deployment runtime has both required language packs. No operator data, network access,
provider, transcript, or persistent page artifact is used. Results are synthetic software measurements,
never field acceptance.
"""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import math
import platform
import random
import re
import statistics
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from drillai.core.config import Settings, get_settings
from drillai.ingestion.ocr import (
    TESSERACT_ADAPTER_VERSION,
    TESSERACT_PAGE_SEGMENTATION_MODE,
    OcrUnavailable,
    TesseractOcrProvider,
)
from drillai.ingestion.ocr_benchmark import error_rates, normalize_for_scoring
from drillai.ingestion.ocr_runtime import _pdfium_details
from drillai.ingestion.parsers import parse_bytes

_CORPUS_PATH = Path(__file__).with_name("data") / "synthetic_ddr_ocr_quality_v3.json"
_MAX_PDF_BYTES = 8 * 1024 * 1024
_MIN_SYNTHETIC_CASES = 60
_MAX_SYNTHETIC_CER = 0.15
_MAX_SYNTHETIC_WER = 0.20
_MIN_CRITICAL_FIELD_ACCURACY = 0.90
_STRATA_DIMENSIONS = ("language", "layout", "scan_condition")
_SUPPORTED_LAYOUTS = {"single_column", "table", "two_column", "compact"}
_SUPPORTED_SCAN_CONDITIONS = {"clean", "low_contrast", "light_skew", "light_noise"}
_FIELD_LABELS = {
    "eng": {
        "sample_id": "sample id",
        "report_date": "report date",
        "measured_depth_m": "measured depth",
        "rop_m_per_h": "rate of penetration",
        "mud_weight_ppg": "mud weight",
        "npt_h": "nonproductive time",
    },
    "fas": {
        "sample_id": "شناسه نمونه",
        "report_date": "تاریخ گزارش",
        "measured_depth_m": "عمق اندازه گیری",
        "rop_m_per_h": "سرعت حفاری",
        "mud_weight_ppg": "وزن گل",
        "npt_h": "زمان غیرمولد",
    },
}
_NUMERIC_FIELD_KEYS = {"measured_depth_m", "rop_m_per_h", "mud_weight_ppg", "npt_h"}
_RENDERER_DISTRIBUTIONS = {
    "Pillow": "Pillow",
    "reportlab": "reportlab",
    "arabic-reshaper": "arabic-reshaper",
    "python-bidi": "python-bidi",
    "pypdfium2": "pypdfium2",
}
_RENDERER_IMPORTS = (
    "arabic_reshaper",
    "bidi.algorithm",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "reportlab.pdfgen.canvas",
)


class _TimedProvider:
    """Transparent adapter that records per-page child-process latency without retaining OCR text."""

    def __init__(self, provider: TesseractOcrProvider) -> None:
        self._provider = provider
        self.recognition_ms: list[float] = []
        self.name = provider.name
        self.languages = provider.languages
        self.max_concurrency = provider.max_concurrency

    @property
    def version(self) -> str:
        return self._provider.version

    def availability(self) -> tuple[bool, str | None]:
        return self._provider.availability()

    def recognize(self, image_png: bytes):
        started = time.perf_counter()
        try:
            return self._provider.recognize(image_png)
        finally:
            self.recognition_ms.append((time.perf_counter() - started) * 1000.0)


def _load_corpus() -> tuple[dict[str, Any], bytes]:
    raw = _CORPUS_PATH.read_bytes()
    document = json.loads(raw)
    if (
        not isinstance(document, dict)
        or document.get("classification") != "synthetic"
        or document.get("corpus_id") != "cp135-synthetic-ddr-ocr"
        or document.get("version") != "3.0.0"
        or document.get("languages") != ["eng", "fas"]
        or document.get("stratification_dimensions") != list(_STRATA_DIMENSIONS)
        or not isinstance(document.get("cases"), list)
        or len(document["cases"]) < _MIN_SYNTHETIC_CASES
    ):
        raise ValueError(
            f"the versioned synthetic DDR OCR corpus is malformed or below its {_MIN_SYNTHETIC_CASES}-case minimum"
        )
    cases = document["cases"]
    if any(not isinstance(case, dict) for case in cases):
        raise ValueError("the synthetic DDR OCR corpus contains an invalid or duplicate case")
    case_ids = [case.get("case_id") for case in cases]
    if (
        any(not isinstance(case_id, str) for case_id in case_ids)
        or len(set(case_ids)) != len(case_ids)
    ):
        raise ValueError("the synthetic DDR OCR corpus contains an invalid or duplicate case")
    for case in cases:
        fields = case.get("critical_fields")
        language = case.get("language")
        if (
            not isinstance(case.get("case_id"), str)
            or not re.fullmatch(r"cp135-ddr-[0-9]{3}", case["case_id"])
            or not isinstance(language, str)
            or language not in _FIELD_LABELS
            or language not in document["languages"]
            or case.get("layout") not in _SUPPORTED_LAYOUTS
            or case.get("scan_condition") not in _SUPPORTED_SCAN_CONDITIONS
            or case.get("document_class") != "daily_drilling_report"
            or not isinstance(case.get("ground_truth"), str)
            or not case["ground_truth"].strip()
            or not isinstance(fields, list)
            or not fields
            or any(
                not isinstance(field, dict)
                or set(field) != {"key", "expected"}
                or not isinstance(field.get("key"), str)
                or field.get("key") not in _FIELD_LABELS[language]
                or not isinstance(field.get("expected"), str)
                or not field["expected"].strip()
                for field in fields
            )
            or len(
                {field["key"] for field in fields if isinstance(field, dict) and isinstance(field.get("key"), str)}
            )
            != len(fields)
        ):
            raise ValueError("the synthetic DDR OCR corpus contains an invalid or duplicate case")

    languages = {case["language"] for case in cases}
    if languages != set(document["languages"]):
        raise ValueError("every declared synthetic OCR language must have at least one scored case")
    if len(cases) != 64 or any(sum(case["language"] == language for case in cases) != 32 for language in document["languages"]):
        raise ValueError("the v3 synthetic OCR corpus must balance 32 cases per language")
    for dimension, allowed in (
        ("layout", _SUPPORTED_LAYOUTS),
        ("scan_condition", _SUPPORTED_SCAN_CONDITIONS),
    ):
        counts = {value: sum(case[dimension] == value for case in cases) for value in allowed}
        if any(count != 16 for count in counts.values()):
            raise ValueError(f"the v3 synthetic OCR corpus must balance every {dimension} stratum across languages")
    for language in document["languages"]:
        for layout in _SUPPORTED_LAYOUTS:
            for scan_condition in _SUPPORTED_SCAN_CONDITIONS:
                count = sum(
                    case["language"] == language
                    and case["layout"] == layout
                    and case["scan_condition"] == scan_condition
                    for case in cases
                )
                if count != 2:
                    raise ValueError(
                        "the v3 synthetic OCR corpus must contain exactly two cases per language/layout/scan cell"
                    )
    return document, raw


def _critical_field_matches(
    actual_text: str, critical_fields: list[dict[str, str]], *, language: str
) -> int:
    """Match expected values to their language-specific field labels, not incidental text elsewhere."""
    labels = {normalize_for_scoring(label): key for key, label in _FIELD_LABELS[language].items()}
    values_by_key: dict[str, str] = {}
    for line in actual_text.splitlines():
        label, separator, value = line.partition(":")
        if separator:
            key = labels.get(normalize_for_scoring(label))
            if key is not None:
                values_by_key.setdefault(key, normalize_for_scoring(value))

    matches = 0
    for field in critical_fields:
        key = field["key"]
        observed = values_by_key.get(key, "")
        expected = normalize_for_scoring(field["expected"])
        if key in _NUMERIC_FIELD_KEYS:
            observed = observed.split(maxsplit=1)[0] if observed else ""
        if observed == expected:
            matches += 1
    return matches


def _raster_pdf(case: dict[str, Any]) -> bytes:
    """Create a deterministic, one-page, image-only DDR PDF entirely in memory."""
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        from PIL import Image, ImageDraw, ImageFont
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas
    except Exception as exc:  # pragma: no cover - deployment extras are checked by runtime preflight
        raise OcrUnavailable("the synthetic OCR renderer is not installed") from exc

    font_dir = Path(__file__).resolve().parents[1] / "assets" / "fonts"
    regular_path = font_dir / "DejaVuSans.ttf"
    bold_path = font_dir / "DejaVuSans-Bold.ttf"
    if not regular_path.is_file() or not bold_path.is_file():
        raise OcrUnavailable("the bundled synthetic OCR font is missing")

    language = case["language"]
    layout = case["layout"]
    scan_condition = case["scan_condition"]
    lines = case["ground_truth"].splitlines()
    if language not in _FIELD_LABELS or layout not in _SUPPORTED_LAYOUTS:
        raise OcrUnavailable("the synthetic OCR case requested an unsupported language or layout")
    if scan_condition not in _SUPPORTED_SCAN_CONDITIONS or not lines:
        raise OcrUnavailable("the synthetic OCR case requested an unsupported scan condition")

    background = (249, 249, 246) if scan_condition == "low_contrast" else (255, 255, 255)
    ink = (74, 74, 74) if scan_condition == "low_contrast" else (20, 20, 20)
    image = Image.new("RGB", (1700, 2200), color=background)
    drawing = ImageDraw.Draw(image)
    regular_path_obj = ImageFont.truetype(str(regular_path), 42)
    bold_path_obj = ImageFont.truetype(str(bold_path), 50)

    def visual_text(text: str) -> str:
        if language == "fas":
            return get_display(arabic_reshaper.reshape(text))
        return text

    def draw_text(
        text: str,
        x: int,
        y: int,
        font: Any,
        *,
        right_aligned: bool = False,
        right_edge: int | None = None,
    ) -> None:
        rendered = visual_text(text)
        bbox = drawing.textbbox((0, 0), rendered, font=font)
        if right_aligned:
            edge = image.width - 125 if right_edge is None else right_edge
            x = edge - (bbox[2] - bbox[0])
        drawing.text((x, y), rendered, font=font, fill=ink)

    draw_text(lines[0], 125, 125, bold_path_obj, right_aligned=(language == "fas"))
    body = lines[1:]

    if layout == "table":
        row_height = 138
        for index, line in enumerate(body):
            y = 300 + index * row_height
            label, separator, value = line.partition(":")
            drawing.line((110, y + 94, 1590, y + 94), fill=(190, 190, 190), width=2)
            if separator and language == "eng":
                draw_text(f"{label.strip()}:", 130, y, regular_path_obj)
                draw_text(value.strip(), 900, y, regular_path_obj)
            elif separator:
                # Right-to-left table: the localized label occupies the right cell and its source
                # value the left cell. Text is shaped only for raster display; ground truth remains
                # in logical Unicode order in the versioned corpus.
                draw_text(f"{label.strip()}:", 780, y, regular_path_obj, right_aligned=True)
                draw_text(value.strip(), 130, y, regular_path_obj)
            else:
                draw_text(line, 130, y, regular_path_obj, right_aligned=(language == "fas"))
    elif layout == "two_column":
        split_at = (len(body) + 1) // 2
        column_font = regular_path_obj
        if language == "fas":
            # Persian labels plus mixed-script values need a narrower type size to remain inside
            # the two independently read columns without colliding with the central gutter.
            column_font = ImageFont.truetype(str(regular_path), 30)
        for index, line in enumerate(body[:split_at]):
            draw_text(
                line,
                120,
                330 + index * 170,
                column_font,
                right_aligned=(language == "fas"),
                right_edge=1590 if language == "fas" else None,
            )
        for index, line in enumerate(body[split_at:]):
            draw_text(
                line,
                880,
                330 + index * 170,
                column_font,
                right_aligned=(language == "fas"),
                right_edge=800 if language == "fas" else None,
            )
        drawing.line((840, 300, 840, 1580), fill=(205, 205, 205), width=2)
    else:
        font = regular_path_obj
        row_height = 132
        if layout == "compact":
            font = ImageFont.truetype(str(regular_path), 34)
            row_height = 108
        for index, line in enumerate(body):
            draw_text(
                line,
                125,
                300 + index * row_height,
                font,
                right_aligned=(language == "fas"),
            )

    if scan_condition == "light_noise":
        seed = int(case["case_id"].rsplit("-", 1)[1])
        noise = random.Random(seed)
        for _ in range(900):
            x = noise.randrange(image.width)
            y = noise.randrange(image.height)
            shade = noise.choice((215, 225, 235))
            drawing.point((x, y), fill=(shade, shade, shade))
    if scan_condition == "light_skew":
        image = image.rotate(0.4, resample=Image.Resampling.BICUBIC, fillcolor=background)

    png = io.BytesIO()
    image.save(png, format="PNG", optimize=True)

    pdf_buffer = io.BytesIO()
    page_width, page_height = 612.0, 792.0
    output = canvas.Canvas(
        pdf_buffer,
        pagesize=(page_width, page_height),
        pageCompression=1,
        invariant=1,
    )
    output.drawImage(ImageReader(io.BytesIO(png.getvalue())), 0, 0, width=page_width, height=page_height)
    output.showPage()
    output.save()
    result = pdf_buffer.getvalue()
    if not result.startswith(b"%PDF-") or len(result) > _MAX_PDF_BYTES:
        raise OcrUnavailable("the generated synthetic OCR PDF is invalid or exceeded its size bound")
    return result


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


def _renderer_dependency_versions() -> tuple[dict[str, str], list[str]]:
    """Probe versioned local renderer dependencies without exposing paths or exception text."""
    versions: dict[str, str] = {}
    missing: set[str] = set()
    for label, distribution in _RENDERER_DISTRIBUTIONS.items():
        try:
            versions[label] = version(distribution)
        except PackageNotFoundError:
            missing.add(label)
    for module in _RENDERER_IMPORTS:
        try:
            importlib.import_module(module)
        except Exception:
            missing.add(module)
    return versions, sorted(missing)


def _resource_limits(settings: Settings) -> dict[str, int | float]:
    return {
        "max_generated_pdf_bytes": _MAX_PDF_BYTES,
        "ocr_max_pages": settings.ocr_max_pages,
        "ocr_dpi": settings.ocr_dpi,
        "ocr_max_page_pixels": settings.ocr_max_page_pixels,
        "ocr_timeout_seconds": settings.ocr_timeout_seconds,
        "ocr_max_concurrency": settings.ocr_max_concurrency,
    }


def _base_report(corpus: dict[str, Any], corpus_bytes: bytes) -> dict[str, Any]:
    return {
        "tool": "drillai-synthetic-ocr-quality-v3",
        "status": "blocked",
        "quality_evaluated": False,
        "dataset": {
            "id": corpus["corpus_id"],
            "version": corpus["version"],
            "classification": corpus["classification"],
            "sha256": hashlib.sha256(corpus_bytes).hexdigest(),
            "case_count": len(corpus["cases"]),
            "languages": list(corpus["languages"]),
            "stratification_dimensions": list(corpus["stratification_dimensions"]),
            "sampling_method": corpus["sampling_method"],
            "ground_truth_protocol": corpus["ground_truth_protocol"],
        },
        "runtime": {
            "provider": "tesseract",
            "adapter_version": TESSERACT_ADAPTER_VERSION,
            "page_segmentation_mode": TESSERACT_PAGE_SEGMENTATION_MODE,
            "runtime_version": None,
            "python_version": platform.python_version(),
            "pdf_renderer_version": None,
            "renderer_dependencies": {},
            "resource_limits": {},
            "required_languages": list(corpus["languages"]),
            "configured_languages": [],
            "available_languages": [],
        },
        "thresholds": {
            "max_character_error_rate": _MAX_SYNTHETIC_CER,
            "max_word_error_rate": _MAX_SYNTHETIC_WER,
            "min_critical_field_exact_rate": _MIN_CRITICAL_FIELD_ACCURACY,
        },
        "metrics": None,
        "cases": [],
        "reason": None,
        "limitations": [
            "All 64 pages are deterministic machine-generated synthetic English/Persian fixtures; none are sampled field or vendor DDRs.",
            "Ground truth is generated from fixture fields, not independently adjudicated; metrics are development-only.",
            "The cases cover printed text, four synthetic layouts and four controlled scan conditions; handwriting, real scanning pipelines and field variability are not represented.",
            "CER/WER and fixture-token recovery do not establish semantic correctness, engineering acceptance, field performance, or production readiness.",
            "No transcript, image, local path, uploaded data, provider request, or persistent output is included in this report.",
        ],
    }


def _thresholds_passed(cer: float | None, wer: float | None, critical_rate: float | None) -> bool:
    return (
        cer is not None
        and wer is not None
        and critical_rate is not None
        and cer <= _MAX_SYNTHETIC_CER
        and wer <= _MAX_SYNTHETIC_WER
        and critical_rate >= _MIN_CRITICAL_FIELD_ACCURACY
    )


def _stratified_metrics(corpus_cases: list[dict[str, Any]], measured: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Return micro-aggregated, completeness-aware metrics for each pinned corpus stratum."""
    evidence_by_id = {item["id"]: item for item in measured}
    result: dict[str, list[dict[str, Any]]] = {}
    for dimension in _STRATA_DIMENSIONS:
        rows: list[dict[str, Any]] = []
        values = sorted({case[dimension] for case in corpus_cases})
        for value in values:
            expected_cases = [case for case in corpus_cases if case[dimension] == value]
            evaluated = [
                evidence_by_id[case["case_id"]]
                for case in expected_cases
                if evidence_by_id.get(case["case_id"], {}).get("status") == "evaluated"
            ]
            complete = len(evaluated) == len(expected_cases)
            character_errors = sum(int(item["character_errors"]) for item in evaluated)
            expected_characters = sum(int(item["expected_characters"]) for item in evaluated)
            word_errors = sum(int(item["word_errors"]) for item in evaluated)
            expected_words = sum(int(item["expected_words"]) for item in evaluated)
            critical_matched = sum(int(item["critical_fields_matched"]) for item in evaluated)
            critical_expected = sum(int(item["critical_fields_expected"]) for item in evaluated)
            cer = character_errors / expected_characters if complete and expected_characters else None
            wer = word_errors / expected_words if complete and expected_words else None
            critical_rate = critical_matched / critical_expected if complete and critical_expected else None
            rows.append(
                {
                    "stratum": value,
                    "case_count": len(expected_cases),
                    "evaluated_cases": len(evaluated),
                    "incomplete_or_failed_cases": len(expected_cases) - len(evaluated),
                    "character_errors": character_errors,
                    "expected_characters": expected_characters,
                    "word_errors": word_errors,
                    "expected_words": expected_words,
                    "character_error_rate": cer,
                    "word_error_rate": wer,
                    "critical_fields_matched": critical_matched,
                    "critical_fields_expected": critical_expected,
                    "critical_field_exact_rate": critical_rate,
                    "thresholds_passed": _thresholds_passed(cer, wer, critical_rate),
                }
            )
        result[dimension] = rows
    return result


def run_synthetic_quality_benchmark(settings: Settings | None = None) -> dict[str, Any]:
    """Measure the pinned 64-case bilingual synthetic corpus through the configured ingestion path."""
    resolved = settings or get_settings()
    corpus, corpus_bytes = _load_corpus()
    report = _base_report(corpus, corpus_bytes)
    required_languages = set(corpus["languages"])
    configured_languages = sorted({
        language.strip()
        for language in re.split(r"[+,]", resolved.ocr_languages)
        if language.strip()
    })
    report["runtime"]["configured_languages"] = configured_languages
    if not required_languages.issubset(configured_languages):
        report["reason"] = "configured OCR languages do not include every pinned corpus language"
        return report

    renderer_versions, missing_renderer_components = _renderer_dependency_versions()
    report["runtime"]["renderer_dependencies"] = renderer_versions
    report["runtime"]["resource_limits"] = _resource_limits(resolved)
    if missing_renderer_components:
        report["reason"] = "one or more required local PDF/image/RTL rendering components are unavailable"
        return report

    provider = TesseractOcrProvider(
        languages=resolved.ocr_languages,
        executable=resolved.ocr_executable,
        timeout_seconds=resolved.ocr_timeout_seconds,
        max_concurrency=resolved.ocr_max_concurrency,
    )
    diagnostics = provider.diagnostics()
    pdfium_available, pdfium_version = _pdfium_details()
    report["runtime"].update(
        {
            "runtime_version": diagnostics.get("runtime_version"),
            "pdf_renderer_version": pdfium_version,
            "available_languages": sorted(diagnostics.get("available_languages", [])),
        }
    )
    missing_languages = set(diagnostics.get("missing_languages", []))
    if (
        not diagnostics.get("available")
        or not pdfium_available
        or missing_languages
        or not required_languages.issubset(set(diagnostics.get("available_languages", [])))
    ):
        report["reason"] = "all pinned Tesseract languages and PDFium must be available in the configured runtime"
        return report

    timed_provider = _TimedProvider(provider)
    started_all = time.perf_counter()
    measured: list[dict[str, Any]] = []
    total_expected_characters = total_character_errors = 0
    total_expected_words = total_word_errors = 0
    critical_expected = critical_matched = 0
    sample_exact = 0
    for case in corpus["cases"]:
        case_started = time.perf_counter()
        item: dict[str, Any] = {
            "id": case["case_id"],
            "language": case["language"],
            "layout": case["layout"],
            "scan_condition": case["scan_condition"],
            "status": "failed",
        }
        try:
            pdf_bytes = _raster_pdf(case)
            item["pdf_sha256"] = hashlib.sha256(pdf_bytes).hexdigest()
            parsed = parse_bytes(
                pdf_bytes,
                filename=f"{case['case_id']}.pdf",
                content_type="application/pdf",
                ocr_provider=timed_provider,
                ocr_max_pages=resolved.ocr_max_pages,
                ocr_dpi=resolved.ocr_dpi,
                ocr_max_page_pixels=resolved.ocr_max_page_pixels,
            )
            if (
                len(parsed.pages) != 1
                or not parsed.pages[0].ocr_applied
                or parsed.pages[0].ocr_status != "applied"
                or parsed.metadata.get("ocr_applied_pages") != [1]
            ):
                item.update({"status": "incomplete", "reason": "OCR did not complete for the generated image-only page"})
                item["processing_ms"] = round((time.perf_counter() - case_started) * 1000.0, 3)
                measured.append(item)
                continue

            actual_text = parsed.pages[0].text
            metrics = error_rates(case["ground_truth"], actual_text)
            critical_fields = case["critical_fields"]
            matched_fields = _critical_field_matches(
                actual_text, critical_fields, language=case["language"]
            )
            critical_expected += len(critical_fields)
            critical_matched += matched_fields
            total_expected_characters += int(metrics["expected_characters"])
            total_character_errors += int(metrics["character_errors"])
            total_expected_words += int(metrics["expected_words"])
            total_word_errors += int(metrics["word_errors"])
            sample_exact += int(metrics["cer"] == 0.0 and metrics["wer"] == 0.0)
            item.update(
                {
                    "status": "evaluated",
                    "cer": metrics["cer"],
                    "wer": metrics["wer"],
                    "character_errors": int(metrics["character_errors"]),
                    "expected_characters": int(metrics["expected_characters"]),
                    "word_errors": int(metrics["word_errors"]),
                    "expected_words": int(metrics["expected_words"]),
                    "critical_fields_expected": len(critical_fields),
                    "critical_fields_matched": matched_fields,
                    "processing_ms": round((time.perf_counter() - case_started) * 1000.0, 3),
                    "recognition_ms": round(timed_provider.recognition_ms[-1], 3)
                    if timed_provider.recognition_ms
                    else None,
                }
            )
            measured.append(item)
        except OcrUnavailable:
            # Runtime exceptions may contain local paths or child-process output.
            item.update({"status": "failed", "reason": "synthetic OCR processing failed (OcrUnavailable)"})
            item["processing_ms"] = round((time.perf_counter() - case_started) * 1000.0, 3)
            measured.append(item)
        except Exception as exc:
            # Parser/provider exceptions can contain local paths, filenames or recognized text.
            item.update({"status": "failed", "reason": f"synthetic OCR processing failed ({type(exc).__name__})"})
            item["processing_ms"] = round((time.perf_counter() - case_started) * 1000.0, 3)
            measured.append(item)

    total_ms = (time.perf_counter() - started_all) * 1000.0
    evaluated = [item for item in measured if item["status"] == "evaluated"]
    recognition_times = [float(value) for value in timed_provider.recognition_ms]
    page_times = [float(item["processing_ms"]) for item in evaluated]
    complete = len(evaluated) == len(corpus["cases"])
    if complete:
        cer = total_character_errors / total_expected_characters if total_expected_characters else None
        wer = total_word_errors / total_expected_words if total_expected_words else None
        critical_rate = critical_matched / critical_expected if critical_expected else None
    else:
        cer = wer = critical_rate = None
    strata = _stratified_metrics(corpus["cases"], measured)
    all_strata_passed = all(
        row["thresholds_passed"] for rows in strata.values() for row in rows
    )
    thresholds_passed = complete and _thresholds_passed(cer, wer, critical_rate) and all_strata_passed
    report.update(
        {
            "status": "passed" if thresholds_passed else "failed",
            "quality_evaluated": complete,
            "metrics": {
                "case_count": len(corpus["cases"]),
                "evaluated_cases": len(evaluated),
                "incomplete_or_failed_cases": len(measured) - len(evaluated),
                "character_errors": total_character_errors,
                "expected_characters": total_expected_characters,
                "character_error_rate": cer,
                "word_errors": total_word_errors,
                "expected_words": total_expected_words,
                "word_error_rate": wer,
                "exact_case_rate": sample_exact / len(evaluated) if complete and evaluated else None,
                "critical_field_exact_rate": critical_rate,
                "critical_fields_matched": critical_matched,
                "critical_fields_expected": critical_expected,
                "median_case_processing_ms": round(statistics.median(page_times), 3) if page_times else None,
                "p95_case_processing_ms": _percentile(page_times, 0.95),
                "median_tesseract_ms": round(statistics.median(recognition_times), 3) if recognition_times else None,
                "p95_tesseract_ms": _percentile(recognition_times, 0.95),
                "total_elapsed_ms": round(total_ms, 3),
                "thresholds_passed": thresholds_passed,
                "all_strata_thresholds_passed": all_strata_passed,
                "stratified_metrics": strata,
            },
            "cases": measured,
            "reason": None if thresholds_passed else (
                "synthetic transcription metrics exceeded a global or stratified software threshold"
                if complete
                else "one or more synthetic pages did not complete the configured OCR path"
            ),
        }
    )
    return report


def main(argv: list[str] | None = None) -> int:
    """Run in the deployment image and emit one bounded JSON report to stdout."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Measure OCR transcription on a pinned synthetic DDR corpus using the local ingestion path."
    )
    parser.parse_args(argv)
    try:
        report = run_synthetic_quality_benchmark()
    except Exception as exc:
        # Never put exception text, paths, input bytes, or child-process output in a machine-readable report.
        report = {
            "tool": "drillai-synthetic-ocr-quality-v3",
            "status": "failed",
            "quality_evaluated": False,
            "reason": f"synthetic OCR benchmark setup failed ({type(exc).__name__})",
            "limitations": ["This synthetic software check is not field or engineering acceptance."],
        }
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
    return 0 if report.get("status") == "passed" and report.get("quality_evaluated") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
