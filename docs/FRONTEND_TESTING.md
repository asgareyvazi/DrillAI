# Frontend testing

Three layers, each with one job. The layer that decides acceptance is the end-to-end one: it runs the
real backend, a real SQLite database seeded by the backend's own seeder, and a real browser. Mocking
is confined to unit and component tests, where it isolates a boundary rather than a feature.

## Commands

| Layer | Command | What it proves |
| --- | --- | --- |
| Types | `cd frontend && npm run typecheck` | strict TypeScript over the app, the tests and the e2e specs |
| Lint | `cd frontend && npm run lint` | no unused code, no `any` slipping in, React rules |
| Build | `cd frontend && npm run build` | `tsc -b` plus a production Vite build |
| Unit + component | `cd frontend && npm test` | values, units, the API boundary, shared UI states, the cockpit contract |
| End-to-end | `cd frontend && npm run e2e` | the real stack through a browser |
| Backend | `cd backend && source .venv/bin/activate && pytest -q` | the APIs the frontend reads |
| Backend lint | `cd backend && ruff check .` | backend style |
| Migrations | `cd backend && alembic upgrade head && alembic check` | no model/migration drift |

`npm run e2e` prepares its own browser (`scripts/run-e2e.mjs`), starts the API and the web server
itself, and seeds the database before the API opens it. `DRILLAI_CHROMIUM_PATH` overrides the browser
binary in environments that provide one.

## Unit and component tests

- `src/lib/format.test.ts` — the value semantics the product promises: missing is not zero, a
  recorded zero is a value, precision does not depend on the value being a number, and only
  registered units convert.
- `src/api/client.test.ts` — query building, identity headers, and the error matrix (platform
  envelope, FastAPI validation, 409 approval-required, malformed body, empty 204, transport failure,
  abort).
- `src/components/common/common.test.tsx` — the shared data states: loading announces itself, an
  error names the failure with its request id and offers a retry, an empty result is visibly empty.
- `src/pages/wells/WellCockpit.test.tsx` — the cockpit rendered from payloads captured off the
  running API (`src/test/fixtures/`). Reading a field the backend does not send fails here.

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
- `e2e/well-cockpit.spec.ts` — journey 1: list → cockpit → NPT → missing data → timeline → reload →
  unknown well → unknown route.

Run one spec:

```bash
cd frontend && npx playwright test e2e/well-cockpit.spec.ts --project=chromium
```

### Journeys still to automate

1. Document upload → ingestion → records → evidence (upload through the UI, then verify the
   extraction record and its page/region evidence).
2. Workflow create → configure → connect → validate → save → version → publish.
3. Run a workflow → node execution → events → completed.
4. Approval approve → resume → complete, and reject → cancel, including a refresh while waiting.
5. A failing node: the error, the timeline, and the remaining nodes not shown as succeeded.
6. The run-event WebSocket: live events with REST reconciliation.
7. Context persistence across cockpit → documents → evidence → engineering → workflow → run.
8. Permission journeys (read/draft/publish/run/approve) with backend authority.
9. RTL/Persian rendering and keyboard accessibility on the primary surfaces.
10. Loading/success/empty/error/forbidden/not-found/partial for each primary surface.

Until those exist, they are **not** claimed as covered.

## Current results

Recorded in `docs/mission-reports/FRONTEND-MISSION.md` with the commit they were produced at. Results
from a different commit are not evidence for this one.
