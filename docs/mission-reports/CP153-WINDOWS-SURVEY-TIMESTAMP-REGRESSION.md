# CP153 — Native-Windows survey timestamp provenance

**Status: `PARTIAL` — regression is instrumented and the unchanged test passes on Linux; native-Windows root cause/fix/rerun are not established.**

## Evidence boundary

CP152 recorded native-Windows job `133112693587` in exact-SHA Actions run `43811089178` on SHA `3a556c441b086666bfadc1651a6afb7d953f1dcc`. The failed node was `tests/operations/test_ddr_idempotency.py::test_report_period_is_not_promoted_as_a_survey_station_measurement_time`. The expected trajectory-station count was 2 and the reported actual count was 0. Available annotation/API metadata did not include the failed records or processor rejection reason; raw log/artifact retrieval had returned `EOF`.

No Windows-specific cause is inferred from that result. The source path accepts an explicit timezone-aware station timestamp, persists draft `observed_at` as record-specific provenance, and sets promoted station `measured_at` only from that record value. The document `period_start` remains document-level metadata and must never fill a missing station time.

## Changes in this phase

- The regression assertion now attaches the actual processing report, extracted survey record metadata/payload, and count of persisted stations to the assertion failure. This is diagnostic-only: expected count remains 2; the first timestamp remains `2026-03-15T10:00:00Z`; the second remains `None`; provenance remains `source_record` / `unknown`; and neither may become the report period `2026-03-10T00:00:00Z`.
- The focused node passed locally on Linux after instrumentation: **1 passed**. The full `tests/operations/test_ddr_idempotency.py` file had previously passed **40/40** on Linux before the latest diagnostic-only edit; it must be rerun after that edit for fresh evidence.
- A read-only parser/extractor probe had also shown equivalent LF and CRLF CSVs produce two survey drafts with explicit timezone normalization and a missing station timestamp of `None`. This is not a native-Windows database/promotion reproduction.
- A bounded `CP153_SURVEY_PROMOTION_DIAGNOSTIC` is now attached only to this regression's failed assertion. It reports allow-listed counts, extracted record validation/confidence/timestamp basis, selected survey fields, and promotion reasons without document/record IDs or arbitrary payload text. The backend inventory verifier exposes only those allow-listed fields in its failure annotation. A security-contract test verifies private/unreviewed keys are dropped. On Linux, Ruff, compile checks, that verifier-contract module, and the focused regression passed in one selection; this verifies the diagnostic boundary, not Windows behavior or a source fix. No test was added to the collected inventory.

## Not done / next verification

- No identified root cause, source fix, or native-Windows rerun is claimed yet.
- The new failure diagnostic must be observed in an exact-SHA native-Windows run. If it reports records present but zero promotions, inspect the `not_promoted` reason/validation state; if records are absent, inspect parser/extractor output and the Windows-created document/job details. Preserve the expected timestamps and no-period-fallback rule while repairing the cause.
- CP166 must rerun the full backend and the same accepted SHA on Linux and native Windows. This report alone does not close CP153.

No operational timestamps, station values, units, or source references were invented. No field/customer acceptance is implied.
