# CP141 — Explicit Survey Datum and Reference Lifecycle

**Disposition:** repository implementation and local software verification are complete. Exact-final-SHA Linux/native-Windows certification remains a CP151 requirement. Evidence here is synthetic and does not establish field correctness, qualified engineering acceptance, or operational readiness.

## Implementation

The survey workspace now makes the values that define a trajectory's vertical and azimuth frames visible alongside the saved calculation: datum and datum provenance, input and output azimuth reference plus frame identity, canonical KB elevation, vertical-section azimuth/reference/frame identity, and each station's input frame and applied signed grid correction. Technical identifiers are rendered LTR inside bilingual English/Persian UI, and unknown or unsupported values remain visible rather than being silently replaced.

The form rejects a KB elevation supplied with a non-RKB datum and retains a trajectory's datum when correcting or importing a revision. The workflow preserves the distinction between input and target azimuth frames: station references must be known and identified; incompatible station-frame identities require a declared target frame and identity; and true/magnetic-to-grid conversion requires an explicit signed correction for each station. It does not infer missing datum, frame identity, correction, or unit. The API's existing calculation contract and persistence behavior are now covered by an added regression that checks refusal paths and saved/read-back source and canonical values.

A new real-browser journey runs the lifecycle in an isolated synthetic database. It tries an incompatible MSL/KB combination, an unknown input reference, a missing frame identity, and a target-grid conversion without correction and observes refusal. It then enters a valid RKB trajectory with explicitly named true-north inputs, a grid target and frame ID, per-station corrections, and an explicitly identified vertical-section frame. The browser checks the saved trajectory through the product API, including datum provenance, exact target/input frame IDs, zero KB elevation in metres, signed corrections, normalized output azimuths, TVDSS, and the persisted calculation parameters. The existing survey browser cases continue to exercise creation/correction of immutable revisions, comparison/export, and reviewed source-row import.

## Verification

- Focused backend survey API selection: **4 passed**.
- Focused frontend survey workspace selection: **8 passed**; `npm run typecheck` passed.
- Isolated real-browser `surveys-workspace.spec.ts`: **3 passed**. The explicit-frame case verifies both rejected submissions and persisted API readback. It ran in isolated `frontend/.e2e-cp141-*` directories, including successful run `20261114T111130Z-cp141-surveys-full`; the standard `frontend/.e2e` path was not modified.
- Full backend with embedded PostgreSQL: **1,752 passed, 0 skipped**; the only warning was the non-failing `RuntimeDirWarning` for unset `XDG_RUNTIME_DIR`. `scripts/verify_backend_test_inventory.py --platform linux` verified the result and all **15 PostgreSQL node IDs across 12 files**.
- Full backend default SQLite suite: **1,737 passed, 15 skipped** (the 15 reviewed PostgreSQL-only tests); `scripts/verify_backend_test_inventory.py --platform default` passed and verified the same 15 PostgreSQL node IDs across 12 files.
- Full frontend Vitest suite: **402 passed in 40 files**. Frontend lint, typecheck, and production build passed. Vite printed its existing advisory for a minified JavaScript chunk over 500 kB; the build succeeded.
- `npm run e2e:verify-shape` verified **128 browser tests in 20 files** (Chromium 121, faults 4, auth 3). The CP16 documentation/count guard passed (**1 test**), backend Ruff passed for the changed tests, and `git diff --check` passed.

## CI status and limits

The reviewed Windows inventory expectation for this 1,752-case backend suite is **1,736 passed, 1 identified POSIX-only skip, 15 PostgreSQL deselections**; it has not yet been observed for CP141. Exact-SHA Actions run [43587829334](https://github.com/asgareyvazi/DrillAI/actions/runs/43587829334) tested parent CP140 SHA `740114e5de4e8667a50c6404fbe411e0661fa2bc`, not these uncommitted CP141 changes. The native-Windows job passed; the Linux job failed at the Docker Compose deployment smoke gate, and downstream Linux E2E gates were skipped. GitHub log and artifact retrieval returned EOF, so the Docker-gate cause remains unknown. This result is not CP141 certification, and no diagnosis or acceptance is inferred from it. CP151 must certify the final identical Linux/native-Windows SHA with all required gates.

All CP141 browser/API values and identities are synthetic. No field survey, external datum authority, grid definition, vendor/provider response, qualified engineering review, or customer sign-off was used. `well_control.kill_sheet` and `tubulars.api_5c3` remain `NEEDS_FIELD_VALIDATION`; this work grants no autonomous rig-control authority and does not imply operational readiness.
