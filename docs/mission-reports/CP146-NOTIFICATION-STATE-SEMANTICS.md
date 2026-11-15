# CP146 — Notification Dispatch and Receipt-State Semantics

**Date:** 2026-10-20

**Status:** The local outbox status and recipient-receipt status are now separate in the API and bilingual UI. The read model reports recipient delivery as `unknown`; no authenticated provider-receipt ingestion mechanism exists, so delivery, bounce, or recipient receipt is not claimed.

**Evidence class:** Local software tests with synthetic database rows and UI fixtures. No vendor endpoint, SMTP relay, webhook recipient, or external provider was contacted.

## State contract

- Outbox `status` represents local queue/transport lifecycle: `queued`, `sending`, transport `accepted`, legacy `sent`, retryable/terminal failure, `dead_letter`, or `reconciliation_required`.
- `accepted` remains limited to the recorded transport evidence (HTTP 2xx, SMTP server acceptance, or local in-app outbox acceptance). It is not a recipient-delivery receipt.
- `sent` is retained as a legacy outbox status and is not retroactively interpreted as proof that a provider or recipient received the message.
- The notification read API now includes a distinct `recipient_delivery_status`, currently `unknown` for every row. `delivery_confirmed` and `external_provider_verified` remain false. Retryable/terminal failures and ambiguous transport outcomes remain distinct outbox states; none is converted into a delivery receipt.
- English and Persian UI display the receipt state separately from the outbox state. Labels for `sent`, `delivered`, and `bounced` are explicitly conditioned on a verified provider receipt, but the current software has no receipt-ingest path and never emits those values.

## Verification observed

- `backend/.venv/bin/python -m pytest -o addopts='' -q tests/api/test_notifications_api.py`: **3 passed**. The added lifecycle regression verifies queued, sending, accepted, legacy sent, retryable/terminal failure, dead-letter, and reconciliation rows retain their outbox state while their recipient receipt remains unknown and unconfirmed.
- Focused frontend `PlatformPage.test.tsx`: **3 passed**; the synthetic ambiguous row renders the separate “No verified receipt” state.
- Frontend typecheck and targeted ESLint passed.
- CP16 documentation/count consistency guard passed. Backend collection is **1,770 tests across 137 files**; the reviewed PostgreSQL list remains **15 IDs across 12 files**. Manifest expectations are Linux 1,770/0 skipped, default SQLite 1,755/15 PostgreSQL-only skips, and Windows 1,754/1 identified POSIX-only skip/15 PostgreSQL deselections. These are expectations, not observed full-platform suite results.
- E2E collection shape verified **128 tests across 20 files** (Chromium 121, faults 4, auth 3); this is not a browser-suite execution.
- Changed Python files passed Ruff and `compileall`; `git diff --check` passed.
- Fresh revalidation alongside CP147–149 changes: notification API, notification worker, and CP148/CP26 auth-boundary selection passed **21 tests, 1 PostgreSQL-marked skip**. The current `PlatformPage.test.tsx` selection passed **4 tests** (including CP149's RTL/accessibility/readback coverage); frontend typecheck and targeted ESLint passed. This confirms the dispatch/receipt separation on the current local worktree, not a provider-receipt ingestion path or final-SHA CI result.

## Limits and disposition

No signed callback, provider webhook, mailbox read receipt, delivery event, or vendor response was received or simulated as external evidence. `delivered` and `bounced` remain unavailable from the current API; UI catalog labels do not imply a configured provider or receipt processor. Any future receipt integration must authenticate the source, bind events to the correct tenant/message, retain event provenance and replay protection, preserve conflicting states, and be tested at real authorization boundaries before those statuses can be populated. Local acceptance, HTTP/SMTP transport behavior, or a legacy `sent` row must never be reported as recipient delivery.

No production notification delivery, provider acceptance, customer receipt, operational availability, or RPO/RTO claim is made. Final full-matrix and exact-SHA Linux/native-Windows certification remains CP151 work.
