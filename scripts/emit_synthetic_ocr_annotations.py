#!/usr/bin/env python3
"""Emit bounded, transcript-free annotations for a CP135 quality-report failure.

Only allowlisted aggregate metrics, runtime identifiers, case IDs, strata, hashes and counts are
printed. Recognized text, expected text, images and filesystem paths are never emitted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

MAX_ANNOTATION_BYTES = 3800
MAX_NOTICES = 9
CASE_FIELDS = (
    "id",
    "language",
    "layout",
    "scan_condition",
    "status",
    "reason",
    "pdf_sha256",
    "cer",
    "wer",
    "character_errors",
    "expected_characters",
    "word_errors",
    "expected_words",
    "critical_fields_matched",
    "critical_fields_expected",
    "processing_ms",
    "recognition_ms",
)
METRIC_FIELDS = (
    "case_count",
    "evaluated_cases",
    "incomplete_or_failed_cases",
    "character_errors",
    "expected_characters",
    "character_error_rate",
    "word_errors",
    "expected_words",
    "word_error_rate",
    "critical_field_exact_rate",
    "critical_fields_matched",
    "critical_fields_expected",
    "thresholds_passed",
    "all_strata_thresholds_passed",
)
RUNTIME_FIELDS = (
    "provider",
    "adapter_version",
    "runtime_version",
    "pdf_renderer_version",
    "required_languages",
    "configured_languages",
    "available_languages",
    "traineddata_sha256",
    "fixture_rendering_policy",
)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _escape_command(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _safe_synthetic_routing(runtime: dict[str, Any]) -> dict[str, dict[str, int]] | None:
    raw = runtime.get("synthetic_routing")
    names = ("combined_only", "fas_rerun", "unknown")
    if not isinstance(raw, dict) or set(raw) != {"eng", "fas"}:
        return None
    result: dict[str, dict[str, int]] = {}
    for language in ("eng", "fas"):
        counts = raw[language]
        if not isinstance(counts, dict) or set(counts) != set(names):
            return None
        if any(isinstance(counts[key], bool) or not isinstance(counts[key], int) or not 0 <= counts[key] <= 64 for key in names):
            return None
        result[language] = {key: counts[key] for key in names}
    return result


def _safe_synthetic_label_diagnostics(runtime: dict[str, Any]) -> dict[str, dict[str, int]] | None:
    raw = runtime.get("synthetic_label_diagnostics")
    fields = ("evaluated", "labels_anywhere", "labels_prefix", "labels_reversed", "pages_with_bidi_controls")
    if not isinstance(raw, dict) or set(raw) != {"eng", "fas"}:
        return None
    result = {}
    for language in ("eng", "fas"):
        row = raw[language]
        if not isinstance(row, dict) or set(row) != set(fields):
            return None
        if any(type(row[field]) is not int or not 0 <= row[field] <= 384 for field in fields):
            return None
        result[language] = {field: row[field] for field in fields}
    return result


def _summary(report: dict[str, Any]) -> dict[str, Any]:
    dataset = report.get("dataset") if isinstance(report.get("dataset"), dict) else {}
    metrics = report.get("metrics") if isinstance(report.get("metrics"), dict) else {}
    runtime = report.get("runtime") if isinstance(report.get("runtime"), dict) else {}
    stratified = metrics.get("stratified_metrics") if isinstance(metrics.get("stratified_metrics"), dict) else {}
    resource_limits = runtime.get("resource_limits") if isinstance(runtime.get("resource_limits"), dict) else {}
    summary = {
        "status": report.get("status"),
        "quality_evaluated": report.get("quality_evaluated"),
        "reason": report.get("reason"),
        "dataset": {
            key: dataset.get(key)
            for key in (
                "id",
                "version",
                "classification",
                "sha256",
                "case_count",
                "languages",
                "stratification_dimensions",
            )
        },
        "thresholds": report.get("thresholds"),
        "runtime": {key: runtime.get(key) for key in RUNTIME_FIELDS},
        "ocr_dpi": resource_limits.get("ocr_dpi"),
        "metrics": {key: metrics.get(key) for key in METRIC_FIELDS},
    }
    synthetic_routing = _safe_synthetic_routing(runtime)
    if synthetic_routing is not None:
        summary["runtime"]["synthetic_routing"] = synthetic_routing
    label_diagnostics = _safe_synthetic_label_diagnostics(runtime)
    if label_diagnostics is not None:
        summary["runtime"]["synthetic_label_diagnostics"] = label_diagnostics
    strata_summary: dict[str, list[dict[str, Any]]] = {}
    for dimension, rows in stratified.items():
        if not isinstance(rows, list):
            continue
        strata_summary[dimension] = [
            {
                "stratum": row.get("stratum"),
                "cases": row.get("case_count"),
                "evaluated": row.get("evaluated_cases"),
                "incomplete": row.get("incomplete_or_failed_cases"),
                "characters": [row.get("character_errors"), row.get("expected_characters"), row.get("character_error_rate")],
                "words": [row.get("word_errors"), row.get("expected_words"), row.get("word_error_rate")],
                "critical": [row.get("critical_fields_matched"), row.get("critical_fields_expected"), row.get("critical_field_exact_rate")],
                "passed": row.get("thresholds_passed"),
            }
            for row in rows
            if isinstance(row, dict)
        ]
    return {"summary": summary, "strata": strata_summary}


def _case_batches(cases: list[Any]) -> list[tuple[int, int, list[dict[str, Any]]]]:
    batches: list[tuple[int, int, list[dict[str, Any]]]] = []
    batch: list[dict[str, Any]] = []
    first = 1
    for case in cases:
        if not isinstance(case, dict):
            continue
        row = {key: case.get(key) for key in CASE_FIELDS}
        candidate = [*batch, row]
        if batch and len(_json_bytes({"cases": candidate})) > MAX_ANNOTATION_BYTES:
            batches.append((first, first + len(batch) - 1, batch))
            first += len(batch)
            batch = [row]
        else:
            batch = candidate
    if batch:
        batches.append((first, first + len(batch) - 1, batch))
    return batches


def annotations(report: dict[str, Any]) -> list[str]:
    notices: list[str] = []

    def emit(title: str, payload: dict[str, Any]) -> None:
        encoded = _json_bytes(payload)
        if len(encoded) > MAX_ANNOTATION_BYTES:
            raise ValueError("sanitized CP135 annotation exceeds its safe size bound")
        notices.append(f"::notice title={title}::{_escape_command(encoded.decode('utf-8'))}")

    emit("CP135 OCR runtime, global and stratified metrics", _summary(report))
    cases = report.get("cases")
    if isinstance(cases, list):
        for first, last, rows in _case_batches(cases):
            emit(f"CP135 OCR cases {first:03d}-{last:03d}", {"cases": rows})
    if len(notices) > MAX_NOTICES:
        raise ValueError("CP135 diagnostic exceeded the GitHub per-step annotation bound")
    return notices


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("Usage: emit_synthetic_ocr_annotations.py REPORT.json", file=sys.stderr)
        return 2
    try:
        raw = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("quality report root is not an object")
        output = annotations(raw)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        # Diagnostic generation must never turn a quality failure into a green or masked result.
        print("::notice title=CP135 OCR diagnostic::quality report was unavailable or exceeded diagnostic bounds")
        return 0
    for item in output:
        print(item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
