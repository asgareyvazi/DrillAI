from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "emit_synthetic_ocr_annotations.py"
SPEC = importlib.util.spec_from_file_location("ocr_annotations", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ocr_annotations = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ocr_annotations)


def test_ocr_failure_annotations_are_bounded_and_transcript_free() -> None:
    secret = "TRANSCRIPT MUST NOT APPEAR IN A CI ANNOTATION"
    report = {
        "status": "failed",
        "quality_evaluated": True,
        "dataset": {"id": "synthetic-corpus", "sha256": "a" * 64, "case_count": 64},
        "thresholds": {"max_character_error_rate": 0.15},
        "runtime": {
            "runtime_version": "tesseract 5.5.0",
            "traineddata_sha256": {"fas": "b" * 64},
        },
        "metrics": {
            "case_count": 64,
            "evaluated_cases": 64,
            "stratified_metrics": {
                "language": [
                    {"stratum": "fas", "case_count": 32, "evaluated_cases": 32, "thresholds_passed": False}
                ]
            },
        },
        "cases": [
            {
                "id": f"case-{index:02d}",
                "language": "fas",
                "status": "evaluated",
                "ground_truth": secret,
                "recognized_text": secret,
                "pdf_sha256": "c" * 64,
            }
            for index in range(64)
        ],
    }

    output = ocr_annotations.annotations(report)

    assert len(output) <= ocr_annotations.MAX_NOTICES
    assert all(len(item.encode("utf-8")) <= ocr_annotations.MAX_ANNOTATION_BYTES + 200 for item in output)
    assert secret not in "\n".join(output)
    assert "traineddata_sha256" in output[0]
    assert "case-63" in "\n".join(output)


def test_ocr_routing_annotation_is_bounded_and_does_not_echo_untrusted_values() -> None:
    import json

    evidence = Path(__file__).resolve().parents[3] / "docs" / "mission-reports" / "CONTINUOUS-TESSEROCR-SYNTHETIC-EVIDENCE.json"
    report = json.loads(evidence.read_text(encoding="utf-8"))
    report["runtime"]["synthetic_routing"] = {
        "eng": {"combined_only": 32, "fas_rerun": 0, "unknown": 0},
        "fas": {"combined_only": 0, "fas_rerun": 32, "unknown": 0},
    }
    rendered = ocr_annotations.annotations(report)
    assert len(rendered) <= ocr_annotations.MAX_NOTICES
    assert "synthetic_routing" in rendered[0]
    assert "cp135-ddr-064" in "\n".join(rendered)

    report["runtime"]["synthetic_routing"]["fas"]["unknown"] = "SENSITIVE TEXT"
    rendered = ocr_annotations.annotations(report)
    assert "synthetic_routing" not in rendered[0]
    assert "SENSITIVE TEXT" not in "\n".join(rendered)


def test_synthetic_label_diagnostics_are_count_only_and_reject_untrusted_values() -> None:
    import json

    from drillai.ingestion.ocr_synthetic_quality import _synthetic_label_diagnostic

    counts = _synthetic_label_diagnostic(
        "تاریخ گزارش: SECRET-123\nگزارش تاریخ\n\u200f", language="fas"
    )
    assert counts == {
        "labels_anywhere": 1,
        "labels_prefix": 1,
        "labels_reversed": 0,
        "pages_with_bidi_controls": 1,
    }
    report = json.loads((Path(__file__).resolve().parents[3] / "docs" / "mission-reports" /
                         "CONTINUOUS-TESSEROCR-SYNTHETIC-EVIDENCE.json").read_text(encoding="utf-8"))
    row = {"evaluated": 1, **counts}
    report["runtime"]["synthetic_label_diagnostics"] = {"eng": row, "fas": row.copy()}
    report["runtime"]["synthetic_routing"] = {
        "eng": {"combined_only": 32, "fas_rerun": 0, "unknown": 0},
        "fas": {"combined_only": 0, "fas_rerun": 32, "unknown": 0},
    }
    notices = ocr_annotations.annotations(report)
    assert len(notices) <= ocr_annotations.MAX_NOTICES
    assert all(len(item.encode("utf-8")) <= ocr_annotations.MAX_ANNOTATION_BYTES + 200 for item in notices)
    output = "\n".join(notices)
    assert "synthetic_label_diagnostics" in output
    assert "SECRET-123" not in output
    report["runtime"]["synthetic_label_diagnostics"]["fas"]["labels_prefix"] = "SECRET-123"
    output = "\n".join(ocr_annotations.annotations(report))
    assert "synthetic_label_diagnostics" not in output
    assert "SECRET-123" not in output
