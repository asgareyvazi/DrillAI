#!/usr/bin/env python3
"""Validate the container OCR report against the pinned CP135 synthetic corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

MIN_CASES = 60
STRATA_DIMENSIONS = ("language", "layout", "scan_condition")
RENDERER_DISTRIBUTIONS = {"Pillow", "reportlab", "arabic-reshaper", "python-bidi", "pypdfium2"}
RESOURCE_LIMIT_KEYS = {
    "max_generated_pdf_bytes",
    "ocr_max_pages",
    "ocr_dpi",
    "ocr_max_page_pixels",
    "ocr_timeout_seconds",
    "ocr_max_concurrency",
}
MAX_CER = 0.15
MAX_WER = 0.20
MIN_CRITICAL_RATE = 0.90
_FORBIDDEN_KEYS = {
    "actual_text",
    "ground_truth",
    "image",
    "input_bytes",
    "local_path",
    "ocr_text",
    "pdf_bytes",
    "recognized_text",
    "stderr",
    "text",
    "transcript",
}


def _walk_keys(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key).casefold()
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _same_number(actual: Any, expected: float) -> bool:
    return _finite_number(actual) and math.isclose(float(actual), expected, rel_tol=1e-12, abs_tol=1e-12)


def _thresholds_pass(cer: float | None, wer: float | None, critical_rate: float | None) -> bool:
    return (
        cer is not None
        and wer is not None
        and critical_rate is not None
        and cer <= MAX_CER
        and wer <= MAX_WER
        and critical_rate >= MIN_CRITICAL_RATE
    )


def _aggregate(items: list[dict[str, Any]]) -> dict[str, Any]:
    character_errors = sum(item["character_errors"] for item in items)
    expected_characters = sum(item["expected_characters"] for item in items)
    word_errors = sum(item["word_errors"] for item in items)
    expected_words = sum(item["expected_words"] for item in items)
    critical_matched = sum(item["critical_fields_matched"] for item in items)
    critical_expected = sum(item["critical_fields_expected"] for item in items)
    exact_cases = sum(item["character_errors"] == 0 and item["word_errors"] == 0 for item in items)
    return {
        "character_errors": character_errors,
        "expected_characters": expected_characters,
        "character_error_rate": character_errors / expected_characters if expected_characters else None,
        "word_errors": word_errors,
        "expected_words": expected_words,
        "word_error_rate": word_errors / expected_words if expected_words else None,
        "critical_fields_matched": critical_matched,
        "critical_fields_expected": critical_expected,
        "critical_field_exact_rate": critical_matched / critical_expected if critical_expected else None,
        "exact_case_rate": exact_cases / len(items) if items else None,
    }


def verify(report_path: Path, corpus_path: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    corpus_bytes = corpus_path.read_bytes()
    corpus = json.loads(corpus_bytes)
    if not isinstance(report, dict) or not isinstance(corpus, dict):
        raise ValueError("report and corpus must be JSON objects")
    if any(key in _FORBIDDEN_KEYS for key in _walk_keys(report)):
        raise ValueError("OCR quality report contains raw text, image, byte, stderr, or local-path material")

    corpus_cases = corpus.get("cases")
    dataset = report.get("dataset")
    dimensions = corpus.get("stratification_dimensions")
    languages = corpus.get("languages")
    if (
        not isinstance(corpus_cases, list)
        or len(corpus_cases) < MIN_CASES
        or corpus.get("corpus_id") != "cp135-synthetic-ddr-ocr"
        or corpus.get("version") != "2.0.0"
        or corpus.get("classification") != "synthetic"
        or languages != ["eng", "fas"]
        or dimensions != list(STRATA_DIMENSIONS)
        or not isinstance(dataset, dict)
    ):
        raise ValueError("checked-in corpus is malformed or below the CP135 bilingual case minimum")

    expected_digest = hashlib.sha256(corpus_bytes).hexdigest()
    runtime = report.get("runtime")
    if (
        report.get("tool") != "drillai-synthetic-ocr-quality-v2"
        or report.get("status") != "passed"
        or report.get("quality_evaluated") is not True
        or dataset.get("id") != corpus.get("corpus_id")
        or dataset.get("version") != corpus.get("version")
        or dataset.get("classification") != "synthetic"
        or dataset.get("sha256") != expected_digest
        or dataset.get("case_count") != len(corpus_cases)
        or dataset.get("languages") != languages
        or dataset.get("stratification_dimensions") != dimensions
        or not isinstance(runtime, dict)
        or runtime.get("required_languages") != languages
        or not set(languages).issubset(runtime.get("configured_languages", []))
        or not set(languages).issubset(runtime.get("available_languages", []))
        or not isinstance(runtime.get("python_version"), str)
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", runtime["python_version"])
        or not isinstance(runtime.get("runtime_version"), str)
        or not isinstance(runtime.get("pdf_renderer_version"), str)
    ):
        raise ValueError("OCR quality report is not a complete bilingual pass for the checked-in corpus/runtime")
    renderer_dependencies = runtime.get("renderer_dependencies")
    resource_limits = runtime.get("resource_limits")
    if (
        not isinstance(renderer_dependencies, dict)
        or set(renderer_dependencies) != RENDERER_DISTRIBUTIONS
        or any(not isinstance(value, str) or not value for value in renderer_dependencies.values())
        or not isinstance(resource_limits, dict)
        or set(resource_limits) != RESOURCE_LIMIT_KEYS
        or any(not _finite_number(value) or value <= 0 for value in resource_limits.values())
    ):
        raise ValueError("OCR quality report omitted rendering dependency versions or bounded runtime settings")

    cases = report.get("cases")
    metrics = report.get("metrics")
    thresholds = report.get("thresholds")
    if (
        not isinstance(cases, list)
        or not isinstance(metrics, dict)
        or not isinstance(thresholds, dict)
        or len(cases) != len(corpus_cases)
        or any(not isinstance(case, dict) for case in corpus_cases)
        or [item.get("id") for item in cases if isinstance(item, dict)]
        != [case.get("case_id") for case in corpus_cases]
        or any(not isinstance(item, dict) or item.get("status") != "evaluated" for item in cases)
    ):
        raise ValueError("OCR report does not contain one evaluated result for each pinned corpus case")

    for corpus_case, item in zip(corpus_cases, cases, strict=True):
        if any(item.get(dimension) != corpus_case.get(dimension) for dimension in STRATA_DIMENSIONS):
            raise ValueError("per-case evidence does not match its pinned language/layout/scan stratum")
        if not isinstance(item.get("pdf_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", item["pdf_sha256"]):
            raise ValueError("per-case evidence omitted the image-only PDF digest")
        for key in (
            "character_errors",
            "expected_characters",
            "word_errors",
            "expected_words",
            "critical_fields_expected",
            "critical_fields_matched",
        ):
            if not _integer(item.get(key)):
                raise ValueError(f"per-case evidence contains an invalid {key} count")
        if (
            item["expected_characters"] == 0
            or item["expected_words"] == 0
            or item["critical_fields_expected"] != len(corpus_case.get("critical_fields", []))
            or item["critical_fields_matched"] > item["critical_fields_expected"]
        ):
            raise ValueError("per-case denominator or critical-field counts do not match the corpus")
        expected_cer = item["character_errors"] / item["expected_characters"]
        expected_wer = item["word_errors"] / item["expected_words"]
        if not _same_number(item.get("cer"), expected_cer) or not _same_number(item.get("wer"), expected_wer):
            raise ValueError("per-case CER/WER do not match their reported edit counts and denominators")
        if not _finite_number(item.get("processing_ms")) or item["processing_ms"] < 0:
            raise ValueError("per-case processing time is invalid")
        if not _finite_number(item.get("recognition_ms")) or item["recognition_ms"] < 0:
            raise ValueError("per-case OCR recognition time is invalid")

    aggregate = _aggregate(cases)
    if (
        metrics.get("case_count") != len(corpus_cases)
        or metrics.get("evaluated_cases") != len(corpus_cases)
        or metrics.get("incomplete_or_failed_cases") != 0
        or any(metrics.get(key) != value for key, value in aggregate.items() if key not in {"character_error_rate", "word_error_rate", "critical_field_exact_rate", "exact_case_rate"})
        or not _same_number(metrics.get("character_error_rate"), aggregate["character_error_rate"])
        or not _same_number(metrics.get("word_error_rate"), aggregate["word_error_rate"])
        or not _same_number(metrics.get("critical_field_exact_rate"), aggregate["critical_field_exact_rate"])
        or not _same_number(metrics.get("exact_case_rate"), aggregate["exact_case_rate"])
    ):
        raise ValueError("global metrics do not reproduce from the per-case evidence")

    expected_thresholds = {
        "max_character_error_rate": MAX_CER,
        "max_word_error_rate": MAX_WER,
        "min_critical_field_exact_rate": MIN_CRITICAL_RATE,
    }
    if any(not _same_number(thresholds.get(key), value) for key, value in expected_thresholds.items()):
        raise ValueError("OCR report changed the reviewed synthetic quality thresholds")
    if (
        not _thresholds_pass(aggregate["character_error_rate"], aggregate["word_error_rate"], aggregate["critical_field_exact_rate"])
        or metrics.get("thresholds_passed") is not True
        or metrics.get("all_strata_thresholds_passed") is not True
    ):
        raise ValueError("measured OCR metrics are outside the reviewed global software thresholds")

    stratified = metrics.get("stratified_metrics")
    if not isinstance(stratified, dict) or set(stratified) != set(STRATA_DIMENSIONS):
        raise ValueError("report omitted one or more reviewed stratified metric dimensions")
    strata_summary: dict[str, dict[str, int]] = {}
    for dimension in STRATA_DIMENSIONS:
        rows = stratified.get(dimension)
        values = sorted({case[dimension] for case in corpus_cases})
        if not isinstance(rows, list) or [row.get("stratum") for row in rows if isinstance(row, dict)] != values:
            raise ValueError(f"{dimension} metrics do not include each pinned stratum exactly once")
        summary_counts: dict[str, int] = {}
        for value, row in zip(values, rows, strict=True):
            expected_group = [case for case in cases if case[dimension] == value]
            grouped = _aggregate(expected_group)
            expected_counts = {
                "case_count": len(expected_group),
                "evaluated_cases": len(expected_group),
                "incomplete_or_failed_cases": 0,
                "character_errors": grouped["character_errors"],
                "expected_characters": grouped["expected_characters"],
                "word_errors": grouped["word_errors"],
                "expected_words": grouped["expected_words"],
                "critical_fields_matched": grouped["critical_fields_matched"],
                "critical_fields_expected": grouped["critical_fields_expected"],
            }
            if any(row.get(key) != expected for key, expected in expected_counts.items()):
                raise ValueError(f"{dimension} stratum {value!r} counts do not reproduce from per-case evidence")
            rates = {
                "character_error_rate": grouped["character_error_rate"],
                "word_error_rate": grouped["word_error_rate"],
                "critical_field_exact_rate": grouped["critical_field_exact_rate"],
            }
            if any(not _same_number(row.get(key), rate) for key, rate in rates.items()):
                raise ValueError(f"{dimension} stratum {value!r} rates do not reproduce from per-case evidence")
            passes = _thresholds_pass(
                grouped["character_error_rate"],
                grouped["word_error_rate"],
                grouped["critical_field_exact_rate"],
            )
            if row.get("thresholds_passed") is not passes or not passes:
                raise ValueError(f"{dimension} stratum {value!r} did not meet the reviewed software thresholds")
            summary_counts[str(value)] = len(expected_group)
        strata_summary[dimension] = summary_counts

    return {
        "cases": len(cases),
        "cer": aggregate["character_error_rate"],
        "wer": aggregate["word_error_rate"],
        "critical_field_exact_rate": aggregate["critical_field_exact_rate"],
        "strata": strata_summary,
        "dataset_sha256": expected_digest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="JSON report emitted by the deployment image")
    parser.add_argument("--corpus", type=Path, required=True, help="pinned synthetic DDR OCR corpus JSON")
    args = parser.parse_args(argv)
    try:
        result = verify(args.report, args.corpus)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"SYNTHETIC_OCR_QUALITY_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    strata = ";".join(
        f"{dimension}=" + ",".join(f"{value}:{count}" for value, count in values.items())
        for dimension, values in result["strata"].items()
    )
    print(
        "SYNTHETIC_OCR_QUALITY_OK "
        f"cases={result['cases']} CER={result['cer']:.5f} WER={result['wer']:.5f} "
        f"critical_field_exact_rate={result['critical_field_exact_rate']:.5f} "
        f"strata={strata} dataset_sha256={result['dataset_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
