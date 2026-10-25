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
| `pytest` (backend, embedded PostgreSQL) | **1,169 passed, 0 skipped** in 88 files (`DRILLAI_TEST_POSTGRES=1`) |
| `pytest -m "not postgres"` (backend, SQLite) | **1,161 passed, 8 deselected**; without the environment variable at all the same 8 tests skip themselves and the suite reports **1,161 passed, 8 skipped** |
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

## 7. Exact-SHA certification

**GitHub Actions run `40085939081`** executed the exact CP32 commit
`08edab3682f7e3877288d819f5189f646ac59d2d` on 2026-10-24T10:26:48Z. **Both jobs passed** and the run's
overall conclusion is `success`. This is the certified release candidate for this series.

| Job | Job ID | Result | Duration |
|---|---|---|---|
| `certification` (Linux) | `120848768971` | **PASSED** — no failure annotation | 10:26:50 → 10:38:16 |
| `windows-certification` (Windows) | `120848768814` | **PASSED** — no failure annotation | 10:26:52 → 10:43:10 |

Every step succeeded in both jobs, including the three that had failed earlier in the series:

- **Linux `Docker Compose deployment smoke gate (docker-compose.yml)`** — passed. This is the gate
  that failed with exit code 22 on `c6453c6` (CP31's root cause), and it is the only container-runtime
  evidence in this series: the sandbox has no Docker daemon, so the gate's authority is CI.
- **The end-to-end suites in both jobs** — passed on all three deployments (main, faults, auth). The
  Windows job ran `Playwright E2E certification across main, faults, and auth stacks` to success: the
  step that failed on `919da0f` with six studio journeys and again on `34979de` with the retried
  commissioning journey.
- **`Backend tests (SQLite + embedded PostgreSQL)` (Linux) and `Backend SQLite test suite` (Windows)**
  — passed with **0 skips**, which the Linux step enforces explicitly.

Also green on the exact SHA: frontend typecheck (compiling the `e2e/` specs on both platforms), lint,
318 unit tests, production build, supply-chain/SHA-pinning/secret scan, backend lint, Alembic
migration check on a fresh database, `verify_stack.py`'s 16 multi-process scenarios and 32-cycle
soak, and the external-vendor interop runner.

The two `Upload … Playwright evidence` steps report `skipped` because they are `if: failure()`. That
is the step's design and not a skipped test: no suite skipped anything on this run.

### The path to green, stated plainly

| SHA | Run | Linux | Windows | Why |
|---|---|---|---|---|
| `bfea530` (baseline) | `39217548722` | fail | fail | the regressions CP25 was created to fix |
| `c6453c6` (CP26) | `39414613792` | fail | pass | Linux: HEAD probe against a GET-only route (exit 22) |
| `919da0f` (CP31) | `39563150524` | **pass** | fail | Windows: six journeys judged the browser's network log |
| `1fda9be` (CP32) | `40079792254` | fail | fail | this checkpoint's guard introduced three test skips |
| `dded419` (CP32 fix) | `40081444618` | **pass** | **pass** | first fully green run of the series |
| `34979de` (CP32 docs) | `40082345895` | pass | fail | Windows: a flake, not a regression — a retried journey's commissioning was a duplicate replay; root-caused and fixed in §8 |
| `08edab3` (CP32 fixes) | `40085939081` | **pass** | **pass** | **certified** — every step green on both platforms |
| `0547d9f` (CP32 docs) | `40089277512` | pass | pass | — |
| `0ff991f` (CP32 docs) | `40090092673` | pass | fail | Windows: a third flake — an `openssl` key-generation stopwatch in the backend suite; fixed in §10 |
| `c09ce79` (CP32 fix) | `40165921576` | **pass** | **pass** | **certified** — the openssl bound, every step green on both platforms (Linux job `121086820132`, Windows job `121086819921`) |

Each failure is recorded with its cause because a release claim is only as good as the chain of
evidence behind it. Two of these failures were caused by this checkpoint (`1fda9be`, and the flake
that `34979de` exposed); both are fixed and named, not written off.

**Documentation tips are certified too.** Every documentation commit after the code was fixed has
also run green on both platforms, so the result is not limited to the commit that happened to fix the
code:

| SHA | Run | Job results |
|---|---|---|
| `448b8a2` (certification record) | `40087070667` | Linux `120852077973` 10:43:57 → 10:55:31 · Windows `120852077882` 10:43:57 → 10:59:41 — both **pass** |
| `df9a1f8` (tip certification record) | `40088126918` | Linux `120855119661` 11:00:16 → 11:11:34 · Windows `120855119554` 11:00:16 → 11:16:26 — both **pass** |
| `0547d9f` (tip certification record) | `40089277512` | Linux `120858446431` 11:17:05 → 11:26:11 · Windows `120858446123` 11:17:05 → 11:28:27 — both **pass**, zero non-success steps |

Each failure is recorded with its cause because a release claim is only as good as the chain of
evidence behind it. The `1fda9be` failure was caused by this checkpoint and is fixed in `dded419`,
not written off.

## 8. The Windows E2E failure on the docs-only commit, and its root cause

Run `40082345895` executed `34979de` — a documentation-only commit — and the Windows job failed the
Playwright step with **120 passed, 1 failed**. The same code had passed on `dded419` minutes earlier,
so the run was not evidence of a regression in that commit; it was a flake. It was investigated
rather than re-run, because a re-run is not available (`gh run rerun` refuses this workflow and the
REST endpoint returns 403 for this token) and because a flake with a nameable cause is a defect.

### What the annotation showed

Two different assertions failed in the same journey, one per attempt:

| Attempt | Failed at | What it means |
|---|---|---|
| first | a `toHaveText` assertion inside the CP10 journey | the journey got *past* the alert assertion; the alert had been raised |
| retry (`retries: 1`) | `live-operations.spec.ts:99`, `alerts_raised` — expected `>= 1`, received `0` | the retry's commissioning run raised nothing |

A retry that fails at a point the first attempt passed is a statement about the **state the first
attempt left behind**, not about the code.

### Reproduced locally, deterministically

Running the journey twice against one database — exactly what a retry does — reproduces it:

```
npx playwright test e2e/live-operations.spec.ts --repeat-each=2
  ✓ repeat 0   … (passed)
  ✘ repeat 1   … expect(commissionResp.report.alerts_raised).toBeGreaterThanOrEqual(1)
                  Expected: >= 1   Received: 0
```

### Root cause

`SyntheticAdapter` derived each frame's `source_point_id` from `(channel, plan index)` alone:

```python
source_point_id=f"{self.key}:{channel_key}:{index}"   # "synthetic:spp:0", "synthetic:spp:1"
```

Ingestion is idempotent on `source_point_id`, which is right for a source replaying the same
readings. But two *commissioning runs* are two declarations of measurements, and they produced the
**same identities**. So the second run's points were counted as duplicates: nothing was written, no
rule was evaluated over new data, and the response honestly reported `alerts_raised: 0`. The database
after the failing run shows it — the second run's rule exists, and no alert was ever raised for it,
because its points never arrived.

The Windows failure is this: the first attempt failed on a UI assertion, leaving an open alert
behind; the retry commissioned the same plan, its points were duplicates, and a *new* rule (the
journey's rule key carries `Date.now()`) had no new data to judge.

### The fix

The adapter takes a `run_id` that scopes the **identity** of its measurements, not their values:

```python
def _source_point_id(self, channel_key: str, index: int) -> str:
    prefix = f"{self.key}:{self._run_id}" if self._run_id else self.key
    return f"{prefix}:{channel_key}:{index}"
```

`POST /wells/{well_id}/timeseries/commission-synthetic` passes a fresh `new_id("cmr")` for each run,
so two commissioning runs write two sets of points and each is evaluated. The measurements stay
exactly as deterministic as before — same timestamps, values, units and qualities — and a caller that
does not pass a run id keeps the historical `"synthetic:spp:0"` identity. An explicit
`Idempotency-Key` still replays, because that is the caller saying the same request is the same
request.

Regression coverage:

- `tests/api/test_live_stream.py::test_repeated_synthetic_commissioning_is_two_sets_of_measurements`
  — a second run writes its own points (`accepted: 2`, `duplicates: 0`); one open condition stays one
  alert; a rule added after the first run is raised by the next run's own measurements; the same
  idempotency key still replays. **Verified to fail without the fix**
  (`AssertionError: a repeat run must write its own points`) and to pass with it.
- `tests/telemetry/test_adapters.py::test_a_run_id_scopes_the_identity_of_the_measurements` — the
  identity differs per run, the historical identity is unchanged when no run id is given, and the
  measurement itself is untouched.
- The journey itself, run three times against one database
  (`--repeat-each=3`): **9 passed**, where it previously failed on the second repeat.

## 9. Two more defects the investigation surfaced

Neither was reported by CI; both were found while making the Windows failure reproducible.

### 9.1 A run's last events could be lost when its stream closed

`tests/api/test_run_event_stream.py::test_the_socket_tails_the_durable_log_and_closes_when_the_run_ends`
failed once during a full-suite run (`1 failed, 1167 passed`) and passed in isolation. The failure was
`min() arg is an empty sequence` — the socket replayed the durable log and then delivered
`stream_closed` **without a single live event**, so the run's final events never arrived on the socket
that was watching it.

The cause is a two-statement read in the stream endpoint:

```python
rows = await session.execute(select(RunEvent).where(RunEvent.seq > sequence, ...))   # statement 1
run  = await session.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))    # statement 2
if run.status in TERMINAL:
    await websocket.send_json({"type": "stream_closed", ...}); await websocket.close()
```

The events and the terminal status are written by **one** transaction, but they are read by **two**
statements under READ COMMITTED. If that transaction commits between them, the status read shows the
run finished while the event read — already done — does not contain what it wrote. The loop then
closes the stream and the tail is lost from the stream. The events stay in the durable log, so a
client that reconnects with `after_seq` recovers them; what is broken is the promise that the stream
tails the log and closes at the end.

The fix reads the log once more before closing, which is guaranteed to be complete because a terminal
status means everything the run wrote is committed:

```python
if run is not None and run.status in TERMINAL:
    trailing = await session.execute(select(RunEvent).where(RunEvent.seq > sequence, ...))
    for row in trailing: ...send...
    await websocket.send_json({"type": "stream_closed", "status": run.status})
```

**Honest limit:** this race is not deterministically reproducible in a test — it needs a commit to
land between two statements of the same tick. The existing test asserts the contract (the stream
delivers the whole log, in order, once, and then closes) and caught it once under full-suite load; no
new test was added that claims to force the interleaving, because one that cannot fail reliably would
be a false guard.

### 9.2 The suite's own duplicate-replay defect

See §8: `SyntheticAdapter` gave every commissioning run the same point identities, so a repeated or
retried run was a silent duplicate replay. That one **is** deterministically reproducible and now has
two regression tests.

## 10. A third flake: a stopwatch on key generation

Run `40090092673` executed `0ff991f` — another documentation commit — and the **Windows backend step**
failed on an unrelated test:

```
tests/telemetry/test_cp13_ssrf_and_egress.py::test_s14_tls_hostname_verification_on_approved_pinned_ip
subprocess.TimeoutExpired: openssl req -x509 -newkey rsa:2048 ... timed out after 15 seconds
1 failed, 1159 passed, 8 deselected
```

The test is about TLS hostname verification against an approved pinned IP. Its fixture generates a
real self-signed certificate with `openssl req -newkey rsa:2048` and bounded that call at **15
seconds**. On the Windows runner the key generation alone exceeded that — a first invocation of a
freshly installed binary with no cached entropy, on a machine that also runs an antivirus. The TLS
behaviour being tested was never reached; the suite went red because a machine was slow.

Both certificate fixtures (in `test_cp13_ssrf_and_egress.py` and
`test_cp14_dns_and_transport_ssrf.py`) now bound the call with one shared
`_OPENSSL_TIMEOUT_SECONDS = 120`. The certificate is exactly as real; only the patience is larger,
and it still bounds a hung tool. A guard,
`test_the_certificate_fixture_never_treats_a_slow_machine_as_a_tls_failure`, asserts the bound is
generous and that neither fixture has drifted back to a stopwatch.

That is three flakes in this series, each with a different cause and each fixed at its cause rather
than retried: the browser's platform-dependent network log (CP32's main subject), a duplicate-replay
commissioning run (§8), and a stopwatch on key generation (§10). None of them was a product defect in
the sense of wrong engineering output; two of them were defects in what the tests measured, and one
(the silent duplicate replay) was a real defect in the commissioning path.

### The openssl bound, certified

Run `40165921576` executed `c09ce79` — the commit that replaced the fifteen-second stopwatch — and
**both jobs passed**, every step green: Linux `121086820132` and Windows `121086819921`. The
`test_s14_tls_hostname_verification_on_approved_pinned_ip` test that had failed on Windows now runs
in the Windows backend suite with the certificate it always generated.

## 11. Open items

- **Docker Compose runtime evidence** — the Compose deployment gate **passed in CI** on the
  certified SHA (run `40081444618`, Linux job `120835678251`). No Docker daemon exists in this
  sandbox, so the container runtime was never exercised on the development host itself; the CI
  result is the only container-runtime evidence and it is not a deployment to a real environment.
- ~~**Remaining platform-dependent oracles**~~ — **closed by CP33.** The seven journeys named here
  (`checkpoint5`, `connectors-platform`, `context-deeplinks`, `error-auth`, `error-matrix`,
  `identity-permissions`, and the two `error-faults` journeys) asserted only through
  `appConsoleErrors`. Every one of the suite's 54 remaining raw assertions now uses
  `expectNoFailures`, and the tolerances are objects that name the request and the status and say
  why. See `CP33-RESPONSE-ORACLE-COVERAGE.md`.
- **External blockers unchanged** — real vendor connectivity, real notification delivery,
  physical-host and PITR recovery, and licence verification remain outside this repository's
  reach, and are stated as such in the readiness matrix.
- **What "certified" does and does not mean here** — both CI jobs are green on the exact SHA, which
  is platform, toolchain and journey certification of this repository. It is not a deployment to a
  production environment, not vendor interoperability, not disaster-recovery evidence for a physical
  host, not field validation of any engineering output, and not a licence decision. Those remain
  blocked and are recorded as blocked.
