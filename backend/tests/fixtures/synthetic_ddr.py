"""Synthetic Daily Drilling Report fixtures — explicitly fictional test inputs.

The canonical bytes live under ``synthetic_ddr_corpus/`` and are pinned by that directory's manifest.
They are not field data. They exist so that the DDR -> structured engineering data pipeline, review
lifecycle, and OCR benchmark mechanics can be exercised without implying an external source or approval.

Nothing in this module is imported by production code: ``drillai.drilling`` never depends on test fixtures.
"""

from __future__ import annotations

from pathlib import Path

SYNTHETIC_CORPUS_ROOT = Path(__file__).with_name("synthetic_ddr_corpus")

#: A DDR-shaped CSV with header rows, an operations table, a drilling-parameters block, and two problems.
SYNTHETIC_DDR_CSV = (SYNTHETIC_CORPUS_ROOT / "records" / "ddr_014_structured.csv").read_bytes()

#: A narrative DDR that exercises label/value parsing without inventing structured rows.
SYNTHETIC_DDR_TEXT = (SYNTHETIC_CORPUS_ROOT / "records" / "ddr_015_narrative.txt").read_bytes()

#: A negative control that must not be promoted as a daily drilling report.
SYNTHETIC_PROGRAM_TEXT = (SYNTHETIC_CORPUS_ROOT / "records" / "program_negative.txt").read_bytes()

#: Raster-only scan and exact intended source text for local OCR benchmark mechanics.
SYNTHETIC_OCR_PDF = (SYNTHETIC_CORPUS_ROOT / "ocr" / "scan_017_image_only.pdf").read_bytes()
SYNTHETIC_OCR_GROUND_TRUTH = (
    SYNTHETIC_CORPUS_ROOT / "ocr" / "scan_017_ground_truth.txt"
).read_bytes()

SYNTHETIC_FIXTURES: dict[str, bytes] = {
    "ddr_csv": SYNTHETIC_DDR_CSV,
    "ddr_text": SYNTHETIC_DDR_TEXT,
    "program": SYNTHETIC_PROGRAM_TEXT,
    "ocr_pdf": SYNTHETIC_OCR_PDF,
    "ocr_ground_truth": SYNTHETIC_OCR_GROUND_TRUTH,
}

#: Every textual fixture/reference must carry this exact marker so test data is not mistaken for field input.
SYNTHETIC_MARKER = "SYNTHETIC TEST DATA"


def fixture(name: str) -> bytes:
    """Return a named synthetic fixture, failing loudly on a typo."""
    try:
        return SYNTHETIC_FIXTURES[name]
    except KeyError as exc:  # pragma: no cover - programming error
        raise KeyError(f"unknown synthetic fixture {name!r}; known: {sorted(SYNTHETIC_FIXTURES)}") from exc
