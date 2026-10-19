# CP21 Mission Report — Multi-User Authentication, Session Security & Browser Identity

- **Repository**: `asgareyvazi/DrillAI`
- **Branch**: `arena/01a0dca0-drillai` (`main` untouched at `bfa066b28e0071880cb9191a4f1d47fdaa143e04`)
- **Alembic Revision**: `d4e7a9c12b83` (`multi_user_sessions_and_auth_hardening`, revising `c9f1a48b2e31`)

---

## 1. CP21-A Source-Backed Security Boundary Audit

| Surface | Pre-CP21 Finding | CP21 Remediation |
| --- | --- | --- |
| `frontend/src/api/client.ts` & `frontend/src/vite-env.d.ts` | Read `import.meta.env.VITE_API_TOKEN`, which inlines static credentials into compiled `dist/assets/*.js` if set during `vite build`. | Removed `VITE_API_TOKEN` from `client.ts` and `vite-env.d.ts`. Added build-time `assertSafeFrontendEnv()` guard in `frontend/src/lib/envSecurity.ts` wired into `frontend/vite.config.ts` to fail production builds if `VITE_API_TOKEN` or any `VITE_*` secret pattern is present. |
| `frontend/src/lib/runEvents.ts` & `frontend/src/lib/wellLiveStream.ts` | Appended `?token=<bearer>` to WebSocket URLs (`buildRunEventsUrl`, `buildWellLiveStreamUrl`), exposing tokens in URLs, proxy logs, and browser history. | Removed `?token=` from browser WebSocket URLs. Browser WebSockets authenticate via the same-origin `HttpOnly` `drillai_session` cookie; backend WebSocket endpoints (`runs.py`, `live.py`) reject `?token=` query parameters with WebSocket close code `4401`. |
| `backend/src/drillai/api/deps.py` & `backend/src/drillai/db/models/identity.py` | Only bearer API tokens (`ApiToken`) and dev-mode headers (`X-DrillAI-Dev-Roles`) existed; no server-side browser session table, CSRF verification, or password reset token model existed. | Added `UserSession` (`user_sessions`), `PasswordResetToken` (`password_reset_tokens`), and `AuthRateLimitBucket` (`auth_rate_limits`) in migration `d4e7a9c12b83`. Updated `get_principal` to resolve `HttpOnly` session cookies (`drillai_session`) with idle/absolute expiry and HMAC-SHA256 `X-CSRF-Token` enforcement on state-changing HTTP methods (`POST`, `PUT`, `PATCH`, `DELETE`). |
| `backend/src/drillai/api/routers/auth.py` | No `/api/v1/auth/*` router existed for login, logout, principal introspection, user provisioning, password change/reset, or scoped API token lifecycle. | Created `backend/src/drillai/api/routers/auth.py` providing `/auth/bootstrap`, `/auth/login`, `/auth/logout`, `/auth/me`, `/auth/password/change`, `/auth/password/reset-tokens`, `/auth/password/reset`, `/auth/users`, `/auth/users/{user_id}`, `/auth/tokens`, and `/auth/tokens/{token_id}` with `AuditLog` persistence across independent DB transactions. |
| `backend/src/drillai/security/rbac.py` & `ApiToken.scopes` | `ApiToken.scopes` existed in ORM schema, but `Principal.permissions` only unioned role permissions without intersecting token scopes. | Added `Principal.scoped_permissions` and enforced permission-scope intersection (`base_perms & set(self.token_scopes)`) in `Principal.can()` and `check_permission()`, plus privilege-escalation prevention at token creation (`POST /api/v1/auth/tokens`). |
| Multi-worker brute-force protection | No shared rate-limit state across multi-worker uvicorn processes. | Implemented DB-backed `AuthRateLimitBucket` (`auth_rate_limits`) enforcing per-(action, identifier/IP) window limits and automatic lockout with `429 Too Many Requests` (`Retry-After` header) and `auth.login.rate_limited` audit entries. |
| `frontend/src/components/layout/AppShell.tsx` & `frontend/src/pages/auth/LoginPage.tsx` | No `/login` screen existed; `shell-role-switch` rendered even before checking `auth_enabled`. | Added `LoginPage.tsx` (`/login`), authenticated principal badge (`shell-auth-principal`), logout button (`shell-logout-button`), unauthenticated sign-in prompt (`shell-unauthenticated`, `shell-login-link`), and hid `shell-role-switch` whenever `auth_enabled` is true or `/platform/identity` returns `401`. |
| `scripts/seed_demo.py` | Created only a single supervisor token without synthetic multi-role user accounts or production guard. | Refuses `DRILLAI_ENVIRONMENT=production` (`SystemExit`) and seeds 5 synthetic demo accounts (`supervisor`, `engineer`, `viewer`, `admin`, `manager` `@demo.drillai.local`) in dev/test. |

---

## 2. Verification Summary

- **PostgreSQL 16 Migration & Zero-Drift Check**:
  `DRILLAI_TEST_POSTGRES=1 backend/.venv/bin/pytest backend/tests/db/test_migrations_postgres.py -o addopts="" -v` → `2 passed` (verified `alembic upgrade head`, `alembic check` zero drift on `d4e7a9c12b83`).
- **CP21 Backend Security Suite**:
  `backend/.venv/bin/pytest backend/tests/security/test_cp21_auth_and_sessions.py -o addopts="" -v` → `5 passed`.
- **Frontend Unit & Build-Time Secret Guard**:
  `cd frontend && npm test && npm run build` → `29` test files, `315 passed`; `VITE_API_TOKEN=sentinel_secret_123 npm run build` refused at build time.
- **Playwright Real-Browser Auth E2E (`chromium-auth`)**:
  `cd frontend && npm run e2e -- e2e/error-auth.spec.ts --project=chromium-auth` → `2 passed` (anonymous 401 behavior + interactive Viewer/Supervisor login, `HttpOnly` cookie isolation from `document.cookie`, RBAC ceiling `L0` vs `L4`, cookie-authenticated WebSocket `/live/stream` without `?token=` or `dev_roles=`, and server-side logout invalidation).
