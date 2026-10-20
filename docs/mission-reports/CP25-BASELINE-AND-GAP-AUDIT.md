# CP25 — Baseline and Initial Gap Audit

**Audit date:** 2026-10-20 (UTC)
**Repository:** `asgareyvazi/DrillAI`
**Session working branch:** `arena/9d8f5568-drillai`
**Initial local HEAD:** `bfea530602a23bcc08efca85a136240f61c44a0d`
**Initial remote `arena/01a0dca0-drillai`:** `bfea530602a23bcc08efca85a136240f61c44a0d`
**Initial `main`:** `bfa066b28e0071880cb9191a4f1d47fdaa143e04`

> Branch reconciliation: the supplied mission names `arena/01a0dca0-drillai`, and that remote branch exists at the stated baseline. The active Arena session is attached to `arena/9d8f5568-drillai`; that is the only branch this session may modify/publish. It did not exist on `origin` at audit start. Work stays on the active session branch; `main` and the source branch are not modified. CI's trigger list must be reconciled with this session branch before final certification.

## 1. Starting-state evidence

Commands executed before tracked changes:

```text
pwd                                      /home/user/DrillAI
git rev-parse --show-toplevel            /home/user/DrillAI
git status --short --branch              ## arena/9d8f5568-drillai
git branch --show-current                arena/9d8f5568-drillai
git rev-parse HEAD                       bfea530602a23bcc08efca85a136240f61c44a0d
git diff --stat                          (empty)
git diff --cached --stat                 (empty)
git ls-files --others --exclude-standard (empty)
```

The checkout was shallow/grafted at the known baseline. `git fetch origin` and an explicit fetch of `origin/arena/01a0dca0-drillai` confirmed the named source branch still pointed to `bfea530602a23bcc08efca85a136240f61c44a0d`; `origin/main` remained at `bfa066b28e0071880cb9191a4f1d47fdaa143e04`. There were no user modifications or untracked source files. A local, ignored `backend/.venv` was created only to install the repository's declared dev dependencies and run tests; it is not tracked.

The repository contains 437 tracked files at baseline (261 backend files, 137 frontend files, plus root/docs/scripts/workflow files). The baseline backend test tree collects approximately 1,100 tests across API, persistence, security, telemetry, engineering, workflow, ingestion, RAG, and operations suites; the exact collected and executed counts are to be reported from the post-fix run, not inferred from older reports.

## 2. Latest certification evidence

GitHub metadata for [run 39217548722](https://github.com/asgareyvazi/DrillAI/actions/runs/39217548722) identifies `head_sha=bfea530602a23bcc08efca85a136240f61c44a0d`, branch `arena/01a0dca0-drillai`, and conclusion `failure` (2026-10-19 UTC). Both the Linux and native Windows jobs failed their backend test step; subsequent Linux lint, migration, stack, Docker, and E2E steps and Windows stack/vendor/E2E steps were skipped. Frontend typecheck, lint, unit tests, build, supply-chain verification, and Windows lint/migration steps completed successfully in that run. These are historical results for the baseline SHA, not certification of later work.

The GitHub Actions metadata and job steps were available using `gh run view --json ...`. Downloading the full log archive with `gh run view --log` and `--log-failed` failed with `EOF` from `results-receiver.actions.githubusercontent.com`; that host is outside this sandbox's permitted outbound-host list. Therefore the full archived logs could not be independently fetched here. The two reported failures were reproduced locally, with the complete SQL statements printed by pytest:

1. `tests/db/test_persistence.py::test_every_model_declares_a_short_id_prefix` fails because `AlertRule` reuses `"arl"` from `AuthRateLimitBucket`.
2. `tests/telemetry/test_queries_api.py::test_a_window_read_issues_no_more_statements_than_it_needs` observes seven `SELECT`s against a six-statement ceiling. Four are current bearer authentication lookups (API token, user, organization, membership); the other three are the channel, window count, and point page. This is measurable SQL overhead, not a telemetry N+1 and not a reason to disable or cache authorization.

The exact reproduction command, from `backend/`, was:

```bash
.venv/bin/python -m pytest \
  tests/db/test_persistence.py::test_every_model_declares_a_short_id_prefix \
  tests/telemetry/test_queries_api.py::test_a_window_read_issues_no_more_statements_than_it_needs -vv
```

Observed result: **2 failed**. The SQL output shows the source of all seven reads, including the unjoined API-token/user/organization lookups.

## 3. Evidence-based gap ledger (initial snapshot)

| ID | Severity | Owner component / exact source | Finding and evidence | Planned correction / regression evidence | Status at audit start |
|---|---|---|---|---|---|
| CP25-DB-01 | High (release gate) | `backend/src/drillai/db/models/identity.py::AuthRateLimitBucket.id_prefix`; `backend/src/drillai/db/models/operations.py::AlertRule.id_prefix`; `backend/src/drillai/api/routers/auth.py::_record_rate_limit_failure`; `backend/tests/db/test_persistence.py::test_every_model_declares_a_short_id_prefix` | Both models declared `"arl"`; the global prefix test failed. The auth rate-limit insertion path also explicitly minted `new_id("arl")`. Alert-rule IDs are the earlier persisted family; auth rate limits were introduced later (`d4e7a9c12b83`). | Use the distinct `rlb` prefix for both the model default and auth limiter's explicit creation path; leave `AlertRule` at `arl`; test global uniqueness, ORM-generated IDs, and actual rate-limit rows. No migration or historical ID rewrite. | **Closed locally**; full backend suite passed with the focused auth-rate-limit ID assertion. See `CP25-CI-REPAIR-AND-CERTIFICATION.md`; exact remote CI remains pending. |
| CP25-API-01 | High (release gate) | `backend/src/drillai/api/deps.py::_resolve_token`; `backend/src/drillai/telemetry/service.py::TelemetryService.window`; `backend/tests/telemetry/test_queries_api.py::test_a_window_read_issues_no_more_statements_than_it_needs` | One authenticated window request performs seven SELECTs: token + user + organization + membership + channel + count + page. The measured ceiling is six. No query cache is acceptable because revocation and active-state changes must be observed immediately. | Fetch token, owner, and organization in one joined query, retain the live membership lookup and all per-request validity/revocation checks, then assert a bounded query budget while retaining cross-tenant 404 regression coverage. | Open; reproduced. |
| CP31-CI-01 | High (certification gap) | `.github/workflows/ci.yml::windows-certification` Playwright step | Windows runs only `e2e/app.spec.ts`, `connectors-platform.spec.ts`, `error-faults.spec.ts`, and `error-auth.spec.ts`; Linux runs unfiltered `npm run e2e`. The three-project marker check does not prove full spec coverage. | Remove the Windows file filters; add a collection/summary check that proves all expected specs/projects ran; update old test-count claims from fresh run output. | Open; source confirmed. |
| CP31-CI-02 | High (publication/certification gap) | `.github/workflows/ci.yml::on.push`; active session branch | Workflow push trigger names only `arena/01a0dca0-drillai`; active session branch is `arena/9d8f5568-drillai`. A push to the session branch would not trigger this workflow unless triggers are reconciled. | Add the active branch to the push trigger (retain PR-to-main coverage), publish only the current Arena branch, and verify the completed run's `head_sha` equals the final remote ref. | Open; source confirmed. |
| CP31-DOC-01 | Medium | `README.md` verification table; `docs/mission-reports/PRODUCTION-READINESS-MATRIX.md`; `docs/mission-reports/REMAINING-GAPS-ROADMAP.md` | Existing documentation has multiple different backend/E2E counts, and the matrix/README describe a full Windows suite although the workflow filters files. CP24's older completion table predates the latest red CP24 run. | Reconcile claims against exact post-fix CI metadata and logs; classify results by OS, PostgreSQL, Docker, E2E, and external evidence; retain unsupported production/field certification claims as blocked. | Open; contradiction confirmed. |
| EXT-01 | Blocked by external evidence | `scripts/verify_external_vendor_interop.py`; `backend/src/drillai/telemetry/protocols/` | Existing roadmap describes only local WITSML/ETP protocol harnesses; no live commercial vendor system is available to this audit. | Keep a commissioning procedure and `BLOCKED` status until a live staging/rig endpoint and credentials are supplied and a redacted successful transcript is archived. | Not certified externally. |
| EXT-02 | Blocked by external infrastructure | `scripts/verify_postgres_dr_drill.py`; production deployment docs | A logical PostgreSQL backup/restore drill does not establish physical-host loss recovery, WAL/PITR, replicated failover, or cloud DR. | Require owner-provided production-like PostgreSQL with continuous WAL archive/replica and demonstrate stated RPO/RTO by restoring to a target time. | Not verified here. |
| EXT-03 | Blocked by owner decision | `backend/pyproject.toml::project.license`; `SECURITY.md` | Package metadata says `Proprietary`, and no owner-selected open-source license file is present. | Preserve all-rights-reserved status unless the owner explicitly chooses and publishes a license. | Correctly unresolved. |

## 4. Repository areas inspected and audit method

The baseline inventory includes all tracked backend modules/tests, migrations, frontend source/tests/specs, scripts, Compose/Docker/Nginx files, workflow configuration, environment examples, README/security/setup documentation, and existing CP14–CP24 mission reports. For CP25, the affected model declarations, model registry, ULID prefix generator, telemetry route, dependency/authentication path, window query, real test fixture, statement counter, CI job summaries, and Linux/Windows workflow commands were traced directly. The remaining CP26–CP32 source audits are recorded in their own checkpoint reports as evidence is gathered; previous mission reports are treated as hypotheses, not as proof.

Environment limits observed: Python 3.11.2 and Node 22.22.3 are installed; this host has no `docker`, `psql`, or `pg_isready` executable. Embedded PostgreSQL via the declared `pgserver` dependency is available for supported PostgreSQL tests on Linux. Native Windows execution is only available in GitHub Actions, not this Linux sandbox. Full GitHub log-archive downloads from the Actions artifact host are blocked by the sandbox's outbound-host policy.

## 5. CP25 execution record

At the moment this baseline report was written, the two failures above were reproduced and the actual code remediation had not yet been applied. This report is a starting snapshot; the checkpoint closure and execution evidence are recorded below and in [`CP25-CI-REPAIR-AND-CERTIFICATION.md`](CP25-CI-REPAIR-AND-CERTIFICATION.md).

## 6. Post-baseline verification update

- **CP25-DB-01: locally closed.** `AlertRule` retains `arl`; `AuthRateLimitBucket` and its explicit auth limiter insert now use `rlb`. No schema migration or rewrite of existing IDs was introduced. Global uniqueness, ORM generation, and actual rate-limit persistence are tested.
- **CP25-API-01: locally closed.** Bearer token, user, and organization resolve in one SQL join; per-request token/revocation/account checks and the live membership lookup remain. The telemetry window test now enforces at most five SELECTs, verifies both credential joins, and verifies that membership is still queried.
- **Local evidence:** `DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest` → **1,100 passed, 0 skipped**; backend Ruff passed; fresh SQLite Alembic upgrade/check reported `No new upgrade operations detected`; frontend typecheck/lint passed; Vitest **315/315**; production build passed with a non-failing chunk-size advisory; Linux Playwright E2E **120/120** across all three configured projects and 17 spec files; stack verification passed 16 scenarios plus a 32-cycle soak; supply-chain/secret gate passed; local protocol harness passed without external network access.
- **Still open:** no fresh GitHub run yet certifies the corrected remote SHA; Windows and Docker runtime certification require their supported hosts. The current CI workflow trigger omits the active Arena session branch and its Windows E2E command filters files (including an untracked/nonexistent `e2e/app.spec.ts` path), recorded as CP31-CI-01/02. External vendor testing, real notification delivery, owner-selected licensing, and physical/PITR disaster recovery remain external/owner-dependent rather than passing this audit.
