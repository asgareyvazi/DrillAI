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
| Checkpoint 1 (docs) | `99a655e054285f0827f114c0514ab13c34038a18` | the report for checkpoint 1 |
| Session 3 baseline HEAD | `b3cd73d` | the commit this session's verification started from |
| Backend pause fix | `b8a88fe` | `human.approval` became a real pause; approvals publish what they asked for |
| Bootstrap mode fix | `67c9230` | `scripts/bootstrap.sh` executable in the index and on disk |
| Checkpoint 2 commit | `60adee04684843455868a1ae6a7c9f00ae3852b6` | run monitor on the real run contract, with the approval record |
| Checkpoint 2 report commit | `2df05239c51be41d986e68c4c1ec881522b5cb21` | the report for checkpoint 2 — **the last commit on the remote** |
| Checkpoint 3 backend commit | `043ea6460180d0f12060f782c7185a7a7418e663` | the run event log became resumable, and the socket that tails it is tested |
| Local HEAD | `043ea64` | at the remote tip; the report commit that follows this one sits on top |
| Remote HEAD | `043ea64` | `git ls-remote --heads origin arena/01a0dca0-drillai` — **matches local** |
| Publication state | — | **PUSHED AND VERIFIED** — `2df0523..043ea64` fast-forward |
| Last source (implementation) commit | `043ea64` | the last commit that changed product code or tests |
| Last test-producing commit | `043ea64` | the commit the unit and E2E numbers below were produced at |
| Last documentation-only commit | this report | changes no product code |
| Working tree | — | `git status --porcelain` empty at `043ea64`; no untracked files |
| Mission branch state | — | branch exists on the remote and contains every commit listed in §2 |

An earlier revision of this file described the state at `c9c0005`; the header above is the state at
the current commit. Results produced at one commit are never reported as evidence for another.
The environment was wiped twice during session 3 (a fresh clone at the grafted base `bfa066b` each
time); the recovery procedure and what survived are recorded in §0.1.

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

## 0.1 Session 3 — two environment resets, and how the repository was recovered

Both resets arrived without warning and left the same shape: a fresh clone checked out at the grafted
base `bfa066b28e0071880cb9191a4f1d47fdaa143e04`, the working-tree *files* intact, and no
`backend/.venv`, no `frontend/node_modules`, no `/tmp`. Nothing committed was ever lost — the branch is
the durable record, which is why every checkpoint is pushed before the next begins.

| Command | Result |
| --- | --- |
| `git status --short` | `?? backend/`, `?? frontend/`, `?? docs/`, `?? scripts/` — tracked history absent, files present |
| `git fetch --depth=50 origin arena/01a0dca0-drillai` | succeeded; branch history recovered |
| `git update-ref refs/remotes/origin/arena/01a0dca0-drillai FETCH_HEAD` | remote ref rebuilt |
| `git reset --mixed FETCH_HEAD` | local branch re-attached at the remote tip; **working tree preserved** (uncommitted work survived) |
| `bash scripts/bootstrap.sh` | backend venv, 400 npm packages, Chromium 153.0.8010.0 re-provisioned |

An earlier recovery of the same kind is recorded in §0. The lesson recorded there held: a lost session
never implies lost work, because the work was committed and pushed before the next batch began.


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

### Checkpoint 2 — Run lifecycle, and the approval record

- Commit `60adee04684843455868a1ae6a7c9f00ae3852b6` —
  `feat(frontend): run monitor on the real run contract, with the approval record (Checkpoint 2)`.
- Preceding commits in the same checkpoint, each pushed and verified before the next:
  `b8a88fe` (the `human.approval` pause and what an approver is shown) and `67c9230` (bootstrap
  executable in the index, not only on disk).

**Root causes found by running the product, not by reading it**

1. **A `human.approval` node never stopped the run.** The traversal discarded `NodeOutcome.approval`,
   so the seeded gate workflow "succeeded" unattended. Proven with a live probe against a seeded API
   (`201 succeeded`, `pending_approval_id: null`) before any code changed. Fixed in the runtime: the
   run parks in `waiting_approval`, the *executing* node's row is the record of the wait (a second row
   would make one execution look like two), and the approval carries the requester's own title,
   description, action level, required role, risk notes and evidence references.
2. **Everything behind an approved gate was skipped.** `nodes._approval` returned `branch=decision`,
   and `_apply_branch` treats a non-null branch as router semantics, so the plain edge after the gate
   was never taken. A decision is data, not a branch name. Fixed, with a regression test that fails
   against the pre-fix body (`issue` stayed `skipped` after the resume).
3. **`GET /runs/{id}` was typed as the run, not as the envelope.** The client read
   `RunSummary & {nodes…}` while the API returns `{run, workflow, node_runs, artifacts, events,
   pending_approval, resumable}` — which is why fields looked "missing" on a running run.
4. **Node runs were displayed by a field the API does not send.** `NodeRunState.name` vs the
   serializer's `node_name`, so every row silently fell back to the node id.
5. **A decision note was silently discarded.** The client sent `comment`; `ApprovalDecision` has no
   such field and Pydantic ignores extras, so `decision_note` stayed null and nothing complained.
   Canonical: `note` in, `decision_note` out.
6. **A decided approval became invisible.** The monitor asked for `status=pending` only, so the note
   and the conditions a person recorded disappeared at the moment they were recorded. `GET /approvals`
   gained a `run_id` filter and `status=any` is now used for the run's own record; the inbox gained a
   status filter for the same reason.
7. **`conditions` had two shapes.** The API accepted an object and stored a list; the same field was an
   object on one row and a list on the next. Now always the list the column holds.
8. **A run's hole section was not stored.** `POST /workflows/{id}/runs` accepted `section_id`, but no
   column existed — it survived only inside `context`. Model column + migration `9c1f4b7d5a20` (which
   backfills existing rows from the context blob) + runtime + serializer, and a test proving two wells
   keep their own scope and no stale scope leaks between runs.
9. **`Badge`/`Button`/`Card` swallowed the attributes callers passed.** `data-*` and `aria-*` props were
   dropped on the floor, which silently disabled test hooks and accessible labels.
10. **Copy defects:** the sidebar and the page were both named "Run Monitor" (two controls, one name);
    the run list tab asserted a count it did not show.

**Also fixed while proving the journey**

- `Table` gained an announced selected row (`aria-current`) instead of colour alone.
- A rejection requires a reason before it can be sent, and resume is refused while a decision is
  pending — the server refuses both, and the UI no longer offers them as if they would work.
- Values the server did not return are named (`Not returned`, `None`) instead of rendering as blanks.
- A catalogue-coverage test now fails when a `t('…')` key is missing from either language, which is how
  65 run-monitor/approval strings were found and translated rather than left as `⟦key⟧`.

### Checkpoint 3 — The stream a client can rely on (backend half)

- Commit `043ea6460180d0f12060f782c7185a7a7418e663` —
  `fix(realtime): make run event streams durable across resume and disconnect`.

The WebSocket endpoint carried a comment saying it was "verified manually", and manual verification
had missed two defects that only a client shows you. Both were found by writing the test the comment
implied, against the real application.

1. **The event log was not monotonic.** `WorkflowRuntime._event` numbered events from an instance
   counter that started at zero, and resuming a run builds a new runtime — so the resumed stretch of
   a suspended run repeated the sequence numbers its client had already consumed. A reader holding a
   cursor (`after_seq=N` on `GET /runs/{id}/events` and on the socket) then received **nothing at
   all**: the new rows are not *after* the cursor. The run looked healthy while the rest of its log
   was invisible, which is the exact failure the durable log exists to prevent.
   Fix: the runtime continues from the durable maximum (`_next_event_seq`, one lookup per run per
   instance); `(run_id, seq)` is now unique **in the schema** rather than by convention; and
   migration `7d5e1c4a90b2` repairs rows written by the old code before it adds the constraint —
   scoped to the runs that actually contain a repeat, renumbered deterministically in recorded order,
   merging nothing and deleting nothing. That ordering is proved against a database that *contains*
   duplicates, because that is what every existing deployment has.
2. **An abandoned socket outlived its client.** The handler only ever wrote, so nothing failed until
   the next write; a browser closing the tab left a task polling the event log once per interval,
   indefinitely, for a run nobody was watching. The handler now reads as well as writes and ends by
   itself within one poll when the client goes away.

The driver matters as much as the tests. `TestClient` tears its portal down by cancelling the
handler wherever it happens to be, and cancelling a task in the middle of SQLAlchemy's
greenlet-based aiosqlite bridge deadlocks the connection — the first version of this test file hung
the suite instead of failing it. `tests/api/ws.py` drives the same ASGI callable inside the test's
own event loop, speaking only what a browser can speak. It is the only WebSocket driver in the tree:
the `TestClient` attempt was removed rather than left beside it, and no helper survives from it except
one comment explaining why (in `ws.py`'s docstring). The driver also exposes `handler_state`, so a
test can insist the handler ended by **returning** — a handler that stops because something cancelled
it has not shown that it noticed the client leaving. The stream's own cleanup waits for the watcher
through `asyncio.wait` for the same reason: a genuine cancellation of the handler must not be
swallowed by its own tidy-up.

**The browser-shaped handshake.** A browser cannot set handshake headers, so the development identity
may now arrive as a query parameter (`?dev_roles=…`), read **below** the `auth_enabled` branch. A test
presents that parameter to an app built with authentication enabled and gets `4401` — the parameter
carries no authority there. An authorization journey (4403) is therefore reachable from a browser in
development without opening a production hole.

New backend tests at this commit (9): the socket replays the durable log in sequence order and closes
with the terminal status; reconnecting with `after_seq` loses no event and duplicates none; closing
the socket frees the server-side task (asserted by the fixture's own teardown); an unknown run is
4404 and a role without `workflow.read` is 4403; the server sends only documented frame types; a
repeated `(run_id, seq)` is refused by the database; the migration renumbers duplicates before
constraining; a cursor past the end means "up to date" and replays nothing; and a development role in
the query string grants nothing when authentication is on.

### Checkpoint 3, backend half — the durable report block

| Field | Value |
| --- | --- |
| Checkpoint | 3 — realtime: durable run-event streams (backend half) |
| Local starting SHA | `2df0523` (after the reset recovery; the pre-reset local tip `bfa066b` held no mission commits) |
| Remote starting SHA | `2df05239c51be41d986e68c4c1ec881522b5cb21` |
| Root causes | (1) event sequences restarted at 1 on resume, so a cursor-based client saw nothing after its cursor; (2) the socket handler never read from the client, so an abandoned connection kept polling forever; (3) the socket had no automated test at all — "verified manually" |
| Files changed | `backend/src/drillai/workflow/runtime.py`, `backend/src/drillai/db/models/workflow.py`, `backend/src/drillai/api/deps.py`, `backend/src/drillai/api/routers/runs.py`, `backend/tests/api/conftest.py`, `backend/tests/workflow/test_runtime_execution.py` |
| Files added | `backend/alembic/versions/7d5e1c4a90b2_run_event_sequence_unique.py`, `backend/tests/api/ws.py`, `backend/tests/api/test_run_event_stream.py`, `backend/tests/db/test_run_event_sequence_migration.py` |
| Migration | `7d5e1c4a90b2` (down_revision `9c1f4b7d5a20`): repair duplicate sequences, then `UNIQUE (run_id, seq)`; reversible |
| Tests | 8 WebSocket + 1 migration + 1 runtime monotonicity regression (the existing resume test gained the assertion) |
| Test counts | full backend **379 passed, 2 skipped**; WS file **8 passed** on three consecutive runs at `043ea64` |
| E2E | not re-run in this checkpoint — no frontend or API-contract change; the `60adee0` numbers remain the E2E evidence and are labelled as such |
| Commit SHA | `043ea6460180d0f12060f782c7185a7a7418e663` |
| Push result | `2df0523..043ea64  arena/01a0dca0-drillai -> arena/01a0dca0-drillai` (fast-forward, exit 0) |
| Remote SHA | `043ea6460180d0f12060f782c7185a7a7418e663` |
| Local == remote | yes (`git rev-parse HEAD` == `git ls-remote --heads origin arena/01a0dca0-drillai`) |
| Working tree | clean (`git status --porcelain` empty) |
| Untracked | none |
| Remaining blockers | none for this half; the frontend half of checkpoint 3 is the next work |
| Next checkpoint | checkpoint 3, frontend half: `frontend/src/lib/runEvents.ts`, Run Monitor integration retiring the 2 s poll, and the real-browser reconnect journey |

---

## 3. Test results

Every row below was produced by running the command shown, at the commit named in the row. Exact
counts, no rounding, and nothing is reported as "all good".

### At `043ea64` (Checkpoint 3, backend) — the current source commit

| Suite / command | Result |
| --- | --- |
| `.venv/bin/python -m pytest -q` (backend, full suite) | **379 passed, 2 skipped** |
| `.venv/bin/python -m pytest tests/api/test_run_event_stream.py` | **8 passed**, three consecutive runs after the driver gained the handler-state assertion (19 s, 16 s, 17 s; no failures) |
| `.venv/bin/python -m pytest tests/db/test_run_event_sequence_migration.py` | **1 passed** (data repair, not schema: see §2) |
| `.venv/bin/python -m pytest tests/workflow/test_runtime_execution.py` | **19 passed** (includes the monotonicity regression on resume) |
| `.venv/bin/ruff check .` (backend) | All checks passed |
| `alembic upgrade head` + `alembic check` (fresh database) | No new upgrade operations detected |
| `alembic downgrade -1` then `upgrade head` | both applied cleanly (the constraint can be removed and re-added) |
| Frontend (`npx tsc -b --noEmit`, `npx eslint`, `npx vitest run` 109 passed, `npm run build`) | unchanged since `60adee0` — no frontend source changed in this checkpoint |

The two skips are the PostgreSQL integration tests, skipped by design when `DRILLAI_TEST_POSTGRES=1`
is not set; the count is taken from the progress output (379 `.` + 2 `s`, no `F` or `E`).

### Environment reset 6 — and how the unpushed work survived it

This session began with another wipe: the checkout was a fresh grafted clone at `bfa066b` with a
single tracked file, the two commits that held the WebSocket work (`08ab942`, `323e29d` — reported as
unpushed in the previous session) were gone from local Git entirely, and `backend/.venv`,
`frontend/node_modules` and the Chromium build were missing.

Recovery, in order, without inventing anything:

1. `git fetch --depth=50 origin arena/01a0dca0-drillai` — authentication worked again (the token that
   blocked the previous session's push had been renewed), and the ref showed the remote tip was
   `2df0523`, not `60adee0`: `2df0523` is the checkpoint-2 *report* commit, a docs-only descendant.
2. `git update-ref refs/remotes/origin/arena/01a0dca0-drillai FETCH_HEAD`, then `git reset --mixed
   FETCH_HEAD` — the branch pointer moved to the remote tip while the working tree was left alone.
3. Result: the working tree held the entire WebSocket implementation as 11 changed/untracked files.
   Nothing was rewritten from memory and nothing was lost.
4. `bash scripts/bootstrap.sh` re-provisioned the virtualenv, the npm packages and Chromium.

So the work was never actually "recovered from the report" — the files survived, only the commits
that had held them did not. It is committed now as `043ea64` and pushed (`2df0523..043ea64`), which is
the point of the rule: until a commit is pushed, a wipe takes it.

### At `60adee0` (Checkpoint 2) — the frontend numbers

| Suite / command | Result |
| --- | --- |
| `npx tsc -b --noEmit` (frontend) | clean, 0 errors |
| `npx eslint src e2e --max-warnings=0` (frontend) | clean, 0 warnings, 0 errors |
| `npx vitest run` | **109 passed** in 10 files: `format` 18, `client` 12, `common` 15, `WellCockpit` 9, `workflowGraph` 12, `NodeConfigEditor` 12, `permissions` 10, `i18n/catalogue` 3, `i18n/usage` 3, **`RunMonitor` 15** |
| `npm run build` | clean (`vite build`, 3.03 s) |
| `node scripts/run-e2e.mjs` (Playwright, real API + real database + real build) | **23 passed / 0 failed** (4 cockpit, 4 documents, 9 workflow studio, **6 run monitor**) |
| `.venv/bin/python -m pytest -q` (backend, full suite) | **370 passed, 2 skipped** |
| `.venv/bin/ruff check .` (backend) | All checks passed |
| `alembic upgrade head` + `alembic check` (fresh database) | No new upgrade operations detected |

The two skips are the PostgreSQL integration tests, skipped by design when
`DRILLAI_TEST_POSTGRES=1` is not set. The pytest summary line is suppressed in this sandbox, so the
count is taken from the progress output (370 `.` + 2 `s`, no `F` or `E`).

New backend tests at this commit (4): the approval node suspends the run and records what it asked
for; a rejected approval stops the run before the next node; an approval node above the caller's
ceiling is still refused (an approval is not a bypass); a run records the scope it was started with —
two wells, two runs, each reading back its own scope. The existing L4 approval contract test gained
the `status=any` / `run_id` reads and the list-shaped `conditions` assertions.

New frontend tests at this commit (18): 15 component tests of the run monitor over payloads captured
from the running API, and 3 catalogue-coverage assertions that fail when a used key is missing.

#### The six new browser journeys (all PASS at `60adee0`)

| # | Spec | Test | Result |
| --- | --- | --- | --- |
| 1 | run-monitor | a published definition runs on a real well and stops at its approval gate | **PASS** |
| 2 | run-monitor | a decision taken by another identity resumes the run and is recorded with its note | **PASS** |
| 3 | run-monitor | a rejection cancels the run instead of resuming it | **PASS** |
| 4 | run-monitor | the approval inbox lists what is waiting and can be widened to decisions taken | **PASS** |
| 5 | run-monitor | the run list shows the runs the API returns, with their own scope and status | **PASS** |
| 6 | run-monitor | a run that fails says which node failed and why (real `failing-node-demo`) | **PASS** |

Journey 1 starts `Daily Drilling Intelligence` from the studio against the seeded well, asserts the
server parked the run at the gate, that every node execution the API recorded is shown by the name the
API gave it, that the approval card carries the title, description, action level, required role and
risk notes, and that a page reload reproduces the same state. Journey 2 decides as a *different*
identity (`well_manager`, while the supervisor started it), asserts the resume completed every node
that had been skipped, then reloads and reads the recorded decision — note, conditions and decider —
from the run's own approval record. Journey 3 rejects and asserts the run is `cancelled` with the
rejection named in its error and the downstream node still unrun.

### At `c9c0005` (Checkpoint 1) — superseded

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

- Journeys 1–4 are automated end to end (well/cockpit; documents/ingestion/evidence; the workflow
  studio lifecycle; and, new in checkpoint 2, the run lifecycle, the approval and rejection journeys,
  the approval inbox and the deterministic failing-node journey). Live events over the WebSocket, the
  error matrix, permission journeys, context persistence, RTL and accessibility are **not yet proven
  at the browser level** — they are checkpoint 3 onwards.
- The run monitor still refreshes a live run on a 2 s interval. That is a deliberate stopgap and it is
  documented in the source: checkpoint 3 replaces it with the events stream rather than running both.
  A run waiting for a human is not polled at all.
- `MISSION CLOSED` requires the WebSocket to be genuinely verified. It is not yet: the backend
  endpoint exists and its contract is recorded, but no browser journey has opened it.
- There is no CI workflow file yet; the gate commands are documented in `docs/FRONTEND_TESTING.md` and
  are currently run by hand (in this session, in full, at every checkpoint).
- No `ops/` deployment assets.
- Persian now covers the shell, cockpit, studio and run monitor; the catalogue-coverage test bounds
  what is left, and every remaining literal is one that test does not see (attributes rather than
  `t('…')` calls) — the RTL checkpoint finishes them.
- A run started from the editor pins the version the editor is showing. That is deliberate (the
  engine supports it and a draft run is a real capability) but the interface does not yet warn when
  the pinned version is not the published one; the context checkpoint is where that is finished.
- Accessibility has been written for (roles, labels, `aria-live`, keyboard-reachable controls, an
  announced selected row) but is not yet asserted by an automated test.
- The workspace fixtures under `frontend/src/test/fixtures` are captured from the running API and
  trimmed deterministically (a ~52 KB generated context prompt appears in several places in one
  payload; strings over 2000 characters become a marker). Keys, types and short values are untouched.

---

## 5. Next checkpoints

1. **Checkpoint 3 — live events.** The server half is at `043ea64` (above): the log is resumable, the
   socket is tested, and the browser-shaped handshake works without opening a production hole. What
   remains is the client half: a reusable WebSocket layer (`frontend/src/lib/runEvents.ts`, not socket
   code inside a component) covering the `stream_opened` / `run_event` / `stream_idle` /
   `stream_closed` / `stream_error` contract, deduplication by `run_id + seq`, bounded reconnect
   backoff, REST reconciliation as the source of truth, an honest connection indicator, `RunMonitor`
   integration that retires the 2 s poll only once the stream proves out, and a real-browser journey
   in which a forced disconnect loses no event and duplicates none.
2. **Checkpoint 4 — the error matrix** (401/403/404/409/422/500/network/timeout/malformed), with
   recovery, and never "backend unreachable" for a deliberate abort.
3. **Checkpoint 5 — permissions, context, RTL, accessibility**: the real role catalogue against
   backend authority, deep links and reload context, query-key scoping, Persian/RTL across the five
   surfaces, and keyboard/`aria` assertions on the real journeys.
4. **Checkpoint 6 — CI and certification**: `.github/workflows/` running install → typecheck → lint →
   unit → build → backend tests → backend lint → migration check → real-stack E2E with no ignored
   failures, then the full certification from a clean checkout.

---

## 6. Final verification and final commit

Not yet applicable: the mission is not complete. When it is, this section will carry the re-run of
every gate at the final commit, the Git report (target branch, initial and final local HEAD, final
remote HEAD, match yes/no, clean tree, untracked files, unpushed commits, exact SHA), and the final
status — exactly `MISSION CLOSED — VERIFIED` or `MISSION BLOCKED — NOT COMPLETE`.
