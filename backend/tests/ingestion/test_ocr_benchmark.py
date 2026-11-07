"""OCR benchmark mechanics and corpus-safety tests; no OCR accuracy is asserted here."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from drillai.ingestion.ocr import TesseractOcrProvider
from drillai.ingestion.ocr_benchmark import (
    BenchmarkCase,
    BenchmarkInputError,
    _checked_relative_path,
    _read_pinned_file,
    error_rates,
    levenshtein_distance,
    load_manifest,
    main,
    run_benchmark,
)


def _corpus_metadata() -> dict:
    return {
        "id": "drillai-synthetic-test-corpus",
        "version": "1.0.0",
        "classification": "synthetic",
        "approval": {"declaration": "not_required_synthetic"},
        "languages": ["eng", "fas"],
        "sampling_method": "hand-authored synthetic mechanics fixtures; not sampled from field documents",
        "ground_truth_protocol": "fixture reference labels only; not independently adjudicated",
    }


def test_levenshtein_and_cer_wer_are_exact_after_documented_normalization() -> None:
    assert levenshtein_distance("kitten", "sitting") == 3
    metrics = error_rates("Cat   sat", "cat sit")
    assert metrics == {
        "expected_characters": 7,
        "actual_characters": 7,
        "character_errors": 1,
        "cer": pytest.approx(1 / 7),
        "expected_words": 2,
        "actual_words": 2,
        "word_errors": 1,
        "wer": pytest.approx(0.5),
    }


def test_edit_distance_and_ground_truth_have_explicit_work_and_content_bounds() -> None:
    with pytest.raises(BenchmarkInputError, match="workload"):
        error_rates("abc", "abd", max_cells=1)
    with pytest.raises(BenchmarkInputError, match="must not be empty"):
        error_rates(" \n\t", "text")


def test_manifest_pins_both_source_pdf_and_ground_truth_hashes(tmp_path) -> None:
    corpus = tmp_path / "corpus"
    (corpus / "scans").mkdir(parents=True)
    (corpus / "labels").mkdir()
    pdf = b"synthetic PDF bytes for manifest mechanics only"
    label = "known answer, not OCR output"
    (corpus / "scans" / "scan.pdf").write_bytes(pdf)
    (corpus / "labels" / "scan.txt").write_text(label, encoding="utf-8")
    manifest_body = {
        "schema_version": 2,
        "corpus": _corpus_metadata(),
        "cases": [
            {
                "id": "scan-001",
                "pdf": "scans/scan.pdf",
                "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
                "ground_truth": "labels/scan.txt",
                "ground_truth_sha256": hashlib.sha256(label.encode()).hexdigest(),
                "locale": "fa",
                "document_class": "daily_report",
                "split": "development",
                "page_numbers": [1, 2],
            }
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_raw = json.dumps(manifest_body, sort_keys=True).encode()
    manifest_path.write_bytes(manifest_raw)

    manifest, cases, manifest_digest = load_manifest(manifest_path)
    assert manifest["schema_version"] == 2
    assert manifest["corpus"]["approval"] == {"declaration": "not_required_synthetic"}
    assert cases[0].case_id == "scan-001"
    assert cases[0].locale == "fa"
    assert cases[0].split == "development"
    assert cases[0].page_numbers == (1, 2)
    assert manifest_digest == hashlib.sha256(manifest_raw).hexdigest()
    loaded_pdf, source_path = _read_pinned_file(
        corpus, cases[0].pdf_path, cases[0].pdf_sha256, max_bytes=1024, label="source PDF"
    )
    assert loaded_pdf == pdf
    assert source_path == corpus / "scans" / "scan.pdf"


def test_synthetic_manifest_is_development_only_and_language_aware(tmp_path) -> None:
    manifest = {
        "schema_version": 2,
        "corpus": _corpus_metadata(),
        "cases": [
            {
                "id": "scan-001",
                "pdf": "scan.pdf",
                "pdf_sha256": "0" * 64,
                "ground_truth": "scan.txt",
                "ground_truth_sha256": "1" * 64,
                "locale": "fa",
                "document_class": "daily_report",
                "split": "development",
            }
        ],
    }
    path = tmp_path / "manifest.json"

    manifest["corpus"]["classification"] = "restricted"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BenchmarkInputError, match="self-attested approval"):
        load_manifest(path)

    manifest["corpus"] = _corpus_metadata()
    manifest["cases"][0]["split"] = "holdout"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BenchmarkInputError, match="synthetic corpus cases must use the development split"):
        load_manifest(path)

    manifest["cases"][0]["split"] = "development"
    manifest["corpus"]["languages"] = ["eng"]
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BenchmarkInputError, match="omits fas"):
        load_manifest(path)

    manifest["corpus"]["languages"] = ["eng", "fas"]
    manifest["cases"][0]["locale"] = {"invalid": "type"}
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(BenchmarkInputError, match="locale"):
        load_manifest(path)


def test_manifest_rejects_path_traversal_windows_paths_and_digest_mismatch(tmp_path) -> None:
    for unsafe in ("../outside.pdf", "scans/../../outside.pdf", "C:/private.pdf", "scans\\private.pdf", "a//b.pdf"):
        with pytest.raises(BenchmarkInputError):
            _checked_relative_path(unsafe, "pdf")

    root = tmp_path / "corpus"
    root.mkdir()
    external = tmp_path / "outside.txt"
    external.write_text("private", encoding="utf-8")
    (root / "escape.txt").symlink_to(external)
    with pytest.raises(BenchmarkInputError, match="outside"):
        _read_pinned_file(root, "escape.txt", hashlib.sha256(b"private").hexdigest(), max_bytes=100, label="file")
    (root / "inside.txt").write_text("local", encoding="utf-8")
    with pytest.raises(BenchmarkInputError, match="does not match"):
        _read_pinned_file(root, "inside.txt", "0" * 64, max_bytes=100, label="file")


def test_benchmark_is_blocked_truthfully_when_tesseract_is_unavailable(tmp_path) -> None:
    manifest = {"schema_version": 2, "corpus": _corpus_metadata()}
    case = BenchmarkCase(
        case_id="pending",
        pdf_path="scan.pdf",
        pdf_sha256="0" * 64,
        ground_truth_path="scan.txt",
        ground_truth_sha256="1" * 64,
        page_numbers=None,
        locale="en",
        document_class="daily_report",
        split="development",
    )
    report = run_benchmark(
        manifest,
        [case],
        "0" * 64,
        corpus_root=tmp_path,
        provider=TesseractOcrProvider(executable="drillai-no-such-tesseract-executable"),
    )
    assert report["status"] == "blocked"
    assert report["provider_available"] is False
    assert report["blocked_reason"] == "Tesseract executable was not found on the server PATH"
    assert report["cases"] == []
    assert report["totals"] is None


def test_cli_reports_missing_runtime_as_blocked_and_exits_nonzero(tmp_path) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "corpus": _corpus_metadata(),
                "cases": [
                    {
                        "id": "pending",
                        "pdf": "scan.pdf",
                        "pdf_sha256": "0" * 64,
                        "ground_truth": "scan.txt",
                        "ground_truth_sha256": "1" * 64,
                        "locale": "en",
                        "document_class": "daily_report",
                        "split": "development",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "report.json"
    exit_code = main(
        [
            "--manifest",
            str(manifest_path),
            "--corpus-root",
            str(tmp_path),
            "--tesseract",
            "drillai-no-such-tesseract-executable",
            "--output",
            str(output_path),
        ]
    )
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 2
    assert report["status"] == "blocked"
    assert report["provider_available"] is False
    assert report["cases"] == []


def test_synthetic_benchmark_report_is_development_only_and_omits_transcripts(tmp_path, monkeypatch) -> None:
    from drillai.ingestion import ocr_benchmark

    pdf = b"fixture bytes; parser is replaced below and this is not OCR evidence"
    label = "حفاری در ۳۲۰۰ متر"
    development_label = "حفاری در ۳۱۰۰ متر"
    (tmp_path / "scan.pdf").write_bytes(pdf)
    (tmp_path / "scan.txt").write_text(label, encoding="utf-8")
    (tmp_path / "scan-development.txt").write_text(development_label, encoding="utf-8")
    case = BenchmarkCase(
        case_id="synthetic-mechanics-only",
        pdf_path="scan.pdf",
        pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        ground_truth_path="scan.txt",
        ground_truth_sha256=hashlib.sha256(label.encode()).hexdigest(),
        page_numbers=None,
        locale="fa",
        document_class="daily_report",
        split="development",
    )
    development_case = BenchmarkCase(
        case_id="synthetic-development-mechanics-only",
        pdf_path="scan.pdf",
        pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        ground_truth_path="scan-development.txt",
        ground_truth_sha256=hashlib.sha256(development_label.encode()).hexdigest(),
        page_numbers=None,
        locale="fa",
        document_class="daily_report",
        split="development",
    )

    class AvailableProvider:
        name = "test-provider"
        version = "test-adapter-only"
        languages = ("eng", "fas")

        @staticmethod
        def availability():
            return True, None

    monkeypatch.setattr(
        ocr_benchmark,
        "parse_bytes",
        lambda *_args, **_kwargs: SimpleNamespace(
            pages=[SimpleNamespace(page_number=1, ocr_applied=True, ocr_status="applied", text=label)]
        ),
    )
    report = run_benchmark(
        {"schema_version": 2, "corpus": _corpus_metadata()},
        [case, development_case],
        "2" * 64,
        corpus_root=tmp_path,
        provider=AvailableProvider(),  # type: ignore[arg-type]
    )

    assert report["status"] == "complete"  # mocked mechanics only; not an OCR quality acceptance
    assert report["evaluation_scope"] == "development_only"
    assert report["corpus"]["approval"] == {"declaration": "not_required_synthetic"}
    assert report["corpus_approval_evidence"] == "not_applicable_synthetic_corpus"
    assert report["totals"]["holdout_cases"] == 0
    assert report["totals"]["holdout_evaluated_cases"] == 0
    assert report["split_integrity_evidence"] == "single_development_fixture_not_independent"
    assert "do not estimate real-world OCR performance" in report["metrics_scope_note"]
    assert report["totals"]["cer"] > 0.0
    assert report["totals"]["wer"] > 0.0
    assert report["split_totals"]["development"]["cer"] > 0.0
    assert report["split_totals"]["development"]["wer"] > 0.0
    assert report["strata"][0]["locale"] == "fa"
    assert report["strata"][0]["document_class"] == "daily_report"
    assert {stratum["split"] for stratum in report["strata"]} == {"development"}
    assert label not in json.dumps(report, ensure_ascii=False)
    assert report["quality_assessment"] == (
        "synthetic fixture transcription metrics only; no real-corpus, field, engineering, or production acceptance"
    )


def test_benchmark_failure_report_does_not_leak_paths_or_transcript_fragments(tmp_path, monkeypatch) -> None:
    from drillai.ingestion import ocr_benchmark

    pdf = b"pdf bytes"
    label = "private ground-truth transcript"
    (tmp_path / "scan.pdf").write_bytes(pdf)
    (tmp_path / "scan.txt").write_text(label, encoding="utf-8")
    case = BenchmarkCase(
        case_id="private-case-id",
        pdf_path="scan.pdf",
        pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        ground_truth_path="scan.txt",
        ground_truth_sha256=hashlib.sha256(label.encode()).hexdigest(),
        page_numbers=None,
        locale="en",
        document_class="daily_report",
        split="development",
    )

    class AvailableProvider:
        name = "test-provider"
        version = "test-adapter-only"
        languages = ("eng", "fas")

        @staticmethod
        def availability():
            return True, None

    monkeypatch.setattr(
        ocr_benchmark,
        "parse_bytes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValueError(f"failed at {tmp_path}/scan.pdf: {label}")
        ),
    )
    report = run_benchmark(
        {"schema_version": 2, "corpus": _corpus_metadata()},
        [case],
        "4" * 64,
        corpus_root=tmp_path,
        provider=AvailableProvider(),  # type: ignore[arg-type]
    )
    serialized = json.dumps(report, ensure_ascii=False)
    assert report["status"] == "incomplete"
    assert report["cases"][0]["reason"] == "processing failed (ValueError)"
    assert str(tmp_path) not in serialized
    assert label not in serialized
    assert "scan.pdf" not in serialized


def test_benchmark_enforces_one_aggregate_edit_distance_budget(tmp_path, monkeypatch) -> None:
    from drillai.ingestion import ocr_benchmark

    pdf = b"pdf bytes"
    label = "a sufficiently long engineering label"
    (tmp_path / "scan.pdf").write_bytes(pdf)
    (tmp_path / "scan.txt").write_text(label, encoding="utf-8")
    case = BenchmarkCase(
        case_id="over-budget",
        pdf_path="scan.pdf",
        pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        ground_truth_path="scan.txt",
        ground_truth_sha256=hashlib.sha256(label.encode()).hexdigest(),
        page_numbers=None,
        locale="en",
        document_class="daily_report",
        split="development",
    )

    class AvailableProvider:
        name = "test-provider"
        version = "test-adapter-only"
        languages = ("eng", "fas")

        @staticmethod
        def availability():
            return True, None

    monkeypatch.setattr(
        ocr_benchmark,
        "parse_bytes",
        lambda *_args, **_kwargs: SimpleNamespace(
            pages=[SimpleNamespace(page_number=1, ocr_applied=True, ocr_status="applied", text=label)]
        ),
    )
    report = run_benchmark(
        {"schema_version": 2, "corpus": _corpus_metadata()},
        [case],
        "3" * 64,
        corpus_root=tmp_path,
        provider=AvailableProvider(),  # type: ignore[arg-type]
        max_total_edit_cells=1,
    )
    assert report["status"] == "incomplete"
    assert report["cases"][0]["status"] == "failed"
    assert "aggregate edit-distance work" in report["cases"][0]["reason"]
    assert report["totals"]["edit_distance_cells_used"] == 0
    assert report["totals"]["edit_distance_cell_limit"] == 1


def test_benchmark_rejects_per_metric_workload_before_scoring_or_leaking_text(tmp_path, monkeypatch) -> None:
    from drillai.ingestion import ocr_benchmark

    pdf = b"pdf bytes"
    label = "a" * 5001
    (tmp_path / "scan.pdf").write_bytes(pdf)
    (tmp_path / "scan.txt").write_text(label, encoding="utf-8")
    case = BenchmarkCase(
        case_id="per-metric-budget",
        pdf_path="scan.pdf",
        pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        ground_truth_path="scan.txt",
        ground_truth_sha256=hashlib.sha256(label.encode()).hexdigest(),
        page_numbers=None,
        locale="en",
        document_class="daily_report",
        split="development",
    )

    class AvailableProvider:
        name = "test-provider"
        version = "test-adapter-only"
        languages = ("eng", "fas")

        @staticmethod
        def availability():
            return True, None

    monkeypatch.setattr(
        ocr_benchmark,
        "parse_bytes",
        lambda *_args, **_kwargs: SimpleNamespace(
            pages=[SimpleNamespace(page_number=1, ocr_applied=True, ocr_status="applied", text=label)]
        ),
    )
    report = run_benchmark(
        {"schema_version": 2, "corpus": _corpus_metadata()},
        [case],
        "5" * 64,
        corpus_root=tmp_path,
        provider=AvailableProvider(),  # type: ignore[arg-type]
    )

    assert report["status"] == "incomplete"
    assert all("per-metric edit-distance workload" in item["reason"] for item in report["cases"])
    assert report["totals"]["edit_distance_cells_used"] == 0
    assert label not in json.dumps(report)


def test_benchmark_does_not_score_truncated_ocr_output(tmp_path, monkeypatch) -> None:
    from drillai.ingestion import ocr_benchmark

    pdf = b"operator-supplied corpus placeholder"
    label = "expected complete transcript"
    (tmp_path / "scan.pdf").write_bytes(pdf)
    (tmp_path / "scan.txt").write_text(label, encoding="utf-8")
    case = BenchmarkCase(
        case_id="truncated",
        pdf_path="scan.pdf",
        pdf_sha256=hashlib.sha256(pdf).hexdigest(),
        ground_truth_path="scan.txt",
        ground_truth_sha256=hashlib.sha256(label.encode()).hexdigest(),
        page_numbers=None,
        locale="en",
        document_class="daily_report",
        split="development",
    )

    class AvailableProvider:
        name = "test-provider"
        version = "contract-only"
        languages = ("eng", "fas")

        @staticmethod
        def availability():
            return True, None

    monkeypatch.setattr(
        ocr_benchmark,
        "parse_bytes",
        lambda *_args, **_kwargs: SimpleNamespace(
            pages=[
                SimpleNamespace(
                    page_number=1,
                    ocr_applied=True,
                    ocr_status="truncated",
                    text="partial result",
                )
            ]
        ),
    )
    report = run_benchmark(
        {"schema_version": 2, "corpus": _corpus_metadata()},
        [case],
        "1" * 64,
        corpus_root=tmp_path,
        provider=AvailableProvider(),  # type: ignore[arg-type]
    )
    assert report["status"] == "incomplete"
    assert report["cases"][0]["status"] == "incomplete"
    assert report["cases"][0]["ocr_status_by_page"] == {"1": "truncated"}
    assert report["totals"]["evaluated_cases"] == 0
    assert report["totals"]["cer"] is None
