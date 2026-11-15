# CP142 — Independent Calculation Assurance

**Phase:** CP142 — independent reference checks across calculation families

**Evidence class:** deterministic synthetic software tests; not measured field data

**Date:** 2026-10-20

**Status:** Implementation and focused reference checks are in place. Both full local backend suites passed and their inventories verified. Exact-SHA Actions run `43687721588` failed its Linux OCR gate and Windows Playwright gate; CP142 is not cross-platform certified.

## Scope

Add independent mathematical references for three calculation families without modifying the engineering engines or supplying plausible defaults:

| Family | Independent reference | Synthetic vector and asserted output |
| --- | --- | --- |
| Trajectory displacement | Closed-form integral of a great-circle interpolation of unit direction vectors, `L tan(Δ/2) / Δ · (u₁ + u₂)` | One 240 m interval from `(I, A) = (18°, 27°)` to `(63°, 119°)`, with explicit grid-frame identity; independently checked TVD, northing and easting. |
| Laminar pipe hydraulics | Newtonian zero-yield-stress limit, Hagen–Poiseuille `Δp = 128 μ L Q / (π D⁴)` | Synthetic SI length, diameter, flow and viscosity; engine string pressure loss compared to the separate closed-form calculation. |
| Drilling mechanical specific energy | Teale work decomposition: axial `WOB / A` plus rotary `2π(N/60)T / (A(ROP/3600))` | Synthetic SI force, torque, RPM, rate of penetration and bit diameter; both components and total asserted independently. With no confined compressive strength supplied, the dysfunction state must remain `unknown`. |

All vectors state their units in the engine contract and use synthetic values only. The checks do not compare against field data, a vendor, or a qualified engineering reference implementation. They do not establish fitness for operational decisions; `well_control.kill_sheet` and `tubulars.api_5c3` remain `NEEDS_FIELD_VALIDATION`.

## Implementation

- `backend/tests/engines/test_cp142_independent_calculation_assurance.py` contains the three independent-reference tests.
- `ci/test-inventory.json` reviews the three additional collected test IDs (1,755 collected tests across 135 files). Local `pytest --collect-only` confirmed that collection count.
- `scripts/verify_backend_test_inventory.py` now parses both quiet and standard banner-wrapped verbose pytest summaries without changing the required duration, exit-code, or exact-count checks. The existing CP32 CI-contract regression tests both output shapes.
- `README.md` and `docs/FRONTEND_TESTING.md` distinguish CP142 observed results, the current manifest expectations, and the separate CP141 history.

## Verification record

| Check | Result |
| --- | --- |
| Three focused independent-reference tests | Passed: 3/3 (earlier focused run). |
| Ruff and Python compilation for the new module | Passed. |
| CP16 documentation/count consistency guard | Passed. The README presents the CP142 PostgreSQL and SQLite totals as observed local results, and the consistency guard reconciles them with the reviewed inventory. |
| PostgreSQL-enabled complete backend suite | Passed: **1,755 passed, 0 skipped**, in 1,666.75 seconds. Pytest emitted three non-failing warnings. The inventory verifier accepted the verbose pytest summary and confirmed all 15 reviewed PostgreSQL test IDs across 12 files. |
| Default SQLite complete backend suite | Passed: **1,740 passed, 15 skipped**, in 1,377.32 seconds. Both warnings were non-failing JUnit `record_property` compatibility notices. The inventory verifier passed and identified exactly the 15 PostgreSQL-only skips. |
| Exact-SHA Actions run `43687721588` for `f0eaac4cd25fd7089333deec6aa133f4042b037f` | Failed overall. Linux backend/inventory and Windows backend/inventory completed successfully, but Linux deployment-image OCR and Windows Playwright failed; details and evidence boundaries follow below. |

The CP142 local manifest expected PostgreSQL-enabled Linux 1,755 passed / 0 skipped and default SQLite 1,740 passed / 15 PostgreSQL-only skips; both local outcomes and inventory verifications were observed. Exact-SHA run `43687721588` accepted the Linux inventory at 1,755 passed / 0 skipped and Windows inventory at 1,739 passed / 1 identified POSIX-only skip / 15 PostgreSQL deselections (1,755 collected); Windows also reported 402 frontend tests. The run nevertheless failed two independent acceptance gates:

- Linux's 64-case deployment-image synthetic OCR gate reported global CER **0.325776**, WER **0.477663**, and critical-field exact rate **0.471354**; thresholds did not pass. In the Persian stratum (32 cases), CER was **0.651797**, WER **0.893707**, and critical-field exact rate **0/192**. These OCR outcomes are not calculation evidence and are not accepted. The output explicitly identified the synthetic benchmark threshold failure.
- Windows Playwright failed at `cp131-synthetic-pilot.spec.ts:305` on an unprovoked `GET http://127.0.0.1:5199/@react-refresh` transport error `net::ERR_NO_BUFFER_SPACE`. The cause is unknown. GitHub's full log retrieval returned EOF; no retry or allowance was used.

The CP142 exact-SHA run therefore does not certify the repository cross-platform. This run's results do not change the separate CP135 benchmark failure record. The CP151 requirement remains a fresh Linux/native-Windows run on one identical final SHA with every acceptance gate passing.

## Acceptance boundary

These results can establish software conformance to the cited mathematical expressions for the tested synthetic vectors only. They do not establish qualified engineering review, validated real-world fluid/trajectory inputs, field measurement quality, vendor interoperability, customer acceptance, deployment readiness, or autonomous rig control.
