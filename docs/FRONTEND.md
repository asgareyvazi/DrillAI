# Frontend — Drilling Intelligence Workspace

The browser client for the Drilling AI platform. It is a **view over the platform's contracts**: it
reads state the backend computed, shows where every value came from, and sends human decisions back.
It never computes engineering truth, never invents a value, and holds no second copy of a backend
model.

- Stack: React 18, TypeScript (strict), Vite 6, React Router 6, Tailwind 3, TanStack Query 5,
  Zustand 5, `@xyflow/react` 12 for the workflow canvas, Vitest + Testing Library, Playwright.
- Entry point: `src/main.tsx` → `src/App.tsx` (routes) → `AppShell` (context, navigation, units).

## Running it

```bash
# from the repository root — creates the backend venv, installs both dependency sets, prepares a browser
scripts/bootstrap.sh

# backend (terminal 1)
cd backend
DRILLAI_ENVIRONMENT=development DRILLAI_AUTH_ENABLED=false \
  ./.venv/bin/uvicorn --factory drillai.api.app:create_app --host 0.0.0.0 --port 8099

# frontend (terminal 2)
cd frontend
npm run dev                     # http://localhost:5173, /api/v1 proxied to the backend
```

`frontend/vite.config.ts` proxies `/api` to `DRILLAI_API_URL` (default `http://127.0.0.1:8099`), so
the browser only ever talks to its own origin — no CORS exceptions and no localhost URLs baked into
the client.

Demo data comes from the backend's seeder, never from the frontend:

```bash
cd backend && source .venv/bin/activate && python ../../scripts/seed_demo.py --reset
```

## Structure

```
src/
  api/         client.ts (transport, error normalisation), endpoints.ts (typed calls), types.ts (contracts)
  components/  common (Card, Table, Value, Async, Badge, Tabs…), layout (AppShell), evidence (panel)
  i18n/        en.ts, fa.ts, index.tsx — one direction strategy, no per-page overrides
  lib/         format.ts — the single unit and number formatting boundary
  pages/       wells/ (list, cockpit, documents), engineering/, workflow/, library/
  stores/      session.ts — locale, unit system, acting identity, selected context
  test/        setup.ts, fixtures/ (payloads captured from the running API)
```

## Boundaries that matter

**One API layer.** Every request goes through `src/api/client.ts`. Screens never call `fetch`
directly and never guess a payload shape. A failed request becomes an `ApiError` carrying the
platform's error code, the request id, the details object, and whether retrying could help; the UI
shows those rather than a generic message.

**Types mirror the wire, and are checked against it.** `src/api/types.ts` is written from captured
responses (`src/test/fixtures/`) and the backend serialisers. When the API changes shape on purpose,
capture the endpoint again and update the types, the component test, and the UI in one commit. The
cockpit once declared a `state.sources` field the API never sent and crashed in the browser on real
data; `src/pages/wells/WellCockpit.test.tsx` now renders the cockpit from those fixtures so that
class of mistake fails in CI instead.

**Absence is not zero.** A missing measurement, a recorded zero, an unavailable capability, a failed
computation and a pending one are five different things and are rendered as five different things:
the label of the missing state plus the backend's own reason (`Value` + `formatValue`). A region that
has no data says which data is missing and how to supply it (`missing[]` from the state payload).

**Server state vs. session state.** Anything the backend owns (wells, documents, twin, runs,
approvals) lives in TanStack Query and is refetched, never mirrored into Zustand. Zustand holds only
what belongs to the session: locale, unit system, acting identity, and the selected
project/well/wellbore/section context that the breadcrumbs and every request scope read from.

**Registry-driven, not hard-coded.** Node types, agent and skill catalogues, engine keys and report
kinds are read from the platform registries (`/registry/…`). The frontend contains no list of node
types or engines, so it cannot drift from the backend's own catalogue.

**Units at one boundary.** `src/lib/format.ts` owns SI/oilfield conversion and number rendering.
Components pass a value and a unit; nothing else formats a number, so a value cannot be shown in one
unit in the header and another in a table.

## Known limitations

- Only journey 1 (well → cockpit → documents → evidence) has an end-to-end Playwright spec so far;
  workflow, run, approval, WebSocket, failure and RTL/accessibility journeys are not yet automated
  (they are listed in `FRONTEND_TESTING.md`).
- Persian (`fa`) translations cover the shell and the primary cockpit strings, not every string in
  every workspace.
- No `ops/` deployment assets and no CI workflow file yet; the same commands are run locally and are
  documented in `FRONTEND_TESTING.md`.
