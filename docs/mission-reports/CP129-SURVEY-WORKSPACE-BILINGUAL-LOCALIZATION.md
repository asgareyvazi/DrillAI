# CP129 — Survey workspace bilingual localization

**Status:** `DONE` for the repository implementation and local verification. Exact-SHA GitHub Actions certification is pending the pushed CP129 commit. This report does not claim field validation or engineering acceptance.

## Scope delivered

Localized the survey workspace's user-facing surface in English and Persian (`en.ts` and `fa.ts`): trajectory and wellbore-purpose enums; datum and datum-provenance values; calculation, review, acceptance and comparison states; azimuth frames; known trajectory warnings; station/extraction summaries; form labels, hints and errors; unit choices and table/comparison headers; plot labels and tooltips; version/count formatting; and revision-comparison deltas. Persian remains RTL, with engineering symbols and source identifiers isolated LTR where appropriate.

Unknown or future enum and frame values are rendered with a localized “unrecognized value” label while preserving the exact raw value. Unsupported saved trajectory kind, datum, target-frame, vertical-section-frame and station-frame options remain visible in the editor rather than falling through to a known value. A save that would submit an unsupported frame is rejected until the operator explicitly selects a supported frame or removes the optional conversion. Unknown trajectory kinds no longer display as anti-collision. Known station-source values (`manual`, `plan`, `mwd`, `gyro`, `tie_in`, `interpolated`, and `unknown`) are localized in the results table; user-defined source labels remain unchanged, and editing keeps the underlying source value.

The existing survey calculation, exact measured-depth comparison, source-ID matching, source-document review gate, canonical-unit handling and provenance contract were not changed. CP120 NPT provenance/uncertainty rules and CP121 source-ID matching remain intact. Added UI tests use explicitly synthetic fixtures; they are not field records, DDRs, provider results or engineering evidence.

## Verification

- Full frontend suite: `cd frontend && npm test` — **398 passed across 39 files**.
- Focused survey and catalogue coverage: `npm run test -- src/pages/wells/SurveysWorkspace.test.tsx src/i18n/catalogue.test.ts src/i18n/usage.test.ts` — **15 passed across 3 files**.
- Frontend strict typecheck: `cd frontend && npm run typecheck` — passed.
- Frontend lint: `cd frontend && npm run lint` — passed.
- Production build: `cd frontend && npm run build` — passed (`tsc -b` and Vite). Vite emitted the existing advisory that the minified main chunk exceeds 500 kB; build exit status was zero.
- CP16 maintained-count guard now requires **398 passed** in both maintained summaries; `cd backend && ./.venv/bin/python -m pytest -o addopts= tests/test_cp16_adversarial_audit.py::test_cp16_10_documentation_and_contract_consistency_guard -q` — **1 passed**. Backend test inventory remains **1,718 tests across 128 files**; CP129 adds frontend tests only.
- Exact-SHA Actions for the pushed CP129 commit: pending.

## Limits

All survey data in the new regression tests is synthetic and labeled as such. No field survey, DDR, external provider, operational deployment, qualified engineering review or live trajectory calculation was contacted or validated. UI localization and passing local software tests do not change `needs_field_validation` or establish operational acceptance.
