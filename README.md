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
| `frontend/` | React/TypeScript client: drilling intelligence workspace, telemetry connector registry, and organization security settings for users and scoped API tokens (see `docs/FRONTEND.md`) |
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
./.venv/bin/pip install -e ".[dev,ocr]"
# OCR remains disabled unless explicitly configured; Tesseract and language data are OS-level installs.

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
- Unit/dimension registry (122 registered unit definitions, 33 dimensions) with explicit, testable conversions.
- 14 engineering engines (trajectory minimum-curvature, wellbore capacity, hydraulics, torque & drag,
  MSE, kill sheet, API 5C3 tubulars, barrier envelope, well schematic, offset similarity, readiness,
  twin reconciliation, optimisation sweep, Pareto) plus an engine registry and execution service.
  Their current evidence is deterministic software test vectors, not field certification; statuses are
  `formula_derived` by default, with `well_control.kill_sheet` and `tubulars.api_5c3` at
  `needs_field_validation`.
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

### Daily drilling reports

The `/wells/{wellId}/daily-reports` workspace supports manual and document-backed draft creation, immutable revisions, human source reconciliation, independent review, correction history, exact case-sensitive report-number search, bilingual exports and read-only reported-period boundary checks. Same-date report groups are ordered only by distinct, explicit period-start instants; incomplete, unapproved, unverified, ambiguous and truncated groups remain unassessed and cannot be skipped to bridge periods. Imported `report_date` and `report_number` proposals are source-backed only by exact matching fields on an extracted `report_header`; a human must accept or reject each proposal. Report identity is immutable after creation, corrections require a separate identity, and extracted well-name text never selects a well or wellbore. See [`docs/DAILY-DRILLING-REPORT-DATA-GOVERNANCE.md`](docs/DAILY-DRILLING-REPORT-DATA-GOVERNANCE.md) for the schema, API, provenance and evidence boundaries. Software tests use synthetic reports and do not establish OCR accuracy, field validation, qualified engineering acceptance or operational authorization.

### How the repository verifies itself

| Layer | Command | Scope |
| --- | --- | --- |
| Frontend types | `cd frontend && npm run typecheck` | strict TypeScript with `noUncheckedIndexedAccess` across app, E2E specs and Node config |
| Frontend lint | `cd frontend && npm run lint` | unused code, `any`, React rules |
| Frontend unit/component | `cd frontend && npm test` | 388 passed in 38 files on the CP106 worktree, including source-backed report identity, same-date period-order labels and exact report-number search |
| Frontend build | `cd frontend && npm run build` | `tsc -b` plus the production Vite build |
| Backend tests | `cd backend && DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest` | CP106 retains the 1,677-test / 121-file inventory; CP105 full suites passed (default 1,663 / 14 skipped; PostgreSQL-enabled 1,677 / 0), and CP106's daily-report API module passed 21/21, including exact search, well-scoping, and filtered-count checks. CP112 will repeat final-SHA assurance. |
| Backend lint | `cd backend && .venv/bin/python -m ruff check .` | style and import hygiene |
| Migrations | `cd backend && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic check` | no model/migration drift, against a fresh database |
| Supply chain and secrets | `backend/.venv/bin/python scripts/verify_supply_chain_and_secrets.py` | full-SHA Actions pins, workspace secret scan, `pip-audit`, full frontend `npm audit` at moderate, and production audit at high |
| Stack verification | `backend/.venv/bin/python scripts/verify_stack.py` | 16 multi-process scenarios: clean migration, backend, dedicated `connector-worker`, real WITSML 1.4.1.1 SOAP & ETP 1.2 Binary Avro ingest, soak metrics, restart recovery & stop |
| Vendor interop runner | `backend/.venv/bin/python scripts/verify_external_vendor_interop.py --mode harness` | offline WITSML 1.4.1.1 SOAP & ETP 1.2 Binary Avro conformance + opt-in `--mode external` vendor probe |
| Docker Compose smoke gate | `POSTGRES_PASSWORD=... docker compose up -d --build` | multi-container PostgreSQL, Alembic migrate, backend, `connector-worker`, and Nginx frontend proxy |
| End-to-end | `cd frontend && npm run e2e` | Reviewed shape: 124 tests / 18 files (chromium 117, faults 4, auth 3). CP103 run `41822291067` and CP104 exact-SHA run `41829810555` passed Linux and native-Windows real-browser gates; CP105's same-date journey and CP106's exact report-number search require exact-final-SHA execution. Do not run locally against existing `.e2e` data. |

CP83.1 publication status: the original exact-head run [41431413781](https://github.com/asgareyvazi/DrillAI/actions/runs/41431413781) exposed stale Windows deselection and Linux E2E totals; Windows E2E did not run in that attempt. The first correction at `8603207` passed every Linux gate, but exact-head run [41451881219](https://github.com/asgareyvazi/DrillAI/actions/runs/41451881219) failed the Windows E2E wrapper because its console parser observed no project/file records. The structured-report correction at `e75d8c1` was verified by exact-head run [41457013404](https://github.com/asgareyvazi/DrillAI/actions/runs/41457013404): GitHub job metadata records both Linux and native Windows jobs and every required gate as successful; only failure-evidence upload steps were skipped as designed. Log-archive downloads returned EOF, so per-test totals were not independently recovered from that run's logs. This certifies CP83.1 at `e75d8c1`, not subsequent CP83.2 or the final CP76–CP96 SHA. See [CP83.1 details](docs/FRONTEND_TESTING.md#cp831-publication-gate-incident-and-remediation-status).

Set `DRILLAI_TEST_POSTGRES=1` to run the fourteen PostgreSQL-backed tests across eleven files covering persistence, migrations, report-domain constraints, promotion identity, backup/restore, pgvector, worker fencing, concurrent uploads, concurrent SQL-CAS, and notification claim fencing through the repository's embedded PostgreSQL (`pgserver`). The reviewed CP106 inventory remains 1,677 tests across 121 files in `backend/tests`; `ci/test-inventory.json` expects **1,677 passed, 0 skipped** with PostgreSQL enabled and **1,663 passed, 14 skipped** by default. At CP105, the full PostgreSQL-enabled suite passed locally at **1,677 passed / 0 skipped** and the default suite passed **1,663 passed / 14 PostgreSQL-only skips**; CP104's default result was also verified by `scripts/verify_backend_test_inventory.py`. The previous CP103 PostgreSQL-enabled suite passed at 1,675 tests with one non-failing `RuntimeDirWarning` for unset `XDG_RUNTIME_DIR`; the CP105 PostgreSQL run likewise emitted only that warning. CP101 exact-SHA Actions run `41815622464` executed its Linux and Windows pytest cases without test failures but failed the inventory gate because its manifest lagged one backend test (Linux 1,673 passed; Windows 1,658 passed, 1 POSIX-only skip, 14 PostgreSQL deselections); that historical SHA remains failed. CP102 corrected that manifest; its exact-SHA Actions run `41818116645` succeeded on Linux, including the browser step, but failed the Windows backend SQLite test step. The failure annotation only reports `pytest exited 1`, the log archive returned EOF, and subsequent Windows stack/E2E gates were skipped; root cause remains unknown and CP102 is not certified. CP103 exact-SHA Actions run `41822291067` passed Linux job `126548137249` and native-Windows job `126548137556`, including both real-browser E2E gates. CP104 exact-SHA run `41829810555` tested commit `1bf18fee72fef883c78bf86d70800ffdb8e4cd3e`; Linux job `126573778395` and Windows job `126573779021` passed, including real-browser E2E. CP105 adds explicit same-date report-period ordering without changing test counts; its browser journey is shape-verified but awaits exact-final-SHA execution. CP106 adds exact case-sensitive report-number search, with API well-scope checks and a browser filter journey; no test inventory count changes. Do not run locally against existing `.e2e` data. The fourteen default skips are PostgreSQL-marked integrations; Windows deselects those fourteen and permits exactly the separately identified POSIX-only Tesseract subprocess skip. No other skips are accepted.
See `docs/WINDOWS-SETUP.md`, `docs/mission-reports/PRODUCTION-READINESS-MATRIX.md`, and
`docs/mission-reports/CP16-MISSION-REPORT.md` for historical commit-stamped verification provenance. The
[CP51–CP58 final assurance review](docs/mission-reports/CP51-58-FINAL-ASSURANCE.md) records that mission's
exact-head CI evidence and residuals; the current CP59–CP75 source audit and results are in the
[capability matrix](docs/mission-reports/CP59-CP75-CAPABILITY-MATRIX.md) and final assurance report.

The end-to-end suite is 18 spec files across three deployments of the same product — development
identity, deterministic fault injection, and authentication enabled — covering the cockpit,
live operational monitor (`live-operations.spec.ts`), telemetry connector registry & real WITSML/ETP
protocols (`connectors-platform.spec.ts`: WITSML 1.4.1.1 SOAP, production ETP 1.2 Binary Avro
`etp.1.2.binary_ws`, and internal test ETP 1.2 JSON `etp.1.2.json_ws` connectors, masked secret
references, connection-bound SSRF protection & private rig-network CIDR policy, 10-stage connection
test milestones, governed start/stop/restart/disable, worker polling, backoff & recovery, duplicate
replay guard, low-quality sensor handling, viewer read-only RBAC, 409 optimistic concurrency, and
Persian RTL technical token isolation), documents and evidence, the workflow studio lifecycle, the run monitor
with approvals, the durable run-event WebSocket, the failure matrix, permissions and identity, deep
links and context, RTL, and keyboard/assistive-technology behaviour. HTTP is never intercepted anywhere — no spec calls
`page.route()`, and the failures a healthy server cannot produce come from the application's own
config-guarded injector. The only browser-level interception is two socket-level relays in the run-event
journeys, which exist so a spec can state exactly when a live stream failed: they forward to the real
backend and fabricate no frames.

CI is triggered on every push to `arena/**` session branches (`.github/workflows/ci.yml`). Linux runs
frontend typecheck/lint/unit/build, the full backend suite with embedded PostgreSQL, lint/migration
checks, multi-process stack verification, Docker Compose smoke, the reviewed E2E inventory, and all
three Playwright projects. Native Windows runs the corresponding frontend gates, the backend suite
excluding the fourteen PostgreSQL-marked cases (which are exercised on Linux), stack/vendor-harness
checks, the same inventory, and all three E2E projects; it does not run Docker Compose. Both platforms
require exactly 124 E2E passes (Chromium 117, faults 4, auth 3). Evidence uploads only after failure.
Every required gate is a separate step that fails the job; there is no `continue-on-error`. The only
`|| true` uses are best-effort Docker Compose cleanup in the exit trap, not test or deployment gates.

Not implemented yet (do not assume otherwise):

- **External commercial WITSML/ETP vendor certification.** `witsml.1.4.1.1.soap_http` and
  `etp.1.2.binary_ws` are verified over real TCP/HTTP/WebSocket against both local protocol harness
  servers (`LocalWitsmlSoapServer`, `LocalEtpBinaryWebSocketServer`) and independent conformance
  suites, not against a live third-party commercial rig store in CI (`external_vendor_verified: false`).
- Integration adapters for outbound messaging (Telegram/WhatsApp/email) and pgvector-backed
  retrieval are configuration-shaped boundaries.
