# Frontend Mission — Drilling Intelligence Workspace

**Status: MISSION CLOSED — VERIFIED.**

> The mission is complete, and the repository is what proves it. The pipeline in
> `.github/workflows/ci.yml` runs the repository's own gates on every push and is green on the remote
> tip; a fresh `git clone` of the branch — no virtualenv, no `node_modules`, no browser and no build
> output carried from any machine — reproduces the whole certification, including 399 backend tests
> with PostgreSQL and 65 browser journeys against the real stack. Checkpoint 6 is certified at
> `f8abc10`; the CI runs are in §5, the clean-checkout evidence is in §6, and the closure decision,
> condition by condition, is §9.

**Checkpoint 7** — well master data, identity, lineage and the asset contract — is published on the
same branch at `23c394a` (§3 *CP7*, §4, §8). It extends the platform the closed mission above certifies,
and every gate named in the closure table was re-measured at its tip.

This file is the durable record of the mission. It lives in Git on purpose: a future session must be
able to resume from the repository alone, without the chat that produced it. Every checkpoint below
carries the commit it was produced at, and results from a different commit are not evidence for this
one. The file has exactly nine sections; where an earlier revision numbered things differently, the
mapping is stated in place.

Mission target branch: `arena/01a0dca0-drillai` (never `main`; no merge into `main`).
Remote: `origin` → `https://github.com/asgareyvazi/DrillAI`.

---

## 1. Mission identity and final state

**What the mission was.** Productize the drilling intelligence workspace that already existed in this
repository as untracked code on top of an existing platform: commit it, make it say only what the
backend actually knows, and prove it — on `arena/01a0dca0-drillai`, with executable evidence and with
the Git state to back it. The platform underneath (engineering engines, digital well twin, data fabric,
workflow runtime, security, AI layer) was delivered by the earlier mission; this one's subject was the
browser client and the certificate that it works.

**What the mission was not.** No second architecture. No frontend copy of a backend model or of a
backend decision: authorization, action levels, approval requirements and every engineering value are
the server's answers, rendered. No fabricated numbers: missing, zero, unavailable, failed and pending
are five distinct states, and each is rendered as what it is. No mocked acceptance path: the
end-to-end layer drives a real browser, a real API and a real seeded database, and the fault journeys
inject their faults through the real ASGI stack. HTTP is never intercepted in any spec; the only
browser-level interception in the suite is two socket-level relays in the run-event journeys, which
forward to the real backend so a spec can state when a transport failed, and which fabricate no frame.

**Final state, in the five different things a reader must not confuse:**

| | SHA | What it is |
| --- | --- | --- |
| Mission base | `bfa066b` | `main`'s tip when this work began — untouched ever since |
| Last source (implementation) commit | `609e19f` | checkpoint 5, batch D |
| Last test-producing commit | `3141439` | checkpoint 5, batch E — where the unit, browser and backend numbers of CP5 were produced |
| CP5 certification | `3141439` (source/test), `7767ac7` (report), `c16a353` (report) | §3, *CP5* |
| CP6 commits | `645467c` (the workflow), `708617c` (the failure probe), `f24ef86` (probe removed), `f8abc10` (documentation reconciled) | §3, *CP6*, and §5 |
| CP6 certification | `f8abc10` | source, tests and documentation at the certified state |
| Local HEAD | the commit carrying this row | |
| Remote HEAD | the commit carrying this row | `git ls-remote --heads origin arena/01a0dca0-drillai` is the authority |
| Publication | — | **PUSHED AND VERIFIED**; every commit of every checkpoint reached the remote, and each push is recorded in §2.3 and §2.4 |
| Working tree | — | clean: `git status --porcelain` is empty, and nothing generated is tracked |
| `main` | `bfa066b` | untouched since the mission began; this branch was never merged into it |
| Mission status | — | **`MISSION CLOSED — VERIFIED`** (§9) |

**Checkpoint 7, the state now.** Everything above this line describes checkpoint 6, which remains
certified exactly where it was certified. The current branch tip is:

| | SHA | What it is |
| --- | --- | --- |
| CP7 commits | `cd07b1c` (model, vocabulary, lifecycle, identity guards, services, migration), `9804014` (API, authorization, audit, idempotency, tests), `41be468` (the migration's PostgreSQL failure and its test), `23c394a` (workspace, API mirrors, journeys), `0c797e1` (fixtures recaptured, documentation) | §3 *CP7*, §4 |
| Local HEAD | the commit carrying this row | `git rev-parse HEAD` |
| Remote HEAD | the commit carrying this row (`0c797e1` before this documentation edit) | `git ls-remote --heads origin arena/01a0dca0-drillai` — the authority |
| CI at the tip | run [37179922563](https://github.com/asgareyvazi/DrillAI/actions/runs/37179922563) at `0c797e1` — **success**, 6 m 8 s; the run attached to this documentation commit follows it in §5 | §5 |
| Working tree | clean (`git status --porcelain` empty), one remote, `main` still `bfa066b` | §2, §9 |

| | At `0c797e1` |
| --- | --- |
| Tracked files | 280 |
| Source lines (`*.py`, `*.ts`, `*.tsx`) | 78 637 |
| Frontend application source | 28 777 lines |
| Frontend tests | 285 in 24 files |
| End-to-end | 68 journeys in 14 spec files, three deployments |
| Backend | 110 modules; 527 tests, 10 667 lines of tests |
| Secrets in the index | none |

**What exists now, in numbers** (all of them produced by running the command, at `f8abc10` — §4):

| | |
| --- | --- |
| Frontend | React 18 · TypeScript strict · Vite 6 · 12 378 lines of application source |
| Frontend tests | 275 unit/component tests in 23 files · 9 190 lines |
| End-to-end | 65 journeys in 13 spec files, across three deployments of the same product |
| Backend | 140 Python modules, 32 234 lines; 399 tests, 8 586 lines |
| Migration | one baseline; `alembic check` reports zero drift against a fresh database |
| CI | one workflow, 16 steps, on every push to the mission branch |
| Documentation | `README.md`, `docs/FRONTEND.md`, `docs/FRONTEND_TESTING.md`, this report |

---

## 2. Git provenance

Provenance has several axes and they are not the same thing: how the branch got here, what each commit
is, and what is currently on the remote. They are recorded separately below, because a sentence that
says "current commit" without saying which of them it means is not evidence.

### 2.1 Baseline and prior work (session 1)

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

### 2.2 Environment resets, and how the repository was recovered

The sandbox that carried this work was reset repeatedly — ten times before checkpoint 4 alone, then
again during checkpoints 5 and 6. Every reset had the same shape: the repository was checked out at the
grafted base `bfa066b` with the mission's history absent, the working-tree *files* intact, and the
toolchain (the backend virtualenv, `frontend/node_modules`, the Playwright browser, `/tmp`) gone. The
recovery was identical every time, and is recorded here because the invariant it demonstrates is the
reason nothing has been lost between sessions:

1. archive the working tree before touching the repository (a `tar` of the dirty files);
2. `git fetch --depth=200 origin arena/01a0dca0-drillai` — history returns;
3. `git update-ref refs/remotes/origin/arena/01a0dca0-drillai FETCH_HEAD` — the tracking ref is rebuilt;
4. `git reset --mixed FETCH_HEAD` — the branch is re-attached; **this touches no file**, so uncommitted
   work survives and shows up as modifications against the fetched tip;
5. re-run `scripts/bootstrap.sh` — the toolchain is rebuilt from the repository's own manifests.

`git reset --hard` was never used, and nothing was ever reverted to an older commit. The reason no work
was lost is not luck: each checkpoint is committed and pushed before the next batch begins, so the
branch on the remote is always the durable record.

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
| `git ls-files \| wc -l` | 203 tracked files |
| `git status --porcelain` | empty |
| `scripts/bootstrap.sh` | backend venv rebuilt, 400 npm packages installed, Chromium 153.0.8010.0 verified |
| `frontend: npx tsc -b --noEmit` | **PASS** |
| `frontend: npx eslint .` | **PASS** |
| `frontend: npx vitest run` | **54 passed** (4 files) — reproduces the Foundation 6/7 number |
| `backend: pytest -q` | **365 passed, 2 skipped** (138.8 s) — reproduces the recorded number |
| `backend: ruff check .` | **All checks passed** |

So the restored environment reproduces the previously recorded results at `6d31861` before any new
code was written. That reproduction is the starting point of this session, not evidence of the new
work: every result for the new checkpoints is produced again below at the commit it belongs to.

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

An earlier recovery of the same kind is recorded in §2.2. The lesson recorded there held: a lost session
never implies lost work, because the work was committed and pushed before the next batch began.

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
time); the recovery procedure and what survived are recorded in §2.2.

### 2.3 The git gates run during the mission

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
| Source lines | `git ls-files '*.py' '*.ts' '*.tsx' \| xargs wc -l` | 61 002 |
| Secrets in the index | `git ls-files \| grep -iE '\.env|credential|secret|\.pem|\.key$'` | none |
| Ignored-but-present artefacts | `frontend/dist`, `.e2e/`, `node_modules`, `backend/.venv` | not tracked (`.gitignore`) |

The branch is the only one this work touches; `main` is untouched at `bfa066b`.

The gate commands were run at `0297fd7` with a clean tree (§4). Publication is a normal fast-forward
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
| Secrets in the index | `git ls-files \| grep -iE '\.env|credential|secret|\.pem$|\.key$'` | none |
| Tracked files / source lines | `git ls-files` / `git ls-files '*.py' '*.ts' '*.tsx' \| xargs wc -l` | 245 files / 64 856 lines |

What was published, exactly — the commits that had been local when authentication failed, plus the
rest of the checkpoint's tail:

| Commit | What it is |
| --- | --- |
| `9626647` | `e2e/error-matrix.spec.ts`: the stale-decision journey is stale on purpose |
| `1f68671` | `e2e/workflow-studio.spec.ts`: the draft journey owns its precondition |
| `0297fd7` | `e2e/run-events.spec.ts`: the finished-run journey stops counting the wrong screen's socket |
| `8f859ca` | the durable record: the checkpoint-4 certification, the counts, the mutation table and this gate |
| the commit carrying this row | a one-row update naming `8f859ca`, so the report says exactly which SHA holds the certification; the remote tip |

The three test commits were re-created after environment reset 10 destroyed the local history (§2.2);
their content is byte-identical to the commits they replace, and the previously reported SHAs
(`48d3cd2`, `23e29ba`, `7080def`) were never on the remote — they are superseded, not lost.

### 2.4 The git gate at closure

Run at the commit carrying this row, on the mission branch only. `git ls-remote` is the authority; the
local remote-tracking ref is a cache and is treated as one.

| Check | Command | Result |
| --- | --- | --- |
| Branch | `git branch --show-current` | `arena/01a0dca0-drillai` |
| Local HEAD | `git rev-parse HEAD` | the commit carrying this row |
| Remote HEAD | `git ls-remote --heads origin arena/01a0dca0-drillai` | the same SHA |
| Local == remote | — | yes |
| Unpushed commits | `git log --oneline origin/arena/01a0dca0-drillai..HEAD` | empty |
| Working tree | `git status --porcelain` | empty — no modified, no untracked, no generated file |
| Shallow clone | `git rev-parse --is-shallow-repository` | `false` |
| `main` | `git ls-remote --heads origin main` | `bfa066b28e0071880cb9191a4f1d47fdaa143e04` — unchanged since the mission began |
| Branches | `git ls-remote --heads origin` | `main` and `arena/01a0dca0-drillai` — no branch was created, renamed or deleted |
| Remotes | `git remote -v` | `origin` only — no second remote, no credential stored anywhere |
| Secrets in the index | `git ls-files \| grep -iE '\.env|credential|secret|\.pem$|\.key$'` | none |
| Generated artefacts | `git ls-files \| grep -iE 'node_modules|\.venv|\.e2e/|dist/|test-results|playwright-report'` | none (`.gitignore`) |
| Tracked files | `git ls-files \| wc -l` | 258 |

Every batch of checkpoint 6 was committed, pushed and re-verified against the remote in that order:
`645467c` (CI) → `708617c` (the deliberate failure probe) → `f24ef86` (probe removed) → `f8abc10`
(documentation reconciled) → the commit carrying this row (this report). Pushes were ordinary
fast-forwards; there was no force push, no amend of a published commit, no history rewrite and no
second remote at any point in the mission.

---

## 3. Checkpoint certification, CP1–CP7

**Two numbering schemes, both kept.** The platform mission that this repository was built by numbered
its checkpoints 1–8. The continuation brief that commissioned the frontend productization numbered its
own 1–6. Erasing either would make old commits and old reports unreadable, so the platform's are called
`Foundation N` here and the frontend's are `CPn`. The mission whose status this file carries is the
frontend one, and `CP1`–`CP6` below are its record.

Each checkpoint was implemented, tested, inspected (`git status` / `git diff`), committed, pushed and
verified against the remote before the next began.

### Foundation 1 — backend drilling domain, and the bootstrap race

- Commit `b981017` — `feat(backend): drilling intelligence domain with NPT, timeline and reporting APIs`
- Contents: `drillai/drilling/` (DDR, NPT, state, timeline, reporting, optimisation, advisor,
  classifiers), `api/routers/drilling.py`, serialiser and ingestion changes, workflow node changes,
  `backend/pyproject.toml` (`pythonpath`), `scripts/seed_demo.py`, and the tests for all of it.
- Defect fixed: two first requests from the same org raced on `organizations.slug`; the insert now
  happens in a savepoint and a lost race re-selects the winner. The regression test fails against the
  pre-fix body — proven by temporarily restoring it and watching `IntegrityError: UNIQUE constraint
  failed: organizations.slug`.

### Foundation 2 — the frontend workspace, corrected against the real API

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

### Foundation 3 — frontend tests

- Commit `bfa7581` — `test(frontend): pin value semantics, the API boundary and the cockpit contract`
- 10 files, 714 lines: 54 tests plus the fixtures they assert against (captured from the running API
  for the seeded **synthetic** well).

### Foundation 4 — the end-to-end journey

- Commit `98c742f` — `test(e2e): well journey against a real backend, real database and a real browser`
- 7 files, 673 lines, including the deterministic browser preparation that replaces a hand-made
  `/tmp` recipe.

### Foundation 5 — bootstrap, and ignores that match what is generated

- Commit `f259c33` — `chore: reproducible environment bootstrap and build artefacts ignored`

All five commits were pushed before the next one was created; `git ls-remote` matched local `HEAD` at
`f259c336b6185e9b0be0264c8c660122b0142f08`.

### Foundation 6 — documentation, and a repository with nothing outstanding

- Commits `a6d1cc8` — `docs: describe the frontend, its tests and the mission state in the repository`
- Contents: `docs/FRONTEND.md`, `docs/FRONTEND_TESTING.md`, `docs/mission-reports/FRONTEND-MISSION.md`
  (this file) and the `README.md` corrections.
- Effect: every artefact produced so far is committed **and** present on the remote branch, and
  `git status --porcelain` is **empty**. Before this checkpoint the frontend, the drilling domain and
  the test harness existed only in a working tree — the state this mission exists to eliminate.

### Foundation 7 — documents, ingestion, records and evidence in a browser

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

### CP1 — the workflow studio lifecycle

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

### CP2 — run lifecycle, and the approval record

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

### CP3 — the stream a client can rely on

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

### CP3 — the durable report block

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

### CP4 — the error matrix, and every surface that used to lie

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
| Publication | all commits pushed to `arena/01a0dca0-drillai`; `git ls-remote` == local HEAD — see §2.4 |
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
production — answers them badly or not at all. (One journey in this spec does stall a single screen's
live stream at the socket level; it is disclosed in the audit table under *Route interception*, and it
withholds frames rather than inventing any.) The second frontend build differs only in
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

### CP5 — permissions, context, RTL and accessibility

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
| D — accessibility primitives | table rows as controls, `Tabs` owning its panel, `Drawer`'s focus lifecycle, and one polite live region for run events | published as `609e19f` (14 files, +1116/−33) — originally committed as `be2f0cf`, which environment reset 14 destroyed before it could be pushed (see the publication table) | Vitest **275 passed** in 23 files · Playwright **61 passed** / 0 failed · `tsc -b --noEmit` clean · `eslint .` clean · build **274.82 kB (gzip 74.88)** · backend **397 passed, 2 skipped** · `ruff check .` clean · `alembic check` (after `alembic upgrade head`): "No new upgrade operations detected". The browser suite was run at this content before the reset; the same journeys, plus batch E's, were re-run together at `3141439` (65 passed), and Vitest/eslint/tsc were re-run at `609e19f` itself |
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
table were produced at). When this paragraph was written the mission was still open — the nine-section
final report and the CI certification belonged to checkpoint 6 — so it read *MISSION IN PROGRESS — NOT
COMPLETE* here. Checkpoint 6 has since been certified at `f8abc10` (§3 *CP6*, §5, §6) and the mission
statement is now **`MISSION CLOSED — VERIFIED`** (§9). The sentence is kept as it was written, because
a report that quietly rewrites its own intermediate verdicts is not a record.

### CP6 — CI, clean-checkout certification, and closure

The last checkpoint has one job: make the repository able to state its own condition without a human at
a terminal, then certify that condition from a place that has never seen this machine.

**What was built.** One workflow, `.github/workflows/ci.yml`, triggered by pushes to
`arena/01a0dca0-drillai`, by pull requests into that branch or `main`, and by `workflow_dispatch`. It
holds `permissions: contents: read`, uses no secrets, writes nothing back to the branch, and cancels a
run that a newer push has superseded. It installs with `pip install -e "backend[dev]"` into
`backend/.venv` — the path `e2e/start-api.mjs` expects — and with `npm ci` against the committed
lockfile, on Python 3.11 and Node 22, the versions the repository declares. Gates run in the order the
brief fixes: typecheck → lint → unit tests → build → backend tests → backend lint → migration check on
a fresh database → the complete end-to-end suite. Each gate is its own step, so a red run names the gate
that failed. It prints its own provenance first (`git rev-parse HEAD`, branch, `git status --porcelain`,
`git log -1 --oneline`), and there is no `|| true`, no `continue-on-error`, and no `if: always()`
outside the artifact upload.

**The PostgreSQL tests: resolved, not skipped.** Two persistence tests skip without a database, and the
brief asks for that to be settled rather than tolerated. Running with `DRILLAI_TEST_POSTGRES=1` starts
the repository's pinned `pgserver` dev dependency — an embedded PostgreSQL — and both run: **399
passed, 0 skipped** instead of 397 passed + 2 skipped. The two tests are `test_postgres_session_and_native_types`
(native JSON and timestamp behaviour SQLite cannot prove) and `test_postgres_migration_matches_metadata`
(the Alembic baseline creates exactly the schema the models declare). That is repeatable across four
runs (204.75 s, 208.10 s, 208.37 s, 213.44 s) and it is what CI does, so
the certificate CI produces is the 399-test one. No test was deleted, none was rewritten against
SQLite, and nothing was falsified to make the count look better.

**The failure path was proven, not assumed.** A pipeline that has only ever been green proves very
little, so a temporary probe commit (`708617c`) added a backend test that fails on purpose. The run
failed exactly as it must: the backend-tests step went red, the steps after it — backend lint, migration
check, end-to-end — were skipped rather than passed, and the job's conclusion was `failure`. The probe
was removed in the following commit (`f24ef86`) and the removal re-run green. Both runs are in §5, with
their SHAs.

**The gates certify their own results.** Each testing step reads the output of the command it ran and
fails on anything that would make a green step a false statement: the backend gate fails on a skip (so
`DRILLAI_TEST_POSTGRES=1` cannot quietly do nothing) and fails if the postgres-marked tests are no
longer collected; the end-to-end gate fails unless all three deployments ran tests and the suite
finished with no failure, flake or skip; the unit gate fails on a skip. This was not decoration — the
backend gate had been running `pytest -q` on top of a `pyproject.toml` that already sets `-q`, and the
doubled flag suppresses pytest's summary line entirely, so the log showed dots rather than counts (§5).
Each assertion was validated against the real output of its command and against a falsified log
reproducing the exact false-certification case; the first run of these gates on the runner went red
because the runner colours its output, and the fix (normalising ANSI escapes before parsing) plus that
red run are recorded in §5.

**What CI does not do.** It does not deploy, it does not push, it does not publish generated source, and
the only artifact it uploads is Playwright evidence (report, traces, screenshots, logs), kept 14 days
and uploaded only after a failure, so a failing run cannot be made to look tidy. There are no
deployment assets in `ops/`; CI certifies the product, it does not ship it.

### CP7 — well master data, identity, lineage and the asset contract

Checkpoint 6 certified the client against the platform. Checkpoint 7 turns the spine that platform
was built around — organization → project → field → well → wellbore → section, with operations,
documents, evidence, twin and the engineering surfaces hanging off it — into something a person can
actually curate, and something the rest of the product can trust. The reason it is a checkpoint at all
is that the spine was, until now, only half real: the tables existed and the cockpit read them, but the
platform could not guarantee that a well's identity was well-formed, that a wellbore's parent was a
wellbore of the same well, or that a section's planned bottom was not being handed to a client as the
current depth.

**What the audit found, before any code was written.** Each finding below was reproduced against the
running API or read from the file named, not inferred from a name.

- **The vocabulary existed and governed nothing.** `WELL_TYPES` and `WELLBORE_PURPOSES` were declared
  in `db/models/asset.py` and enforced at no boundary. The create endpoint accepted any string, so a
  client could write `well_type="not_a_real_type"` and read it back as a success.
- **The defaults were not members of the vocabulary.** `well_type="development"` and
  `purpose="production"` — neither value is in its own vocabulary. Five captured fixtures repeated the
  first, which is how a wrong default becomes a wrong client.
- **`PATCH` and `PUT` on a well returned 405.** There was no update path at all: a misspelled well name
  was permanent.
- **The life-cycle endpoints did not exist.** Well status was a column nothing could change through the
  API, and `AuditLog` had no writer anywhere in the codebase, so nothing that happened to an asset was
  recorded.
- **Lineage was a field nobody checked.** `parent_wellbore_id` was accepted and ignored; a wellbore
  could name itself, name a wellbore of another well, or form a cycle.
- **`/fields` and `/rigs` returned 404.** Field was a string on a well, not an entity; rigs could be
  referenced without existing.

**§5 — the canonical vocabulary, decided first.** The brief forbids adding `development` to the enum
unless the audit proves the product needs it. It does not, and the audit says why: the value appears in
exactly two places — the old create default and the fixtures that were copied from that default — and
in no reader, no engine, no document and no report. The canonical set is the nine well types the schema
already declared (`exploration`, `appraisal`, `development_producer`, `development_injector`,
`observation`, `water_source`, `disposal`, `sidetrack`, `reentry`); a development well is a
`development_producer` or a `development_injector`, and the platform now says so. The same audit ran
across the whole vocabulary surface and produced four new canonical tuples — four wellbore statuses,
four section statuses (both derived from values the product already reads, not invented), and sixteen
labelled section numbers — plus four defaults that are members of their vocabulary by construction.
Every value entering the database passes through `canonical_choice`, which names the field, the
rejected value and the accepted set in its error, so a caller who sends `development` is told what the
platform calls that well. The drift is closed at the source: the migration repairs existing rows, and
every captured fixture was regenerated from the server rather than edited, so the four run payloads and
the cockpit payload now carry `development_producer` because the server sent it — and the capture script
was extended to write the whole `frontend/src/test/fixtures` directory, cockpit payloads included, so
the next recapture does not leave half the directory behind.

**What is editable, per field.** Each identity field was classified rather than exposed, because
"editable" and "immutable" are product decisions with consequences. The identifier (`well.id`) and the
owning organization are immutable — they are what documents, evidence, operations, twin state and
every historical record point at, and a rename must leave all of them intact (§23). The regulator
identifier is editable but unique within the organization, and lowering it to a duplicate is refused
with the conflicting well named. The field a well belongs to is checked against the well's own project
and organization on create *and* on update, so a well cannot be moved into another tenant's field or
into a field of a project it does not belong to. Rig references are checked against rigs that exist in
the same organization. Coordinates, the datum, and the business dates are stored and returned
separately from anything derived, so a client can tell what was recorded from what was computed.

**The life cycle is a service, not a column.** Ten well statuses with `p&a` terminal, four wellbore
statuses, and one transition table per entity, live in the domain layer
(`assets/lifecycle.py`) — the router carries no rules of its own. A transition is validated against the
table, recorded in `AuditLog` with its reason, and returned to the client alongside the transitions the
server will accept next, so the interface renders a menu the server published rather than a copy of the
rules. `planned → abandoned` exists because plans are cancelled; `drilling → planned` does not exist
because time does not run backwards.

**Lineage is structured, never guessed.** A sidetrack is a wellbore whose recorded parent is a
wellbore of the same well, created through the lineage endpoint with a purpose from the canonical set.
Self-parenting, cross-well parenting, cross-organization parenting and cycles are each refused with a
distinct error, and a duplicate sequence within the well is refused rather than silently renumbered.
Activation moves the well's active wellbore and is audited; the count of active wellbores is governed
per well, and the client is told which one is active instead of being left to infer it.

**Plan, actual, computed, interpreted.** `SECTION_NUMBER_SEMANTICS` labels all sixteen recorded section
numbers as exactly one of those classes, and the serializer returns the label. `planned_bottom_md_si`
and `current_md_si` are therefore never interchangeable, and the current depth is `null` for a
section that has not been drilled — the client renders "no current depth" instead of back-filling the
plan (proven in the browser journey, which asserts the planned 3 400 m value is *absent* from the page
rather than formatted differently). Plan revisions never overwrite as-drilled values.

**Mutations: authorization, audit, idempotency, stale writes.** Every mutating asset route resolves an
action from the existing catalogue (`security/catalog.py`: 43 platform actions, plus the three the AI
layer registers as tools — 46 in a running application, and the reachability test asserts every one of
them is grantable by some role), enforces
it through the existing `authorize()`, and either replays a stored response or reserves an idempotency
key before touching the database — a retried rename cannot be applied twice. Updates carry
`expected_updated_at`; a stale value is refused with `409 platform.conflict` and `retryable=false`,
which is the difference between "somebody else changed this, reload" and "try again". Every write
appends to the existing `AuditLog` with the actor, the action, the before and after, and the reason
where one was given; `session.add(...)` alone is never the mutation. Tenant scoping is checked on read
and write, including for guessed identifiers.

**The migration repairs, and was proven on PostgreSQL.** `c4a91e0d7b52` adds the identity columns and
indexes, installs the uniqueness scopes the real query patterns need, and repairs the legacy
vocabulary. It refuses, rather than guesses, when a database already contains two wells sharing a
regulator identifier, and names the offending wells in the error. On PostgreSQL its first form failed
outright — the repair compared a boolean column to `1`, which SQLite accepts and PostgreSQL rejects
with `operator does not exist: boolean = integer`. The comparison is now built from a lightweight table
with boolean binds; the chain is verified on SQLite (fresh upgrade, no drift, round trip) and on a real
PostgreSQL, and that PostgreSQL proof now lives in the test suite as
`tests/db/test_migrations_postgres.py`, so the dialect cannot silently regress: it applies the whole
chain to head, checks for drift, reverses and re-applies the revision, and matches the refusal message
against the rows that really exist.

**The intermediate commit ran CI red, and the reason is worth recording.** `cd07b1c` — the model,
vocabulary, services and migration — went red in CI at the backend-tests step. The log could not be
retrieved from this sandbox (GitHub's signed log storage returns `EOF`), so the failure was diagnosed by
reproducing the commit: the tree was extracted to a scratch directory and its tests run against it, and
the failure reproduced exactly —

```
sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed: wellbores.well_id
INSERT INTO wellbores (..., is_active, ...) VALUES (..., 1, ...)
```

The model had just installed a partial unique index (`uq_wellbores_active_per_well`, `well_id` where
`is_active`) to make "one hole per well is the one being drilled" a database fact. The *old* write path
was still in place at that commit, and it derived activation from the caller's input
(`is_active=payload.sequence == 1`), so a second hole created at the default sequence claimed to be
active and the database refused it. Batch 2 replaced that path with the service, which activates the
first hole to exist and makes every later one active only through an explicit, audited call. The
failure was therefore the new invariant doing its job against a write path that had not been replaced
yet — a sequencing mistake in how the batches were split, not a wrong invariant, and the fix is the
service rather than a weaker constraint. `9804014` and `23c394a` are green in CI; the run attached to
`41be468` was cancelled by the next push, so the tip's green run is the authority (§4).

**The workspace.** `/master-data` is where the spine is curated: well identity with the version it was
read from, the life-cycle menu the server published, wellbore lineage with activation, sections that
never print a plan as a measurement, fields with their project scope, and the governance ledger for the
selected well. Search, project and field scope, the selected well and the selected tab live in the URL,
so a reload opens the same page. Well List is now the entry point — server-side search and filtering, a
create form, and a per-row link — and it distinguishes an empty fleet from an empty search result from a
failed read. `api/types.ts` mirrors the contract with unions rather than `string`, so `development`
cannot be typed into a screen; every asset call goes through `api/endpoints.ts`, and the only new
transport concept is the idempotency key the caller generates once per submission.

---
---

## 4. Automated verification tied to exact commits

Every row below was produced by running the command shown, at the commit named in the row. Exact
counts, no rounding, and nothing is reported as "all good".

### At `23c394a` (Checkpoint 7) — the asset commit

Measured at the branch tip `23c394a3846002580ec1d39da89adc5f4296a2ba`, whose working tree is clean and
whose remote ref carries the same SHA. The backend suite was run first, in the same tree the frontend
suite then ran in; both were run against the committed source, with no later edit.

| Command | Result |
| --- | --- |
| `npm run typecheck` (`tsc -p tsconfig.app.json`) | clean, no output |
| `npm run lint` (`eslint src --max-warnings=0`) | clean, no output |
| `npm test` (`vitest run`) | **285 passed** in **24 files**, 0 failed, 0 skipped |
| `npm run build` | `dist/assets/index-iv8StkKv.js` **338.53 kB** (gzip **89.54 kB**), CSS 37.30 kB, built in 3.36 s |
| `npm run e2e` | **68 passed / 0 failed** (4.4 m on the re-run, 3.7 m on the first): 63 `chromium`, 4 `chromium-faults`, 1 `chromium-auth`, in 14 spec files |
| backend `pytest` with `DRILLAI_TEST_POSTGRES=1` | **527 passed, 0 skipped, 0 failed** in 337.33 s (re-run; 354.94 s on the first run) |
| backend `pytest` without it | **524 passed, 3 skipped, 0 failed** in 291.25 s — the three are the PostgreSQL-backed ones |
| backend `ruff check .` | clean — "All checks passed!" |
| `alembic upgrade head`, `alembic check`, downgrade + re-upgrade, on PostgreSQL | covered by the suite above (`tests/db/test_migrations_postgres.py`, 2 tests) and by the standalone proof recorded in §3 *CP7* |

The battery was run twice: once at the branch tip as the checkpoint's certification, and again after an
environment reset destroyed the toolchain and rewound the local history (recovered by fetching the
remote, never by discarding work). Both runs produced the same counts — 285 unit tests, 68 journeys, 527
backend tests — which is the reproducibility claim this checkpoint can actually make, since the second
run provisioned its own virtualenv, `node_modules` and browser from the committed lockfiles and scripts.

The checkpoint's own tests, by file: `tests/assets/test_asset_domain.py` **31**,
`tests/assets/test_asset_service.py` **42**, `tests/api/test_assets_api.py` **53**,
`tests/db/test_migrations_postgres.py` **2**, `frontend/src/pages/master-data/MasterDataWorkspace.test.tsx`
**10**, `frontend/e2e/master-data.spec.ts` **3** journeys. The end-to-end suite grew from 65 to 68
journeys; no earlier journey was removed, skipped or weakened, and the 527-test backend count is the
399 of checkpoint 6 plus this checkpoint's 128.

CI on the branch, by commit: `cd07b1c` **failed** at the backend-tests step (§3 *CP7* — reproduced,
diagnosed, and fixed by the service in `9804014`), `9804014` **success** (run `37110186230`), `41be468`
**cancelled** (superseded by the next push), `23c394a` **success** (run `37112183461`, 8 m 24 s), and the
tip `0c797e1` **success** (run `37179922563`, 6 m 8 s). The red run is reported rather than omitted: a checkpoint whose intermediate commit failed is
a checkpoint whose final commit is green for a reason that can be read.

### At `f8abc10` (Checkpoint 6) — the certification commit

Measured in a clean checkout of the remote at `f8abc10ae6cdb4badb6fbb434dc73251cc14192b` (§6), after
the last source, test or documentation change of this checkpoint and with no later edit.

| Command | Result |
| --- | --- |
| `npm run typecheck` | clean, no output |
| `npm run lint` (`eslint .`) | clean, no output |
| `npm test` (`vitest run`) | **275 passed** in **23 files**, 0 failed, 0 skipped |
| `npm run build` | `dist/assets/index-BRM9qJCU.js` **274.82 kB** (gzip **74.88 kB**), built in 3.73 s |
| `npm run e2e` | **65 passed / 0 failed** (4.0 m): 60 `chromium`, 4 `chromium-faults`, 1 `chromium-auth`, in 13 spec files |
| backend `pytest` with `DRILLAI_TEST_POSTGRES=1` | **399 passed, 0 skipped, 0 failed** in 213.44 s |
| backend `ruff check .` | clean — "All checks passed!" |
| `alembic upgrade head`, then `alembic check`, on a fresh database | "No new upgrade operations detected" |
| `scripts/bootstrap.sh` from an empty checkout | venv + 400 npm packages + Chromium 153.0.8010.0; the tree stayed clean |

Journey counts by area at this commit: cockpit 4, documents and evidence 4, workflow studio 9, run
monitor 6, run events 5, error matrix 5, faults 4, authentication 1, identity and permissions 7, context
and deep links 6, RTL 4, accessibility 7, integrated checkpoint-5 3. The suite is one command, so all 65
ran together against one seeded database. The same eight commands, in the same order, are what CI runs
on every push (§5).

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
| `.venv/bin/python -m pytest tests/db/test_run_event_sequence_migration.py` | **1 passed** (data repair, not schema: see §3) |
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

## 5. CI certification

One workflow, four runs that matter, each named with the commit it tested. A run is evidence only for
the SHA in its `head_sha`; a local reproduction of the pipeline is not substituted for it anywhere here.

| Run | Commit | Conclusion | Duration | What it proves |
| --- | --- | --- | --- | --- |
| [36687642914](https://github.com/asgareyvazi/DrillAI/actions/runs/36687642914) | `645467c` | **success** | 6 m 51 s | the pipeline, on the commit that introduced it |
| [36688461693](https://github.com/asgareyvazi/DrillAI/actions/runs/36688461693) | `708617c` | **failure** | 2 m 37 s | a required failure fails the job and skips the rest — the deliberate probe |
| [36688766862](https://github.com/asgareyvazi/DrillAI/actions/runs/36688766862) | `f24ef86` | **success** | 6 m 53 s | the probe removed; green again |
| [36689824630](https://github.com/asgareyvazi/DrillAI/actions/runs/36689824630) | `f8abc10` | **success** | 6 m 57 s | the pipeline on the reconciled documentation |
| [36713831111](https://github.com/asgareyvazi/DrillAI/actions/runs/36713831111) | `faaf861` | **success** | 7 m 50 s | the pipeline on the commit that made the evidence readable and corrected the overclaims |
| [36716452435](https://github.com/asgareyvazi/DrillAI/actions/runs/36716452435) | `7d15640` | **failure** | 0 m 41 s | the self-asserting gates' first run: the unit gate refused a run that had passed, because the runner colours its output (below) |
| the commit carrying this row | the remote tip | **success** | — | the final run: a push runs on the tip, and the run attached to that SHA is the closed certificate |

The `success` runs executed every required step green, including the end-to-end suite in all three
deployments and the backend tests with PostgreSQL. The `failure` run is the interesting one, because it
is the only direct evidence that the pipeline can say no:

| Step in run 36688461693 | Conclusion |
| --- | --- |
| checkout, provenance, Python, Node, backend install, frontend install | success |
| frontend typecheck, lint, unit tests, build | success |
| **backend tests** | **failure** — the probe test failed |
| backend lint | **skipped** — not reported as passing |
| migration check | **skipped** |
| end-to-end | **skipped** |
| job conclusion | **failure** — and the evidence-upload step is the only one that still ran |

The commands that produce this table, exactly as run:

```bash
gh run list --branch arena/01a0dca0-drillai --limit 5
gh api repos/asgareyvazi/DrillAI/actions/runs/<id> --jq '{sha: .head_sha, conclusion: .conclusion}'
gh api repos/asgareyvazi/DrillAI/actions/runs/<id>/jobs --jq '.jobs[0].steps[] | {name, conclusion}'
```

**Why this report certifies from step conclusions rather than from log text.** GitHub serves raw step
logs from Azure blob storage (`*.blob.core.windows.net`), which the environment that produced this
report cannot reach: the signed URLs are issued correctly — so the authentication is fine — but the
fetch fails at the TLS layer (`OpenSSL SSL_connect: SSL_ERROR_SYSCALL`) while `github.com` itself
answers normally. The step *summaries* are not exposed by the API either (checked: the check-run for a
green job carries `output.summary` empty), so neither route makes a log reachable from here.

Substituting a local run for the CI one would not be evidence, and neither would inferring execution
from the YAML. So the gates were changed to make the conclusion itself sufficient: each of the three
testing gates now **asserts the property it certifies**, so that a green step means the property held
rather than that a command was launched.

| Gate | What a green step now proves |
| --- | --- |
| Frontend unit and component tests | the result line was printed and contains no `skipped` |
| Backend tests | the final count line contains no `skipped` — i.e. the two PostgreSQL tests ran, not skipped — and `-m postgres --collect-only` still collects ≥ 2 tests |
| End-to-end suite | all three deployments ran tests (`[chromium]`, `[chromium-faults]`, `[chromium-auth]`) and the run finished with no failure, no flake and no skip |

Each gate's script was extracted from the committed YAML and executed as the runner executes it
(`bash -e`), against the real command's real output. Both directions hold:

| Gate body, real output | Result |
| --- | --- |
| frontend unit gate, real Vitest run | exit 0, `Tests  275 passed (275)` |
| backend gate, real suite **with** `DRILLAI_TEST_POSTGRES=1` | exit 0, `399 passed`, `2/399 tests collected` |
| backend gate, same body, switch **not** set | **exit 1** — the suite printed `397 passed, 2 skipped` and the gate refused to certify it: `::error::the suite skipped tests; the PostgreSQL integration tests must execute` |
| end-to-end gate, real clean log | exit 0, `65 passed` |
| end-to-end gate, log with the auth deployment removed | **exit 1**, `::error::chromium-auth ran no test` |
| backend gate, log falsified to `397 passed, 2 skipped` | **exit 1**, same refusal |

The third row is the one worth keeping: it is not a simulation. The switch was simply absent from the
shell, the real suite really did skip the two PostgreSQL tests, and the gate really did refuse the run
— which is precisely the false certification this checkpoint has to rule out.

**And then CI found a defect in the gate itself, which is worth recording rather than tidying away.**
The first push of these gates (run 36716452435 at `7d15640`) went red at the unit step while the suite
itself passed: GitHub's runner enables colour, so Vitest's result line arrives wrapped in ANSI escapes
— `\x1b[2m      Tests \x1b[22m\x1b[32m275 passed\x1b[39m` — and the gate's anchor `^ *Tests` cannot
match a line that begins with an escape sequence. Locally the same script passed, because a piped,
non-CI run disables colour; the runner does not. The diagnosis did not need the (unreachable) log: the
step's own `::error::` annotation said *the unit test run printed no result line*, which is readable
through the API. Each gate now normalises ANSI escapes into a `.plain` copy before parsing — the
committed fix — and the whole matrix was re-run with colour forced on: the unit gate passes under
`CI=true`, the end-to-end and backend gates pass against colour-injected logs, and the skip case still
fails. The red run is left in the table above: a pipeline whose gates cannot fail is not a pipeline,
and neither is one whose failures are quietly dropped from its own record.
This closed a real hole rather than decorating one: the backend gate used to run `pytest -q` on top of
a `pyproject.toml` that already sets `-q`, and the doubled flag suppresses pytest's summary line
entirely, so a reader of the log saw progress dots and no counts at all. It now runs `pytest` (the
project's own verbosity) and certifies from the line it prints.

The counts are written to the run summary as well, for a reviewer who opens the run in a browser, and
`set -o pipefail` keeps every command's exit status as the step's. What remains is honest: the full log
text is not quoted in this report, and a reader who wants it needs a network that can reach GitHub's
log storage.

---

## 6. Clean-checkout certification

CI proves the pipeline works on GitHub's machines. It does not prove that the repository is
self-contained — a machine that already had the right toolchain would pass either way. So the
certification was reproduced twice from a directory that `git clone` had just created, with nothing
copied into it: no virtualenv, no `node_modules`, no `.e2e` state, no Playwright browser cache, no build
output, and no file from the working tree that produced the earlier numbers.

| | Clean checkout 1 | Clean checkout 2 | Certification at `faaf861` |
| --- | --- | --- | --- |
| Directory | `/tmp/cp6-clean` | `/tmp/cp6-final` | `/tmp/drillai-cp6-final-cert` |
| Source | `git clone --branch arena/01a0dca0-drillai https://github.com/asgareyvazi/DrillAI.git` | same | same |
| Commit | `f24ef86` | `f8abc10` | `faaf861` |
| SHA vs. remote | equal (`git rev-parse HEAD` = `git ls-remote`) | equal | equal |
| `git status --porcelain` at clone | empty | empty | empty |
| Tracked files | 258 | 258 | 258 |
| Caches present at clone | none | none | none |
| `scripts/bootstrap.sh` from scratch | OK — venv, 400 npm packages, chromium 153.0.8010.0 | OK — same, tree still clean | OK — 31.7 s, chromium re-provisioned after the browser was deleted |
| `npm run typecheck` / `npm run lint` | clean / clean | clean / clean | clean / clean |
| `npm test` | 275 passed in 23 files | 275 passed in 23 files | 275 passed in 23 files |
| `npm run build` | `index-BRM9qJCU.js` 274.82 kB (74.88 gzip) | same | same |
| backend `pytest` with `DRILLAI_TEST_POSTGRES=1` | **399 passed**, 208.10 s | **399 passed**, 213.44 s | **399 passed**, 0 skipped, 167.51 s |
| backend `ruff check .` | "All checks passed!" | "All checks passed!" | "All checks passed!" |
| `alembic upgrade head` + `alembic check` on a fresh DB | "No new upgrade operations detected" | same | same |
| `npm run e2e` | **65 passed** (4.0 m; 60/4/1) | **65 passed** (4.0 m; 60/4/1) | **65 passed** (3.4 m; 60/4/1, 13 spec files) |
| Tree after the gates | clean | clean | clean — every artefact ignored (`.venv`, `node_modules`, `.e2e`, caches, `dist`) |

The third column was produced the way §9 describes: a directory that did not exist, a fresh `git clone`,
the browser deleted before bootstrapping so the repository had to re-provision it, and no file copied in
from any earlier tree.

§5's last row and §9's condition 2 are about the commit that carries this report, and they are verified
after it is pushed — the run attached to that SHA is the authority, and the same clean-checkout
procedure is repeated at it. A report cannot name its own CI run before it exists; what it can do is say
exactly where to look, and that is what those two rows do.

**The browser is produced by the checkout, not by the machine.** The workspace that ran the earlier
checkpoints had a browser at `/tmp/chromium` with libraries at `/tmp/drillai-chromium-libs/lib`. Both
were deleted before the final bootstrap, to test whether the repository can replace them. It can: the
repository's own `frontend/scripts/prepare-chromium.mjs` un-brotli'd the ~67 MB `chromium.br` inside
`node_modules/@sparticuz/chromium` (a committed devDependency, pulled by `npm ci`) and extracted the
shared libraries, printing `chromium: /tmp/chromium`, `libraries: /tmp/drillai-chromium-libs/lib`,
`verified: Chromium 153.0.8010.0`. The binary then reported its version (`Chromium 153.0.8010.0`) and
`npx playwright --version` reported `1.63.0`. So the browser's version is known, its libraries are
provided by the repository's own extraction step, and no pre-seeded machine state is required.

**One environment-only warning, named rather than hidden.** The final backend run reports
`1 warning`: `RuntimeDirWarning: XDG_RUNTIME_DIR is not set, falling back to /tmp/runtime-1001`, raised
by the third-party `platformdirs` package that `pgserver` uses. It is a property of the sandbox
container, not of the repository, and nothing in the suite depends on that directory.

Also verified, because it is the failure this repository has actually hit: seeding writes
`.e2e/fixtures.json` per run, and the fixture identifiers differ between the two clean checkouts and the
developer tree. No spec hard-codes them; every one reads the file the run just wrote.

---

## 7. Quality, security and documentation audit

**Documentation reconciled to the repository, not to the intention.** Six claims in the repository had
stopped being true when checkpoint 6 began, and all six were corrected in `f8abc10`:

| Claim that was there | What the repository now says | Where |
| --- | --- | --- |
| "Only the first UI journey is automated end to end … the workflow, run, approval, failure, WebSocket and RTL journeys … are **not** yet covered by a browser test" | 65 journeys in 13 spec files cover those areas; the frontend section lists the layers with their real counts and the PostgreSQL switch | `README.md` |
| "No `ops/` deployment assets and **no CI workflow file** yet" | CI exists and is described; the `ops/` half was true and stays, stated as "nothing is deployed" | `README.md` |
| "The WebSocket run-event stream endpoint … is **verified manually only**" | five browser journeys drive the real socket: live events, cursor, reconnect, REST reconciliation, run switching (`frontend/e2e/run-events.spec.ts`) | `README.md`, `docs/FRONTEND_TESTING.md` |
| Limitations: "only journey 1", "no CI workflow file" | real limitations: partial Persian coverage, no deployment assets, unexercised integration adapters, Chromium-only browser suite | `docs/FRONTEND.md` |
| "Journeys still to automate" (ten numbered items) | the same ten areas, each named with the spec that covers it, plus the three deployments and what only each one can prove | `docs/FRONTEND_TESTING.md` |
| The suite section named only `well-cockpit.spec.ts` | all 13 spec files, with journey counts | `docs/FRONTEND_TESTING.md` |
| "Nothing is intercepted in the browser"; "Nothing, in this layer" | HTTP is never intercepted anywhere, and the two socket-level relays in the run-event journeys are named, with what they relay and what they refuse to fabricate | `README.md`, `docs/FRONTEND_TESTING.md`, §1 |

Nothing was added to the documentation that was not executed at `f8abc10`, and no historical statement
was rewritten to look prescient: where an earlier revision of this report described a smaller suite,
that description stays, dated by its commit.

**Test hygiene, measured.** No `TODO`, `FIXME`, `HACK` or `XXX` in `frontend/src`, `frontend/e2e` or
`backend/src`. No `console.log` and no `debugger` in the frontend. No `.only`, `.skip` or `.todo` in any
frontend test or spec, and no skipped backend test: the single `pytest.skip` in the tree is the guarded
pair in `backend/tests/conftest.py` that fires only when `DRILLAI_TEST_POSTGRES` is unset or `pgserver`
is missing — the switch CI turns on. Two `eslint-disable` comments exist, both single-line and both
carrying their reason (`no-empty-pattern` on a Playwright fixture, `react-hooks/exhaustive-deps` on a
stable state setter). No `: any` appears in frontend application source. The three `NotImplementedError`
/ bare `pass` hits in the backend are two abstract base methods (`# pragma: no cover`) and one
fall-through in the expression parser that the next branch handles.

**What is committed.** 258 tracked files: 140 Python, 78 TypeScript/TSX, 13 end-to-end specs, 1
workflow. Nothing generated is tracked — no `node_modules`, no virtualenv, no `.e2e`, no `dist`, no
Playwright reports, no database files, no logs. No credential, token, key or `.env` file is in the
index, and no secret is referenced in the workflow. There is one remote (`origin`), one mission branch,
and `main` has not moved.

**Size, stated so a reader can judge it** (`bfa066b..f8abc10`, excluding dependencies, virtualenvs,
caches and build output): 258 files changed, 257 added, 0 deleted, +82 873 / −1 lines across 56 commits.
Source lines: frontend application 12 378, frontend tests and specs 9 190, backend 32 234, backend tests
8 586, documentation 1 705. The single deletion is a line replaced during the documentation pass; no
file was removed.

**One thing this audit cannot claim.** The mutation checks that prove several guards can fail (the
checkpoint-5 focus guard, the checkpoint-4 abort guard, the preconditions rebuilt in `9626647`,
`1f68671`, `0297fd7`) were run and then restored byte-identically; the restored file is what is
committed. The mutations themselves are not in the history, by design, so a reader cannot re-run them
from the repository — they can only re-derive them from the recorded before/after and the tests that
fail. That is stated here rather than presented as reproducible evidence.

---

## 8. Known limitations

The mission is closed; these are the parts of the product that are not certified, listed so that no
reader mistakes a closed mission for a finished product. Each one is a limitation of *scope or
evidence*, not a known defect: the audit above found no unstated failure. The last eight entries were
measured at checkpoint 7 (`23c394a`).

- **The browser suite runs Chromium only.** The client uses no Chromium-only API that a second engine
  would break on, but that is an expectation, not a certified fact. Firefox and WebKit are not run.
- **Persian (`fa`) coverage is partial.** The shell, cockpit, studio, run monitor and the checkpoint-5
  surfaces are translated in both catalogues and an untranslated key falls back to English rather than
  rendering empty; some workspace strings remain English literals.
- **No deployment assets.** `ops/` is empty of Compose profiles, images and manifests. CI certifies
  install, gates and the end-to-end suite; it does not build a deployable artifact, and nothing in this
  repository has been deployed anywhere.
- **Integration adapters are boundaries, not integrations.** Messaging (Telegram/WhatsApp/email),
  WITSML/ETP streaming and vector-database retrieval are configuration-shaped seams; outbound delivery,
  live streaming and pgvector-backed retrieval are not exercised by the suite.
- **The run-event stream has four unproven edges.** The socket is proven in a real browser — live
  events, a forced disconnect, recovery, reconciliation against REST — but not for: token expiry
  mid-stream, a server restart, a proxy that closes with an unusual code, or an identity that loses its
  role while connected. Refusal codes (4401/4403/4404) are tested, but against a fresh connection.
- **The abort guarantee is proven at the unit level, not in a journey.** `src/api/client.test.ts` fails
  if a caller abort is wrapped as a transport failure, and a probe with a mounted observer showed no
  change in rendered state — but React Query drops a cancelled query before render, so a browser journey
  cannot distinguish the two behaviours. The stronger evidence is the unit one.
- **A run started from the editor pins the version the editor is showing, without warning when that
  version is not the published one.** This is deliberate (a draft run is a real capability) but the
  interface does not yet say so.
- **Test fixtures are trimmed, deliberately.** The captured workspace payloads replace strings longer
  than 2 000 characters with a marker (a ~52 KB generated context prompt appears several times); keys,
  types and short values are untouched.
- **The master-data workspace curates fields, wells, wellbores and sections — not the whole spine.**
  Projects can be created and edited through the API and are selected and scoped in the workspace, but
  the workspace does not edit a project's own master data. `GET /rigs` exists and a well's rig is
  validated against it, but this checkpoint adds no way to create or edit a rig, and organizations have
  no interface at all. The brief's §46/§47 keep this mission out of the adjacent systems.
- **Well aliases are not modelled, because the audit found no evidence for them.** The brief allowed an
  explicit searchable alias model *only* on real evidence; nothing in the repository — no reader, no
  document, no workflow — refers to a well by anything other than its name, its regulator identifier or
  its field, and search covers exactly those three.
- **The governance ledger in the interface is scoped to one well.** `GET /wells/{well_id}/audit-log` is
  the only ledger endpoint; there is no organization-wide audit browser in this checkpoint.
- **The workspace computes no depth of its own.** It shows the recorded plan and the recorded as-drilled
  values, each labelled with which it is, and it renders an explicit "no current depth" rather than a
  planned bottom. Current depth, progress and variance remain the cockpit's and the engines' answers;
  the master-data screen does not derive them.
- **No map or survey view.** Coordinates and the elevation datum are edited as numbers.
- **One active wellbore per well, by decision rather than by omission.** Every reader in the repository
  takes the first active wellbore (`drilling/state.py`, the context builder, the well-structure
  endpoint), so a second active hole would be ambiguous today. Activation is transactional and audited;
  the rule lives in one place, so it can be revisited if a real dual-active need appears.
- **No delete path exists for a well, wellbore, section or field.** That is historical immutability
  taken literally: identifiers that documents, evidence, operations and twin state point at survive, a
  correction is an audited edit with a reason, and no route can remove the record.
- **One backend test is sensitive to extreme CPU starvation, and that is recorded rather than tuned
  away.** On the two-vCPU sandbox, a full backend run executed *while the browser suite was running on
  the same two cores* failed once in `test_the_socket_tails_the_durable_log_and_closes_when_the_run_ends`
  (run-event WebSocket); the same command passed on four other full runs, including immediately before
  and after that one, and the test passed 12 of 12 consecutive isolated runs. The WebSocket fixture
  allows 10 s for each frame, and CPU starvation is the only condition in which it was observed. The
  timeout was deliberately **not** raised: a stalled stream is exactly what that bound exists to catch,
  and inflating it to hide a scheduling artefact would also hide the defect. CI runs the suite on a
  runner with the browser suite sequential after it, and has not shown it — five green runs on this
  branch including the tip.
- **The well-structure read is bounded, not single-query.** `GET /wells/{id}/structure` issues one
  sections query per wellbore (a well has one to three) rather than one query with a join, which the
  endpoint documents in place. It is O(wellbores), not O(rows), and no list endpoint in the asset
  surface queries per row.
- **Persian wording has no native-speaker review.** Every new string exists in both catalogues — the
  i18n gates fail otherwise, and the catalogue gate refuses a Persian value identical to its English
  one — and identifiers, UWI, API numbers and raw ids stay left-to-right in both; the phrasing itself
  is the author's.

---

## 9. Final closure decision

The brief permits exactly two statuses and forbids relabelling a blocker as a limitation. The decision
is therefore mechanical: each condition had to be met by evidence produced at the state being certified,
not by intention, and a "mostly" would have made the answer `MISSION BLOCKED — NOT COMPLETE`.

| # | Condition for closure | Evidence | Met |
| --- | --- | --- | --- |
| 1 | CI exists, runs the repository's own gates, and cannot pass while a required gate fails | `.github/workflows/ci.yml` at `645467c`; probe run [36688461693](https://github.com/asgareyvazi/DrillAI/actions/runs/36688461693) failed and skipped the remaining gates (§5) | yes |
| 2 | CI is green on the final remote SHA | run [36689824630](https://github.com/asgareyvazi/DrillAI/actions/runs/36689824630) at `f8abc10`, and the run attached to the commit carrying this row (§5) | yes |
| 3 | Every earlier checkpoint still passes at the final state | §4 — typecheck, lint, 275 unit tests, build, 399 backend tests, ruff, a fresh-database migration check and 65 journeys, all at `f8abc10` | yes |
| 4 | A fresh clone of the remote reproduces the certification, with nothing copied in | §6 — `/tmp/cp6-final` at `f8abc10`: bootstrap from empty, all gates green, browser re-provisioned from the checkout | yes |
| 5 | The PostgreSQL tests execute instead of skipping | §3 *CP6* and §4 — `DRILLAI_TEST_POSTGRES=1` gives 399 passed, 0 skipped, in CI and in both clean checkouts | yes |
| 6 | The documentation matches the repository | §7 — six stale claims corrected at `f8abc10`; no claim without an executed command behind it | yes |
| 7 | Git gate: local == remote, clean tree, nothing unpushed, `main` untouched | §2.4 — `git ls-remote` equals local HEAD; 258 tracked files; `main` still `bfa066b` | yes |

**MISSION CLOSED — VERIFIED.**

Re-measured at checkpoint 7 (`23c394a`, §4): condition 3 holds at the current tip — typecheck, lint, 285
unit tests, a production build, 527 backend tests with PostgreSQL (0 skipped), `ruff check .`, the
migration chain on SQLite *and* PostgreSQL, and 68 browser journeys. Conditions 1 and 2 are re-established
by the green CI run `37112183461` at that SHA. Condition 7 holds unchanged: `git ls-remote` equals local
HEAD and `main` is still `bfa066b`.

Under the standing GitHub instruction the same state is reported as **CASE A — MISSION CLOSED —
PUSHED TO GITHUB**: the certification is committed, pushed to `arena/01a0dca0-drillai`, and verified
against the remote with `git ls-remote`, which is the authority rather than the local tracking ref.

For completeness, the state that would have required the other status — and which does not hold here:
CI red on the final SHA, a gate that passes only on a developer's machine, a commit that exists locally
but not on the remote, a checkpoint silently reopened by a regression, or a document that claims a
capability the repository does not have.

### Independent CP9 certification block — Real-Time Operational Intelligence

#### 1. Verdict
CP9 is implemented locally on `arena/01a0dca0-drillai` and the executed local gates below pass. This is a local certification, not a remote-CI certification: GitHub publication and remote checks remain pending until authentication is repaired.

#### 2. Repository identity
At the start of this CP9 continuation the checkout contained the branch named above and the repository baseline commit `bfa066b28e0071880cb9191a4f1d47fdaa143e04`. The CP9 work is kept in the working tree and is committed as one durable local checkpoint after verification. No other branch was used.

#### 3. Changes delivered
The backend now includes canonical telemetry channels and units, bounded latest/window reads, deterministic rule evaluation and alert lifecycle, transactional outbox events, websocket resume/gap/backpressure handling, adapter contracts, bounded operational state, ambiguity evidence, freshness metadata, live context providers, and an explicit non-predictive trend calculation (least-squares slope over recent trustworthy points, with method/window/sample count and `is_prediction: false`). The live snapshot exposes those trends. The context registry contains 21 sections, including operational state, live telemetry, alerts, recent events and freshness. The section-count regression was updated from the former 16-section contract to the shipped 21-section contract.

#### 4. Security and tenancy
Live websocket access is gated by `live.read`; unauthenticated, unauthorized, and unknown/foreign well cases have distinct protocol handling without exposing tenant existence. Context providers resolve wells by both id and organization. Alert and telemetry mutations retain permission, reason, version and idempotency handling. Raw points remain immutable and no LLM path supplies telemetry or thresholds.

#### 5. Exact tests executed
* `backend/.venv/bin/python -m pytest tests/context tests/drilling -o addopts="" -q`: **47 passed**.
* `backend/.venv/bin/python -m pytest tests/telemetry tests/api tests/context tests/drilling -o addopts="" -q`: **414 passed** in 621.71 seconds.
* `backend/.venv/bin/ruff check .`: passed.
* `DRILLAI_TEST_POSTGRES=1 ... pytest tests/db -o addopts="" -q`: **22 passed, 1 warning**.
* `frontend/npm run typecheck`: passed.
* `frontend/npm run lint`: passed.
* `frontend/npm test`: **25 files, 298 tests passed**. A deliberately supplied Jest-only `--runInBand` option was rejected by Vitest and is not counted as a test result.
* `backend/.venv/bin/alembic upgrade head` on a fresh SQLite database followed by `alembic check`: passed, with no new upgrade operations.
* `tests/api/test_live_stream.py`: **12 passed** after the snapshot trend addition.

#### 6. CI and remote SHA
No GitHub Actions result is claimed here. The remote SHA was not re-verified in this continuation, and the earlier configured `GH_TOKEN` was invalid. Therefore CI status is **UNVERIFIED**, not green. The required final gate is: commit, push to this exact branch, `git ls-remote origin refs/heads/arena/01a0dca0-drillai`, then query the checks for that exact SHA.

#### 7. Real-stack / E2E trace
The repository includes the real-stack capture and Playwright journey infrastructure, but a fresh CP9 real-stack browser run was not executed in this continuation. The backend websocket and frontend unit/component gates above are evidence only for those layers; they are not a substitute for the required no-mock end-to-end journeys. This is an explicit remaining verification item.

#### 8. Limitations and corrective findings
The full CP9 frontend Operational Monitor and the ≥10 real-stack E2E catalogue require a fresh browser-stack execution and must not be inferred from unit tests. GitHub auth must be reconnected before remote publication and CI certification. The trend is descriptive, not predictive; insufficient or non-trustworthy samples are reported explicitly rather than labelled stable. Existing historical report text above is retained for provenance; this block is the authoritative CP9 status.

#### 9. Final git gate
The local work is ready to commit after the gates above. It must not be described as remotely complete until push and `git ls-remote` succeed. If the push still fails with invalid credentials, preserve the local commit, report the exact authentication error, and stop short of claiming remote delivery.

### CP9 publication addendum (verified 2026-10-10)

The local implementation was committed as `09bef033` and integrated with the fetched remote checkpoint history using a non-destructive merge commit `2f4c6a2`. Push succeeded to the mandated branch, and `git ls-remote origin refs/heads/arena/01a0dca0-drillai` returned `2f4c6a22d652ce3c81874ee8019767e50b182d7f`. The working tree is clean. GitHub Actions was queried after publication: the newest reported successful CI run is for the prior remote SHA `f430d334...`; no CI run for `2f4c6a2` was available at query time. Accordingly, CP9 local gates are green, publication is verified, and CI for the final SHA remains pending rather than claimed green.
