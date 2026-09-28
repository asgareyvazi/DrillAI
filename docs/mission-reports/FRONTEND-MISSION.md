# Frontend Mission — Drilling Intelligence Workspace

**Status: IN PROGRESS — NOT COMPLETE.**

This file is the durable record of the mission. It lives in Git on purpose: a future session must be
able to resume from the repository alone, without the chat that produced it. Every checkpoint below
carries the commit it was produced at, and results from a different commit are not evidence for this
one.

Mission target branch: `arena/01a0dca0-drillai` (never `main`; no merge into `main`).
Remote: `origin` → `https://github.com/asgareyvazi/DrillAI`.

## Where the work stands right now

Five different things are easy to confuse, so they are named separately. Nothing in this file may
state "current commit" without saying which of these it means.

| | SHA | What it is |
| --- | --- | --- |
| Checkpoint 0 HEAD | `80fb61dd3a07312d2a56da99d63e658d19961616` | the reconciled state the previous session ended on |
| Checkpoint 1 commit | `c9c000593e2ded279127236193d53b4590ba90d5` | workflow studio lifecycle, contracts fixed at the source |
| Remote HEAD | `c9c000593e2ded279127236193d53b4590ba90d5` | `git ls-remote origin refs/heads/arena/01a0dca0-drillai` — **matches local** |
| Last source (implementation) commit | `c9c0005` | the last commit that changed product code or tests |
| Last test-producing commit | `c9c0005` | the commit the unit and E2E numbers below were produced at |
| Last documentation-only commit | `80fb61d` | this report — changes no product code |
| Working tree | — | `git status --porcelain` empty at `c9c0005`; no untracked files |
| Mission branch state | — | branch exists on the remote and contains every commit listed in §2 |

An earlier revision of this file described the state at `6d31861`; the header above is the state at
the current commit. Results produced at one commit are never reported as evidence for another.

---

## 0. Session 2 — verification performed before any change (Checkpoint 0)

The environment was wiped between sessions again: the sandbox came back with a fresh, shallow clone
and no `backend/.venv`, no `frontend/node_modules` and no prepared browser. The working tree files
were intact and `git status` was clean against the baseline commit, so nothing was lost — but the
mission branch had to be re-attached to the remote before any work continued.

Commands and their actual results:

| Command | Result |
| --- | --- |
| `git ls-remote origin` | `6d31861502a93832454de87348738a2f81f4110a  refs/heads/arena/01a0dca0-drillai`; `bfa066b… refs/heads/main` |
| `git fetch --depth=50 origin arena/01a0dca0-drillai` | succeeded; branch history recovered (13 commits visible) |
| `git reset --mixed FETCH_HEAD` | local branch re-attached at `6d31861`; `git status --porcelain` → **0 entries** (tree already matched the commit) |
| `git rev-parse HEAD` | `6d31861502a93832454de87348738a2f81f4110a` |
| `git branch -vv` | `* arena/01a0dca0-drillai 6d31861` (tracking established by push/pull after re-attach) |
| `git log --oneline -n 20` | 13 commits: `aa7ff9d` (platform foundation) → `6d31861` (docs) |
| `git ls-files | wc -l` | 203 tracked files |
| `git status --porcelain` | empty |
| `scripts/bootstrap.sh` | backend venv rebuilt, 400 npm packages installed, Chromium 153.0.8010.0 verified |
| `frontend: npx tsc -b --noEmit` | **PASS** |
| `frontend: npx eslint .` | **PASS** |
| `frontend: npx vitest run` | **54 passed** (4 files) — reproduces the Checkpoint 6/7 number |
| `backend: pytest -q` | **365 passed, 2 skipped** (138.8 s) — reproduces the recorded number |
| `backend: ruff check .` | **All checks passed** |

So the restored environment reproduces the previously recorded results at `6d31861` before any new
code was written. That reproduction is the starting point of this session, not evidence of the new
work: every result for the new checkpoints is produced again below at the commit it belongs to.

---

## 1. Baseline (repository verification, session 1)

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

### Checkpoint 6 — Documentation, and a repository with nothing outstanding

- Commits `a6d1cc8` — `docs: describe the frontend, its tests and the mission state in the repository`
- Contents: `docs/FRONTEND.md`, `docs/FRONTEND_TESTING.md`, `docs/mission-reports/FRONTEND-MISSION.md`
  (this file) and the `README.md` corrections.
- Effect: every artefact produced so far is committed **and** present on the remote branch, and
  `git status --porcelain` is **empty**. Before this checkpoint the frontend, the drilling domain and
  the test harness existed only in a working tree — the state this mission exists to eliminate.

### Checkpoint 7 — Documents, ingestion, records and evidence in a browser

- Commits `80e347c` (product fix) and `a85375e` (journey and harness), both pushed and verified.
- New journey: `e2e/documents-evidence.spec.ts`, 4 tests, run against the real stack.
- Defects it found and that are now fixed:
  1. every extracted row was rendered as the literal word "record" — the code read `record_kind`
     where the API sends `record_type`, and looked for an `extractor` field that does not exist
     (the API sends `method`/`method_version`);
  2. the extraction badge claimed "extractors: …" for a document from which nothing was extracted
     (the API reports the extractor **set that ran**, not the ones that produced rows);
  3. re-uploading identical bytes reused the existing document while the UI reported
     "ingestion started" — the pipeline's `skipped_duplicate` job is now stated as such;
  4. the API layer's document calls were `Record<string, unknown>` placeholders, now typed
     (`ExtractionRecord`, `IngestionJob`, `DocumentChunk`, `DocumentProvenance`).
- Harness honesty fixes: the Playwright config no longer reuses a running server on either port
  (a leftover server holds the previous database and the previous revision's modules), and
  `scripts/run-e2e.mjs` refuses to start when a port is occupied, naming the fix.

---

### Checkpoint 8 — The workflow studio lifecycle (Checkpoint 1 of the continuation brief)

- Commit `c9c0005`, pushed; local HEAD == remote HEAD at the time of writing.
- Test counts at this commit: Vitest 91 (8 files), Playwright 17/17, backend 365 passed + 2 skipped,
  `tsc`/`eslint`/`ruff` clean, `vite build` clean, `alembic check` clean.
- The studio could list and create definitions, but editing one lost data and publishing was not
  reachable from the interface at all. Both were real defects, not missing polish.

**Fixed at the source, in the backend, rather than worked around in the client**

1. `ValidationReport.is_valid` was a plain property, so it was **not** in any serialized report: the
   validate endpoint added it by hand and a stored version row had no such field. The studio read
   `validation.errors`, which existed neither on the wire nor in the model — a validation run blanked
   the page with `Cannot read properties of undefined`. `is_valid` is now a serialized computed field,
   and reports stored before the change are normalised on read (`_validation_payload`) instead of being
   rewritten in the database.
2. The version history payload carried no `created_at`, `created_by` or `change_reason`, so a version
   list could not say when a version was saved or why. Added.
3. `tests/api/test_api.py` now pins the report shape on both paths — the validate endpoint and the
   version stored by a save.

**Frontend**

4. `lib/workflowGraph.ts`: one mapping between the graph contract and the canvas. Every field the
   editor cannot display (`inputs`, `on_error`, `retry`, `timeout_seconds`, `is_breakpoint`, `notes`,
   `action_level`, edge `condition`/`label`/`is_loop_back`, graph `settings`/`description`) is copied
   forward, so saving an edited graph no longer deletes the rest of it. 12 tests.
5. `StudioNode` had no `<Handle>`s. React Flow had nothing to anchor an edge to: a loaded graph
   rendered as five disconnected boxes and no edge could be drawn or connected. Fixed, with the
   handle names kept as canvas wiring rather than invented values in the saved graph.
6. `NodeConfigEditor`: typing JSON no longer destroys what is being typed (the text is the source of
   truth while editing, the parse error is shown inline, and the config is committed on blur only when
   it parses); `config_schema` drives typed fields — names, required markers, primitives, enums,
   defaults, ranges — beside the JSON view, with "not set" kept distinct from `false`. 12 tests.
7. The editor loads the **newest saved version** explicitly. `GET /workflows/{id}` answers with the
   published version when there is one, which is right for a run and wrong for an editor: after saving
   a draft, reopening the workflow showed the published graph and the draft looked lost. The version
   strip now says which version is loaded and when it is not the newest, and any version can be loaded
   from the history (a read; saving from it creates a new version).
8. Publishing is exposed and permission-aware: the identity endpoint's permission **patterns** decide
   whether the buttons are offered, with the reason stated when they are not (`workflow.publish` is
   held by the supervisor and well-manager roles, not by the engineer's default identity), and the
   server refuses regardless — proven in the journey by a direct `POST /workflows/{id}/publish` that
   answers 403 for the engineer and succeeds for the supervisor. `lib/permissions.ts` mirrors
   `rbac._pattern_matches` case for case; 10 tests, plus the E2E cross-check.
9. Unsaved edits are protected: the browser's own confirmation on reload, and the studio's own dialog
   on switching definitions (stay, or discard and switch — the stored version is untouched either way).
10. Save semantics are the server's: an unchanged graph creates no version and the editor says so; an
    invalid graph saves as a draft the server refuses to publish; a real edit produces the next version
    number; the numbers on screen always come from the server.

**Proof (real stack, no mocks)**

New journey `frontend/e2e/workflow-studio.spec.ts`, 9 tests: create → empty editor that says so;
unchanged save → no new version → real edit → v2; add → configure from `config_schema` → connect →
validate (the server's own message) → save invalid draft → publish refused → fix `title` → validate
valid → save v2 → publish v2 → reload; every node and edge survives a save including the fields the
editor does not display; a draft is not discarded without being asked; a run carries the well it was
started for (a second well is created in the journey, and the same definition run against it is scoped
to *that* well); publishing is offered only to an identity the server accepts it from; the history
loads an older version without overwriting the newest; the palette is the node registry (every node
type the API returns appears, and the tab badge counts them).

---

## 3. Test results

Every row below was produced by running the command shown, at the commit named in the row. Exact
counts, no rounding, and nothing is reported as "all good".

At `c9c0005` (Checkpoint 1):

| Suite / command | Result |
| --- | --- |
| `npx tsc -b --noEmit` (frontend) | clean, 0 errors |
| `npx eslint .` (frontend) | clean, 0 warnings, 0 errors |
| `npx vitest run` | **91 passed** in 8 files: `format` 18, `client` 12, `common` 15, `WellCockpit` 9, `workflowGraph` 12, `NodeConfigEditor` 12, `permissions` 10, `i18n/catalogue` 3 |
| `npm run build` | clean |
| `npm run e2e` (Playwright, real API + real build) | **17 passed / 0 failed** (4 cockpit, 4 documents, 9 workflow studio) |
| `python -m pytest -q` (backend, full suite) | **365 passed, 2 skipped** (the two are the PostgreSQL-marked tests, skipped without a PostgreSQL instance) |
| `python -m ruff check .` (backend) | All checks passed |
| `alembic upgrade head` + `alembic check` (fresh database) | No new upgrade operations detected |

The pytest summary line is suppressed in this sandbox, so the count is taken from the progress output
(365 `.` + 2 `s`, no `F` or `E`). It matches the pre-checkpoint baseline of 365/2, which is the point:
this checkpoint added assertions to existing tests rather than new backend test functions.

| Suite | Command | Result |
| --- | --- | --- |
| Backend unit + API | `cd backend && source .venv/bin/activate && pytest -q` | **365 passed, 0 failed, 2 skipped** (104.1 s, re-run at `e3600c6`) |
| Backend lint | `cd backend && ruff check .` | **clean** |
| Migration drift | `alembic upgrade head` then `alembic check` | **no new upgrade operations detected** |
| Frontend types | `cd frontend && npm run typecheck` | **clean** |
| Frontend lint | `cd frontend && npm run lint` | **clean** |
| Frontend build | `cd frontend && npm run build` | **built** — 35.4 kB CSS, 165.7 kB react chunk, 185.8 kB flow chunk, 196.5 kB app chunk |
| Frontend unit + component | `cd frontend && npm test` | **54 passed** (4 files) |
| Frontend end-to-end | `cd frontend && npm run e2e` | **8 passed** (2 files, real API, real DB, real browser; run at `a85375e`, and the only change after it is documentation) |

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

`e2e/well-cockpit.spec.ts` and `e2e/documents-evidence.spec.ts`, 8 tests, all executed against the
real backend, a seeded real database and a real browser (results from commit `a85375e`):

| # | Spec | Test | Result |
| --- | --- | --- | --- |
| 1 | well-cockpit | lists the seeded well and opens a cockpit that matches the API | **PASS** |
| 2 | well-cockpit | shows measured progress, NPT and missing-data honesty from the API | **PASS** |
| 3 | well-cockpit | the operation timeline merges documents, operations and events | **PASS** |
| 4 | well-cockpit | deep links survive a reload and an unknown well is a not-found, not a blank page | **PASS** |
| 5 | documents-evidence | shows the extraction the backend actually performed on the seeded documents | **PASS** |
| 6 | documents-evidence | an extracted value can be traced back to its page and its evidence | **PASS** |
| 7 | documents-evidence | uploading a new report runs the real pipeline, and identical bytes are not ingested twice | **PASS** |
| 8 | documents-evidence | the upload assets are the backend's own synthetic fixtures | **PASS** |

The remaining journeys (documents → evidence, workflow studio, run monitor, approval, failing node,
WebSocket, context persistence, permissions, RTL/accessibility, error-matrix surfaces) are **not yet
automated** and are therefore not claimed.

---

## 4. Known limitations

- Journeys 1, 2 and 3 are automated end to end (well/cockpit, documents/ingestion/evidence, and the
  workflow studio lifecycle). The run lifecycle, live events, approvals, the error matrix, permissions
  and context persistence are implemented in the UI but not yet proven at the browser level.
- Persian covers the shell and cockpit strings, not every string in every workspace.
- There is no CI workflow file yet; the gate commands are documented in
  `docs/FRONTEND_TESTING.md` but are currently run by hand.
- No `ops/` deployment assets.
- The run-event WebSocket is verified manually only.
- The studio still holds a number of English literals that are not in the catalogue (the edge
  inspector's hints, several diagnostic `title` attributes, and the run-context explanatory lines).
  They are enumerated here rather than left implicit, and the RTL checkpoint covers them.
- A run started from the editor pins the version the editor is showing. That is deliberate (the
  engine supports it and a draft run is a real capability) but the interface does not yet warn when
  the pinned version is not the published one; the run-context journey is where that is finished.
- Accessibility has been written for (roles, labels, `aria-live`, keyboard-reachable controls) but is
  not yet asserted by an automated test.

---

## 5. Next checkpoints

1. Checkpoint 2 of the continuation brief: the run lifecycle in the browser — published workflow →
   select well → run → queued → running → node execution → final state, with the run list, the
   inspector, a deterministic failing-node journey, and reload reconstruction for running, completed
   and `waiting_approval` runs. (The studio lifecycle is done — checkpoint 8; documents → ingestion →
   records → evidence is checkpoint 7.)
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
