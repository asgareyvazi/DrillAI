# Autonomous Mission 5 — partial, not accepted

**Overall: NOT ACCEPTED.** This is a deliberately limited increment, not completion of the master mission. [Baseline and safe reconciliation](AUTONOMOUS-MISSION5-BASELINE.md), [starting workspace manifest](AUTONOMOUS-MISSION5-WORKSPACE-START.jsonl), and [previous engine inventory](AUTONOMOUS-MISSION4-ENGINE-INVENTORY.md) preserve the evidence and limitations. No unverified engineering facts or acceptance have been introduced.

## Implemented increment

The OCR provider's shared deadline previously started **after** synchronous image layout detection. It now starts before that analysis, so when analysis exhausts the budget the existing subprocess guard refuses to launch Tesseract. Both recognition passes continue sharing the same deadline and semaphore. Layout analysis itself remains synchronous and cannot be interrupted, so this is **not** a strict end-to-end wall-clock limit. Adapter provenance is now `tesseract-cli-tsv-v9`; the deployment verification script requires the new version. Its dataset, scorer, critical-field tests, thresholds and stratification are unchanged. The existing routing test now also simulates a slow detector and asserts no subprocess starts, preserving the reviewed test counts. **IMPLEMENTED**, not an OCR accuracy improvement or gate recovery.

In the isolated `/tmp` virtual environment, all 166 tests in `backend/tests/ingestion`, `backend/tests/engines/test_mission4_hydraulics_safety.py`, and `backend/tests/engines/test_proximity.py` passed, including the new timeout assertion; an earlier focused OCR subset passed 50. Ruff on the three changed Python files and `git diff --check` passed. This uses mocks and synthetic tests, not the deployment executable or a production benchmark. No frontend/E2E, Windows, real PostgreSQL, deployed CLI, real-document holdout, or field test was performed locally in this increment.

## Acceptance and remaining work

| Area | Status | Evidence / remaining work |
|---|---|---|
| Persian deployed OCR | NOT_ACCEPTED | Mission 4 exact-SHA deployment failed; no recognition-accuracy recovery was made. CP135 baseline overall CER 0.108396, WER 0.155069, critical 308/384; Persian CER 0.229478, WER 0.293367, critical 118/192. Table 50/96 critical. All 64 evaluated. Required ≤0.15 CER, ≤0.20 WER and ≥0.90 exact critical globally and every stratum. New SHA must be tested independently. |
| Authentic OCR holdout | BLOCKED_BY_MISSING_AUTHORIZED_EVIDENCE | Existing benchmark validates manifests and approval self-attestations but no authorized, independently labeled authentic DDR holdout was supplied. Synthetic and emulator results are not a holdout. |
| Capacity/circulation | PARTIAL | Existing scoped declarations and persisted synthetic engine run; governed revisions, review, comparison and report journey remain. |
| Hydraulics/ECD and rheology | PARTIAL | Independent Newtonian analytical checks cover selected formulas only; no integrated authenticated survey-derived TVD profile or genuine new reference-backed rheology model. |
| Survey proximity and uncertainty | PARTIAL | Geometric segment proximity only; no persisted survey-to-report integration, validated covariance or defensible anti-collision uncertainty model. |
| 15 existing engines | PARTIAL | Mission 4 inventory lists all 15, versions, tests and limits; full independent source-to-report audit absent. `well_control.kill_sheet` and `tubulars.api_5c3` remain NEEDS_FIELD_VALIDATION. Selected hydraulics reference checks are not validation of the full model. |
| DDR/manual/batch/NPT | PARTIAL | Existing code; new comprehensive journey and assurance not completed. No confidential real data used. |
| Well Design and Programs/Procedures | PARTIAL | Existing revisioned product capabilities; requested full design and procedure journeys not completed. |
| Advisor, agents, telemetry, RBAC, reporting | PARTIAL | Existing components; comprehensive new tests and real UI→API→DB→report journeys not completed. |
| English/Persian/JSON catalogue | PARTIAL | Existing registry-derived catalogue is not the requested comprehensive historical/current/planned audit. |
| New engineering workflow | PLANNED | No complete new workflow delivered. |
| Windows diagnostic | PARTIAL | Historical socket root cause remains unproven; prior passing Windows runs do not establish causation. |
| Final-SHA CI | PARTIAL | See GitHub Actions for this commit's actual result. Mission 4 run [44993533095](https://github.com/asgareyvazi/DrillAI/actions/runs/44993533095) is historical and cannot certify this commit. Existing 129 E2E inventory was not changed. |

No real-field, vendor, customer, anti-collision or autonomous approval claim is made. Remaining priorities are deployed OCR source diagnosis and unchanged-gate verification; an authorized independent holdout; governed revision/report journeys; credible reference-backed models and uncertainty; independent engine assurance; complete catalogue and cross-platform exact-SHA CI.
