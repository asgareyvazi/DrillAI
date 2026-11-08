# CP131 — Synthetic-Well Browser Pilot

**Status:** `DONE` for CP131 repository software and browser acceptance on the integrated CP130–CP132 SHA `286945c9d627ba58a4f765b3f28cc65a5e0003ae`. `NEEDS_FIELD_VALIDATION` remains for field data and qualified engineering acceptance; see the exact-SHA closure below.

## Scope and data boundaries

CP131 exercises one explicitly labelled synthetic hierarchy, `CP131-SYNTHETIC-PILOT-01`, through the actual DrillAI UI, API and SQLite database. It does not mock application APIs, seed a field report, use external credentials, call a live AI provider, or modify an existing `.e2e` directory. The synthetic CSV and survey inputs are test fixtures, not customer or field evidence.

The dedicated command is:

```bash
cd frontend && npm run e2e:cp131
```

Each invocation receives a new run ID and isolated direct-child paths:

- `.e2e-cp131-<runId>` — database and blobs;
- `.e2e-cp131-results-<runId>` — browser reports and artifacts.

The config refuses an existing database or report path; there is no reset opt-in. `DRILLAI_CP131_ALLOW_RESET` is not supported. Use a new run ID instead of resetting or reusing a prior directory. The dedicated seed creates the CP131 schema and named hierarchy only; it does not invoke `seed_demo.py --reset`.

## Implementation

- The document-upload workspace now offers explicit wellbore and optional section scope, preserves operation selection, synchronizes operation scope to its wellbore/section, and clears dependent selection when the parent scope changes. The labels are available in English and Persian.
- `DailyReportService._validate_evidence` now validates a source record's recorded section against the owning project/well/wellbore, preserves that narrower section scope, and requires a linked document and source record to agree on section. It does not treat a missing requested section as a wildcard or rewrite provenance.
- Management summaries may have an empty `offsets` section whose `kind` is `evidence`. That section no longer suppresses the distinct `evidence` trail: the report builder now tests for the dedicated `evidence` key. An API assertion verifies that the imported document remains in the evidence trail while the empty offset section remains explicit.
- The CP131 browser spec is included in the reviewed default E2E inventory. The default suite collects **127 tests across 20 spec files**: Chromium 120, faults 4, authentication 3. The inventory shape check passed, and the latest isolated full local run completed all 127 tests with zero retries; details follow under Local verification.

## Pilot journey and observed results

On the synchronized branch tree, the Playwright case `CP131 synthetic-well pilot persists Survey → DDR → Operations/NPT → Engineering → RAG/Advisor → Reports/Export` passed locally: **1 passed**, zero retries, 22.8 seconds for the test (32.6 seconds for the runner invocation). Run ID: `20261020-cp131-6df31b08`.

The journey verifies persisted results at each step:

1. **Survey:** creates three explicit stations at 0, 500 and 1,000 m, with RKB datum and the declared synthetic grid frame. `trajectory.minimum_curvature` returns persisted hashes and a calculated revision. `engineering_acceptance` remains `not_established`; the survey is not field-validated.
2. **DDR:** uploads the fixture `frontend/e2e/assets/cp131_synthetic_ddr.csv` through the browser, explicitly scoped to the synthetic wellbore and section. The real extractor persists report number `131`, the source rows, and mud weight `10.8 ppg`; the browser invokes the actual promotion endpoint.
3. **Operations/NPT:** verifies source-document and source-record lineage, section scope, and the reported 2 h stuck-pipe event. Event occurrence/end times and root cause remain unknown/null; the test does not infer them from the report date or text. The summary is 2 h on recorded events, not a claim of total physical downtime.
4. **Daily report:** imports the same extracted source into a new daily-report revision, maps the source rows, records six synthetic operator dispositions, then reads back revision 2 and its reconciliation history through the API.
5. **Engineering:** runs `drilling.mse` through the workspace and confirms the stored input/output hashes. The inputs and outputs are synthetic; drilling efficiency remains null without supplied strength data.
6. **RAG and Advisor:** publishes and runs a scoped `rag.search` workflow against the uploaded document in **lexical** mode, then checks the hit in the persisted node output. Separately, the real Advisor API returns the stored 2 h NPT fact with provenance and `narrative: null`; there is no provider-backed or semantic-quality claim.
7. **Reports/export:** builds the management summary from persisted data, asserts the source-linked operation/event in chronology, section scope, unknown time basis, NPT total, calculation hash and evidence-trail document, then downloads and checks the JSON export from the UI.

Retained successful-run data and output:

- Database and blobs: `frontend/.e2e-cp131-20261020-cp131-6df31b08/` (one seeded synthetic well; one trajectory with three stations, one document, two operations, one event, one daily report, two engine runs, and one workflow run persisted).
- Browser JSON report and artifacts: `frontend/.e2e-cp131-results-20261020-cp131-6df31b08/` (`stats.expected=1`, `unexpected=0`, `skipped=0`, `flaky=0`; per-test result passed with retry index 0).

The existing-path guard was re-exercised with `DRILLAI_CP131_ALLOW_RESET=true` against the intentionally pre-created probe `frontend/.e2e-cp131-guard-probe-20261020-cp132-final/`. The config rejected it before API startup, the sentinel SHA-256 remained `59be9051e36eb1e79d011bada8221fdf68af6f8f997f2bcd9d9dee5440f3d126`, and no output path `frontend/.e2e-cp131-results-20261020-cp131-guard-final/` was created. The successful run data and this sentinel probe are retained as ignored test data; no standard `.e2e` path was used.

## Local verification

- `backend/tests/api/test_daily_reports_api.py` — **22 passed**, including section-tagged source evidence and cross-section record/document refusal.
- `test_reports_keep_evidence_apart_from_calculations` — **1 passed**, including the management-summary evidence regression.
- Full default SQLite backend suite — **1,725 passed, 15 PostgreSQL-only skipped**; `scripts/verify_backend_test_inventory.py --platform default` passed.
- Full PostgreSQL-enabled backend suite — **1,740 passed, 0 skipped** (one non-failing `RuntimeDirWarning`); `scripts/verify_backend_test_inventory.py --platform linux` passed and verified 15 PostgreSQL node IDs across 12 files.
- Full backend Ruff and `compileall` passed; the changed API/section-scope and report-summary tests were also run directly.
- Fresh SQLite `alembic upgrade head && alembic check` passed. `scripts/verify_stack.py` passed all **16 multi-process scenarios** and its **32-cycle bounded soak**.
- `scripts/verify_external_vendor_interop.py --mode harness` passed WITSML 1.4.1.1 and ETP 1.2 harness checks with `network_calls_made=0`; it reports `external_vendor_verified=false` and `BLOCKED_BY_EXTERNAL_EVIDENCE`, not vendor acceptance.
- `scripts/verify_supply_chain_and_secrets.py` passed locally: **9 pinned Actions, 616 workspace files scanned**, `pip-audit`, and both npm audit scopes.
- `test_cp16_10_documentation_and_contract_consistency_guard` — **1 passed** after the README, inventory, and frontend-testing totals were reconciled.
- Full frontend Vitest suite — **400 passed in 40 files**; typecheck and lint passed. Production build passed with the existing >500 kB main-chunk advisory.
- `npm run e2e:verify-shape` — passed: **127 tests / 20 files**, Chromium 120, faults 4, auth 3.
- The complete isolated three-project local E2E run `full5` passed **127/127**, zero retries (Chromium 120, faults 4, auth 3; 20.4 minutes); `scripts/verify-e2e-run.mjs` accepted both the log and structured report. Fresh `.e2e-cp132-main-*`, `-faults-*`, `-auth-*`, and `-results-*` directories were used; the standard `.e2e` path was absent and untouched. Retained results: `frontend/.e2e-cp132-results-20261020-cp132-full5/` and matching deployment directories.
- `npm run e2e:cp131` — passed the isolated real-browser pilot as recorded above. The existing-path guard refused the retained sentinel probe before API startup; the sentinel checksum was unchanged.

These local results are synthetic software evidence on the reconciled worktree. The dedicated CP131 run used only unique `.e2e-cp131-*` paths; the standard `.e2e` path was absent and untouched. The exact-SHA Linux/native-Windows closure and its actual gate totals are recorded below; they supersede the pre-CI certification requirement in this historical local-verification paragraph.

## Acceptance and limitations

| Area | Result |
| --- | --- |
| Isolated synthetic browser/API/database flow | `DONE` locally |
| Section-tagged source validation and linked-document consistency | `DONE` locally; focused and module tests passed |
| Management-summary source evidence and JSON export | `DONE` locally; regression and browser assertions passed |
| Integrated CP131 Linux/native-Windows software acceptance | `DONE` — the CP131 test is included in the 127-test suite on both platforms in CP132 run `42481107515`; the standalone CP131 commit was not separately run |
| Real DDR/OCR/provider response/customer acceptance | `NEEDS_FIELD_VALIDATION` — no such evidence was generated |
| Qualified survey or engineering acceptance | `NEEDS_FIELD_VALIDATION` — synthetic grid/datum and formula outputs are not field validation |
| Semantic RAG quality or live Advisor narrative quality | Not measured; lexical retrieval and computed facts only |

No autonomous rig-control decision, engineering recommendation, verified cause, real-world survey accuracy, delivered notification, customer acceptance, or production deployment is established by this pilot.

## CP133 exact-SHA closure (2026-11-08)

The integrated CP130–CP132 branch commit `286945c9d627ba58a4f765b3f28cc65a5e0003ae` was tested by [GitHub Actions run `42481107515`](https://github.com/asgareyvazi/DrillAI/actions/runs/42481107515), whose `headSha` is that exact commit. Both platform jobs concluded `success`:

| Platform | Job ID | Backend | Frontend | Full Playwright suite |
| --- | ---: | --- | --- | --- |
| Linux | `128733009790` | 1,740 passed; 0 skipped/deselected; 1,740 collected; 15 PostgreSQL node IDs across 12 files | 400 passed | 127 passed across 20 spec files; Chromium 120, faults 4, auth 3; 0 retries |
| Native Windows PowerShell | `128733009555` | 1,724 passed; 1 identified POSIX-only skip; 15 PostgreSQL tests deselected; 1,740 collected | 400 passed | 127 passed across 20 spec files; Chromium 120, faults 4, auth 3; 0 retries |

The reviewed E2E inventory includes `cp131-synthetic-pilot.spec.ts`; therefore the successful 127-test real-browser run on each platform includes the CP131 pilot case. This closes CP131 repository software/browser acceptance on the integrated exact SHA. It does not establish field DDR accuracy, approved-corpus OCR quality, survey/engineering qualification, live-provider narrative quality, customer acceptance, delivery, vendor commissioning, or production readiness. The pilot and all its values remain explicitly synthetic; the source evidence, unknown timestamps and lexical retrieval boundaries above remain unchanged.
