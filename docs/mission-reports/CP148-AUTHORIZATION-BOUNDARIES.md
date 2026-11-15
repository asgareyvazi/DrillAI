# CP148 — Adversarial Authorization-Boundary Regressions

**Date:** 2026-10-20

**Status:** Targeted authenticated API boundary regressions pass against the real FastAPI application, HTTP/ASGI stack, SQLAlchemy session layer, and isolated file-backed SQLite database. This is repository regression evidence only; it is not an independent penetration test or security assessment.

**Evidence class:** Synthetic organizations/users/tokens/outbox rows created in a per-test database. Bearer tokens are generated inside the tests and only their hashes are persisted. No vendor, provider, customer, or production endpoint was contacted.

## Adversarial cases exercised

- A bearer token is scoped to tenant A while the same user has a separate membership in tenants A and B. The actual notification API list returns only tenant-A data even when the caller supplies an untrusted `X-Org-ID` hint naming B. Message bodies, recipients, and tenant-B identifiers do not appear in the response.
- A foreign tenant-B notification ID submitted to the manual-retry endpoint returns the non-disclosing `404`. The foreign row remains `dead_letter` with its attempt count unchanged and no corresponding retry audit record.
- The same authorized tenant-A operator can requeue its own eligible synthetic row. The API response and independent database readback show `queued`, attempts reset to zero, and recipient receipt still `unknown`; one audit record binds the action to tenant A and the same-tenant row.
- A separate read-only token can read the outbox but cannot use `X-Dev-Roles: admin` to widen its permissions for a state change. With auth enabled, a development-role header without a bearer credential receives `401`. The denied request leaves the row and retry-audit table unchanged.

## Verification observed

- New `tests/security/test_cp148_authorization_boundaries.py`: **2 passed**.
- New CP148 tests plus the existing CP26 real-auth boundary module: **6 passed**.
- Ruff and `compileall` passed for the changed Python files.
- Backend inventory: **1,773 tests across 138 files**; the reviewed PostgreSQL inventory remains **15 node IDs across 12 files**. Manifest expectations are Linux 1,773/0 skipped, default SQLite 1,758 passed/15 PostgreSQL-only skips, and Windows 1,757 passed/1 identified POSIX-only skip/15 PostgreSQL deselections. These splits are reviewed inventory arithmetic, not observed full-suite results.
- Notification dispatch is disabled in these test apps. No SMTP, HTTP, webhook or other provider call is made.

## Limits

These tests exercise a narrow set of real application authorization and persistence paths. They do not establish broad API coverage, exploit resistance beyond the tested cases, production configuration correctness, an independent security review, or penetration-test completion. The `X-Org-ID` value is only an attacker-controlled test hint; the API uses the authenticated token's organization, not that header. No secret, password, bearer token, or customer data is included in this report.
