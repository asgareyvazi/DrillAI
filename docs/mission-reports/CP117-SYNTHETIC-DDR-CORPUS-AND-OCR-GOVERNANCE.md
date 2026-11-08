# CP117 — Governed Synthetic DDR Corpus and Safe OCR Benchmark Path

## Disposition

| Area | Status | Evidence / boundary |
| --- | --- | --- |
| Synthetic DDR corpus governance | **DONE** | Added a manifest-pinned fictional corpus with structured CSV, narrative text, a negative program control, and an image-only synthetic OCR PDF with intended fixture text. Every text/reference source carries the exact `SYNTHETIC TEST DATA` marker. No field/vendor source or external approval is claimed. |
| Synthetic OCR benchmark input path | **DONE** | OCR benchmark schema 2 accepts only an explicit `classification: synthetic` plus `approval: {declaration: not_required_synthetic}` for synthetic corpora. Such cases are development-only; loader and direct benchmark calls reject validation/holdout labels. Reports identify approval as not applicable and state that fixture metrics do not estimate field OCR performance. Operator-supplied corpora retain their existing attestation contract. |
| Local OCR runtime / transcription quality | **BLOCKED_EXTERNAL** | The installed Tesseract executable is absent from `PATH`. The runtime self-test and real synthetic benchmark both return blocked; no OCR transcription, CER/WER result, or runtime latency was produced. PDFium is present but is not an OCR engine. |
| Field, engineering, or operational acceptance | **NEEDS_FIELD_VALIDATION** | The corpus is fictional software-test material. It provides no evidence of real-report extraction accuracy, qualified engineering review, field validation, or operational authorization. |

## Corpus and provenance

The canonical files are under `backend/tests/fixtures/synthetic_ddr_corpus/`; `corpus.json` binds the file hashes, roles, expected fixture values, and source locators. The structured and narrative records are the bytes used by the existing DDR ingestion/idempotency tests; the non-DDR program is a negative control. The OCR PDF contains a single raster page with no extractable PDF text operators. Its intended text is a separate, SHA-pinned fixture label. The corpus README defines the update policy and prohibits synthetic holdout claims and fabricated operator approvals.

The benchmark's synthetic metadata contains no operator, approval reference, timestamp, or attestation. `corpus_approval_evidence` is `not_applicable_synthetic_corpus`, `evaluation_scope` is `development_only`, and `quality_assessment` explicitly limits any future run to synthetic fixture transcription metrics. The existing real/operator-corpus path still requires an operator self-attestation and reports it as unverified by the tool; this change does not manufacture or upgrade such evidence.

## Verification

- `backend/.venv/bin/python -m pytest tests/ingestion/test_ocr_benchmark.py tests/ingestion/test_synthetic_ddr_corpus.py -q`: **15 passed**. Tests verify SHA pins, exact source locators, raster-only PDF behavior, development-only split enforcement, no-approval report metadata, bounds, and safe failure reporting. Mock-provider tests exercise benchmark/report mechanics only and are not OCR-quality evidence.
- Related ingestion/document/DDR idempotency selection (`test_pipeline.py`, `test_ddr_idempotency.py`, `test_document_provenance.py`, and both corpus/benchmark modules): **152 passed**.
- Targeted Ruff, changed-module `compileall`, and JSON parsing passed.
- `python -m drillai.ingestion.ocr_runtime --self-test`: exit **1**, `runtime_status: blocked`, `quality_evaluated: false`, reason `Tesseract executable was not found on the server PATH`; the in-memory canary was blocked and emitted no recognized text.
- The pinned synthetic OCR benchmark CLI produced `status: blocked`, `provider_available: false`, `totals: null`, no evaluated cases, and exit **2** for the same missing executable. Its report contained only the explicit synthetic no-approval status; no CER/WER or latency was reported.
- Current backend collection is **1,707 tests across 128 files**. `ci/test-inventory.json` was updated to the reviewed Linux/default/Windows expectations. A full 1,707-test suite and exact-final-SHA CI were not run at this checkpoint.

## Limits and follow-up

Install and configure Tesseract plus the required language packs in a controlled runtime before claiming OCR runtime readiness. A synthetic OCR result, if later produced, remains a development-fixture result and does not qualify extraction on real reports. Real-corpus quality work requires authorized data, genuine governance and independently adjudicated labels; none are fabricated or bundled here. The later CP130–CP132 exact-SHA Linux/native-Windows certification completed in run `42481107515`; its image OCR notice measured 32 synthetic English cases (CER 0, WER 0, critical-field exact rate 1.0). This does not alter the historical local-runtime failure or establish field OCR accuracy; see `CP130-EXECUTION-RELIABILITY-AND-EVALUATION.md`.
