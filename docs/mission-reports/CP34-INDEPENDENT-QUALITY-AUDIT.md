# CP34–CP42 — Independent Quality Audit and Final Release Assurance

**Audit date:** 2026-10-26 | **Repository:** `asgareyvazi/DrillAI` | **Active branch:** `arena/9d8f5568-drillai`

**Verdict:** **PARTIAL** — local software, security-regression, type, and real-browser gates pass; this is not field certification or authorization for live operations.

**Publication note:** the exact final GitHub Actions run is minted after this report is committed. Its run ID, job IDs, tested SHA, and Linux/Windows result are stated in the mission closeout response rather than embedded here, since adding the ID as a follow-up commit would change the SHA being certified.

This report is the current CP34–CP42 review, not a repetition of prior milestone summaries. Earlier CP25–CP33 claims were treated as hypotheses until their code, tests, runtime behavior, and available CI evidence were checked. A green CI run is not equated with branch protection; deterministic formula tests are not field validation; local vendor harnesses are not commercial interoperability; and a multi-process local stack is not Docker Compose.

## A. Verdict and release decision

**Verdict: PARTIAL.** The repository is a software release candidate with a passing local Linux verification set, including the full PostgreSQL backend suite and all three real-browser E2E deployments. The CP34 review also found and closed security and transport defects, and the final backend and E2E runs include their regression tests.

The verdict is not `PASS` for production/field readiness. These gates remain outside the evidence obtained here:

- Commercial WITSML/ETP interoperability and third-party notification delivery: **blocked**; no vendor endpoint or credentials were configured, and the external probe made zero network calls.
- `well_control.kill_sheet` and `tubulars.api_5c3`: **need field validation**. The other engine statuses are `formula_derived`; none is represented as an operational certificate.
- Physical-host-loss / cloud point-in-time recovery: logical backup/restore coverage does not establish physical or cloud PITR.
- Docker Compose on this sandbox: **not run**; the Docker CLI is not installed. Linux CI has its own Compose deployment gate.
- Enforced required-check policy: not independently demonstrated; current branch metadata says `protected: false`, while the detailed protection endpoint denied access with HTTP 403.

No setting in GitHub was changed. No change was made to `main`.

## B. Scope, provenance, and preservation

### Scope reviewed

The audit revisited the CP25–CP33 implementation and evidence, then exercised CP34–CP42 scope across:

- model identity, database constraints, telemetry query budgets, synthetic commissioning identity, run-event sequencing, and cross-tenant/parent scoping;
- the browser error oracle, request/response collection across the Playwright context, cancellation evidence, exact per-scenario allowances, mutation tests, and CI enforcement of the reviewed E2E inventory;
- every TypeScript project’s `noUncheckedIndexedAccess` setting;
- authentication, trusted proxy handling, tenant/user/token boundaries, secret references and encryption, SSRF/egress, workflow action levels, notification delivery, and data integrity;
- all 14 engineering engines, the 122 unit definitions / 33 dimensions, and the user-facing meaning of feasibility labels;
- Linux/Windows workflow structure, CI run evidence, branch protection/ruleset access, and repository publication state.

### Initial and branch state

| Item | Observed value |
|---|---|
| Initial local `HEAD` | `bfea530602a23bcc08efca85a136240f61c44a0d` |
| Fetched `origin/arena/9d8f5568-drillai` at mission start | `7baa4e95be1cf39d2496a10cf5283a78001ea82d` |
| `origin/main` at mission start | `bfa066b28e0071880cb9191a4f1d47fdaa143e04` |
| Branch status | `active` |
| Initial index | no staged changes |
| Initial worktree | 52 modified tracked paths and 13 untracked paths relative to the shallow local baseline |

The initial 65 dirty paths were compared byte-for-byte with the corresponding published branch blobs before advancing the branch pointer: **all 65 matched `origin/arena/9d8f5568-drillai` exactly**. The apparent dirt was the published CP25–CP33 tree populated in a shallow checkout, not unique local work. A non-destructive `git reset --mixed origin/arena/9d8f5568-drillai` aligned the active branch/index with that fetched tip and preserved the worktree; no hard reset, clean, force-push, history rewrite, or branch switch was used. New work remained on `arena/9d8f5568-drillai`.

The latest prior CP33 Actions run available at the start was run `40169700046` on exact SHA `7baa4e95be1cf39d2496a10cf5283a78001ea82d`; Linux job `121097864416` and Windows job `121097864370` succeeded. That is prior-tip evidence only, not evidence for the final CP34 SHA.

## C. Independent CP25–CP33 audit

| Checkpoint area | Independent conclusion | Evidence / limit |
|---|---|---|
| CP25 persistence and telemetry baseline | The model-ID and telemetry query-budget regressions remain covered; the final full backend run exercises the affected API, persistence, and telemetry tests. No current mismatch was found in that audited path. | `backend/tests/api/`, `backend/tests/db/`, `backend/tests/telemetry/`; current full suite result is in §I. |
| CP26 identity and tenant boundaries | The test contracts check that caller-supplied `X-Forwarded-For` cannot select login rate-limit/audit IPs, only the configured proxy is trusted, empty-scope legacy tokens fail closed, tenant admins cannot reset/deactivate a shared user or grant global roles, and a scoped token cannot delegate platform authority. | `backend/tests/security/test_cp26_auth_proxy_and_tenant_boundaries.py` (4 tests); all pass in the full suite. This is regression evidence, not a penetration test of every deployment network. |
| CP31 CI deployment probes | Linux workflow probes use real GET routes and assert readiness/security headers; the workflow contract test guards probe/route consistency. The Compose runtime remains a Linux CI responsibility, not a local sandbox result. | `backend/tests/security/test_cp31_ci_deployment_probe_contract.py`; local Docker CLI absent. |
| CP32 platform-independent failure verdict | E2E acceptance observes product console errors and HTTP/network outcomes rather than relying on Chromium’s platform-specific console wording. E2E is strict-typechecked and CI rejects a missing project, test skip, failure, or flake. | `test_cp32_e2e_console_error_contract.py` (66 collected tests), `frontend/tsconfig.e2e.json`, `.github/workflows/ci.yml`. |
| CP33 response-oracle coverage | The reviewed inventory is 121 tests / 17 specs; the oracle records failing responses and request failures and forces each deliberate failure through a narrow allowance. The final Linux E2E run executed all 121. | `test_cp33_e2e_response_oracle_coverage.py` (58 collected tests), `frontend/e2e/fixtures.ts`, `frontend/e2e/failure-oracle.ts`. |

The independent review did not treat previous report language as proof. In particular, CP19’s historical “PASS” ledger was narrowed in `CP19-ENGINEERING-VALIDATION.md`: hand-calculated vectors establish deterministic software regression behavior, not authoritative-edition conformance, domain-owner sign-off, field performance, or certification.

## D. E2E failure oracle, runtime behavior, and mutations

The browser-context fixture observes all pages in the Playwright context, including pages created after the fixture starts. It records every HTTP response with status ≥400 and every `requestfailed` event. The runtime matcher accepts a failure only when one allowance matches exactly and within its positive `maxMatches` limit.

An allowance must define:

- one HTTP method;
- a canonical `/api/v1/` path (literal segments or one-segment `:parameter`), no wildcard/prefix matching;
- the exact query key/value multiset;
- one explicit HTTP status or one exact Chromium `net::ERR_*` code;
- a meaningful reason and positive bounded match count.

Overlapping allowances fail as ambiguous; each observation consumes at most one allowance; excess observations remain failures. `ERR_ABORTED` alone is not benign. A caller cancellation is accepted only with the unique request ID and a matching `AbortError` from that same fetch. Navigation supersession requires the later navigation to start after the original request, start before its failure, and commit in the same frame. A deliberate timeout has its own exact request allowance and `TimeoutError` evidence. There is no blanket `ERR_ABORTED`, wildcard, substring, or “ignore all failed requests” exception.

Mutation / contract evidence:

- `backend/tests/security/test_cp32_e2e_console_error_contract.py`: 66 tests collected;
- `backend/tests/security/test_cp33_e2e_response_oracle_coverage.py`: 58 tests collected;
- `frontend/src/lib/failureOracle.test.ts`: 11 Vitest tests passed, covering field mismatches, broad/ambiguous targets, match counts, and cancellation evidence;
- `npm run e2e:verify-shape`: **121 tests in 17 files**, `chromium=114`, `chromium-faults=4`, `chromium-auth=3`.

The full real-stack E2E run passed **121/121** after the CP34 WebSocket fix. Expected server-side `RuntimeError` log entries came only from the deliberately injected fault stack at `/api/v1/wells`; they are the controlled 500 scenario, not evidence that a broad oracle allowance hid an unexpected error. The authentication-project rerun passed **3/3** and emitted no WebSocket disconnect traceback after the fix.

## E. `noUncheckedIndexedAccess` exception measurement

Before CP34, the frontend application project already enabled the rule. There were exactly **two project-level exceptions**:

1. `frontend/tsconfig.e2e.json` explicitly set `noUncheckedIndexedAccess: false`;
2. `frontend/tsconfig.node.json` omitted the option, so it defaulted to false.

Both exceptions are closed. The app, E2E, and Node/Vite projects now explicitly set `noUncheckedIndexedAccess: true`; the E2E explanatory comment was updated to match. `npm run typecheck` passes across all three referenced projects, including E2E specs and Vite config. **Remaining project-level exceptions: 0.**

## F. Security, tenant, workflow, and data-integrity findings

### F1. Environment SecretRef locator scope — fixed

**Finding:** a legacy or manually inserted `SecretRef(backend="env")` locator could otherwise name a process variable outside the dedicated secret namespace. The API’s creation validation alone is not sufficient protection against old/directly inserted rows.

**Correction:** connector and notification resolution now require a full `DRILLAI_SECRET_[A-Z0-9_]{2,120}` environment-name match at resolution time, not just at API creation. Invalid old rows fail closed, and errors do not return the backing value. Direct notification-secret lookup no longer falls back to an arbitrary environment variable name. Duplicate active/previous master-key versions are rejected rather than silently shadowed.

**Regression evidence:** `backend/tests/security/test_cp22_secrets_and_crypto.py` checks broad variable rejection, valid scoped refs, encrypted secret handling, tenant/key AAD binding, rotation, and duplicate key versions. The complete suite passes.

### F2. Plaintext credential headers in workflow configuration — fixed

**Finding:** a workflow could persist a literal credential-bearing HTTP header in graph JSON; validation diagnostics must not echo the header value.

**Correction:** workflow HTTP-fetch and notify config validation reject credential/auth/token/password/signature header names; Pydantic input hiding is enabled on these config models. Webhook dispatch also rejects credential-bearing custom headers if a row bypasses graph validation. Secret material is supplied through the supported managed-secret path rather than inline header values.

**Regression evidence:** `test_workflow_graph_rejects_credential_headers_without_echoing_the_secret` verifies the canary value is absent from the validation message; `test_workflow_http_egress.py` covers delivery-time rejection and egress behavior.

### F3. Workflow/webhook SSRF and proxy bypass — fixed

**Finding:** policy checks must constrain the connection actually made, not only validate a URL before a second DNS resolution or allow a proxy/redirect to select a new destination.

**Correction:** workflow HTTP and webhook delivery synchronize the authoritative egress policy, resolve/pin the approved destination, set the intended `Host` and HTTPS SNI, disable environment proxy routing (`trust_env=False`), and refuse redirects. Production scheme policy remains HTTPS-only for the workflow HTTP profile. DNS timeout and transient policy-store failure remain fail-closed/retry-classified.

**Regression evidence:** `backend/tests/workflow/test_workflow_http_egress.py`, existing connector SSRF/DNS/transport tests, and the 16-scenario local multi-process stack all pass. This does not certify third-party endpoint behavior.

### F4. Workflow action-level downgrade / L5 forgery — fixed

**Finding:** a serialized per-node action level must not lower the registered node type’s minimum or turn an ordinary node into L5 automation.

**Correction:** graph validation emits `action_level_below_node_minimum` and `unsupported_action_level_override`; runtime independently resolves and enforces the registered minimum and only permits L5 where the node type is registered for L5. Approval records use the resolved effective level.

**Regression evidence:** graph/runtime tests reject L0 override of the L4 `integration.http_fetch` node and reject a forged L5 report node; the full backend suite passes.

### F5. Run-event WebSocket disconnect mislabeled as authentication failure — fixed

**Finding:** the authentication prelude caught every exception, including `WebSocketDisconnect` while sending `stream_opened`, then attempted a second close with 4401. In the full E2E logs this surfaced as an ASGI traceback (`Cannot call send once a close message has been sent`) on a normal transport cancellation. This was a real defect found during runtime review, not dismissed as a flake.

**Correction:** the route returns on `WebSocketDisconnect`, maps only `AuthenticationRequired` to 4401 and `PermissionDenied` to 4403, and sends refusal frames through a state-aware close helper. Unexpected database/application exceptions are no longer mislabeled as failed authentication.

**Regression evidence:** `test_disconnect_while_sending_stream_opened_is_not_reclassified_as_auth_failure` was added; the run-event/auth backend subset passed **14/14**; the auth E2E project passed **3/3** without the prior disconnect traceback; then the full **121-test** E2E suite passed.

### F6. Tenant, authentication, and integrity controls

The CP26 regressions check server-derived peer IP, scoped tokens, role ceilings, cross-organization membership state, cookie-session and CSRF behavior, user/password-reset/token scope, and tenant-bound resource lookup. The CP21 session/auth tests and run-event socket tests pass in the full suite. Run-event persistence enforces per-run sequence uniqueness; the local `verify_stack.py` scenarios additionally exercised connector lease/fencing, egress revocation, replay idempotency, rollback on reconciliation failure, and policy recovery. These are automated software controls, not an external identity-provider or live-multi-tenant penetration certification.

## G. Engineering engines, units, and safety semantics

The runtime registry contains **14 engines**, **122 unit definitions**, and **33 dimensions**. The current per-engine registry statuses are:

- **`formula_derived` (12):** `drilling.mse`, `hydraulics.laminar`, `integrity.barrier_envelope`, `offsets.similarity`, `optimization.pareto`, `optimization.sweep`, `readiness.assessment`, `schematic.well_model`, `torque_drag.soft_string`, `trajectory.minimum_curvature`, `twin.reconciliation`, and `wellbore.capacity`.
- **`needs_field_validation` (2):** `tubulars.api_5c3` and `well_control.kill_sheet`.

CP19 tests exercise deterministic hand-calculated vectors, unit conversion round trips, boundary rejection, selected metamorphic invariants, finite JSON serialization, and stored run provenance. The backend suite passes these checks. They do **not** prove that each equation matches every applicable edition of a standard, calibrate a model to a well, establish manufacturer connection ratings, or certify site operating procedures.

`EngineResult.is_feasible` means only “this engine result contains no emitted error-severity violation of its declared model constraints.” The engineering UI labels this **“Within modeled constraints”** / **“Modeled constraint violation”** and displays an English and Persian disclaimer that it is not a safety certificate or authorization to operate. The optimization page separately says a Pareto rank is not operational approval. Inputs, assumptions, warnings, limitations, evidence, and the site’s procedures remain part of human review.

## H. CI, deployment gates, and repository policy

`.github/workflows/ci.yml` runs a Linux `certification` job and a native Windows `windows-certification` job on the active branch. The workflow checks out the commit under test, validates E2E inventory shape, and requires exactly 121 completed browser tests across all three projects. Linux runs backend tests with embedded PostgreSQL and the Docker Compose deployment smoke gate. Windows runs the SQLite backend with `-m "not postgres"` (the eight PostgreSQL cases are not run there), its multi-process stack/vendor harness, and the same full frontend/E2E inventory. The workflow is read-only and has no `continue-on-error` gate.

Current GitHub governance evidence:

- Branch API metadata for `arena/9d8f5568-drillai`: `protected: false`.
- Repository rulesets query: returned no rows.
- Branch protection detail endpoint: **403 `Resource not accessible by integration`**.

Therefore: no evidence that classic branch protection requires checks; detailed enforcement could not be independently queried. A passing Actions run is not a substitute for that setting. Repository owners control the policy; no branch protection or ruleset change was attempted.

The local sandbox has **no Docker CLI installed**. No Docker daemon or local Compose deployment result is claimed. `scripts/verify_stack.py` did run locally: `STACK_VERIFICATION_OK`, **16 multi-process scenarios plus a 32-cycle bounded soak**. That is a separate process-level harness, not a container-runtime test. The Linux CI Compose gate remains the evidence for Docker deployment behavior.

## I. Verification ledger

| Check | Final local result | Qualification |
|---|---|---|
| Backend, embedded PostgreSQL | `DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest -o addopts=""`: **1,248 passed**, 1 warning, 90 test files. | Warning: `XDG_RUNTIME_DIR` unset, `platformdirs` fell back to `/tmp/runtime-1001`. No skips. |
| Backend, PostgreSQL disabled | `DRILLAI_TEST_POSTGRES=0 .venv/bin/python -m pytest -o addopts=""`: **1,240 passed, 8 skipped**, 90 test files. | The eight PostgreSQL-marked tests are skipped; not called passed. |
| Frontend unit/components | `npm test`: **329 passed / 31 files**. | Non-blocking React Router / `act(...)` warnings remain; no test skipped. |
| Typecheck | `npm run typecheck`: pass. | All app, E2E, and Node/Vite projects; index-access rule enabled in each. |
| Lint | `npm run lint`: pass. | ESLint. |
| Production build | `npm run build`: pass. | Vite reports one 530 kB minified chunk size warning; build succeeds. |
| E2E inventory | `npm run e2e:verify-shape`: pass. | 121 / 17 files, project split 114/4/3. Shape verification is not execution. |
| Full Playwright E2E | `npm run e2e`: **121 passed**. | Real API, SQLite seed, Vite, browser; three deployments; 5.4 minutes on this Linux host. |
| Auth E2E after WebSocket fix | `npm run e2e -- --project=chromium-auth`: **3 passed**. | 36.2 seconds; no WebSocket disconnect traceback in captured output. |
| Backend lint | `.venv/bin/python -m ruff check .`: pass. | Full backend tree. |
| Fresh SQLite migrations | `alembic upgrade head`; `alembic check`: pass. | Temporary fresh DB; `No new upgrade operations detected`. An initial invocation from repository root used a config whose relative `script_location` expects `backend/`; corrected run from `backend/` passed. |
| Supply chain/secrets | `scripts/verify_supply_chain_and_secrets.py`: `SUPPLY_CHAIN_AND_SECRET_SCAN_OK`. | 8 pinned actions, 450 tracked files scanned, `pip_audit=pass`, production `npm_audit_high=pass`. |
| Local vendor harness | `--mode harness`: pass. | WITSML SOAP + ETP binary harness and negative protocol cases; `network_calls_made: 0`, `external_vendor_verified: false`. |
| External vendor mode | `--mode external` with endpoint variables explicitly unset: exit 0, blocked. | `BLOCKED_BY_EXTERNAL_EVIDENCE`, `network_calls_made: 0`, no vendor verification. |
| Multi-process stack | `scripts/verify_stack.py`: `STACK_VERIFICATION_OK`. | All 16 scenarios and 32-cycle bounded soak. |

A preliminary no-PostgreSQL run failed only the CP16 documentation-consistency guard because the README claimed 91 backend test files while the actual census was 90. README and testing documentation were corrected to the measured count; the guard passed, then the complete no-PostgreSQL suite passed at **1,240 passed, 8 skipped**. Failed command attempts caused by the excluded `.venv` being absent after a turn boundary started no test run; the environment was re-created using the repository metadata and `npm ci`. No result from a failed setup/path command is counted as passing evidence.

## J. Remaining blockers and exact-SHA handoff

### External / owner-controlled exit criteria

| Blocker | What is required before closing it |
|---|---|
| Commercial WITSML/ETP validation | Operator-provided staging/rig endpoint and credentials; run `scripts/verify_external_vendor_interop.py --mode external --require-external`; preserve a credential-redacted transcript/hash and record vendor profile evidence. |
| Live notification provider | Owner-approved staging webhook or SMTP relay credentials; demonstrate an actual provider receipt, without assuming local adapters certify SaaS delivery. |
| Field validation / operational approval | Domain owner and field data for `well_control.kill_sheet` and `tubulars.api_5c3`; verify applicable standards, connection tables, procedures, and input-quality limits. Do not authorize operations from software test vectors alone. |
| Physical/PITR disaster recovery | Production-like infrastructure with WAL archiving/standby; execute timed restore and document RPO/RTO. Logical backup/restore is not a substitute. |
| Branch protection | Repository owner should independently set/confirm required Linux and Windows checks if policy requires them. Current API evidence does not establish enforcement. |

### Publication proof

The final release candidate must be committed and pushed only to `arena/9d8f5568-drillai`. After push, verify the exact remote ref with `git ls-remote`, then inspect the Actions run whose `head_sha` equals the final local `HEAD` and require both Linux `certification` and Windows `windows-certification` jobs to finish successfully. The exact commit SHA, workflow run ID, job IDs, and result are stated in the mission closeout response. A prior green run, the local tests, `protected:false`, and a future/pending run are not substitutes for that final exact-SHA check.
