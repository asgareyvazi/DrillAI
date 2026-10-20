# CP25 — Reproduced CI Failure Repair and Local Certification

**Repository:** `asgareyvazi/DrillAI`
**Session branch:** `arena/9d8f5568-drillai` (the active Arena branch; the mission's named `arena/01a0dca0-drillai` remains a separate remote branch at the baseline)
**Base SHA:** `bfea530602a23bcc08efca85a136240f61c44a0d`
**Code commit:** pending publication at the time of this report; exact SHA will be added after commit.
**Known baseline CI:** [run 39217548722](https://github.com/asgareyvazi/DrillAI/actions/runs/39217548722), failure on `bfea530602a23bcc08efca85a136240f61c44a0d`.

## 1. Findings and corrections

### CP25-A — Globally colliding ORM ID prefixes

**Reproduction:**

```bash
.venv/bin/python -m pytest \
  tests/db/test_persistence.py::test_every_model_declares_a_short_id_prefix -vv
```

At baseline, `AlertRule.id_prefix` and `AuthRateLimitBucket.id_prefix` both equaled `"arl"`. Migration history establishes that `AlertRule` is the older persisted ID family (created in `d5a71c93e2f8_telemetry_spine.py`) and the durable auth limiter arrived later in `d4e7a9c12b83_multi_user_sessions_and_auth_hardening.py`. The explicit creation path in `api/routers/auth.py::_record_rate_limit_failure` also hard-coded `new_id("arl")`, so changing only the model default would not have fixed all newly generated rate-limit IDs.

**Correction:**

- Kept `AlertRule` at `arl` for compatibility with existing persisted IDs; `backend/src/drillai/telemetry/alerts.py` continues to mint alert-rule IDs with `new_id("arl")`.
- Changed the newer `AuthRateLimitBucket` prefix to `rlb` in `backend/src/drillai/db/models/identity.py` and changed the explicit auth rate-limit creation path in `backend/src/drillai/api/routers/auth.py` to `new_id("rlb")`.
- Added an ORM flush regression in `backend/tests/db/test_persistence.py` and extended `test_cp21_05_multi_worker_db_rate_limiting_and_lockout` to assert the actual persisted rate-limit rows have valid `rlb_` IDs.
- No schema field, database constraint, migration, or historical row was rewritten; the change affects only IDs newly minted for auth rate-limit buckets.

### CP25-B — Authenticated telemetry window statement budget

**Reproduction:**

```bash
.venv/bin/python -m pytest \
  tests/telemetry/test_queries_api.py::test_a_window_read_issues_no_more_statements_than_it_needs -vv
```

The measured baseline was seven `SELECT`s: one each for `ApiToken`, `User`, `Organization`, live `Membership`, channel, range count, and point page. This was not an N+1 in telemetry. No authorization cache was introduced.

**Correction:** `backend/src/drillai/api/deps.py::_resolve_token` now selects the API-token row, owner `User`, and `Organization` through one joined query. Every request still checks token revocation, expiration, user activation, organization activation, token digest, and a fresh membership query. `TelemetryService.window` and its tenant-scoped channel lookup remain unchanged. `backend/tests/telemetry/test_queries_api.py::test_a_window_read_issues_no_more_statements_than_it_needs` now enforces a five-SELECT ceiling, requires exactly one credential query containing both joins, and requires exactly one live membership query. Existing foreign-channel tests continue to assert a tenant-scoped 404.

The measured query contract is five bounded reads: token/owner/organization join, membership, channel, count, and page.

## 2. Test and gate results on the corrected local source

All commands below ran from the paths shown; no suite was filtered for the complete backend run.

| Gate | Command / environment | Result |
|---|---|---|
| Initial two-test failure reproduction | `python -m pytest <two baseline node IDs> -vv` | **2 failed**, confirming both baseline defects. |
| Corrected focused tests | `.venv/bin/python -m pytest tests/db/test_persistence.py tests/telemetry/test_queries_api.py tests/security/test_cp21_auth_and_sessions.py` | **38 passed, 2 skipped**; the two PostgreSQL-marked tests are deselected by the default opt-in setting in this focused run. |
| Full backend suite | `DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest` | **1,100 passed, 0 skipped**, with 1 `XDG_RUNTIME_DIR` warning, in 223.47 seconds. Includes the supported embedded-PostgreSQL integration tests. |
| Backend lint | `.venv/bin/python -m ruff check .` | **Passed** (`All checks passed!`). |
| Fresh SQLite migration and drift | `DRILLAI_DATABASE_URL=sqlite+aiosqlite:////tmp/<fresh-db>.db .venv/bin/python -m alembic upgrade head && .venv/bin/python -m alembic check` | **Passed**; `No new upgrade operations detected.` |
| PostgreSQL marker coverage | `DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest -m postgres` | **8 passed, 1,092 deselected** on the supported embedded PostgreSQL runtime; the complete backend run above also passed all tests without deselection. |
| Frontend install | `npm ci --no-audit --no-fund` | **Passed**; 400 packages installed from the committed lockfile. |
| Frontend typecheck | `npm run typecheck` | **Passed**. |
| Frontend lint | `npm run lint` | **Passed**. |
| Frontend component/unit tests | `npm test` | **315 passed across 29 test files**, no skips. Existing React Router and `act(...)` warnings are non-failing test output. |
| Frontend production build | `npm run build` | **Passed**; Vite built the production bundle. Vite reports a non-failing >500 kB chunk advisory that remains an optimization item. |
| Full local Linux E2E | `npm run e2e` | **120 passed**: `chromium` 114 tests across 15 specs, `chromium-faults` 4 tests in `error-faults.spec.ts`, and `chromium-auth` 2 tests in `error-auth.spec.ts`; all 17 spec files were exercised across the three deployments. |
| Multi-process application stack | `backend/.venv/bin/python scripts/verify_stack.py` | **Passed**: `STACK_VERIFICATION_OK`, 16 multi-process scenarios and a 32-cycle bounded soak. |
| Supply-chain and secret gate | `backend/.venv/bin/python scripts/verify_supply_chain_and_secrets.py` | **Passed**: 8 pinned actions, 437 tracked files scanned, `pip-audit` pass, production `npm audit` pass. |
| Local protocol harness | `backend/.venv/bin/python scripts/verify_external_vendor_interop.py --mode harness` | **Passed** for the in-repo WITSML 1.4.1.1 SOAP, ETP 1.2 binary WebSocket, and ETP JSON test harness profiles; `network_calls_made=0`; `external_vendor_verified=false`. |
| Docker Compose smoke | `docker compose config` / deployment smoke | **Not run**: this host has no Docker CLI/daemon (`docker: command not found`). Existing in-repository deployment tests do not substitute for a Docker runtime smoke test. |
| Native Windows certification | GitHub Actions `windows-certification` | **Not run locally** (Linux host). Baseline Windows run 39217548722 failed before later gates. A fresh Windows job must certify the pushed final SHA. |

## 3. Certification status and remaining CI actions

The CP25 defects are fixed and locally regression-tested. The latest completed Actions run is still the historical failed baseline run, not the corrected source. Full archived logs for that run were not retrievable: `gh run view 39217548722 --log` and `--log-failed` returned EOF from GitHub's Actions log-archive host, which is outside this sandbox's allowed outbound hosts; its job metadata and the failures were independently verified/reproduced locally.

The local source results above do **not** certify Windows, Docker deployment, or the final remote SHA. Two CI defects discovered during the source audit remain open for CP31: `.github/workflows/ci.yml` only triggers `push` for `arena/01a0dca0-drillai` (not the active session branch `arena/9d8f5568-drillai`), and the Windows E2E command filters to four spec paths, including `e2e/app.spec.ts`, which is not among the 17 tracked specs. Both must be corrected before the final exact-SHA certification run. The branch trigger will not be misrepresented as having run until GitHub Actions reports a completed run for the pushed SHA.

## 4. Checkpoint closure

CP25-A and CP25-B: **implemented and locally verified**. No regression tests were removed or weakened; the telemetry budget was tightened from six to five and now checks that authorization remains live. CP25 publication/remote CI status is pending the code commit and push; remote verification and final CI status will be added after publication.
