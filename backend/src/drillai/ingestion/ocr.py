"""Bounded OCR adapter used only when an operator explicitly enables OCR.

The Python package does not itself bundle Tesseract or language data; the standard backend image installs
Tesseract plus English/Persian packs, while native deployments must install the configured packs. Install
the optional ``ocr`` extra for PDF rasterization and set ``DRILLAI_OCR_ENABLED=true`` only after runtime
commissioning. Missing runtime pieces remain visible in the parsed document instead of turning a scanned
PDF into an apparently complete, empty extraction.

Uploaded bytes are passed to the executable over stdin; no user-controlled filename or shell command is
used. This module reports word-derived line boxes and confidence supplied by Tesseract, not semantic
accuracy or engineering validation.
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
import shutil
import subprocess
import threading
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

__all__ = [
    "OcrLine",
    "OcrProvider",
    "OcrUnavailable",
    "TesseractOcrProvider",
    "parse_tesseract_tsv",
]

MAX_TSV_BYTES = 16 * 1024 * 1024
MAX_DIAGNOSTIC_OUTPUT_BYTES = 128 * 1024
DIAGNOSTIC_TIMEOUT_SECONDS = 3.0
TESSERACT_ADAPTER_VERSION = "tesseract-cli-tsv-v3"
TESSERACT_PAGE_SEGMENTATION_MODE = 3
# Automatic page segmentation lets Tesseract detect page blocks and columns; deployment-image
# CP135 measured severe two-column/RTL errors with the previous single-block PSM 6 configuration.
_PROCESS_SEMAPHORES: dict[int, threading.BoundedSemaphore] = {}
_PROCESS_SEMAPHORES_LOCK = threading.Lock()
_TESSERACT_DIAGNOSTIC_CACHE: dict[tuple[str, str, float, str, str], dict[str, object]] = {}
_TESSERACT_DIAGNOSTIC_CACHE_LOCK = threading.Lock()


def _process_semaphore(limit: int) -> threading.BoundedSemaphore:
    # One semaphore per configured limit; all providers created under the same application settings
    # share it, so concurrent uploads cannot start unbounded Tesseract processes.
    with _PROCESS_SEMAPHORES_LOCK:
        semaphore = _PROCESS_SEMAPHORES.get(limit)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(limit)
            _PROCESS_SEMAPHORES[limit] = semaphore
        return semaphore


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    # Pixel coordinates in the rendered source image: x, y, width, height.
    bbox: tuple[int, int, int, int]


class OcrProvider(Protocol):
    name: str
    version: str

    def availability(self) -> tuple[bool, str | None]: ...

    def recognize(self, image_png: bytes) -> list[OcrLine]: ...


class OcrUnavailable(RuntimeError):
    """An explicitly requested OCR runtime could not be used."""


_REQUIRED_TSV_COLUMNS = (
    "level",
    "page_num",
    "block_num",
    "par_num",
    "line_num",
    "left",
    "top",
    "width",
    "height",
    "conf",
    "text",
)


def parse_tesseract_tsv(data: bytes) -> list[OcrLine]:
    """Turn Tesseract word rows into ordered lines, preserving boxes and confidence.

    Parent rows and negative confidence (Tesseract's "not scored" marker) are not treated as words.
    A line score is character-weighted over its word scores; it is a transcription-confidence signal,
    not a probability that a drilling value is correct. The TSV parser has its own hard byte ceiling so
    callers cannot accidentally bypass the provider's subprocess-output bound.
    """
    if len(data) > MAX_TSV_BYTES:
        raise OcrUnavailable("Tesseract TSV exceeded the 16 MiB parser limit")
    try:
        text = data.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text, newline=""), delimiter="\t")
        if reader.fieldnames is None or not set(_REQUIRED_TSV_COLUMNS).issubset(reader.fieldnames):
            raise OcrUnavailable("Tesseract returned an unrecognised TSV format")
        grouped: dict[tuple[int, int, int, int], list[tuple[str, float, int, int, int, int]]] = {}
        for row in reader:
            if row.get("level") != "5":
                continue
            word = (row.get("text") or "").strip()
            if not word:
                continue
            try:
                page_num = int(row["page_num"])
                block_num = int(row["block_num"])
                par_num = int(row["par_num"])
                line_num = int(row["line_num"])
                left = int(row["left"])
                top = int(row["top"])
                width = int(row["width"])
                height = int(row["height"])
                raw_confidence = float(row["conf"])
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            if (
                page_num < 1
                or min(block_num, par_num, line_num, left, top, width, height) < 0
                or width == 0
                or height == 0
                or not math.isfinite(raw_confidence)
                or not 0 <= raw_confidence <= 100
            ):
                continue
            key = (page_num, block_num, par_num, line_num)
            # Preserve Tesseract's row order. Sorting by each word's top coordinate can scramble
            # mixed-script text and words whose glyph tops differ slightly on the same baseline.
            grouped.setdefault(key, []).append((word, raw_confidence / 100.0, left, top, width, height))
    except UnicodeDecodeError as exc:
        raise OcrUnavailable("Tesseract returned non-UTF-8 TSV output") from exc

    lines: list[OcrLine] = []
    for words in grouped.values():
        x0 = min(word[2] for word in words)
        y0 = min(word[3] for word in words)
        x1 = max(word[2] + word[4] for word in words)
        y1 = max(word[3] + word[5] for word in words)
        total_chars = sum(len(word[0]) for word in words)
        confidence = (
            sum(word[1] * len(word[0]) for word in words) / total_chars if total_chars else 0.0
        )
        lines.append(
            OcrLine(
                text=" ".join(word[0] for word in words),
                confidence=confidence,
                bbox=(x0, y0, x1 - x0, y1 - y0),
            )
        )
    return lines


@dataclass
class TesseractOcrProvider:
    """Run installed Tesseract with a fixed TSV protocol and bounded timeout/output.

    Availability includes an inexpensive, time-bounded version and installed-language probe. The
    application records the adapter and runtime versions separately from confidence/transcription
    quality; neither version nor confidence is an accuracy claim.
    """

    languages: str = "eng+fas"
    executable: str = "tesseract"
    timeout_seconds: float = 30.0
    max_output_bytes: int = 4 * 1024 * 1024
    max_concurrency: int = 2

    name: str = "tesseract"
    _runtime_version: str | None = field(default=None, init=False, repr=False)
    _installed_languages: tuple[str, ...] = field(default=(), init=False, repr=False)
    _diagnostic_reason: str | None = field(default=None, init=False, repr=False)

    @property
    def adapter_version(self) -> str:
        return TESSERACT_ADAPTER_VERSION

    @property
    def version(self) -> str:
        if self._runtime_version is None and self._diagnostic_reason is None:
            self.diagnostics()
        if self._runtime_version:
            return f"{self.adapter_version}; {self._runtime_version}"
        return self.adapter_version

    def __post_init__(self) -> None:
        parts = re.split(r"[+,]", self.languages) if isinstance(self.languages, str) else []
        if (
            not parts
            or len(self.languages) > 120
            or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", part) for part in parts)
        ):
            raise ValueError("languages must be a list of Tesseract language identifiers")
        # Tesseract separates multiple languages with '+'. Accept comma-separated operator config,
        # but convert it here rather than passing a separator its CLI does not define.
        self.languages = "+".join(parts)
        if not self.executable or "\x00" in self.executable:
            raise ValueError("executable must be a non-empty path")
        if not math.isfinite(self.timeout_seconds) or not 1 <= self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        if not 1024 <= self.max_output_bytes <= MAX_TSV_BYTES:
            raise ValueError(f"max_output_bytes must be between 1 KiB and {MAX_TSV_BYTES} bytes")
        if isinstance(self.max_concurrency, bool) or not isinstance(self.max_concurrency, int) or not 1 <= self.max_concurrency <= 8:
            raise ValueError("max_concurrency must be an integer between 1 and 8")

    def _resolved_executable(self) -> str | None:
        resolved = shutil.which(self.executable)
        if resolved:
            return resolved
        candidate = Path(self.executable)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        return None

    def _child_environment(self) -> dict[str, str]:
        # Do not forward application credentials or arbitrary request-scoped environment variables to
        # the OCR child process. Operators may set TESSDATA_PREFIX for installed language data.
        child_env = {
            "PATH": os.environ.get("PATH", ""),
            "OMP_THREAD_LIMIT": "1",
        }
        if os.environ.get("TESSDATA_PREFIX"):
            child_env["TESSDATA_PREFIX"] = os.environ["TESSDATA_PREFIX"]
        if os.name == "nt":
            for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"):
                if os.environ.get(key):
                    child_env[key] = os.environ[key]
        return child_env

    def _run_diagnostic(self, executable: str, argument: str) -> tuple[str | None, str | None]:
        try:
            process = subprocess.Popen(
                [executable, argument],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self._child_environment(),
                bufsize=0,
            )
        except OSError:
            return None, "Tesseract diagnostic probe could not be started"
        assert process.stdout is not None
        output = bytearray()
        output_overflow = threading.Event()
        reader_errors: list[Exception] = []

        def read_bounded_output() -> None:
            try:
                while True:
                    chunk = process.stdout.read(16 * 1024)
                    if not chunk:
                        return
                    remaining = MAX_DIAGNOSTIC_OUTPUT_BYTES + 1 - len(output)
                    if len(chunk) > remaining:
                        output.extend(chunk[:remaining])
                        output_overflow.set()
                        with suppress(OSError):
                            process.kill()
                        return
                    output.extend(chunk)
            except (OSError, ValueError) as exc:
                reader_errors.append(exc)

        reader = threading.Thread(target=read_bounded_output, name="drillai-ocr-diagnostic", daemon=True)
        reader.start()
        timed_out = False
        try:
            process.wait(timeout=DIAGNOSTIC_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            timed_out = True
            with suppress(OSError):
                process.kill()
            process.wait()
        finally:
            if process.poll() is None:
                with suppress(OSError):
                    process.kill()
                process.wait()
            reader.join(timeout=1.0)
            if reader.is_alive():
                with suppress(OSError):
                    process.stdout.close()
                reader.join(timeout=1.0)
            with suppress(OSError):
                process.stdout.close()

        if timed_out:
            return None, "Tesseract diagnostic probe timed out"
        if output_overflow.is_set() or len(output) > MAX_DIAGNOSTIC_OUTPUT_BYTES:
            return None, "Tesseract diagnostic output exceeded the configured bound"
        if reader.is_alive():
            return None, "Tesseract diagnostic output reader did not stop"
        if reader_errors:
            return None, "Tesseract diagnostic output could not be read"
        if process.returncode != 0:
            return None, "Tesseract diagnostic command failed"
        return bytes(output).decode("utf-8", errors="replace"), None

    def diagnostics(self) -> dict[str, object]:
        """Return a cached, bounded probe of the immutable deployed OCR runtime."""
        key = (
            self.executable,
            self.languages,
            self.timeout_seconds,
            os.environ.get("PATH", ""),
            os.environ.get("TESSDATA_PREFIX", ""),
        )
        with _TESSERACT_DIAGNOSTIC_CACHE_LOCK:
            snapshot = _TESSERACT_DIAGNOSTIC_CACHE.get(key)
            if snapshot is None:
                snapshot = self._probe_diagnostics()
                if len(_TESSERACT_DIAGNOSTIC_CACHE) >= 32:
                    _TESSERACT_DIAGNOSTIC_CACHE.pop(next(iter(_TESSERACT_DIAGNOSTIC_CACHE)))
                _TESSERACT_DIAGNOSTIC_CACHE[key] = {
                    **snapshot,
                    "configured_languages": list(snapshot["configured_languages"]),
                    "available_languages": list(snapshot["available_languages"]),
                    "missing_languages": list(snapshot["missing_languages"]),
                }
                snapshot = _TESSERACT_DIAGNOSTIC_CACHE[key]

            runtime_version = snapshot.get("runtime_version")
            self._runtime_version = runtime_version if isinstance(runtime_version, str) else None
            self._installed_languages = tuple(snapshot.get("available_languages", []))
            reason = snapshot.get("reason")
            self._diagnostic_reason = reason if isinstance(reason, str) else None
            return {
                **snapshot,
                "configured_languages": list(snapshot["configured_languages"]),
                "available_languages": list(snapshot["available_languages"]),
                "missing_languages": list(snapshot["missing_languages"]),
            }

    def _probe_diagnostics(self) -> dict[str, object]:
        """Return bounded runtime facts without exposing executable paths or process output."""
        configured = self.languages.split("+")
        result: dict[str, object] = {
            "available": False,
            "runtime_version": None,
            "configured_languages": configured,
            "available_languages": [],
            "missing_languages": configured,
            "reason": None,
        }
        executable = self._resolved_executable()
        if executable is None:
            result["reason"] = "Tesseract executable was not found on the server PATH"
            self._runtime_version = None
            self._installed_languages = ()
            self._diagnostic_reason = str(result["reason"])
            return result

        version_output, error = self._run_diagnostic(executable, "--version")
        if error or not version_output:
            result["reason"] = error or "Tesseract returned an empty version response"
            self._runtime_version = None
            self._installed_languages = ()
            self._diagnostic_reason = str(result["reason"])
            return result
        raw_version_line = next(
            (line.strip() for line in version_output.splitlines() if "tesseract" in line.casefold()),
            None,
        )
        version_match = (
            re.fullmatch(r"tesseract\s+([0-9][A-Za-z0-9._+-]{0,80})", raw_version_line, re.IGNORECASE)
            if raw_version_line
            else None
        )
        if not version_match:
            result["reason"] = "Tesseract returned an unrecognised version response"
            self._runtime_version = None
            self._installed_languages = ()
            self._diagnostic_reason = str(result["reason"])
            return result
        version_line = f"tesseract {version_match.group(1)}"

        languages_output, error = self._run_diagnostic(executable, "--list-langs")
        if error or languages_output is None:
            result["runtime_version"] = version_line[:120]
            result["reason"] = error or "Tesseract returned no language inventory"
            self._runtime_version = version_line[:120]
            self._installed_languages = ()
            self._diagnostic_reason = str(result["reason"])
            return result
        installed = tuple(sorted({
            line.strip()
            for line in languages_output.splitlines()
            if re.fullmatch(r"[A-Za-z0-9_-]{1,40}", line.strip())
        }))
        missing = sorted(set(configured) - set(installed))
        self._runtime_version = version_line[:120]
        self._installed_languages = installed
        self._diagnostic_reason = (
            "Tesseract is missing configured language data: " + ", ".join(missing) if missing else None
        )
        result.update(
            {
                "available": not missing,
                "runtime_version": self._runtime_version,
                "available_languages": list(installed),
                "missing_languages": missing,
                "reason": self._diagnostic_reason,
            }
        )
        return result

    def availability(self) -> tuple[bool, str | None]:
        diagnostics = self.diagnostics()
        return bool(diagnostics["available"]), diagnostics["reason"] if isinstance(diagnostics["reason"], str) else None

    def recognize(self, image_png: bytes) -> list[OcrLine]:
        executable = self._resolved_executable()
        if executable is None:
            raise OcrUnavailable("Tesseract executable was not found on the server PATH")
        if not image_png:
            raise OcrUnavailable("rendered page image is empty")

        semaphore = _process_semaphore(self.max_concurrency)
        if not semaphore.acquire(blocking=False):
            raise OcrUnavailable(
                f"OCR concurrency limit ({self.max_concurrency}) is reached; retry when capacity is available"
            )
        try:
            return self._recognize_acquired(image_png, executable)
        finally:
            semaphore.release()

    def _recognize_acquired(self, image_png: bytes, executable: str) -> list[OcrLine]:
        child_env = self._child_environment()
        try:
            process = subprocess.Popen(
                [
                    executable,
                    "stdin",
                    "stdout",
                    "--psm",
                    str(TESSERACT_PAGE_SEGMENTATION_MODE),
                    "-l",
                    self.languages,
                    "tsv",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=child_env,
                bufsize=0,
            )
        except OSError as exc:
            raise OcrUnavailable("Tesseract could not be started") from exc

        assert process.stdin is not None and process.stdout is not None
        output = bytearray()
        output_overflow = threading.Event()
        reader_errors: list[OSError] = []

        def read_bounded_stdout() -> None:
            try:
                while True:
                    chunk = process.stdout.read(64 * 1024)
                    if not chunk:
                        return
                    remaining = self.max_output_bytes + 1 - len(output)
                    if len(chunk) > remaining:
                        output.extend(chunk[:remaining])
                        output_overflow.set()
                        with suppress(OSError):
                            process.kill()
                        return
                    output.extend(chunk)
            except OSError as exc:
                reader_errors.append(exc)

        def write_image() -> None:
            try:
                remaining = memoryview(image_png)
                while remaining:
                    written = process.stdin.write(remaining)
                    if not written:
                        raise BrokenPipeError("Tesseract stopped reading its input")
                    remaining = remaining[written:]
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                # The main thread reports the child's bounded failure/exit status.
                with suppress(OSError, ValueError):
                    process.stdin.close()

        reader = threading.Thread(target=read_bounded_stdout, name="drillai-ocr-stdout", daemon=True)
        writer = threading.Thread(target=write_image, name="drillai-ocr-stdin", daemon=True)
        reader.start()
        writer.start()
        timed_out = False
        try:
            process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            with suppress(OSError):
                process.kill()
            process.wait()
        finally:
            if process.poll() is None:
                with suppress(OSError):
                    process.kill()
                process.wait()
            writer.join(timeout=1.0)
            reader.join(timeout=1.0)
            if writer.is_alive():
                with suppress(OSError):
                    process.stdin.close()
                writer.join(timeout=1.0)
            if reader.is_alive():
                with suppress(OSError):
                    process.stdout.close()
                reader.join(timeout=1.0)
            with suppress(OSError):
                process.stdin.close()
            with suppress(OSError):
                process.stdout.close()

        if timed_out:
            raise OcrUnavailable("Tesseract exceeded the configured per-page timeout")
        if output_overflow.is_set() or len(output) > self.max_output_bytes:
            raise OcrUnavailable("Tesseract output exceeded the configured page limit")
        if writer.is_alive() or reader.is_alive():
            raise OcrUnavailable("Tesseract I/O did not stop after the child process exited")
        if reader_errors:
            raise OcrUnavailable("Tesseract output could not be read") from reader_errors[0]
        if process.returncode != 0:
            raise OcrUnavailable(f"Tesseract exited with status {process.returncode}")
        return parse_tesseract_tsv(bytes(output))
