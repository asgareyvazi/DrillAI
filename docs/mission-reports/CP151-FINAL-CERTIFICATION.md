# CP151 — Final software matrix and exact-SHA certification

**Date:** 2026-10-20

**Branch:** `arena/9d8f5568-drillai`

**Scope:** CP133–CP151 repository work; this report records local preflight evidence and points to the branch's exact-commit Actions checks for the controlling CI result.

## Status and evidence boundary

The implementation and local test matrix were advanced, but local results do **not** establish a complete exact-SHA Linux/native-Windows certification. CP135 remains **not accepted** by the measured local adapter-v7 benchmark: it completed all 64 synthetic cases but failed the critical-field threshold and multiple strata. No threshold, case, skip, retry, or verifier gate was relaxed. The authoritative CI outcome is the run whose checked-out SHA matches the final branch commit; consult the [CI runs for this branch](https://github.com/asgareyvazi/DrillAI/actions/workflows/ci.yml?query=branch%3Aarena%2F9d8f5568-drillai) and verify the SHA printed by both jobs.

All OCR/CP150 fixtures are generated and synthetic. Nothing here is field validation, customer acceptance, external vendor/provider acceptance, production recovery evidence, an independent penetration test, engineering qualification, or operational readiness. Keep `well_control.kill_sheet` and `tubulars.api_5c3` at `NEEDS_FIELD_VALIDATION`; no autonomous rig control is authorized or implied.

## CP135 — measured local v7 quality result

The candidate uses the production `parse_bytes` path and a bounded script-based language route: configured `eng+fas` first, then a Persian-only second recognition pass only when the first-pass output is predominantly RTL. The passes share one per-page deadline and concurrency permit; a failed second pass remains a failure. Layout detection may select PSM 11 only after a visible central separator is detected, including a bounded small-skew search. The routing and layout policies are part of runtime/report metadata and are checked by the report verifier.

The fresh local fast-model run used Python **3.11.2**, Tesseract **5.5.2-23-g397887**, PDFium **5.15.2**, 200 DPI, and `tessdata_fast` commit `87416418657359cb625c412a48b6e1d6d41c29bd`. It completed **64/64** cases with **0** incomplete/failed cases, but the benchmark exited **2** with status **failed**:

| Measure | Observed | Gate | Result |
|---|---:|---:|---|
| Global CER | 1,777 / 15,305 = **0.1161058478** | ≤ 0.15 | pass |
| Global WER | 384 / 2,328 = **0.1649484536** | ≤ 0.20 | pass |
| Critical-field exact | 287 / 384 = **0.7473958333** | ≥ 0.90 | **fail** |
| English critical-field exact | 190 / 192 = **0.9895833333** | ≥ 0.90 | pass |
| Persian critical-field exact | 97 / 192 = **0.5052083333** | ≥ 0.90 | **fail** |

All four layout strata and all four scan-condition strata also fail critical-field exactness; the table layout additionally misses WER. Full stratified metrics, case IDs, PDF digests, denominators, timings, and runtime metadata are retained without transcripts in `CP135-LOCAL-ADAPTER-V7-FAST-EVIDENCE.json` (SHA-256 `6ea3acbd3c4a91778ad01fc395dc512c2bd43368dd07e760a0f95f1619c86c69`). Prior v5 deployment-image failure and v6/v7 local comparisons remain separately documented in `CP135-BILINGUAL-INGESTION-OCR-BENCHMARK.md`; historical results are not overwritten or converted into a pass. A missing local Docker executable prevented a local Compose/deployment-image run; the CI deployment-image gate remains mandatory.

## Final-matrix inventory and implementation changes

- The reviewed backend inventory is now **1,780 tests across 138 files**, including **15 PostgreSQL node IDs across 12 files**. Manifest arithmetic is Linux **1,780 passed / 0 skipped**, default SQLite **1,765 passed / 15 PostgreSQL-only skipped**, and native Windows **1,764 passed / 1 specifically identified POSIX-only skip / 15 PostgreSQL deselected**. These are reviewed expectations, not observed native-Windows results. Collection and manifest reconciliation confirmed 1,780 cases and the 15 PostgreSQL IDs.
- The CP16 documentation/count guard now identifies the current manifest as CP151 and reconciles the README's totals with `ci/test-inventory.json`; this changes only the phase label/count contract, not the assertions or acceptance thresholds.
- Standard Playwright Vite servers now disable HMR websockets in E2E mode. This is a mitigation for resource pressure after earlier native-Windows runs reported `ERR_NO_BUFFER_SPACE`; it does not establish the earlier failure's root cause. Test retries remain **0**.
- The CI workflow continues the standard Playwright inventory/execution and isolated CP150 pilot after earlier job failures unless cancelled. Failure-only artifacts now retain Linux/Windows backend logs and the standard Playwright logs/reports; CP135 report and CP150 failure evidence remain failure-only. A successful CP150 pilot still reports its exact counts in the step summary.

## Local preflight evidence (not exact-SHA certification)

| Gate | Observed local result |
|---|---|
| Backend full suite, embedded PostgreSQL enabled | **1,779 passed, 1 failed, 1 warning** across the collected 1,780 tests. The sole failure was the CP16 documentation/count guard comparing the previous 1,779 manifest totals with the updated 1,780 inventory. The README and CP151 marker were corrected forward-only; the focused guard then passed. This is **not** a passing full-backend run; the exact-SHA CI run must execute the full inventory again. |
| Backend focused OCR tests | Ruff passed; **40 passed** across `test_ocr.py`, `test_ocr_runtime.py`, and `test_ocr_synthetic_quality.py`. |
| Backend checks after the documentation correction | Full Ruff and `compileall` passed; the focused CP16 documentation/count guard passed. |
| Frontend unit/component suite | **406 passed across 40 files**, no skips; typecheck and lint passed. |
| Frontend production build | Passed; Vite transformed **302 modules**. Existing non-blocking bundle-size warning remains (main JS chunk about 859 kB uncompressed). |
| Standard Playwright shape | **128 tests / 20 spec files**, Chromium **121**, faults **4**, auth **3**. |
| Standard real-browser Playwright | Fresh isolated run passed **128/128**, **0 skipped/unexpected/flaky**, **0 retries**, across 20 spec files; the execution verifier passed. Data was under `/tmp/.e2e-cp151-standard-aeb167fedf3f42f68ba0f7654e87194c`, not `frontend/.e2e`. An earlier setup attempt used a temporary directory name outside the read-only verifier's `.e2e*` scope and failed its one DB-readback assertion; that failure was retained as a separate failed attempt, then the fresh correctly scoped run passed. It is not hidden or counted as a retry. |
| Isolated CP150 browser/API/DB pilot | Fresh run `cp151-local-577d752cc6f041c9990e07012bef3672`: **1 expected, 1 passed, 0 skipped/unexpected/flaky, 0 retries**. The real browser created one synthetic draft per well and independently read scoped state through product APIs and the read-only SQLite verifier. Report: `frontend/.e2e-cp150-results-cp151-local-577d752cc6f041c9990e07012bef3672/e2e-results.json`; fixture/database artifacts are in the corresponding ignored `frontend/.e2e-cp150-cp151-local-577d752cc6f041c9990e07012bef3672/` directory. No existing `frontend/.e2e` data was changed. |
| Multi-process stack | `STACK_VERIFICATION_OK`: **16/16** scenarios and a **32-cycle** bounded soak. Observed WITSML points accepted **9**, ETP binary **8**, duplicates suppressed **328**, stale writes fenced **1**, DNS permits returned **8/8**, reconciliation failures **0**, maximum poll **70.89 ms**, worker RSS **111.47 → 116.48 MiB**. Local harness evidence only; not production RPO/RTO or vendor acceptance. |
| Fresh SQLite migration | `alembic upgrade head` and `alembic check` passed; no new upgrade operations detected. |
| Supply-chain/secrets scan | Passed: **12** pinned actions, **650** workspace files scanned, `pip_audit` pass, npm all-moderate pass, npm production-high pass. |
| Vendor protocol harness | Local WITSML/ETP harnesses passed. External gate remained `BLOCKED_BY_EXTERNAL_EVIDENCE`; `external_vendor_verified=false`, `network_calls_made=0`. No vendor endpoint was contacted. |

The full preflight backend log recorded the stale count failure; the focused guard, Ruff, and compilation results after correction are separate runs. Do not summarize these as a clean local full-suite pass. Exact-SHA Actions logs and failure artifacts are the retained cross-platform record for the final branch commit.

## CP145 forward-only audit disposition

The CP145 phase date remains its historical **2026-10-20**. The stash object is unavailable in this checkout; no apply, pop, reset, reconstruction, or overwrite was performed. The prior comparison is treated as historical, not newly reproduced. The reported snapshot date `2026-11-15` is conflicting metadata and is not used to assert that an audit occurred on that date. The CP145 report's audit review date remains **2026-10-20**, the authoritative date for this checkpoint; no future-dated review is fabricated. All CP145 corrections remain forward-only.

## Final disposition

Software changes and local checks above do not satisfy CP135's OCR-quality gate. The only local full-backend run remains **1 failed / 1,779 passed**; after correcting the stale documentation assertion, only the focused guard—not the full suite—was rerun. The controlling exact-SHA requirement is the Linux and native-Windows Actions run whose checked-out SHA matches the final branch commit, including the full backend inventory, deployment-image OCR check, standard 128-test E2E suite, and isolated synthetic CP150 pilot. Its summaries and failure-only artifacts are in the linked Actions history; a queued, in-progress, cancelled, or failed required gate is not acceptance. The workflow's exact-SHA results—not inventory declarations, prior runs, or local results—are the evidence for those checks. A complete green CI matrix, if achieved, still would not imply field validation, customer acceptance, vendor acceptance, or operational readiness.
