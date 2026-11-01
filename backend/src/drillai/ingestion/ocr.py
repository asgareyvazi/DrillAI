"""Bounded OCR adapter used only when an operator explicitly enables OCR.

The backend does not bundle Tesseract or language data. Install the optional ``ocr`` extra for PDF
rasterization, install Tesseract plus the configured language packs at the host/container layer, and set
``DRILLAI_OCR_ENABLED=true``. Missing runtime pieces remain visible in the parsed document instead of
turning a scanned PDF into an apparently complete, empty extraction.

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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class TesseractOcrProvider:
    """Run installed Tesseract with a fixed TSV protocol and bounded timeout/output."""

    languages: str = "eng"
    executable: str = "tesseract"
    timeout_seconds: float = 30.0
    max_output_bytes: int = 4 * 1024 * 1024

    name: str = "tesseract"
    version: str = "tesseract-cli-tsv-v1"

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
        object.__setattr__(self, "languages", "+".join(parts))
        if not self.executable or "\x00" in self.executable:
            raise ValueError("executable must be a non-empty path")
        if not math.isfinite(self.timeout_seconds) or not 1 <= self.timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between 1 and 120")
        if not 1024 <= self.max_output_bytes <= MAX_TSV_BYTES:
            raise ValueError(f"max_output_bytes must be between 1 KiB and {MAX_TSV_BYTES} bytes")

    def _resolved_executable(self) -> str | None:
        resolved = shutil.which(self.executable)
        if resolved:
            return resolved
        candidate = Path(self.executable)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        return None

    def availability(self) -> tuple[bool, str | None]:
        if self._resolved_executable() is None:
            return False, "Tesseract executable was not found on the server PATH"
        return True, None

    def recognize(self, image_png: bytes) -> list[OcrLine]:
        executable = self._resolved_executable()
        if executable is None:
            raise OcrUnavailable("Tesseract executable was not found on the server PATH")
        if not image_png:
            raise OcrUnavailable("rendered page image is empty")

        # Do not forward application credentials or arbitrary request-scoped environment variables to
        # the OCR child process. Operators may set TESSDATA_PREFIX to point at installed language data.
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
        try:
            process = subprocess.Popen(
                [executable, "stdin", "stdout", "--psm", "6", "-l", self.languages, "tsv"],
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
