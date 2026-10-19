# Security Policy

## Supported Versions

DrillAI is maintained on the active certification branch (`arena/01a0dca0-drillai`) and `main`. Security fixes are applied to the active release branch and verified across both Linux (`ubuntu-latest`) and native Windows (`windows-latest`) certification pipelines.

| Version / Branch | Supported | Notes |
|---|---|---|
| `0.1.x` (`arena/01a0dca0-drillai`) | Yes | Active multi-platform hardened baseline (CP21–CP25) |
| `< 0.1.0` | No | Pre-CP21 prototypes are unsupported |

---

## Reporting a Vulnerability

**Do not report undisclosed security vulnerabilities in public GitHub issues, pull requests, or discussions.**

Please report suspected vulnerabilities privately via **GitHub Private Vulnerability Reporting** (`Security -> Advisories -> Report a vulnerability` on `https://github.com/asgareyvazi/DrillAI/security/advisories/new`) with:

1. Affected component (`backend/src/drillai/security/`, `backend/src/drillai/api/`, `backend/src/drillai/telemetry/`, `frontend/`, `docker-compose*.yml`, or `frontend/nginx.conf`).
2. Exact reproduction steps, request/response trace, or proof-of-concept test.
3. Impact assessment (authentication bypass, cross-tenant data access, SSRF/egress bypass, secret exposure, or privilege escalation).

### Response Targets
- **Initial acknowledgement**: within **48 hours**.
- **Triage & severity classification**: within **5 business days**.
- **Remediation & regression test publication**: prioritized by severity (`Critical` / `High` before any feature work).

---

## Security Architecture & Hardening Controls

Detailed documentation of DrillAI's authentication, session lifecycle, CSRF protection, `AES-256-GCM` + `HKDF-SHA256` secret encryption, `Argon2id`/`scrypt` password hashing, SSRF egress controls, and production fail-closed startup guards is maintained in [`docs/SECURITY_MODEL.md`](docs/SECURITY_MODEL.md).

Key security invariants enforced in CI (`scripts/verify_supply_chain_and_secrets.py`):
- **No Frontend Secret Embedding**: Production Vite bundles (`frontend/dist`) are scanned for `VITE_API_TOKEN`, `VITE_SECRET`, `VITE_PRIVATE`, and sentinel credentials. Browser sessions use `HttpOnly`, `SameSite=Lax` cookies (`drillai_session`) paired with HMAC-SHA256 synchronizer CSRF tokens (`X-CSRF-Token`).
- **No WebSocket Query-String Credentials**: Browser WebSocket connections (`/api/v1/runs/{id}/events`, `/api/v1/wells/{id}/live`) authenticate via the `HttpOnly` session cookie and reject `?token=` query parameters.
- **GitHub Actions Full-SHA Pinning**: Every third-party action in `.github/workflows/ci.yml` is pinned to an immutable 40-character commit SHA.
- **Automated Supply-Chain & Secret Scanning**: Every CI run executes `pip-audit`, `npm audit --omit=dev --audit-level=high`, and git-tracked secret scanning with zero `continue-on-error` suppression.

---

## Software License Status

No open-source `LICENSE` file has been added to this repository; `backend/pyproject.toml` declares `license = { text = "Proprietary" }`. Until the repository owner explicitly selects and commits a license, all rights remain reserved by the owner.
