# Frontend Mission — Drilling Intelligence Workspace

**Status: IN PROGRESS — NOT COMPLETE.**

This file is the durable record of the mission. It lives in Git on purpose: a future session must be
able to resume from the repository alone, without the chat that produced it. Every checkpoint below
carries the commit it was produced at, and results from a different commit are not evidence for this
one.

Mission target branch: `arena/01a0dca0-drillai` (never `main`; no merge into `main`).
Remote: `origin` → `https://github.com/asgareyvazi/DrillAI`.

---

## 1. Baseline (repository verification, this session)

| Item | Value |
| --- | --- |
| Branch | `arena/01a0dca0-drillai` |
| Baseline HEAD (start of this session's work) | `aa7ff9d236082db93342d3f0e892430778126a11` — `feat: establish drilling AI platform foundation` |
| Baseline remote HEAD | `aa7ff9d…` (identical; verified with `git ls-remote --heads origin`) |
| Baseline tracked files | 122 |
| Baseline working tree | dirty: 11 modified files, the frontend, the drilling domain, the bootstrap and E2E harness and the new tests all **untracked** |
| History | present but shallow/grafted; ancestry is not invented anywhere in this report |

**Prior-work classification, from the repository rather than from any report:**

| Item | Classification | Evidence |
| --- | --- | --- |
| Python platform foundation (engines, twin, data fabric, workflow runtime, security, AI layer) | **complete and committed** | `aa7ff9d`, 122 tracked files, 365 passing backend tests |
| Frontend | **partial, uncommitted, and one crash-on-real-data defect** | `frontend/` was untracked; the cockpit threw `TypeError … reading 'join'` against the real API |
| Drilling domain module (`drillai/drilling/`) | **complete but uncommitted** | untracked; 34 tests across `tests/drilling/` and `tests/api/test_drilling_api.py` |
| Bootstrap race fix in `api/deps.py` | **complete but uncommitted** | untracked regression test that fails against the pre-fix body |
| E2E harness | **partial, uncommitted, and previously unable to fail honestly** | it seeded after starting the server, so it drove an empty database |

The claim "the UI is live" that preceded this mission is **not** treated as evidence anywhere here; a
dev server starting is a smoke signal, not a result.

---

## 2. Checkpoints

Each checkpoint was implemented, tested, inspected (`git status` / `git diff`), committed, pushed and
verified against the remote before the next began.

### Checkpoint 1 — Backend drilling domain and the bootstrap race

- Commit `b981017` — `feat(backend): drilling intelligence domain with NPT, timeline and reporting APIs`
- Contents: `drillai/drilling/` (DDR, NPT, state, timeline, reporting, optimisation, advisor,
  classifiers), `api/routers/drilling.py`, serialiser and ingestion changes, workflow node changes,
  `backend/pyproject.toml` (`pythonpath`), `scripts/seed_demo.py`, and the tests for all of it.
- Defect fixed: two first requests from the same org raced on `organizations.slug`; the insert now
  happens in a savepoint and a lost race re-selects the winner. The regression test fails against the
  pre-fix body — proven by temporarily restoring it and watching `IntegrityError: UNIQUE constraint
  failed: organizations.slug`.

### Checkpoint 2 — The frontend workspace, corrected against the real API

- Commit `8681fc2` — `feat(frontend): drilling intelligence workspace on the existing API contracts`
- 39 files, 15,397 lines: API layer, formatting boundary, app shell, cockpits and workspaces, i18n.
- Defects fixed in this checkpoint, all found by running the product against the real backend rather
  than by reading it:
  - the cockpit crashed on real data because `types.ts` declared fields the API never sent
    (`state.sources`, a full NPT inside the state bundle, `entry.detail`, `entry.state`,
    `entry.confidence`, `missing[].label`);
  - the NPT Pareto belongs to `GET /wells/{id}/npt`, which answers inside an envelope
    (`{ "npt": … }`), while the state bundle carries only a six-field headline;
  - controllability is tri-state and was being collapsed to yes/no;
  - the header never named the well it was showing;
  - a cancelled request was reported as "the backend is unreachable";
  - the 404 page had no level-1 heading.

### Checkpoint 3 — Frontend tests

- Commit `bfa7581` — `test(frontend): pin value semantics, the API boundary and the cockpit contract`
- 10 files, 714 lines: 54 tests plus the fixtures they assert against (captured from the running API
  for the seeded **synthetic** well).

### Checkpoint 4 — End-to-end journey

- Commit `98c742f` — `test(e2e): well journey against a real backend, real database and a real browser`
- 7 files, 673 lines, including the deterministic browser preparation that replaces a hand-made
  `/tmp` recipe.

### Checkpoint 5 — Bootstrap and ignores

- Commit `f259c33` — `chore: reproducible environment bootstrap and build artefacts ignored`

All five commits were pushed before the next one was created; `git ls-remote` matched local `HEAD` at
`f259c336b6185e9b0be0264c8c660122b0142f08`.

---

## 3. Test results (at commit `f259c33`)

Exact counts, produced by running the commands below at this commit.

| Suite | Command | Result |
| --- | --- | --- |
| Backend unit + API | `cd backend && source .venv/bin/activate && pytest -q` | **365 passed, 0 failed, 2 skipped** (96.9 s) |
| Backend lint | `cd backend && ruff check .` | **clean** |
| Migration drift | `alembic upgrade head` then `alembic check` | **no new upgrade operations detected** |
| Frontend types | `cd frontend && npm run typecheck` | **clean** |
| Frontend lint | `cd frontend && npm run lint` | **clean** |
| Frontend build | `cd frontend && npm run build` | **built** — 263 modules, 546.6 kB JS (168.2 kB gzip), 35.4 kB CSS |
| Frontend unit + component | `cd frontend && npm test` | **54 passed** (4 files) |
| Frontend end-to-end | `cd frontend && npm run e2e` | **4 passed** (1 file, real API, real DB, real browser) |

Backend per-file results:

| File | Passed | Failed | Skipped |
| --- | --- | --- | --- |
| `tests/ai/test_ai_layer.py` | 26 | 0 | 0 |
| `tests/api/test_api.py` | 31 | 0 | 0 |
| `tests/api/test_bootstrap_concurrency.py` | 3 | 0 | 0 |
| `tests/api/test_drilling_api.py` | 12 | 0 | 0 |
| `tests/context/test_engineering_context.py` | 14 | 0 | 0 |
| `tests/db/test_persistence.py` | 9 | 0 | 2 |
| `tests/drilling/test_classifiers_and_npt.py` | 19 | 0 | 0 |
| `tests/drilling/test_npt_summary.py` | 3 | 0 | 0 |
| `tests/engines/test_fluids.py` | 20 | 0 | 0 |
| `tests/engines/test_mechanics.py` | 14 | 0 | 0 |
| `tests/engines/test_planning_engines.py` | 35 | 0 | 0 |
| `tests/engines/test_registry_contracts.py` | 11 | 0 | 0 |
| `tests/engines/test_safety_engines.py` | 29 | 0 | 0 |
| `tests/engines/test_trajectory.py` | 11 | 0 | 0 |
| `tests/engines/test_twin_optimization_engines.py` | 17 | 0 | 0 |
| `tests/ingestion/test_pipeline.py` | 13 | 0 | 0 |
| `tests/rag/test_retriever.py` | 9 | 0 | 0 |
| `tests/security/test_security.py` | 22 | 0 | 0 |
| `tests/twin/test_twin_service.py` | 9 | 0 | 0 |
| `tests/units/test_registry` (5 files) | 29 | 0 | 0 |
| `tests/workflow/test_graph_and_expressions.py` | 13 | 0 | 0 |
| `tests/workflow/test_runtime_execution.py` | 16 | 0 | 0 |
| **Total** | **365** | **0** | **2** |

The two skips are the PostgreSQL integration tests, skipped by design when
`DRILLAI_TEST_POSTGRES=1` is not set (`tests/db/test_persistence.py:236,251`).

### End-to-end pass/fail list (this checkpoint)

`e2e/well-cockpit.spec.ts`, 4 tests, all executed against the real backend:

| # | Test | Result |
| --- | --- | --- |
| 1 | lists the seeded well and opens a cockpit that matches the API | **PASS** |
| 2 | shows measured progress, NPT and missing-data honesty from the API | **PASS** |
| 3 | the operation timeline merges documents, operations and events | **PASS** |
| 4 | deep links survive a reload and an unknown well is a not-found, not a blank page | **PASS** |

The remaining journeys (documents → evidence, workflow studio, run monitor, approval, failing node,
WebSocket, context persistence, permissions, RTL/accessibility, error-matrix surfaces) are **not yet
automated** and are therefore not claimed.

---

## 4. Known limitations

- Only journey 1 is automated end to end. Everything else in the brief is implemented in the UI but
  unproven at the browser level.
- Persian covers the shell and cockpit strings, not every string in every workspace.
- There is no CI workflow file yet; the gate commands are documented in
  `docs/FRONTEND_TESTING.md` but are currently run by hand.
- No `ops/` deployment assets.
- The run-event WebSocket is verified manually only.
- Accessibility has been written for (roles, labels, `aria-live`, keyboard-reachable controls) but is
  not yet asserted by an automated test.

---

## 5. Next checkpoints

1. Automate the remaining journeys, highest value first: documents → ingestion → records → evidence;
   workflow create → configure → connect → validate → save → publish; run → node execution → events;
   approval approve/reject including refresh-while-waiting; and the failing-node journey.
2. Add the WebSocket journey with REST reconciliation, and the error-matrix surfaces
   (401/403/404/409/422/500/network/timeout/malformed).
3. Add permission journeys against backend authority, and RTL/accessibility checks.
4. Add the CI workflow (frontend install/typecheck/lint/test/build + backend tests/lint/migration
   check + the E2E suite), with no ignored failures, and verify it from a clean `npm ci`.
5. Finish the Persian catalogue and the remaining docs, then run the final certification from a clean
   checkout.

---

## 6. Final verification and final commit

Not yet applicable: the mission is not complete. When it is, this section will carry the re-run of
every gate at the final commit, the Git report (target branch, initial and final local HEAD, final
remote HEAD, match yes/no, clean tree, untracked files, unpushed commits, exact SHA), and the final
status — exactly `MISSION CLOSED — VERIFIED` or `MISSION BLOCKED — NOT COMPLETE`.
