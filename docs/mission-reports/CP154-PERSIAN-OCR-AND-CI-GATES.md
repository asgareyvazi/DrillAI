# CP154 — Persian OCR provenance, quality measurements, and separated CI gates

**Status: `PARTIAL` — model identity is pinned and Persian diagnostics/CI gates improved; CP135 quality remains `NOT_ACCEPTED`. Deployment-image measurement and native-Windows rerun remain outstanding.**

## Source and runtime changes

- Vendored the official Persian `tessdata_fast` file from `tesseract-ocr/tessdata_fast` commit `87416418657359cb625c412a48b6e1d6d41c29bd` at `backend/tessdata/fas.traineddata` (431,500 bytes, SHA-256 `db1c0a91208aff00d3cf1ed2c1d23f76419afd5f024688b4f71adc3f2ce4a505`). The Apache-2.0 license text is retained in `backend/tessdata/LICENSE`; source and pin are recorded in `backend/tessdata/README.md`.
- The Docker image now verifies that exact file digest before installing it into the local Tesseract data directory and checks that Tesseract lists `fas`. English remains supplied by the base-image apt package; its exact digest is reported at runtime, not falsely claimed pinned by this change. Docker was unavailable locally, so this Dockerfile change has not been built in this sandbox.
- OCR diagnostics and synthetic quality reports now include SHA-256 digests of found language models, never local file paths or OCR text. The model identity is evidence of the installed bytes, not evidence of accuracy.
- The deployment job now separates Compose readiness, deployment-image OCR, HTTP readiness, security headers, connector authorization, container inventory, and cleanup into independent steps. Post-OCR probes are allowed to continue after OCR failure unless the workflow is cancelled. On OCR failure, a bounded transcript-free annotation helper reports runtime/model digests, unchanged thresholds, all strata, and case IDs without printing recognized or expected text.

## Full 64-case local measurements

All complete reports under `docs/mission-reports/CP154-LOCAL-*-EVIDENCE.json` use the checked-in synthetic CP135 DDR v3 corpus (dataset SHA-256 `31aca80b4323fe470d34705e36038296ef620adb7525373f5196c9f561b6735d`), 64/64 evaluated cases, and no failed/incomplete cases for the valid runtime runs. These are local virtual-environment measurements with Tesseract `5.5.2-23-g397887`, PDFium `5.15.2`, Python `3.11.2`; they are **not** deployment-image or field measurements.

The unchanged acceptance requirements are global CER ≤ **0.15**, global WER ≤ **0.20**, critical-field exact ≥ **0.90**, and the same critical-field floor in every language/layout/scan-condition stratum. No threshold or expected result was lowered.

| Run / model / renderer | DPI | Global CER | Global WER | Global critical exact | Persian CER / WER / critical exact | Result |
|---|---:|---:|---:|---:|---:|---|
| Fast model baseline, pre-render correction | 200 | 0.116106 | 0.164948 | 0.747396 | 0.246679 / 0.317177 / 0.505208 | `NOT_ACCEPTED` |
| Fast model baseline, pre-render correction | 150 | 0.106762 | 0.154639 | 0.747396 | 0.226961 / 0.295918 / 0.515625 | `NOT_ACCEPTED` |
| Fast model baseline, pre-render correction | 300 | 0.126821 | 0.175687 | 0.734375 | 0.268913 / 0.333333 / 0.484375 | `NOT_ACCEPTED` |
| Best model comparison, original renderer | 200 | 0.113166 | 0.167096 | 0.742188 | 0.239966 / 0.319728 / 0.489583 | `NOT_ACCEPTED`; not adopted |
| Fast model, rejected all-LTR isolation trial (`cp135-rtl-ltr-isolation-v2`) | 200 | 0.140346 | 0.216924 | 0.565104 | 0.298560 / 0.420068 / 0.140625 | `NOT_ACCEPTED`; renderer trial rejected |
| Fast model, ISO-date-only LRM correction (`cp135-rtl-ltr-isolation-v3`) | 200 | 0.107024 | 0.154639 | 0.809896 | 0.227241 / 0.296769 / 0.630208 | `NOT_ACCEPTED` |
| Fast model, ISO-date-only LRM correction (`cp135-rtl-ltr-isolation-v3`) | 150 | 0.097746 | 0.144330 | 0.809896 | 0.207663 / 0.275510 / 0.640625 | `NOT_ACCEPTED`; best measured local aggregate, not a deployed default |

The renderer correction is narrowly scoped: Persian bidi layout had reversed the hyphen-separated ISO date components in the raster fixture while leaving corpus ground truth unchanged. The current renderer isolates only ISO dates with invisible U+200E marks; a test verifies that the expected string itself is unchanged. The broader LTR-isolation trial was measured, failed, and is retained as evidence rather than treated as an improvement.

At 150 DPI with the current renderer, the combined layout strata for `compact`, `single_column`, and `two_column` pass the thresholds, while `table` critical exact is **0.489583** and fails. The Persian language stratum fails all three requirements (CER **0.207663**, WER **0.275510**, critical exact **0.640625**); all scan-condition strata also fail the 0.90 critical floor. Thus the better aggregate is not acceptance and no production DPI change is made.

A targeted exploratory 32-Persian-case PSM 4 / PSM 11 probe at 150 DPI was materially worse than the PSM 6 current path (critical exact **0.026042** for each); it did not evaluate the English half and is not a full quality gate. A line-selection scratch experiment was not accepted as a runtime change because it traded CER against WER without establishing threshold compliance.

The alternate `tessdata_best` file used in the comparison came from `tesseract-ocr/tessdata_best` commit `e12c65a915945e4c28e237a9b52bc4a8f39a0cec`, with Persian model SHA-256 `99e420969b5ddd2cb135b416316a7ed417c59c4faf9e0d28941348f6448114df`. It was not vendored. One initial diagnostic invocation used a temporary `TESSDATA_PREFIX` lacking Tesseract's `configs/` directory and resulted in 64 incomplete cases; that setup was invalid and is excluded from model comparison. The follow-up copied the required local configs, verified the TSV protocol, and produced the complete comparison row above.

## Verification status

- Local OCR runtime/annotation/synthetic-harness tests: **17 passed** before the final renderer-policy addition; focused renderer/report-verifier tests subsequently **3 passed**, then **2 passed** after date-only scoping. A full fresh OCR test-file rerun is still required before phase closure.
- CP135 local reports deliberately exit as failed (`2`) when the unchanged quality criteria fail. Reports are preserved for every full valid run. The verifier still rejects a failed report; no failing result was converted to success.
- The failure annotation helper was exercised on the 64-case local report: 9 bounded notices (summary plus all case IDs), model hash present, no transcript. Its unit test checks the no-transcript and bound properties.
- `ruff` and Python compile checks passed for the affected OCR/annotation source and tests after corrections. The full backend and frontend matrix has not yet run on this modified SHA.
- No local Docker/Compose build or deployment-image OCR result is available. CI after push must determine whether the vendored model path and OCR report work in the exact deployment image. Even a passing software gate would not establish field/vendor OCR accuracy or engineering/customer acceptance.

## Scope boundary

These measurements remain synthetic development evidence. Persian OCR is improved in this local synthetic setup relative to the pre-render baseline, but remains `NOT_ACCEPTED`; CP135 thresholds remain unchanged. No confidential document was sent to a cloud provider, no text was included in CI annotations, and no operational/field validation is claimed.
