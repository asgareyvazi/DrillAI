# Synthetic DDR test corpus

This directory contains a small, **fictional** test corpus for DrillAI's DDR ingestion and OCR benchmark mechanics. Every record was hand-authored or generated locally for software tests; none was copied from a rig, operator, vendor, or confidential report. The labels describe intended fixture content only.

`corpus.json` pins each file by SHA-256 and records its intended role, synthetic classification, expected reference fields, and source locators. The structured CSV and narrative text are the canonical bytes used by `tests/fixtures/synthetic_ddr.py`. `program_negative.txt` is a negative document-type control. `ocr/scan_017_image_only.pdf` is a raster-only PDF: its image contains a fictional DDR, while `ocr/scan_017_ground_truth.txt` contains the exact intended text. The nested `ocr/manifest.json` follows the local OCR benchmark schema and declares only a development fixture. The root `.gitattributes` forces LF working-tree bytes for all four manifest-pinned CSV/text files, so the declared hashes are stable on Windows and Linux; do not normalize these files to a platform-default line ending.

No corpus-approval, field-sampling, independent-transcription, operator-review, or engineering-approval claim is made. Synthetic benchmark output must remain labelled as fixture-only and must not be presented as OCR quality on real reports, field validation, or production acceptance. The absence of external approval is intentional and is not a deficiency for this software-only fixture.

## Updating the corpus

1. Create only fictional data; include the exact `SYNTHETIC TEST DATA` marker in every text source and in the OCR reference text.
2. Record each file's media type, locale, purpose, SHA-256, expected reference values, and exact source locator in `corpus.json`.
3. For OCR cases, use a raster-only PDF, pin both PDF and reference-text hashes in `ocr/manifest.json`, keep the split `development`, and declare `classification: synthetic` with only `approval.declaration: not_required_synthetic`.
4. Do not add operator-attested approval fields to synthetic metadata, do not call a synthetic split a holdout, and do not imply that the intended label is an independently adjudicated field truth.
5. Run `tests/ingestion/test_synthetic_ddr_corpus.py` and `tests/ingestion/test_ocr_benchmark.py` after changes.
