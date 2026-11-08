"""Synthetic OCR quality runner tests; only the container CI run uses real Tesseract accuracy."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path

from pypdf import PdfReader

from drillai.core.config import Settings
from drillai.ingestion import ocr_synthetic_quality as quality
from drillai.ingestion.ocr import OcrLine
from drillai.ingestion.parsers import parse_bytes


def test_pinned_synthetic_ocr_corpus_has_32_unique_ddr_cases_and_explicit_limitations() -> None:
    corpus, raw = quality._load_corpus()

    assert corpus["classification"] == "synthetic"
    assert len(corpus["cases"]) == 32
    assert len({case["case_id"] for case in corpus["cases"]}) == 32
    assert all(case["language"] == "eng" for case in corpus["cases"])
    assert all(len(case["critical_fields"]) == 6 for case in corpus["cases"])
    assert "not sampled" in corpus["sampling_method"]
    assert "not independently adjudicated" in corpus["ground_truth_protocol"]
    assert hashlib.sha256(raw).hexdigest() == quality._base_report(corpus, raw)["dataset"]["sha256"]


def test_critical_field_metric_requires_the_value_under_its_expected_label() -> None:
    critical_fields = [
        {"key": "report_date", "expected": "2026-01-01"},
        {"key": "measured_depth_m", "expected": "1087.5"},
        {"key": "mud_weight_ppg", "expected": "10.4"},
    ]

    assert quality._critical_field_matches(
        "SAMPLE ID: 2026-01-01\nREPORT DATE: 2026-01-01\nMEASURED DEPTH: 1087.5 M\nMUD WEIGHT: 10.4 PPG",
        critical_fields,
    ) == 3
    assert quality._critical_field_matches(
        "SAMPLE ID: 2026-01-01\nREPORT DATE: 2026-01-02\nMEASURED DEPTH: 1087.5 M\nMUD WEIGHT: 10.4 PPG",
        critical_fields,
    ) == 2


def test_generated_page_is_image_only_and_the_real_pdfium_parser_records_ocr_provenance() -> None:
    corpus, _ = quality._load_corpus()
    expected = corpus["cases"][0]["ground_truth"]
    pdf_bytes = quality._raster_pdf(expected)

    assert PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text() in (None, "")

    class FixtureProvider:
        name = "synthetic-test-provider"
        version = "synthetic-test-provider-v1"

        def availability(self) -> tuple[bool, None]:
            return True, None

        def recognize(self, image_png: bytes) -> list[OcrLine]:
            assert image_png.startswith(b"\x89PNG\r\n\x1a\n")
            return [
                OcrLine(line, 1.0, (10, 10 + index * 50, 1200, 40))
                for index, line in enumerate(expected.splitlines())
            ]

    parsed = parse_bytes(
        pdf_bytes,
        filename="synthetic-image-only-ddr.pdf",
        content_type="application/pdf",
        ocr_provider=FixtureProvider(),
        ocr_max_pages=1,
        ocr_dpi=200,
        ocr_max_page_pixels=12_000_000,
    )

    assert parsed.pages[0].ocr_applied is True
    assert parsed.pages[0].ocr_status == "applied"
    assert parsed.pages[0].text == expected
    assert parsed.metadata["ocr_applied_pages"] == [1]
    assert parsed.metadata["ocr_provider"] == "synthetic-test-provider"
    assert parsed.metadata["ocr_status_by_page"] == {"1": "applied"}


def test_32_case_quality_benchmark_uses_image_only_pdfs_and_never_emits_transcripts(monkeypatch) -> None:
    corpus, raw = quality._load_corpus()

    class DeterministicTestProvider:
        def __init__(self, **kwargs) -> None:
            self.languages = kwargs["languages"]
            self.max_concurrency = kwargs["max_concurrency"]
            self.name = "tesseract"
            self.version = "synthetic-stub-v1"
            self.case_index = 0

        def diagnostics(self) -> dict[str, object]:
            return {
                "available": True,
                "runtime_version": "synthetic-stub-v1",
                "missing_languages": [],
            }

        def availability(self) -> tuple[bool, None]:
            return True, None

        def recognize(self, image_png: bytes) -> list[OcrLine]:
            assert image_png.startswith(b"\x89PNG\r\n\x1a\n")
            expected = corpus["cases"][self.case_index]["ground_truth"]
            self.case_index += 1
            return [
                OcrLine(line, 1.0, (10, 10 + index * 50, 1200, 40))
                for index, line in enumerate(expected.splitlines())
            ]

    monkeypatch.setattr(quality, "TesseractOcrProvider", DeterministicTestProvider)
    report = quality.run_synthetic_quality_benchmark(Settings(_env_file=None, ocr_languages="eng+fas"))
    serialized = json.dumps(report, sort_keys=True)

    assert report["status"] == "passed"
    assert report["quality_evaluated"] is True
    assert report["dataset"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert report["metrics"]["case_count"] == 32
    assert report["metrics"]["evaluated_cases"] == 32
    assert report["metrics"]["incomplete_or_failed_cases"] == 0
    assert report["metrics"]["character_error_rate"] == 0
    assert report["metrics"]["word_error_rate"] == 0
    assert report["metrics"]["critical_field_exact_rate"] == 1
    assert report["metrics"]["thresholds_passed"] is True
    assert len(report["cases"]) == 32
    assert all(case["status"] == "evaluated" for case in report["cases"])
    assert "SYNTHETIC DAILY DRILLING REPORT" not in serialized
    assert "MEASURED DEPTH:" not in serialized
    assert "not sampled field or vendor DDRs" in report["limitations"][0]


def test_quality_benchmark_blocks_when_the_configured_ocr_runtime_is_unavailable(monkeypatch) -> None:
    class MissingRuntime:
        def __init__(self, **_kwargs) -> None:
            self.languages = "eng+fas"
            self.max_concurrency = 2
            self.name = "tesseract"
            self.version = "adapter-only"

        def diagnostics(self) -> dict[str, object]:
            return {
                "available": False,
                "runtime_version": None,
                "missing_languages": ["eng", "fas"],
            }

    monkeypatch.setattr(quality, "TesseractOcrProvider", MissingRuntime)
    monkeypatch.setattr(quality, "_pdfium_details", lambda: (True, "6.0.0"))
    report = quality.run_synthetic_quality_benchmark(Settings(_env_file=None))

    assert report["status"] == "blocked"
    assert report["quality_evaluated"] is False
    assert report["metrics"] is None
    assert report["cases"] == []
    assert report["runtime"]["runtime_version"] is None


def _valid_quality_report(corpus: dict, raw: bytes) -> dict:
    return {
        "tool": "drillai-synthetic-ocr-quality-v1",
        "status": "passed",
        "quality_evaluated": True,
        "dataset": {
            "id": corpus["corpus_id"],
            "version": corpus["version"],
            "classification": "synthetic",
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "thresholds": {
            "max_character_error_rate": 0.15,
            "max_word_error_rate": 0.2,
            "min_critical_field_exact_rate": 0.9,
        },
        "metrics": {
            "case_count": len(corpus["cases"]),
            "evaluated_cases": len(corpus["cases"]),
            "incomplete_or_failed_cases": 0,
            "character_error_rate": 0.01,
            "word_error_rate": 0.02,
            "critical_field_exact_rate": 0.95,
            "thresholds_passed": True,
        },
        "cases": [{"id": case["case_id"], "status": "evaluated"} for case in corpus["cases"]],
    }


def test_deployment_quality_report_verifier_accepts_complete_pinned_metrics(tmp_path) -> None:
    corpus, raw = quality._load_corpus()
    report_path = tmp_path / "quality-report.json"
    corpus_path = quality._CORPUS_PATH
    report_path.write_text(json.dumps(_valid_quality_report(corpus, raw)), encoding="utf-8")
    verifier = Path(__file__).resolve().parents[3] / "scripts" / "verify_synthetic_ocr_quality.py"

    result = subprocess.run(
        [sys.executable, str(verifier), str(report_path), "--corpus", str(corpus_path)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "SYNTHETIC_OCR_QUALITY_OK cases=32" in result.stdout


def test_deployment_quality_report_verifier_rejects_transcript_leak_and_digest_mismatch(tmp_path) -> None:
    corpus, raw = quality._load_corpus()
    report = _valid_quality_report(corpus, raw)
    report["cases"][0]["text"] = "synthetic recognized text must never be persisted"
    report_path = tmp_path / "unsafe-quality-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    verifier = Path(__file__).resolve().parents[3] / "scripts" / "verify_synthetic_ocr_quality.py"

    leaked = subprocess.run(
        [sys.executable, str(verifier), str(report_path), "--corpus", str(quality._CORPUS_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert leaked.returncode == 1
    assert "raw text" in leaked.stderr

    report["cases"][0].pop("text")
    report["dataset"]["sha256"] = "0" * 64
    report_path.write_text(json.dumps(report), encoding="utf-8")
    stale = subprocess.run(
        [sys.executable, str(verifier), str(report_path), "--corpus", str(quality._CORPUS_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert stale.returncode == 1
    assert "complete pass" in stale.stderr
