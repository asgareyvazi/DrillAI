"""Contract tests for the opt-in OCR adapter, PDF rendering path, and provenance.

The PDFium tests use a deterministic provider only to exercise the adapter boundary, image limits, and
persisted boxes. They are not OCR accuracy tests and make no claim about transcription quality. The
Tesseract-compatible child-process test verifies the CLI protocol and resource limits with a local
executable; it is not a Tesseract or language-pack integration test.
"""

from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pypdf import PdfWriter

from drillai.ingestion import ocr as ocr_module
from drillai.ingestion.ocr import OcrLine, OcrUnavailable, TesseractOcrProvider, parse_tesseract_tsv
from drillai.ingestion.parsers import parse_bytes


@pytest.fixture(autouse=True)
def clear_tesseract_diagnostic_cache():
    with ocr_module._TESSERACT_DIAGNOSTIC_CACHE_LOCK:
        ocr_module._TESSERACT_DIAGNOSTIC_CACHE.clear()
    yield
    with ocr_module._TESSERACT_DIAGNOSTIC_CACHE_LOCK:
        ocr_module._TESSERACT_DIAGNOSTIC_CACHE.clear()


_COLUMNS = (
    "level",
    "page_num",
    "block_num",
    "par_num",
    "line_num",
    "word_num",
    "left",
    "top",
    "width",
    "height",
    "conf",
    "text",
)


def _tsv(rows: list[list[str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter="\t", lineterminator="\n")
    writer.writerow(_COLUMNS)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _word(*, page: int = 1, block: int = 1, line: int = 1, left: int, top: int, width: int, height: int, conf: str, text: str) -> list[str]:
    return ["5", str(page), str(block), "1", str(line), "1", str(left), str(top), str(width), str(height), conf, text]


def _blank_pdf(page_count: int = 1, *, width: float = 612, height: float = 792, rotation: int = 0) -> bytes:
    writer = PdfWriter()
    for _ in range(page_count):
        page = writer.add_blank_page(width=width, height=height)
        if rotation:
            page.rotate(rotation)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class ContractProvider:
    name = "contract-test-only"
    version = "synthetic-contract-v1"

    def __init__(self, lines: list[OcrLine] | None = None, *, available: bool = True) -> None:
        self.lines = lines or []
        self.is_available = available
        self.calls = 0
        self.image_size: tuple[int, int] | None = None

    def availability(self) -> tuple[bool, str | None]:
        return self.is_available, None if self.is_available else "disabled in contract test"

    def recognize(self, image_png: bytes) -> list[OcrLine]:
        from PIL import Image

        image = Image.open(io.BytesIO(image_png))
        self.image_size = image.size
        self.calls += 1
        return list(self.lines)


@pytest.mark.parametrize(
    ("languages", "expected"),
    [("eng", "eng"), ("eng+fas", "eng+fas"), ("eng,fas", "eng+fas")],
)
def test_tesseract_provider_normalizes_language_identifiers(languages: str, expected: str) -> None:
    assert TesseractOcrProvider(languages=languages).languages == expected


@pytest.mark.parametrize("languages", ["", "eng;rm -rf", "eng fas", "+fas", "fas+"])
def test_tesseract_provider_rejects_invalid_language_identifiers(languages: str) -> None:
    with pytest.raises(ValueError, match="language"):
        TesseractOcrProvider(languages=languages)


def test_tesseract_provider_validates_explicit_ocr_page_segmentation_mode() -> None:
    assert TesseractOcrProvider().page_segmentation_mode == ocr_module.TESSERACT_PAGE_SEGMENTATION_MODE == 6
    assert TesseractOcrProvider(page_segmentation_mode=11).page_segmentation_mode == 11
    for mode in (0, 2, -1, 14, True, 6.0):
        with pytest.raises(ValueError, match="page_segmentation_mode"):
            TesseractOcrProvider(page_segmentation_mode=mode)


def test_page_layout_detector_requires_a_long_central_separator_and_orders_columns(monkeypatch) -> None:
    from PIL import Image, ImageDraw

    divided = Image.new("RGB", (1000, 1200), "white")
    draw = ImageDraw.Draw(divided)
    draw.line((500, 120, 500, 850), fill=(205, 205, 205), width=2)
    for y in (220, 360, 500, 640):
        draw.line((100, y, 430, y), fill="black", width=8)
        draw.line((570, y, 900, y), fill="black", width=8)
    encoded = io.BytesIO()
    divided.save(encoded, format="PNG")

    divider = ocr_module._detect_central_column_divider(encoded.getvalue())
    assert divider is not None
    split_x, width, height = divider
    assert abs(split_x - 500) <= 1
    assert (width, height) == (1000, 1200)

    table = Image.new("RGB", (1000, 1200), "white")
    table_draw = ImageDraw.Draw(table)
    for y in (220, 360, 500, 640, 780):
        table_draw.line((100, y, 900, y), fill=(190, 190, 190), width=2)
    table_png = io.BytesIO()
    table.save(table_png, format="PNG")
    assert ocr_module._detect_central_column_divider(table_png.getvalue()) is None

    lines = [
        OcrLine("heading", 0.9, (80, 50, 700, 30)),
        OcrLine("left-1", 0.9, (100, 250, 250, 30)),
        OcrLine("right-1", 0.9, (600, 250, 250, 30)),
        OcrLine("left-2", 0.9, (100, 420, 250, 30)),
        OcrLine("right-2", 0.9, (600, 420, 250, 30)),
    ]
    ltr_order = ocr_module._order_column_lines(
        lines, split_x=500, image_width=1000, image_height=1200
    )
    rtl_lines = [
        OcrLine("عنوان", 0.9, (80, 50, 700, 30)),
        OcrLine("م-چپ-۱", 0.9, (100, 250, 250, 30)),
        OcrLine("م-راست-۱", 0.9, (600, 250, 250, 30)),
        OcrLine("م-چپ-۲", 0.9, (100, 420, 250, 30)),
        OcrLine("م-راست-۲", 0.9, (600, 420, 250, 30)),
    ]
    rtl_order = ocr_module._order_column_lines(
        rtl_lines, split_x=500, image_width=1000, image_height=1200
    )
    assert [line.text for line in ltr_order] == ["heading", "left-1", "left-2", "right-1", "right-2"]
    assert [line.text for line in rtl_order] == ["عنوان", "م-راست-۱", "م-راست-۲", "م-چپ-۱", "م-چپ-۲"]

    monkeypatch.setitem(sys.modules, "numpy", None)
    assert ocr_module._detect_central_column_divider(encoded.getvalue()) is None


def test_tesseract_tsv_preserves_engine_word_order_and_calculates_line_evidence() -> None:
    rows = [
        # Word two has a slightly higher baseline; sorting by top coordinate would reverse the text.
        _word(left=100, top=100, width=50, height=15, conf="90", text="mud"),
        _word(left=50, top=98, width=30, height=17, conf="80", text="weight"),
        _word(line=2, left=50, top=130, width=45, height=15, conf="100", text="12.4"),
        _word(line=3, left=50, top=160, width=10, height=12, conf="-1", text="not-scored"),
        ["4", "1", "1", "1", "1", "0", "10", "10", "20", "10", "-1", "parent row"],
        _word(line=4, left=-1, top=190, width=10, height=10, conf="90", text="negative-box"),
    ]

    lines = parse_tesseract_tsv(_tsv(rows))

    assert [line.text for line in lines] == ["mud weight", "12.4"]
    assert lines[0].confidence == pytest.approx(0.8333333333)
    assert lines[0].bbox == (50, 98, 100, 17)
    assert lines[1].bbox == (50, 130, 45, 15)


def test_tesseract_tsv_refuses_oversized_output_and_unknown_headers() -> None:
    with pytest.raises(OcrUnavailable, match="16 MiB"):
        parse_tesseract_tsv(b"x" * (16 * 1024 * 1024 + 1))
    with pytest.raises(OcrUnavailable, match="TSV format"):
        parse_tesseract_tsv(b"not\ta\ttesseract\tfile\n")


def test_pdfium_path_records_text_boxes_confidence_and_page_status() -> None:
    provider = ContractProvider([OcrLine("Daily report", 0.875, (10, 20, 100, 25))])

    parsed = parse_bytes(
        _blank_pdf(),
        filename="scan.pdf",
        ocr_provider=provider,
        ocr_max_pages=1,
        ocr_dpi=200,
        ocr_max_page_pixels=1_000_000,
    )

    page = parsed.pages[0]
    assert provider.calls == 1
    assert provider.image_size is not None
    assert provider.image_size[0] * provider.image_size[1] <= 1_000_000
    assert page.ocr_applied is True
    assert page.ocr_status == "applied"
    assert page.text == "Daily report"
    assert page.layout["ocr"]["provider_version"] == "synthetic-contract-v1"
    assert page.regions[-1].confidence == pytest.approx(0.875)
    assert page.regions[-1].source_locator["line_boxes"][0]["bbox_px"] == {
        "x0": 10,
        "y0": 20,
        "x1": 110,
        "y1": 45,
    }
    assert parsed.metadata["ocr_applied_pages"] == [1]
    assert parsed.metadata["ocr_required_pages"] == []
    assert parsed.metadata["ocr_status_by_page"] == {"1": "applied"}


def test_rotated_pdf_retains_display_dimensions_and_maps_ocr_boxes_to_page_points() -> None:
    provider = ContractProvider([OcrLine("Rotated", 0.9, (10, 20, 40, 20))])

    parsed = parse_bytes(
        _blank_pdf(rotation=90),
        filename="rotated-scan.pdf",
        ocr_provider=provider,
        ocr_max_page_pixels=1_000_000,
    )

    page = parsed.pages[0]
    assert page.rotation == 90
    assert (page.width_pt, page.height_pt) == (792.0, 612.0)
    assert page.regions[-1].bbox is not None
    assert page.regions[-1].bbox["x1"] <= 792.0
    assert page.regions[-1].bbox["y1"] <= 612.0


def test_ocr_page_limit_and_unavailable_provider_remain_incomplete() -> None:
    provider = ContractProvider([OcrLine("Recognized", 0.8, (10, 10, 80, 20))])
    two_pages = parse_bytes(
        _blank_pdf(page_count=2),
        filename="two-scans.pdf",
        ocr_provider=provider,
        ocr_max_pages=1,
    )
    assert provider.calls == 1
    assert two_pages.metadata["ocr_required_pages"] == [2]
    assert two_pages.metadata["ocr_status_by_page"] == {"1": "applied", "2": "page_limit"}

    unavailable = ContractProvider(available=False)
    missing_runtime = parse_bytes(
        _blank_pdf(), filename="scan.pdf", ocr_provider=unavailable
    )
    assert missing_runtime.metadata["ocr_required"] is True
    assert missing_runtime.metadata["ocr_status_by_page"] == {"1": "unavailable"}
    assert any("unavailable" in warning for warning in missing_runtime.warnings)


def test_ocr_pixel_floor_skips_extreme_pages_without_rendering() -> None:
    provider = ContractProvider([OcrLine("Not called", 0.8, (1, 1, 2, 2))])
    parsed = parse_bytes(
        _blank_pdf(width=100_000, height=100_000),
        filename="oversized-page.pdf",
        ocr_provider=provider,
        ocr_max_page_pixels=100_000,
    )

    assert provider.calls == 0
    assert parsed.metadata["ocr_required_pages"] == [1]
    assert parsed.metadata["ocr_status_by_page"] == {"1": "pixel_limit"}


@pytest.mark.skipif(os.name == "nt", reason="temporary shebang executables are POSIX-only")
def test_tesseract_cli_adapter_uses_stdin_and_bounds_child_output(tmp_path: Path, monkeypatch) -> None:
    script = tmp_path / "tesseract-contract"
    expected_psm = tmp_path / "expected-psm"
    expected_psm.write_text("11", encoding="utf-8")
    expected = _tsv([_word(left=1, top=2, width=30, height=10, conf="88", text="Drill")]).decode()
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "if sys.argv[1:] == ['--version']:\n"
        " print('tesseract 5.3.0')\n"
        " sys.exit(0)\n"
        "if sys.argv[1:] == ['--list-langs']:\n"
        " print('List of available languages (2):\\neng\\nfas')\n"
        " sys.exit(0)\n"
        "assert sys.argv[1:3] == ['stdin', 'stdout']\n"
        f"assert sys.argv[3] == '--psm' and sys.argv[4] == open({str(expected_psm)!r}).read()\n"
        "assert sys.argv[5:] == ['-l', 'eng+fas', 'tsv']\n"
        "assert sys.stdin.buffer.read() == b'png-contract'\n"
        "assert 'DRILLAI_TEST_SECRET' not in os.environ\n"
        f"sys.stdout.write({expected!r})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("DRILLAI_TEST_SECRET", "must-not-be-forwarded")
    monkeypatch.setattr(ocr_module, "_detect_central_column_divider", lambda _png: (500, 1000, 1000))

    provider = TesseractOcrProvider(
        languages="eng,fas", executable=str(script), timeout_seconds=5, max_output_bytes=1024
    )
    assert provider.availability() == (True, None)
    assert provider.version == "tesseract-cli-tsv-v5; tesseract 5.3.0"
    lines = provider.recognize(b"png-contract")
    assert len(lines) == 1
    assert lines[0].text == "Drill"
    assert lines[0].confidence == pytest.approx(0.88)
    assert lines[0].bbox == (1, 2, 30, 10)

    expected_psm.write_text("6", encoding="utf-8")
    monkeypatch.setattr(ocr_module, "_detect_central_column_divider", lambda _png: None)
    default_lines = provider.recognize(b"png-contract")
    assert [line.text for line in default_lines] == ["Drill"]

    noisy = tmp_path / "noisy-tesseract"
    noisy.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.write('x' * 1000000)\n",
        encoding="utf-8",
    )
    noisy.chmod(0o755)
    bounded = TesseractOcrProvider(
        executable=str(noisy), timeout_seconds=5, max_output_bytes=1024
    )
    with pytest.raises(OcrUnavailable, match="output exceeded"):
        bounded.recognize(b"small image")


def test_tesseract_provider_availability_is_explicit_for_missing_executable() -> None:
    provider = TesseractOcrProvider(executable="drillai-missing-tesseract-executable")
    available, reason = provider.availability()
    assert available is False
    assert reason == "Tesseract executable was not found on the server PATH"
    with pytest.raises(OcrUnavailable, match="not found"):
        provider.recognize(b"image")


def test_tesseract_diagnostics_verify_languages_and_capture_runtime_version(monkeypatch) -> None:
    from drillai.ingestion import ocr

    monkeypatch.setattr(ocr.shutil, "which", lambda _executable: "/opt/tesseract")

    outputs = iter(
        [
            b"tesseract 5.4.1\n",
            b"List of available languages (1):\neng\n",
        ]
    )

    class FakeProcess:
        def __init__(self, output: bytes):
            self.stdout = io.BytesIO(output)
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    def popen(_command, **kwargs):
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.STDOUT
        return FakeProcess(next(outputs))

    monkeypatch.setattr(ocr.subprocess, "Popen", popen)
    provider = TesseractOcrProvider(languages="eng+fas")

    diagnostics = provider.diagnostics()

    assert diagnostics["available"] is False
    assert diagnostics["runtime_version"] == "tesseract 5.4.1"
    assert diagnostics["available_languages"] == ["eng"]
    assert diagnostics["missing_languages"] == ["fas"]
    assert diagnostics["reason"] == "Tesseract is missing configured language data: fas"
    assert provider.version == "tesseract-cli-tsv-v5; tesseract 5.4.1"


def test_tesseract_diagnostic_version_rejects_path_or_untrusted_process_text(monkeypatch) -> None:
    from drillai.ingestion import ocr

    monkeypatch.setattr(ocr.shutil, "which", lambda _executable: "/opt/tesseract")

    class UntrustedVersionProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"tesseract /private/deploy/path 5.4.1\\n")
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(
        ocr.subprocess,
        "Popen",
        lambda *_args, **_kwargs: UntrustedVersionProcess(),
    )
    report = TesseractOcrProvider(executable="tesseract").diagnostics()

    assert report["available"] is False
    assert report["reason"] == "Tesseract returned an unrecognised version response"
    assert "/private/deploy/path" not in str(report)


def test_tesseract_runtime_diagnostics_are_cached_across_provider_instances(monkeypatch) -> None:
    from drillai.ingestion import ocr

    monkeypatch.setattr(ocr.shutil, "which", lambda _executable: "/opt/tesseract")
    calls: list[str] = []

    class FakeProcess:
        def __init__(self, command):
            calls.append(command[-1])
            output = (
                b"tesseract 5.4.1\n"
                if command[-1] == "--version"
                else b"List of available languages (2):\neng\nfas\n"
            )
            self.stdout = io.BytesIO(output)
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(ocr.subprocess, "Popen", lambda command, **_kwargs: FakeProcess(command))
    first_provider = TesseractOcrProvider(executable="tesseract")
    second_provider = TesseractOcrProvider(executable="tesseract")

    first_version = first_provider.version
    first = first_provider.diagnostics()
    second = second_provider.diagnostics()
    second["available_languages"].append("tampered")

    assert first_version == "tesseract-cli-tsv-v5; tesseract 5.4.1"
    assert calls == ["--version", "--list-langs"]
    assert second_provider.version == "tesseract-cli-tsv-v5; tesseract 5.4.1"
    assert first["available_languages"] == ["eng", "fas"]


def test_tesseract_diagnostic_probe_timeout_is_reported_without_raw_process_output(monkeypatch) -> None:
    from drillai.ingestion import ocr

    monkeypatch.setattr(ocr.shutil, "which", lambda _executable: "/opt/tesseract")

    class TimeoutProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"")
            self.returncode = None

        def wait(self, timeout=None):
            if timeout is not None:
                raise subprocess.TimeoutExpired("tesseract --version", timeout, output=b"/private/path")
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(ocr.subprocess, "Popen", lambda *_args, **_kwargs: TimeoutProcess())
    provider = TesseractOcrProvider(executable="tesseract")
    diagnostics = provider.diagnostics()
    assert diagnostics["available"] is False
    assert diagnostics["reason"] == "Tesseract diagnostic probe timed out"
    assert "/private/path" not in str(diagnostics)


def test_tesseract_diagnostic_probe_caps_output_before_buffering_unbounded_data(monkeypatch) -> None:
    from drillai.ingestion import ocr

    monkeypatch.setattr(ocr.shutil, "which", lambda _executable: "/opt/tesseract")

    class NoisyProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"x" * (ocr.MAX_DIAGNOSTIC_OUTPUT_BYTES + 100_000))
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(ocr.subprocess, "Popen", lambda *_args, **_kwargs: NoisyProcess())
    provider = TesseractOcrProvider(executable="tesseract")

    diagnostics = provider.diagnostics()

    assert diagnostics["available"] is False
    assert diagnostics["reason"] == "Tesseract diagnostic output exceeded the configured bound"


def test_tesseract_process_concurrency_limit_fails_fast(monkeypatch) -> None:
    from drillai.ingestion import ocr

    monkeypatch.setattr(ocr.shutil, "which", lambda _executable: "/opt/tesseract")
    provider = TesseractOcrProvider(executable="tesseract", max_concurrency=1)
    semaphore = ocr._process_semaphore(1)
    assert semaphore.acquire(blocking=False)
    try:
        with pytest.raises(OcrUnavailable, match="concurrency limit"):
            provider.recognize(b"rendered-png")
    finally:
        semaphore.release()
