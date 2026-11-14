# CP136 — DDR Intake, Review, and Database Readback

**Status:** implementation and focused local verification are complete. This report does not claim exact-SHA Linux/native-Windows certification; CP151 owns the fresh final-SHA matrix.

## Implemented

CP136 strengthens the daily-report lifecycle journey so the browser exercises the real operator path instead of creating the uploaded source through a test-only API shortcut:

1. A synthetic project, well, and wellbore are created through the product API. The browser opens the well's Documents workspace, selects the engineer role and DDR document type, chooses the wellbore, and uploads a generated CSV through the real file input. The browser observes the actual `POST /api/v1/documents` response and requires HTTP 201.
2. The test reads the persisted document through the API and waits for the real extractor to create a `report_header` and an `operation_row`. It checks the uploaded document's well/wellbore scope and the extracted date, report number, summary, operation, duration, and explicit measured-depth value.
3. Through the Daily Reports workspace, the operator selects the exact header and operation records, maps the report date/number and summary to source fields, supplies the explicit wellbore and time zone, and reviews five provenance-linked reconciliation proposals. The user accepts each proposal, submits the draft, then switches to a distinct drilling-supervisor principal to approve it. API readback confirms the submitted/approved states and actor separation.
4. A separate Python helper opens the same E2E SQLite database read-only and verifies the stored report identity, approved revision pointer, source document/record IDs, accepted reconciliation rows, typed duration source value/unit, and source/accepted measurement values/units. Free-text summary/activity fields are emitted only as SHA-256 digests; no transcript or path is printed.

The helper accepts only an absolute `sqlite+aiosqlite` URL with no query/fragment and a resolved `.e2e` or `.e2e-*` parent containing `e2e.db`. It resolves symlinks before that check, connects using SQLite `mode=ro`, sets `PRAGMA query_only=ON`, uses parameterized queries and scopes reads by report, well, wellbore, and the report's organization. Database/SQL errors and command arguments are not echoed into the browser output. The focused helper tests verify the hashes-only snapshot, unchanged database digest, read-only write refusal, invalid database paths/URLs, and cross-well/wellbore refusal.

## Verification

- Isolated real-browser run of `daily-reports-workspace.spec.ts` on Chromium: **3 passed, 0 retries**. It ran against fresh `frontend/.e2e-cp136-main-20261020`, `frontend/.e2e-cp136-faults-20261020`, and `frontend/.e2e-cp136-auth-20261020` data directories. The pre-existing `frontend/.e2e` path was not used or reset.
- `npm run e2e:verify-shape` passed and verified the unchanged reviewed inventory of **127 tests in 20 files** (Chromium 120, faults 4, auth 3); the CP136 work strengthens one existing spec and adds no browser case.
- Backend readback + daily-report API/domain selection: **28 passed, 1 PostgreSQL-marked skip**. The skip is an existing database-backend marker; it is not an added skip or a suppressed failure.
- Complete local backend inventory: with embedded PostgreSQL **1,744 passed, 0 skipped** (one non-failing `RuntimeDirWarning` about unset `XDG_RUNTIME_DIR`); default SQLite **1,729 passed, 15 skipped**, all 15 PostgreSQL-marked. The reviewed PostgreSQL node-ID set also matched: **15 tests across 12 files**. The exact-SHA CI and Windows run remain separate requirements.
- Frontend typecheck and lint passed. Frontend unit/component suite: **400 passed in 40 files**. Production build completed; Vite emitted the existing advisory that one minified JavaScript chunk exceeds 500 kB. Full backend/script Ruff, Python compilation and `git diff --check` passed.
- The E2E used a synthetic CSV and development-role identities. The database snapshot was read-only, scoped, and hash-only for free text.

## Limits and acceptance boundary

This establishes a local software/browser path for synthetic intake, source reconciliation, independent review, and database persistence. It is not an exact-SHA CI result, a real operator/customer approval, field-document interpretation evidence, OCR quality acceptance, a vendor/provider response, or production readiness. CP135 remains **not accepted**: deployment-image OCR run `42734158263` and the later local adapter-v5 experiment both failed the unchanged reviewed quality thresholds. No source fact, unit, wellbore, reviewer, receipt, or field outcome is inferred from this CP136 test.