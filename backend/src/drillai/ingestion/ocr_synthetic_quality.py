"""In-memory synthetic OCR quality check for the deployed PDFium/Tesseract ingestion path.

The fixture corpus is versioned in ``data/synthetic_ddr_ocr_quality_v1.json``. Each page is rendered
as an image-only PDF, passed through the same ``parse_bytes`` and PDFium/Tesseract adapters used by
uploads, and compared with generated ground truth. No operator data, network access, provider, or
persistent artifact is used. Results are synthetic software measurements, never field acceptance.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from drillai.core.config import Settings, get_settings
from drillai.ingestion.ocr import TESSERACT_ADAPTER_VERSION, OcrUnavailable, TesseractOcrProvider
from drillai.ingestion.ocr_benchmark import error_rates, normalize_for_scoring
from drillai.ingestion.ocr_runtime import _pdfium_details
from drillai.ingestion.parsers import parse_bytes

_CORPUS_PATH = Path(__file__).with_name("data") / "synthetic_ddr_ocr_quality_v1.json"
_MAX_PDF_BYTES = 8 * 1024 * 1024
_MAX_SYNTHETIC_CER = 0.15
_MAX_SYNTHETIC_WER = 0.20
_MIN_CRITICAL_FIELD_ACCURACY = 0.90
_FIELD_LABELS = {
    "sample_id": "sample id",
    "report_date": "report date",
    "measured_depth_m": "measured depth",
    "rop_m_per_h": "rate of penetration",
    "mud_weight_ppg": "mud weight",
    "npt_h": "nonproductive time",
}
_NUMERIC_FIELD_KEYS = {"measured_depth_m", "rop_m_per_h", "mud_weight_ppg", "npt_h"}


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
        or document.get("corpus_id") != "cp130-synthetic-ddr-ocr"
        or document.get("version") != "1.0.0"
        or document.get("languages") != ["eng"]
        or not isinstance(document.get("cases"), list)
        or len(document["cases"]) < 30
    ):
        raise ValueError("the versioned synthetic DDR OCR corpus is malformed or below its 30-case minimum")
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
        if (
            not isinstance(case.get("case_id"), str)
            or not re.fullmatch(r"cp130-ddr-[0-9]{3}", case["case_id"])
            or case.get("language") != "eng"
            or case.get("document_class") != "daily_drilling_report"
            or not isinstance(case.get("ground_truth"), str)
            or not case["ground_truth"].strip()
            or not isinstance(fields, list)
            or not fields
            or any(
                not isinstance(field, dict)
                or set(field) != {"key", "expected"}
                or not isinstance(field.get("key"), str)
                or field.get("key") not in _FIELD_LABELS
                or not isinstance(field.get("expected"), str)
                or not field["expected"].strip()
                for field in fields
            )
            or len({field["key"] for field in fields if isinstance(field, dict) and isinstance(field.get("key"), str)})
            != len(fields)
        ):
            raise ValueError("the synthetic DDR OCR corpus contains an invalid or duplicate case")
    return document, raw


def _critical_field_matches(actual_text: str, critical_fields: list[dict[str, str]]) -> int:
    """Match expected values to their explicit OCR field labels, not to incidental text elsewhere."""
    values_by_label: dict[str, str] = {}
    for line in actual_text.splitlines():
        label, separator, value = line.partition(":")
        if separator:
            normalized_label = normalize_for_scoring(label)
            if normalized_label in _FIELD_LABELS.values():
                values_by_label.setdefault(normalized_label, normalize_for_scoring(value))

    matches = 0
    for field in critical_fields:
        key = field["key"]
        observed = values_by_label.get(_FIELD_LABELS[key], "")
        expected = normalize_for_scoring(field["expected"])
        if key in _NUMERIC_FIELD_KEYS:
            observed = observed.split(maxsplit=1)[0] if observed else ""
        if observed == expected:
            matches += 1
    return matches


def _raster_pdf(ground_truth: str) -> bytes:
    """Create a clean, one-page image-only DDR PDF entirely in memory."""
    try:
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

    image = Image.new("RGB", (1700, 2200), color="white")
    drawing = ImageDraw.Draw(image)
    regular = ImageFont.truetype(str(regular_path), 48)
    bold = ImageFont.truetype(str(bold_path), 54)
    lines = ground_truth.splitlines()
    for index, line in enumerate(lines):
        y = 230 + index * 126
        drawing.text((130, y), line, font=bold if index == 0 else regular, fill=(0, 0, 0))
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


def _base_report(corpus: dict[str, Any], corpus_bytes: bytes) -> dict[str, Any]:
    return {
        "tool": "drillai-synthetic-ocr-quality-v1",
        "status": "blocked",
        "quality_evaluated": False,
        "dataset": {
            "id": corpus["corpus_id"],
            "version": corpus["version"],
            "classification": corpus["classification"],
            "sha256": hashlib.sha256(corpus_bytes).hexdigest(),
            "case_count": len(corpus["cases"]),
            "languages": list(corpus["languages"]),
            "sampling_method": corpus["sampling_method"],
            "ground_truth_protocol": corpus["ground_truth_protocol"],
        },
        "runtime": {
            "provider": "tesseract",
            "adapter_version": TESSERACT_ADAPTER_VERSION,
            "runtime_version": None,
            "pdf_renderer_version": None,
            "configured_languages": [],
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
            "All pages are clean, machine-generated synthetic English fixtures; they are not sampled field or vendor DDRs.",
            "Ground truth is generated from fixture fields, not independently adjudicated; metrics are development-only.",
            "CER/WER and exact fixture-token recovery do not establish semantic correctness, engineering acceptance, field performance, or production readiness.",
            "No transcript, image, local path, uploaded data, provider request, or persistent output is included in this report.",
        ],
    }


def run_synthetic_quality_benchmark(settings: Settings | None = None) -> dict[str, Any]:
    """Measure end-to-end PDF parsing/OCR quality over the pinned 32+ case synthetic corpus."""
    resolved = settings or get_settings()
    corpus, corpus_bytes = _load_corpus()
    report = _base_report(corpus, corpus_bytes)
    configured_languages = resolved.ocr_languages.split("+")
    report["runtime"]["configured_languages"] = configured_languages
    if "eng" not in configured_languages:
        report["reason"] = "configured OCR languages do not include the English synthetic corpus locale"
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
        }
    )
    missing_languages = diagnostics.get("missing_languages", [])
    if not diagnostics.get("available") or not pdfium_available or missing_languages:
        report["reason"] = "configured Tesseract, English language data, and PDFium must all be available"
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
        item: dict[str, Any] = {"id": case["case_id"], "status": "failed"}
        try:
            pdf_bytes = _raster_pdf(case["ground_truth"])
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
            matched_fields = _critical_field_matches(actual_text, critical_fields)
            critical_expected += len(critical_fields)
            critical_matched += matched_fields
            total_expected_characters += int(metrics["expected_characters"])
            total_character_errors += int(metrics["character_errors"])
            total_expected_words += int(metrics["expected_words"])
            total_word_errors += int(metrics["word_errors"])
            sample_exact += int(metrics["cer"] == 0.0)
            item.update(
                {
                    "status": "evaluated",
                    "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
                    "cer": metrics["cer"],
                    "wer": metrics["wer"],
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
        thresholds_passed = (
            cer is not None
            and wer is not None
            and critical_rate is not None
            and cer <= _MAX_SYNTHETIC_CER
            and wer <= _MAX_SYNTHETIC_WER
            and critical_rate >= _MIN_CRITICAL_FIELD_ACCURACY
        )
    else:
        cer = wer = critical_rate = None
        thresholds_passed = False
    report.update(
        {
            "status": "passed" if complete and thresholds_passed else "failed",
            "quality_evaluated": complete,
            "metrics": {
                "case_count": len(corpus["cases"]),
                "evaluated_cases": len(evaluated),
                "incomplete_or_failed_cases": len(measured) - len(evaluated),
                "character_error_rate": cer,
                "word_error_rate": wer,
                "exact_case_rate": sample_exact / len(evaluated) if evaluated else None,
                "critical_field_exact_rate": critical_rate,
                "critical_fields_matched": critical_matched,
                "critical_fields_expected": critical_expected,
                "median_case_processing_ms": round(statistics.median(page_times), 3) if page_times else None,
                "p95_case_processing_ms": _percentile(page_times, 0.95),
                "median_tesseract_ms": round(statistics.median(recognition_times), 3) if recognition_times else None,
                "p95_tesseract_ms": _percentile(recognition_times, 0.95),
                "total_elapsed_ms": round(total_ms, 3),
                "thresholds_passed": thresholds_passed,
            },
            "cases": measured,
            "reason": None if complete and thresholds_passed else (
                "synthetic transcription metrics exceeded a software threshold"
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
            "tool": "drillai-synthetic-ocr-quality-v1",
            "status": "failed",
            "quality_evaluated": False,
            "reason": f"synthetic OCR benchmark setup failed ({type(exc).__name__})",
            "limitations": ["This synthetic software check is not field or engineering acceptance."],
        }
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
    return 0 if report.get("status") == "passed" and report.get("quality_evaluated") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())
