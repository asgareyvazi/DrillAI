# CP130 — Execution reliability and synthetic evaluation

**Status:** `DONE for repository software scope on CP130 SHA 70c7de6bada761f5f6f2c580bced9dcb22be13a5; external field/vendor/customer acceptance is not established.` Exact-SHA Linux/native-Windows CI run `42460161993` completed successfully, including the deployment-image synthetic OCR gate. This report covers repository software evidence only. No vendor or field DDR corpus, customer acceptance, live-provider response, delivery receipt, engineering qualification, or production-readiness claim is made.

## Delivered

- Added a pinned, versioned synthetic OCR corpus of 32 generated daily drilling report pages, a bounded in-memory image-only PDF runner through DrillAI's actual `parse_bytes`/PDFium/Tesseract ingestion path, runtime/version and OCR quality metrics (CER, WER, labelled critical-field exact rate, per-case and runtime latency), and a verifier that rejects incomplete/stale reports and transcript leakage. Synthetic fixtures are not sampled field/vendor reports or independently adjudicated ground truth. The benchmark fails closed when runtime, language data, PDFium, or case processing is unavailable. The deployment workflow executes the benchmark *inside the backend deployment image* and adds a numeric-only, explicitly synthetic summary to the job report. Exact-SHA run `42460161993` on CP130 SHA `70c7de6bada761f5f6f2c580bced9dcb22be13a5` passed that deployment-image benchmark gate; its raw metric values could not be recovered here, so none are restated.
- Expanded the deterministic, synthetic RAG benchmark to 160 chunks and 106 queries (including 100 generated scale queries), with explicit cross-well/tenant distractors, quarantine, depth/time/section scope checks, and citation/source-locator metrics. The benchmark remains lexical and synthetic; it does not measure embeddings, semantic ranking, a live LLM, or answer quality.
- Added Advisor tests for evidence grounding, conflicting/ambiguous facts, and abstention; independent-database-pool lifecycle and concurrent-writer coverage; separate connector worker concurrency/race checks; and workflow/notification failure-state coverage with sensitive provider details excluded. Existing authorization and resource-bound checks remain in the suite; no accepted path bypass or test skip was introduced.
- Updated the reviewed backend inventory and maintained documentation for the 14 added backend cases.

## Verification completed locally

- Default backend suite: **1,719 passed, 15 PostgreSQL-only skipped**; inventory verifier accepted.
- PostgreSQL-enabled backend suite: **1,734 passed, 0 skipped**; inventory verifier accepted.
- CP16 maintained documentation/inventory guard: **1 passed**.
- Ruff, Python `compileall`, and `git diff --check`: passed.
- OCR corpus is included in an isolated package wheel build. This sandbox has neither Docker nor Tesseract, so no local OCR quality result is claimed.
- Exact-SHA Actions run [42460161993](https://github.com/asgareyvazi/DrillAI/actions/runs/42460161993) tested `70c7de6bada761f5f6f2c580bced9dcb22be13a5`. Linux job `128667533586` and native-Windows job `128667533459` both concluded `success`. `gh run view` confirmed every named acceptance gate concluded success on both platforms: frontend types/lint/unit/build, supply-chain, backend lint/tests, fresh migration, stack and vendor harness, E2E inventory, and the full Playwright gate. Linux also passed Docker Compose deployment smoke; its script runs the pinned OCR benchmark inside the backend image and fails unless the report is complete, contains all cases, meets thresholds, and passes transcript-leak checks. Only failure-only evidence uploads were skipped on the successful jobs; no acceptance gate was skipped.
- The Actions log archive redirects to `results-receiver.actions.githubusercontent.com`, which is outside this environment's allowed egress hosts and returns `EOF`. Therefore the OCR metric values and printed per-platform test totals from run `42460161993` could not be independently read here; this report does not invent or restate them as observed numbers. The step conclusion proves the image benchmark and its verifier returned successfully, not field OCR accuracy. CP131/CP132 edits after this SHA require their own exact-final-SHA Linux/native-Windows run; CP130's run does not certify that later integration.

## Limitations and disposition

- The OCR corpus is clean, machine-generated English text. Its results can establish only that the configured software/runtime processes these synthetic pages and meets the stated fixture thresholds. It cannot establish field accuracy or DDR semantics.
- The RAG benchmark's generated distractors and relevance labels are deterministic development fixtures, not an independent or representative test set. Lexical retrieval is not semantic-quality evidence.
- No genuine connectors, notification provider, SMTP delivery, vendor service, or external customer systems were contacted. An accepted HTTP/SMTP request would not prove delivery.
- `well_control.kill_sheet` and `tubulars.api_5c3` remain `NEEDS_FIELD_VALIDATION`; no autonomous rig control or arbitrary wellsite write path is authorized.
- CP130's repository software scope is complete for SHA `70c7de6bada761f5f6f2c580bced9dcb22be13a5` based on the successful exact-SHA run above. That run certifies only this commit; later CP131/CP132 changes require their own exact-final-SHA Actions evidence. No field/vendor evidence or qualified engineering acceptance is implied.

## Later CP132 OCR evidence addendum (2026-11-08)

The statement above that raw metrics could not be recovered was specific to base run `42460161993` and remains historically accurate for that run. The later integrated exact-SHA run `42481107515` tested `286945c9d627ba58a4f765b3f28cc65a5e0003ae`; its Linux job `128733009790` emitted the numeric-only verifier notice:

- `SYNTHETIC_OCR_QUALITY_OK`: 32 generated synthetic cases.
- CER `0.00000`; WER `0.00000`; critical-field exact rate `1.00000`.
- Dataset SHA-256: `b682f05c5294a30ac361e26a5f37752587a208aa0d08f83a73e2789e3466ea57`.

This is the deployment-image benchmark's observed result on the pinned synthetic corpus, not OCR evidence for field DDRs. The cases are clean generated English pages; they do not represent Persian documents, vendor forms, difficult/rotated/low-quality scans, independently adjudicated field data, extraction semantics or engineering correctness. No field accuracy, vendor acceptance or production readiness is inferred.
