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
| Unit + component | `cd frontend && npm test` | 329 passed in 31 files: values, units, the API boundary, cancellation, the request-failure oracle, permissions, identity settings, the shared UI states, and the pages' contracts |
| End-to-end | `cd frontend && npm run e2e` | 121 passed in 17 spec files: the real stack through a browser, in three deployments |
| Backend | `cd backend && DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest -o addopts=""` | 1,248 passed in 90 files, including the embedded PostgreSQL integration cases |
| Backend lint | `cd backend && .venv/bin/python -m ruff check .` | backend style and import hygiene |
| Migrations | `cd backend && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic check` | no model/migration drift — against a **fresh** database |

`npm run e2e` prepares its own browser (`scripts/run-e2e.mjs`), starts the API and the web server
itself, and seeds the database before the API opens it. `DRILLAI_CHROMIUM_PATH` overrides the browser
binary in environments that provide one.

### PostgreSQL-backed tests

Eight tests across seven files assert against a real PostgreSQL: two in `tests/db/test_persistence.py`
(JSON and timestamp behavior SQLite cannot prove, and that the Alembic baseline creates exactly the
schema the models declare); one in `tests/db/test_migrations_postgres.py` (the whole migration chain,
drift, downgrade/re-upgrade, and duplicate regulator identifiers); one in
`tests/db/test_cp18_postgres_backup_restore.py` (logical backup/restore and migration recovery drill);
one in `tests/test_cp20_integration_completeness.py` (pgvector retrieval); one in
`tests/telemetry/test_cp15_egress_policy_recovery.py` (separate-worker egress-policy recovery); one in
`tests/telemetry/test_connector_worker.py` (`test_competing_workers_fencing_on_postgres`); and one in
`tests/telemetry/test_cp13_witsml_and_worker_races.py` (`test_phase6_postgres_concurrent_sql_cas_races`).
The migration check exists because a boolean comparison previously failed on PostgreSQL but passed on
SQLite: the dialect that accepts a comparison cannot certify the dialect that rejects it. These tests
are enabled by the repository's embedded PostgreSQL, never replaced with SQLite, and skip unless the
PostgreSQL flag is set:

```bash
cd backend && DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest -o addopts=""
```

The latest complete run for this revision reports **1,248 passed across 90 test files** with the flag
set. CI runs the same PostgreSQL-backed suite (the `pgserver` dev dependency is in
`backend/pyproject.toml`); the recorded result is the enabled-PostgreSQL run, not a test-vector or an
assumption that skipped integration tests passed.

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
inventory before execution: **121 tests in 17 files** (`chromium=114`, `chromium-faults=4`,
`chromium-auth=3`). CI then requires **121 passed** in each platform job; the inventory check itself
is collection-only and is not reported as executed tests.

| Project | Deployment | What only it can prove |
| --- | --- | --- |
| `chromium` | development identity | the product's own journeys |
| `chromium-faults` | `DRILLAI_E2E_FAULTS=true` + a 3 s client deadline | a real deadline exceeded, a body that is not the contract, an unhandled 500 injected into a request the interface really makes |
| `chromium-auth` | `DRILLAI_E2E_AUTH=true` | that "you are not signed in" is a real state, produced by the real authorization path |

### The journeys

121 journeys in 17 spec files. The list is the coverage; the counts are what the suite reports.

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

`.github/workflows/ci.yml` runs the same commands, in the same order, on pushes to `arena/**`, on
pull requests targeting `arena/01a0dca0-drillai` or `main`, and on manual dispatch:

```
checkout → provenance → Python 3.11 + Node 22 (npm cache)
→ backend install (`pip install -e "backend[dev]"` into backend/.venv)
→ frontend install (`npm ci`, lockfile authoritative)
→ typecheck → lint → unit tests → build
→ backend tests (DRILLAI_TEST_POSTGRES=1) → ruff → migration check on a fresh database
→ reviewed E2E inventory (121 tests: Chromium 114, faults 4, auth 3)
→ the complete end-to-end suite (main + faults + auth; exactly 121 passes)
→ Playwright evidence, uploaded only after a failure
```

Each required gate is its own step, so a red run names the gate that failed. There is no
`continue-on-error`, no `|| true`, and no `if: always()` outside the artifact step: a required failure
fails the job and skips what follows.

The three testing gates **certify their own results** rather than trusting their inputs. The backend
gate fails if the suite reports any skip (`DRILLAI_TEST_POSTGRES=1` is only evidence if the PostgreSQL
tests actually ran) and if the postgres-marked tests are no longer collected; the end-to-end gate fails
unless all three deployments ran tests and the run finished with no failure, flake or skip; the unit
gate fails on a skip. This matters because GitHub serves raw logs from Azure blob storage, which some
networks cannot reach: the step's conclusion stays readable through the API everywhere, so a green step
has to mean the property held. The counts are written to the run summary as well, for anyone who opens
the run, and `set -o pipefail` keeps each command's exit status as the step's. Each gate normalises the
ANSI escapes the runner adds before it parses anything: the first version did not, and refused a run
that had in fact passed. This was certified, not assumed — a temporary probe commit
failed the backend gate on purpose and the run went red, skipped the backend lint, migration and
end-to-end steps, and collected no artifacts; it was removed in the following commit
(see `docs/mission-reports/FRONTEND-MISSION.md`, §CP6).

Linux runs the full PostgreSQL-enabled backend suite and Docker Compose deployment gate. Windows
runs the native SQLite backend suite with `-m "not postgres"` (the eight PostgreSQL-marked cases are
not run on Windows), the multi-process stack and local vendor-harness checks, and the complete
frontend/E2E suite; Docker Compose is Linux-only. The external vendor probe makes zero network calls
without configured endpoints/credentials, so no live interoperability certification is inferred.
Both platform jobs run the reviewed E2E inventory and require all 121 journeys to pass across
Chromium (114), faults (4), and auth (3). Do not interpret the Windows PostgreSQL exclusions or
Linux-only Compose gate as tests passed on Windows.

The workflow is read-only (`permissions: contents: read`), uses no secrets, and never writes to the
repository.

### Clean-checkout certification

The pipeline is not the only proof. A fresh clone of the remote branch is bootstrapped from nothing
(`scripts/bootstrap.sh`) and put through the same gates — types, lint, unit tests, build, backend
tests with PostgreSQL, lint, a fresh-database migration check, and the complete end-to-end suite — with
no `node_modules`, `.venv`, `.e2e`, browser cache or build output copied from any earlier tree. That is
what makes the repository self-contained rather than dependent on a developer's machine.

## Current results

Recorded in `docs/mission-reports/FRONTEND-MISSION.md` with the commit they were produced at. Results
from a different commit are not evidence for this one.
