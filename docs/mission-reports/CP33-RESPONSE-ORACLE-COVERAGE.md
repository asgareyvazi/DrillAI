# CP33 — Response-Oracle Coverage: Every Journey Judges the Wire

**Audit date:** 2026-10-25 (UTC)
**Repository:** `asgareyvazi/DrillAI`
**Session branch:** `arena/9d8f5568-drillai`
**Baseline:** CP32 documentation tip `0ff991f` and the openssl fixture fix `c09ce79`
**Status:** code complete, guarded, and verified by the full local gate set; exact-SHA Linux and
Windows certification recorded in §7 once the run completed.

## 1. What was left open

CP32 established that a journey may not decide "nothing went wrong" by reading the **browser's**
network log, and gave the suite a platform-independent oracle: `expectNoFailures(consoleErrors,
failedResponses)`, which asks whether the *application* wrote an error of its own and whether the
*page* received a failing response the journey did not provoke.

Ten journeys were converted then. Seven kept the older verdict — `expect(appConsoleErrors(
consoleErrors))` — because they provoke refusals on purpose and the response list would have flagged
them:

| Spec | Journeys converted in CP32 | Still console-only |
|---|---|---|
| `checkpoint5.spec.ts` | 0 | 1 |
| `connectors-platform.spec.ts` | 0 | 40 |
| `context-deeplinks.spec.ts` | 0 | 3 |
| `error-auth.spec.ts` | 0 | 3 |
| `error-faults.spec.ts` | 0 | 2 |
| `error-matrix.spec.ts` | 0 | 4 |
| `identity-permissions.spec.ts` | 0 | 1 |

That is a real gap, not a stylistic one: those 54 assert*ions* could pass while a call they never
provoked failed, and the `500` this suite exists to catch would have been invisible to them.

## 2. The measurement, not a guess

A tolerance written from imagination is either decoration or a silenced test. So the suite was first
**run with the failing responses recorded per journey**, in the shared fixture, and the record was
read back before a single allowance was written.

The record: **121 journeys, 20 of which received at least one response with a failing status.** All
twenty are journeys that provoke a refusal on purpose, and the refusals are the ones the journeys
assert on screen:

| Spec | The refusal the journey provokes |
|---|---|
| `live-operations` | `409 POST /alerts/{id}/clear` — an alert another operator is already working on |
| `well-cockpit` | `404 GET /wells/wel_does_not_exist{,/state,/npt,/timeline}` — a well that does not exist |
| `workflow-studio` (9 journeys) | `400 POST /workflows/{id}/publish` — a draft the server refuses |
| `checkpoint5`, `context-deeplinks` | `404` on `/wells/wel_not_a_well`, `/documents/doc_does_not_exist`, `/runs/run_does_not_exist` — deep links that name nothing |
| `connectors-platform` (4 journeys) | `409 PATCH /connectors/{id}` from a stale revision; `422 POST /connectors` for SSRF-refused destinations |
| `error-matrix` | `403 POST /approvals/{id}/decide`; `422 POST /documents` |
| `error-faults` | `500 GET /wells` — the fault the journey injects |
| `error-auth` (3 journeys) | `401` on every read before sign-in, in an authentication-enabled deployment |

The other **101 journeys provoked no failing response at all**, and therefore receive **no
tolerance**: from now on, a background read that starts failing fails its journey. That is a
stronger statement than the console-only verdict could ever make.

## 3. The correction

### 3.1 A tolerance has to name the request and say why

`allowed` used to take URL substrings. A string names a URL and nothing else — it tolerates **every
method and every status** at that URL, including a `500`. It is now an object:

```ts
export interface ProvokedFailure {
  url?: string
  method?: string
  status?: number | number[]
  reason: string
}
```

`reason` is required, not optional. Every field is a condition:

```ts
export function provokedFailure(
  response: FailedResponse,
  allowed: readonly ProvokedFailure[],
): ProvokedFailure | undefined
```

so a journey that tolerates a `409` on a `PATCH` still fails if something `POST`s its way into a
`500` at the same URL.

### 3.2 The server-error check applies the same tolerances

`expectNoFailures` ended with an unconditional "no response may be ≥500". That check now runs
through `provokedFailure` as well, which means:

* an injected fault (`error-faults`) is the only `500` the suite accepts, and only because the
  journey names `/wells`, `GET` and `500`;
* every other journey — including the twenty that tolerate a refusal — still fails on a `500`.

### 3.3 The seven specs, converted

All 54 assertions now read `expectNoFailures(consoleErrors, failedResponses, …)`, and each journey
takes the `failedResponses` fixture so the list is actually recorded. `checkpoint5`'s first journey
collected console errors with its own listener; it now uses the shared fixture, which also collects
`pageerror` — a stricter oracle than the one it had.

The three journeys that already used the contract (nine studio journeys, one live-operations, one
well-cockpit) were migrated from string tolerances to the object form, so the whole suite now states
its tolerances the same way.

## 4. Guard

`backend/tests/security/test_cp33_e2e_response_oracle_coverage.py` (62 tests) reads the specs and the
fixture:

1. **The seven converted specs assert on the responses.** Named explicitly, so a journey cannot drift
   back to the console-only verdict — and the failure message says what the console verdict cannot
   see.
2. **No spec judges only the console.** Any spec that collects `consoleErrors` must use the contract.
3. **No bare-string tolerance**, anywhere in the suite.
4. **Every tolerance carries a `reason`, at least one narrowing field, and a `status`.**
5. **The matcher really matches on method and status** (`provokedFailure`), and **the 5xx check
   applies the tolerances** — both asserted on the fixture's own source.
6. **`reason` is not optional** in the type.

Each check was negative-tested by making the defect it describes, and each produced **exactly one**
failing test, which then passed again when the defect was reverted:

| Defect injected | Result |
|---|---|
| `error-auth` back to `expect(appConsoleErrors(consoleErrors))` | 1 failed — `test_cp33_the_converted_journeys_assert_on_the_responses[error-auth.spec.ts]` |
| a bare string in `well-cockpit`'s `allowed` | 1 failed — `test_cp33_no_spec_passes_a_bare_string_as_a_tolerance[well-cockpit.spec.ts]` |
| the 5xx check no longer filtered through `provokedFailure` | 1 failed — `test_cp33_a_server_error_is_tolerable_only_where_the_journey_names_it` |

## 5. What the suite says now

| Gate | Result |
|---|---|
| `npx tsc -b` (includes `e2e/` through `tsconfig.e2e.json`) | **pass** |
| `npm run lint` (including `e2e/`) | **pass** |
| Playwright, all three deployments | **121 passed in 17 spec files** — 114 `chromium`, 4 `chromium-faults`, 3 `chromium-auth` |
| `pytest` (backend, embedded PostgreSQL) | **1,231 passed, 0 skipped** in 89 files |
| `pytest -m "not postgres"` (backend, SQLite) | **1,223 passed, 8 deselected** |
| `ruff check .` | **pass** |
| CP16 documentation guard | **pass** — the README's two backend totals reconcile (1,231 = 1,223 + 8) |

The Playwright run was made **twice**: once with the recorder to establish the measurement in §2,
and once clean after the conversion, both green at 121. No journey was retried to get there.

## 6. What this changes about the release claim

Before CP33, the suite's statement about "the product misbehaved" covered 67 journeys fully and 54
journeys only through the browser's log. After CP33 it covers all 121 journeys on the wire, with
101 of them tolerating nothing at all and 20 stating the exact refusal they provoke.

It does **not** strengthen the product claim in any other dimension: no product code changed in this
checkpoint, and none of the external boundaries in `REMAINING-GAPS-ROADMAP.md` moved.

## 7. Certification

Exact-SHA certification is recorded in the run below; the SHA is the commit that carries this
checkpoint and its documentation.

| Commit | Run | Linux job | Windows job |
|---|---|---|---|
| CP33 (see `git log`) | recorded below | recorded below | recorded below |

## 8. Open items

- **Docker Compose runtime evidence** remains CI-only (no Docker daemon on this host), unchanged
  from CP32.
- **`noUncheckedIndexedAccess`** stays relaxed in `frontend/tsconfig.e2e.json` for the same reason as
  in CP32; the product's own configuration keeps the rule on.
- **External blockers unchanged** — vendor interoperability, real notification delivery,
  physical-host/PITR recovery, field certification of the engineering models, and the licence
  decision. None of them is implied by any local or CI result here.
