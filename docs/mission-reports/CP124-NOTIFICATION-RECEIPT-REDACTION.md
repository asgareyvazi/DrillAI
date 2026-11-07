# CP124 — Notification Receipt Metadata Redaction

## Disposition

**CP124 software security-hardening scope: DONE.** Operational provider delivery remains **PARTIAL** and is not evidenced by this repository-only change.

| Item | Status | Evidence / boundary |
| --- | --- | --- |
| Prevent unsafe provider receipt identifiers from entering delivery records | **DONE (software)** | Receipt identifiers are persisted only when they are bounded ASCII opaque tokens. Regression coverage verifies URL, recipient, secret-locator, control-character and overlong values are omitted from the accepted outbox row, dispatch result and persisted ledger. |
| Keep notification errors sanitized | **DONE (reviewed contract)** | Existing dispatch logic stores an allowlisted error code and a generic message; SMTP response bodies/recipient-refusal details and webhook transport exception text are not copied into the delivery ledger. Existing regression tests cover SMTP refusal and adapter-error sanitization. |
| Provider delivery and receipt semantics | **PARTIAL / not externally verified** | HTTP 2xx and SMTP server acceptance remain acceptance evidence only. `delivery_confirmed` and `external_provider_verified` remain false; no vendor delivery receipt, provider evaluation or production send was performed. |

## Implementation

`_safe_provider_message_id` now accepts only identifiers of at most 128 characters matching a conservative opaque-token alphabet: ASCII letters and digits, plus `.`, `_` and `-`, with an alphanumeric first character. It rejects endpoint URLs, email addresses, secret locators, control characters, non-ASCII text and values outside the length bound. Secret-reference-like prefixes are explicitly rejected as well. Before persistence, the candidate is also compared case-insensitively against the outbound recipient and the configured `payload.secret_ref`, so a plain reference such as `notify-password` is dropped even though it matches the token alphabet. Invalid optional receipt metadata is dropped rather than changing the outcome of an otherwise accepted notification.

The same helper is applied before the receipt identifier is put on the outbox row, returned by dispatch, or appended to the delivery ledger. This prevents adapter-supplied free text from turning the provider-message-ID field into a secondary channel for recipient or secret-reference text. Existing free-form error text remains excluded by the generic error formatter and the error-code allowlist.

The rule is intentionally conservative: a provider ID containing other punctuation will be omitted. It does not mark the notification as failed and does not turn acceptance into delivery confirmation.

## Verification

- From `backend/`: `./.venv/bin/python -m pytest tests/workflow/test_notification_worker.py -q` — **7 passed, 1 PostgreSQL-marked test skipped**.
- Backend Ruff passed for the changed Python files; `compileall` passed; `git diff --check` passed.
- Collection and inventory check: **1,712 tests across 128 files**; the manifest's **14 PostgreSQL tests across 11 files** match live collection.
- The PostgreSQL-backed independent-worker test was skipped in the focused default run. No full backend suite, native-Windows run or exact-final-SHA certification is claimed at this intermediate CP124 milestone; CP132 remains the exact-final-SHA certification point.

## Limits

These tests establish local software behavior for sanitization, not external-provider trustworthiness, SMTP/HTTP delivery, mailbox receipt, notification latency or operational availability. Existing delivery rows are not retroactively rewritten. No credentials, real recipients, provider responses or delivery receipts were used or fabricated.

## Exact-SHA CI follow-up

GitHub Actions run [42359053299](https://github.com/asgareyvazi/DrillAI/actions/runs/42359053299), for commit `6c5a44e9e649bce78a30b685ee82fe4902bb3ff2`, is listed as **cancelled**. It provides no pass/fail evidence for that SHA; CP124's exact-SHA certification remains unavailable, and CP132 must certify the final integrated SHA.
