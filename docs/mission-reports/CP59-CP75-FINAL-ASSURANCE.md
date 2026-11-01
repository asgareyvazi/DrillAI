# CP59–CP75 Final Assurance

**Review date:** 2026-11-01
**Disposition:** **PARTIAL — the implementation and tested release gates pass; live-vendor, field, OCR-quality, LLM-quality, and production-recovery evidence remain unavailable.**
**Repository:** `asgareyvazi/DrillAI`
**Active branch:** `arena/9d8f5568-drillai`
**Mission baseline:** `c1bcf67b62ad6c7faa5e232318a629beaa2bba4a`
**Implementation / exact-code-CI SHA:** `472274a1044cb868a26a09bc8cbc01330601ac94`
**Code-SHA CI:** run [41297713165](https://github.com/asgareyvazi/DrillAI/actions/runs/41297713165) — Linux job `124821982248` and native Windows job `124821982352`, both successful.
**Observed `origin/main`:** `bfa066b28e0071880cb9191a4f1d47fdaa143e04` (untouched).

The companion [CP59–CP75 capability matrix](CP59-CP75-CAPABILITY-MATRIX.md) preserves the phase-by-phase starting assessments, implementation results, and remaining limits. This report records the measured outcome and final boundaries; it is not a field-acceptance or production-readiness certificate.

## Executive assurance decision

The safe software gaps identified during CP59–CP75 were implemented across backend, migration, security, frontend, tests, and operator documentation. The manual operation/event workspace and imported extracted-record review lifecycle now have server-authoritative validation, scoped authorization, audit, version conflict protection, bounded payload behavior, and bilingual UI coverage. A bounded opt-in OCR adapter and repeatable OCR/RAG benchmark harnesses were added; report JSON export preserves the server response bytes. Cross-document survey promotion now fails closed unless the current actual trajectory is anchored to that same document.

The local regression, recovery, frontend, supply-chain, and protocol gates passed. GitHub Actions passed both Linux and native Windows jobs on the implementation SHA above. One Windows-only test is deliberately skipped because it creates a temporary POSIX shebang executable; the Windows gate prints and permits exactly that reason, while separately confirming that the twelve PostgreSQL-marked tests are deselected there. No assertion was loosened and no broad skip/xfail was added.

**Overall disposition remains PARTIAL.** Software tests do not supply missing operator endpoints, approved field data, engineering sign-off, vendor transcripts, production infrastructure, or validated OCR/LLM quality. In particular:

- `well_control.kill_sheet` and `tubulars.api_5c3` remain **`needs_field_validation`**.
- Cross-survey datum, vertical-reference, azimuth-reference, tool, and survey-run compatibility across documents is not established. The implementation blocks the cross-document merge path; source identity alone is not compatibility evidence.
- Required NPT event time may fall back to the extracted record's creation/ingestion time only with an explicit `ingestion_timestamp_fallback` basis/warning. A lone report `period_end` is not treated as event time.
- WITSML/ETP evidence is local harness conformance only (`network_calls_made=0`, `external_vendor_verified=false`).
- OCR contract tests are not a real-corpus accuracy study. No live OCR transcription or LLM answer-quality evidence is claimed.

## Baseline, worktree reconciliation, and publication boundary

The matrix was first created at the CP59 baseline. On continuation, the provided local checkout was shallow at `bfea530602a23bcc08efca85a136240f61c44a0d`, while the fetched active-branch remote was `4596790a3e1412554b5b7a2bd5a6b0076529aa23`. The remote was a strict descendant: merge-base `bfea530...`, `git rev-list --left-right --count` `0 21`. The complete current tree was staged only for comparison against fetched remote blobs; the resulting delta was the CP59–CP75 implementation (53 files, 5,567 insertions, 165 deletions, no deletions). Earlier published CP25–CP58 content matched the remote. The local branch reference was advanced to the fetched remote using an expected-old-value `git update-ref`; no worktree files were overwritten or discarded. The CP51 baseline `c1bcf67...` is an ancestor of the active branch.

Three non-rewriting commits were pushed only to `arena/9d8f5568-drillai`:

1. `aa64c161bf79d31f93a054773a184e2d687bb9d8` — capability implementation and regression coverage.
2. `f521eff051e4e777c47a5b305b731aae61b229d7` — install the OCR test extra on CI and developer bootstrap paths after exact-head CI exposed the missing PDF renderer.
3. `472274a1044cb868a26a09bc8cbc01330601ac94` — make the Windows gate account for its single explicit POSIX-only test skip, requiring the exact reason and exactly twelve PostgreSQL deselections.

The code-SHA CI above is green on both operating systems. This assurance report is a documentation-only follow-up to that tested implementation. Its publication commit is checked separately after push; its exact SHA and check result are provided in the completion handoff rather than self-referenced inside the commit that contains this file.

## Implemented capability and safety changes

### Database, migration, and recovery

- Added `da0881aa121d_version_extracted_record_reviews.py` with durable extracted-record review versions and migration tests.
- Added explicit SQLite fresh-database upgrade and drift verification, in addition to the PostgreSQL-enabled backend suite.
- Re-ran the isolated PostgreSQL 16 logical backup/restore drill through Alembic verification, tenant checks, restored Digital Well Twin snapshot, egress-policy revision, encrypted secret reference, and corrupt-archive rejection.
- Preserved the known rollback caveat: migration `c0f6d8a9b271` consolidates duplicate raw-artifact rows; its downgrade cannot reconstruct the previous row cardinality and associations. Take a verified backup before applying a rollback that needs the old shape.

### Survey, time, depth, and DDR lifecycle

- Cross-document DDR survey promotion is refused when the current actual trajectory is sourced from a different or unattributed document; the record is retained for human review. Same-document idempotent promotion retains per-record provenance.
- Time/depth validation preserves zero as a real depth, requires timezone-aware observed timestamps, distinguishes actual/planned intervals, and does not infer a report event time from a lone period end.
- Manual operation/event entry uses the authorized API, backend-controlled vocabularies, idempotency keys, and server-side validation. The UI converts local operator time to UTC; it does not infer vertical datum or field validity.
- Extracted-record review provides bounded detail (`64 KiB`), correction/validate/reject, an expected-version compare-and-swap, audit metadata/digests, revalidation conflict handling, and explicit null-clearing for canonical time/depth. A still-truncated payload is not editable.
- `well_sections.actual_bottom_md_si` remains the progress source (`3,200.0 m` in the regression fixture), not a general report depth (`2,412.0 m`).

### Ingestion, OCR, engines, operations, and exports

- Added opt-in Tesseract OCR integration with bounded PDF rendering, time/output/page/pixel limits, explicit unavailable/incomplete states, line geometry, and provider confidence. The optional `ocr` extra supplies PDFium/Pillow for rendering; Tesseract binary and language packs remain OS-level and opt-in.
- Added a repeatable OCR benchmark runner requiring a versioned, SHA-pinned approved corpus; no such production corpus was present or run here.
- The authoritative 14-engine inventory remains `backend/src/drillai/engines/registry.py`. The CP19 analytical-vector test independently computes expected values for all 14 registered engines (trajectory, capacity, hydraulics, torque/drag, MSE, kill sheet, tubular ratings, optimization, twin, readiness, integrity and related outputs) and enforces finite JSON. Formula and validation-status semantics were not changed to force test success.
- Added authorized, audited JSON report attachment export, a 20 MiB response ceiling, no-store/nosniff response headers, digest ETag, and frontend download of the validated, unmodified bytes.
- Updated operation, report, OCR, review, recovery, Windows bootstrap, and test-install instructions. PDF/DOCX report rendering remains unimplemented.

### Telemetry, AI/RAG, workflow, and security

- Kept vendor adapters and protocol scopes bounded to the documented WITSML 1.4.1.1 SOAP and ETP 1.2 binary/JSON test profiles; external vendor verification remains false.
- Added a deterministic lexical RAG benchmark with a fixed synthetic dataset version/hash and citation/scope metrics. It does not evaluate semantic relevance, embeddings/hybrid ranking, answer quality, or a live LLM.
- Reviewed workflow action-level authorization and mutating API routes. Targeted auth, secret, tenant-boundary, workflow, and deployment-security tests passed; authenticated and fault-injection browser projects also passed.
- Hardened the workspace supply-chain gate to scan tracked, untracked, and eligible ignored text files, and to run both a full npm moderate-severity audit and the production high-severity audit. An npm override for `minimatch` 11.0.3 removed the initial development-tree moderate advisories; post-install full audit reported zero vulnerabilities.

## Measured verification results

All results below are measured on the CP59–CP75 implementation source. The exact code SHA and cross-platform CI jobs are recorded above.

| Area / command | Result | Qualification |
|---|---|---|
| Backend default: `env -u DRILLAI_TEST_POSTGRES .venv/bin/python -m pytest -o addopts='' -q` | **1,543 passed, 12 skipped**, 353.75 s; 106 test files | The 12 are PostgreSQL-marked; they are not counted as passes. |
| Backend PostgreSQL: `DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest -o addopts='' -q` | **1,555 passed, 0 skipped**, 377.98 s; 106 files | Embedded PostgreSQL; one benign `XDG_RUNTIME_DIR` fallback warning. |
| Windows backend in exact-SHA CI | **1,542 passed, 1 skipped, 12 deselected** | The single skip is `test_tesseract_cli_adapter_uses_stdin_and_bounds_child_output`, explicitly marked POSIX-only; CI prints and asserts that exact reason. The twelve deselections are PostgreSQL tests. |
| Backend Ruff / compileall | Passed | Full backend `ruff check .` and `compileall -q src tests`; the modified root supply-chain script also passed Ruff. |
| CP16 audit and documentation consistency | **10 passed** after updating measured totals | Exact README backend/frontend totals remain guarded by tests. |
| Auth, secret, tenant, workflow, and deployment-security selection | **67 passed** in 27.04 s | Targeted existing integration/security tests; no authorization assertions were weakened. |
| Engineering engines | **146 passed** | Includes `test_cp19_02_independent_analytical_benchmarks_for_all_14_engines`, independent formula vectors and strict finite-JSON checks; `kill_sheet` and `api_5c3` statuses remain field-validation-required. |
| OCR/parser/benchmark/RAG focused selection | **24 passed** | Contract and synthetic-harness coverage, not real-corpus quality. |
| Fresh SQLite migration | Passed | Full migration chain to `da0881aa121d`; Alembic reports `No new upgrade operations detected.` |
| PostgreSQL DR drill | **Passed** on PostgreSQL 16 | Custom-format backup 456,948 bytes; exact row counts before/after; zero drift; tenant A/B isolation; twin snapshot, egress revision and encrypted secret reference restored; corrupt backup rejected (exit 1 as expected). |
| Frontend install | `npm ci` passed | 379 packages added / 380 audited; installed audit result: zero vulnerabilities. |
| Frontend audit | Full `npm audit --audit-level=moderate` and production `npm audit --omit=dev --audit-level=high` passed | Zero findings after the `minimatch` override. |
| Frontend typecheck / lint | Passed | `npm run typecheck`; `npm run lint`. |
| Frontend unit/component | **356 passed in 35 files** | No test failure. |
| Frontend production build | Passed | Vite emitted a non-fatal >500 kB chunk warning. |
| E2E inventory | **121 tests in 17 specs** | Project shape: Chromium 114, fault injection 4, authentication 3. |
| Real Playwright E2E | **121 passed in 5.8 minutes**, retries `0` | Real API/database/browser stacks, including auth and fault deployments; process exit 0. |
| Stack / soak | **16/16 scenarios; 32 cycles** | 9 WITSML and 8 ETP binary points accepted; 328 duplicates suppressed; one stale write fenced; DNS permits recovered 8/8; zero reconciliation failures; maximum poll 82.35 ms; worker RSS 107.73→113.62 MB. |
| Offline vendor harness | Passed | WITSML SOAP, ETP 1.2 Binary Avro, and ETP JSON WebSocket harness conformance; `network_calls_made=0`, `external_vendor_verified=false`. |
| Synthetic RAG benchmark | Passed | Dataset `drillai-rag-synthetic-v1`, SHA-256 `bc8cee30df32b503c26cee4f58d1df88b5b3cbb61c6df23f4bc64bef8ab8ffd1`; 5 chunks / 3 queries; citation coverage 1.0, macro precision@k 0.2, macro/micro recall@k 1.0, MRR 1.0, scope violations 0. Lexical-only. |
| Supply-chain / workspace secret gate | `SUPPLY_CHAIN_AND_SECRET_SCAN_OK` | 8 full-SHA Actions pins; **504 eligible tracked/untracked/ignored text files scanned**, including this final report; `pip-audit`, full npm moderate audit, and production npm high audit all passed. |
| Exact-code-SHA GitHub Actions | **Run 41297713165 passed** | SHA `472274a1044cb868a26a09bc8cbc01330601ac94`; Linux job `124821982248` and Windows job `124821982352` both succeeded. Linux Docker Compose deployment smoke gate passed in CI; it was not run locally. |

### CI failure/remediation ledger

- Run `41295428203` on `aa64c161...` failed backend tests on both operating systems because CI/bootstrap installed `[dev]` without the optional PDF renderer; the Windows summary showed five OCR contract tests returning `renderer_unavailable`. No failures were hidden or skipped. The follow-up added `[dev,ocr]` to CI and bootstrap installs.
- Run `41296505011` on `f521eff...` passed all Linux steps. Windows ran the suite (`1,542 passed, 1 skipped, 12 deselected`) but the old workflow gate incorrectly rejected every skip. The sole skip is an explicit OS-bound temporary shebang subprocess test; it is not an OCR quality skip.
- The next commit made the Windows gate require `-rs`, the exact POSIX-only skip reason, and exactly twelve PostgreSQL deselections. Run `41297713165` then passed both Linux and Windows jobs on the exact code SHA. No broad skip/xfail or weakened test was introduced.

## External evidence and residual risk register

| Evidence area | Final limit |
|---|---|
| Commercial telemetry vendors | No endpoint/credentials/transcript; offline harness only, zero live network calls. |
| OCR quality | `tesseract` executable and approved corpus unavailable locally; no live OCR run or CER/WER/accuracy claim. Benchmarks must use an approved SHA-pinned corpus outside Git. |
| AI/LLM quality | Synthetic lexical retrieval only; no live model, semantic-quality, or field-grounding evaluation. `StubRouter` tests are not live LLM evidence; copy-only exact matching is not natural-language summarization. |
| Engineering acceptance | `well_control.kill_sheet` and `tubulars.api_5c3` stay `needs_field_validation`; formulas and feasibility flags are not field sign-off. |
| Survey compatibility | Cross-document promotion is blocked unless the actual trajectory source document matches; no cross-document datum/azimuth/tool/survey-run compatibility proof exists. |
| NPT chronology | If a source timestamp cannot be established, the required event may use record creation/ingestion time with explicit fallback basis/warning; period-end alone is not event time. |
| Report formats | Authorized machine-readable JSON export only; PDF/DOCX rendering is not implemented. |
| Production recovery | Embedded PostgreSQL logical backup/restore and CI deployment gate pass; physical PITR, cloud failover, production RPO/RTO, and real deployment recovery are not demonstrated. |
| Local containers/tools | Docker, Compose, `psql`, `pg_dump`, `pg_restore`, and Tesseract executables are unavailable locally. No local Compose run is claimed; the Compose smoke gate passed only in Linux CI. |
| UI build | Build succeeds; the production bundle has a non-fatal JavaScript chunk above 500 kB. |
| Migration rollback | The `c0f6d8a9b271` downgrade does not recreate merged duplicate raw-artifact rows; take a verified backup before applying a rollback requiring the old row mapping/cardinality. |

## Release and operating disposition

**Software verification:** PASS within the tested scope on implementation SHA `472274a...`, with exact-head Linux and Windows CI success.
**Field/vendor/production certification:** **NOT ESTABLISHED.** No production deployment, field validation, approved OCR corpus, live LLM quality run, commercial vendor call, or alert-provider receipt is claimed.

The final release decision must retain all residuals above, preserve the documented backup-before-rollback procedure, and require owner-supplied vendor, approved-data, engineering, and field evidence before upgrading any blocked capability. The branch remains `arena/9d8f5568-drillai`; `main` was not changed or merged.
