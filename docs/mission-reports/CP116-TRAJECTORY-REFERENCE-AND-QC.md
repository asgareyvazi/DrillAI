# CP116 — Trajectory Formula and Survey-QC Regression Evidence

**Status:** DONE for repository software tests; `NEEDS_FIELD_VALIDATION` for qualified field acceptance.
**Branch:** `arena/9d8f5568-drillai`
**Evidence class:** local synthetic mathematical tests only. No vendor, confidential DDR, field-survey, or engineering-signoff data was used.

## Change

The `trajectory.minimum_curvature` engine now publishes version `1.3.0`. It rejects an interval whose endpoint survey directions are antipodal (or numerically indistinguishable from antipodal) instead of evaluating the singular ratio factor and returning a misleading displacement. The error states that the arc is undefined without an intermediate station.

`backend/tests/engines/test_trajectory.py` retains the analytical build-and-hold checks and adds a multi-axis synthetic reference vector. The independent reference numerically integrates the great-circle unit tangent with Simpson quadrature in positive-down TVD, north, and east axes; it does not call or reproduce the production ratio-factor loop. Hard-coded expected station coordinates, dogleg-severity values, closure distance, and closure azimuth are checked against that independent calculation and the engine result.

QC coverage now also asserts:

- high-DLS and large-inclination-change flags and warnings without hiding the computed coordinates;
- plan-limit violations with the actual, limit, severity, and unit; and
- strict threshold behavior at a 10-degree inclination jump and 15 deg/30 m DLS.

## Local verification

- `backend/.venv/bin/python -m ruff check backend/src/drillai/engines/trajectory.py backend/tests/engines/test_trajectory.py` — passed.
- `backend/.venv/bin/python -m pytest backend/tests/engines/test_trajectory.py -q` — **23 passed**.
- At the CP116 checkpoint, exact-final-SHA Linux/native-Windows certification remained outstanding. The later integrated CP130–CP132 SHA passed exact-SHA run `42481107515` on both platforms; that does not retroactively certify the CP116-only SHA. The 23 tests are software reference checks, not field calibration or an independent engineering qualification.
