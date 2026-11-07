"""Governance and source-integrity checks for the fictional DDR/OCR test corpus."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath

from pypdf import PdfReader
from tests.fixtures.synthetic_ddr import (
    SYNTHETIC_CORPUS_ROOT,
    SYNTHETIC_DDR_CSV,
    SYNTHETIC_DDR_TEXT,
    SYNTHETIC_MARKER,
    SYNTHETIC_OCR_GROUND_TRUTH,
    SYNTHETIC_OCR_PDF,
    SYNTHETIC_PROGRAM_TEXT,
)

from drillai.ingestion.ocr import TesseractOcrProvider
from drillai.ingestion.ocr_benchmark import _read_pinned_file, load_manifest, run_benchmark


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_synthetic_corpus_is_pinned_marked_and_locator_checked() -> None:
    corpus_root = SYNTHETIC_CORPUS_ROOT
    manifest = json.loads((corpus_root / "corpus.json").read_text(encoding="utf-8"))

    assert set(manifest) == {"schema_version", "corpus", "documents"}
    assert manifest["schema_version"] == 1
    assert manifest["corpus"]["classification"] == "synthetic"
    assert manifest["corpus"]["external_approval"] == "not_applicable_synthetic_fixture"
    assert manifest["corpus"]["label_status"] == "synthetic_reference_only"
    assert "field-document accuracy" in manifest["corpus"]["not_evidence_of"]
    assert len(manifest["documents"]) == 4

    canonical_fixtures = {
        "ddr-csv-014": SYNTHETIC_DDR_CSV,
        "ddr-narrative-015": SYNTHETIC_DDR_TEXT,
        "ddr-negative-program-016": SYNTHETIC_PROGRAM_TEXT,
        "ddr-ocr-scan-017": SYNTHETIC_OCR_PDF,
    }
    seen_ids: set[str] = set()
    for document in manifest["documents"]:
        assert set(document) == {
            "id", "path", "sha256", "media_type", "locale", "role", "synthetic_marker", "expected", "source_locators"
        } | ({"reference_text", "reference_sha256"} if document["media_type"] == "application/pdf" else set())
        assert document["id"] not in seen_ids
        seen_ids.add(document["id"])
        assert document["locale"] == "en"
        assert document["synthetic_marker"] == SYNTHETIC_MARKER

        relative = PurePosixPath(document["path"])
        assert not relative.is_absolute() and ".." not in relative.parts
        source_path = (corpus_root / Path(*relative.parts)).resolve(strict=True)
        source_path.relative_to(corpus_root.resolve(strict=True))
        source = source_path.read_bytes()
        assert _sha256(source) == document["sha256"]
        assert source == canonical_fixtures[document["id"]]

        if document["media_type"] == "application/pdf":
            reference_path = corpus_root / document["reference_text"]
            reference = reference_path.read_bytes()
            assert _sha256(reference) == document["reference_sha256"]
            assert SYNTHETIC_MARKER.encode() in reference
            assert len(PdfReader(io.BytesIO(source)).pages) == 1
            assert "".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(source)).pages) == ""
            locator_bytes = reference
        else:
            assert SYNTHETIC_MARKER.encode() in source
            locator_bytes = source

        text_lines = locator_bytes.decode("utf-8").splitlines()
        csv_rows = list(csv.reader(io.StringIO(source.decode("utf-8")))) if document["media_type"] == "text/csv" else []
        for item in document["source_locators"]:
            locator = item["locator"]
            if locator["kind"] == "csv_cell":
                assert csv_rows[locator["row"] - 1][locator["column"] - 1] == item["value"]
            elif locator["kind"] == "csv_rows":
                assert locator["first"] <= locator["last"]
                count = locator["last"] - locator["first"] + 1
                assert count == int(item["value"] if item["field"] == "measurement_rows" else item["value"].split("-")[-1])
                assert locator["last"] <= len(csv_rows)
            elif locator["kind"] in {"line", "ground_truth_line"}:
                assert item["value"] in text_lines[locator["number"] - 1]
            else:  # pragma: no cover - manifest contract guard
                raise AssertionError(f"unsupported synthetic source locator: {locator['kind']}")

    assert seen_ids == set(canonical_fixtures)


def test_synthetic_ocr_benchmark_manifest_needs_no_external_approval_and_fails_closed() -> None:
    ocr_root = SYNTHETIC_CORPUS_ROOT / "ocr"
    manifest, cases, manifest_digest = load_manifest(ocr_root / "manifest.json")

    assert manifest["corpus"]["classification"] == "synthetic"
    assert manifest["corpus"]["approval"] == {"declaration": "not_required_synthetic"}
    assert len(cases) == 1
    assert cases[0].split == "development"

    pdf, _ = _read_pinned_file(
        ocr_root,
        cases[0].pdf_path,
        cases[0].pdf_sha256,
        max_bytes=2_000_000,
        label="synthetic PDF",
    )
    reference, _ = _read_pinned_file(
        ocr_root,
        cases[0].ground_truth_path,
        cases[0].ground_truth_sha256,
        max_bytes=100_000,
        label="synthetic reference",
    )
    assert pdf == SYNTHETIC_OCR_PDF
    assert reference == SYNTHETIC_OCR_GROUND_TRUTH
    assert SYNTHETIC_MARKER.encode() in reference

    report = run_benchmark(
        manifest,
        cases,
        manifest_digest,
        corpus_root=ocr_root,
        provider=TesseractOcrProvider(languages="eng", executable="drillai-no-such-tesseract-executable"),
    )
    assert report["status"] == "blocked"
    assert report["provider_available"] is False
    assert report["blocked_reason"] == "Tesseract executable was not found on the server PATH"
    assert report["corpus_approval_evidence"] == "not_applicable_synthetic_corpus"
    assert report["evaluation_scope"] == "development_only"
    assert report["quality_assessment"].startswith("synthetic fixture transcription metrics only")
    assert report["cases"] == []
    assert report["totals"] is None


def test_synthetic_benchmark_refuses_holdout_cases_even_for_direct_call(tmp_path) -> None:
    import pytest

    from drillai.ingestion.ocr_benchmark import BenchmarkCase, BenchmarkInputError

    case = BenchmarkCase(
        case_id="synthetic-no-holdout",
        pdf_path="scan.pdf",
        pdf_sha256="0" * 64,
        ground_truth_path="scan.txt",
        ground_truth_sha256="1" * 64,
        page_numbers=None,
        locale="en",
        document_class="daily_report",
        split="holdout",
    )
    with pytest.raises(BenchmarkInputError, match="synthetic corpus cases must use the development split"):
        run_benchmark(
            {"schema_version": 2, "corpus": {
                "id": "synthetic-direct-call",
                "version": "1.0",
                "classification": "synthetic",
                "approval": {"declaration": "not_required_synthetic"},
                "languages": ["eng"],
                "sampling_method": "generated test-only fixture",
                "ground_truth_protocol": "fixture reference only",
            }},
            [case],
            "2" * 64,
            corpus_root=tmp_path,
            provider=TesseractOcrProvider(languages="eng", executable="drillai-no-such-tesseract-executable"),
        )
