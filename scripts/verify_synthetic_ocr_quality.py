#!/usr/bin/env python3
"""Validate the container OCR quality report against its pinned synthetic corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

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


def verify(report_path: Path, corpus_path: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    corpus_bytes = corpus_path.read_bytes()
    corpus = json.loads(corpus_bytes)
    if not isinstance(report, dict) or not isinstance(corpus, dict):
        raise ValueError("report and corpus must be JSON objects")
    if any(key in _FORBIDDEN_KEYS for key in _walk_keys(report)):
        raise ValueError("OCR quality report contains raw text, image, byte, stderr, or local-path material")
    dataset = report.get("dataset")
    if not isinstance(dataset, dict):
        raise ValueError("report omitted its dataset identity")
    expected_digest = hashlib.sha256(corpus_bytes).hexdigest()
    if (
        report.get("tool") != "drillai-synthetic-ocr-quality-v1"
        or report.get("status") != "passed"
        or report.get("quality_evaluated") is not True
        or dataset.get("id") != corpus.get("corpus_id")
        or dataset.get("version") != corpus.get("version")
        or dataset.get("classification") != "synthetic"
        or dataset.get("sha256") != expected_digest
    ):
        raise ValueError("OCR quality report is not a complete pass for the checked-in synthetic corpus")

    corpus_cases = corpus.get("cases")
    cases = report.get("cases")
    metrics = report.get("metrics")
    thresholds = report.get("thresholds")
    if (
        not isinstance(corpus_cases, list)
        or len(corpus_cases) < 30
        or not isinstance(cases, list)
        or not isinstance(metrics, dict)
        or not isinstance(thresholds, dict)
        or len(cases) != len(corpus_cases)
        or [item.get("id") for item in cases if isinstance(item, dict)]
        != [item.get("case_id") for item in corpus_cases if isinstance(item, dict)]
        or any(not isinstance(item, dict) or item.get("status") != "evaluated" for item in cases)
    ):
        raise ValueError("OCR report does not contain one evaluated result for each pinned corpus case")

    cer = metrics.get("character_error_rate")
    wer = metrics.get("word_error_rate")
    critical_rate = metrics.get("critical_field_exact_rate")
    expected_cases = metrics.get("case_count")
    evaluated_cases = metrics.get("evaluated_cases")
    failures = metrics.get("incomplete_or_failed_cases")
    threshold_cer = thresholds.get("max_character_error_rate")
    threshold_wer = thresholds.get("max_word_error_rate")
    threshold_critical = thresholds.get("min_critical_field_exact_rate")
    numeric = (cer, wer, critical_rate, threshold_cer, threshold_wer, threshold_critical)
    if (
        any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in numeric)
        or expected_cases != len(corpus_cases)
        or evaluated_cases != len(corpus_cases)
        or failures != 0
        or not 0 <= cer <= threshold_cer < 1
        or not 0 <= wer <= threshold_wer < 1
        or not 0 <= threshold_critical <= critical_rate <= 1
        or metrics.get("thresholds_passed") is not True
    ):
        raise ValueError("measured quality metrics are incomplete or outside the declared software thresholds")

    return {
        "cases": len(cases),
        "cer": cer,
        "wer": wer,
        "critical_field_exact_rate": critical_rate,
        "dataset_sha256": expected_digest,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="JSON report emitted by the deployment image")
    parser.add_argument("--corpus", type=Path, required=True, help="pinned synthetic DDR OCR corpus JSON")
    args = parser.parse_args(argv)
    try:
        result = verify(args.report, args.corpus)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"SYNTHETIC_OCR_QUALITY_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        "SYNTHETIC_OCR_QUALITY_OK "
        f"cases={result['cases']} CER={result['cer']:.5f} WER={result['wer']:.5f} "
        f"critical_field_exact_rate={result['critical_field_exact_rate']:.5f} "
        f"dataset_sha256={result['dataset_sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
