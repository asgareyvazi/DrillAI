# DrillAI — Well Engineering Intelligence Platform

Extensible platform foundation for well engineering (drilling first, full well lifecycle in scope:
planning → design → drilling → completion → intervention → integrity → P&A → lessons learned).

The platform core is **Engineering Context + Digital Well Twin + Data Fabric + Engineering Engines +
Workflow Runtime**. The LLM is one replaceable component behind an adapter: it never performs
engineering arithmetic, and it never invents engineering values.

## Repository layout

| Path | Contents |
| --- | --- |
| `backend/` | Python 3.11 service: domain models, engines, data fabric, workflow runtime, AI/LLM abstraction, WITSML 1.4.1.1 SOAP & ETP 1.2 WebSocket connectors, durable connector worker, FastAPI |
| `scripts/` | Operator/repository tooling (`bootstrap.sh` setup, `seed_demo.py` demo data, `smoke_e2e.py` API smoke check, `verify_stack.py` multi-process stack verification) |
| `frontend/` | React/TypeScript client: the drilling intelligence workspace & telemetry connector registry (see `docs/FRONTEND.md`) |
| `docs/` | Frontend, connector/protocol & operational runbook documentation (`docs/CONNECTORS_AND_OPERATIONS.md`) and mission reports (`docs/mission-reports/`) |
| `docker-compose.yml` | Reproducible multi-service deployment stack (`postgres`, `migrate`, `backend`, `connector-worker`, `frontend` Nginx proxy) with `.env.example` |

## Quickstart

```bash
# backend + frontend + a browser, from a fresh checkout
scripts/bootstrap.sh
```

Or step by step (backend):

```bash
cd backend
python3.11 -m venv .venv
./.venv/bin/pip install -e ".[dev]"

# configuration is environment-driven (prefix DRILLAI_), see src/drillai/core/config.py
DRILLAI_DATABASE_URL="sqlite+aiosqlite:///./drillai.db" ./.venv/bin/python -m alembic upgrade head
DRILLAI_DATABASE_URL="sqlite+aiosqlite:///./drillai.db" ./.venv/bin/uvicorn --factory \
    "drillai.api.app:create_app" --host 0.0.0.0 --port 8080
```

PostgreSQL is the intended production database (`postgresql+asyncpg://…`); SQLite is supported for
local development and for the test suite.

## Verification

```bash
cd backend
./.venv/bin/python -m pytest                 # full suite
./.venv/bin/python -m ruff check .           # lint (also runnable from the repository root)
./.venv/bin/python -m alembic check          # "No new upgrade operations detected" == zero model drift

cd ..
backend/.venv/bin/python scripts/smoke_e2e.py   # end-to-end smoke, no network access required
```

`scripts/smoke_e2e.py` walks the real pipeline on in-memory SQLite: context → ingestion → evidence →
digital twin → RAG → workflow runtime → engineering engine → LLM router → recommendation, including
the L4 approval (suspend/resume) path.

**PostgreSQL note.** `alembic check` must run against a database that has been upgraded first, or it
reports "Target database is not up to date" — which is the check working, not failing. CI always
creates a fresh database for it (`${{ runner.temp }}`), never reuses a developer's.

## Status

Implemented and covered by tests (backend):

- 90-table domain/data model with a zero-drift Alembic baseline migration.
- Unit/dimension registry (121 units, 33 dimensions) with explicit, testable conversions.
- 14 engineering engines (trajectory minimum-curvature, wellbore capacity, hydraulics, torque & drag,
  MSE, kill sheet, API 5C3 tubulars, barrier envelope, well schematic, offset similarity, readiness,
  twin reconciliation, optimisation sweep, Pareto) plus an engine registry and execution service.
- Digital Well Twin with planned/actual/current/historical/predicted/recommended state kinds,
  revisions, snapshots and change history.
- Data fabric: PDF/Excel/Word/CSV/DDR ingestion with extraction records, page/region provenance,
  confidence and validation status; evidence links; local and in-memory blob stores.
- Context builder (single source of truth for agents, skills, workflows, engines, prompts) with
  purpose-scoped bundles, plus retrieval (lexical/hybrid/metadata) with scope-aware filters.
- Workflow runtime: node registry across 9 families, expressions, run events, pauses, per-node
  human approvals, resume, and recommendation/artifact persistence.
- Security: 33 catalogued actions with L0–L5 action levels, RBAC roles, separation of duties,
  fail-closed authorisation for unregistered actions, token authentication and secret references.
- AI layer: provider abstraction, model registry, capability/cost/privacy routing, LLM call audit,
  tool registry and agent/skill registries (agents capped at L2).
- FastAPI service (assets/context/documents/evidence/twin/workflows/runs/registry/platform) with
  request IDs, structured errors, CORS, i18n/RTL locale plumbing and observability hooks.

Frontend status: the workspace is implemented and committed under `frontend/` — well list and
cockpit (state, NPT, timeline, twin, audit, missing data), document workspace, engineering and
optimisation workspaces, advisor and reports, workflow studio, run monitor, library and platform
pages, in English and Persian. It holds no authority of its own: permissions, action levels and
approval requirements are the server's answers, rendered. `docs/FRONTEND.md` describes the boundaries
it holds to and `docs/FRONTEND_TESTING.md` lists what is verified and how.

### How the repository verifies itself

| Layer | Command | Scope |
| --- | --- | --- |
| Frontend types | `cd frontend && npm run typecheck` | strict TypeScript over app, tests and specs |
| Frontend lint | `cd frontend && npm run lint` | unused code, `any`, React rules |
| Frontend unit/component | `cd frontend && npm test` | 307 tests in 29 files |
| Frontend build | `cd frontend && npm run build` | `tsc -b` plus the production Vite build |
| Backend tests | `cd backend && DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest -o addopts=""` | 996 tests, including 4 PostgreSQL persistence, migration & competing-worker fencing tests |
| Backend lint | `cd backend && .venv/bin/python -m ruff check .` | style and import hygiene |
| Migrations | `cd backend && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic check` | no model/migration drift, against a fresh database |
| Stack verification | `backend/.venv/bin/python scripts/verify_stack.py` | clean migration, backend, dedicated `connector-worker`, real WITSML 1.4.1.1 SOAP ingest & restart recovery |
| End-to-end | `cd frontend && npm run e2e` | 91 browser journeys in 17 spec files against the real stack |

Set `DRILLAI_TEST_POSTGRES=1` to run the PostgreSQL-backed persistence and competing-worker fencing
tests through the repository's own embedded PostgreSQL (`pgserver`); without it they skip, and the
suite reports **992 passed, 4 skipped** rather than **996 passed**.

The end-to-end suite is 17 spec files across three deployments of the same product — development
identity, deterministic fault injection, and authentication enabled — covering the cockpit,
live operational monitor (`live-operations.spec.ts`), telemetry connector registry & real WITSML/ETP
protocols (`connectors-platform.spec.ts`: WITSML 1.4.1.1 SOAP & ETP 1.2 WebSocket connectors, masked
secret references, SSRF protection, governed start/stop/restart/disable, worker polling, backoff &
recovery, duplicate replay guard, low-quality sensor handling, viewer read-only RBAC, 409 optimistic
concurrency, and Persian RTL technical token isolation), documents and evidence, the workflow studio lifecycle, the run monitor
with approvals, the durable run-event WebSocket, the failure matrix, permissions and identity, deep
links and context, RTL, and keyboard/assistive-technology behaviour. HTTP is never intercepted anywhere — no spec calls
`page.route()`, and the failures a healthy server cannot produce come from the application's own
config-guarded injector. The only browser-level interception is two socket-level relays in the run-event
journeys, which exist so a spec can state exactly when a live stream failed: they forward to the real
backend and fabricate no frames.

CI runs that whole sequence on every push to `arena/01a0dca0-drillai` (`.github/workflows/ci.yml`):
install → frontend typecheck, lint, tests, build → backend tests (with PostgreSQL), lint, migration
check → the complete end-to-end suite, with Playwright evidence uploaded only when a run has failed.
Every required gate is a separate step that fails the job; there is no `continue-on-error` and no
`|| true` in the pipeline.

Not implemented yet (do not assume otherwise):

- **External commercial WITSML/ETP vendor certification.** `witsml.1.4.1.1.soap_http` and
  `etp.1.2.json_ws` are verified over real TCP/HTTP/WebSocket against the local protocol harness
  servers (`LocalWitsmlSoapServer` and `LocalEtpWebSocketServer`), not against a live third-party
  commercial rig store in CI (`external_vendor_verified: false`).
- Integration adapters for outbound messaging (Telegram/WhatsApp/email) and pgvector-backed
  retrieval are configuration-shaped boundaries.
