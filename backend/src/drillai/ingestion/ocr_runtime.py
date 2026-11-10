"""Operator-facing OCR runtime preflight and health reporting.

This command reports only local software/runtime availability. It never reads a corpus or uploads
content, contacts a provider, evaluates transcription quality, or claims engineering validation.
Run it inside the same container/venv as the API so that the diagnosed PDFium, Tesseract binary, and
language packs are the ones ingestion will use.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import io
import json
import sys
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

from drillai.core.config import Settings, get_settings
from drillai.ingestion.ocr import (
    TESSERACT_ADAPTER_VERSION,
    TESSERACT_PAGE_SEGMENTATION_MODE,
    OcrUnavailable,
    TesseractOcrProvider,
)

_RUNTIME_PROBE_LOCK = threading.Lock()


def _pdfium_details() -> tuple[bool, str | None]:
    try:
        import pypdfium2  # noqa: F401
    except Exception:
        # Import errors can include native-library loader messages and local paths; report a stable,
        # non-sensitive reason instead of leaking deployment filesystem details through health APIs.
        return False, None
    try:
        return True, importlib.metadata.version("pypdfium2")
    except importlib.metadata.PackageNotFoundError:
        return True, None


@lru_cache(maxsize=32)
def _runtime_snapshot(
    executable: str,
    languages: str,
    timeout_seconds: float,
    max_concurrency: int,
) -> tuple[dict[str, object], bool, str | None]:
    """Probe immutable deployment dependencies once per process/configuration.

    Runtime packages and language packs belong to the deployed image. Restarting after an operator
    changes them clears this cache and makes readiness observe the new deployment.
    """
    provider = TesseractOcrProvider(
        languages=languages,
        executable=executable,
        timeout_seconds=timeout_seconds,
        max_concurrency=max_concurrency,
    )
    return provider.diagnostics(), *_pdfium_details()


def inspect_ocr_runtime(
    settings: Settings | None = None,
    *,
    probe_when_disabled: bool = True,
) -> dict[str, Any]:
    """Describe configured OCR requirements, without treating presence as quality evidence."""
    resolved = settings or get_settings()
    configuration = {
        "languages": resolved.ocr_languages.split("+"),
        "page_segmentation_mode": TESSERACT_PAGE_SEGMENTATION_MODE,
        "timeout_seconds_per_page": resolved.ocr_timeout_seconds,
        "max_blank_pages_per_document": resolved.ocr_max_pages,
        "max_page_pixels": resolved.ocr_max_page_pixels,
        "max_concurrent_tesseract_processes": resolved.ocr_max_concurrency,
    }
    base: dict[str, Any] = {
        "enabled": resolved.ocr_enabled,
        "status": "disabled" if not resolved.ocr_enabled else "blocked",
        "runtime_status": "not_checked",
        "runtime_ready": None,
        "required_ready": not resolved.ocr_enabled,
        "provider": "tesseract",
        "adapter_version": TESSERACT_ADAPTER_VERSION,
        "runtime_version": None,
        "configured_languages": configuration["languages"],
        "available_languages": [],
        "missing_languages": configuration["languages"],
        "tesseract_available": None,
        "pdf_renderer_available": None,
        "pdf_renderer_version": None,
        "configuration": configuration,
        "quality_evaluated": False,
        "note": (
            "Runtime availability and word confidence do not establish OCR accuracy, source correctness, "
            "or engineering acceptance."
        ),
        "reason": None,
    }
    if not resolved.ocr_enabled and not probe_when_disabled:
        return base

    with _RUNTIME_PROBE_LOCK:
        cached_runtime, pdfium_available, pdfium_version = _runtime_snapshot(
            resolved.ocr_executable,
            resolved.ocr_languages,
            resolved.ocr_timeout_seconds,
            resolved.ocr_max_concurrency,
        )
    runtime = dict(cached_runtime)
    runtime["available_languages"] = list(cached_runtime.get("available_languages", []))
    runtime["missing_languages"] = list(cached_runtime.get("missing_languages", []))
    tesseract_available = bool(runtime["available"])
    runtime_ready = tesseract_available and pdfium_available
    reasons = [
        str(runtime["reason"]) if runtime.get("reason") else None,
        "PDFium renderer is not installed or could not be loaded" if not pdfium_available else None,
    ]
    reason = "; ".join(item for item in reasons if item) or None
    base.update(
        {
            "status": (
                "disabled"
                if not resolved.ocr_enabled
                else "ready" if runtime_ready else "blocked"
            ),
            "runtime_status": "ready" if runtime_ready else "blocked",
            "runtime_ready": runtime_ready,
            "required_ready": (not resolved.ocr_enabled) or runtime_ready,
            "runtime_version": runtime.get("runtime_version"),
            "available_languages": runtime.get("available_languages", []),
            "missing_languages": runtime.get("missing_languages", configuration["languages"]),
            "tesseract_available": tesseract_available,
            "pdf_renderer_available": pdfium_available,
            "pdf_renderer_version": pdfium_version,
            "reason": reason,
        }
    )
    return base


_SYNTHETIC_CANARY_ID = "drillai-ocr-runtime-canary-v1"
_SYNTHETIC_CANARY_TEXT = "DRILLAI OCR RUNTIME TEST 1234"
_SYNTHETIC_CANARY_NOTE = (
    "This in-memory synthetic canary checks that Tesseract can process a generated image and return "
    "non-empty text. It does not score transcription accuracy, evaluate a corpus, or validate engineering content."
)


def _synthetic_canary_png() -> bytes:
    """Render a fixed, non-sensitive canary in memory using the bundled font."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:
        raise OcrUnavailable("Pillow image support is not installed for the synthetic canary") from exc

    font_path = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "DejaVuSans.ttf"
    try:
        image = Image.new("RGB", (1280, 220), color="white")
        font = ImageFont.truetype(str(font_path), 64)
        ImageDraw.Draw(image).text((48, 64), _SYNTHETIC_CANARY_TEXT, font=font, fill="black")
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=False)
        encoded = output.getvalue()
    except Exception as exc:
        raise OcrUnavailable("The synthetic OCR canary image could not be rendered") from exc
    if not encoded.startswith(b"\x89PNG\r\n\x1a\n") or len(encoded) > 2 * 1024 * 1024:
        raise OcrUnavailable("The synthetic OCR canary image is invalid or exceeded its size bound")
    return encoded


def _synthetic_canary_result(
    status: str,
    *,
    reason: str | None = None,
    line_count: int = 0,
) -> dict[str, Any]:
    return {
        "case_id": _SYNTHETIC_CANARY_ID,
        "source": "generated_in_memory",
        "status": status,
        "line_count": line_count,
        "recognized_text_emitted": False,
        "quality_evaluated": False,
        "reason": reason,
        "note": _SYNTHETIC_CANARY_NOTE,
    }


def run_synthetic_runtime_canary(settings: Settings | None = None) -> dict[str, Any]:
    """Exercise the installed OCR process on a fixed in-memory image without returning its text."""
    resolved = settings or get_settings()
    provider = TesseractOcrProvider(
        languages=resolved.ocr_languages,
        executable=resolved.ocr_executable,
        timeout_seconds=resolved.ocr_timeout_seconds,
        max_concurrency=resolved.ocr_max_concurrency,
    )
    available, reason = provider.availability()
    if not available:
        return _synthetic_canary_result(
            "blocked", reason=reason or "The configured Tesseract runtime is unavailable"
        )
    try:
        image_png = _synthetic_canary_png()
        lines = provider.recognize(image_png)
    except OcrUnavailable as exc:
        return _synthetic_canary_result("failed", reason=str(exc))
    except Exception:
        return _synthetic_canary_result("failed", reason="The synthetic OCR canary failed safely")

    line_count = len(lines)
    if line_count > 0 and any(isinstance(line.text, str) and line.text.strip() for line in lines):
        return _synthetic_canary_result("passed", line_count=min(line_count, 1000))
    return _synthetic_canary_result(
        "failed",
        reason="Tesseract returned no non-empty lines for the synthetic canary image",
        line_count=min(line_count, 1000),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check local OCR runtime dependencies; no corpus or OCR quality is evaluated."
    )
    parser.add_argument(
        "--require-runtime",
        action="store_true",
        help="exit non-zero unless Tesseract, configured languages and PDFium are available",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="also run a fixed in-memory Tesseract canary; this does not score OCR quality",
    )
    args = parser.parse_args(argv)
    settings = get_settings()
    report = inspect_ocr_runtime(settings=settings, probe_when_disabled=True)
    if args.self_test:
        report["synthetic_canary"] = (
            run_synthetic_runtime_canary(settings)
            if report.get("runtime_ready") is True
            else _synthetic_canary_result(
                "blocked",
                reason=(
                    report.get("reason")
                    if isinstance(report.get("reason"), str)
                    else "The required Tesseract and PDFium runtime is not ready"
                ),
            )
        )
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")
    if (
        (report["enabled"] and not report["required_ready"])
        or (args.require_runtime and not report["runtime_ready"])
        or (args.self_test and report["synthetic_canary"]["status"] != "passed")
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
