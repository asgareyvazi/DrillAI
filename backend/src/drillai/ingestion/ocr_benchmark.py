"""Reproducible, local-only OCR quality benchmark runner.

The manifest names a governed corpus and SHA-256-pinned expected text. Operator-supplied corpora require
self-attested approval metadata; explicitly synthetic corpora do not, and are restricted to the development
split. The runner never downloads content, stores transcripts in its report, or treats Tesseract confidence
as semantic correctness. A case is scored only when every selected page was actually OCR-processed; missing
runtimes, limits, or incomplete pages remain explicit and produce a non-zero CLI exit status.

Example (manifest and corpus are operator-provided and must not be committed if restricted)::

    python -m drillai.ingestion.ocr_benchmark \\
      --manifest /secure/ocr/manifest.json --corpus-root /secure/ocr/corpus \\
      --languages eng+fas --output /secure/ocr/benchmark-result.json

Manifest schema 2 (all file paths are relative to ``--corpus-root``). A synthetic fixture explicitly
avoids external approval claims and is restricted to the development split::

    {"schema_version": 2, "corpus": {
      "id": "drillai-synthetic-ddr-ocr", "version": "1.0.0", "classification": "synthetic",
      "approval": {"declaration": "not_required_synthetic"}, "languages": ["eng"],
      "sampling_method": "one deterministic generated raster page; not field-sampled",
      "ground_truth_protocol": "fixture reference text; not independently adjudicated"
    }, "cases": [{
      "id": "synthetic-scan-001", "pdf": "scans/scan-001.pdf", "pdf_sha256": "…",
      "ground_truth": "labels/scan-001.txt", "ground_truth_sha256": "…",
      "locale": "en", "document_class": "daily_report", "split": "development",
      "page_numbers": [1]
    }]}

For a non-synthetic operator corpus, the manifest instead requires a self-attested approval declaration,
reference, owner role, timestamp, sampling method, and ground-truth protocol. That self-attestation is
reported as unverified by this tool and must not be fabricated for test fixtures.

``page_numbers`` is optional; when omitted, every page must have been OCR-processed. CER/WER use
NFKC, case-folding and whitespace normalization and exact Levenshtein distance. Work per metric is
bounded to avoid turning a large corpus manifest into unbounded CPU use.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from drillai.ingestion.ocr import OcrUnavailable, TesseractOcrProvider
from drillai.ingestion.ocr_runtime import _pdfium_details
from drillai.ingestion.parsers import MAX_BYTES, parse_bytes

TOOL_VERSION = "ocr-benchmark-v2.1"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_LABEL_BYTES = 2 * 1024 * 1024
MAX_CASES = 1000
MAX_EDIT_CELLS = 25_000_000
MAX_TOTAL_EDIT_CELLS = 250_000_000
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


class BenchmarkInputError(ValueError):
    """A manifest or corpus file is malformed, unsafe, or does not match its pinned digest."""


@dataclass(frozen=True)
class BenchmarkCase:
    case_id: str
    pdf_path: str
    pdf_sha256: str
    ground_truth_path: str
    ground_truth_sha256: str
    page_numbers: tuple[int, ...] | None
    locale: str
    document_class: str
    split: str


def normalize_for_scoring(text: str) -> str:
    """Normalize Unicode compatibility forms and whitespace without dropping engineering tokens."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _distance_work_components(expected: str, actual: str) -> tuple[int, int]:
    expected_normalized = normalize_for_scoring(expected)
    actual_normalized = normalize_for_scoring(actual)
    character_cells = len(expected_normalized) * len(actual_normalized)
    word_cells = len(expected_normalized.split()) * len(actual_normalized.split())
    return character_cells, word_cells


def levenshtein_distance(left: str, right: str, *, max_cells: int = MAX_EDIT_CELLS) -> int:
    """Exact character/token edit distance with O(min(n,m)) memory and a deterministic work bound."""
    if not isinstance(left, str) or not isinstance(right, str):
        raise TypeError("Levenshtein inputs must be strings")
    if max_cells < 1:
        raise ValueError("max_cells must be positive")
    if len(left) * len(right) > max_cells:
        raise BenchmarkInputError(
            f"edit-distance workload exceeds the {max_cells:,}-cell benchmark limit"
        )
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for right_index, right_char in enumerate(right, start=1):
        current = [right_index]
        for left_index, left_char in enumerate(left, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[left_index] + 1,
                    previous[left_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def error_rates(expected: str, actual: str, *, max_cells: int = MAX_EDIT_CELLS) -> dict[str, int | float]:
    """Return exact CER/WER error counts and rates after the benchmark's documented normalization."""
    expected_normalized = normalize_for_scoring(expected)
    actual_normalized = normalize_for_scoring(actual)
    if not expected_normalized:
        raise BenchmarkInputError("ground-truth text must not be empty after normalization")
    char_errors = levenshtein_distance(expected_normalized, actual_normalized, max_cells=max_cells)
    expected_words = expected_normalized.split()
    actual_words = actual_normalized.split()
    # Token-level WER is measured over word strings, not across a delimiter-joined approximation.
    if len(expected_words) * len(actual_words) > max_cells:
        raise BenchmarkInputError(
            f"word edit-distance workload exceeds the {max_cells:,}-cell benchmark limit"
        )
    word_errors = _token_levenshtein(expected_words, actual_words)
    return {
        "expected_characters": len(expected_normalized),
        "actual_characters": len(actual_normalized),
        "character_errors": char_errors,
        "cer": char_errors / len(expected_normalized),
        "expected_words": len(expected_words),
        "actual_words": len(actual_words),
        "word_errors": word_errors,
        "wer": word_errors / len(expected_words),
    }


def _token_levenshtein(left: list[str], right: list[str]) -> int:
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for right_index, right_token in enumerate(right, start=1):
        current = [right_index]
        for left_index, left_token in enumerate(left, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[left_index] + 1,
                    previous[left_index - 1] + (left_token != right_token),
                )
            )
        previous = current
    return previous[-1]


def _checked_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise BenchmarkInputError(f"{field} must be a 64-character hexadecimal SHA-256")
    return value.lower()


def _checked_text(value: Any, field: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise BenchmarkInputError(f"{field} must be non-empty printable text no longer than {maximum} characters")
    return value.strip()


def _validate_corpus_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "id", "version", "classification", "approval", "languages", "sampling_method", "ground_truth_protocol"
    }:
        raise BenchmarkInputError("corpus must declare identity, classification, approval, languages, sampling and gold-label protocol")
    corpus_id = _checked_text(value.get("id"), "corpus.id", maximum=120)
    corpus_version = _checked_text(value.get("version"), "corpus.version", maximum=80)
    classification = _checked_text(value.get("classification"), "corpus.classification", maximum=80)
    if not _CASE_ID_PATTERN.fullmatch(corpus_id) or not _CASE_ID_PATTERN.fullmatch(corpus_version):
        raise BenchmarkInputError("corpus.id and corpus.version must use letters, digits, dot, underscore or hyphen")
    approval = value.get("approval")
    if classification == "synthetic":
        if not isinstance(approval, dict) or set(approval) != {"declaration"}:
            raise BenchmarkInputError(
                "synthetic corpus.approval must contain only declaration: not_required_synthetic; "
                "external approval evidence is not applicable"
            )
        if approval.get("declaration") != "not_required_synthetic":
            raise BenchmarkInputError("synthetic corpus.approval.declaration must be not_required_synthetic")
        approval_metadata = {"declaration": "not_required_synthetic"}
    else:
        if not isinstance(approval, dict) or set(approval) != {
            "declaration", "reference", "owner_role", "approved_at"
        }:
            raise BenchmarkInputError(
                "corpus.approval must declare a self-attested approval reference, owner role and timestamp"
            )
        if approval.get("declaration") != "operator_attests_approved":
            raise BenchmarkInputError("corpus.approval.declaration must be operator_attests_approved")
        approval_metadata = {
            "declaration": "operator_attests_approved",
            "reference": _checked_text(approval.get("reference"), "corpus.approval.reference", maximum=240),
            "owner_role": _checked_text(approval.get("owner_role"), "corpus.approval.owner_role", maximum=120),
            "approved_at": _checked_text(approval.get("approved_at"), "corpus.approval.approved_at", maximum=40),
        }
        try:
            approved_at = dt.datetime.fromisoformat(approval_metadata["approved_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise BenchmarkInputError(
                "corpus.approval.approved_at must be an ISO-8601 timezone-aware timestamp"
            ) from exc
        if approved_at.tzinfo is None or approved_at.utcoffset() is None:
            raise BenchmarkInputError("corpus.approval.approved_at must include a timezone")
    languages = value.get("languages")
    if (
        not isinstance(languages, list)
        or not languages
        or any(not isinstance(language, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", language) for language in languages)
        or languages != sorted(set(languages))
    ):
        raise BenchmarkInputError("corpus.languages must be sorted, unique Tesseract language identifiers")
    return {
        "id": corpus_id,
        "version": corpus_version,
        "classification": classification,
        "approval": approval_metadata,
        "languages": languages,
        "sampling_method": _checked_text(value.get("sampling_method"), "corpus.sampling_method", maximum=1000),
        "ground_truth_protocol": _checked_text(
            value.get("ground_truth_protocol"), "corpus.ground_truth_protocol", maximum=1000
        ),
    }


def _checked_relative_path(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise BenchmarkInputError(f"{field} must be a non-empty POSIX relative path")
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    parts = value.split("/")
    if posix_path.is_absolute() or windows_path.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        raise BenchmarkInputError(f"{field} must be a normalized path inside the corpus root")
    return value


def _resolve_corpus_path(root: Path, relative: str, *, field: str) -> Path:
    resolved_root = root.resolve(strict=True)
    candidate = (resolved_root / relative).resolve(strict=True)
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise BenchmarkInputError(f"{field} resolves outside the corpus root") from exc
    if not candidate.is_file():
        raise BenchmarkInputError(f"{field} must name a regular file")
    return candidate


def load_manifest(path: Path) -> tuple[dict[str, Any], list[BenchmarkCase], str]:
    manifest_path = path.resolve(strict=True)
    if not manifest_path.is_file() or manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise BenchmarkInputError(f"manifest must be a regular file no larger than {MAX_MANIFEST_BYTES} bytes")
    raw = manifest_path.read_bytes()
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkInputError("manifest must be valid UTF-8 JSON") from exc
    if (
        not isinstance(document, dict)
        or isinstance(document.get("schema_version"), bool)
        or document.get("schema_version") != 2
    ):
        raise BenchmarkInputError("manifest schema_version must be 2")
    if set(document) != {"schema_version", "corpus", "cases"}:
        raise BenchmarkInputError("manifest must contain only schema_version, corpus and cases")
    corpus = _validate_corpus_metadata(document.get("corpus"))
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= MAX_CASES:
        raise BenchmarkInputError(f"manifest cases must contain between 1 and {MAX_CASES} entries")
    cases: list[BenchmarkCase] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_cases):
        if not isinstance(item, dict):
            raise BenchmarkInputError(f"case {index} must be an object")
        required_fields = {
            "id", "pdf", "pdf_sha256", "ground_truth", "ground_truth_sha256", "locale", "document_class", "split"
        }
        allowed_fields = required_fields | {"page_numbers"}
        if set(item) - allowed_fields or not required_fields.issubset(item):
            raise BenchmarkInputError(f"case {index} must contain the required fields and no unsupported fields")
        case_id = item.get("id")
        if not isinstance(case_id, str) or not _CASE_ID_PATTERN.fullmatch(case_id) or case_id in seen_ids:
            raise BenchmarkInputError(f"case {index} has an invalid or duplicate id")
        seen_ids.add(case_id)
        locale = item.get("locale")
        if not isinstance(locale, str) or locale not in {"en", "fa", "mixed", "unknown"}:
            raise BenchmarkInputError(f"case {index}.locale must be en, fa, mixed or unknown")
        document_class = _checked_text(item.get("document_class"), f"cases[{index}].document_class", maximum=80)
        if not _CASE_ID_PATTERN.fullmatch(document_class):
            raise BenchmarkInputError(f"case {index}.document_class must use letters, digits, dot, underscore or hyphen")
        split = item.get("split")
        if not isinstance(split, str) or split not in {"development", "validation", "holdout"}:
            raise BenchmarkInputError(f"case {index}.split must be development, validation or holdout")
        if corpus["classification"] == "synthetic" and split != "development":
            raise BenchmarkInputError("synthetic corpus cases must use the development split; holdout claims are not allowed")
        if locale == "en" and "eng" not in corpus["languages"]:
            raise BenchmarkInputError(f"case {index} is English but corpus.languages omits eng")
        if locale == "fa" and "fas" not in corpus["languages"]:
            raise BenchmarkInputError(f"case {index} is Persian but corpus.languages omits fas")
        if locale == "mixed" and not {"eng", "fas"}.issubset(corpus["languages"]):
            raise BenchmarkInputError(f"case {index} is mixed English/Persian but corpus.languages omits eng or fas")
        pdf_path = _checked_relative_path(item.get("pdf"), f"cases[{index}].pdf")
        label_path = _checked_relative_path(item.get("ground_truth"), f"cases[{index}].ground_truth")
        raw_pages = item.get("page_numbers")
        page_numbers: tuple[int, ...] | None = None
        if raw_pages is not None:
            if (
                not isinstance(raw_pages, list)
                or not raw_pages
                or any(isinstance(page, bool) or not isinstance(page, int) or page < 1 for page in raw_pages)
                or raw_pages != sorted(set(raw_pages))
            ):
                raise BenchmarkInputError(f"cases[{index}].page_numbers must be sorted, unique positive integers")
            page_numbers = tuple(raw_pages)
        cases.append(
            BenchmarkCase(
                case_id=case_id,
                pdf_path=pdf_path,
                pdf_sha256=_checked_digest(item.get("pdf_sha256"), f"cases[{index}].pdf_sha256"),
                ground_truth_path=label_path,
                ground_truth_sha256=_checked_digest(
                    item.get("ground_truth_sha256"), f"cases[{index}].ground_truth_sha256"
                ),
                page_numbers=page_numbers,
                locale=locale,
                document_class=document_class,
                split=split,
            )
        )
    document["corpus"] = corpus
    return document, cases, hashlib.sha256(raw).hexdigest()


def _read_pinned_file(
    root: Path,
    relative: str,
    expected_sha256: str,
    *,
    max_bytes: int,
    label: str,
) -> tuple[bytes, Path]:
    path = _resolve_corpus_path(root, relative, field=label)
    if path.stat().st_size > max_bytes:
        raise BenchmarkInputError(f"{label} exceeds the {max_bytes:,}-byte benchmark limit")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual, expected_sha256):
        raise BenchmarkInputError(f"{label} SHA-256 does not match the pinned manifest digest")
    return data, path


def run_benchmark(
    manifest: dict[str, Any],
    cases: list[BenchmarkCase],
    manifest_sha256: str,
    *,
    corpus_root: Path,
    provider: TesseractOcrProvider,
    max_pages: int = 20,
    dpi: int = 200,
    max_page_pixels: int = 12_000_000,
    max_total_edit_cells: int = MAX_TOTAL_EDIT_CELLS,
) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        raise BenchmarkInputError("run_benchmark requires a validated schema-version-2 manifest")
    corpus = _validate_corpus_metadata(manifest.get("corpus"))
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise BenchmarkInputError(f"benchmark requires between 1 and {MAX_CASES} validated corpus cases")
    if any(not isinstance(case, BenchmarkCase) for case in cases):
        raise BenchmarkInputError("benchmark cases must be validated BenchmarkCase entries")
    synthetic_corpus = corpus["classification"] == "synthetic"
    if synthetic_corpus and any(case.split != "development" for case in cases):
        raise BenchmarkInputError("synthetic corpus cases must use the development split; holdout claims are not allowed")
    manifest_sha256 = _checked_digest(manifest_sha256, "manifest_sha256")
    if (
        isinstance(max_total_edit_cells, bool)
        or not isinstance(max_total_edit_cells, int)
        or not 1 <= max_total_edit_cells <= MAX_TOTAL_EDIT_CELLS
    ):
        raise BenchmarkInputError(f"max_total_edit_cells must be between 1 and {MAX_TOTAL_EDIT_CELLS:,}")
    diagnostic_method = getattr(provider, "diagnostics", None)
    diagnostics = diagnostic_method() if callable(diagnostic_method) else {}
    if diagnostics:
        available = bool(diagnostics.get("available"))
        unavailable_reason = diagnostics.get("reason")
    else:
        available, unavailable_reason = provider.availability()
    configured_languages = getattr(provider, "languages", [])
    if isinstance(configured_languages, str):
        configured_languages = configured_languages.split("+")
    if not isinstance(configured_languages, (list, tuple)):
        configured_languages = []
    corpus_languages = corpus.get("languages", [])
    if available and corpus_languages and not set(corpus_languages).issubset(set(configured_languages)):
        available = False
        unavailable_reason = "configured Tesseract languages do not cover the declared corpus languages"
    pdfium_available, pdfium_version = _pdfium_details()
    if available and not pdfium_available:
        available = False
        unavailable_reason = "PDFium renderer is not installed or could not be loaded"
    report: dict[str, Any] = {
        "tool": TOOL_VERSION,
        "manifest_schema_version": manifest.get("schema_version"),
        "manifest_sha256": manifest_sha256,
        "corpus": corpus,
        "corpus_approval_evidence": (
            "not_applicable_synthetic_corpus"
            if synthetic_corpus
            else "operator_attested_not_independently_verified"
        ),
        "split_integrity_evidence": (
            "single_development_fixture_not_independent"
            if synthetic_corpus
            else "operator_declared_not_independently_verified"
        ),
        "provider": provider.name,
        "provider_version": provider.version,
        "provider_adapter_version": getattr(provider, "adapter_version", None),
        "provider_runtime_version": diagnostics.get("runtime_version"),
        "provider_available": available,
        "configured_languages": list(configured_languages),
        "available_languages": diagnostics.get("available_languages", []),
        "missing_languages": diagnostics.get("missing_languages", []),
        "pdf_renderer_available": pdfium_available,
        "pdf_renderer_version": pdfium_version,
        "evaluation_scope": (
            "development_only"
            if synthetic_corpus
            else "holdout_included" if any(case.split == "holdout" for case in cases) else "development_only"
        ),
        "metrics_scope_note": (
            "Synthetic development-fixture metrics do not estimate real-world OCR performance or establish field, "
            "engineering, or production acceptance."
            if synthetic_corpus
            else "pooled totals mix declared splits; holdout independence is unverified; use split_totals.holdout for operator-declared holdout-only metrics"
        ),
        "quality_assessment": (
            "synthetic fixture transcription metrics only; no real-corpus, field, engineering, or production acceptance"
            if synthetic_corpus
            else "transcription error metrics only; no engineering or production acceptance"
        ),
        "configuration": {
            "languages": list(configured_languages),
            "max_pages": max_pages,
            "dpi": dpi,
            "max_page_pixels": max_page_pixels,
            "max_concurrency": getattr(provider, "max_concurrency", None),
            "max_total_edit_cells": max_total_edit_cells,
        },
        "status": "running" if available else "blocked",
        "blocked_reason": unavailable_reason,
        "cases": [],
        "totals": None,
        "split_totals": {},
        "strata": [],
    }
    if not available:
        return report

    expected_characters = actual_characters = character_errors = 0
    expected_words = actual_words = word_errors = 0
    failed_cases = 0
    total_edit_cells_used = 0
    strata: dict[tuple[str, str, str], dict[str, int]] = {}
    for case in cases:
        result: dict[str, Any] = {
            "id": case.case_id,
            "pdf_sha256": case.pdf_sha256,
            "ground_truth_sha256": case.ground_truth_sha256,
            "locale": case.locale,
            "document_class": case.document_class,
            "split": case.split,
            "status": "failed",
        }
        try:
            data, _pdf_path = _read_pinned_file(
                corpus_root,
                case.pdf_path,
                case.pdf_sha256,
                max_bytes=MAX_BYTES,
                label=f"case {case.case_id} PDF",
            )
            ground_truth_bytes, _label_path = _read_pinned_file(
                corpus_root,
                case.ground_truth_path,
                case.ground_truth_sha256,
                max_bytes=MAX_LABEL_BYTES,
                label=f"case {case.case_id} ground truth",
            )
            try:
                ground_truth = ground_truth_bytes.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise BenchmarkInputError(f"case {case.case_id} ground truth must be UTF-8") from exc
            parsed = parse_bytes(
                data,
                filename=case.pdf_path,
                content_type="application/pdf",
                ocr_provider=provider,
                ocr_max_pages=max_pages,
                ocr_dpi=dpi,
                ocr_max_page_pixels=max_page_pixels,
            )
            if case.page_numbers is None:
                selected_pages = parsed.pages
            else:
                by_number = {page.page_number: page for page in parsed.pages}
                missing = [page_number for page_number in case.page_numbers if page_number not in by_number]
                if missing:
                    raise BenchmarkInputError(f"case {case.case_id} references missing page(s): {missing}")
                selected_pages = [by_number[page_number] for page_number in case.page_numbers]
            incomplete_pages = [
                page for page in selected_pages if not page.ocr_applied or page.ocr_status == "truncated"
            ]
            if incomplete_pages:
                incomplete_numbers = [page.page_number for page in incomplete_pages]
                result.update(
                    {
                        "status": "incomplete",
                        "reason": f"OCR did not return a complete result for selected page(s): {incomplete_numbers}",
                        "ocr_status_by_page": {
                            str(page.page_number): page.ocr_status for page in selected_pages
                        },
                    }
                )
                failed_cases += 1
                report["cases"].append(result)
                continue
            actual_text = "\n".join(page.text for page in selected_pages)
            normalized_truth = normalize_for_scoring(ground_truth)
            normalized_actual = normalize_for_scoring(actual_text)
            if not normalized_truth:
                raise BenchmarkInputError("ground-truth text must not be empty after normalization")
            character_cells, word_cells = _distance_work_components(normalized_truth, normalized_actual)
            if character_cells > MAX_EDIT_CELLS or word_cells > MAX_EDIT_CELLS:
                raise BenchmarkInputError(
                    f"per-metric edit-distance workload exceeds the {MAX_EDIT_CELLS:,}-cell case limit"
                )
            case_work = character_cells + word_cells
            if case_work > max_total_edit_cells - total_edit_cells_used:
                raise BenchmarkInputError(
                    f"aggregate edit-distance work would exceed the {max_total_edit_cells:,}-cell run limit"
                )
            total_edit_cells_used += case_work
            metrics = error_rates(ground_truth, actual_text)
            result.update(
                {
                    "status": "evaluated",
                    "page_numbers": [page.page_number for page in selected_pages],
                    **metrics,
                }
            )
            expected_characters += int(metrics["expected_characters"])
            actual_characters += int(metrics["actual_characters"])
            character_errors += int(metrics["character_errors"])
            expected_words += int(metrics["expected_words"])
            actual_words += int(metrics["actual_words"])
            word_errors += int(metrics["word_errors"])
            key = (case.locale, case.document_class, case.split)
            bucket = strata.setdefault(
                key,
                {
                    "evaluated_cases": 0,
                    "expected_characters": 0,
                    "actual_characters": 0,
                    "character_errors": 0,
                    "expected_words": 0,
                    "actual_words": 0,
                    "word_errors": 0,
                },
            )
            bucket["evaluated_cases"] += 1
            for metric_key in (
                "expected_characters", "actual_characters", "character_errors",
                "expected_words", "actual_words", "word_errors",
            ):
                bucket[metric_key] += int(metrics[metric_key])
        except BenchmarkInputError as exc:
            # These messages name schema fields/case IDs, not local paths or file contents.
            result.update({"status": "failed", "reason": str(exc)[:500]})
            failed_cases += 1
        except OcrUnavailable as exc:
            result.update({"status": "failed", "reason": str(exc)[:500]})
            failed_cases += 1
        except OSError as exc:
            result.update({"status": "failed", "reason": f"local corpus/runtime I/O failed ({type(exc).__name__})"})
            failed_cases += 1
        except Exception as exc:
            # Parser/provider exceptions can embed filenames, text fragments or operating-system paths.
            result.update({"status": "failed", "reason": f"processing failed ({type(exc).__name__})"})
            failed_cases += 1
        report["cases"].append(result)

    evaluated = sum(case["status"] == "evaluated" for case in report["cases"])
    report["totals"] = {
        "case_count": len(cases),
        "evaluated_cases": evaluated,
        "holdout_cases": sum(case.split == "holdout" for case in cases),
        "holdout_evaluated_cases": sum(
            case["status"] == "evaluated" and case["split"] == "holdout" for case in report["cases"]
        ),
        "incomplete_or_failed_cases": failed_cases,
        "edit_distance_cells_used": total_edit_cells_used,
        "edit_distance_cell_limit": max_total_edit_cells,
        "expected_characters": expected_characters,
        "actual_characters": actual_characters,
        "character_errors": character_errors,
        "cer": character_errors / expected_characters if expected_characters else None,
        "expected_words": expected_words,
        "actual_words": actual_words,
        "word_errors": word_errors,
        "wer": word_errors / expected_words if expected_words else None,
    }
    report["strata"] = [
        {
            "locale": key[0],
            "document_class": key[1],
            "split": key[2],
            **values,
            "cer": values["character_errors"] / values["expected_characters"] if values["expected_characters"] else None,
            "wer": values["word_errors"] / values["expected_words"] if values["expected_words"] else None,
        }
        for key, values in sorted(strata.items())
    ]
    for split in sorted({case["split"] for case in report["cases"]}):
        split_cases = [case for case in report["cases"] if case["split"] == split]
        evaluated_cases = [case for case in split_cases if case["status"] == "evaluated"]
        split_expected_characters = sum(int(case.get("expected_characters", 0)) for case in evaluated_cases)
        split_expected_words = sum(int(case.get("expected_words", 0)) for case in evaluated_cases)
        split_character_errors = sum(int(case.get("character_errors", 0)) for case in evaluated_cases)
        split_word_errors = sum(int(case.get("word_errors", 0)) for case in evaluated_cases)
        report["split_totals"][split] = {
            "case_count": len(split_cases),
            "evaluated_cases": len(evaluated_cases),
            "incomplete_or_failed_cases": len(split_cases) - len(evaluated_cases),
            "expected_characters": split_expected_characters,
            "character_errors": split_character_errors,
            "cer": split_character_errors / split_expected_characters if split_expected_characters else None,
            "expected_words": split_expected_words,
            "word_errors": split_word_errors,
            "wer": split_word_errors / split_expected_words if split_expected_words else None,
            "status": "complete" if len(evaluated_cases) == len(split_cases) else "incomplete",
        }
    report["status"] = "complete" if failed_cases == 0 and evaluated == len(cases) else "incomplete"
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a bounded, local OCR benchmark against a SHA-256-pinned manifest."
    )
    parser.add_argument("--manifest", required=True, type=Path, help="SHA-256-pinned JSON corpus manifest")
    parser.add_argument("--corpus-root", required=True, type=Path, help="directory containing the manifest-pinned corpus")
    parser.add_argument("--languages", default="eng+fas", help="installed Tesseract language ids, e.g. eng+fas")
    parser.add_argument("--tesseract", default="tesseract", help="Tesseract executable name or path")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-page OCR timeout in seconds")
    parser.add_argument("--max-pages", type=int, default=20, help="maximum blank pages OCR processes per PDF")
    parser.add_argument("--max-concurrency", type=int, default=2, help="maximum concurrent Tesseract processes")
    parser.add_argument("--dpi", type=int, default=200, help="target rendering DPI, capped by pixel budget")
    parser.add_argument("--max-page-pixels", type=int, default=12_000_000, help="rendered pixel ceiling per page")
    parser.add_argument(
        "--max-total-edit-cells",
        type=int,
        default=MAX_TOTAL_EDIT_CELLS,
        help=f"total CER/WER edit-distance work cap, at most {MAX_TOTAL_EDIT_CELLS:,}",
    )
    parser.add_argument("--output", type=Path, help="write the JSON report to this path instead of stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest, cases, manifest_sha256 = load_manifest(args.manifest)
        corpus_root = args.corpus_root.resolve(strict=True)
        if not corpus_root.is_dir():
            raise BenchmarkInputError("corpus root must be a directory")
        provider = TesseractOcrProvider(
            languages=args.languages,
            executable=str(args.tesseract),
            timeout_seconds=args.timeout,
            max_concurrency=args.max_concurrency,
        )
        report = run_benchmark(
            manifest,
            cases,
            manifest_sha256,
            corpus_root=corpus_root,
            provider=provider,
            max_pages=args.max_pages,
            dpi=args.dpi,
            max_page_pixels=args.max_page_pixels,
            max_total_edit_cells=args.max_total_edit_cells,
        )
        serialized = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
        if args.output:
            args.output.write_text(serialized, encoding="utf-8")
        else:
            sys.stdout.write(serialized)
        if report["status"] == "blocked":
            return 2
        return 0 if report["status"] == "complete" else 1
    except (OSError, BenchmarkInputError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"OCR benchmark input/runtime error: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
