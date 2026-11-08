# CP127 — Production SMTP Egress Policy and DNS Pinning

**Repository implementation: DONE. Live relay/provider verification: BLOCKED_EXTERNAL.**

## Implemented

Production SMTP now applies the same authoritative egress-policy loader and snapshot used by workflow HTTP and webhook transports (database policy when present, with the existing configured environment baseline on initial startup). Before resolving credentials or opening a socket, the worker:

- uses only the operator-configured `DRILLAI_SMTP_URL` (the CP125 message-level endpoint override still fails closed);
- requires `smtps` in production and validates the destination against the egress/SSRF policy, including all resolved addresses;
- rejects loopback, link-local/metadata, and unapproved private-network destinations;
- connects to the validated, pinned IP rather than resolving the hostname again, while TLS certificate verification and SNI continue to use the configured hostname; and
- returns a generic policy error without recording the endpoint or resolver exception. Temporary authoritative-policy-store failures remain retryable and fail closed before transport.

An internal `workflow.smtp` resolver profile and `resolve_and_pin_smtp_endpoint_async` helper reuse the connector DNS bounds and egress-policy machinery. This is application-level outbound protection; it is not a host/network firewall or a policy for unrelated processes.

## Verification

- `backend/.venv/bin/python -m pytest -o addopts= tests/workflow/test_notification_worker.py tests/workflow/test_workflow_http_egress.py -q` — **18 passed, 1 PostgreSQL-marked skip**.
- Backend collection — **1,718 tests**; the reviewed PostgreSQL inventory remains **14 node IDs across 11 files**.
- Full backend Ruff, `compileall`, JSON parsing, and `git diff --check` passed.
- The resolver tests use controlled synthetic DNS answers; the SMTP adapter tests use a fake transport. No DNS query to an external provider, SMTP connection, recipient contact, or delivery attempt was made.

## Limits and acceptance

The application still trusts the operator-controlled SMTP hostname and the configured global egress policy. Public destinations remain governed by the repository's existing policy behavior; approved private relays require the configured private-network policy. Pinning prevents a second DNS lookup in this adapter but does not impose infrastructure-level egress controls. A fake SMTP acceptance is not provider acceptance, and real SMTP acceptance would still not prove mailbox delivery. No live relay, production deployment, or operator network-policy result is claimed. The CP127 exact-SHA Actions run `42362056058` (SHA `5b21fb9197ea3f91f8143f6aa6e1170380cd3080`) was canceled when a higher-priority request for this branch arrived; it supplies no CI pass/fail certification. At the CP127 checkpoint, full backend reruns and native-Windows certification were still pending. The later integrated CP130–CP132 SHA passed exact-SHA run `42481107515`; this does not retroactively certify CP127's cancelled SHA or prove live SMTP delivery.
