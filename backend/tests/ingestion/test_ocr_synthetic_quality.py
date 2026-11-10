"""Synthetic OCR harness tests; only deployment-image CI uses real Tesseract accuracy."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

from pypdf import PdfReader

from drillai.core.config import Settings
from drillai.ingestion import ocr_synthetic_quality as quality
from drillai.ingestion.ocr import OcrLine
from drillai.ingestion.parsers import parse_bytes


def test_pinned_synthetic_ocr_corpus_has_64_unique_bilingual_stratified_cases(tmp_path) -> None:
    corpus, raw = quality._load_corpus()

    assert corpus["classification"] == "synthetic"
    assert corpus["version"] == "2.0.0"
    assert corpus["stratification_dimensions"] == ["language", "layout", "scan_condition"]
    assert len(corpus["cases"]) == 64
    assert len({case["case_id"] for case in corpus["cases"]}) == 64
    assert Counter(case["language"] for case in corpus["cases"]) == {"eng": 48, "fas": 16}
    assert Counter(case["layout"] for case in corpus["cases"]) == {
        "single_column": 20,
        "table": 16,
        "two_column": 12,
        "compact": 16,
    }
    assert Counter(case["scan_condition"] for case in corpus["cases"]) == {
        "clean": 16,
        "low_contrast": 16,
        "light_skew": 16,
        "light_noise": 16,
    }
    assert all(len(case["critical_fields"]) == 6 for case in corpus["cases"])
    assert "No field or vendor documents are sampled" in corpus["sampling_method"]
    assert "not independently adjudicated" in corpus["ground_truth_protocol"]
    assert hashlib.sha256(raw).hexdigest() == quality._base_report(corpus, raw)["dataset"]["sha256"]

    generated = tmp_path / "regenerated-corpus.json"
    generator = Path(__file__).resolve().parents[3] / "scripts" / "generate_synthetic_ocr_quality_corpus.py"
    result = subprocess.run(
        [sys.executable, str(generator), "--output", str(generated)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert generated.read_bytes() == raw


def test_critical_field_metric_requires_language_specific_value_under_expected_label() -> None:
    english_fields = [
        {"key": "report_date", "expected": "2026-01-01"},
        {"key": "measured_depth_m", "expected": "1087.5"},
        {"key": "mud_weight_ppg", "expected": "10.4"},
    ]
    persian_fields = [
        {"key": "report_date", "expected": "2026-01-01"},
        {"key": "measured_depth_m", "expected": "1087.5"},
    ]

    assert quality._critical_field_matches(
        "SAMPLE ID: 2026-01-01\nREPORT DATE: 2026-01-01\nMEASURED DEPTH: 1087.5 M\nMUD WEIGHT: 10.4 PPG",
        english_fields,
        language="eng",
    ) == 3
    assert quality._critical_field_matches(
        "SAMPLE ID: 2026-01-01\nREPORT DATE: 2026-01-02\nMEASURED DEPTH: 1087.5 M\nMUD WEIGHT: 10.4 PPG",
        english_fields,
        language="eng",
    ) == 2
    assert quality._critical_field_matches(
        "شناسه نمونه: 2026-01-01\nتاریخ گزارش: 2026-01-01\nعمق اندازه گیری: 1087.5 M",
        persian_fields,
        language="fas",
    ) == 2


def test_generated_english_and_persian_pages_are_image_only_and_use_real_pdfium_ingestion() -> None:
    corpus, _ = quality._load_corpus()
    cases = (corpus["cases"][0], corpus["cases"][49])

    for case in cases:
        expected = case["ground_truth"]
        pdf_bytes = quality._raster_pdf(case)
        assert PdfReader(io.BytesIO(pdf_bytes)).pages[0].extract_text() in (None, "")

        class FixtureProvider:
            name = "synthetic-test-provider"
            version = "synthetic-test-provider-v1"

            def __init__(self, expected_text: str) -> None:
                self.expected_text = expected_text

            def availability(self) -> tuple[bool, None]:
                return True, None

            def recognize(self, image_png: bytes) -> list[OcrLine]:
                assert image_png.startswith(b"\x89PNG\r\n\x1a\n")
                return [
                    OcrLine(line, 1.0, (10, 10 + index * 50, 1200, 40))
                    for index, line in enumerate(self.expected_text.splitlines())
                ]

        parsed = parse_bytes(
            pdf_bytes,
            filename=f"{case['case_id']}.pdf",
            content_type="application/pdf",
            ocr_provider=FixtureProvider(expected),
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


def test_64_case_quality_harness_exercises_bilingual_strata_without_emitting_transcripts(monkeypatch) -> None:
    corpus, raw = quality._load_corpus()

    class DeterministicTestProvider:
        def __init__(self, **kwargs) -> None:
            self.languages = kwargs["languages"]
            self.max_concurrency = kwargs["max_concurrency"]
            self.name = "tesseract"
            self.version = "synthetic-test-stub-v1"
            self.case_index = 0

        def diagnostics(self) -> dict[str, object]:
            return {
                "available": True,
                "runtime_version": "synthetic-test-stub-v1",
                "configured_languages": ["eng", "fas"],
                "available_languages": ["eng", "fas"],
                "missing_languages": [],
            }

        def availability(self) -> tuple[bool, None]:
            return True, None

        def recognize(self, image_png: bytes) -> list[OcrLine]:
            assert image_png.startswith(b"\x89PNG\r\n\x1a\n")
            expected = corpus["cases"][self.case_index]["ground_truth"]
            self.case_index += 1
            return [
                OcrLine(line, 1.0, (10, 10 + index * 50, 1500, 45))
                for index, line in enumerate(expected.splitlines())
            ]

    monkeypatch.setattr(quality, "TesseractOcrProvider", DeterministicTestProvider)
    report = quality.run_synthetic_quality_benchmark(Settings(_env_file=None, ocr_languages="eng+fas"))
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)

    assert report["status"] == "passed"
    assert report["quality_evaluated"] is True
    assert report["dataset"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert set(report["runtime"]["renderer_dependencies"]) == set(quality._RENDERER_DISTRIBUTIONS)
    assert report["runtime"]["resource_limits"]["max_generated_pdf_bytes"] == quality._MAX_PDF_BYTES
    assert report["metrics"]["case_count"] == 64
    assert report["metrics"]["evaluated_cases"] == 64
    assert report["metrics"]["incomplete_or_failed_cases"] == 0
    assert report["metrics"]["character_error_rate"] == 0
    assert report["metrics"]["word_error_rate"] == 0
    assert report["metrics"]["critical_field_exact_rate"] == 1
    assert report["metrics"]["thresholds_passed"] is True
    assert report["metrics"]["all_strata_thresholds_passed"] is True
    assert len(report["cases"]) == 64
    assert all(case["status"] == "evaluated" for case in report["cases"])
    assert all(
        row["thresholds_passed"]
        for rows in report["metrics"]["stratified_metrics"].values()
        for row in rows
    )
    assert "SYNTHETIC DAILY DRILLING REPORT" not in serialized
    assert "MEASURED DEPTH:" not in serialized
    assert "شناسه نمونه:" not in serialized
    assert "none are sampled field or vendor DDRs" in report["limitations"][0]


def test_quality_benchmark_fails_closed_for_missing_runtime_or_configured_language(monkeypatch) -> None:
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
                "configured_languages": ["eng", "fas"],
                "available_languages": ["eng"],
                "missing_languages": ["fas"],
            }

    monkeypatch.setattr(quality, "TesseractOcrProvider", MissingRuntime)
    monkeypatch.setattr(quality, "_pdfium_details", lambda: (True, "6.0.0"))
    report = quality.run_synthetic_quality_benchmark(Settings(_env_file=None, ocr_languages="eng+fas"))

    assert report["status"] == "blocked"
    assert report["quality_evaluated"] is False
    assert report["metrics"] is None
    assert report["cases"] == []
    assert report["runtime"]["runtime_version"] is None
    assert report["runtime"]["available_languages"] == ["eng"]

    unconfigured = quality.run_synthetic_quality_benchmark(Settings(_env_file=None, ocr_languages="eng"))
    assert unconfigured["status"] == "blocked"
    assert unconfigured["quality_evaluated"] is False
    assert unconfigured["metrics"] is None
    assert unconfigured["runtime"]["configured_languages"] == ["eng"]
    assert "every pinned corpus language" in unconfigured["reason"]


def _valid_quality_report(corpus: dict, raw: bytes) -> dict:
    """Build a zero-edit schema-contract fixture; it is not an OCR/runtime quality result."""
    case_evidence = []
    for case in corpus["cases"]:
        text_metrics = quality.error_rates(case["ground_truth"], case["ground_truth"])
        critical_count = len(case["critical_fields"])
        case_evidence.append(
            {
                "id": case["case_id"],
                "language": case["language"],
                "layout": case["layout"],
                "scan_condition": case["scan_condition"],
                "status": "evaluated",
                "pdf_sha256": hashlib.sha256(case["case_id"].encode()).hexdigest(),
                "cer": 0.0,
                "wer": 0.0,
                "character_errors": 0,
                "expected_characters": int(text_metrics["expected_characters"]),
                "word_errors": 0,
                "expected_words": int(text_metrics["expected_words"]),
                "critical_fields_expected": critical_count,
                "critical_fields_matched": critical_count,
                "processing_ms": 1.0,
                "recognition_ms": 0.5,
            }
        )
    strata = quality._stratified_metrics(corpus["cases"], case_evidence)
    total_character_count = sum(item["expected_characters"] for item in case_evidence)
    total_word_count = sum(item["expected_words"] for item in case_evidence)
    total_critical_count = sum(item["critical_fields_expected"] for item in case_evidence)
    return {
        "tool": "drillai-synthetic-ocr-quality-v2",
        "status": "passed",
        "quality_evaluated": True,
        "dataset": {
            "id": corpus["corpus_id"],
            "version": corpus["version"],
            "classification": "synthetic",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "case_count": len(corpus["cases"]),
            "languages": corpus["languages"],
            "stratification_dimensions": corpus["stratification_dimensions"],
        },
        "runtime": {
            "provider": "tesseract",
            "adapter_version": quality.TESSERACT_ADAPTER_VERSION,
            "page_segmentation_mode": quality.TESSERACT_PAGE_SEGMENTATION_MODE,
            "runtime_version": "test-fixture-only",
            "python_version": "3.11.8",
            "pdf_renderer_version": "test-fixture-only",
            "renderer_dependencies": {
                "Pillow": "13.0.0",
                "reportlab": "4.5.1",
                "arabic-reshaper": "3.1.0",
                "python-bidi": "0.6.12",
                "pypdfium2": "5.15.1",
            },
            "resource_limits": {
                "max_generated_pdf_bytes": quality._MAX_PDF_BYTES,
                "ocr_max_pages": 1,
                "ocr_dpi": 200,
                "ocr_max_page_pixels": 12_000_000,
                "ocr_timeout_seconds": 30.0,
                "ocr_max_concurrency": 2,
            },
            "required_languages": corpus["languages"],
            "configured_languages": ["eng", "fas"],
            "available_languages": ["eng", "fas"],
        },
        "thresholds": {
            "max_character_error_rate": 0.15,
            "max_word_error_rate": 0.20,
            "min_critical_field_exact_rate": 0.90,
        },
        "metrics": {
            "case_count": len(case_evidence),
            "evaluated_cases": len(case_evidence),
            "incomplete_or_failed_cases": 0,
            "character_errors": 0,
            "expected_characters": total_character_count,
            "character_error_rate": 0.0,
            "word_errors": 0,
            "expected_words": total_word_count,
            "word_error_rate": 0.0,
            "exact_case_rate": 1.0,
            "critical_fields_matched": total_critical_count,
            "critical_fields_expected": total_critical_count,
            "critical_field_exact_rate": 1.0,
            "thresholds_passed": True,
            "all_strata_thresholds_passed": True,
            "stratified_metrics": strata,
        },
        "cases": case_evidence,
        "reason": None,
        "limitations": ["Schema-only test fixture; not deployment OCR evidence."],
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
    assert "SYNTHETIC_OCR_QUALITY_OK cases=64" in result.stdout
    assert "language=eng:48,fas:16" in result.stdout
    assert "layout=compact:16,single_column:20,table:16,two_column:12" in result.stdout


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
    assert "complete bilingual pass" in stale.stderr
