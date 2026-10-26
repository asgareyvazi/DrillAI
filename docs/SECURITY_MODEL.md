# DrillAI Security Model & Architecture (`docs/SECURITY_MODEL.md`)

This document is the authoritative reference for DrillAI's authentication, session security,
authorization (RBAC + L0–L5 action governance), multi-tenant isolation, cryptographic password
and secret storage, outbound SSRF/egress defense, and production startup guards.

---

## 1. Authentication Architecture

DrillAI separates interactive browser identity, programmatic API automation, and local development
impersonation into three distinct, non-overlapping paths in `backend/src/drillai/api/deps.py`:

| Path | Transport | Intended Caller | Storage & Verification |
| --- | --- | --- | --- |
| **Interactive Browser Session** | `Cookie: drillai_session=<ds_prefix_secret>` (`HttpOnly`, `SameSite=lax\|strict`, `Secure` in production) + `X-CSRF-Token` header on mutations | React web application (`/login`, `/wells`, `/cockpit`, `/workflows`, WebSockets) | Server-side `user_sessions` table (`UserSession`). Only `sha256$<hex>` of `prefix.secret` is stored. Enforces sliding idle and absolute TTL (`DRILLAI_SESSION_TTL_HOURS`) and immediate server-side revocation (`POST /api/v1/auth/logout` or user deactivation). |
| **Programmatic API Token** | `Authorization: Bearer dk_<prefix>_<secret>` | Headless CLI tools, CI scripts, external orchestrators | Server-side `api_tokens` table (`ApiToken`). Clear token is returned once at creation (`POST /api/v1/auth/tokens`); only `sha256$<hex>` is stored. Effective permissions are `user_role_permissions ∩ token.scopes`. |
| **Development Role Header** | `X-DrillAI-Dev-Roles: engineer,admin` (or `?dev_roles=` on dev WebSockets) | Local development & automated non-auth E2E suites **only** when `DRILLAI_AUTH_ENABLED=false` | Completely ignored when `DRILLAI_AUTH_ENABLED=true`. Production startup refuses `DRILLAI_AUTH_ENABLED=false` (`ConfigurationError`). |

### Frontend Bundle Secret Isolation (`CP21-B`)

- The frontend **never** reads `VITE_API_TOKEN` or embeds static API bearer tokens into production
  JavaScript bundles.
- `frontend/src/lib/envSecurity.ts` (`assertSafeFrontendEnv`) runs inside `frontend/vite.config.ts`
  before every build and fails immediately if `VITE_API_TOKEN` is set in production mode or if any
  `VITE_*` variable matches secret/key/password naming patterns (`SECRET`, `PASSWORD`, `PRIVATE_KEY`,
  `MASTER_KEY`, `API_KEY`, `DATABASE_URL`) or credential prefixes (`dk_`, `ds_`, `prt_`, `sk-`).

---

## 2. Password Hashing, DoS Bounds & Rehash-on-Login (`CP22-B`)

Implemented in `backend/src/drillai/security/passwords.py`:

1. **Supported Memory-Hard KDF Schemes**:
   - **Hardened OWASP `scrypt` (default)**: `scrypt$n=32768,r=8,p=1$<salt-b64>$<hash-b64>`
     (`N = 2^15 = 32,768`, `r = 8`, `p = 1`, requiring **32.0 MiB** per derivation; configurable up to
     `N = 2^16 = 65,536` / **64.0 MiB**).
   - **OWASP `Argon2id`**: `$argon2id$v=19$m=19456,t=2,p=1$<salt-b64>$<hash-b64>`
     (`m = 19,456 KiB` / **19.0 MiB**, `t = 2` iterations, `p = 1` lane) via OpenSSL `Argon2id`.
2. **Legacy Compatibility & Automatic Rehash-on-Login**:
   - Legacy `scrypt$n=16384,r=8,p=1$...` hashes continue to verify in `verify_password(...)`.
   - `needs_rehash(...)` returns `True` for any `scrypt` hash with `n < 32768` (or `r < 8`) and any
     `Argon2id` hash with `m < 19456` or `t < 2`.
   - On successful `POST /api/v1/auth/login`, if `needs_rehash(user.password_hash)` is `True`, the
     router transparently upgrades `user.password_hash` to the current policy (`scrypt$n=32768,r=8,p=1$...`).
3. **Pre-Verification Parameter Bounds (CPU/Memory DoS Prevention)**:
   - Before invoking `hashlib.scrypt` or `Argon2id.verify_phc_encoded`, `verify_password` validates:
     - Password length `<= 1024` UTF-8 bytes.
     - For `scrypt`: `n` is a power of two in `[1024, 65536]`, `r in [1, 16]`, `p in [1, 4]`,
       `128 * r * n * p <= 64 MiB`, `salt` length in `[8, 64]`, and `dklen` in `[16, 64]`.
     - For `Argon2id`: `m in [8192, 65536]` KiB, `t in [1, 8]`, `p in [1, 4]`.
   - Any out-of-range or malformed hash string returns `False` in `< 0.1 ms` without allocating KDF memory.
4. **Bounded KDF Concurrency**:
   - `hash_password_async` and `verify_password_async` run on a dedicated bounded
     `ThreadPoolExecutor(max_workers=4, thread_name_prefix="drillai-kdf")` guarded by a process-wide
     `threading.BoundedSemaphore(4)`, ensuring concurrent login bursts cannot starve the asyncio
     event loop or exhaust container RAM (worst-case concurrent KDF memory is bounded to `4 × 32 MiB = 128 MiB`).

---

## 3. Session Security, CSRF & WebSocket Authentication (`CP21-C`)

- **Cookie Attributes**: `drillai_session` is set with `HttpOnly=True`, `Path="/"`,
  `SameSite=settings.session_cookie_samesite` (`lax` default), and
  `Secure=True` in production (`DRILLAI_SESSION_COOKIE_SECURE=true`). JavaScript (`document.cookie`)
  cannot read `drillai_session`.
- **CSRF Synchronizer Token**:
  - Every session has a deterministic HMAC-SHA256 CSRF token derived via
    `HMAC-SHA256(session_csrf_secret, f"csrf:{session_id}:{session_token_hash}")`.
  - All state-changing HTTP methods (`POST`, `PUT`, `PATCH`, `DELETE`) authenticated via the
    `drillai_session` cookie (except `/api/v1/auth/login`, `/api/v1/auth/bootstrap`, and
    `/api/v1/auth/password/reset`) must supply a matching `X-CSRF-Token` header (`hmac.compare_digest`);
    missing or mismatched tokens fail closed with `403 PermissionDenied` (`csrf_validation_failed`).
  - Requests authenticated via an explicit `Authorization: Bearer dk_...` header are not subject to
    cookie CSRF because browsers never attach `Authorization` headers ambiently across origins.
- **WebSocket Authentication (`/api/v1/runs/{id}/events/stream`, `/api/v1/wells/{id}/live/stream`)**:
  - Browser WebSockets authenticate using the ambient `HttpOnly` `drillai_session` cookie on the
    WebSocket upgrade handshake.
  - Passing `?token=` in a WebSocket query string when `DRILLAI_AUTH_ENABLED=true` is **rejected**
    with WebSocket close code `4401` (`AuthenticationRequired`) so bearer credentials never leak into
    access logs, proxy logs, or browser history.

---

## 4. Multi-Worker Brute-Force Rate Limiting (`CP21-E`)

- Rate limiting state is persisted in the database table `auth_rate_limits` (`AuthRateLimitBucket`),
  making it consistent across multiple `uvicorn` worker processes and container replicas.
- Rate limit checks and failure increments run inside an **independent database session/transaction**
  so raising `401 AuthenticationRequired` or `429 RateLimited` on the request session never rolls
  back the recorded failure count or audit entry.
- Configured via:
  - `DRILLAI_AUTH_RATE_LIMIT_WINDOW_SECONDS` (default `300` s)
  - `DRILLAI_AUTH_RATE_LIMIT_MAX_FAILURES` (default `5` failures per window)
  - `DRILLAI_AUTH_RATE_LIMIT_LOCKOUT_SECONDS` (default `300` s lockout with `429` + `Retry-After` header)

---

## 5. RBAC, Action Levels (`L0`–`L5`) & Scoped API Tokens (`CP21-D`)

Defined in `backend/src/drillai/security/actions.py`, `backend/src/drillai/security/rbac.py`, and
`backend/src/drillai/security/catalog.py`:

| Action Level | Governance Meaning | Examples |
| --- | --- | --- |
| `L0` | Read-only observation | `well.read`, `twin.read`, `workflow.read`, `telemetry.read` |
| `L1` | Low-impact workspace annotation / draft | `context.build`, `ai.complete` |
| `L2` | Standard engineering calculation & draft authoring | `engine.run`, `workflow.create`, `document.upload`, `auth.token.create` |
| `L3` | Operational workflow execution & tenant user administration | `workflow.execute`, `telemetry.ingest`, `user.manage` |
| `L4` | High-consequence well twin mutation, approval decision, connector/secret governance | `twin.update`, `workflow.approve`, `connector.manage`, `security.manage` |
| `L5` | Autonomous closed-loop execution (disabled unless an explicit org automation envelope is active) | Autonomous rig/control actions |

- **Fail-Closed Unknown Actions**: Any action key not registered in `catalog.py` defaults to `L4`
  and requires explicit permission.
- **Workflow node-level floor**: a persisted `action_level` may escalate a node above its registered
  `NodeSpec.action_level`, but cannot lower the floor or claim `L5` unless that node type is itself
  registered for `L5`. The graph validator and runtime authorization gate both enforce this; an
  editor-supplied `L0` cannot turn an L4 integration node into unapproved execution.
- **Role Separation**:
  - `org_admin`: Tenant-scoped user/token administrator (`user.read`, `user.manage`, `audit.read`,
    `well.read`, `project.read`). Cannot assign the platform `admin` role or modify system-wide
    `ConnectorEgressPolicy` (`system.egress_policy.manage`).
  - `admin`: Platform administrator (includes `security.manage`, `user.manage`,
    `system.egress_policy.manage`). Does not bypass engineering approval roles (`drilling_supervisor`,
    `well_manager`) for `twin.update` or `workflow.approve`.
- **Scoped API Tokens**: When `Principal` is resolved from an `ApiToken` with non-empty `scopes`,
  `Principal.permissions` is narrowed to the intersection of the user's role permissions and the
  token's `scopes`.

---

## 6. Secrets Management & Authenticated Encryption (`CP22-A`)

Managed connector and notification credentials use `SecretRef` rows (`secret_refs` table, scoped
by `(org_id, key)`) rather than literal credential fields. The environment backend accepts only
`env:DRILLAI_SECRET_<NAME>` where `<NAME>` matches `[A-Z0-9_]{2,120}`; both creation and runtime
resolution enforce that scope, including legacy rows. The encrypted database backend is implemented
in `backend/src/drillai/security/secrets.py`:

- Workflow `output.notify` and generic `integration.http_fetch` reject credential-bearing custom
  header names (authorization/auth, cookies, API/access keys, tokens, secrets, passwords, private
  keys, credentials, signatures, and bearer headers) so such values are not persisted in workflow
  JSON. Webhook notifications use the managed `secret_ref` field and the selected bearer/HMAC scheme.
- Generic `integration.http_fetch` has no managed arbitrary-header secret mapping; it must not be used
  with credentials embedded in its URL, query, body, or ordinary custom headers. Free-form request
  bodies and URL query strings remain operator-authored content and are not a general-purpose secret
  store; do not place credentials there.
- Notification delivery fails closed for credential-bearing custom headers in legacy `OutboundMessage`
  payloads; use the managed reference instead. Errors avoid echoing supplied secret values.

`SecretRef` rows support two production backends:

1. **`backend="env"`**:
   - `SecretRef.locator` holds `env:DRILLAI_SECRET_<NAME>`, resolved at runtime from the process
     environment.
2. **`backend="db_encrypted"` (`AES-256-GCM`)**:
   - Encrypted at rest in `SecretRef.locator` using `AES-256-GCM` (`cryptography` `AESGCM`).
   - **Key Derivation**: 256-bit per-version subkeys derived via `HKDF-SHA256` from
     `DRILLAI_SECRET_MASTER_KEY` (and optional `DRILLAI_SECRET_PREVIOUS_MASTER_KEYS="v1:<old_key>"`
     during key rotation) with domain info `b"drillai-secret-ref-aead-v1:kv=<version>"`.
   - **AEAD Context Binding (`AAD`)**: Every encryption binds
     `AAD = f"org:{org_id}|key:{secret_key}|kv:{key_version}"` into the 128-bit GCM authentication tag.
     Transplanting an encrypted `locator` across organizations or secret keys fails tag verification.
   - **Envelope Format**: `enc:v1:kv=<version>:<nonce_b64>:<ciphertext_and_tag_b64>`.
   - **Key Rotation**: `POST /api/v1/platform/secrets/{secret_key}/rotate` decrypts under the envelope's
     `kv` and re-encrypts with a fresh 96-bit CSPRNG nonce under `DRILLAI_SECRET_ACTIVE_KEY_VERSION`.
     Startup rejects duplicate active/previous key-version identifiers rather than silently replacing
     one key in the parsed keyring.
   - **Redaction**: `GET /api/v1/platform/secrets` and all connector/notification responses return
     only `mask_secret_ref_row(...)` (`masked_preview: "********"`), never exposing `locator` or plaintext.

---

## 7. Outbound SSRF & Egress Policy Defense

Implemented in `backend/src/drillai/telemetry/connectors.py` and enforced across WITSML, ETP,
workflow `integration.http_fetch`, and outbound webhook notifications
(`backend/src/drillai/workflow/nodes.py`, `backend/src/drillai/workflow/notifications.py`):

- Rejects loopback (`127.0.0.0/8`, `::1`), link-local (`169.254.0.0/16`, cloud metadata `169.254.169.254`),
  multicast, unspecified, CGNAT (`100.64.0.0/10`), benchmark (`198.18.0.0/15`), NAT64 (`64:ff9b::/96`),
  IPv4-mapped IPv6, and ambiguous octal/hex/integer IP encodings.
- Private RFC 1918 rig networks require explicit allowlisting in the durable `ConnectorEgressPolicy`
  table (`allowed_hosts` + `allowed_private_cidrs`), governed by `system.egress_policy.manage`.
- DNS resolution executes asynchronously via a bounded thread pool with a 3-second timeout and pins
  the validated IP address at socket connect time to defeat DNS rebinding (TOCTOU).
- Workflow HTTP and webhook clients disable environment proxy routing (`trust_env=False`), connect to
  the pinned address while retaining the validated Host/SNI identity, and refuse redirects so a
  permitted first hop cannot redirect to a disallowed destination. Workflow HTTP is L4 and remains
  subject to the node's approval gate; approval does not bypass the egress policy.

---

## 8. Production Startup Guards (`CP22-C`)

When `DRILLAI_ENVIRONMENT=production`, `create_app()` in `backend/src/drillai/api/app.py` fails
closed at startup (`ConfigurationError`) if any of the following hold:

- `DRILLAI_AUTH_ENABLED` is `false`
- `DRILLAI_DEBUG` is `true`
- `DRILLAI_CORS_ORIGINS` is empty or contains `"*"`
- `DRILLAI_E2E_FAULTS` is `true`
- `DRILLAI_CONNECTOR_ALLOW_LOOPBACK` is `true`
- `DRILLAI_ALLOWED_HOSTS` is empty or contains `"*"` (mounts `TrustedHostMiddleware` when explicit hosts are configured)
- `DRILLAI_SESSION_COOKIE_SECURE` is `false`
- `DRILLAI_SESSION_CSRF_SECRET` is empty, default (`drillai-dev-csrf-synchronizer-key`), or `< 32` characters
- `DRILLAI_SECRET_BACKEND=database` and `DRILLAI_SECRET_MASTER_KEY` is `< 32` characters
- `DRILLAI_DATABASE_URL` contains a placeholder password (`change_me`, `changeme`, `drillai:drillai@`, `postgres:postgres@`, `sample_password`)
- `scripts/seed_demo.py` is invoked with `DRILLAI_ENVIRONMENT=production`

---

## 9. Honest Out-of-Scope / External Deployment Responsibilities

- **External Enterprise IdP (OIDC / SAML 2.0) Federation**: Not implemented in-repo; authentication
  is local multi-user credentials + server-side sessions + scoped API tokens.
- **Hardware HSM / Cloud KMS Envelope Wrapping**: Master keys (`DRILLAI_SECRET_MASTER_KEY`) are
  injected via environment variables from the deployment orchestrator rather than fetched over a cloud
  KMS SDK API.
- **TLS Termination**: Terminated at the reverse proxy / ingress boundary (see `docs/CONNECTORS_AND_OPERATIONS.md`).
