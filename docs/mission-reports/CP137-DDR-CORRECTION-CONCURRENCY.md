# CP137 — Daily-Report Correction Concurrency

**Status:** repository software implementation and focused verification are complete. This report does not claim exact-SHA cross-platform certification; CP151 owns the final Linux/native-Windows matrix.

## Implemented

The existing daily-report API already reserves a correction revision with a compare-and-swap on the report's `latest_revision_number`. CP137 adds an API-level concurrency regression to the established competing-writer journey, exercising that correction path after an independent supervisor approves revision 1.

The test issues two concurrent correction requests with distinct idempotency keys against the same approved report. It requires exactly one `201 Created` and one `409 Conflict`, then reads the resulting report and history through the API. The winner must be revision 2, `supersedes_revision_id` must reference the approved revision 1, `latest_revision_number` must be 2, and `latest_approved_revision_number` must remain 1. History must contain exactly two ordered revisions and a valid supersession link. This verifies the externally observable lifecycle and prevents two writers from claiming the same revision number or advancing an orphaned pointer.

The case uses the real API, persistence, authorization and idempotency middleware; it does not mock the correction service or database. No schema or product behavior was changed. The focused test reuses an existing test function, so the reviewed backend test inventory remains 1,745 and the Playwright inventory remains 127 / 20 files.

## Verification

- Competing draft-update plus concurrent-correction API regression: **1 passed**.
- Daily-report API, tenant, domain and scoped DB-readback selection: **30 passed, 1 PostgreSQL-marked skip**. The skip is the existing PostgreSQL-only marker and is reported as skipped.
- Ruff on the changed API test and `git diff --check`: passed.
- The CP136 parent-worktree full local Linux and default backend inventory runs remain **1,745 passed / 0 skipped** and **1,730 passed / 15 PostgreSQL-only skips**, respectively. CP137's added assertions passed in the focused selection; a new exact-SHA full cross-platform result is not claimed.

## Acceptance boundary

This is synthetic local software evidence for database concurrency control. It is not a field report, operator acceptance, customer approval, external-provider response, production database soak or production RPO/RTO result. Revision history and approved state remain distinct; no concurrent winner is treated as evidence of business approval beyond the explicit synthetic supervisor action in the test.
