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
| Unit + component | `cd frontend && npm test` | 385 passed in 38 files: values and authoritative unit conversion, report-specific unknown-unit handling, multi-format document upload/review, the API boundary, cancellation, the request-failure oracle, permissions, identity settings, the shared UI states, document-review guidance and direction, and the pages' contracts |
| End-to-end | `cd frontend && npm run e2e` | Local Linux real-browser run: 123 passed in 5.0m across 18 reviewed spec files (chromium 116, faults 4, auth 3); inventory shape also verified 123 tests, no skips or retries. The CP83.1 cross-platform run is historical; verify the final CP76–CP96 commit against the matching `ci.yml` Actions run. |
| Backend | `cd backend && DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest` | Current local full runs: PostgreSQL-enabled 1,672 passed / 0 skipped (one non-failing `RuntimeDirWarning` because `XDG_RUNTIME_DIR` is unset); default 1,658 passed / 14 PostgreSQL-marked skips. Inventory: 1,672 tests across 120 files; no failures, deselections or xfails. |
| Backend lint | `cd backend && .venv/bin/python -m ruff check .` | backend style and import hygiene |
| Migrations | `cd backend && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic check` | no model/migration drift — against a **fresh** database |
| Supply chain + secrets | `backend/.venv/bin/python scripts/verify_supply_chain_and_secrets.py` | full-SHA Actions pins, tracked/untracked/eligible ignored text scan, `pip-audit`, full frontend `npm audit` at moderate, and production audit at high |

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

Fourteen tests across eleven files assert against a real PostgreSQL: two in `tests/db/test_persistence.py`
(JSON and timestamp behavior SQLite cannot prove, and that the Alembic baseline creates exactly the
schema the models declare); one in `tests/db/test_migrations_postgres.py` (the whole migration chain,
drift, downgrade/re-upgrade, and duplicate regulator identifiers); one in
`tests/db/test_cp18_postgres_backup_restore.py` (logical backup/restore and migration recovery drill);
one in `tests/db/test_promotion_identity_migration.py` (PostgreSQL promotion fingerprint uniqueness and tenant scope);
one in `tests/drilling/test_daily_report_domain.py` (composite tenant FKs and typed report-domain checks);
three in `tests/documents/test_document_identity.py` (operation-aware identity migration backfill/downgrade,
concurrent uploads of one logical document, and concurrent uploads into different scopes); one in
`tests/test_cp20_integration_completeness.py` (pgvector retrieval); one in
`tests/telemetry/test_cp15_egress_policy_recovery.py` (separate-worker egress-policy recovery); one in
`tests/telemetry/test_connector_worker.py` (`test_competing_workers_fencing_on_postgres`); one in
`tests/telemetry/test_cp13_witsml_and_worker_races.py` (`test_phase6_postgres_concurrent_sql_cas_races`); and one in
`tests/workflow/test_notification_worker.py` (`test_competing_notification_worker_claims_skip_locked_rows_on_postgres`, which holds the first transaction's row lock while an independent second session attempts a claim).
The migration check exists because a boolean comparison previously failed on PostgreSQL but passed on
SQLite: the dialect that accepts a comparison cannot certify the dialect that rejects it. These tests
are enabled by the repository's embedded PostgreSQL, never replaced with SQLite, and skip unless the
PostgreSQL flag is set:

```bash
cd backend && DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest
```

The current full local PostgreSQL-enabled run reports **1,672 passed, 0 skipped** across 120 test files. With the PostgreSQL flag unset, the same 1,672-test inventory reports **1,658 passed, 14 skipped**; all 14 cases are PostgreSQL-marked and are not represented as passes. Neither run had failures, deselections or xfails. The PostgreSQL-enabled run emitted one non-failing `RuntimeDirWarning` because `XDG_RUNTIME_DIR` is unset; the default run emitted no warning. CI runs the enabled suite on Linux (the `pgserver` dev dependency is in `backend/pyproject.toml`); native Windows uses `-m "not postgres"` to deselect the 14 PostgreSQL integration cases and permits only the separately identified POSIX-only Tesseract CLI subprocess skip. The reviewed inventory accounts for 1,672 Linux passes, 1,658 default passes plus 14 PostgreSQL skips, and 1,657 Windows passes plus one POSIX-only skip and 14 deselections; the final native-Windows result remains the authority for platform execution.

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
spec-file set and counts before execution: **123 tests in 18 files** (`chromium=116`,
`chromium-faults=4`, `chromium-auth=3`). The CI run emits Playwright's structured JSON report; the
post-run verifier requires a zero process exit, a 123-passed CLI summary, zero skipped/flaky/unexpected
results, exactly one passing zero-retry attempt per test, and the reviewed project distribution and
exact spec-file set. The inventory check itself is collection-only and is not reported as executed tests.

| Project | Deployment | What only it can prove |
| --- | --- | --- |
| `chromium` | development identity | the product's own journeys |
| `chromium-faults` | `DRILLAI_E2E_FAULTS=true` + a 3 s client deadline | a real deadline exceeded, a body that is not the contract, an unhandled 500 injected into a request the interface really makes |
| `chromium-auth` | `DRILLAI_E2E_AUTH=true` | that "you are not signed in" is a real state, produced by the real authorization path |

### The journeys

123 journeys in 18 spec files. The list is the coverage; the counts are what the suite reports.

| Spec | Journeys | Covers |
| --- | --- | --- |
| `well-cockpit.spec.ts` | 4 | list → cockpit → NPT → missing data → timeline → reload → unknown well → unknown route |
| `live-operations.spec.ts` | 3 | CP10 live operational vertical slice: synthetic telemetry commissioning → automatic rule evaluation on ingestion → live KPI strip & bounded channel history chart → historical `T1` alert evidence drawer unaffected by later `T2` points → server-governed `allowed_transitions` ack/clear/cancel & real 409 conflict recovery → hysteresis auto-clear → cross-well stream isolation → missing/untrustworthy quality honesty → Persian RTL token isolation |
| `connectors-platform.spec.ts` | 42 | CP11–CP16 telemetry connectors, WITSML 1.4.1.1 SOAP, production ETP 1.2 Binary Avro (`etp.1.2.binary_ws`), & internal-test ETP 1.2 JSON (`etp.1.2.json_ws`), connection-bound SSRF & durable private rig-network CIDR policy (`EGRESS_POLICY_STATES`, redaction, `manifest-policy-revision`, `worker-policy-stale-badge`), 10-stage connection-test milestones, secret minimization & redaction, reconciled run disposition manifests (11-state `POLL_DATA_OUTCOMES`, per-channel `channel_cursors`), governed dry-run (`dry_run_preview`) & live replay, 409 optimistic concurrency, mid-poll lease preemption, RBAC, and Persian RTL technical token isolation |
| `documents-evidence.spec.ts` | 4 | upload → ingestion → extraction record → page/region evidence |
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

`.github/workflows/ci.yml` runs the same commands, in the same order, on pushes to `arena/**`, on
pull requests targeting `arena/01a0dca0-drillai` or `main`, and on manual dispatch:

```
checkout → provenance → Python 3.11 + Node 22 (npm cache)
→ backend install (`pip install -e "backend[dev,ocr]"` into backend/.venv; Tesseract remains an opt-in OS dependency)
→ frontend install (`npm ci`, lockfile authoritative)
→ typecheck → lint → unit tests → build
→ backend tests (DRILLAI_TEST_POSTGRES=1) → ruff → migration check on a fresh database
→ reviewed E2E inventory (123 tests: Chromium 116, faults 4, auth 3)
→ the complete end-to-end suite (main + faults + auth; exactly 123 passes)
→ Playwright evidence, uploaded only after a failure
```

Each required gate is its own step, so a red run names the gate that failed. There is no
`continue-on-error`; the only `|| true` uses are best-effort Docker Compose cleanup in the exit trap,
not test or deployment gates. There is no `if: always()` outside the failure-artifact step: a required
failure fails the job and skips what follows.

The testing gates **certify their own results** rather than trusting their inputs. Backend expectations
are centralized in `ci/test-inventory.json`. The Linux verifier requires the full PostgreSQL-enabled
suite to pass with zero skips and compares every PostgreSQL-marked node ID with the reviewed list. The
Current Windows verifier inventory requires exactly 1,642 passes, one skip, and 14 deselections; it permits only the
`tests/ingestion/test_ocr.py` POSIX-only temporary-shebang fixture with its exact reason, and confirms
the deselections equal the reviewed PostgreSQL node-ID list. Any changed test identity or total must be
reviewed in the manifest before CI can pass. Frontend unit tests reject skips.

E2E inventory validation and execution are separate gates on both platforms. The pre-run collection
check requires the exact 116/4/3 project distribution and exact reviewed 18-spec filename set. The
following real-browser run must have exit code zero and a 123-passed CLI summary; its structured JSON
report must show 123 expected passes, zero unexpected/flaky/skipped results, exactly one successful
zero-retry attempt per test, and the same exact project and filename inventory. Failure annotations
preserve the relevant `Error`, `Expected`, and `Received` lines; full HTML/trace/JSON evidence uploads
only after failure. This matters because some networks cannot reach GitHub's raw log object store: a
green step's own conclusion and summary must still represent the checked property.

Linux runs the full PostgreSQL-enabled backend suite and Docker Compose deployment gate. Windows runs
the native SQLite backend suite with `-m "not postgres"` (the fourteen PostgreSQL-marked cases are not
run on Windows), the multi-process stack and local vendor-harness checks, and the complete frontend/E2E
suite; Docker Compose is Linux-only. The external vendor probe makes zero network calls without
configured endpoints/credentials, so no live interoperability certification is inferred. Both platform
jobs must run all 123 journeys across Chromium (116), faults (4), and auth (3). Do not interpret the
Windows PostgreSQL exclusions or Linux-only Compose gate as tests passed on Windows.

The workflow is read-only (`permissions: contents: read`), uses no secrets, and never writes to the
repository.

### Clean-checkout certification

The pipeline is not the only proof. A fresh clone of the remote branch is bootstrapped from nothing
(`scripts/bootstrap.sh`) and put through the same gates — types, lint, unit tests, build, backend
tests with PostgreSQL, lint, a fresh-database migration check, and the complete end-to-end suite — with
no `node_modules`, `.venv`, `.e2e`, browser cache or build output copied from any earlier tree. That is
what makes the repository self-contained rather than dependent on a developer's machine.

## Current results

This page's CP76–CP96 local test results and evidence limits are recorded in
[`docs/mission-reports/CP83-CP96-FINAL-ASSURANCE.md`](mission-reports/CP83-CP96-FINAL-ASSURANCE.md).
The matching exact-final-SHA Linux and native-Windows run must be checked in GitHub Actions against
the commit that contains that report; the earlier CP83.1/CP83.2 runs cited above are not a substitute.
