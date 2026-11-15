# CP142 — Independent Calculation Assurance

**Phase:** CP142 — independent reference checks across calculation families

**Evidence class:** deterministic synthetic software tests; not measured field data

**Date:** 2026-10-20

**Status:** Implementation and focused reference checks are in place. Both full local backend suites passed and their inventories verified. Native-Windows and exact-SHA CI evidence are not yet available.

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
- `README.md` and `docs/FRONTEND_TESTING.md` distinguish manifest expectations from completed test results and preserve the earlier CP141 evidence as historical.

## Verification record

| Check | Result |
| --- | --- |
| Three focused independent-reference tests | Passed: 3/3 (earlier focused run). |
| Ruff and Python compilation for the new module | Passed. |
| CP16 documentation/count consistency guard | Passed. The README presents the CP142 PostgreSQL and SQLite totals as observed local results, and the consistency guard reconciles them with the reviewed inventory. |
| PostgreSQL-enabled complete backend suite | Passed: **1,755 passed, 0 skipped**, in 1,666.75 seconds. Pytest emitted three non-failing warnings. The inventory verifier accepted the verbose pytest summary and confirmed all 15 reviewed PostgreSQL test IDs across 12 files. |
| Default SQLite complete backend suite | Passed: **1,740 passed, 15 skipped**, in 1,377.32 seconds. Both warnings were non-failing JUnit `record_property` compatibility notices. The inventory verifier passed and identified exactly the 15 PostgreSQL-only skips. |
| Exact-SHA Linux/native-Windows certification | Not yet run for the CP142 commit. |

The manifest expects PostgreSQL-enabled Linux 1,755 passed / 0 skipped and default SQLite 1,740 passed / 15 PostgreSQL-only skips; both local outcomes and inventory verifications are observed. The Windows expectation is 1,739 passed / 1 identified POSIX-only skip / 15 PostgreSQL deselections and remains unobserved until native-Windows execution. Exact-SHA CI for this phase and the later final CP151 certification remain outstanding.

## Acceptance boundary

These results can establish software conformance to the cited mathematical expressions for the tested synthetic vectors only. They do not establish qualified engineering review, validated real-world fluid/trajectory inputs, field measurement quality, vendor interoperability, customer acceptance, deployment readiness, or autonomous rig control.
