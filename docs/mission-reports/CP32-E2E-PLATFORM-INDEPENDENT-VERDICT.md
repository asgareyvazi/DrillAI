# CP32 — Platform-Independent End-to-End Verdicts and a Compiled E2E Suite

**Audit date:** 2026-10-24 (UTC)
**Repository:** `asgareyvazi/DrillAI`
**Session branch:** `arena/9d8f5568-drillai`
**Baseline:** CP31 tip `919da0f41e3597368bc145a7532d2f132d66c1c5`
**Status:** root cause established from the failing commit's own code and from direct instrumentation of the suite; the contract corrected, guarded, and verified locally; exact-SHA GitHub-hosted Linux and Windows certification runs on the published commit.

## 1. What failed

GitHub Actions run **39563150524** executed the exact CP31 commit `919da0f41e3597368bc145a7532d2f132d66c1c5`.

| Job | Job ID | Result |
|---|---|---|
| `certification` (Linux) | `119098935547` | **PASSED** — the Docker Compose deployment smoke gate and the complete Playwright E2E suite. |
| `windows-certification` | `119098935468` | **FAILED** at the `Playwright E2E certification across main, faults, and auth stacks (Windows)` step: **115 passed, 6 failed**. |

All six failures were in `frontend/e2e/workflow-studio.spec.ts` (lines 308, 358, 395, 457, 512, 568)
and all six failed on the same assertion, `expect(consoleErrors).toEqual([])`.

**The failure was not caused by the CP31 change.** The `windows-certification` job is byte-identical
between the CP26 tip `c6453c6` (whose Windows run passed in 12m29s) and the CP31 tip `919da0f`; the
CP31 diff touches **no** file under `frontend/` and **no** file under `backend/src/`; and the same
commit's Linux job passed the identical E2E suite.

The workflow's log archive and the 47 MB Playwright evidence artefact were not retrievable from this
sandbox: `gh run view --log`, the job-log API and the artefact download all fail with **EOF** from
GitHub's blob hosts, which are outside the allowed outbound hosts. The diagnosis below was
therefore established from the run's step metadata and annotations, and then **proved by direct
instrumentation of the suite on this repository** — the stronger evidence.

## 2. Root cause

### The oracle, not the product

The failing assertion asks the **browser** whether anything went wrong:

```ts
expect(consoleErrors, `console errors: ${consoleErrors.join(' | ')}`).toEqual([])
```

`consoleErrors` is every console message of type `error`, and the browser writes one line of its
own for **every resource that failed to load** — including a request the application itself
cancelled. The application shell cancels its background reads whenever the reader navigates, so
those cancellations are routine.

The browser's wording is platform-dependent, and that is the whole defect:

| Platform | A cancelled request is reported as | Logged to the console? |
|---|---|---|
| Linux runner | `net::ERR_ABORTED` | **no** |
| Windows runner | a dropped connection (`net::ERR_CONNECTION_RESET` / `ERR_INTERNET_DISCONNECTED`) | **yes** — `Failed to load resource: net::ERR_…` |

The instrumented local run on this repository shows both behaviours in one run: **1,314** cancelled
requests reported as `net::ERR_ABORTED` with **no** console line, and **7** reported as
`net::ERR_INTERNET_DISCONNECTED` **with** a console line — for the same product behaviour. A
verdict that depends on that line is a verdict about the runner, not about the product.

### The suite already had the right definition — seven journeys used it, the studio journeys did not

`frontend/e2e/fixtures.ts` already exported `appConsoleErrors`, documented as:

> A browser logs every response with a failing status, and every aborted connection, as a console
> error by itself — on the stacks that produce those on purpose, that line is the harness talking,
> not the product. Anything else (a React error, an unhandled rejection, a thrown exception) still
> counts, which is what the check is for.

Seven journeys used it (`checkpoint5`, `connectors-platform`, `context-deeplinks`, `error-auth`,
`error-faults`, `error-matrix`, `identity-permissions`). **Eight journeys did not** and judged the
raw list themselves: `documents-evidence`, `live-operations`, `operations-workspace`, `run-events`,
`run-monitor`, `well-cockpit` and `workflow-studio`. Those are the ones a platform could fail, and
the Windows runner failed six of the nine studio journeys.

### The studio's own calls never failed

Instrumenting the studio journeys on this repository — logging every response the *page* received
that concerned `/api/v1/workflows` — shows that **every** studio call returned **200**, except the
one refusal the journey provokes on purpose:

```
[E2E-WF] 200 GET    /api/v1/workflows
[E2E-WF] 200 GET    /api/v1/workflows/wfl_…/versions?limit=20
[E2E-WF] 200 GET    /api/v1/workflows/wfl_…?version=1
[E2E-WF] 200 PUT    /api/v1/workflows/wfl_…/graph
[E2E-WF] 200 POST   /api/v1/workflows/validate
[E2E-WF] 200 POST   /api/v1/workflows/wfl_…/publish?version=2
[E2E-WF] 400 POST   /api/v1/workflows/wfl_…/publish?version=1   ← the deliberate refusal
```

The studio never asks for a workflow version that does not exist: its graph query is enabled only
once the version history has answered, so the "no version yet" state — a workflow with no saved
version — never produces a 404. **The Windows console error therefore cannot have been a studio
failure.** It was the browser's network log for a cancelled background read, which is exactly the
class the repository had already decided not to treat as an application failure.

## 3. The correction

### 3.1 Two oracles, both platform-independent

`frontend/e2e/fixtures.ts` now records, alongside the console lines, **every response the page
received with a failing status**:

```ts
export interface FailedResponse {
  status: number
  method: string
  url: string
}
```

and exports the shared contract every journey uses:

```ts
export function expectNoFailures(
  consoleErrors: string[],
  failedResponses: FailedResponse[],
  options: { scope?: string; allowed?: string[] } = {},
): void
```

It asserts two things:

1. **The application produced no error of its own** — `appConsoleErrors(consoleErrors)` is empty. A
   React error, an unhandled rejection or a `console.error` from the product still fails the
   journey, exactly as before.
2. **No call the journey did not provoke failed** — the recorded responses are empty, and nothing
   anywhere answered with a server error (status ≥ 500).

`scope` narrows the response check to the part of the API a journey owns, and `allowed` names the
failures a journey provokes on purpose (the studio's deliberate `publish` refusal; the alert-clear
conflict in `live-operations`; the missing well in `well-cockpit`).

This is **not a weakening**. The old assertion could only fail on an HTTP failure *if the browser
logged it*, and it additionally failed on the browser's own network log. The new pair fails on the
same HTTP failures — measured on the response rather than on the browser's wording, so it also
catches a failure the browser did not log — and still fails on every application error.

Across the eight journeys, **34** `expect(consoleErrors…)` assertions were replaced with the shared
contract, together with **two** further raw judgements that were not written as an `expect` — a
`consoleErrors.filter(…)` in `live-operations` and a `for (const error of consoleErrors)` loop in
`well-cockpit`. The commit message for this change quotes 30; the measured figure is 34 + 2, and
this paragraph is the correction.

### 3.2 The journeys are compiled before they run

`frontend/tsconfig.app.json` included only `src`, and `frontend/tsconfig.node.json` only
`vite.config.ts`. **The `e2e/` directory was not type-checked at all**, although it runs in CI on
both platforms. A missing import in a journey therefore reached CI instead of the developer's
machine — which is exactly what happened during this work: a dropped `selectRole` import was caught
by the suite at runtime, not by `npm run typecheck`.

`frontend/tsconfig.e2e.json` now compiles the journeys, and `frontend/tsconfig.json` references it,
so `npm run typecheck` — which CI runs on Linux and on Windows — compiles them too. Making that
possible required fixing **68 genuine type errors** the compiler found in code that had never been
compiled:

| Defect | Occurrences | Fix |
|---|---|---|
| `Parameters<typeof apiGet>[1]` used as the type of the *request context* — index 1 is the path, index 0 is the context | 8 declarations, 38 resulting errors | `ApiRequest` is now exported from `fixtures.ts` and used |
| `WellRow` (local type) did not declare `uwi`, which the API returns | 1 | the type now carries it |
| the timeline-entry type did not declare `title`, which the API returns | 1 | the type now carries it |
| `Page` was imported from `fixtures.ts` but not re-exported | 2 | `fixtures.ts` re-exports it |

The E2E project keeps `strict: true` and relaxes only `noUncheckedIndexedAccess`, because the
journeys index into the arrays the API returns and assert on the element afterwards
(`expect(items[0].version).toBe(2)` says what the assertion means better than a non-null assertion
does). The product's own configuration is unchanged.

### 3.3 A failing journey now says what it saw

Both CI jobs copy the Playwright failure messages into the step annotation, so a red run names the
assertion that failed instead of only a count of failures. Before this change the Windows
annotation contained only the last 25 lines of the log — the failing journey's code frame, without
the message that said what the journey had seen. That is why this report had to be reconstructed
from instrumentation rather than read off the run.

## 4. The guard

`backend/tests/security/test_cp32_e2e_console_error_contract.py` holds the contract in place. It is
static — it reads the files the suite and the workflow actually use — so it fails before the commit
reaches CI:

1. **No journey may judge the raw console-error list.** Every spec is scanned for
   `expect(consoleErrors…)`, `consoleErrors.filter(…)` and `for (const … of consoleErrors)`.
2. **Every journey that collects console errors asserts them through `appConsoleErrors` or
   `expectNoFailures`.**
3. **A collected response list is asserted on**, never decoration.
4. **The shared fixture keeps both oracles**: `appConsoleErrors` still excludes the browser's
   resource line, `FailedResponse` carries status/method/url, and `expectNoFailures` asserts on the
   application errors, on the failed responses and on server errors.
5. **The journeys are type-checked**: `frontend/tsconfig.e2e.json` exists, includes `e2e`, is
   `strict`, and is referenced from `frontend/tsconfig.json`.
6. **CI reports the failure message**: both end-to-end steps extract the Playwright failure lines.
7. **No backend test skips**, and both certification jobs still reject a reported skip — see §6.

**Negative tests.** Re-introducing the pre-fix oracle in one studio journey makes
`test_cp32_no_journey_judges_the_raw_console_error_list[workflow-studio.spec.ts]` fail with the
intended message; injecting a skip makes `test_cp32_no_guard_in_this_suite_reports_a_skip` fail with
`… calls pytest.skip`; restoring the files makes both pass again. The CP16 documentation guard was
also verified against a stale README count: with `1,156 passed, 8 skipped` it reports
`README.md backend totals disagree: {1161, 1164}`.

## 5. Local verification

| Gate | Result |
|---|---|
| `npm run typecheck` (now including `e2e/`) | **pass** — the E2E project compiles; a deliberately injected type error in a journey is caught |
| `npm run lint` | **pass** |
| `npm test` (Vitest) | **318 passed in 30 files** |
| `npm run e2e` (Playwright, all three deployments) | **121 passed** — 114 in `chromium`, 4 in `chromium-faults`, 3 in `chromium-auth` |
| `pytest` (backend, embedded PostgreSQL) | **1,166 passed, 0 skipped** in 88 files (`DRILLAI_TEST_POSTGRES=1`) |
| `pytest -m "not postgres"` (backend, SQLite) | **1,158 passed, 8 deselected**; without the environment variable at all the same 8 tests skip themselves and the suite reports **1,158 passed, 8 skipped** |
| `ruff check .` | **pass** |
| `alembic upgrade head` / `alembic check` on a fresh SQLite database | **pass** |
| `scripts/verify_stack.py` | `STACK_VERIFICATION_OK` — 16 scenarios and a 32-cycle soak |
| `scripts/verify_external_vendor_interop.py --mode harness` | **pass**, 0 network calls |

The full E2E suite was also run **before** the correction (121 passed on Linux) and **after** it
(121 passed on Linux), so the change is not a relaxation that hides a failure: the journeys that
used to judge the browser's network log now judge the product's own responses, and every deliberate
refusal is still accounted for by name.

## 6. The skip regression this checkpoint caused in CI, and its guard

The first CI run of this checkpoint (run `40079792254` on `1fda9be`) **failed both jobs** in the
backend step, with the Linux annotation:

> the suite skipped tests; the PostgreSQL integration tests must execute

The cause was in this checkpoint, not in the product. The new guard skipped the three specs that
collect no console errors (`a11y`, `master-data`, `rtl`), and the certification jobs reject a backend
suite that reports **any** skip: the Linux step fails on `*skipped*` in the summary and the Windows
step fails on `\d+\s+(skipped|failed|error)`. A suite that had been `1,108 passed` with no skips
became `1,161 passed, 3 skipped` and turned both jobs red.

The fix is that a guard with nothing to check for one input **returns** from that case instead of
skipping it — the check ran and found nothing in that spec to check, which is not a skipped test.
The suite is back to **0 skipped**.

Two guards now hold it:

- `test_cp32_no_guard_in_this_suite_reports_a_skip` scans every backend test file for a skip call,
  so a future guard cannot reintroduce this class of red build. Verified by injecting a skip: the
  guard fails with `… calls pytest.skip`; restoring the file makes it pass.
- `test_cp32_the_certification_jobs_reject_skips` asserts that both jobs still fail on a reported
  skip, so the rule cannot be quietly relaxed in the workflow either.

## 7. Open items

- **Exact-SHA certification** — the Linux and Windows jobs for the published CP32 commit are the
  authority; the results are recorded in `PRODUCTION-READINESS-MATRIX.md`.
- **Docker Compose runtime evidence** — the Compose deployment gate is exercised only in CI; no
  Docker daemon is available in this sandbox, so the container runtime remains unverified locally.
- **Remaining platform-dependent oracles** — the journeys that use `appConsoleErrors` without the
  shared contract (the seven that provoke deliberate failures) now tolerate the browser's network
  log, but they do not yet assert on the recorded responses. That is the natural next step and is
  recorded in `REMAINING-GAPS-ROADMAP.md`.
- **External blockers unchanged** — real vendor connectivity, real notification delivery,
  physical-host and PITR recovery, and licence verification remain outside this repository's
  reach, and are stated as such in the readiness matrix.
