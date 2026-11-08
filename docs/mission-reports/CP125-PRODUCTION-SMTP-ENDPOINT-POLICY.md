# CP125 — Production SMTP Endpoint Policy

## Disposition

**CP125 software guard: DONE.** Live SMTP connectivity, deployment configuration and recipient delivery remain **PARTIAL / not externally verified**.

| Item | Status | Evidence / boundary |
| --- | --- | --- |
| Prevent per-message SMTP endpoint selection in production | **DONE (software)** | In production, `EmailSmtpNotificationAdapter` uses only `DRILLAI_SMTP_URL`; a message-level `smtp_url` that differs from the operator-configured value fails closed before a socket is opened. |
| Preserve non-production test/development behavior | **DONE (software)** | The existing message-level endpoint path remains available outside production; existing SMTP transport/refusal tests still pass. |
| Live SMTP delivery and network egress policy | **PARTIAL** | No real SMTP relay or recipient was contacted. This change does not enforce a network firewall/egress allowlist against a misconfigured or compromised operator-supplied `DRILLAI_SMTP_URL`. |

## Implementation

Production previously accepted `smtp_url` from the persisted notification payload even though webhook targets pass through the authoritative HTTP egress policy. Production SMTP now requires the endpoint to come from the deployment's `DRILLAI_SMTP_URL`. A payload endpoint may be omitted or must exactly match that configured value; a mismatch, including a payload-only endpoint when the environment value is absent, returns a generic terminal `smtp_endpoint_policy_blocked` outcome without resolving the supplied hostname or opening an SMTP connection. The existing production `smtps` requirement remains in effect for the configured endpoint.

Outside production, message-level SMTP configuration continues to work for local development and deterministic adapter tests. No endpoint was guessed, scanned, or contacted while implementing this guard.

## Verification

- From `backend/`: `./.venv/bin/python -m pytest tests/workflow/test_notification_worker.py -q` — **8 passed, 1 PostgreSQL-marked test skipped**. The new regression simulates production settings, removes `DRILLAI_SMTP_URL`, supplies a payload-only endpoint under the reserved `.invalid` domain, and verifies a sanitized terminal rejection before the patched SMTP constructor can be reached.
- Backend Ruff passed for the changed Python files; `compileall` passed; `git diff --check` passed.
- The reviewed backend inventory is **1,713 tests across 128 files**; expected PostgreSQL-enabled/default/Windows totals were updated to 1,713/0, 1,699/14 and 1,698/1 POSIX-only skip/14 PostgreSQL deselections. Live collection and PostgreSQL node-ID inventory are checked before the CP125 commit.
- This intermediate milestone does not claim a full backend suite, production deployment, live SMTP acceptance, mailbox receipt or exact-final-SHA Linux/native-Windows certification.

## Limits

This restricts a tenant/workflow-selected endpoint in production; it is not a general SMTP SSRF defense. The operator-provided URL must still be protected by deployment secrets and outbound network policy. A configured endpoint's DNS, IP reachability, TLS certificate, relay authorization and delivery behavior were not exercised. SMTP acceptance remains distinct from recipient delivery.

## Exact-SHA CI follow-up

GitHub Actions run [42359313648](https://github.com/asgareyvazi/DrillAI/actions/runs/42359313648), for commit `7c274ab99017b9ad148c25ca94f28e78a596124c`, is listed as **cancelled**. It provides no pass/fail evidence for that SHA; CP125's exact-SHA certification remains unavailable; the later integrated CP130–CP132 SHA was certified by run `42481107515` (see `CP132-FINAL-RELEASE-ASSURANCE.md`), without retroactively certifying CP125's cancelled SHA.
