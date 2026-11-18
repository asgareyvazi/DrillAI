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
