# CP115 — Survey Workbench implementation evidence

**Status:** `DONE` for repository software implementation. `NEEDS_FIELD_VALIDATION` for field or qualified engineering acceptance.
**Evidence SHA:** integrated branch head `c1716b2e9a86a97a594876d38263510ff0520c7a`; exact-SHA Actions run `42451684890` passed Linux job `128642063559` and native-Windows job `128642063746`.
**Evidence boundary:** synthetic fixtures and software/CI only. No field survey, operational DDR, vendor service, or qualified engineering sign-off is claimed.

## Product route and workflow

The well-scoped route is `/wells/:wellId/surveys`, registered in `frontend/src/App.tsx` and exposed as a well-only item in `frontend/src/components/layout/AppShell.tsx`. `SurveysWorkspace` provides English/Persian forms, source review and survey-series navigation. Its typed contracts are in `frontend/src/api/types.ts` and `frontend/src/api/endpoints.ts`.

The server routes are in `backend/src/drillai/api/routers/surveys.py`; domain validation, source resolution, revision creation, calculation, comparison and CSV serialization are in `backend/src/drillai/drilling/surveys.py`. The supported API operations list, read, create a manual revision, import selected reviewed survey records, compare two revisions, and export a provenance CSV.

## Persisted revisions, source preservation and authorization

A survey revision stores its wellbore, name/kind, version, current-selection state, datum and datum provenance, source document, source-record basis, engine identity, calculation state, inputs/outputs hashes, QC/acceptance state and lineage. A correction creates a new trajectory revision; the prior engineering inputs and stations are not edited. Mutations use `expected_current_version` and an optional base trajectory, and the API supports idempotency keys.

Manual station inputs retain the entered value and unit alongside canonical values and conversion metadata from `drillai.units.registry`. Imported stations retain the source document/record, page/region locator, accepted reviewed payload, original source values/units, extraction method/version, review version and conversion metadata. Inferred units require current explicit human review before calculation. A source record re-reviewed after import makes the dependent trajectory unavailable for silent reuse. Unknown datum, frame, measured time, or unsupported unit stays unknown or fails validation; it is not filled from creation time or a guessed reference.

The routes require the survey read/write/import/export actions, call server-side `authorize`, and scope access through the service to the well, wellbore and source document. Survey calculation separately requires the `engine.run` action for `trajectory.minimum_curvature`. Read-only viewers can read/compare but cannot create or import.

## Calculation, QC, comparison and export

The workbench calculates with the registered `trajectory.minimum_curvature` engine and persists its inputs/outputs hashes and result. The API returns station coordinates, measured-depth/true-vertical-depth and east/north displacement, calculation state, quality flags/warnings and acceptance status. The UI renders plan-view and MD/TVD charts, station tables, QC warnings and revision history; supported QC limits report violations without hiding the calculated values.

Comparison uses exact canonical measured-depth equality and explicitly reports matched/added/removed stations and deltas; it does not interpolate unmatched stations. CSV export preserves source-recorded and canonical values, units, references, provenance, revision and calculation metadata. The route requires `survey.export`, returns private/no-store and `nosniff` headers, and includes a SHA-256 ETag for the exact export bytes.

## Automated evidence

- `backend/tests/api/test_surveys_api.py` covers immutable manual revisions, compare-and-swap/idempotent replay, stale-write denial, source-unit preservation, reviewed-record import, source re-review invalidation, export provenance and read-only viewer permissions.
- `backend/tests/engines/test_trajectory.py` contains formula, reference-frame, datum, QC, antipodal, bounds and independent multi-axis reference checks. The independent synthetic calculation is software evidence, not field calibration.
- `frontend/src/pages/wells/SurveysWorkspace.test.tsx` covers the bilingual form, catalogue values, revision/source states, chart presentation and unknown metadata handling.
- `frontend/e2e/surveys-workspace.spec.ts` contains two real-browser journeys: manual create/correct/compare/chart/export; and source upload, unit-review gating, extracted-record review, import and persisted provenance readback.
- At SHA `c1716b2e9a86a97a594876d38263510ff0520c7a`, exact-SHA run `42451684890` passed all required Linux and native-Windows workflow steps. The reviewed inventory passed (Linux 1,720 backend tests; Windows 1,705 passed, one identified POSIX-only skip and 14 PostgreSQL deselections); frontend suites were 399 tests across 40 files on each platform; the complete real-browser suite was 126 tests with zero retries on each platform. These results certify software behavior on that SHA, not field correctness.

## Limits

All report/sample inputs in this implementation evidence are synthetic. No trajectory data has been independently checked against a field survey or qualified reference. The workbench does not upgrade engineering acceptance. `well_control.kill_sheet` and `tubulars.api_5c3` remain `needs_field_validation`; no autonomous rig control is present or implied. Deployment, live-vendor and production acceptance remain separate evidence requirements.
