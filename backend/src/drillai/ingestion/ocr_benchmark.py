"""Reproducible, local-only OCR quality benchmark runner.

The manifest names an approved corpus and SHA-256-pinned expected text. The runner never downloads
content, stores transcripts in its report, or treats Tesseract confidence as semantic correctness. A
case is scored only when every selected page was actually OCR-processed; missing runtimes, limits, or
incomplete pages remain explicit and produce a non-zero CLI exit status.

Example (manifest and corpus are operator-provided and must not be committed if restricted)::

    python -m drillai.ingestion.ocr_benchmark \\
      --manifest /secure/ocr/manifest.json --corpus-root /secure/ocr/corpus \\
      --languages eng+fas --output /secure/ocr/benchmark-result.json

Manifest schema (all file paths are relative to ``--corpus-root``)::

    {"schema_version": 1, "cases": [{
      "id": "scan-001", "pdf": "scans/scan-001.pdf", "pdf_sha256": "…",
      "ground_truth": "labels/scan-001.txt", "ground_truth_sha256": "…",
      "page_numbers": [1]
    }]}

``page_numbers`` is optional; when omitted, every page must have been OCR-processed. CER/WER use
NFKC, case-folding and whitespace normalization and exact Levenshtein distance. Work per metric is
bounded to avoid turning a large corpus manifest into unbounded CPU use.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from drillai.ingestion.ocr import TesseractOcrProvider
from drillai.ingestion.parsers import MAX_BYTES, parse_bytes

TOOL_VERSION = "ocr-benchmark-v1"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_LABEL_BYTES = 2 * 1024 * 1024
MAX_CASES = 1000
MAX_EDIT_CELLS = 25_000_000
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


def normalize_for_scoring(text: str) -> str:
    """Normalize Unicode compatibility forms and whitespace without dropping engineering tokens."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


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
        or document.get("schema_version") != 1
    ):
        raise BenchmarkInputError("manifest schema_version must be 1")
    if set(document) != {"schema_version", "cases"}:
        raise BenchmarkInputError("manifest may contain only schema_version and cases")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not 1 <= len(raw_cases) <= MAX_CASES:
        raise BenchmarkInputError(f"manifest cases must contain between 1 and {MAX_CASES} entries")
    cases: list[BenchmarkCase] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_cases):
        if not isinstance(item, dict):
            raise BenchmarkInputError(f"case {index} must be an object")
        allowed_fields = {"id", "pdf", "pdf_sha256", "ground_truth", "ground_truth_sha256", "page_numbers"}
        if set(item) - allowed_fields:
            raise BenchmarkInputError(f"case {index} contains unsupported field(s)")
        case_id = item.get("id")
        if not isinstance(case_id, str) or not _CASE_ID_PATTERN.fullmatch(case_id) or case_id in seen_ids:
            raise BenchmarkInputError(f"case {index} has an invalid or duplicate id")
        seen_ids.add(case_id)
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
            )
        )
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
) -> dict[str, Any]:
    available, unavailable_reason = provider.availability()
    report: dict[str, Any] = {
        "tool": TOOL_VERSION,
        "manifest_schema_version": manifest.get("schema_version"),
        "manifest_sha256": manifest_sha256,
        "provider": provider.name,
        "provider_adapter_version": provider.version,
        "provider_available": available,
        "configuration": {
            "languages": provider.languages,
            "max_pages": max_pages,
            "dpi": dpi,
            "max_page_pixels": max_page_pixels,
        },
        "status": "running" if available else "blocked",
        "blocked_reason": unavailable_reason,
        "cases": [],
        "totals": None,
    }
    if not available:
        return report

    expected_characters = actual_characters = character_errors = 0
    expected_words = actual_words = word_errors = 0
    failed_cases = 0
    for case in cases:
        result: dict[str, Any] = {
            "id": case.case_id,
            "pdf_sha256": case.pdf_sha256,
            "ground_truth_sha256": case.ground_truth_sha256,
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
        except Exception as exc:
            result.update({"status": "failed", "reason": str(exc)[:500]})
            failed_cases += 1
        report["cases"].append(result)

    evaluated = sum(case["status"] == "evaluated" for case in report["cases"])
    report["totals"] = {
        "case_count": len(cases),
        "evaluated_cases": evaluated,
        "incomplete_or_failed_cases": failed_cases,
        "expected_characters": expected_characters,
        "actual_characters": actual_characters,
        "character_errors": character_errors,
        "cer": character_errors / expected_characters if expected_characters else None,
        "expected_words": expected_words,
        "actual_words": actual_words,
        "word_errors": word_errors,
        "wer": word_errors / expected_words if expected_words else None,
    }
    report["status"] = "complete" if failed_cases == 0 and evaluated == len(cases) else "incomplete"
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a bounded, local OCR benchmark against a SHA-256-pinned manifest."
    )
    parser.add_argument("--manifest", required=True, type=Path, help="SHA-256-pinned JSON corpus manifest")
    parser.add_argument("--corpus-root", required=True, type=Path, help="directory containing the approved corpus")
    parser.add_argument("--languages", default="eng", help="installed Tesseract language ids, e.g. eng+fas")
    parser.add_argument("--tesseract", default="tesseract", help="Tesseract executable name or path")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-page OCR timeout in seconds")
    parser.add_argument("--max-pages", type=int, default=20, help="maximum blank pages OCR processes per PDF")
    parser.add_argument("--dpi", type=int, default=200, help="target rendering DPI, capped by pixel budget")
    parser.add_argument("--max-page-pixels", type=int, default=12_000_000, help="rendered pixel ceiling per page")
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
