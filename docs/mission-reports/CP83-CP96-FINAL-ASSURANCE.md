# CP83–CP96 Final Assurance Report

**Repository:** `asgareyvazi/DrillAI`
**Authorized branch:** `arena/9d8f5568-drillai`
**Report date:** 2026-11-04
**Scope:** CP83.1–CP96, in mission order
**Disposition at initial report preparation:** Repository-solvable implementation and local assurance gates pass. Publication closure additionally requires a push to the authorized branch and green Linux and native-Windows CI on the same exact SHA; the exact final-SHA result is returned in the delivery response.

## 1. Executive disposition

The repository-feasible work assigned for CP83.1–CP96 is implemented and tested locally. The current inventory contains 1,657 backend tests, including 14 explicitly reviewed PostgreSQL cases. Backend default and PostgreSQL-enabled suites, frontend unit/build/type/E2E gates, source lint/compile, migrations, recovery drills, the offline vendor harness, synthetic RAG benchmark, and supply-chain/secret checks pass as recorded below.

This is a software assurance report, **not** a field, engineering, vendor, production-readiness, or end-recipient-delivery certificate. The publication gate is to push this report-containing commit only to `arena/9d8f5568-drillai`, then verify green Linux and native-Windows jobs against the identical final branch SHA. Do not count either historical CP83.1 success or CP83.2 failure as verification of that SHA. The final SHA and workflow run/job IDs are returned in the delivery response rather than written back into this report; editing the report after CI would create a different, unverified SHA.

### Mission disposition

| Mission | Repository-solvable outcome | Evidence boundary |
|---|---|---|
| CP83.1–83.2 | Survey azimuth/reference identity, raw/correction provenance, conservative legacy datum and station-time treatment, timezone-aware measurement provenance, unsafe trajectory replay/append rejection, and migration coverage are implemented. | CP83.1 Actions run `41457013404` passed on historical SHA `e75d8c1ce92dd1adb2bc07c5a10d17303a7eb0b1`; CP83.2 run `41587153712` failed backend steps on historical SHA `658c4f094f5980aacb9a36274fc42a3980157dac`. Its signed logs were unavailable, so no specific failing test is attributed. Neither run certifies this final worktree. No survey-reference compatibility, datum, north-reference or magnetic-azimuth conversion is inferred. |
| CP84 | Event occurrence/end time, depth/value basis and unknown states are preserved across services, APIs, run monitor, context and reports. Missing `occurred_at` stays unknown; creation/ingestion/audit time and lifecycle closure do not become event occurrence/end time. | Software tests only; no field timestamp or measurement-accuracy claim. Migration test targets remain `c79f2a61d5b3` for the specified daily-report migrations and `b83e4a6d1c29` for the trajectory-frame prior-only test. |
| CP85–86 | All 14 canonical engines receive formula/unit/boundary regression coverage and explicit limitations; misleading SCR/LOT/FIT verification claims are removed. | `well_control.kill_sheet` and `tubulars.api_5c3` remain `needs_field_validation`; no qualified engineering, manufacturer, signed field-record or operating-envelope evidence was supplied. |
| CP87–88 | RAG scope, citation and embedding identity/freshness guards are implemented; provider selection, privacy ceilings and unavailable states fail closed rather than silently substituting stubs. | The benchmark is synthetic lexical retrieval only. No live LLM/embedding provider, authorized credential, production corpus or human answer-quality review was available. |
| CP89 | Local WITSML 1.4.1.1 SOAP and ETP 1.2 binary conformance harness passes; operator commissioning guidance and diagnostics distinguish local harness from vendor verification. | External mode was deliberately run with endpoints unset: blocked, `external_vendor_verified=false`, zero network calls. No vendor transcript or commercial peer acceptance exists. |
| CP90 | Durable outbox claims, worker dispatch, retry/dead-letter/reconciliation, tenant-scoped audited manual requeue, acceptance evidence, bilingual operator view, generic error persistence and legacy-attribute sanitization are implemented. Inline dispatch compatibility APIs raise before side effects. | A PostgreSQL two-session claim test verifies `FOR UPDATE SKIP LOCKED`; local HTTP/SMTP tests are harness traffic only. No external-provider verification or recipient delivery receipt exists. |
| CP91 | Workflow/alert updates and approval, resume, and rejection-cancellation paths use tenant-scoped conditional writes/CAS. A lost rejection-cancellation CAS returns 409 and rolls back the approval decision. | Regression and full-suite evidence only; no qualified human-workflow acceptance or production recovery claim. |
| CP92 | KPI lineage/freshness and the NPT formula/scope are explicit. NPT percent is selected NPT hours divided by recorded productive plus selected NPT hours in the same well/section/operation/time scope. Overlapping rows contribute full recorded hours without proration; partial-window allocation requires interval detail. | Recorded zero NPT is not treated as zero physical downtime. Operator/field definitions and production thresholds remain unapproved. |
| CP93–94 | Tenant/authz/security regression, secret scanning, clean SQLite migration check, isolated PostgreSQL DR restore, multi-process stack recovery and bounded soak pass. | No independent penetration test, local Compose build/smoke, cloud failover, PITR, measured production RPO/RTO or production deployment was available. The report-containing Linux CI Compose smoke is reported separately at handoff and is not production deployment evidence. |
| CP95 | Current frontend unit/type/lint/build/E2E shape and actual Linux Chromium journeys pass, including Persian RTL, technical LTR identifiers, authorization, fault handling and accessibility journeys. | Synthetic/local browser evidence only; no Persian/English user study or user acceptance. |
| CP96 | Test inventory, backend/frontend/recovery/supply-chain gates and the final assurance report are prepared for publication. | The post-commit GitHub Actions gate must show green Linux and native-Windows results on the same final `head_sha`; its final SHA and run/job IDs are recorded in the delivery response, not self-included here. |

## 2. Test accounting and local software gates

### Backend

The full backend suite was run after the final backend implementation edits, including the rejected-approval CAS guard, sanitized notification error-code persistence, and the disabled inline-dispatch compatibility APIs.

| Run | Passed | Failed/errors | Skipped | Deselected | Xfailed/xpassed | Total | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| Default SQLite mode | 1,643 | 0 | 14 | 0 | 0 / 0 | 1,657 | All 14 skips are reviewed PostgreSQL-marked integration tests. |
| PostgreSQL-enabled mode | 1,657 | 0 | 0 | 0 | 0 / 0 | 1,657 | Embedded PostgreSQL; one non-failing `XDG_RUNTIME_DIR` warning. |
| Native Windows expectation | 1,642 | 0 | 1 | 14 deselected | 0 / 0 | 1,657 accounted | One exact POSIX-only Tesseract temporary-executable skip; PostgreSQL cases are deselected, not passes. This is the inventory expectation, not a substitute for final-SHA native-Windows Actions evidence reported at handoff. |

`ci/test-inventory.json` and `scripts/verify_backend_test_inventory.py` agree on 1,657 collected tests, 14 reviewed PostgreSQL node IDs across 11 files, and the accounting above. The CP90/CP92 focused selection reported **62 passed, 2 PostgreSQL skips** in default mode. The notification-worker file has **6 passes and 1 PostgreSQL skip** by default and **7 passes** with PostgreSQL. Its `SessionScope` test reuses one supplied `AsyncSession` and establishes dispatch behavior only; a separate observer-session test establishes committed-claim visibility/recovery, and the PostgreSQL two-session row-lock test specifically exercises competing claims. These results are not overstated as separate-worker transport delivery.

The rejected-approval regression injects a zero-row run-cancellation CAS and verifies HTTP 409, the run remains waiting, and the approval remains pending after rollback. Notification compatibility regressions verify that `dispatch_pending_notifications`, `dispatch_outbound_message`, and `NotificationDispatcher.dispatch_message` raise before changing queued status/attempt counts. Persisted/public notification errors use allowlisted codes and generic descriptions; raw SMTP/HTTP errors, response bodies, recipient values and secret-reference detail are not persisted. Platform summaries do not treat legacy attributes as proof of provider verification.

### Frontend

- `npm ci`: **379 packages installed**, audit **0 vulnerabilities**.
- `npm run typecheck`: passed.
- `npm run lint`: passed.
- `npm test`: **375 passed across 37 files**, no failed/skipped tests.
- `npm run build`: passed. Vite retains a non-failing main JavaScript chunk advisory: **714.76 kB minified / 177.01 kB gzip**.
- `npm run e2e:verify-shape`: **123 tests / 18 reviewed spec files**, Chromium 116, fault-injection 4, auth 3.
- `npm run e2e`: **123 passed in 5.0 minutes**, 0 failures, 0 skips, 0 retries. Deliberate fault-injection traces and transient WebSocket proxy `ECONNRESET`/`EPIPE` logs occurred during the run; the actual browser suite passed.

### Code, migration, security and analytical checks

- Full backend Ruff check, `compileall`, and `git diff --check`: passed.
- Fresh temporary SQLite `alembic upgrade head` and `alembic check`: passed; no new upgrade operations at revision `e90f1c8a2b64`.
- Supply-chain/secret scan including the final report and post-edit workspace: 8 full-SHA-pinned GitHub Actions; **543 workspace text files** scanned; `pip-audit`, all-dependency npm audit and production npm audit passed with no findings. The final scan was rerun after all report and matrix edits, immediately before staging/publication.
- `scripts/verify_stack.py`: **16/16** multi-process scenarios and a **32-cycle** bounded soak passed. WITSML accepted 9 points; ETP accepted 8; 328 duplicates were suppressed; one stale fenced write was rejected; DNS permits recovered to 8/8; reconciliation failures 0; maximum poll duration 78.47 ms; worker RSS 110.73→115.58 MB. These are synthetic local measurements.
- `scripts/verify_postgres_dr_drill.py`: PostgreSQL 16 custom-format backup/restore passed. Archive **538,102 bytes**, SHA-256 `f2c808b046b11ccc0fe7005e3a8f5bf410d5d1ae6e6257ba3647dacd3b661802`; Alembic revision `e90f1c8a2b64`, zero drift, exact pre/post table-count parity, tenant-isolation/readiness/secret-ref checks, and corrupt-archive detection passed. This is an isolated software drill, not production recovery.
- Synthetic RAG benchmark `drillai-rag-synthetic-v1`: 5 chunks, 3 queries; Recall@5 1.0, MRR 1.0, citation coverage 1.0, scope violations 0, macro precision@5 0.20. Dataset SHA-256 `bc8cee30df32b503c26cee4f58d1df88b5b3cbb61c6df23f4bc64bef8ab8ffd1`. The benchmark is lexical-only and explicitly not production-corpus, semantic, engineering or answer-quality evidence.
- Vendor harness: WITSML 1.4.1.1 SOAP and ETP 1.2 binary tests passed; `network_calls_made=0`. External mode with endpoints unset returned `BLOCKED_BY_EXTERNAL_EVIDENCE`, `external_vendor_verified=false`, and `network_calls_made=0`.

## 3. External and field evidence limits

The following are **not verified** and are not claimed as complete by software tests:

- OCR availability/quality: Tesseract is absent; the required-runtime diagnostic reported `runtime_ready=false` and exited 1. PDFium 5.14.0 is present, but configured `eng`/`fas` Tesseract packs are not. No approved real corpus or gold labels were supplied; no OCR accuracy claim is made.
- Vendor interoperability: no operator-owned external WITSML/ETP endpoint or credentials were configured; no external request was made.
- Live LLM or embedding quality: no provider/credential was configured; no live request or quality judgement was made.
- Notification delivery: local fake SMTP/HTTP confirms adapter behavior only. Server/provider acceptance is not a recipient delivery receipt; provider verification and `delivery_confirmed` remain false absent real evidence.
- Local production-operation tools: Docker, Podman, `pg_dump`, `pg_restore`, and `psql` executables are absent in this workspace. Consequently local Docker Compose build/smoke, cloud failover, PITR, deployment acceptance, production load and measured RPO/RTO were not performed. The Linux CI Docker Compose smoke gate is a separate CI check, not production deployment or recovery evidence; its exact final-SHA result is reported at handoff.
- Engineering and field acceptance: no qualified engineer sign-off, vendor/manufacturer package, signed field records, field trial, user acceptance, or independent penetration test was supplied. `well_control.kill_sheet` and `tubulars.api_5c3` remain `needs_field_validation`.

## 4. Historical CI and publication gate

- CP83.1 run [41457013404](https://github.com/asgareyvazi/DrillAI/actions/runs/41457013404) succeeded on historical SHA `e75d8c1ce92dd1adb2bc07c5a10d17303a7eb0b1` (Linux job `125316148652`, native Windows job `125316148199`). It certifies only that historical SHA, not the current report-containing commit.
- CP83.2 run [41587153712](https://github.com/asgareyvazi/DrillAI/actions/runs/41587153712) failed backend test steps on historical SHA `658c4f094f5980aacb9a36274fc42a3980157dac` (Linux job `125755218824`, native Windows job `125755219238`). Frontend and supply-chain steps passed. Annotations report pytest exit 1, but detailed Actions logs could not be retrieved (EOF); the cause is therefore unknown.
- At the initial publication-preparation baseline, the authorized branch was `arena/9d8f5568-drillai`; local and remote were aligned at `da125cf3ab4d41fc77b237fca1b407ac3da0de91` before the report/CP90/CP91 changes. `origin/main` was `bfa066b28e0071880cb9191a4f1d47fdaa143e04`. This is historical baseline context; the final report-containing SHA and its CI evidence are returned in the delivery response to avoid amending the tested report.
- Final release closure requires: clean worktree; local and `origin/arena/9d8f5568-drillai` exact SHA equality; unchanged `origin/main`; successful push only to the authorized branch; and green Linux plus native-Windows CI on that same final SHA. The workflow result is recorded in GitHub Actions and the delivery response, not by amending this self-containing report after it is tested.

## 5. Source-of-truth companion record

The full CP76–CP96 execution matrix, per-mission source evidence and residuals are in [`CP76-CP96-CAPABILITY-MATRIX.md`](CP76-CP96-CAPABILITY-MATRIX.md). This assurance report summarizes local verification; it does not elevate synthetic tests, old CI runs, local vendor harnesses, OCR diagnostics or database drills into external acceptance evidence.
