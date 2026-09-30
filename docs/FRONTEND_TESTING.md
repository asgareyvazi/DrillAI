# Frontend testing

Four layers, each with one job. The layer that decides acceptance is the end-to-end one: it runs the
real backend, a real SQLite database seeded by the backend's own seeder, and a real browser. Mocking
is confined to unit and component tests, where it isolates a boundary rather than a feature.

## Commands

| Layer | Command | What it proves |
| --- | --- | --- |
| Types | `cd frontend && npm run typecheck` | strict TypeScript over the app, the tests and the e2e specs |
| Lint | `cd frontend && npm run lint` | no unused code, no `any` slipping in, React rules |
| Build | `cd frontend && npm run build` | `tsc -b` plus a production Vite build |
| Unit + component | `cd frontend && npm test` | values, units, the API boundary, cancellation, permissions, the shared UI states, and the pages' contracts |
| End-to-end | `cd frontend && npm run e2e` | the real stack through a browser, in three deployments |
| Backend | `cd backend && .venv/bin/python -m pytest` | the APIs the frontend reads (and the rest of the platform) |
| Backend lint | `cd backend && .venv/bin/python -m ruff check .` | backend style and import hygiene |
| Migrations | `cd backend && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic check` | no model/migration drift — against a **fresh** database |

`npm run e2e` prepares its own browser (`scripts/run-e2e.mjs`), starts the API and the web server
itself, and seeds the database before the API opens it. `DRILLAI_CHROMIUM_PATH` overrides the browser
binary in environments that provide one.

### PostgreSQL-backed tests

Two persistence tests (`tests/db/test_persistence.py`) assert against a real PostgreSQL: JSON and
timestamp behaviour that SQLite cannot prove, and that the Alembic baseline creates exactly the schema
the models declare. They skip unless the repository's own embedded PostgreSQL is switched on:

```bash
cd backend && DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest
```

With it, the suite reports **399 passed**; without it, **397 passed, 2 skipped**. CI runs them (the
`pgserver` dev dependency is already in `backend/pyproject.toml`), so the certificate is the 399-test
one. Nothing is replaced with SQLite to make them pass, and the tests are never deleted.

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
- `e2e/fixtures.ts` — the shared `test`, extended with the seeded well id and the page's console
  errors, plus typed API helpers so a spec can compare the UI with the backend's own answer instead
  of with a literal.
- `e2e/run-e2e.mjs` — resolves a browser, refuses to start when the API or web port is already held
  by a stale server, and runs the suite.

Run one spec:

```bash
cd frontend && npx playwright test e2e/well-cockpit.spec.ts --project=chromium
```

### Three deployments, one product

Playwright starts six servers. The application code is identical in all three deployments; what
differs is the deployment they are pointed at. A CI run that executes only `chromium` certifies a
third of the suite, so all three projects run.

| Project | Deployment | What only it can prove |
| --- | --- | --- |
| `chromium` | development identity | the product's own journeys |
| `chromium-faults` | `DRILLAI_E2E_FAULTS=true` + a 3 s client deadline | a real deadline exceeded, a body that is not the contract, an unhandled 500 injected into a request the interface really makes |
| `chromium-auth` | `DRILLAI_E2E_AUTH=true` | that "you are not signed in" is a real state, produced by the real authorization path |

### The journeys

65 journeys in 13 spec files. The list is the coverage; the counts are what the suite reports.

| Spec | Journeys | Covers |
| --- | --- | --- |
| `well-cockpit.spec.ts` | 4 | list → cockpit → NPT → missing data → timeline → reload → unknown well → unknown route |
| `documents-evidence.spec.ts` | 4 | upload → ingestion → extraction record → page/region evidence |
| `workflow-studio.spec.ts` | 9 | create → configure → connect → validate → save → version → publish, and the draft lifecycle |
| `run-monitor.spec.ts` | 6 | run list, node executions, the approval inbox, a run that fails and says which node failed |
| `run-events.spec.ts` | 5 | the durable run-event WebSocket: live events, cursors, reconnect, REST reconciliation, run switching |
| `error-matrix.spec.ts` | 5 | 409 stale decisions, permission refusals, not-found, and the surfaces that used to report a quiet success |
| `error-faults.spec.ts` | 4 | real timeout, malformed body, unhandled 500, and the recovery each one offers |
| `error-auth.spec.ts` | 1 | an authentication-enabled deployment refusing the client |
| `identity-permissions.spec.ts` | 7 | the role catalogue against backend authority, ceiling vs permission refusals, identity switching without a reload, socket termination on switch |
| `context-deeplinks.spec.ts` | 6 | deep links and reload: run in the address, cross-well document, unknown document/workflow/run |
| `rtl.spec.ts` | 4 | Persian layout, numerals that still equal the API's numbers, mixed-direction identifiers, graph geometry |
| `a11y.spec.ts` | 7 | keyboard-only operation, tab/table/dialog primitives, the live region during a real resumption, focus stability across a refresh, and a nameless-control sweep |
| `checkpoint5.spec.ts` | 3 | the four areas against each other: a run deep-linked in Persian, a refusal in Persian that changes identity in place, a cross-well document in the accessibility tree |

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

`.github/workflows/ci.yml` runs the same commands, in the same order, on every push to
`arena/01a0dca0-drillai`, on pull requests, and on manual dispatch:

```
checkout → provenance → Python 3.11 + Node 22 (npm cache)
→ backend install (`pip install -e "backend[dev]"` into backend/.venv)
→ frontend install (`npm ci`, lockfile authoritative)
→ typecheck → lint → unit tests → build
→ backend tests (DRILLAI_TEST_POSTGRES=1) → ruff → migration check on a fresh database
→ the complete end-to-end suite (main + faults + auth)
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
