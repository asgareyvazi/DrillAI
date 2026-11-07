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

The `/wells/{wellId}/daily-reports` workspace supports manual and document-backed draft creation, immutable revisions, human source reconciliation, independent review, correction history, exact case-sensitive report-number and wellbore-scoped search, bilingual exports and read-only reported-period boundary checks. Database composite keys enforce organization/well/wellbore consistency for wellbores and daily reports. Same-date report groups are ordered only by distinct, explicit period-start instants; incomplete, unapproved, unverified, ambiguous and truncated groups remain unassessed and cannot be skipped to bridge periods. Imported `report_date` and `report_number` proposals are source-backed only by exact matching fields on an extracted `report_header`; a human must accept or reject each proposal. Report identity is immutable after creation, corrections require a separate identity, and extracted well-name text never selects a well or wellbore. See [`docs/DAILY-DRILLING-REPORT-DATA-GOVERNANCE.md`](docs/DAILY-DRILLING-REPORT-DATA-GOVERNANCE.md) for the schema, API, provenance and evidence boundaries, and the [CP117 corpus report](docs/mission-reports/CP117-SYNTHETIC-DDR-CORPUS-AND-OCR-GOVERNANCE.md) for synthetic OCR input and runtime status. Software tests use synthetic reports and do not establish OCR accuracy, field validation, qualified engineering acceptance or operational authorization.

### How the repository verifies itself

| Layer | Command | Scope |
| --- | --- | --- |
| Frontend types | `cd frontend && npm run typecheck` | strict TypeScript with `noUncheckedIndexedAccess` across app, E2E specs and Node config |
| Frontend lint | `cd frontend && npm run lint` | unused code, `any`, React rules |
| Frontend unit/component | `cd frontend && npm test` | 390 passed in 39 files on the CP115 worktree, including reviewed survey-import eligibility and source-linked review coverage |
| Frontend build | `cd frontend && npm run build` | `tsc -b` plus the production Vite build |
| Backend tests | `cd backend && DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest` | The CP123 reviewed inventory contains **1,711 backend tests across 128 files** (two CP119 Advisor provenance tests, one CP120 NPT-grounding test, and one CP123 stale-snapshot worker-CAS regression test; CP121–CP122 update existing coverage without adding test cases). CP115's complete local runs are historical evidence for SHA `160bcc0b10d1cfbf139c79038cf3a0034f44ce64`: PostgreSQL-enabled **1,700/0 skipped** and default SQLite **1,686/14 PostgreSQL-only skips**, both accepted by the inventory verifier. Full backend suite reruns for CP116–CP123 remain pending; the CP121 Advisor/API/state-freshness/telemetry-latest selection passed 64 tests (33 in the Advisor/API subset), the CP122 connector/protocol/SSRF selection passed 26 tests, the CP123 connector-worker fencing selection passed 7 tests with two PostgreSQL-marked tests skipped by the default run; the PostgreSQL-enabled selection passed 9 tests, and the CP118 RAG/Advisor selection passed 70 tests. CP119's frontend typecheck/lint plus 11 tests across its three targeted files passed; CP121 rechecked frontend typecheck/lint and passed 20 tests across four engineering workspace files, and CP122 rechecked frontend typecheck/lint plus the 7 ConnectorsPage tests. CP117's DDR/OCR corpus-integrity and benchmark-mechanics selection passed **15 tests**; the actual synthetic OCR benchmark was **BLOCKED** because Tesseract is absent, so no OCR quality result is claimed. These local focused results and any earlier CI do not certify the final CP132 SHA. Earlier candidate SHA `43d6231e6b2c5929632a6d41e851bc0a2b23606c` keeps its historical totals and platform evidence unchanged below. Exact-final-SHA certification is required after CP113–CP132 integration. |
| Backend lint | `cd backend && .venv/bin/python -m ruff check .` | style and import hygiene |
| Migrations | `cd backend && .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic check` | no model/migration drift, against a fresh database |
| Supply chain and secrets | `backend/.venv/bin/python scripts/verify_supply_chain_and_secrets.py` | full-SHA Actions pins, workspace secret scan, `pip-audit`, full frontend `npm audit` at moderate, and production audit at high; current fresh local run passed (9 pinned Actions, 542 scanned files, both npm audits and pip-audit) |
| Stack verification | `backend/.venv/bin/python scripts/verify_stack.py` | 16 multi-process scenarios: clean migration, backend, dedicated `connector-worker`, real WITSML 1.4.1.1 SOAP & ETP 1.2 Binary Avro ingest, soak metrics, restart recovery & stop |
| Vendor interop runner | `backend/.venv/bin/python scripts/verify_external_vendor_interop.py --mode harness` | offline WITSML 1.4.1.1 SOAP & ETP 1.2 Binary Avro conformance + opt-in `--mode external` vendor probe |
| Docker Compose smoke gate | `POSTGRES_PASSWORD=... docker compose up -d --build` | multi-container PostgreSQL, Alembic migrate, backend, `connector-worker`, and Nginx frontend proxy |
| End-to-end | `cd frontend && npm run e2e` | Reviewed shape: 126 tests / 19 files (chromium 119, faults 4, auth 3), including two real-browser survey lifecycle/import journeys. CP103 run `41822291067` and CP104 exact-SHA run `41829810555` passed Linux and native-Windows real-browser gates. CP105 run `41834333302` was canceled before browser completion; CP106 run `41836351234` failed both browser jobs on a strict-mode locator. CP107 corrects that locator and adds wellbore-scoped filtering. Its original SHA `fce7f953921856280f3f5f1bbcfcebd10b659618` failed Windows SQLite tests with unavailable details; follow-up exact-SHA run `41845191298` passed both platforms, including E2E, on diagnostics-only commit `4deb23d351f0ef7b3d6400acd2152591f53ac334`. This does not retroactively certify the original SHA. CP108's implementation SHA `b6af66bf8e2b2a916f81c90c4355044971e2c811` passed Linux including real-browser E2E but failed the Windows backend SQLite step in run `41852513867`; the uploaded Windows artifact could not be downloaded from this sandbox and the cause remains unknown. Diagnostics-only follow-up SHA `845665416cdc92826b9c4b207c8320d672dcdc36` passed exact-SHA run `41855798854` on Linux job `126663950354` and native-Windows job `126663949682`, including both real-browser suites. This does not retroactively certify the failed `b6af...` run. Do not run locally against existing `.e2e` data; CP132 must certify the exact final SHA. |

CP83.1 publication status: the original exact-head run [41431413781](https://github.com/asgareyvazi/DrillAI/actions/runs/41431413781) exposed stale Windows deselection and Linux E2E totals; Windows E2E did not run in that attempt. The first correction at `8603207` passed every Linux gate, but exact-head run [41451881219](https://github.com/asgareyvazi/DrillAI/actions/runs/41451881219) failed the Windows E2E wrapper because its console parser observed no project/file records. The structured-report correction at `e75d8c1` was verified by exact-head run [41457013404](https://github.com/asgareyvazi/DrillAI/actions/runs/41457013404): GitHub job metadata records both Linux and native Windows jobs and every required gate as successful; only failure-evidence upload steps were skipped as designed. Log-archive downloads returned EOF, so per-test totals were not independently recovered from that run's logs. This certifies CP83.1 at `e75d8c1`, not subsequent CP83.2 or the final CP76–CP96 SHA. See [CP83.1 details](docs/FRONTEND_TESTING.md#cp831-publication-gate-incident-and-remediation-status).

Set `DRILLAI_TEST_POSTGRES=1` to run the fourteen PostgreSQL-backed tests across eleven files covering persistence, migrations, report-domain constraints, promotion identity, backup/restore, pgvector, worker fencing, concurrent uploads, concurrent SQL-CAS, and notification claim fencing through the repository's embedded PostgreSQL (`pgserver`). The CP123 inventory contains **1,711 tests across 128 files** in `backend/tests`; `ci/test-inventory.json` expects **1,711 passed, 0 skipped** with PostgreSQL enabled, **1,697 passed, 14 skipped** by default, and **1,696 passed, 1 identified POSIX-only OCR skip, 14 PostgreSQL deselections** on Windows. These are inventory expectations, not a claim that the CP116–CP123 full suites have run. CP115's complete local runs and inventory verification are recorded above; full-suite reruns for the CP116–CP123 current tree remain pending. Native-Windows values are expectations until the final exact-SHA CI. On candidate SHA `43d6231e6b2c5929632a6d41e851bc0a2b23606c`, the full PostgreSQL-enabled suite passed 1,680/0 skipped (one non-failing `RuntimeDirWarning`) and the default suite passed 1,666/14 PostgreSQL-only skips; the backend inventory verifier accepted both. The diagnostics-only follow-up's changed-file test/lint checks passed; its complete suites are covered by its exact-SHA workflow. The first rerun found only the stale 121-file documentation assertion; after updating the docs, manifest and exact-count guard, both full reruns passed. CP108 figures below are historical. CP108 local PostgreSQL-enabled tests passed **1,677 / 0 skipped** with one non-failing XDG runtime-directory warning; the default SQLite suite passed **1,663 / 14 PostgreSQL-only skips**, and the inventory verifier accepted both. Fresh SQLite migrations/check, full Ruff, `compileall`, focused SQLite migration tests and the PostgreSQL migration-chain scope assertions passed. At CP105, the full PostgreSQL-enabled suite passed locally at **1,677 passed / 0 skipped** and the default suite passed **1,663 passed / 14 PostgreSQL-only skips**; CP104's default result was also verified by `scripts/verify_backend_test_inventory.py`. The previous CP103 PostgreSQL-enabled suite passed at 1,675 tests with one non-failing `RuntimeDirWarning` for unset `XDG_RUNTIME_DIR`; the CP105 PostgreSQL run likewise emitted only that warning. CP101 exact-SHA Actions run `41815622464` executed its Linux and Windows pytest cases without test failures but failed the inventory gate because its manifest lagged one backend test (Linux 1,673 passed; Windows 1,658 passed, 1 POSIX-only skip, 14 PostgreSQL deselections); that historical SHA remains failed. CP102 corrected that manifest; its exact-SHA Actions run `41818116645` succeeded on Linux, including the browser step, but failed the Windows backend SQLite test step. The failure annotation only reports `pytest exited 1`, the log archive returned EOF, and subsequent Windows stack/E2E gates were skipped; root cause remains unknown and CP102 is not certified. CP103 exact-SHA Actions run `41822291067` passed Linux job `126548137249` and native-Windows job `126548137556`, including both real-browser E2E gates. CP104 exact-SHA run `41829810555` tested commit `1bf18fee72fef883c78bf86d70800ffdb8e4cd3e`; Linux job `126573778395` and Windows job `126573779021` passed, including real-browser E2E. CP105 adds explicit same-date report-period ordering without changing test counts. Its exact-SHA run `41834333302` was canceled after the Linux job reached E2E and the Windows job reached stack verification; it is not certified. CP106 adds exact case-sensitive report-number search, with API well-scope and filtered-count checks; exact-SHA run `41836351234` passed all pre-E2E gates on Linux and native Windows but both browser steps failed on a strict-mode locator in the CP105 same-date journey (`li` matched two comparison rows). CP107 fixes the selector and adds wellbore-scoped filtering. Original exact-SHA run `41842149924` at `fce7f953921856280f3f5f1bbcfcebd10b659618` failed the Windows backend SQLite suite; logs returned EOF and annotations only reported `pytest exited 1`, so the root cause remains unknown and that SHA is not certified. Follow-up exact-SHA run `41845191298` at diagnostics-only commit `4deb23d351f0ef7b3d6400acd2152591f53ac334` passed Linux and native Windows, including both real-browser E2E gates and the Windows suite; it certifies only `4deb...`. CP108 adds composite organization/well/wellbore foreign keys with a non-destructive preflight. Its implementation SHA `b6af66bf8e2b2a916f81c90c4355044971e2c811` was tested by exact-SHA run `41852513867`: Linux job `126652278250` passed, while native-Windows job `126652277812` failed the SQLite backend suite. Its uploaded artifact `windows-backend-tests-41852513867-1` (ID `13335199857`) could not be downloaded from this sandbox because GitHub redirected to a storage host outside the egress allowlist; the failing test/root cause remains unknown. Diagnostic-only SHA `845665416cdc92826b9c4b207c8320d672dcdc36` added sanitized failure summaries without changing CP108 product code; exact-SHA run `41855798854` passed Linux job `126663950354` and native-Windows job `126663949682`, including both real-browser suites and the Windows backend suite. This follow-up does not retroactively change the original failed run; CP132 must certify the exact final SHA. A local scan separately found advisories in the preserved virtual environment's `pip 23.0.1`, so only the fresh-runner exact-final-SHA supply-chain gate can establish a clean result. No local Playwright run was made against existing `.e2e` data. The fourteen default skips are PostgreSQL-marked integrations; Windows deselects those fourteen and permits exactly the separately identified POSIX-only Tesseract subprocess skip. No other skips are accepted.
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
require exactly 126 E2E passes (Chromium 119, faults 4, auth 3). Evidence uploads only after failure.
Every required gate is a separate step that fails the job; there is no `continue-on-error`. The only
`|| true` uses are best-effort Docker Compose cleanup in the exit trap, not test or deployment gates.

Not implemented yet (do not assume otherwise):

- **External commercial WITSML/ETP vendor certification.** `witsml.1.4.1.1.soap_http` and
  `etp.1.2.binary_ws` are verified over real TCP/HTTP/WebSocket against both local protocol harness
  servers (`LocalWitsmlSoapServer`, `LocalEtpBinaryWebSocketServer`) and independent conformance
  suites, not against a live third-party commercial rig store in CI (`external_vendor_verified: false`).
- Integration adapters for outbound messaging (Telegram/WhatsApp/email) and pgvector-backed
  retrieval are configuration-shaped boundaries.
