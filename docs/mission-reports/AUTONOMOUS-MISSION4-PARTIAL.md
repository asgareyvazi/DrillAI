# Mission 4 — partial recovery, unresolved acceptance

**Overall: NOT ACCEPTED.** This report records a bounded software change, not completion of the 18-phase mission. Starting branch `arena/9d8f5568-drillai` and remote `2be1f7bffab0fbca0a49f9b9a08c5efceea51814`; main `bfa066b28e0071880cb9191a4f1d47fdaa143e04`. Initial local/remote were equal, main was the merge base, 0 behind / 287 ahead. Full history was available, fetch completed, and 710 index files matched worktree content and executable/symlink modes. There was one worktree, no listed stashes, and historical object `7f13981f35eebc39fb463f84b487fbfa79d0f54e` was not accessible. No reset, clean, forced push or main changes were used. Git timestamps are repository metadata and not independently verified dates.

## Reopened obligations

| Obligation | Status | Evidence or gap |
|---|---|---|
| Full wellbore geometry | PARTIAL | `fluids.py` checks coverage/nesting, but tool joints, washout, eccentricity and other configurations remain unmodeled. |
| Scoped open-bore evidence | PARTIAL | `CirculationWorkspace.tsx` now accepts interval declarations and persists them as engine-run inputs; user-supplied author, timestamp and basis are not authenticated or reviewed source records. |
| MD/TVD datum | PARTIAL | Caller declarations and matching origin required; no verified survey/datum conversion. |
| Circulation path and bottoms-up | PARTIAL | Full nominal-path geometry tests exist; no full governed design lifecycle. |
| Pump consistency | PARTIAL | Engine checks declared pump/flow consistency; equipment measurements and calibration not validated. |
| Capacity design workflow | PARTIAL | Browser/API persisted run and scoped assertion demonstrated, not revision, review, comparison or approved report. |
| Hydraulics/ECD workflow | PARTIAL | Engine 1.6.0 prevents several false known-zero pressure outputs; not a reviewed design or surveyed ECD profile. |
| Survey-to-hydraulics | FAIL | No authenticated survey linkage, datum transformation or surveyed TVD profile. |
| Geometric proximity integration | PARTIAL | Existing `GEOMETRIC_PROXIMITY_ONLY`; no selected persisted survey workflow or report. |
| Anti-collision uncertainty | FAIL | No validated covariance, separation factor, ISCWSA compliance or risk analysis. |
| Deployment OCR | FAIL | Baseline exact-SHA deployment gate fails with unchanged thresholds; no production OCR accuracy fix implemented here. |
| Independent holdout | BLOCKED | No authorized real-world DDR holdout or adjudicated labels. Emulator/synthetic corpus is not one. |
| Historical Windows socket issue | PARTIAL | `HISTORICAL_ROOT_CAUSE_NOT_PROVEN`; no new native reproducer. |
| All-engine independent assurance | PARTIAL | [15-engine inventory](AUTONOMOUS-MISSION4-ENGINE-INVENTORY.md) and existing CP19 vectors; no comprehensive independent audit. |
| Full catalogue | PARTIAL | English/Persian/JSON files remain preliminary and registry-derived, not the requested full source-to-report historical audit or validated schema. |
| Other product modules | PARTIAL | Existing program/procedure, DDR, telemetry, agents and other code remain; no claim that their Mission 4 acceptance journeys were completed. |
| Exact-SHA certification | PARTIAL | Baseline [CI 44989271640](https://github.com/asgareyvazi/DrillAI/actions/runs/44989271640) failed OCR; final SHA needs its own full run. |
| Workspace preservation/closure | PARTIAL | Four prior evidence folders checked; a test/build produced additional ignored artifacts. See workspace disclosure below. |

## Implemented safety improvements

`backend/src/drillai/engines/fluids.py`: `hydraulics.laminar` **1.6.0** returns null for unavailable nozzle metrics instead of zero, requires an explicit nozzle discharge coefficient when TFA is supplied, and will not return total/standpipe pressure without declared additional downhole losses; standpipe also requires declared surface loss. The new losses and optional flow ranges are caller assertions, not independently verified or a calculated pressure-loss curve. `total_pressure_loss_si` excludes surface loss. Missing cuttings diameter or density leaves cuttings metrics unavailable rather than substituting nominal values. There is no field validation, turbulent model, verified survey, surface network, tool-joint model, or uncertainty analysis. Bit diameter inference from a hole and other legacy assumptions remain open for further review.

`backend/src/drillai/drilling/optimisation.py` now applies the swept mud density; rejects unmapped variables and WOB without torque-drag inputs; cannot use a point loss at another flow (or any loss at another mud density); requires SPP constraints even when not selected as an objective; and marks laminar-model-exceedance candidates infeasible. Explicit ranges assert a constant loss or conservative bound over the range; the application cannot prove that assertion. Torque/hookload constraints and full optimisation assurance still require further audit.

`frontend/src/pages/engineering/CirculationWorkspace.tsx` adds bilingual, scoped unoccupied interval input and an explicit unverified-assertion notice; it does not authenticate the asserted person or create an approval. The isolated Chromium `frontend/e2e/checkpoint5.spec.ts` test checks both saved runs, unknown pressure terms and an asserted open interval with analytical cylinder-volume check and API readback. This is a synthetic execution record, not a governed design or DB approval lifecycle. The reviewed 129-test E2E inventory remains unchanged; this extends an existing test.

`scripts/verify_engineering_product_db_readback.py` replaces SQLite Row key comprehensions with `dict(row)` (preserving name-to-value semantics; iteration of Row alone returns values). Full backend-and-scripts Ruff passes. No blanket suppression was added.

## OCR diagnostic (not deployment acceptance)

Baseline deployment Tesseract CLI 5.5.0 / adapter `tesseract-cli-tsv-v8`, 200 DPI, dataset SHA-256 `31aca80b4323fe470d34705e36038296ef620adb7525373f5196c9f561b6735d`. All 64 cases evaluated; global CER 0.108396, WER 0.155069, critical 308/384. Persian CER 0.229478, WER 0.293367, critical 118/192; English 190/192. Critical by layout: compact 88/96, single-column 88/96, table 50/96, two-column 82/96. Every scan-condition stratum 77/96. Required maxima CER 0.15 and WER 0.20, and minimum critical exact 0.90 globally and each stratum, are unchanged. See baseline CI annotations. Full log download returned EOF in the prior session.

A fresh synthetic `tesserocr` CLI emulator rerun (not the deployment executable) evaluated 64/64, failed with CER 0.107024, WER 0.154639 and critical 311/384; Persian 121/192. Diagnostic-only field misses among Persian cases: sample_id 32/32, ROP 10, date 8, mud weight 8, NPT 8, measured depth 5. These counters localize field-match failure, not its proven root cause or a real-world performance estimate. No model, scorer, corpus or threshold was changed. `/tmp/drillai-mission4/ocr-diagnostic.json` is ephemeral and not a durable repository artifact.

## Tests and scope

The local expanded test added separate closed-form Poiseuille/slot and orifice calculations and unavailable-input, explicit range, optimisation and regime regressions in `backend/tests/engines/test_mission4_hydraulics_safety.py`; existing numerical checks were retained and old incorrect zero assertions changed to explicit null checks. A focused pre-final backend run passed 283; a full SQLite run was started before later source changes and cannot certify the final tree. Browser journey passed in isolated Chromium with zero retries after the seed was corrected to a laminar synthetic example. Full frontend run initially failed three tests under concurrent load (two timeouts and a subsequent missing-label failure); a fresh run passed 417 tests in 42 files and built, but later edits require exact-SHA CI. The build warned about a >500 kB chunk. Final full CI and push evidence must be reported separately against the exact SHA.

## Workspace exception

The four prior evidence folders and their five file hashes are tracked in [Mission 3 baseline report](AUTONOMOUS-MISSION3-PARTIAL.md); initial Mission 4 hashes matched. Initial ignored inventory included frontend dependencies/build outputs and caches. Local browser testing additionally created ignored `frontend/.e2e-m4-7a0c/`, `frontend/.e2e-m4-auth-7a0c/`, `frontend/.e2e-m4-fault-7a0c/` and `frontend/.e2e-m4-results-7a0c/` with synthetic databases, fixtures and result metadata. They are retained rather than deleting databases, fixtures or reports contrary to the preservation rule. Build/test tools also overwrote pre-existing ignored build/cache artifacts; the prior build was reconstructed from the initial Git SHA but its original content hashes were not captured. See [workspace disclosure](AUTONOMOUS-MISSION4-WORKSPACE.md). Therefore no clean-workspace or full-preservation claim is made solely from Git status.
