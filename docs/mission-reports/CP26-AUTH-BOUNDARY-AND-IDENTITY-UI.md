# CP26 — Authentication Boundaries, Scoped Tokens & Identity Settings

**Audit date:** 2026-10-20 (UTC)
**Repository:** `asgareyvazi/DrillAI`
**Session branch:** `arena/9d8f5568-drillai`
**Baseline:** CP25 source `9c5db2f45d6c489aaf87392892b82cd8438cb684`; prior evidence tip `c01fa06aebd1badc4860c462e1483d680e67c6ea`
**Status:** local Linux gates passed; GitHub-hosted Linux/Windows and Docker Compose runtime gates are pending publication of this checkpoint.

## 1. Findings and changes

| Finding | Evidence / impact | Remediation and regression coverage |
|---|---|---|
| Caller-controlled `X-Forwarded-For` was used directly for authentication rate-limit buckets and audit IPs. | A direct client could select its own rate-limit key and falsify the IP recorded in security audit rows. A proxy peer is trustworthy only when the ASGI server has an explicit trusted-proxy list. | Auth now records the resolved ASGI peer (`request.client.host`) rather than parsing forwarding headers. Compose pins Nginx at `172.31.240.10` on `172.31.240.0/28`; Uvicorn trusts only that peer by default. Nginx retains the forwarding chain for Uvicorn's proxy parser but omits raw forwarded-for from access logs. CP26 tests assert rate-limit/audit source and static proxy settings. **Compose was not launched locally.** |
| Tenant-admin user operations could mutate a globally active account or grant high-authority roles beyond the tenant boundary. | `User.is_active` is global while memberships are organization-scoped; resetting/deactivating a user shared across organizations has cross-tenant impact. `well_manager` carries a global wildcard permission pattern. | Password reset and deactivation reject cross-organization/shared-account targets and self-deactivation. Granting `admin` or `well_manager` requires an explicit platform-admin role, not merely wildcard permission matching. CP26 tests verify denied resets, deactivation and privilege escalation while protecting another organization's token/session. Existing CP21 org-admin engineer provisioning remains supported. |
| Empty API-token scopes could be treated as unrestricted by the permission matcher. | Empty-scope credentials—including legacy rows—did not establish an explicit authorization boundary. | Empty scopes are rejected at authentication and token creation; token scopes remain intersected with the owner's current membership permissions. Requested wildcard scopes cannot exceed the caller's and target user's grants. Existing integration fixtures now declare explicit scopes; negative regression coverage preserves fail-closed behavior. No migration or historical token rewrite was added. |
| Authenticated users lacked a user-facing identity/security management surface. | Existing auth endpoints did not provide a cohesive user-and-token workflow in the frontend. | Added `/security` with password change, organization user provisioning/update/deactivation, role-aware controls, and scoped API-token create/list/revoke. A newly issued token is displayed once and discarded from page state when dismissed; it is not written to browser storage. English/Persian translations and page tests cover provisioning and one-time token handling. Playwright covers the seeded tenant-admin flow end-to-end. |

## 2. Local verification evidence

| Gate | Result |
|---|---|
| Backend full suite | `DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest` — **1,104 passed**, including the PostgreSQL-marked tests; one non-failing `XDG_RUNTIME_DIR` warning from `platformdirs`. |
| Backend lint | `.venv/bin/python -m ruff check .` — passed. |
| Focused regression/integration set | CP21/CP26 auth tests plus affected API, tenant and event tests — **65 passed**. |
| Fresh SQLite migration | `alembic upgrade head` then `alembic check` — passed; `No new upgrade operations detected`. No schema migration was required. |
| Frontend unit/component | `npm test` — **318 passed in 30 files**. |
| Frontend static gates/build | `npm run typecheck`, `npm run lint`, and `npm run build` — passed. Vite emitted its existing non-failing >500 kB chunk advisory. |
| Browser E2E | `npm run e2e` — **121 passed** across all three projects (`chromium`, `chromium-faults`, `chromium-auth`) and 17 spec files. The auth project includes admin user creation, one-time scoped-token reveal, and token revocation. |
| Multi-process stack | `scripts/verify_stack.py` — `STACK_VERIFICATION_OK`; 16 scenarios and 32-cycle soak passed. |
| Offline protocol harness | `scripts/verify_external_vendor_interop.py --mode harness` — WITSML 1.4.1.1 SOAP and ETP 1.2 binary harnesses passed; `network_calls_made: 0`. |
| Supply chain/secrets | `scripts/verify_supply_chain_and_secrets.py` — passed after staging the complete checkpoint; scanned **443 tracked files**, with 8 pinned Actions, `pip-audit` passing, and no production npm high-severity findings. |
| Static Compose/workflow parse | YAML parse plus explicit assertions for static proxy IPAM, Uvicorn trust, the `arena/**` push trigger, and unfiltered Windows E2E — passed. This is not the Docker Compose smoke gate. |

## 3. CI trigger and platform certification

The workflow previously pushed only on `arena/01a0dca0-drillai`, so CP25 publication to the active Arena session branch had no automatic Actions run. The workflow now matches `arena/**`; the Windows Playwright command is unfiltered so it runs the same complete spec suite rather than a file subset. This change is intended to provide publication-bound Linux and Windows evidence, not to claim it in advance.

The sandbox is Linux and has no Docker CLI/daemon or native Windows host. Therefore, until GitHub Actions completes against the exact pushed commit:

- Native Windows certification: **pending**.
- Docker Compose config/build/runtime smoke gate: **pending**.
- Exact-SHA GitHub Actions certification: **pending**.

## 4. Remaining external and owner-dependent limits

- Live commercial WITSML/ETP interoperability remains `external_vendor_verified: false`; `--mode external` reported `BLOCKED_BY_EXTERNAL_EVIDENCE` and `network_calls_made: 0` because no vendor endpoint or credentials were configured. The local protocol harness is not vendor certification.
- Third-party notification delivery (email/SMS/webhook SaaS) remains unverified against an operator-owned staging endpoint.
- The PostgreSQL logical backup/restore test does not prove physical-host loss recovery, WAL/PITR, replicated failover, or cloud RPO/RTO.
- Engineering outputs remain preliminary decision-support artifacts and are not field-certified without the required domain-owner validation and commissioning evidence.
- The owner-selected licensing decision remains unresolved; package metadata remains proprietary.

These are recorded blockers, not passed checks.
