# Frontend Mission — Drilling Intelligence Workspace

**Status: CHECKPOINT 5 — VERIFIED · CHECKPOINT 4 — VERIFIED · MISSION IN PROGRESS — NOT COMPLETE.**

> Checkpoint 5 (permissions and identity, context and deep links, RTL, accessibility) is complete and
> published: batches A–E, certified at the source/test commit `3141439`, with the acceptance criteria
> listed one by one in §2 and the full gate re-run at that commit. The mission itself is not closed —
> the closing brief's nine-section report and CI certification are not part of this checkpoint — so the
> mission statement below stays **IN PROGRESS — NOT COMPLETE**, and nothing here claims otherwise.

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
| Checkpoint 0 HEAD | `80fb61d` | the reconciled state the previous session ended on |
| Checkpoint 1 commit | `c9c0005` | workflow studio lifecycle, contracts fixed at the source |
| Backend pause fix | `b8a88fe` | `human.approval` became a real pause |
| Bootstrap mode fix | `67c9230` | `scripts/bootstrap.sh` executable in the index and on disk |
| Checkpoint 2 commit | `60adee0` | run monitor on the real run contract, with the approval record |
| Checkpoint 3 commits | `043ea64`, `dd6d311`, `618b9b4`, `1ec38b8`, `1fa5dd9` | the durable run-event log, the stream client, the monitor on it, the browser journeys, run switching and refusals |
| Checkpoint 3 report commits | `79ed0bc`, `d43f069`, `6e5e318` | documentation only |
| Checkpoint 4 commits | `3d719f0`, `572cc2c`, `bb15c60`, `e21530a`, `e1db229` | the error model, the backend contract and fault injector, the approval read, the browser matrix, the last eight silent reads |
| Checkpoint 4 completion commits | `4edff04`, `1584880`, `d9cefaf`, `9626647`, `1f68671`, `0297fd7` | journeys that can fail, the health badge and the dead-code audit, and three journeys whose preconditions are now facts |
| Last source (implementation) commit | `609e19f` | the last commit that changed product code (checkpoint 5, batch D) |
| Last test-producing commit | `3141439` | the commit the unit, browser and backend numbers in §3, §3.1 were produced at (checkpoint 5, batch E) |
| Checkpoint 4 certification | `8f859ca` | the durable record of checkpoint 4: counts, journeys, mutation table, git gate |
| Checkpoint 5 commits | `bc25b1f`, `583f80a`, `51a5bb4`, `609e19f`, `7b1c110`, `3141439` | identity and permissions · context and deep links · RTL · accessibility primitives · the integrated certification |
| Local HEAD | the commit carrying this row | the report for checkpoint 5; the source/test commit the numbers were produced at is `3141439` |
| Remote HEAD | the commit carrying this row | `git ls-remote --heads origin arena/01a0dca0-drillai` is the authority |
| Publication state | — | **PUSHED AND VERIFIED** — every commit of this checkpoint is on the remote; a push failure occurred mid-checkpoint (see the publication table in §2) and was resolved, and the failure itself is recorded rather than dropped |
| Working tree | — | clean (`git status --porcelain` empty); no untracked files |
| Checkpoint 4 status | — | `CHECKPOINT 4 — VERIFIED` (source/test commit `0297fd7`) |
| Checkpoint 5 status | — | **`CHECKPOINT 5 — VERIFIED`** (source/test commit `3141439`; criteria listed in §2) |

An earlier revision of this file described the state at `c9c0005`; the header above is the state at
the current local commit, which is also the remote HEAD.

**Environment reset 10, and the re-created commits.** The sandbox was reset a tenth time while this
checkpoint's last three commits were still local: the repository was left on the grafted base commit
`bfa066b` with no CP4 history at all, and the toolchain (the backend virtualenv, `frontend/node_modules`
and the Playwright Chromium build) was gone. The working tree survived, so the *content* of the
unpublished commits survived with it. Recovery, in order, before anything was changed: the working tree
was archived (`tar`, 305 files, checksummed); the remote branch was fetched and the index reconciled to
it (`git fetch --depth=200` → `git update-ref refs/remotes/origin/… FETCH_HEAD` → `git reset --mixed
FETCH_HEAD`, which touches no file); the resulting `git status` showed exactly three modified files and
nothing else, i.e. exactly the content of the lost commits. Those three were re-committed:

| previously reported (never published, now unreachable) | re-created as | content |
| --- | --- | --- |
| `48d3cd2` | `9626647` | `e2e/error-matrix.spec.ts` — the stale-decision journey |
| `23e29ba` | `1f68671` | `e2e/workflow-studio.spec.ts` — the draft journey |
| `7080def` | this report commit | the report itself |
| — | `0297fd7` | `e2e/run-events.spec.ts` — a premise fix found by the checkpoint-3 gate (below) |

The file contents are byte-identical to what the lost commits carried: they came from the same working
tree, which was backed up before the repository was touched, and the diff against the remote after
reconciliation was exactly those files and no others. Only the SHAs are new, and the re-created commits
say so in their own messages. Results produced at one commit are never reported as evidence for another.
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

### Checkpoint 3 — The stream a client can rely on

Three commits, in the order they were built: the durable log and the socket that tails it
(`043ea64`), the reusable client (`dd6d311`), the run monitor on it (`618b9b4`), the browser
journeys (`1ec38b8`) and their hardening (`1fa5dd9`).

#### The client (`frontend/src/lib/runEvents.ts`)

The wire protocol as a discriminated union, field for field as the server sends it, decoded by
`decodeRunStreamFrame`, which returns *ignored with a reason* rather than throwing — an unknown frame
type from a newer server, a frame for another run, a sequence that is not a number. `runEventStreamUrl`
derives the socket URL from the configured API base (relative base → page origin, absolute base → its
own host, `ws`/`wss` by scheme) and passes the token, or the development role, as a query parameter
because a browser cannot set handshake headers; the development role is sent only outside a production
build, whatever the session store holds. `mergeRunEvents` deduplicates by `run_id + seq` and orders by
that sequence, because the same durable event legitimately arrives through REST, through the socket,
and again after a reconnect. `RunEventStream` becomes `live` only when the protocol says
`stream_opened`, resumes from the cursor the caller hands it, stops reconnecting on `stream_closed`
and on a refusal (4401/4403/4404 — waiting cannot change an answer the server has already given), and
`dispose` closes the socket, cancels the pending retry and leaves the instance inert.

#### The screen (`useRunEventStream`, `RunMonitor`)

The two-second interval is gone. The hook merges live events into the run's own query key and
reconciles that query against REST after a burst, on every reconnect and once when the run ends, so
the authoritative envelope still comes from REST. The indicator reports the transport unedited and
shows the cursor it has applied (`Live · seq 27`): "Live" alone is a claim, a sequence number is
evidence. A dropped socket reads *Realtime disconnected — showing saved history*; it is never called
an unreachable backend, because REST may still be answering and the history on screen is still real.

Two product problems surfaced only in the browser:

* **The identity had to become a real dependency.** TanStack Query's structural sharing means a
  refetch that returns identical data does not re-render, so a socket could outlive the identity that
  opened it. The identity is now subscribable (`subscribeIdentity`/`identityKey` in `api/client.ts`)
  and read through `useSyncExternalStore`. The identity-switch journey caught this; it is not
  theoretical.
* **A stream that ends because the run ends keeps saying so.** Resetting the indicator to "not
  streamed" after a terminal close erased the moment the operator was watching for; a monitor opened
  on an already-finished run still starts idle.

`waiting_approval` and `paused` runs are streamed. A parked run changes without this page doing
anything — a decision taken in the inbox, a resume by another operator — and that is exactly the
event a monitor must not miss. The alternative would be an interval under the socket, which is the
stopgap this checkpoint removed.

#### The browser evidence

`socket opens → events arrive → the transport is dropped → the server produces more events → the
client reconnects with its cursor → the page equals the API log`.

The disconnect is forced through Playwright's WebSocket routing, which connects to the real server and
forwards both directions: the socket is real, the server is real, and only the failure is injected.
The journey was checked against a deliberate regression — with the resume cursor forced back to `0`
(the defect this checkpoint fixes), the journey fails. A test that cannot fail is not evidence.

Five journeys: the drop-and-recover above; leaving the monitor closes its socket and opens no further
ones; switching identity replaces the socket instead of reusing another principal's stream; switching
runs closes A's socket and never mixes the two logs; and a run that has already finished is not
streamed at all.

---


#### The backend half — commit `043ea64`

Commit `043ea6460180d0f12060f782c7185a7a7418e663` —
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

### Checkpoint 3 — the durable report block

| Field | Value |
| --- | --- |
| Checkpoint | 3 — realtime: a durable run-event stream, end to end |
| Local starting SHA | `bfa066b` (a fresh grafted clone after environment reset 6) → `2df0523` after recovery |
| Remote starting SHA | `2df05239c51be41d986e68c4c1ec881522b5cb21` |
| Root causes | (1) event sequences restarted at 1 on resume, so a cursor-based client saw nothing after its cursor; (2) the socket handler never read from the client, so an abandoned connection kept polling forever; (3) the socket had no automated test at all — "verified manually"; (4) the run monitor polled every 2 s because no client existed for the stream; (5) a changed identity left the old principal's socket open |
| Files changed | the backend four above, plus `frontend/src/api/client.ts`, `frontend/src/hooks/useRunEventStream.ts`, `frontend/src/pages/workflow/RunMonitor.tsx`, `frontend/src/i18n/{en,fa}.ts`, six captured fixtures |
| Files added | the migration and three backend test files above, plus `frontend/src/lib/runEvents.ts`, `frontend/src/hooks/useRunEventStream.ts`, their test files, and `frontend/e2e/run-events.spec.ts` |
| Migration | `7d5e1c4a90b2` (down_revision `9c1f4b7d5a20`): repair duplicate sequences, then `UNIQUE (run_id, seq)`; reversible |
| Tests added | 8 WebSocket + 1 migration data-repair + 1 runtime monotonicity regression + 36 stream + 9 hook + 6 monitor tests + 5 browser journeys |
| Test counts at `1fa5dd9` | backend **379 passed, 2 skipped**; Vitest **161 passed** in 12 files; Playwright **28 passed** / 0 failed (23 pre-existing + 5 new) |
| E2E provenance | the 28-pass run was made at `1fa5dd9`, after the last source change; the 23 pre-existing journeys were also re-run at that commit |
| Commits | `043ea64` backend, `dd6d311` client, `618b9b4` integration, `1ec38b8` browser, `1fa5dd9` hardening |
| Push result | five fast-forwards from `2df0523`, each verified with `git ls-remote` |
| Remote SHA | `1fa5dd904ba39201bb60704c1e23d6ad4cfa309b` |
| Local == remote | yes (`git rev-parse HEAD` == `git ls-remote --heads origin arena/01a0dca0-drillai`) |
| Working tree | clean (`git status --porcelain` empty); untracked files: none |
| Remaining blockers | none |
| Next checkpoint | 4 — the error matrix (400/401/403/404/409/422/500/network/timeout/malformed/abort) with recovery, and never "backend unreachable" for a deliberate abort |

### Checkpoint 4 — the error matrix, and every surface that used to lie

| Field | Value |
| --- | --- |
| Checkpoint | 4 — one error model, every failure named and recoverable, and no read that reports a quiet success |
| Local starting SHA | `6e5e318` (the report-only tip of checkpoint 3) |
| Remote starting SHA | `6e5e318` |
| Commits | `3d719f0` the client and the shared error surface; `572cc2c` the backend contract and the fault injector; `bb15c60` the approval read; `e21530a` the browser matrix; `e1db229` the last eight silent reads; `4edff04`, `1584880`, `9626647`, `1f68671`, `0297fd7` the journeys; `d9cefaf` the health badge and the audit |
| Root causes | (1) a non-2xx answer was classified by status alone, so a 404 could read as an outage and a 403 as a sign-in problem; (2) a deliberate abort was wrapped in `ApiError(0, …)`, which is indistinguishable from an unreachable backend; (3) a deadline was a network failure with a message; (4) the timeout lived in a helper that did not abort the fetch, so the server kept working; (5) eight reads had neither `.error` nor an `<Async>` boundary and rendered `null`, `undefined`, `[]` or a fallback as if the read had succeeded; (6) nothing could produce the failures a healthy server cannot (an unhandled 500, a body that is not the contract, a deadline) in a real browser without intercepting HTTP |
| The model | `ApiError` (`frontend/src/api/client.ts`) carries `status`, `code`, `kind`, `requestId`, `retryable`, `path`, `contentType`, `messageFromServer`. `classify()` is the only place a status becomes a kind (`invalid_request`, `unauthenticated`, `forbidden`, `not_found`, `conflict`, `validation`, `server`, `network`, `timeout`, `malformed`, `cancelled`, `unknown`); `ApiErrorExtras.kind` overrides it only at the transport boundary. The backend answers with `DrillAIError(code, http_status, retryable, message, details)` → `{"error": {code, message, retryable, details, trace_id?}}` and sends `X-Request-ID` |
| Retry policy | one place: `shouldRetryRequest` (transport failures and 5xx only, two automatic attempts) in `createQueryClient()`; `canRetry` is its manual counterpart, and `ErrorState` renders "Retry" only when `canRetry`. A 409 offers "Reload the current state" (`error-reconcile`), never a retry |
| Abort | a caller abort is re-thrown untouched and is never classified as network; the raw `AbortError` is recognised by `isAbortError` (client and `ErrorState`), so a cancellation renders nothing |
| Timeout | at the transport boundary only: a deadline aborts the fetch (`DOMException(…, 'TimeoutError')`) and is classified `timeout`, distinct from a caller's cancellation and from an unreachable backend |
| Fault injection | `POST /__faults/{arm,disarm}`, `GET /__faults/armed` (`backend/src/drillai/api/routers/faults.py`), modes `unhandled|slow|malformed`, `MAX_SLEEP_MS = 60_000`, mounted only when `settings.e2e_faults` is set; the server refuses the flag in production. The middleware is innermost, so injected responses still carry `X-Request-ID` and `X-Response-Time-Ms`. Nothing in the browser intercepts an HTTP request |
| Files changed | `frontend/src/api/client.ts`, `frontend/src/api/endpoints.ts`, `frontend/src/api/queryClient.ts`, `frontend/src/components/common/index.tsx`, `frontend/src/components/evidence/EvidencePanel.tsx`, `frontend/src/components/layout/AppShell.tsx`, `frontend/src/pages/engineering/{Engineering,Optimisation}Workspace.tsx`, `frontend/src/pages/wells/{WellList,DocumentWorkspace}.tsx`, `frontend/src/pages/workflow/{WorkflowStudio,RunMonitor}.tsx`, `frontend/src/i18n/{en,fa}.ts`, `backend/src/drillai/api/{app.py,routers/faults.py}`, `backend/src/drillai/core/config.py`, plus the tests named below |
| Tests added | 18 backend contract tests (`tests/api/test_error_contract.py`); 19 component tests for the eight silent reads; 3 tests for the health badge; 9 browser journeys (A–J); the client/query-client/cancellation suites |
| Test counts at `0297fd7` | Vitest **237 passed** in 20 files; Playwright **38 passed / 0 failed** (33 main + 4 faults + 1 auth, 2.5 m); backend **397 passed, 2 skipped, 0 failed, 0 errors** of 399; `ruff check .` clean; `alembic check` on a fresh database: "No new upgrade operations detected"; `npm run build` 263.55 kB (71.15 kB gzip) |
| Working tree at the gate | clean, no untracked files |
| Publication | all commits pushed to `arena/01a0dca0-drillai`; `git ls-remote` == local HEAD — see §5.2 |
| Remaining blockers | none for this checkpoint |

#### The eight reads that reported a quiet success (commit `e1db229`)

A scan for `useQuery` bindings that were referenced neither through `.error` nor through an `<Async>`
boundary found exactly eight; they are the surfaces from the code, not a list copied from a brief.

| Binding | What it did | What it does now |
| --- | --- | --- |
| `EvidencePanel.summary` | a failed strip read produced no strip at all, which is indistinguishable from "this run recorded no evidence" | the strip opens with `data-summary-state="failed"` and says the summary could not be read; no counts are invented; the rest of the panel still renders; retry only when `canRetry` |
| `AppShell.well` | a failed well read fell back to the product name, which reads as "no well is open" | the header shows the route's id plus "well details could not be read" (`shell-well-name`/`shell-well-detail`) |
| `EngineeringWorkspace.wellbores` | an empty scope list silently produced "sent against the well alone" | `engine-scope-failure` says the list could not be read and the run will be sent against the well alone; retry invalidates both scope queries |
| `EngineeringWorkspace.sections` | a failed section read looked like "no sections" | `engine-scope-failure` says the run will be sent without a section |
| `OptimisationWorkspace.wellbores` | as above | `optimise-scope-failure` with the same honesty about what will be sent |
| `OptimisationWorkspace.sections` | as above | `optimise-scope-failure`, retry re-reads the sections |
| `WellList.projects` | a failed project read produced a subtitle that read as a real inventory, including a fabricated `0 wells` | `projects-unavailable` card; the subtitle prints only what answered |
| `WorkflowStudio.identity` | a failed permission read made every check answer "no", and the publish button claimed the identity lacks `workflow.publish` | `publishChecking` while loading and `cannotPublishUnknown` when the read failed, plus an `identity-unavailable` notice with retry |

New visible strings were added to **both** catalogues (`en.ts`, `fa.ts`): `app.wellNotRead`,
`app.healthChecking|healthUnreachable|healthNotAnswering|healthError`, `common.projects`,
`wells.projectsUnavailable|projectsHint`, `cockpit.evidenceSummaryUnavailable`,
`engineering.scopeUnavailable|scopeSectionUnavailable|scopeRetry`,
`optimisation.scopeUnavailable`, `workflow.publishChecking|cannotPublishUnknown`, and earlier
`errors.reloadState`.

#### The health badge (commit `d9cefaf`)

The audit of the error surfaces found one that checkpoint 4 had not touched: the shell's health badge,
which is on every screen, called **every** failure "API unreachable" and was the only error text not
going through the catalogue. A 500 from `/health` or a deadline now reads as an API error or as "not
answering"; a real transport failure still reads as unreachable. Three component tests hold the three
apart, and the label comes from the catalogue in both locales.

#### The nine browser journeys

Nothing here is mocked: the page issues ordinary requests, and a second instance of the same API —
same application, same database schema, fault endpoints enabled by configuration and refused in
production — answers them badly or not at all. The second frontend build differs only in
`VITE_API_TIMEOUT_MS=3000`, so a deadline is reachable inside a journey.

| # | Journey | Spec | What it proves |
| --- | --- | --- | --- |
| A | a deep link to a run that does not exist | `error-matrix` | 404 is `not_found` with a navigation way out, not an outage |
| B | a real 401 with `www-authenticate: Bearer` | `error-auth` | "you are not signed in", not "backend unreachable"; the locked identity is signed out of the workspace |
| C | an identity that may not decide an approval | `error-matrix` | 403 names the permission, offers no retry and no reconcile, and leaves the approval on screen |
| D | an upload the server rejects | `error-matrix` | 422 reports what was wrong with the file; no fake success notice |
| E | a decision somebody else already took | `error-matrix` | 409 with "reload the current state"; after reconciling, the record shows the standing decision and the second decision changed nothing |
| F | an unhandled server fault | `error-faults` | 500 reads as a server failure, quotes the request id the server really sent, and retries successfully |
| G | the network goes away | `error-matrix` | `network` with no invented status; recovery when the network returns |
| H | a deadline the server is still holding | `error-faults` | `timeout`, the request really cancelled at the transport, a late answer cannot replace the screen, retry recovers |
| I | a response that is not the contract | `error-faults` | `malformed` with a request id, and no retry offer |
| J | a read the operator navigates away from | `error-faults` | no error state, no console noise, no stale overwrite, the abandoned request reported failed and never finished |

#### Mutation certification (the six required mutations)

Each mutation was applied to the real source, the named test was run and had to fail, the source was
restored with `git checkout`, the test had to pass again, and the diff was inspected. No mutation was
committed; `git status --porcelain` was empty after each pair.

| # | Mutation | Target | Result |
| --- | --- | --- | --- |
| M1 | a caller abort is no longer re-thrown (`if (false && (options.signal?.aborted \|\| isAbortError(cause))) throw cause`) so it becomes `ApiError(0, 'network.unreachable')` | `api/client.ts` | **Caught at the unit level**: `src/api/client.test.ts` fails 3 of 38 — "passes a caller abort through instead of dressing it up as an outage", "aborts before the first byte when the caller has already cancelled", "keeps a caller abort distinct from a deadline that fired on the same request". Journey J still passes, and that is a property of React Query, not of the client: it drops a cancelled query before any render, so the classification never reaches the DOM. Probed directly: with a *mounted* observer (switching the approval filter from `pending` to `any` while the read is in flight) the screen shows 0 error states under both the clean and the mutated client. Recorded as the unit contract, not claimed as journey evidence |
| M2 | the deadline classification removed (`if (false && timedOut)`) | `api/client.ts` | **Caught by journey H**: expected `data-error-kind="timeout"`, received `network`. Restored → passed |
| M3 | 403 collapsed into 401 (`case 403: return 'unauthenticated'`) | `api/client.ts` | **Caught by journey C**: expected `forbidden`, received `unauthenticated`. Restored → passed |
| M4 | 404 read as a network failure (`case 404: return 'network'`) | `api/client.ts` | **Caught by journey A**: expected `not_found`, received `network`. Restored → passed |
| M5 | the caller's signal no longer forwarded (`if (false && options.signal)`) | `api/client.ts` | **Caught by journey J**: "an abandoned read reported: finished:after, failed:after" — the read the operator walked away from completed instead of being cancelled. Restored → passed |
| M6 | the request id no longer retained (`this.requestId = null`) | `api/client.ts` | **Caught by journey F**: `the id on screen must be one the server sent — screen said "platform.internal_error", server sent req_…, req_…, req_…`. Restored → passed |

Two of these mutations were only caught after the tests were made able to fail, which is the point of
running them:

- Journey J originally required *an* abandoned request to exist, which an incidental abort of the same
  URL satisfied; under M5 the armed read **finished**. The journey now marks the moment of the
  switch and requires the request abandoned there to be reported **failed after the switch** and never
  **finished after the switch**, with the fault's 2 s delay kept below the client's 3 s deadline so
  the deadline cannot be what ended it (`4edff04`, `1584880`).
- Journey F originally required the request-id line to be non-empty — which the error *code* alone
  already satisfied, so a client that dropped the id passed. It now compares what the screen shows
  with the ids the server really put on its answers: the first attempt (dropping only the `X-Request-ID`
  header) was still not caught, because the id also arrives in the error envelope; the mutation that
  removes the retained id is the one the journey now fails on (`1584880`).
- `page.on('requestfailed')` also reports the run's event-stream socket teardown, so the collector
  filters to `resourceType() === 'fetch'`; otherwise journey J would pass for the wrong reason.

#### The audits (commits `d9cefaf`, and the re-run of checkpoint 3's guarantees)

| Audit | Result |
| --- | --- |
| A second error taxonomy | none: one `ApiError`, one `classify()`, one retry policy |
| `fetch()` or `response.ok` outside the API layer | none (the only raw `fetch` is inside `api/client.ts`) |
| `AbortController` outside the transport boundary | none |
| Retry/timeout helpers | `backoffDelays` in `lib/runEvents.ts` had no production caller and its test asserted a copy of the formula the stream never used; removed, and the test now drives the stream through ten failed connections and asserts the delays it handed to its own timer (`d9cefaf`; verified by flattening the real timeline → 3 tests red) |
| Dead test helpers | `selectUnits` and `cardWithText` in `e2e/fixtures.ts` had no caller anywhere in the repository; removed |
| Duplicate fault injectors | one (`backend/src/drillai/api/routers/faults.py`) |
| Route interception in acceptance E2E | no HTTP interception anywhere; two `routeWebSocket` uses, both deliberate and socket-level: the checkpoint-3 forced disconnect, and journey E's cut of the stale screen's stream |
| Debug leftovers | no `.only`, `.skip`, `.todo`, `debugger`, `console.log` or TODO/FIXME outside the backend's documented ones; no snapshots |
| Old error strings | no hard-coded "backend unreachable" outside the catalogue and the client's own `network` message |
| Duplicate translations | none: 415 keys in each catalogue, identical paths, no duplicate path in either file (the catalogue test asserts parity and non-empty leaves) |
| Unused components or hooks | none — every `src` module is imported, and every `lib`/`hooks` export has a caller or a test |

Checkpoint 3's guarantees were re-run on this tree as a regression gate before the final numbers were
taken: ordered application, dedupe, reconnect, `after_seq`, burst reconciliation, terminal handling,
run switching, identity change, named refusals and the real-browser forced disconnect. Evidence:
`src/lib/runEvents.test.ts` 36 passed, `src/hooks/useRunEventStream.test.tsx` 9 passed,
`e2e/run-events.spec.ts` 5 passed in a real browser, and 28 backend tests
(`tests/api/test_run_event_stream.py`, `tests/db/test_run_event_sequence_migration.py`,
`tests/workflow/test_runtime_execution.py`) passed with 0 failures. No regression was found, so no
new checkpoint-3 test was added.

#### Three journeys whose preconditions were hopes (commits `9626647`, `1f68671`, `0297fd7`)

Both failures appeared for the first time inside full runs — each had passed in isolation before —
and both were premise failures rather than product failures:

- **Journey E (the stale decision)** assumed the second operator's screen was still out of date when
  its decision was sent. It was not: the screen's live stream reconciles it within a frame, and the
  journey only passed while it outran that reconciliation; in a full run the first screen's redraw
  took longer than the second screen's five-second inbox refresh, the approve button went away and
  the click timed out. The second screen's stream is now cut with `routeWebSocket` (socket-level, no
  HTTP mock, server untouched) and the first decision is confirmed against the server instead of
  against a redraw of the other screen. Verified by three consecutive runs, then the full suite.
- **The draft journey** took the first two rows of the workflow list and asserted that an edit made
  the draft dirty. Journeys earlier in the same file save drafts containing exactly the node it adds,
  so whether the graph changed depended on the run order. It now creates both definitions, saves the
  first edit as v1, and edits a node type that version does not contain — so the dirty state is a
  fact. The shared readiness gate (`openStoredWorkflow`) waits for the version label the server sent:
  the "saved" badge is rendered from the first frame, before the stored graph arrives, and an edit
  made in that window is thrown away by the load.

- **The finished-run journey (checkpoint 3's, found by the checkpoint-3 gate below)** asserted that
  loading a run which has already finished opens no stream socket — but it sampled the socket count on
  the screen that had *started* the run, which is the screen watching it while the API finishes it. That
  screen opens its socket when the read that said "parked" is answered, so on a loaded machine the socket
  opened after the sample and the count was attributed to the wrong page. Diagnosed by instrumenting a
  scratch copy of the file: the failing repeat logged `SAMPLE before=0` followed 45 ms later by
  `OPEN page=/runs?run=<that run> url=/api/v1/runs/<that run>/events/stream?after_seq=12`. The run is now
  finished while the page is on the list (no run open, nothing streaming), the sample is taken there, and
  a settle window precedes the comparison so a late socket is still caught. Product behaviour is
  unchanged: a fresh load of a terminal run opens no socket and reports `data-stream-state="idle"`.

All three are recorded here because a green suite that passes for the wrong reason is not evidence, and
because two of the three were only reachable inside a full run: each had passed in isolation.

---

### Checkpoint 5 — permissions, context, RTL and accessibility

The brief for this checkpoint is one product-level problem stated in four areas, and its premise is that
identity, permission, action level, deep links, context, locale, direction, focus, keyboard operation and
live data are correctness properties rather than polish. It is delivered as five batches, each committed
and published on its own. **Batches A–C are published; Batch D is committed locally and its push is
blocked by an invalid GitHub token (below). Batch E — the integrated certification — has not been run
yet, so no checkpoint-5 verdict is stated here.**

| Batch | What it changed | Commit | Gate at that commit |
| --- | --- | --- | --- |
| A — identity and permissions | the four states of a refused action (permission missing · permission present but ceiling insufficient · sufficient · approval required) rendered from the server's own answer, the dev identity switch terminating the old socket, and the identity's cached answers being rejected on a switch | `bc25b1f` (22 files, +1513/−91) | Vitest **261 passed** in 22 files · Playwright **45 passed** / 0 failed · `npm run build` **268.38 kB (gzip 72.80)** · backend **397 passed, 2 skipped** · `ruff check .` clean |
| B — context and deep links | unknown `?workflow=` / `?document=` / `?run=`, a document belonging to another well, and a failed (non-404) detail read: each named for what it is, none of them rendered as an outage, and the run cursor reset when the run in the address changes | `583f80a` (8 files, +386/−13) | new spec `e2e/context-deeplinks.spec.ts` **6 passed**; suite **51 journeys passed** / 0 failed · Vitest **262 passed** in 22 files · `tsc -b --noEmit` clean · `eslint .` clean |
| C — RTL | one technical-string rule (`[dir='rtl'] .font-mono { direction: ltr; unicode-bidi: isolate }`), `Json` rendered `dir="ltr"`, a stable `locale-switch` handle, and the workflow canvas left to the direction `@xyflow/react`'s own stylesheet sets | `51a5bb4` (5 files, +220/−1) | new spec `e2e/rtl.spec.ts` **4 passed**; suite 55 journeys passed (with B) · Vitest **262 passed** in 22 files · `tsc` and `eslint` clean · build **271.25 kB (gzip 73.64)** |
| D — accessibility primitives | table rows as controls, `Tabs` owning its panel, `Drawer`'s focus lifecycle, and one polite live region for run events | `be2f0cf` (14 files, +1116/−33) | Vitest **275 passed** in 23 files · Playwright **61 passed** / 0 failed · `tsc -b --noEmit` clean · `eslint .` clean · build **274.82 kB (gzip 74.88)** · backend **397 passed, 2 skipped** · `ruff check .` clean · `alembic check` (after `alembic upgrade head`): "No new upgrade operations detected" |
| E — integrated certification | three journeys that cross the areas against each other, plus one more accessibility guard, and the full gate re-run at the certification commit | `7b1c110`, `3141439` (2 files, +330/−2) | Playwright **65 passed / 0 failed** · Vitest **275 passed** in 23 files · `tsc` clean · `eslint .` clean · build **274.82 kB (gzip 74.88)** · backend **397 passed, 2 skipped** · `ruff check .` clean · `alembic check`: "No new upgrade operations detected" |

#### What each batch actually asserts

**A — the four states a refused action can be in.** The interface never grants authority and never keeps
a second copy of the role catalogue: it renders the server's answer. A missing permission, a permission
that is present but above the identity's level ceiling, a sufficient identity, and an action that is
allowed but requires approval are four different surfaces, not one "denied" path. Switching identity ends
the previous principal's WebSocket, discards that identity's cached answers and refetches — with no full
page reload, because a reload would hide exactly the state that leaked.

**B — context is never invented, and a mismatch fails safely.** The URL's well and a record's own
`well_id` are compared before the record is shown; a run scope is historical and is displayed as the
server recorded it rather than re-interpreted through the current page's context; a workflow is not
well-bound, because `workflow.well_id` does not exist. A 404 is an answer (`ApiError.status === 404`, so
"no retry button"), while any other failure of the same read is a rendered `ErrorState` with a retry.

**C — direction is more than `dir="rtl"`.** Persisted values, identifiers and signed quantities stay in
their own direction and keep their sign; engineering numbers are compared against the engine's answer
after digit mapping rather than against a translation; the graph's geometry is byte-identical between
locales because the canvas library pins its own direction — asserted, not assumed. A `dir="ltr"` wrapper
added defensively to the canvas was removed again after the mutation that deleted it did **not** fail any
journey, and the dependency's own rule (`@xyflow/react@12.12.0`, `dist/style.css:4`) was recorded instead.

**D — keyboard and assistive technology.** The critical journey is walkable without a mouse: a tab group
is entered once and moved through with arrow keys whose meaning follows the reading direction (in Persian
the next tab is to the left), `Home`/`End` work, a table row that opens a record is focusable and
activates on `Enter` and `Space` without scrolling, and the row that is open says so through
`aria-current`. The evidence drawer makes its `aria-modal` claim true — focus moves in, Tab cycles inside
(reading the focusable set at the moment of the key press, so a control that appears while it is open is
reachable), `Escape` closes, and focus returns to the opener. Run events are announced once per burst by a
polite region that never takes focus, never scrolls, and — when the resumption removes the button the
operator just pressed — hands focus to the badge that reports the outcome instead of dropping it on the
document body. A sweep of the accessibility tree over five screens found one unnamed control (a progress
bar), which is now labelled by the label drawn above it.

#### Mutation certification (checkpoint 5)

Each mutation was applied to the working tree, run, observed to fail, and reverted; the tree was then
verified byte-identical to its pre-mutation content. A mutation that does not fail its guard means the
guard is decorative, and two were handled that way rather than reported as passing.

| # | Mutation | Guard | Result |
| --- | --- | --- | --- |
| M1 | `resetQueries` → `invalidateQueries` on identity switch | identity E2E | failed, restored |
| M2 | `resetQueries` → `clear` | identity E2E | failed, restored |
| M3 | evaluate permission presence before the level ceiling | identity E2E | failed, restored |
| M4 | send the action as a raw `POST` without the client's authorization path | identity E2E | failed, restored |
| M5 | remove the run-cursor reset (`cursorRunRef`) | `useRunEventStream.test.tsx` — "starts a different run at that run's own cursor" | failed (`after_seq=4` vs `after_seq=1`), restored |
| M6 | remove `[dir='rtl'] .font-mono` | `e2e/rtl.spec.ts` | failed (direction resolved `rtl`), restored |
| M7 | remove the canvas `dir="ltr"` wrapper | `e2e/rtl.spec.ts` | **did not fail** — the wrapper was inert, so it was deleted and the library's own rule documented |
| M8 | remove `dir="ltr"` from `Json` | `e2e/rtl.spec.ts` | failed (`null`), restored |
| M9 | remove the table row's tab stop | `a11y.test.tsx` and the browser journey | failed in both, restored |
| M10 | make the tab arrows ignore the reading direction | `a11y.test.tsx` (RTL case) and the Persian browser journey | failed in both, restored |
| M11 | drop `aria-controls` from the tabs | `a11y.test.tsx` | failed, restored |
| M12 | announce the log a screen opens onto (history as news) | `RunMonitor.test.tsx` | failed after the guard was strengthened (below), restored |
| M13 | count a burst by its newest event only | `RunMonitor.test.tsx` | failed, restored |
| M14 | remove the dialog's focus restore | `a11y.test.tsx` and the browser journey | failed in both, restored |
| M15 | give every table row a key derived from `Date.now()` (remounting the table on each render) | the refresh journey in `e2e/a11y.spec.ts` | **did not fail the first version of the guard** (an unchanged poll re-renders nothing, so there was nothing to break) — the journey now forces a real change into the list, and the mutation fails: "the row survived the refresh as the same node"; restored byte-identical |

Two of these changed the code rather than the confidence in it, which is the point of running them:

* **M7 deleted a line.** The canvas wrapper could not be made to fail any journey because
  `@xyflow/react` sets `direction: ltr` on `.react-flow` itself. Rather than keep an attribute whose only
  effect was to look like a safeguard, it was removed and the journey now asserts the *resolved*
  direction.
* **M12 and M13 found defects in the live region, not in the test.** The coalescing window was originally
  cleared and re-armed on every effect run, so the REST reconcile that follows a burst — a re-render
  carrying no new event — cancelled the announcement that was about to describe it; and the burst count
  looked only at the newest event, under-reporting the common case where several events arrive in one
  render. The window is now opened by the first event of a burst and closed by a timer, and the count is
  taken from the log's own sequence numbers. The first version of M12's guard also passed when it should
  not have, because the assertion ran before the window could close; the guard now waits past the window,
  and the mutation fails — a guard that cannot fail is not a guard.

#### Batch E — the areas certified against each other

Per-area specs cannot see the gaps between areas: a refusal explained only in one locale, a deep link
that survives a reload only in one direction, a keyboard journey that stops working once the page is
right-to-left. `e2e/checkpoint5.spec.ts` (three journeys) exercises them together against the real
backend:

1. **identity + context + direction**: a run started from the studio and opened while the interface is
   Persian — the run in the address is the run on screen, the scope shown is compared with the API's own
   envelope rather than with the page, every visible monospace string resolves LTR, the ASCII mapping of
   the page carries the id the API returned, and a reload reproduces all of it, still RTL. The event log
   is then compared against the server's events for that run.
2. **identity + direction**: a refusal read by someone whose interface is Persian — the explanation
   carries the action level and ceiling the *server* reports for the same identity and the same request
   (an L0 viewer refused an L2 action), the level codes are not translated, switching to the supervisor
   changes the control's state **in place with zero main-frame navigations** while the locale stays
   Persian, and the request the button would send is authorised by the server rather than merely drawn
   as authorised.
3. **context + accessibility**: a deep link naming a document that belongs to another well — refused in
   the page's own words and naming both ids, present in the accessibility tree (`ariaSnapshot`), the
   document's own title absent from the page, and no Retry offered for an answer that will not change.

Two defects were found by building this batch, and both were fixed rather than worked around:

* **`selectRole` could not switch identity in Persian.** The helper looked the control up by its label
  ("Acting as"), which is translated, so a Persian user's own action was untestable. It now uses the
  control's `shell-role-switch` handle, the same in both locales.
* **A focus-stability guard that could not fail.** The first version of the "a refresh does not move the
  reader's focus" journey waited seven seconds and asserted the focused row was still focused. The
  mutation that should have broken it — a row key derived from `Date.now()`, which remounts the table on
  every render — did **not** fail it, because React Query's structural sharing returns the same object
  for an unchanged poll, so nothing re-rendered. The journey now starts a run behind the page's back so
  the refresh genuinely changes the list, and the mutation fails (`the row survived the refresh as the
  same node`), first, before being restored to a byte-identical file.

#### The client holds no second copy of the authority (checked, not assumed)

The rule is that the backend is the only authorization authority and the interface must not keep a
second role catalogue. Checked against the tree rather than asserted in prose:

* no role key appears in frontend production code — `grep -rn "drilling_supervisor\|integrity_engineer\|
  well_manager\|data_manager" src` matches a comment in `src/stores/session.ts` and nothing else; the
  other matches are test fixtures;
* the identity switcher's options are the server's `development_presets` (journey: the offered options
  equal the server's list, `+1` for the session's own echo) and the ceiling shown is the server's
  `max_action_level` for every catalogued role;
* `src/lib/permissions.ts` implements the *pattern* semantics (`*`, `.*`, `.**`) so the interface does
  not offer an action certain to be refused; it grants nothing, protects no route, and is asserted
  against the server's own behaviour in the journeys;
* the only level-keyed table on the client is `ACTION_LEVEL_LABELS` in `src/lib/format.ts`, which maps a
  server value (`L3`) to its label (`L3 · propose`) and a rank for comparison — presentation of the
  server's value, not a source of authority.

#### Publication state, in the order it happened

| # | Event | Evidence |
| --- | --- | --- |
| 1 | Batches A, B and C pushed and verified | `git ls-remote` at each batch: `bc25b1f`, `583f80a`, `51a5bb4` |
| 2 | Batch D committed locally; **push failed** | `fatal: could not read Username for 'https://github.com': terminal prompts disabled`; `gh auth status`: the `GH_TOKEN` token is no longer valid |
| 3 | The remote was read without credentials | `GET https://api.github.com/repos/asgareyvazi/DrillAI/branches/arena/01a0dca0-drillai` → `"sha": "51a5bb4a0645d8a004b72d7fe01244c026a27bf0"`, parent `583f80a` — so the remote was exactly at batch C, and the local stale `refs/remotes/origin/…` (`bc25b1f`) was **not** evidence of anything |
| 4 | Environment reset **14** destroyed the local history again | repository rewound to the grafted base `bfa066b`; working tree intact (Batch D and the report edits), toolchain gone |
| 5 | Recovered by the proven sequence | working tree archived first (`/tmp/prereset14/worktree.tar`, 316 entries) → `git fetch --depth=200` → `git update-ref refs/remotes/origin/… FETCH_HEAD` → `git reset --mixed FETCH_HEAD` (**never** `--hard`); `git status` then showed exactly the content of the lost commits and nothing else |
| 6 | Batch D re-created and **pushed** | `609e19f` (the original local `be2f0cf` was never published); `git ls-remote` → `609e19f09ecd5b4bf82098ef09d18e061c2b8f2a` |
| 7 | Batches E pushed | `7b1c110`, `3141439`; remote verified with `git ls-remote` after each |

The re-created commit says so in its own message. Content that was never published is re-created, never
renamed: the report names the original SHA (`be2f0cf`) as unreachable and the new one (`609e19f`) as the
published fact.

#### Checkpoint 5 — the acceptance criteria, one by one

| # | Criterion | Where it is proven |
| --- | --- | --- |
| 1 | The backend is the sole authority; the UI grants nothing | Batch E journey 2 (the server authorises the very request the screen reports as ready); the client-holds-no-catalogue check above |
| 2 | No second role catalogue | switcher options == server `development_presets` (+1 echo); no role key in production code |
| 3 | Action level ≠ permission: four distinct states | missing permission (`no-permission`, names the permission and the role), ceiling insufficient (`level-below`, names both levels), sufficient (`ready`, and the server runs it), approval required (the run monitor's pending-approval card, the blocked resume, and the approval inbox journeys) |
| 4 | Identity switching: old socket terminated, old answers rejected, refetch, no reload | the two identity journeys (socket count and request identity), and Batch E journey 2's zero main-frame navigations |
| 5 | Query keys scoped only where the response varies | the approval read is scoped to the run (`scopes the approval read to the run being shown`); Batch B audited every `queryKey` in the tree |
| 6 | Context never invented; mismatch fails safely | cross-well document refused with both ids named; unknown workflow/document/run each named for what it is; the run's scope is the server's recorded scope |
| 7 | Run scope is historical; workflows are not well-bound | `shows the scope the run was started against, naming what was not returned`; the well-free workflow journey |
| 8 | RTL is more than `dir="rtl"` | the four RTL journeys (layout, numerals, technical strings, graph geometry) plus Batch E journey 1 |
| 9 | Engineering numbers stay numerically correct | the NPT total equals the API after digit mapping; the ASCII mapping of the page carries the API's id |
| 10 | React Flow graph semantics never mirrored | node transforms byte-identical between locales, asserted on the resolved direction the library itself sets |
| 11 | Keyboard-only operation of the critical journey | Batch D journeys 1–4 and 6, and the component tests |
| 12 | Real focus management, no focus theft | the drawer's focus lifecycle; the deliberate hand-over after resuming; the refresh journey (a real re-render leaves the reader where they were) |
| 13 | Live regions that are not noisy | one polite, atomic region per burst, silent for history, never focused, asserted at 600 ms coalescing |
| 14 | Native semantics before ARIA | the arrow-key journey operates real buttons and their panel; the unnamed-control sweep over five screens (which found and fixed one unnamed progress bar) |
| 15 | Real browser, real backend, real database; fixtures seeded | every journey above; the fixture ids come from the seed, and the numbers are read from the API rather than pasted |
| 16 | Mutations that fail and are restored | M1–M15 in the mutation table — including two that changed the code (M7, and the M15 correction) and one that exposed a guard which could not fail |
| 17 | Commits published and verified | the publication table above; `git ls-remote` equals the local tip |

**CHECKPOINT 5 — VERIFIED** at the source/test commit `3141439` (the commit the numbers in the batch
table were produced at). The mission is not closed: the nine-section final report and the CI
certification of the closing brief are not part of this checkpoint, so the mission statement remains
**MISSION IN PROGRESS — NOT COMPLETE**.

---

## 3. Test results

Every row below was produced by running the command shown, at the commit named in the row. Exact
counts, no rounding, and nothing is reported as "all good".

### At `3141439` (Checkpoint 5) — the certification commit

Measured on the working tree at `3141439e90e9ac38897dd4cd9a6527ba30dcd033`, after the last source or test change of this checkpoint and
with no later edit. Every command below was run at that state; nothing here is inferred.

| Command | Result |
| --- | --- |
| `npm run typecheck` (`tsc -b --noEmit`) | clean, no output |
| `npx eslint .` | clean |
| `npx vitest run` | **275 passed** in **23 files**, 0 failed, 0 skipped |
| `npm run build` | `dist/assets/index-BRM9qJCU.js` **274.82 kB** (gzip **74.88 kB**), built in 3.78 s |
| `npm run e2e` | **65 passed / 0 failed** (3.6 m), including the 7 accessibility journeys and the 3 integrated checkpoint-5 journeys |
| `python -m pytest` (backend) | **397 passed, 2 skipped, 0 failed** in 187.71 s |
| `ruff check .` (backend) | clean — "All checks passed!" |
| `alembic check` (after `alembic upgrade head`) | "No new upgrade operations detected" |

Journey counts by area, at this commit: identity and permissions **7**, context and deep links **6**,
RTL **4**, accessibility **7**, checkpoint-5 integrated **3**; the remainder are the checkpoints 1–4
journeys, unchanged. The full suite is one command, so all 65 ran together against the same seeded
database.

### At `0297fd7` (Checkpoint 4) — the final source/test commit

Measured on the working tree at `0297fd7849bf9a4f6e164de862974bedec30fc00`, which is the last commit
that changed a test, with a clean tree apart from the report. The last commit that changed product code
is `d9cefaf`; `0297fd7` and the two commits before it are test-only. Every number below was produced by
the command shown, at this commit, in this session, after the environment reset and the recovery
described above — none of them is carried over from an earlier commit or from an earlier report.

| Gate | Command | Result at `0297fd7` |
| --- | --- | --- |
| Frontend types | `npm run typecheck` (`tsc -b --noEmit`) | no output, exit 0 |
| Frontend lint | `npx eslint .` | no output, exit 0 |
| Frontend unit/component | `npm run test` | **237 passed** in 20 files (was 234 in 20 files at `e1db229`; the three new ones are the health badge) |
| Frontend build | `npm run build` | built in 4.97 s; `index-D3VwJPYx.js` 263.55 kB (gzip 71.15 kB), `flow-*.js` 185.77 kB, `react-*.js` 165.65 kB, CSS 37.01 kB |
| Browser (all three stacks) | `npx playwright test` | **38 passed, 0 failed** (2.5 m): 33 against the development-identity stack, 4 against the fault stack, 1 against the authentication stack |
| Backend tests | `.venv/bin/python -m pytest -q --junitxml=…` | **399 tests: 397 passed, 2 skipped, 0 failed, 0 errors**. Both skips are the Postgres-only tests (`tests.db.test_persistence`), skipped because no Postgres server exists in this environment |
| Backend lint | `.venv/bin/ruff check .` | "All checks passed!" |
| Migration | fresh database → `alembic upgrade head` → `alembic check` | "No new upgrade operations detected." |
| Checkpoint 3 realtime suite (backend) | `pytest tests/api/test_run_event_stream.py tests/db/test_run_event_sequence_migration.py tests/workflow/test_runtime_execution.py -q` | 28 passed (`............................  [100%]`) |

The `38 passed` browser figure is the checkpoint-3 regression gate and the checkpoint-4 error matrix in
one run: 33 journeys on the development-identity stack (including all five `run-events.spec.ts` stream
journeys), 4 on the fault stack (the deadline, the malformed body, the unhandled fault, the deliberate
cancellation) and 1 on the authentication stack (a real 401). It is also the restored-state evidence for
mutations M2–M6 below: those mutations were applied and reverted one at a time, and this run is the
whole suite passing on the restored code — a stronger check than re-running each targeted journey.

Per-suite counts, as produced: backend 399 (`test_error_contract.py` 18, `test_run_event_stream.py` 8,
`test_run_event_sequence_migration.py` 1, `test_runtime_execution.py` 19, remainder pre-existing);
frontend unit 237 across 20 files (`runEvents.test.ts` 36, `client.test.ts` 38,
`queryCancellation.test.tsx` 9, `AppShell.test.tsx` 6, `EvidencePanel.test.tsx` 3,
`EngineeringWorkspace.test.tsx` 3, `OptimisationWorkspace.test.tsx` 3, `WellList.test.tsx` 4,
`WorkflowStudio.test.tsx` 3, `catalogue.test.ts` 3, `usage.test.ts` 3, and the pre-existing suites);
browser 38 (main 33, faults 4, auth 1).

### At `1fa5dd9` (Checkpoint 3) — the current source commit

Measured on the working tree at `1fa5dd904ba39201bb60704c1e23d6ad4cfa309b`, after the last commit that
changed product code or tests. Each row's provenance is the commit named in it.

| Suite / command | Commit | Result |
| --- | --- | --- |
| `npm run typecheck` (frontend) | `1fa5dd9` | clean, no diagnostics |
| `npx eslint .` (frontend) | `1fa5dd9` | clean, no output |
| `npx vitest run` (frontend, full suite) | `1fa5dd9` | **161 passed**, 12 files |
| `npm run build` (frontend) | `1fa5dd9` | built in 4.26 s; `index-DButXYdn.js` 249.49 kB / gzip 67.67 kB |
| `node scripts/run-e2e.mjs` (Playwright, real backend) | `1fa5dd9` | **28 passed**, 0 failed, 0 skipped |
| `node scripts/run-e2e.mjs e2e/run-events.spec.ts` (the new journeys alone) | `1ec38b8` + run-switch journey at `1fa5dd9` | **5 passed** in 26.1 s |
| `.venv/bin/python -m pytest -q` (backend, full suite) | `1fa5dd9` (no backend change since `043ea64`) | **379 passed, 2 skipped** |

The five browser journeys, named:

| Journey | What it proves |
| --- | --- |
| the socket opens, streams, survives a drop, and ends up equal to the API log | events arrive live; the transport is dropped mid-run; the server keeps producing events with no browser watching; the client reconnects with its cursor; the page's log equals `GET /runs/{id}/events` exactly, once each, in sequence order; a reload shows the same log |
| leaving the run closes the socket instead of leaving it watching | navigating away disposes the stream: the socket closes and no further socket is opened |
| changing identity replaces the socket rather than reusing another principal's stream | an identity change opens a new socket under the new identity and closes the old one |
| moving to another run closes the first socket and never mixes the two logs | run A's socket closes when run B is opened, B's socket names B, and the page shows B's log only |
| a run that has already finished is not streamed at all | a terminal run gets no socket and reads `idle`, not a stale "live" |

### At `043ea64` (Checkpoint 3, backend) — superseded by `1fa5dd9`

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

### At `60adee0` (Checkpoint 2) — superseded

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
  the approval inbox and the deterministic failing-node journey). Live events over the WebSocket are
  proven at the browser level (checkpoint 3, five journeys) and the error matrix is too (checkpoint 4,
  ten journeys across three stacks). Permission journeys, context persistence, RTL and accessibility
  are **not yet proven at the browser level** — they are checkpoint 5 onwards.
- Checkpoint 4's abort guarantee is proven at the unit level, not in a journey, and that is recorded
  rather than papered over: React Query drops a cancelled query before any render, so a client that
  wrapped an abort as a network failure would not change what any screen shows. The evidence is
  `src/api/client.test.ts` (three failures under the mutation) plus a probe with a *mounted* observer
  that showed the same 0 error states under both the clean and the mutated client.
- Three journey preconditions were found to be properties of the run rather than facts and were
  rebuilt (`9626647`, `1f68671`, `0297fd7`); every one of them had passed in isolation and failed
  inside a full run. A green suite that passes for the wrong reason is not evidence, which is why the
  mutation table and these three fixes are in this report rather than only in the commit messages.
- Unlocalized literals remain in surfaces checkpoint 4 did not audit (`PlatformPage`'s "ready"/
  "unreachable" badge, "Declared input ports", "Computed objectives", "Not returned", …). They are
  pre-existing, they are not new strings introduced here, and they are the i18n/RTL checkpoint's
  scope; every string this checkpoint added went into both catalogues.
- The run monitor no longer polls: the 2 s interval was retired in checkpoint 3 (`618b9b4`), together
  with `LIVE_RUN_STATUSES`, and the replacement is the stream plus a REST reconciliation after bursts,
  on reconnect and on terminal. Runs in `waiting_approval` and `paused` are streamed as well — a
  parked run can change without this page doing anything.
- The WebSocket is verified: a real server, a real socket in a real browser, a forced disconnect, and
  a final log equal to the API's. Still **not** verified: token expiry mid-stream, a server restart, a
  proxy that closes with an unusual code, and a refusal after a role change — refused connections are
  tested by code (4401/4403/4404) but not against a real identity that loses its role while connected.
- The frontend unit tests cover the stream's states, cursor, deduplication, reconnect URL,
  out-of-order frames, wrong-run frames, unknown frames, terminal close and disposal; the browser
  journeys cover the product-level behaviour. Nothing in the stream is verified only by a mock.
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

1. ~~**Checkpoint 3 — live events.**~~ **Done**, in five commits: `043ea64` (the durable log and the
   tested socket), `dd6d311` (the reusable client), `618b9b4` (the run monitor on it, poll retired),
   `1ec38b8` (the browser journeys), `1fa5dd9` (run switching and named refusals). All pushed and
   verified on the remote.
2. ~~**Checkpoint 4 — the error matrix**~~ **Done and verified in this tree**, in ten commits:
   `3d719f0` (the client model and the shared error surface), `572cc2c` (the backend contract and the
   config-guarded fault injector), `bb15c60` (the approval read), `e21530a` (the browser matrix),
   `e1db229` (the last eight silent reads), `4edff04`/`1584880` (journeys that can fail),
   `d9cefaf` (the health badge and the dead-code audit), `9626647`/`1f68671`/`0297fd7` (three
   preconditions that were hopes). All of them are published and verified on the remote (§5.2).
3. **Checkpoint 5 — permissions, context, RTL, accessibility**: the real role catalogue against
   backend authority, deep links and reload context, query-key scoping, Persian/RTL across the five
   surfaces, and keyboard/`aria` assertions on the real journeys.
4. **Checkpoint 6 — CI and certification**: `.github/workflows/` running install → typecheck → lint →
   unit → build → backend tests → backend lint → migration check → real-stack E2E with no ignored
   failures, then the full certification from a clean checkout.

---

## 5.1 The git gate, run at `79ed0bc` (checkpoint 3)

`git ls-remote` is what settles whether a commit is published; the local remote-tracking ref is a
cache and can lag. It did lag here — `refs/remotes/origin/arena/01a0dca0-drillai` still pointed at
`2df0523` after the pushes, which made `git log origin/…..HEAD` show seven "unpushed" commits that
were in fact all on the remote. Fetching and re-pointing that ref (`git fetch --depth=50 origin
arena/01a0dca0-drillai` → `git update-ref refs/remotes/origin/… FETCH_HEAD`) resolved it; the
verification below uses `ls-remote` as the authority and the tracking ref only as a convenience.

| Check | Command | Result |
| --- | --- | --- |
| Branch | `git branch -vv` | `* arena/01a0dca0-drillai 79ed0bc` (tracking `origin/arena/01a0dca0-drillai`) |
| Local HEAD | `git rev-parse HEAD` | `79ed0bcbdc3c4500c3e8e0249064a5fe85ac0a12` |
| Remote HEAD | `git ls-remote --heads origin arena/01a0dca0-drillai` | `79ed0bcbdc3c4500c3e8e0249064a5fe85ac0a12` |
| Local == remote | — | **yes** |
| Unpushed commits | `git log --oneline origin/arena/01a0dca0-drillai..HEAD` | 0 |
| Difference from remote | `git diff --stat origin/arena/01a0dca0-drillai...HEAD` | empty |
| Working tree | `git status --porcelain` | empty |
| Untracked files | `git status --porcelain` | none |
| Shallow clone | `git rev-parse --is-shallow-repository` | `false` (29 commits reconciled) |
| Tracked files | `git ls-files` | 231 |
| Source lines | `git ls-files '*.py' '*.ts' '*.tsx' | xargs wc -l` | 61 002 |
| Secrets in the index | `git ls-files | grep -iE '\.env|credential|secret|\.pem|\.key$'` | none |
| Ignored-but-present artefacts | `frontend/dist`, `.e2e/`, `node_modules`, `backend/.venv` | not tracked (`.gitignore`) |

The branch is the only one this work touches; `main` is untouched at `bfa066b`.

---

## 5.2 The git gate at the checkpoint-4 commit — published and verified

The gate commands were run at `0297fd7` with a clean tree (§3). Publication is a normal fast-forward
push: no force, no amend, no rewritten history, no second remote, no credential written anywhere. The
earlier authentication failure was resolved by the user reconnecting GitHub in the environment; it was
never worked around.

| Check | Command | Result |
| --- | --- | --- |
| Branch | `git branch -vv` | `* arena/01a0dca0-drillai` tracking `origin/arena/01a0dca0-drillai` |
| Local HEAD before the report commit | `git rev-parse HEAD` | `0297fd7849bf9a4f6e164de862974bedec30fc00` |
| Remote HEAD before the report commit | `git ls-remote --heads origin arena/01a0dca0-drillai` | `0297fd7849bf9a4f6e164de862974bedec30fc00` |
| Unpushed commits (before the report commit) | `git log --oneline origin/arena/01a0dca0-drillai..HEAD` | empty |
| Push | `git push origin arena/01a0dca0-drillai` | fast-forward, `d9cefaf..0297fd7` |
| Working tree | `git status --porcelain` | empty apart from this report, which is the commit that carries it |
| Untracked files | `git status --porcelain` | none |
| Shallow clone | `git rev-parse --is-shallow-repository` | `false` for the branch history fetched to depth 200 |
| `main` | `git log --oneline -1 main` | `bfa066b` — untouched, and one commit behind nothing (this branch is not merged into it) |
| Branch discipline | `git branch -a` | `main` and `arena/01a0dca0-drillai` only; no branch was created, renamed or deleted |
| Secrets in the index | `git ls-files | grep -iE '\.env|credential|secret|\.pem$|\.key$'` | none |
| Tracked files / source lines | `git ls-files` / `git ls-files '*.py' '*.ts' '*.tsx' | xargs wc -l` | 245 files / 64 856 lines |

What was published, exactly — the commits that had been local when authentication failed, plus the
rest of the checkpoint's tail:

| Commit | What it is |
| --- | --- |
| `9626647` | `e2e/error-matrix.spec.ts`: the stale-decision journey is stale on purpose |
| `1f68671` | `e2e/workflow-studio.spec.ts`: the draft journey owns its precondition |
| `0297fd7` | `e2e/run-events.spec.ts`: the finished-run journey stops counting the wrong screen's socket |
| `8f859ca` | the durable record: the checkpoint-4 certification, the counts, the mutation table and this gate |
| the commit carrying this row | a one-row update naming `8f859ca`, so the report says exactly which SHA holds the certification; the remote tip |

The three test commits were re-created after environment reset 10 destroyed the local history (§header);
their content is byte-identical to the commits they replace, and the previously reported SHAs
(`48d3cd2`, `23e29ba`, `7080def`) were never on the remote — they are superseded, not lost.

---

## 6. Final verification and final commit

Not yet applicable: the mission is not complete. When it is, this section will carry the re-run of
every gate at the final commit, the Git report (target branch, initial and final local HEAD, final
remote HEAD, match yes/no, clean tree, untracked files, unpushed commits, exact SHA), and the final
status — exactly `MISSION CLOSED — VERIFIED` or `MISSION BLOCKED — NOT COMPLETE`.

**CHECKPOINT 4 — VERIFIED.** Every acceptance criterion of the completion brief holds at this state:
the eleven failure classifications are distinct and proven in a real browser (A–J), the eight audited
reads are truthful, three weak journey preconditions were rebuilt and the mutations that prove them were
re-run, the cleanup is audited, and every intended commit is on the remote with `git ls-remote` equal to
the local tip (§5.2). The mission as a whole is unchanged: **MISSION IN PROGRESS — NOT COMPLETE**, with
checkpoint 5 (permissions, context, RTL, accessibility) and checkpoint 6 (CI and the final certification)
outstanding.

Provenance, stated separately because they are different things:

| | |
| --- | --- |
| Final source (implementation) commit | `d9cefaf` |
| Final test commit — every number in §3 was produced here | `0297fd7` |
| Final report (documentation) commit | `8f859ca` — the report content |
| Remote HEAD | the commit carrying this row, one report-only commit above `8f859ca` (`git ls-remote` is the authority) |
| Unpushed commits | none |
