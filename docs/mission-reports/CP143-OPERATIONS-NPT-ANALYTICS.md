# CP143 — Operations and NPT Analytics

**Date:** 2026-10-20

**Status:** The operations-basis KPI change, focused checks, default-SQLite full backend suite, inventory verification, and CP16 documentation/count guard passed locally. Exact-SHA CI, PostgreSQL-enabled CP143 full-suite evidence, and field/business acceptance remain separate.

**Evidence class:** deterministic software tests with synthetic rows. No operator, field, customer, or external-provider records were used.

## Metric and source contract

The NPT API keeps event and operation totals separate. This phase makes the `basis=operations` numerator and evidence cases actual-only: only tenant- and well-scoped `operation_class=actual` rows with a positive recorded `npt_hours` value contribute. Planned and forecast rows remain planning data and cannot inflate a historical operations-basis loss KPI or its case count. The API still exposes both source totals so the caller can reconcile them; it never adds the two bases together.

`productive_hours` comes from recorded `actual_duration_hours` on actual, productive operation rows under the same well/section/operation/time scope. The reported denominator is that value plus the explicitly selected NPT numerator; the percentage is null at a zero denominator. It is a scoped fraction of recorded productive-plus-selected-NPT hours, not elapsed calendar time, and does not claim that the source rows exhaust physical downtime.

When a time window is requested, operation-basis NPT rows are placed using actual start/end values only when their corresponding time-provenance bases are accepted for event-time filtering. An actual row with only planned timestamps and unknown actual time does not match a narrowed window. In an unwindowed summary the row's recorded NPT amount remains visible, while missing actual timestamps remain null and their stored basis remains available in case provenance. Matching interval rows contribute their full recorded hours on overlap; the API does not prorate partial overlaps.

The well cockpit labels the operations figure as an actual-operation roll-up and states in English and Persian that planned/forecast rows are excluded. The API metric contract and `docs/OPERATIONS-DASHBOARD-METRICS.md` describe the same population and time policy.

## Verification

- Backend NPT summary and API/accounting selection: **17 passed** (`tests/drilling/test_npt_summary.py` and `tests/operations/test_npt_accounting.py`). The synthetic regressions cover actual-only numerator and case-count arithmetic, exclusion of plan/forecast values, denominator consistency, and refusal to use planned timestamps for an actual row with unknown actual time.
- Ruff passed for the changed NPT implementation and summary tests.
- Well cockpit component suite: **10 passed**, including English and Persian actual-only labels. Frontend typecheck and lint passed.
- Backend collection: **1,757 tests across 135 files**. Full default-SQLite backend suite: **1,742 passed, 15 skipped, 2 warnings in 704.39 seconds**. The inventory verifier passed and confirmed the 15 reviewed PostgreSQL test IDs across 12 files. The 15 skips are the reviewed PostgreSQL-only tests.
- An initial quiet-mode full-suite invocation completed pytest but its inventory verifier rejected the log because it contained no parseable final summary with duration. The suite was rerun in verbose mode; the result above is the completed run accepted by the verifier. This is a verifier/log-format issue, not a test failure.
- CP16 documentation/count consistency guard passed after the inventory and documentation updates.
- No PostgreSQL-enabled full CP143 suite or native-Windows CP143 execution is claimed here. The exact-SHA CI and final cross-platform certification remain pending for the then-current SHA and CP151's final SHA.

## Limits and disposition

This change does not classify a source row as physically correct, promote a plan to actual, establish that all downtime was captured, or make operational recommendations. A missing actual timestamp does not become a planned timestamp, report date, ingest time, or inferred time. Planned/forecast NPT estimates remain available as plan data outside the historical operations-basis KPI. Full-hour overlap behavior remains explicit and unprorated. No field validation, qualified engineering approval, production RPO/RTO, customer acceptance, vendor response, or operational-readiness claim is made. Keep `well_control.kill_sheet` and `tubulars.api_5c3` at `NEEDS_FIELD_VALIDATION`; no autonomous rig control is authorized or implied. The independent CP151 final assurance still requires all acceptance gates on one identical final SHA, including Linux and native Windows.
