# CP119 — Operations Advisor Provenance and Unknown-State Contract

## Disposition

| Item | Status | Evidence / boundary |
| --- | --- | --- |
| Preserve field provenance in structured Advisor facts | **DONE (software)** | Facts now include a `provenance` object alongside their source, source ID, evidence reference, value and unit. Measured fields retain available observation/receipt timestamps, timestamp basis, quality, freshness, age and source note. |
| Distinguish terminal operation status from actual end time | **DONE (software)** | A non-active latest operation is not presented as an active operation. A `completed`/`cancelled` row without `actual_end` yields an explicit unknown; status does not establish `ended_at` and no `created_at` substitution is made. |
| Show fact provenance in Advisor UI | **DONE (software)** | Source ID, evidence reference and expandable provenance are rendered in English and Persian. |
| Live model, field truth and operational acceptance | **NOT ESTABLISHED** | No live provider, approved operational corpus, rig data, engineering approval or field evaluation was used. The answer remains a recorded-state summary. |

## Implementation

`backend/src/drillai/drilling/advisor.py` serializes the provenance already carried by `WellStateService` rather than reducing measured values to a statement and unit. The telemetry latest-reading response now includes internal point ID, external source-point ID, source value/unit, and canonical value/unit; the Advisor fact carries the channel ID, both point identities, source fields, and a conversion descriptor with dimension and whether source/canonical units differ. Extracted-record facts remain explicitly `extracted_source` with their original unit and no claimed conversion. All measured facts retain `observed_at: null` and `observed_at_basis: "unknown"` as unknown when that is what the source state says; no unavailable conversion metadata is fabricated. Measured depth facts identify the measured source field and explicitly state that planned depth was not used. Operation facts retain actual start/end and duration bases, status and data-quality provenance; previous-operation duration variance is labelled as the recorded subtraction of actual minus planned duration with both input bases.

For operations whose status is terminal or otherwise non-active, the answer labels the latest operation with its recorded status and does not call it the active operation. When a completed/cancelled row has no `actual_end`, the `unknown` section states that the lifecycle status is not an occurrence time and asks for an explicit `source_record` or `operator_entered` basis. It never substitutes operation creation time. Existing current-operation discovery still uses provenance-qualified actual starts.

`frontend/src/pages/engineering/AdvisorWorkspace.tsx` now shows the source ID and evidence reference and exposes the provenance object in an LTR JSON detail panel. Labels are present in both English and Persian. The Advisor fact TypeScript contract describes the added provenance.

## Local verification

- The backend Advisor/API/state-freshness/telemetry-latest selection passed **63 tests**. The direct Advisor/API portion (`test_advisor.py` plus `test_drilling_api.py`) passed **32 tests**.
- The Advisor unit suite includes synthetic cases for a human-validated source parameter with unknown observation time and for linked completed operations with no end timestamps. It asserts preserved source unit, source/evidence identities, unknown freshness/time, and no `created_at` substitution. The API integration also verifies a synthetic telemetry point's raw `klbf` value, canonical `N` value, internal/external point identities, source/receipt times, and conversion descriptor.
- `frontend/npm run typecheck`: passed.
- `frontend/npm run lint`: passed.
- `frontend/npm test -- src/pages/engineering/AdvisorWorkspace.test.tsx`: **6 passed**.
- Backend Ruff and `compileall` for the changed Advisor module/tests passed.
- Reviewed inventory increased by two Advisor tests to **1,709 collected across 128 files**. Expectations are 1,709 Linux, 1,695 default with 14 PostgreSQL skips, and 1,694 Windows with one identified POSIX-only skip plus 14 PostgreSQL deselections.

## Limits

The synthetic fixture verifies serialization and abstention mechanics, not truth of any real drilling record. The source-state layer's own value/canonicalization contract remains authoritative; no source unit is silently converted here. A source quality label is not an independent validation. Echo or another deterministic provider is not live AI evidence, and exact-copy narration is not semantic entailment. No external provider request or field decision was made. Full-suite and exact-final-SHA Linux/native-Windows certification remain CP132 work.
