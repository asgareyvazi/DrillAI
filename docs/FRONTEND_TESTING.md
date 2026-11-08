# Frontend testing

Four layers, each with one job. The layer that decides acceptance is the end-to-end one: it runs the
real backend, a real SQLite database seeded by the backend's own seeder, and a real browser. Mocking
is confined to unit and component tests, where it isolates a boundary rather than a feature.

## Commands

| Layer | Command | What it proves |
| --- | --- | --- |
| Types | `cd frontend && npm run typecheck` | strict TypeScript with `noUncheckedIndexedAccess` enabled in the app, E2E, and Node-config projects; includes tests and E2E specs |
| Lint | `cd frontend && npm run lint` | no unused code, no `any` slipping in, React rules |
| Build | `cd frontend && npm run build` | `tsc -b` plus a production Vite build |
| Unit + component | `cd frontend && npm test` | Current local suite: **400 passed in 40 files**, including English/Persian survey forms, wellbore/section-scoped document-upload coverage, and the safe Playwright failure-location diagnostic regression. CP129's original localization run was 398/39; CP126's 391/39 and CP115's 390/39 results are historical. |
| End-to-end | `cd frontend && npm run e2e` | Reviewed shape is 127 tests / 20 files (chromium 120, faults 4, auth 3), including the survey lifecycle/import journeys and CP131 synthetic-well pilot. The dedicated `npm run e2e:cp131` runs only the pilot in unique `.e2e-cp131-<runId>` and `.e2e-cp131-results-<runId>` directories; it refuses an existing path, never resets or reuses one, and does not touch `.e2e`. Latest isolated pilot run `20261020-cp131-6df31b08`: 1 passed, zero retries (22.8 s for the test; 32.6 s runner invocation), with database and output retained in the matching run directories. The complete isolated three-project local suite `full5` passed 127/127 with zero retries (Chromium 120, faults 4, auth 3; 20.4 minutes); `scripts/verify-e2e-run.mjs` accepted the console log and structured report. It used fresh `.e2e-cp132-*` deployment/results directories; the standard `.e2e` path was absent and untouched. Exact-SHA run `42481107515` on `286945c9d627ba58a4f765b3f28cc65a5e0003ae` passed both Linux and native Windows, with 127 E2E passes and zero retries on each. CP105 run `41834333302` was canceled; CP106 run `41836351234` failed both E2E jobs on a strict-mode locator. CP107 corrects the locator and adds wellbore filtering. Its original SHA `fce7f953921856280f3f5f1bbcfcebd10b659618` failed Windows SQLite tests with unavailable details; follow-up exact-SHA run `41845191298` passed both Linux and native-Windows jobs, including E2E, on diagnostics-only SHA `4deb23d351f0ef7b3d6400acd2152591f53ac334`. This does not retroactively certify `fce7...`; the CP132 Actions run for the final SHA is the cross-platform certification record. Do not run locally against existing `.e2e` data. |
| Backend | `cd backend && DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest` | The CP130 base inventory contained 1,734 tests across 132 files (14 more than CP129). CP131's added Playwright spec activates six data-driven CP32/CP33 backend contract cases (three per existing guard module), yielding the current reviewed inventory of **1,740 tests across 132 files**. Full local runs on the reconciled tree passed PostgreSQL-enabled **1,740 passed, 0 skipped** and default SQLite **1,725 passed, 15 PostgreSQL-only skips**; both inventory verifiers passed. The Windows manifest split was observed in exact-SHA run `42481107515`: **1,724 passed, 1 identified POSIX-only skip, 15 PostgreSQL deselections**; the notification-worker selections passed 7 tests with 1 PostgreSQL-marked skip for CP124, 8 tests with 1 PostgreSQL-marked skip for CP125, and 18 tests with 1 PostgreSQL-marked skip for the CP127 notification/egress selection. CP127 adds five SMTP egress and socket-pinning regressions. CP126's full frontend suite passed 391 tests across 39 files, with typecheck, lint and Vite production build passing (the build retains the existing large-chunk warning). CP117's focused synthetic DDR corpus/OCR mechanics selection passed 15 tests; the actual OCR benchmark is blocked because Tesseract is not installed, so no transcription-quality result is claimed. Exact-SHA run `42381196438` for inventory-reviewed SHA `f65c8a9` passed Linux's full workflow and Windows backend/inventory gates, but Windows Playwright E2E failed with an unidentified generic assertion; that SHA's cross-platform result remained incomplete. Exact-SHA run `42451684890` later passed on `c1716b2` (see the CP129 closure below). Earlier CP116–CP129 failures include CP126's exact-SHA Linux backend documentation-count guard (the native-Windows causes remain unknown), CP127's run was canceled, CP128's exact-SHA run failed with two native-Windows backend failures plus a Linux E2E assertion whose cause remains unknown, and the initial six CP129 exact-SHA runs failed overall: the first two exposed Linux E2E assertions now corrected; source SHA `6af3f03` and documentation-only SHAs `995b592`, `189faad`, and `ec725f3` passed Linux including all 126 browser tests but Windows SQLite reported 2, 3, 2, and 2 failures, respectively (latest: 1,701 passed, 1 skipped, 14 deselected). Windows artifacts were inaccessible and causes remain unknown; the CP121 Advisor/API/state-freshness/telemetry-latest selection passed 64 tests (33 in the Advisor/API subset), the CP122 connector/protocol/SSRF selection passed 26 tests, the CP123 connector-worker fencing selection passed 7 tests with two PostgreSQL-marked tests skipped by the default run; the PostgreSQL-enabled selection passed 9 tests, and the CP118 RAG/Advisor selection passed 70 tests. CP119's frontend typecheck/lint plus 11 tests across its three targeted files passed; CP121 rechecked frontend typecheck/lint and passed 20 tests across four engineering workspace files, and CP122 rechecked frontend typecheck/lint plus the 7 ConnectorsPage tests. A full PostgreSQL-enabled run exposed four issues: two historical migration regressions called Alembic drift-check before advancing the new additive migration, one survey extraction expectation omitted newly retained source-unit metadata, and the CP16 maintained-count guard still described the prior inventory. Those tests/docs were updated without changing their historical migration target or weakening their assertions. A full CP115 run first exposed the CP32 contract test's stale literal Playwright output path after E2E artifacts were made isolated/configurable (1,699 passed, one failed); the contract now checks both the configurable JSON report path and matching output directory. On the source tree committed as CP115 SHA `160bcc0b10d1cfbf139c79038cf3a0034f44ce64`, the corrected full PostgreSQL-enabled suite passed **1,700 tests, 0 skipped** in 382.59 seconds (one `RuntimeDirWarning` and two `record_property`/JUnit warnings); its inventory verifier passed. The default SQLite suite passed **1,686 tests, 14 PostgreSQL-only skips** in 335.60 seconds (two `record_property`/JUnit warnings); its inventory verifier passed. CP116 adds four formula/QC tests; the current focused trajectory module passed 23 tests and the combined trajectory/API/unit-contract run passed 44. The CP116 report did not claim a full-suite rerun at that checkpoint; the later integrated full-suite and inventory verification are recorded in the CP132 closure above. These are CP116 local results and, at the time, did not certify an exact final SHA on both platforms. Candidate SHA `43d6231e6b2c5929632a6d41e851bc0a2b23606c` passed 1,680/0 with PostgreSQL and 1,666/14 by default; that evidence remains historical. The later integrated SHA `c1716b2e9a86a97a594876d38263510ff0520c7a` passed exact-SHA Linux/native-Windows CI in run `42451684890` (CP129 closure below). For CP132, exact-SHA run `42481107515` certified integrated SHA `286945c9d627ba58a4f765b3f28cc65a5e0003ae`; Linux passed 1,740/0 skipped and Windows passed 1,724 with the one manifest-identified POSIX skip and 15 PostgreSQL deselections. Diagnostic-only follow-up SHA `845665416cdc92826b9c4b207c8320d672dcdc36` passed Linux/native-Windows exact-SHA CI including browser gates (run `41855798854`); original CP108 SHA's Windows SQLite failure in run `41852513867` remains unexplained. |
| Backend lint | `cd backend && .venv/bin/python -m ruff check .` | backend style and import hygiene |
| Migrations | `cd backend && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic check` | no model/migration drift — against a **fresh** database |
| Supply chain + secrets | `backend/.venv/bin/python scripts/verify_supply_chain_and_secrets.py` | full-SHA Actions pins, tracked/untracked/eligible ignored text scan, `pip-audit`, full frontend `npm audit` at moderate, and production audit at high; latest local run passed (9 pinned Actions, 616 workspace files scanned, `pip-audit`, and both npm audits). The final branch commit's exact-SHA Actions run is the certification record. |

## Current integrated release closure — CP130–CP132

Exact-SHA Actions run [`42481107515`](https://github.com/asgareyvazi/DrillAI/actions/runs/42481107515) tested `286945c9d627ba58a4f765b3f28cc65a5e0003ae`; Linux job `128733009790` and native-Windows job `128733009555` both concluded `success`. Linux inventory: 1,740 passed/0 skipped; Windows: 1,724 passed/1 identified POSIX-only skip/15 PostgreSQL deselected. Both jobs reported 400 frontend tests and 127 Playwright passes across 20 spec files (Chromium 120, faults 4, auth 3), with zero retries. The reviewed inventory includes the CP131 synthetic-well pilot. Every acceptance gate ran; only failure-only artifact uploads were skipped. The Linux deployment-image smoke emitted 32 synthetic OCR cases, CER 0, WER 0, critical-field exact rate 1.0, dataset SHA `b682f05c5294a30ac361e26a5f37752587a208aa0d08f83a73e2789e3466ea57`. These results certify repository software/browser behavior on this SHA only, not real DDR accuracy, field/engineering qualification, live-provider quality, vendor commissioning, customer acceptance, or production readiness. See [`CP132 final assurance`](mission-reports/CP132-FINAL-RELEASE-ASSURANCE.md) and the CP130/CP131 appendices.

### CP131 isolated browser run

Run `cd frontend && npm run e2e:cp131` to execute only the synthetic-well pilot. By default each invocation generates a new `DRILLAI_CP131_RUN_ID` and uses direct-child paths `.e2e-cp131-<runId>` and `.e2e-cp131-results-<runId>`. Optional path overrides must retain those prefixes and must not already exist; the configuration rejects existing data/output paths before starting the API. There is no reset opt-in: `DRILLAI_CP131_ALLOW_RESET` is intentionally ignored, and prior CP131 directories are preserved. Do not point this pilot at `.e2e`; do not reuse an old run ID. The dedicated API seed creates only the labelled CP131 hierarchy in the new SQLite database and does not run the destructive demo seeder. Latest isolated local run `20261020-cp131-6df31b08` passed **1 test, zero retries** (22.8 seconds for the test, 32.6 seconds for the runner). Its SQLite data and Playwright JSON report are retained in `frontend/.e2e-cp131-20261020-cp131-6df31b08/` and `frontend/.e2e-cp131-results-20261020-cp131-6df31b08/`; no standard `.e2e` data was used or changed. Separately, the complete isolated local suite passed **127/127 with zero retries**; see the End-to-end row above for the run scope and report paths.

### CP126 exact-SHA CI outcome

Actions run [42359789735](https://github.com/asgareyvazi/DrillAI/actions/runs/42359789735) tested CP126 SHA `24fe365b07dcb24a236a909e1e9b22e52460ace0` and **failed**. Frontend typecheck, lint, unit tests, build, supply-chain checks, and fresh SQLite migration checks succeeded. Linux backend reported **1 failed, 1,712 passed**: `tests/test_cp16_adversarial_audit.py::test_cp16_10_documentation_and_contract_consistency_guard` still asserted that the maintained docs contain `390 passed`, while CP126 had updated the visible frontend total to `391 passed`. That stale assertion is corrected in a descendant; the failed SHA remains failed and uncertified.

The native-Windows backend run reported **4 failed, 1,694 passed, 1 skipped, 14 deselected**, but the job exposed no failed node IDs. Its failure artifact (`windows-backend-tests-42359789735-1`, ID `13569192872`) could not be downloaded because its GitHub redirect targeted a storage host outside the permitted egress allowlist. The Windows causes remain **unknown**; later stack and Playwright gates were skipped. Neither the correction to the Linux guard nor a later successful workflow would retroactively certify SHA `24fe...`; CP132's status is determined by its own exact-final-SHA Linux/native-Windows workflow.

### CP127–CP129 exact-SHA CI status

CP127's exact-SHA run [42362056058](https://github.com/asgareyvazi/DrillAI/actions/runs/42362056058) for SHA `5b21fb9197ea3f91f8143f6aa6e1170380cd3080` was canceled when a higher-priority request for this branch arrived; it is not a CI result. CP128's exact-SHA run [42362293307](https://github.com/asgareyvazi/DrillAI/actions/runs/42362293307) for SHA `9d3e7ddd56b4e14ac459517b0ca8beded080dea4` **failed**: Linux passed backend and preceding gates but the real-browser E2E assertion failed generically, while native Windows reported 2 backend failures, 1,701 passed, 1 skipped and 14 deselected. The E2E artifact and Windows backend artifact both returned EOF through the GitHub storage redirect, so neither failure has a diagnosed test/node cause. Neither SHA is certified. See the [CP128 report](mission-reports/CP128-CI-DOCUMENTATION-COUNT-GUARD.md) for artifact IDs and full evidence. CP129's exact-SHA run [42364712075](https://github.com/asgareyvazi/DrillAI/actions/runs/42364712075) for SHA `9eab3bf1bd3e022516b708b2762a0b20ead38516` **failed**: Linux E2E reported a survey-workspace text count of expected 2, received 3; the annotation and `surveys-workspace.spec.ts:235` identify `Human validated` counting the two reviewed records plus CP129's subtitle. Descendant `a84a82b2eeb0204d9750f918a19b2165891962c9` removes the duplicate subtitle text and tightens the bilingual assertion, but its exact-SHA run [42366092717](https://github.com/asgareyvazi/DrillAI/actions/runs/42366092717) also **failed**: Linux E2E reported only a generic `expect(received).toBe(expected)` annotation; a safe isolated local full run found a stale `documents-evidence.spec.ts:250` fixture-identity assertion consistent with that message, now corrected to compare the upload assets with the canonical backend corpus files. The failed SHA `a84...` local suite also had two separate connector UI failures (`connectors-platform.spec.ts:85` and `:1407`), which remain untriaged and are not identified by the generic CI annotation. The Playwright artifact for run `42366092717` and both Windows backend artifacts failed with EOF from the storage redirect, so attribution and Windows causes remain unconfirmed. Windows reported **2 failed, 1,701 passed, 1 skipped, 14 deselected** on the first three CP129 exact-SHA runs; documentation-only SHA `995b592` reported **3 failed, 1,700 passed, 1 skipped, 14 deselected**, and subsequent documentation-only SHAs `189faad` and `ec725f3` each reported **2 failed, 1,701 passed, 1 skipped, 14 deselected**. None provided failed node IDs. See the [CP129 report](mission-reports/CP129-SURVEY-WORKSPACE-BILINGUAL-LOCALIZATION.md) for artifact IDs, focused E2E results and details. Follow-up SHA `6af3f03c436603b29ceb18d9a88fa52f1b461e31` was tested by exact-SHA run [42368395119](https://github.com/asgareyvazi/DrillAI/actions/runs/42368395119): the Linux job passed all gates, including the full 126-test real-browser suite; the Windows backend job failed with **2 failed, 1,701 passed, 1 skipped, 14 deselected** and no failed node summary. Its backend artifact (ID `13572719245`) returned EOF, so the Windows causes remain unknown and all downstream Windows stack/vendor/browser gates were skipped. The fixture identity correction is therefore covered by passing Linux CI; the two local connector failures did not recur there. Documentation-only exact-SHA run [42369638277](https://github.com/asgareyvazi/DrillAI/actions/runs/42369638277) for `995b59226cf28973637fc9aed1c798a87997fe5c` again passed every Linux gate, including the 126-test browser suite, but Windows failed with **3 failed, 1,700 passed, 1 skipped, 14 deselected**; artifact `13573905479` returned EOF and the cause is unknown. Windows downstream stack/vendor/browser gates were skipped. Further documentation-only run [42371005320](https://github.com/asgareyvazi/DrillAI/actions/runs/42371005320) for `189faad011a51cf984de58af436b02e9cfbf1099` again passed every Linux gate, including the 126-test browser suite, but Windows reported **2 failed, 1,701 passed, 1 skipped, 14 deselected**; artifact `13574047184` returned EOF and the cause remains unknown. Windows downstream stack/vendor/browser gates were skipped. Further documentation-only exact-SHA run [42372388961](https://github.com/asgareyvazi/DrillAI/actions/runs/42372388961) for `ec725f348434cf45cd43edab756a98613b153209` again passed every Linux gate, including the 126-test browser suite, but Windows reported **2 failed, 1,701 passed, 1 skipped, 14 deselected**; artifact `13573999842` returned EOF and the cause remains unknown. Windows downstream stack/vendor/browser gates were skipped. Exact-SHA run [42374484214](https://github.com/asgareyvazi/DrillAI/actions/runs/42374484214) for `5338f496aca082d1ea820b26de43768ac2a73ca4` passed all Linux gates but Windows reported **3 failed, 1,700 passed, 1 skipped, 14 deselected**; annotation output had no failed node IDs and artifact `13574664220` returned EOF. Exact-SHA diagnostic run [42376133015](https://github.com/asgareyvazi/DrillAI/actions/runs/42376133015) for `840ec75c5c3ce89c184eb4cb09f340a5da0660f7` passed Linux and surfaced three sanitized Windows test identities (two synthetic-corpus checks and the DDR half-promotion reconciliation test); exception classes, assertion lines and the artifact remain unavailable. The current follow-up pins the hash-governed corpus text to LF and adds numeric-only traceback-line hints to the sanitized annotation. Exact-SHA run `42377917012` for `1e112a3` passed Linux; Windows passed both corpus-integrity checks but failed `test_a_report_promotes_into_traceable_operations` at the assertion comparing the ordered operation-name list (line 99; 1 failed, 1,702 passed, 1 skipped, 14 deselected); actual values are unavailable. The source-order follow-up uses unique `(page_number, payload.row_index)` coordinates and preserves query order for missing or ambiguous coordinates. Exact-SHA run [42379699710](https://github.com/asgareyvazi/DrillAI/actions/runs/42379699710) for `2dbc8465e689356f9dc497ee0293b9ab8809f337` then passed every backend test case on Linux (**1,720 passed**) and Windows (**1,705 passed, 1 skipped, 14 deselected**), but the workflow failed because the reviewed inventory still expected 1,718 tests (Linux 1,718 / Windows 1,703). Run [42381196438](https://github.com/asgareyvazi/DrillAI/actions/runs/42381196438) for inventory-updated SHA `f65c8a979af3023394667e102dc57eda4616d475` passed Linux's full workflow and Windows through inventory; Windows backend tests passed, and only Playwright E2E failed with the generic `expect(received).toBe(expected) // Object.is equality` annotation. The failed browser test, values and cause remain unknown; the evidence artifact and run-log archive returned EOF, and GitHub rejected a failed-job rerun. Diagnostic follow-up `a5fc6df` was tested by exact-SHA run [42449658834](https://github.com/asgareyvazi/DrillAI/actions/runs/42449658834): Linux passed its full workflow; Windows passed every gate through inventory and failed only Playwright E2E. The safe annotation identified `chromium connectors-platform.spec.ts:2212` and `chromium documents-evidence.spec.ts:240` plus a generic failure-oracle assertion; artifact `windows-playwright-evidence-42449658834` returned EOF. A Windows-style `core.autocrlf=true` checkout reproduced CRLF conversion of the two unpinned frontend upload assets, making the documents fixture's byte-equality check fail against LF-pinned canonical files. The current follow-up pins both assets to LF and extends the existing corpus test (no inventory-count change); it also emits only sanitized HTTP/transport finding labels. The connector Journey 41 failure remains historically undiagnosed; no allowance or skip was added. Exact-SHA run `42451684890` on `c1716b2e9a86a97a594876d38263510ff0520c7a` passed the full Linux and native-Windows workflows, including all 126 Playwright tests on both platforms. The earlier Journey 41 HTTP/transport finding did not recur; the successful run does not identify its old root cause. CP129 is `DONE` for cross-platform software/browser certification only. The later integrated CP130–CP132 SHA was certified by exact-SHA run `42481107515`; see the current closure section above.

### CP129 current exact-SHA closure

Run [42451684890](https://github.com/asgareyvazi/DrillAI/actions/runs/42451684890) tested exact SHA `c1716b2e9a86a97a594876d38263510ff0520c7a` on `arena/9d8f5568-drillai`. Linux job `128642063559` and native-Windows job `128642063746` both completed successfully. Both jobs passed frontend typecheck/lint/unit tests/build, supply-chain checks, backend lint/tests, fresh migrations, inventory, stack verification and the full 126-test Playwright suite; Linux also passed Docker Compose smoke, and Windows passed the local protocol harness. Reported totals: Linux backend **1,720 passed**; Windows backend **1,705 passed, 1 POSIX-only skip, 14 PostgreSQL deselections**; both frontends **399 tests / 40 files**; both browsers **126 passed, zero retries**. Failure-evidence uploads were skipped on success as designed; no acceptance gate was skipped.

The exact test at `connectors-platform.spec.ts:2212` is Journey 41 (CP15 egress-policy RBAC and worker-policy synchronization). It creates and polls a local WITSML harness connector, checks that an `org_admin` cannot change the system egress policy, verifies a revision-checked admin update and stale-policy badge, then polls and verifies the worker revision catches up. The earlier HTTP/transport finding was not recovered from run `42449658834`; its cause remains unknown. It did not recur in `42451684890`, which passed the complete suite with zero retries. The later pass does not diagnose or retroactively alter the old failure. The documents fixture EOL correction is separately supported by the `core.autocrlf=true` byte-identity simulation and the successful Windows suite. No failure allowance or skip was introduced. This closes CP129 for software/browser certification only; no field or qualified engineering validation is claimed. The later integrated CP130–CP132 SHA was certified by exact-SHA run `42481107515`; see the current closure section above.

### CP115 survey-workspace browser run

The focused local Linux/Chromium run executed `e2e/surveys-workspace.spec.ts`: **2 passed in 40.7 seconds**. It covered immutable create/correction/revision comparison, chart/table display, CSV export, current unit-review gating, and reviewed-source import through the real API and browser. It used fresh data directories `.e2e-cp115e-main`, `.e2e-cp115e-faults`, `.e2e-cp115e-auth` and output `.e2e-cp115e-results`, with ports 8594–8599; no pre-existing E2E directory was reused or altered. This is focused local evidence, not a full 126-test run, native-Windows run, or exact-final-SHA certification. All generated E2E directories are retained.

The [CP51–CP58 final assurance review](mission-reports/CP51-58-FINAL-ASSURANCE.md) contains that mission's
file-by-file inventory, measured test counts, limitations, and CI/vendor evidence. The current
[CP59–CP75 capability matrix](mission-reports/CP59-CP75-CAPABILITY-MATRIX.md) records the latest
source-backed test results and remaining external/field limits.

`npm run e2e` prepares its own browser (`scripts/run-e2e.mjs`), starts the API and the web server
itself, and seeds the database before the API opens it. `DRILLAI_CHROMIUM_PATH` overrides the browser
binary in environments that provide one.

### PostgreSQL-backed tests

The complete backend suite also exercises PDF rendering and OCR contracts. Install the test/runtime
extras with `pip install -e "backend[dev,ocr]"` (the bootstrap scripts and CI do this); the OCR tests
inject deterministic providers and do not require a Tesseract executable. Tesseract and language data
remain opt-in operating-system dependencies, as described in `CONNECTORS_AND_OPERATIONS.md`.

Fifteen tests across twelve files assert against a real PostgreSQL: two in `tests/db/test_persistence.py`
(JSON and timestamp behavior SQLite cannot prove, and that the Alembic baseline creates exactly the
schema the models declare); one in `tests/db/test_migrations_postgres.py` (the whole migration chain,
drift, downgrade/re-upgrade, duplicate regulator identifiers, and cross-organization/well composite scope constraints); one in
`tests/db/test_cp18_postgres_backup_restore.py` (logical backup/restore and migration recovery drill);
one in `tests/db/test_promotion_identity_migration.py` (PostgreSQL promotion fingerprint uniqueness and tenant scope);
one in `tests/drilling/test_daily_report_domain.py` (composite tenant FKs and typed report-domain checks);
three in `tests/documents/test_document_identity.py` (operation-aware identity migration backfill/downgrade,
concurrent uploads of one logical document, and concurrent uploads into different scopes); one in
`tests/test_cp20_integration_completeness.py` (pgvector retrieval); one in
`tests/telemetry/test_cp15_egress_policy_recovery.py` (separate-worker egress-policy recovery); one in
`tests/telemetry/test_connector_worker.py` (`test_competing_workers_fencing_on_postgres`); one in
`tests/telemetry/test_cp130_independent_connector_workers.py`
(`test_postgres_competing_workers_with_independent_engines_have_one_cas_winner`, which verifies a single CAS winner across independent database engines); one in
`tests/telemetry/test_cp13_witsml_and_worker_races.py` (`test_phase6_postgres_concurrent_sql_cas_races`); and one in
`tests/workflow/test_notification_worker.py` (`test_competing_notification_worker_claims_skip_locked_rows_on_postgres`, which holds the first transaction's row lock while an independent second session attempts a claim).
The migration check exists because a boolean comparison previously failed on PostgreSQL but passed on
SQLite: the dialect that accepts a comparison cannot certify the dialect that rejects it. These tests
are enabled by the repository's embedded PostgreSQL, never replaced with SQLite, and skip unless the
PostgreSQL flag is set:

```bash
cd backend && DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest
```

The CP130 base inventory was 1,734 tests across 132 files; CP131's new E2E spec activates six data-driven CP32/CP33 contract cases, yielding the current reviewed total of **1,740 tests across 132 files**. `ci/test-inventory.json` expects **1,740 passed, 0 skipped** with PostgreSQL enabled, **1,725 passed, 15 PostgreSQL-only skips** by default, and **1,724 passed, 1 identified POSIX-only skip, 15 PostgreSQL deselections** on Windows. The first two splits were observed in complete local runs and accepted by the verifier. The Windows manifest split was confirmed by exact-SHA run `42481107515`: 1,724 passed, one identified POSIX-only skip, 15 PostgreSQL deselected. The historical CP116–CP128 CI record remains unchanged: CP126's exact-SHA backend failure, CP127's canceled run, and CP128's two native-Windows backend failures plus Linux E2E assertion failure are recorded above and in the CP128 mission report. CP115's complete local runs on the source tree committed as `160bcc0b10d1cfbf139c79038cf3a0034f44ce64` passed 1,700/0 skipped with PostgreSQL and 1,686/14 by default; both inventory-verifier checks passed. CP116's focused trajectory module passed 23 tests (the related engine/API/unit-contract selection passed 44); that standalone checkpoint did not claim a full-suite run; the later integrated CP130–CP132 exact-SHA suites passed on both platforms, with actual totals in the closure section above. On historical CP110/CP111 candidate SHA `43d6231e6b2c5929632a6d41e851bc0a2b23606c`, the suite passed 1,680/0 with PostgreSQL and 1,666/14 by default; that inventory verifier result applies only to that earlier SHA. The later diagnostics-only follow-up's CP32 contract test and changed-file checks passed; full suites on that exact commit are governed by its workflow. The first rerun caught only the stale 121-file documentation assertion (1,679 passed); after refreshing README, this guide, the exact-count guard and manifest, both full suites passed. The earlier CP108 local runs were 1,677 passed/0 skipped with embedded PostgreSQL and 1,663 passed/14 PostgreSQL-only skips by default; those counts are historical. Fresh SQLite migration/check, full Ruff, `compileall`, and SQLite/PostgreSQL composite-scope migration assertions passed. At CP105, the PostgreSQL-enabled full suite passed locally at **1,677 passed / 0 skipped**, and the default run passed **1,663 passed / 14 PostgreSQL-only skips**. `scripts/verify_backend_test_inventory.py` verified the CP104 default result. At the CP105 checkpoint, Windows totals were manifest expectations; the later exact-final-SHA CP132 run observed 1,724 passed, one identified POSIX-only skip and 15 PostgreSQL deselections. CP104's Windows result remains separately recorded below. The previous CP103 full PostgreSQL-enabled backend suite passed 1,675 tests with one non-failing `RuntimeDirWarning` because `XDG_RUNTIME_DIR` is unset. CP101 exact-SHA Actions run `41815622464` ran its Linux and Windows pytest cases without test failures but failed inventory verification because the manifest lagged one test (Linux 1,673 passed; Windows 1,658 passed, one POSIX-only skip and 14 PostgreSQL deselections); that SHA remains historically failed. CP102 corrected the manifest. CP102 exact-SHA run `41818116645` succeeded on Linux, including the browser step, but failed the native-Windows backend suite; annotations only say `pytest exited 1`, log retrieval returned EOF, and the downstream Windows browser step was skipped. The cause remains unknown; CP102 is not exact-SHA certified. CP103 exact-SHA Actions run `41822291067` passed Linux job `126548137249` and native-Windows job `126548137556`, including real-browser E2E on both platforms. CP104 exact-SHA Actions run `41829810555` tested commit `1bf18fee72fef883c78bf86d70800ffdb8e4cd3e`; Linux job `126573778395` and native-Windows job `126573779021` passed, including browser E2E. CP105 adds same-date period comparisons; exact-SHA run `41834333302` was canceled after Linux reached E2E and native Windows reached stack verification, so it is not certified. CP106 adds exact report-number search; run `41836351234` passed all preceding gates on both platforms but failed both E2E steps on a strict-mode locator in the CP105 comparison list (`li` resolved to two rows). CP107 narrows that locator and adds wellbore-scoped browser filtering. Its original run `41842149924` at `fce7f953921856280f3f5f1bbcfcebd10b659618` failed the Windows SQLite suite; annotations said only `pytest exited 1`, log retrieval returned EOF, and no backend log artifact existed, so the cause remains unknown and that SHA is not certified. CI commit `4deb23d351f0ef7b3d6400acd2152591f53ac334` added failure-only Windows backend-log retention; follow-up exact-SHA run `41845191298` passed Linux and native Windows, including both E2E gates and the Windows suite. This certifies only `4deb...` and does not retroactively certify `fce7...`. CP108's implementation SHA `b6af66bf8e2b2a916f81c90c4355044971e2c811` failed the Windows backend SQLite step in exact-SHA run `41852513867` (Linux job `126652278250` passed, Windows job `126652277812` failed). The uploaded failure artifact (ID `13335199857`) could not be downloaded from this sandbox because the redirect host is outside the permitted egress allowlist; the original failure/root cause remains unknown. Follow-up diagnostics-only SHA `845665416cdc92826b9c4b207c8320d672dcdc36` passed run `41855798854` on Linux job `126663950354` and native-Windows job `126663949682`, including backend, stack and real-browser gates. This certifies that follow-up SHA, not retroactively the failed `b6af...` run; CP132 cross-platform status is determined by its exact-final-SHA Linux/native-Windows workflow. CP108 local supply-chain scanning is separately blocked by advisories in the preserved `.venv` `pip 23.0.1`; the later integrated exact-SHA supply-chain gate passed on Linux and native Windows in run `42481107515`; the old environment finding remains historical. No local Playwright run was made against existing `.e2e` data; exact-final-SHA browser evidence was supplied by run `42481107515` for the integrated SHA; future changes require a new run. The current 15 default skips are PostgreSQL-marked integrations. Windows uses `-m "not postgres"` to deselect those cases and permits only the separately identified POSIX-only Tesseract CLI subprocess skip; no broad skips are allowed.

## Unit and component tests

- `src/lib/format.test.ts` — the value semantics the product promises: missing is not zero, a
  recorded zero is a value, precision does not depend on the value being a number, and only
  registered units convert.
- `src/lib/permissions.test.ts` — the RBAC *pattern* semantics (`*`, `.*`, `.**`) the interface uses
  to avoid offering an action certain to be refused. It grants nothing: every authorization decision
  is the server's.
- `src/lib/runEvents.test.ts` — frame decoding, cursor handling, refusal close codes, backoff.
- `src/lib/workflowGraph.test.ts` — graph reading and validation.
- `src/api/client.test.ts`, `src/api/queryCancellation.test.ts` — query building, identity headers,
  the error matrix (platform envelope, FastAPI validation, 409 approval-required, malformed body,
  empty 204, transport failure, abort), and that a cancelled request cannot overwrite newer state.
- `src/components/common/common.test.tsx`, `src/components/common/a11y.test.tsx` — the shared data
  states plus the interactive primitives: table rows as controls, the tab pattern (roving tabindex,
  RTL-aware arrows, panel naming), the dialog's focus lifecycle, and the live region.
- `src/components/evidence/EvidencePanel.test.tsx`, `src/components/layout/AppShell.test.tsx` — the
  evidence drawer and the shell's identity/units/locale controls.
- `src/pages/**/*.test.tsx` — the pages rendered from payloads captured off the running API
  (`src/test/fixtures/`). Reading a field the backend does not send fails here.

Fixtures are captured responses for the seeded **synthetic** well and are recaptured, not edited,
when a contract changes on purpose.

## End-to-end suite

`e2e/` runs against the real stack:

- `e2e/start-api.mjs` — seeds the database (`scripts/seed_demo.py --reset`), writes
  `.e2e/fixtures.json`, then starts uvicorn against the seeded file. Seeding after the server starts
  is the bug this ordering avoids: the server would hold a deleted inode and serve an empty database.
- `e2e/global-setup.ts` — fails the run if the API cannot serve the seeded well, so a mis-seeded run
  reports a setup failure instead of asserting against 404s.
- `e2e/fixtures.ts` — the shared `test` and browser-context-wide failure oracle. It records every
  HTTP response with status 400 or higher and every `requestfailed` event across every page in the
  context, binds abort evidence to a unique same-origin fetch request ID, and distinguishes only a
  witnessed caller abort or a successful same-frame navigation supersession as benign. All other
  transport failures must match an explicit allowance. API helpers let a spec compare the UI with the
  backend's own answer instead of with a literal.
- `e2e/failure-oracle.ts` — the Playwright-independent matching contract. Every allowance requires
  an exact method, canonical API path, exact query shape, HTTP status or Chromium error code, reason,
  and positive `maxMatches` bound. Wildcard/substring rules and ambiguous matches fail closed; direct
  mutation tests exercise method/path/query/status/error-code/count and cancellation evidence.
- `e2e/run-e2e.mjs` — resolves a browser, refuses to start when the API or web port is already held
  by a stale server, and runs the suite.

Run one spec:

```bash
cd frontend && npx playwright test e2e/well-cockpit.spec.ts --project=chromium
```

### Three deployments, one product

Playwright starts six servers. The application code is identical in all three deployments; what
differs is the deployment they are pointed at. A CI run that executes only `chromium` certifies a
third of the suite, so all three projects run. `npm run e2e:verify-shape` checks the exact reviewed
spec-file set and counts before execution: **127 tests in 20 files** (`chromium=120`,
`chromium-faults=4`, `chromium-auth=3`). The CI run emits Playwright's structured JSON report; the
post-run verifier requires a zero process exit, a 127-passed CLI summary, zero skipped/flaky/unexpected
results, exactly one passing zero-retry attempt per test, and the reviewed project distribution and
exact spec-file set. The inventory check itself is collection-only and is not reported as executed tests.

| Project | Deployment | What only it can prove |
| --- | --- | --- |
| `chromium` | development identity | the product's own journeys |
| `chromium-faults` | `DRILLAI_E2E_FAULTS=true` + a 3 s client deadline | a real deadline exceeded, a body that is not the contract, an unhandled 500 injected into a request the interface really makes |
| `chromium-auth` | `DRILLAI_E2E_AUTH=true` | that "you are not signed in" is a real state, produced by the real authorization path |

### The journeys

127 test cases in 20 spec files across the three configured deployments. The inventory and project counts are maintained in `ci/test-inventory.json`; the dedicated CP131 command runs only its isolated synthetic-well spec. The list is the coverage; the counts are what the suite reports.

| Spec | Journeys | Covers |
| --- | --- | --- |
| `well-cockpit.spec.ts` | 4 | list → cockpit → NPT → missing data → timeline → reload → unknown well → unknown route |
| `live-operations.spec.ts` | 3 | CP10 live operational vertical slice: synthetic telemetry commissioning → automatic rule evaluation on ingestion → live KPI strip & bounded channel history chart → historical `T1` alert evidence drawer unaffected by later `T2` points → server-governed `allowed_transitions` ack/clear/cancel & real 409 conflict recovery → hysteresis auto-clear → cross-well stream isolation → missing/untrustworthy quality honesty → Persian RTL token isolation |
| `connectors-platform.spec.ts` | 42 | CP11–CP16 telemetry connectors, WITSML 1.4.1.1 SOAP, production ETP 1.2 Binary Avro (`etp.1.2.binary_ws`), & internal-test ETP 1.2 JSON (`etp.1.2.json_ws`), connection-bound SSRF & durable private rig-network CIDR policy (`EGRESS_POLICY_STATES`, redaction, `manifest-policy-revision`, `worker-policy-stale-badge`), 10-stage connection-test milestones, secret minimization & redaction, reconciled run disposition manifests (11-state `POLL_DATA_OUTCOMES`, per-channel `channel_cursors`), governed dry-run (`dry_run_preview`) & live replay, 409 optimistic concurrency, mid-poll lease preemption, RBAC, and Persian RTL technical token isolation |
| `documents-evidence.spec.ts` | 4 | upload → ingestion → extraction record → page/region evidence |
| `daily-reports-workspace.spec.ts` | 3 | import/reconcile extracted rows, compare explicit same-date periods without bridging unknown bounds, and preserve reviewer/correction/withdrawal history |
| `workflow-studio.spec.ts` | 9 | create → configure → connect → validate → save → version → publish, and the draft lifecycle |
| `run-monitor.spec.ts` | 6 | run list, node executions, the approval inbox, a run that fails and says which node failed |
| `run-events.spec.ts` | 5 | the durable run-event WebSocket: live events, cursors, reconnect, REST reconciliation, run switching |
| `error-matrix.spec.ts` | 5 | 409 stale decisions, permission refusals, not-found, and the surfaces that used to report a quiet success |
| `error-faults.spec.ts` | 4 | real timeout, malformed body, unhandled 500, and the recovery each one offers |
| `error-auth.spec.ts` | 3 | real unauthenticated refusals, browser login/session/RBAC/logout, and organization user plus one-time scoped-token management |
| `identity-permissions.spec.ts` | 7 | the role catalogue against backend authority, ceiling vs permission refusals, identity switching without a reload, socket termination on switch |
| `context-deeplinks.spec.ts` | 6 | deep links and reload: run in the address, cross-well document, unknown document/workflow/run |
| `rtl.spec.ts` | 5 | Persian layout, numerals that still equal the API's numbers, mixed-direction identifiers, the operational record in Persian with its identifiers LTR, graph geometry |
| `a11y.spec.ts` | 7 | keyboard-only operation, tab/table/dialog primitives, the live region during a real resumption, focus stability across a refresh, and a nameless-control sweep over every critical screen — the cockpit, engineering, the operational record, the documents behind it, runs, workflows and the platform |
| `checkpoint5.spec.ts` | 3 | the four areas against each other: a run deep-linked in Persian, a refusal in Persian that changes identity in place, a cross-well document in the accessibility tree |
| `operations-workspace.spec.ts` | 5 | the operational record: the operations the API lists, an operation's actual hours and the document it was promoted from (the link really opens it), the transitions the server allows and a terminal record offering none, an event's NPT charge from its own category and hours, and the merged timeline deep-linking to its source document |
| `master-data.spec.ts` | 3 | the asset spine end to end: project → field → well → wellbore → section, then rename, move, transition, sidetrack and activate with every id and the attached document preserved; the refusals (a viewer's 403, a stale write's 409, the lineage rules); and well creation from the interface read back from the server |
| `surveys-workspace.spec.ts` | 2 | create/correct/compare/visualize/export source-backed trajectory revisions; enforce current unit review before importing reviewed source rows |
| `cp131-synthetic-pilot.spec.ts` | 1 | synthetic survey persistence → scoped DDR extraction/promotion → daily-report reconciliation → MSE → real lexical RAG → Advisor computed facts → management-summary evidence and JSON export; no provider, field data or engineering acceptance is claimed |

### What a journey asserts about the product

A journey that says "nothing went wrong" checks both the application's own errors and the complete
browser-context wire record:

1. **The application produced no error of its own** — `appConsoleErrors(consoleErrors)` is empty.
   A React error, an unhandled rejection or a `console.error` from the product fails the journey.
2. **No unprovoked HTTP or transport failure occurred** — the shared fixture observes responses
   with status 400 or higher and `requestfailed` events for every page in the Playwright context.
   Its `auto` teardown repeats the verdict even if a spec omits `expectNoFailures`.
3. **A deliberate refusal is narrowly stated, never silently filtered.** A structured allowance names
   the method, canonical API route, exact query, exact HTTP status or Chromium network code, reason,
   and positive match-count bound. The matcher rejects wildcard/substring targets and overlapping
   allowances. Each observation consumes at most one allowance and excess failures remain unexpected.
4. **Cancellation requires linked evidence.** `net::ERR_ABORTED` alone is unexpected. It is benign
   only when the same fetch request ID reports a caller `AbortError`, or when a newer same-frame
   navigation started after the original request and before its failure and then committed. A
   deliberate timeout uses its own exact request allowance and a `TimeoutError` signal; other network
   errors need an exact scenario allowance.

The browser's own console line for a failed resource (`Failed to load resource: …`) is not used as a
platform-dependent verdict. The application shell cancels background reads on navigation, and
Chromium words these events differently across runners. The fixtures instead retain the response or
request, correlate lifecycle evidence, and attach redacted request-level diagnostics on failure. The
runtime contract and its mutation tests are held by
`backend/tests/security/test_cp32_e2e_console_error_contract.py`,
`backend/tests/security/test_cp33_e2e_response_oracle_coverage.py`, and
`frontend/src/lib/failureOracle.test.ts`.

The journeys are also **compiled** before they run: `frontend/tsconfig.e2e.json` type-checks `e2e/`
in strict mode with `noUncheckedIndexedAccess: true`, and `frontend/tsconfig.json` references it. The
application and Node/Vite project configs enable the same indexed-access rule; the initial exception
in the E2E config and omission from the Node config are now closed. `npm run typecheck` compiles all
three referenced projects, including app tests and E2E specs, and passes.

### What is mocked, and where

Nothing, in this layer. The specs drive the real UI; the UI calls the real API; the API reads a real
SQLite database seeded by the backend's own seeder; the run-event log arrives over the real WebSocket;
the faults are injected by the application itself (`backend/src/drillai/api/routers/faults.py`, mounted
only when `DRILLAI_E2E_FAULTS=true` and refused outright in production). There is no `page.route()`
interception in the suite, no canned JSON standing in for a response, and no fake permission answer.
The synthetic seed fixtures are the exception that proves the rule: they are created *by the
application*, and the browser still talks to the real backend about them.

Two exceptions exist at the socket level, and they are named here because an absolute claim would be
false: `run-events.spec.ts` wraps the stream in a pass-through relay (`routeWebSocket` →
`connectToServer()`) so a journey can drop the transport at a moment it chooses, and
`error-matrix.spec.ts` stalls one screen's stream the same way so that a stale screen stays stale. In
both, the frames the page renders are the ones the real server sent — one relays them, the other
withholds them; neither invents one, and no HTTP response is replaced anywhere in the suite.

### What the suite asserts about itself

Synthesised values are never compared with literals: a spec reads the number from the API and compares
the page with it. Journeys that change shared state clean up after themselves (a started run approves
its own gate, so the approval inbox is as it was found). Where a journey's premise cannot be arranged
from the seed, the journey fails rather than skipping quietly.

## CI

### CP83.1 publication-gate incident and remediation status

The original exact-head run [`41431413781`](https://github.com/asgareyvazi/DrillAI/actions/runs/41431413781) on `a61754dc0c0a23bb76b9ce6b2cbb580ec8d6844e` failed its wrappers, not the two underlying results:

- Linux job `125230994278` completed the real Playwright run and reported **123 passed (5.3m)**, then failed because the wrapper still required 121. Its collection check had validated 123 tests in 18 files (Chromium 116, faults 4, auth 3).
- Windows job `125230993794` ran pytest with exit code 0: **1,607 passed, 1 skipped, 13 deselected**. The skip was the exact POSIX-only OCR CLI fixture described above; all 13 deselections were PostgreSQL-marked tests. The PowerShell wrapper still expected 12 deselections, so Windows stack, vendor-harness, E2E inventory and real Playwright steps did not run in that attempt.

The first correction at `8603207` fixed the stale totals and added shared inventory gates. Exact-head run [`41451881219`](https://github.com/asgareyvazi/DrillAI/actions/runs/41451881219) then passed every Linux gate, including the full actual browser suite. On native Windows, backend, lint, migration, stack, vendor-harness and pre-run E2E inventory passed; the browser command exited 0 and printed **123 passed**, but the wrapper failed because its console parser observed zero project/file records. Therefore that Windows attempt did not verify the runtime 116/4/3 distribution and is not CI-certified.

A local CI-mode Linux real-browser rerun passed **123 tests in 4.9 minutes**. Playwright's JSON report showed `expected=123`, `unexpected=0`, `skipped=0`, `flaky=0`; the structured verifier confirmed all 18 reviewed spec files, project totals 116/4/3 and one passing attempt per test with zero retries. The run also exposed a strict-mode ambiguity in the daily-report list assertion; it was corrected by scoping the same visibility assertion to the report list, and the full browser suite passed afterward. This is local Linux evidence, not native-Windows certification.

The current remediation keeps backend node IDs and platform totals in `ci/test-inventory.json`, adds the exact 18-spec filename set, distinguishes collection from execution, and validates Playwright's exit code, CLI summary, structured JSON outcomes, actual project/file distribution, and zero retries. Exact-head run [`41457013404`](https://github.com/asgareyvazi/DrillAI/actions/runs/41457013404) tested `e75d8c1ce92dd1adb2bc07c5a10d17303a7eb0b` and completed successfully: `gh run view` reported conclusion `success` for Linux job `125316148652` and native Windows job `125316148199`; every required gate step in both jobs was successful. The failure-only Playwright-evidence upload steps were skipped as designed. Attempts to retrieve either job log returned EOF from GitHub's results log archive, so that run's per-test output totals could not be independently recovered; the run is status-verified, but this document does not invent counts from unavailable logs. This run certifies the CP83.1 SHA only; it does not cover the following CP83.2 changes or the final CP76–CP96 SHA.

The CP83.2 exact-head run [`41587153712`](https://github.com/asgareyvazi/DrillAI/actions/runs/41587153712) on `658c4f094f5980aacb9a36274fc42a3980157dac` is verified failed: Linux job `125755218824` and native Windows job `125755219238` both failed their backend test steps; frontend and supply-chain steps passed. Check-run annotations report pytest exit 1, but `gh run view --log-failed` could not retrieve the signed log archive (EOF), so the exact CI failure is not attributed to a particular test. The current complete local runs pass **1,643 tests with 14 PostgreSQL-only skips** by default and **1,657 tests with embedded PostgreSQL enabled**; both account for 1,657 collected tests across 119 files. They are local evidence, not exact-SHA CI evidence. `ci/test-inventory.json` now reviews 1,657 collected tests and 14 exact PostgreSQL node IDs; native Windows expects 1,642 passed, one explicitly POSIX-only skip, and 14 PostgreSQL deselections, to be verified by the final Windows job.

The CP84 browser acceptance initially exposed a stale RTL test selector that depended on the incidental `.font-mono` class rather than the semantic `dir="ltr"` property. The assertion now targets the timeline's explicitly LTR timestamp. The latest local real-browser run on the CP76–CP96 worktree passed **123 tests in 4.8 minutes**; fault-injection scenarios intentionally produce server exceptions and reset WebSocket proxy sockets, with no failed E2E cases. The production build still reports Vite's advisory that the main JavaScript chunk exceeds 500 kB.

`.github/workflows/ci.yml` runs on pushes to `arena/**`, pull requests targeting
`arena/01a0dca0-drillai` or `main`, and manual dispatch. Its current certification sequence is:

```
checkout + provenance → Python 3.11 / Node 22 → backend[dev,ocr] install + npm ci
→ frontend typecheck / lint / unit tests (400 expected, zero skips) / production build
→ supply-chain and secret checks
→ backend tests and reviewed inventory (Linux: 1,740 passed, zero skips)
→ backend Ruff → fresh-database migration check → stack and vendor-harness checks
→ Linux Docker Compose deployment/OCR gate
→ E2E collection (127 tests / 20 spec files: Chromium 120, faults 4, auth 3)
→ real-browser E2E (exactly 127 passes, zero retries) → failure-only evidence upload
```

Each acceptance gate is a required step; there is no `continue-on-error` or broad skip allowance.
The only skipped steps on a successful job are the failure-only evidence uploads. The deployment-image
OCR step is an actual synthetic benchmark with a checked report, but it is not field accuracy evidence.

Backend expectations are centralized in `ci/test-inventory.json`. CP131's new browser spec causes
three data-driven checks in each of the existing CP32 and CP33 backend contract modules to be collected;
these six tests are included in the 1,740 total, not skipped or removed. The Linux verifier requires the
full PostgreSQL-enabled suite to pass with zero skips and compares every PostgreSQL-marked node ID with
the reviewed list. Windows expects **1,724 passed, one identified POSIX-only skip, and 15 PostgreSQL
deselections**; it permits only the temporary-shebang fixture in
`tests/ingestion/test_ocr.py` with its exact reason and checks that deselections equal the reviewed
PostgreSQL node-ID list. Any changed test identity or total must be reviewed in the manifest before CI
can pass. Frontend unit tests reject skips.

E2E inventory validation and execution are separate gates on both platforms. The collection check
requires the exact **120/4/3** project distribution and exact reviewed **20-spec** filename set. The
real-browser run must have exit code zero and a 127-passed CLI summary; its structured JSON report must
show 127 expected passes, zero unexpected/flaky/skipped results, exactly one successful zero-retry
attempt per test, and the same exact project and filename inventory. Failure annotations preserve only
reviewed, safe test locations; full HTML/trace/JSON evidence uploads only after failure.

Linux runs the full PostgreSQL-enabled backend suite and Docker Compose deployment/OCR gate. Windows
runs the native SQLite backend suite with `-m "not postgres"` (15 PostgreSQL-marked cases are not run
on Windows), the multi-process stack and local vendor-harness checks, and the complete frontend/E2E
suite; Docker Compose is Linux-only. The external vendor probe makes zero network calls without
configured endpoints/credentials, so no live interoperability certification is inferred. Both platform
jobs must verify the same 127 journeys across Chromium (120), faults (4), and auth (3). Do not interpret
the Windows PostgreSQL exclusions or Linux-only Compose gate as tests passed on Windows.

CP132's criterion was met by exact-SHA run `42481107515` on `286945c9d627ba58a4f765b3f28cc65a5e0003ae`: Linux and native-Windows jobs both succeeded on that identical SHA, all acceptance steps ran, and the inventory annotations reported the actual totals. Only failure-only evidence uploads were skipped. Any later source or documentation edit requires a new exact-SHA run; no future change inherits this certification. The workflow is read-only (`permissions: contents: read`), uses no secrets, and never writes to the repository.

### Clean-checkout certification

The pipeline is not the only proof. A fresh clone of the remote branch is bootstrapped from nothing
(`scripts/bootstrap.sh`) and put through the same gates — types, lint, unit tests, build, backend
tests with PostgreSQL, lint, a fresh-database migration check, and the complete end-to-end suite — with
no `node_modules`, `.venv`, `.e2e`, browser cache or build output copied from any earlier tree. That is
what makes the repository self-contained rather than dependent on a developer's machine.

## Current results

The post-sync local CP131 verification is recorded in
[`docs/mission-reports/CP131-SYNTHETIC-WELL-BROWSER-PILOT.md`](mission-reports/CP131-SYNTHETIC-WELL-BROWSER-PILOT.md): backend default **1,725 passed / 15 PostgreSQL-only skips**, PostgreSQL-enabled **1,740 passed / 0 skipped**, frontend **400 tests / 40 files**, E2E shape **127 tests / 20 files**, isolated full E2E **127 passed / 0 retries**, and the dedicated CP131 browser pilot **1 passed / 0 retries**. Both backend inventory-verifier checks and the CP16 documentation/count guard passed after documentation reconciliation. The local runs are not themselves cross-platform certification; use the exact-SHA Actions result attached to the final branch commit.

The base CP130 exact-SHA run `42460161993` on `70c7de6bada761f5f6f2c580bced9dcb22be13a5` completed successfully on Linux and native Windows, as recorded in the [CP130 report](mission-reports/CP130-EXECUTION-RELIABILITY-AND-EVALUATION.md); its raw OCR metrics and printed test totals were not recoverable. It predates the CP131 browser/inventory changes and all final documentation updates. CP132 cross-platform acceptance is determined by a fresh exact-final-SHA Actions run with Linux and native Windows both successful and every acceptance gate executed on the same final SHA. External field, vendor, customer, delivery, and qualified-engineering acceptance remain unestablished without corresponding evidence.
