# CP149 — Bilingual, Accessible, State-Reflective Notification UI

**Date:** 2026-10-20

**Status:** The notification UI exposes recipient-receipt status separately from outbox dispatch state; Persian RTL/LTR handling and accessible manual-retry interactions are covered by component tests. The stateful UI test verifies a fresh API readback after a successful retry response. It is not a live database or browser test; the integrated real-browser/API/database pilot remains CP150.

**Evidence class:** Local component tests using synthetic notification records and a stateful API test double. The authenticated API and DB readback exercised in CP148 used an isolated local test database. No provider or recipient was contacted.

## UI and interaction contract

- The outbox dispatch status and `recipient_delivery_status` are separate columns. English and Persian strings identify the receipt state as unknown without implying delivery from transport acceptance or a legacy `sent` row.
- Persian document direction is RTL, while message IDs, channels, timestamps, and provider identifiers remain explicitly LTR to preserve the direction and readability of technical values.
- The manual retry form is a named accessible region. The audit-reason textarea and duplicate-risk acknowledgement have associated labels; the confirmation button is disabled until the reason has the required length and the acknowledgement is checked. The native review button is keyboard-activated with Enter.
- The component does not claim success by locally changing a status optimistically. A stateful API test double updates its outbox read model only when the retry API call succeeds; query invalidation refetches that model, and the UI then shows `queued` while receipt remains `unknown`.

## Verification observed

- Latest fresh full local Vitest suite: **406 passed across 40 files** in 53.78 seconds, with zero skips or failures (another complete run also passed 406/40 in 58.75 seconds).
- Focused `PlatformPage.test.tsx`: **4 passed**, including the Persian RTL/LTR, keyboard, accessible form, receipt-separation, and post-success API-readback assertions.
- Frontend typecheck, targeted ESLint and E2E shape verification passed. E2E inventory shape is **128 tests across 20 files**; this is shape verification, not the default full browser-suite execution.
- A prior full frontend run had 405 passes and one 5-second timeout in the DailyReportsWorkspace manual-draft test. The exact test passed in a focused 1.2-second run, and a fresh full run has now passed without changing the timeout, adding retries, or weakening assertions; the original timeout remains historical evidence.
- The component readback uses a test double, not a database. CP148 separately verifies actual authenticated API state and database/audit readback; CP150 still requires a real-browser pilot against product APIs with independent database readback.

## Limits

This phase makes no claim of screen-reader certification, formal WCAG conformance, visual RTL review by Persian users, recipient receipt processing, delivery, provider acceptance, or customer acceptance. UI translations and local component evidence do not provide operational readiness or field evidence. Final Linux/native-Windows certification on one exact final SHA remains CP151 work.
