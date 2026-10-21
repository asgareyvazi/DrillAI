# CP31 — CI Deployment Probe Contract and Linux Compose Gate Repair

**Audit date:** 2026-10-21 (UTC)
**Repository:** `asgareyvazi/DrillAI`
**Session branch:** `arena/9d8f5568-drillai`
**Baseline:** CP26 tip `c6453c6f4550fb13b445df9c96b270448dd68dbb`
**Status:** defect reproduced locally, corrected in the workflow, and guarded by a regression test that fails on the uncorrected workflow; exact-SHA GitHub-hosted Linux and Windows certification is pending the push of this checkpoint.

## 1. The defect

GitHub Actions run **39414613792** executed the exact CP26 commit `c6453c6f4550fb13b445df9c96b270448dd68dbb` on 2026-10-20T11:23:32Z.

| Job | Job ID | Result |
|---|---|---|
| `windows-certification` | `118600471702` | **PASSED** in 12m29s — full frontend typecheck, lint, 318 unit tests, build, supply-chain scan, backend lint, migration check, stack verification, external-vendor runner and the complete Playwright E2E suite across the main, faults and auth deployments. |
| `certification` (Linux) | `118600471494` | **FAILED** at the `Docker Compose deployment smoke gate (docker-compose.yml)` step, exit code **22**. Every preceding Linux step succeeded; the Playwright E2E step was skipped because the gate failed first. |

The workflow's full log archive was not retrievable: `gh run view --log-failed` and
`gh api .../actions/jobs/118600471494/logs` both returned **EOF** from GitHub's log-archive/blob
hosts, which are outside this sandbox's allowed outbound hosts. The finding below was therefore
established from the step-level job metadata and the annotation
`Process completed with exit code 22`, and then **reproduced locally**, which is the stronger
evidence.

### Root cause

The Linux Compose gate ended with two header probes:

```bash
curl -fsSI http://127.0.0.1:8080/api/v1/health/ready | grep -iq 'x-content-type-options: nosniff'
curl -fsSI http://127.0.0.1:8080/api/v1/health/ready | grep -iq 'content-security-policy:'
```

`-I` makes `curl` send a **HEAD** request. `/api/v1/health/ready` is registered with FastAPI's
`@router.get(...)`, and FastAPI's `APIRoute` sets `route.methods` to exactly the declared methods —
unlike Starlette's plain `Route`, it does **not** add `HEAD` to a `GET` route. The request was
therefore answered with **405 Method Not Allowed**.

`curl -f` turns an HTTP error response into exit code **22**, and the step runs under
`set -euo pipefail`, so the pipeline's status is 22 and the job dies. That is exactly the exit code
GitHub reported. The gate failed for a reason entirely unrelated to the deployment it was checking:
the deployment was healthy, the probe was malformed.

### Local reproduction

Against a live server started from this repository (`uvicorn drillai.api.app:create_app --factory`,
a migrated SQLite database, `DRILLAI_AUTH_ENABLED=false`):

```bash
curl -fsSI http://127.0.0.1:8765/api/v1/health/ready | grep -iq 'x-content-type-options: nosniff'
# curl: (22) The requested URL returned error: 405
# exit status 22
```

The same script with the corrected probes passed all four assertions:

```
[1] backend readiness payload OK
[2] nosniff header OK
[3] CSP header OK
[4] connector profiles OK
ALL_GATE_PROBES_PASSED   (exit 0)
```

## 2. The correction

| Change | Rationale |
|---|---|
| Both header probes now use `curl -fsS -D - -o /dev/null <url>` | `GET` is a method the route serves, so the response is `200`. `-D -` writes the response headers to stdout, so `grep` still inspects the headers; `-o /dev/null` discards the body. The assertions are unchanged in meaning — the same two headers are still required. |
| The readiness wait loop now sets an explicit `ready` flag and reports `DEPLOYMENT_GATE_FAILED: the stack did not become ready within 90 seconds.` | The loop previously fell through silently after 45 attempts, so an unreachable stack and a malformed probe produced the same symptom. |
| An `ERR` trap prints `DEPLOYMENT_GATE_FAILED at line <n>: <command>` | `curl -f`'s exit code 22 identifies neither the failing probe nor its line. The trap names both, so the next occurrence of this class of failure is diagnosable from the step output alone. |
| A new regression test, `backend/tests/security/test_cp31_ci_deployment_probe_contract.py`, reads the workflow and holds the probes to the application | The defect shipped because a `curl` flag was wrong and nothing in the repository compared the workflow's probes with the routes the application actually serves. The guard closes that gap generically. |

The application was **not** changed. `/api/v1/health/ready` remains a `GET`-only route; the bug was
in the probe, not in the route, and widening the route's method set would have masked a workflow
defect rather than corrected it.

## 3. The regression guard

`backend/tests/security/test_cp31_ci_deployment_probe_contract.py` parses `.github/workflows/ci.yml`,
extracts every `curl` probe the workflow makes against the DrillAI API, and enforces four invariants
against the real application:

| Test | Invariant |
|---|---|
| `test_cp31_ci_01_every_workflow_api_probe_is_served_by_the_application` | Every API path a probe targets exists in the application's route table, and the HTTP method the probe uses is one of the methods that path is registered for. A probe that is not servable fails here with the offending method, the served methods, and the probe's source line. |
| `test_cp31_ci_02_workflow_probe_assertions_hold_against_the_real_application` | Every `grep` a probe is piped into matches the response the application actually produces — in the body, or in the dumped headers when the probe uses `-D -`. This proves the gate's assertions hold before they are spent on a CI runner. |
| `test_cp31_ci_03_no_api_probe_uses_head_against_a_get_only_route` | No API probe uses `HEAD`. This states the concrete rule that was broken, so the workflow's intent is readable without archaeology. |
| `test_cp31_ci_04_the_readiness_route_the_gate_probes_answers_get_with_the_expected_payload` | `GET /api/v1/health/ready` returns `200`, carries `"status":"ok"`, and carries the two security headers the gate greps for. |

The parser expands curl's clustered short options, so `-fsSI` is recognised as `-f -s -S -I` rather
than as an unknown token. This matters: the first version of the guard missed the very defect it was
written for, because it matched only a standalone `-I`. The defect was only caught after the parser
was corrected, which is recorded here rather than quietly fixed.

The guard is generic — it reads whatever probes the workflow contains — so a future probe that
regresses in the same way is caught without editing the test.

### Guard verified against the uncorrected workflow

The corrected workflow was temporarily reverted to the original `curl -fsSI` form and the guard was
re-run. It failed with the intended message:

```
HEAD /api/v1/health/ready: the route /api/v1/health/ready is registered for ['GET'] only, so this
probe is answered with 405 Method Not Allowed and `curl -f` fails the job with exit code 22
(probe source: curl -fsSI http://127.0.0.1:8080/api/v1/health/ready | grep -iq 'x-content-type-options: nosniff')
```

Both `test_cp31_ci_01` and `test_cp31_ci_03` failed. The workflow was then restored, and all four
tests pass against it.

## 4. Documentation correction found while re-running the gates

The README stated the backend suite reported "**1,097 passed, 7 skipped**" without
`DRILLAI_TEST_POSTGRES=1`. Measured against the committed CP26 tree (`c6453c6`, with this
checkpoint's changes stashed), the suite reports **1,096 passed, 8 skipped** — the skip count was
wrong, and the totals did not reconcile (1,097 + 7 = 1,104 against a real total of 1,104 with a
different split). The README now states the measured values: **1,108 passed, 8 skipped** without
PostgreSQL and **1,108 passed** with it, in **87 files**. The CP16 documentation-consistency guard
enforces the file count and was updated to the current truth.

## 5. Local verification evidence

All commands ran from the paths shown, on the corrected source. No suite was filtered for a
complete run.

| Gate | Command | Result |
|---|---|---|
| Backend full suite | `DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest` | **1,108 passed**, 0 skipped, one non-failing `XDG_RUNTIME_DIR` warning from `platformdirs`, in 238.15 s. |
| Backend suite without PostgreSQL | `.venv/bin/python -m pytest` | **1,100 passed, 8 skipped**. |
| PostgreSQL marker coverage | `DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest -m postgres` | **8 passed, 1,100 deselected**. |
| Backend lint | `.venv/bin/python -m ruff check .` | **Passed** (`All checks passed!`). |
| CP31 guard | `.venv/bin/python -m pytest tests/security/test_cp31_ci_deployment_probe_contract.py` | **4 passed**. |
| Guard against the uncorrected workflow | same, with the workflow temporarily reverted | **2 failed** (the two method/HEAD invariants), by design. |
| Fresh SQLite migration and drift | `alembic upgrade head` then `alembic check` | **Passed**; `No new upgrade operations detected.` No schema migration was required. |
| Frontend unit/component | `npm test` | **318 passed in 30 files**. |
| Frontend static gates | `npm run typecheck` and `npm run lint` | **Passed**. |
| Multi-process stack | `scripts/verify_stack.py` | **Passed**: `STACK_VERIFICATION_OK`, 16 multi-process scenarios and a 32-cycle bounded soak. |
| Offline protocol harness | `scripts/verify_external_vendor_interop.py --mode harness` | **Passed** for the in-repo WITSML 1.4.1.1 SOAP and ETP 1.2 binary harnesses; `network_calls_made: 0`; `external_vendor_verified: false`. |
| Supply chain and secrets | `scripts/verify_supply_chain_and_secrets.py` | **Passed**: `SUPPLY_CHAIN_AND_SECRET_SCAN_OK`, 8 pinned actions, **444 tracked files** scanned, `pip-audit` pass, production `npm audit` pass. |
| Local gate-probe rehearsal | The four Compose-gate assertions replayed against a live server from this repository | **All four passed**, exit 0. The original `curl -fsSI` form failed with exit 22. |
| Docker Compose runtime smoke | `docker compose up -d --build` | **Not run**: this host has no Docker CLI/daemon. The local rehearsal exercises the gate's HTTP assertions against the real application, but it is **not** a container-runtime test and does not substitute for one. |
| Native Windows certification | GitHub Actions `windows-certification` | **Not run locally** (Linux host). Run `118600471702` passed on the CP26 SHA; a fresh Windows job must certify the final pushed SHA. |

## 6. Certification status and remaining limits

The Linux gate that failed on `c6453c6` is corrected in source and guarded by a regression test
that demonstrably fails on the uncorrected workflow. What that evidence does **not** establish:

- **Docker Compose runtime behaviour is still unverified locally.** The gate also exercises
  PostgreSQL, the Alembic migrate service, the dedicated connector worker and the Nginx reverse
  proxy; none of that runs here. The four HTTP assertions were rehearsed against the real
  application, which is a genuine check of the probes but not of the container topology.
- **Exact-SHA GitHub-hosted certification is pending** for the commit that carries this checkpoint.
  The CP26 run is evidence about `c6453c6`, not about the corrected source.
- Full archived CI logs remain unobtainable from this sandbox; step-level metadata plus local
  reproduction is the available evidence, and is what is cited above.

Unchanged external and owner-dependent limits from CP25/CP26 still apply and are **not** closed by
this checkpoint:

- Live commercial WITSML 1.4.1.1 and ETP v1.2 interoperability remain `external_vendor_verified:
  false`; `--mode external` reported `BLOCKED_BY_EXTERNAL_EVIDENCE` with `network_calls_made: 0`.
- Third-party notification delivery to an operator-owned staging endpoint remains unverified.
- The PostgreSQL logical backup/restore drill does not prove physical-host loss recovery, WAL/PITR,
  replicated failover, or cloud RPO/RTO.
- Engineering outputs remain preliminary decision-support artifacts, not field-certified results,
  without the required domain-owner validation and commissioning evidence.
- The owner-selected licensing decision remains unresolved; package metadata remains proprietary.

These are recorded blockers, not passed checks.
