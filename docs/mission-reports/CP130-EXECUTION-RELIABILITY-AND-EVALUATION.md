# CP130 — Execution reliability and synthetic evaluation

**Status:** `PARTIAL — implementation and Linux local verification delivered; deployment-image OCR and exact-SHA cross-platform certification pending.` This report covers repository software evidence only. No vendor or field DDR corpus, customer acceptance, live-provider response, delivery receipt, engineering qualification, or production-readiness claim is made.

## Delivered

- Added a pinned, versioned synthetic OCR corpus of 32 generated daily drilling report pages, a bounded in-memory image-only PDF runner through DrillAI's actual `parse_bytes`/PDFium/Tesseract ingestion path, runtime/version and OCR quality metrics (CER, WER, labelled critical-field exact rate, per-case and runtime latency), and a verifier that rejects incomplete/stale reports and transcript leakage. Synthetic fixtures are not sampled field/vendor reports or independently adjudicated ground truth. The benchmark fails closed when runtime, language data, PDFium, or case processing is unavailable. The deployment workflow now executes the benchmark *inside the backend deployment image* and adds a numeric-only, explicitly synthetic summary to the job report. That image run is not yet observed on this commit.
- Expanded the deterministic, synthetic RAG benchmark to 160 chunks and 106 queries (including 100 generated scale queries), with explicit cross-well/tenant distractors, quarantine, depth/time/section scope checks, and citation/source-locator metrics. The benchmark remains lexical and synthetic; it does not measure embeddings, semantic ranking, a live LLM, or answer quality.
- Added Advisor tests for evidence grounding, conflicting/ambiguous facts, and abstention; independent-database-pool lifecycle and concurrent-writer coverage; separate connector worker concurrency/race checks; and workflow/notification failure-state coverage with sensitive provider details excluded. Existing authorization and resource-bound checks remain in the suite; no accepted path bypass or test skip was introduced.
- Updated the reviewed backend inventory and maintained documentation for the 14 added backend cases.

## Verification completed locally

- Default backend suite: **1,719 passed, 15 PostgreSQL-only skipped**; inventory verifier accepted.
- PostgreSQL-enabled backend suite: **1,734 passed, 0 skipped**; inventory verifier accepted.
- CP16 maintained documentation/inventory guard: **1 passed**.
- Ruff, Python `compileall`, and `git diff --check`: passed.
- OCR corpus is included in an isolated package wheel build. The local environment lacks Tesseract; no local OCR quality result is claimed. The deployment-image run is the required actual-runtime measurement and remains pending.
- Native-Windows execution and exact-SHA CI have not yet run on this CP130 commit. A prior successful CI run applies only to its recorded earlier SHA and does not certify these changes.

## Limitations and disposition

- The OCR corpus is clean, machine-generated English text. Its results can establish only that the configured software/runtime processes these synthetic pages and meets the stated fixture thresholds. It cannot establish field accuracy or DDR semantics.
- The RAG benchmark's generated distractors and relevance labels are deterministic development fixtures, not an independent or representative test set. Lexical retrieval is not semantic-quality evidence.
- No genuine connectors, notification provider, SMTP delivery, vendor service, or external customer systems were contacted. An accepted HTTP/SMTP request would not prove delivery.
- `well_control.kill_sheet` and `tubulars.api_5c3` remain `NEEDS_FIELD_VALIDATION`; no autonomous rig control or arbitrary wellsite write path is authorized.
- CP130 is not certified complete until the current commit's deployment-image OCR run, native-Windows matrix, exact-SHA Linux/native-Windows CI and gate counts are inspected. Any subsequent source change requires certification of the new final SHA.
