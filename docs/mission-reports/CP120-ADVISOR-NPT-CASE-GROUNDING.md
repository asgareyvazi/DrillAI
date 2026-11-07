# CP120 — Advisor NPT Basis and Case-Level Grounding

## Disposition

| Item | Status | Evidence / boundary |
| --- | --- | --- |
| Preserve NPT selected/alternate basis in Advisor | **DONE (software)** | The aggregate fact identifies the chosen `events` or `operations` basis and carries both totals, matching count, denominator, scope, and service caveats. An alternate-basis total is labeled separately and never blended into the selected total. |
| Attach bounded case provenance | **DONE (software)** | Up to ten case facts carry row ID, evidence reference when present, category/code/classification source, hours/time basis, depth/section/operation context, controllability and confidence. Aggregate metrics remain from the full service population. |
| Abstain on absent or mismatched evidence | **DONE (software)** | No records produce an explicit unknown (not “zero downtime”); a selected zero total explicitly does not establish absence of physical downtime, and bounded case lists produce explicit caveats. |
| Physical downtime or field attribution | **NOT ESTABLISHED** | Synthetic fixtures validate serialization and basis handling only. Event records are not independent observation or qualified NPT attribution. |

## Implementation

`OperationsAdvisor.ask` now translates the `NptSummary` contract into separate structured facts:

- `npt.total` identifies the selected basis, its hours and number of matching cases; provenance retains both event and operation totals, denominator/productive hours, time/scope, returned case count and truncation metadata.
- Each displayed `npt.case.*` fact identifies the actual event/operation source row, preserves evidence and document links when supplied, and carries the exact classification, time, depth, confidence and hours bases. Untrusted/unknown times remain fields with their basis; the prose does not assert them as event times.
- At most ten case facts are included. The unknown/caveat section says when the Advisor or NPT service bounded the case list. It does not imply that the listed cases are the entire ledger.
- Event and operation roll-ups remain independent to prevent double counting. Whenever their totals differ, including when the alternate total is zero, the answer labels that basis and its total. A zero selected total is accompanied by an explicit unknown that it does not establish absence of physical downtime; if neither basis has any record, the separate no-record unknown states the same boundary.

## Local verification

- The CP120 Advisor/API/state-freshness/telemetry-latest selection passed **64 tests**, including the synthetic NPT case-cap/aggregate test and the API Advisor flow that promotes two synthetic DDR NPT events and checks their document/source lineage.
- The direct Advisor/API portion contains **33 tests**. The separate CP118 RAG/Advisor selection remains **70 passed**.
- Backend Ruff and `compileall` passed. Frontend typecheck/lint and **20 tests across four engineering workspace files** passed; the Advisor component test verifies that a numeric NPT value and its source-backed case statement render together.
- Backend inventory increased by one test to **1,710 collected across 128 files**. Inventory arithmetic is 1,710 Linux, 1,696 default plus 14 PostgreSQL skips, and 1,695 Windows plus one identified POSIX-only skip and 14 PostgreSQL deselections. The PostgreSQL node-ID manifest still contains 14 tests across 11 files.

## Limits

NPT totals are sums of recorded rows on an explicitly selected basis, not a measurement of all physical downtime. Classification, controllability, duration, timestamp and depth remain as source-reported or unknown. The synthetic Advisor fixture and hand-authored benchmark are not field evidence, operator acceptance, engineering qualification or live-provider evaluation. Full backend suite, deployment verification and exact-final-SHA Linux/native-Windows certification remain outstanding for CP132.
