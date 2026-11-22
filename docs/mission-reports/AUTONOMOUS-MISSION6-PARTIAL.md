# Mission 6 — partial implementation, NOT ACCEPTED

## Executive summary

The requested mission has **not** been completed. This increment adds a narrowly scoped, reference-backed laminar power-law **circular pipe** engine through the existing registry/API/persisted EngineRun path, and prevents a partial Tesseract input write from being reported as successful OCR. Neither is a complete engineering workflow or an OCR accuracy recovery. There is no field or vendor acceptance. The preliminary bilingual/JSON catalogue is regenerated for the new engine but is not comprehensive. Baseline, refs and protected files: [baseline](AUTONOMOUS-MISSION6-BASELINE.md) and [starting manifest](AUTONOMOUS-MISSION6-WORKSPACE-START.jsonl).

## OCR investigation — NOT_ACCEPTED

The frozen dataset SHA remains `31aca80b4323fe470d34705e36038296ef620adb7525373f5196c9f561b6735d`. The prior exact-deployment [Mission 5 run](https://github.com/asgareyvazi/DrillAI/actions/runs/44999172097) evaluated 64/64: global CER 0.1083959490, WER 0.1550687285, critical 308/384; Persian CER 0.2294783946, WER 0.2933673469, critical 118/192; table 50/96, two-column 82/96. Requirements remain ≤0.15 CER, ≤0.20 WER and ≥0.90 critical globally and in every stratum. It is not certification of this tree. The local sandbox lacks Docker and deployed Tesseract CLI; the authoritative new deployment evaluation must run in CI.

A local development-only, **not deployed CLI** investigation used tesserocr 2.10.0 / native Tesseract 5.5.1 with the same English (`7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2`) and Persian (`db1c0a91208aff00d3cf1ed2c1d23f76419afd5f024688b4f71adc3f2ce4a505`) model hashes. The existing synthetic emulator, product adapter and unchanged scorer evaluated 64/64 and failed: CER 0.1070238484, WER 0.1546391753, critical 311/384; Persian CER 0.2272409453, WER 0.2967687075, critical 121/192. English critical was 190/192, table 49/96 and two-column 86/96. This is not an independent holdout or production deployment result.

A second synthetic-only direct-library probe rendered all 64 PDFs and captured `/tmp/drillai-mission6/ocr-cases/<case>/` with PDF, image, `eng+fas`/`fas` TSV, line boxes, expected values, scoring booleans and metrics. These are **ephemeral outside-checkout artifacts**, not durable field evidence. The probe chooses known fixture language for its baseline rather than production routing; its results are exploratory, not the benchmark result. For all 32 Persian pages, the Persian-only pass produced the expected sample-ID label but scored 0/32 sample IDs. The combined pass also scored 0/32; in 26/32 its value became exactly the synthetic ID only after removing a directional mark, whereas the Persian-only result still matched 0/32 after such removal. Thus label detection 190/192 and identifier mismatch 32/32 are distinct observations: Persian-only recognition of Latin identifiers and bidirectional formatting in combined output are two visible contributing mechanisms, not a demonstrated complete deployment root cause. Table lines also exhibit value/unit order and recognition errors; no conclusion that controls alone explain table failure is justified.

A synthetic-only line-fusion experiment using overlapping line boxes, matching labels and a hyphenated Latin token improved its exploratory aggregate from 311/384 to 337/384, but table remained 57/96 and Persian CER/WER remained 0.186/0.265. It was not integrated: it fails the unchanged required strata and would select between conflicting recognitions without sufficient real-document validation/provenance. Other requested alternatives, exhaustive causal classifications and deployed-image experiments remain incomplete. No ground truth, scorer, corpus, stratification, gates or thresholds were changed. No private documents were used.

The production fix in `backend/src/drillai/ingestion/ocr.py` records a failed stdin write and refuses zero-exit child output after an incomplete image transfer, with a sanitized error; adapter version is `tesseract-cli-tsv-v10`. The existing bounded output and failure checks remain. `backend/tests/ingestion/test_ocr.py` supplies a deterministic zero-exit/broken-input regression. This is a data-integrity fix, **not** recognition-accuracy improvement. Layout detection remains synchronous and non-interruptible; PDF rendering and encoding precede the provider deadline. No strict end-to-end wall-clock bound is claimed.

## New power-law pipe engine — PARTIAL

`backend/src/drillai/engines/power_law.py`, registered as `hydraulics.power_law_pipe` 1.0.0, implements steady fully-developed laminar circular-pipe loss using wall force balance and Rabinowitsch-Mooney shear-rate correction. Sources: BYU Chemical Engineering Class 33, “More On Non-Newtonian Laminar Flow”, equations 1 and 3–4, https://uolab.groups.et.byu.net/files/pipevisco/hints/374_Class_33.pdf; generalized Reynolds expression as presented at https://www.checalc.com/fluid_flow_power_law.html. Neither source states an edition on these pages. K is supplied in Pa·s^n and is deliberately not represented as ordinary viscosity or automatically converted. Shear-rate fit bounds must be declared; the wall shear rate outside them, invalid values, non-finite values and Reynolds ≥2100 are rejected. A nonzero minimum fit rate produces an explicit near-axis extrapolation warning, and passing the Reynolds screen is not proof of actual laminar flow.

`backend/tests/engines/test_mission6_power_law.py` independently integrates the radial velocity profile, checks the Newtonian Poiseuille limit and tests invalid/inapplicable inputs. `backend/tests/api/test_api.py::test_power_law_pipe_api_persists_scoped_run_and_rejects_out_of_range` verifies API and DB readback and that a rejected case is not saved. These selected checks are **INDEPENDENT_SOFTWARE_REFERENCE_CHECKED**, not field validation. Generic EngineRun persistence/API exists; dedicated bilingual engineering UI, governed design revisions, review, report, annulus, survey/ECD and full vertical slice do **not**. This does not fulfill the requested complete new workflow. The 15 previous engines have not received the requested exhaustive independent re-audit; safety engines remain NEEDS_FIELD_VALIDATION.

## Recovery matrix — every item remains open unless explicitly stated

The referenced tests are existing or focused software evidence, not proof of whole-workflow acceptance. For the earlier 15 engines see [Mission 4 source/test inventory](AUTONOMOUS-MISSION4-ENGINE-INVENTORY.md); it is explicitly not a complete independent audit.

| # | Requirement | Source / current evidence | Deficiency and next action | Status |
|---:|---|---|---|---|
| 1 | Persian deployment OCR | `ingestion/ocr.py`, `ocr_synthetic_quality.py`; Mission 5 CI | Restore actual accuracy and pass all unchanged deployed strata | NOT_ACCEPTED |
| 2 | Bounded page OCR | `ingestion/ocr.py`, `parsers.py`; `tests/ingestion/test_ocr.py` | Layout/rendering uninterruptible; implement isolated termination and native Windows stress | PARTIAL |
| 3 | Authorized holdout | `ingestion/ocr_benchmark.py`; `test_ocr_benchmark.py` | Obtain approved authentic DDRs, freeze independently adjudicated labels and split | BLOCKED_BY_MISSING_AUTHORIZED_EVIDENCE |
| 4 | Full capacity geometry | `engines/fluids.py`; `tests/engines/test_fluids.py` | Tool joints, washout, nesting and measured/design distinction | PARTIAL |
| 5 | Authenticated bore evidence | `engines/fluids.py:OpenBoreDeclaration` | Caller text is not authenticated evidence; build review and identity linkage | PARTIAL |
| 6 | Versioned capacity design | `CirculationWorkspace.tsx`, `engines/service.py` | No governed capacity revision/approval/export journey | PARTIAL |
| 7 | Hydraulics/ECD slice | `engines/fluids.py`; `test_mission4_hydraulics_safety.py` | Full surveyed depth profile, components, review and reports missing | PARTIAL |
| 8 | Survey-linked depths | `drilling/surveys.py`, `engines/trajectory.py` | Connect persisted revision and validated datum to hydraulics | PARTIAL |
| 9 | New rheology | `engines/power_law.py`; `test_mission6_power_law.py` | Pipe math only; annular/full-well design and report still missing | PARTIAL |
| 10 | Survey geometric proximity | `engines/proximity.py`; `test_proximity.py` | Select persisted surveys, datum validation and dedicated workflow | PARTIAL |
| 11 | Anti-collision uncertainty | `engines/proximity.py` | Covariance/error model/SF not implemented; select source and implement | PLANNED |
| 12 | Engine assurance | `engines/registry.py`; Mission 4 inventory; new pipe tests | Full independent source-to-report audit of all 16 not done | PARTIAL |
| 13 | Manual DDR | `drilling/daily_reports.py`; `test_daily_report_domain.py` | Complete authored engineering data/review journey | PARTIAL |
| 14 | DDR batch/correction | `ingestion/pipeline.py`, `drilling/ddr.py` | Batch lifecycle and immutable corrections not fully verified | PARTIAL |
| 15 | Chronology | `drilling/timeline.py`, `operations/time_windows.py` | Reconcile overlaps and missing timestamps; no comprehensive journey | PARTIAL |
| 16 | NPT | `drilling/npt.py`; `test_npt_summary.py` | Recorded-hour totals are not interval-union deduplicated; review and chronology required | PARTIAL |
| 17 | Well Design | `engineering/products.py`, `db/models/design.py` | Cross-domain linked design and E2E not complete | PARTIAL |
| 18 | Programs/Procedures | `engineering/service.py`, `engineering/exports.py`, `ProgramsProceduresWorkspace.tsx` | Existing lifecycle; full acceptance/export journey not reverified | PARTIAL |
| 19 | Advisor/RAG | `drilling/advisor.py`, `rag/retriever.py`; `test_advisor.py` | Retrieval metrics, scope and full citation E2E remain | PARTIAL |
| 20 | Specialist agents | `workflow/nodes.py`, `workflow/graph.py` | Dedicated bounded domain agents and human review incomplete | PARTIAL |
| 21 | Telemetry/Twin | `telemetry/service.py`, `engines/twin.py`; `tests/telemetry/` | End-to-end reconciliation and external interop unverified | PARTIAL |
| 22 | Reports/exports | `engineering/exports.py`, `drilling/reporting.py` | Exact design/approval provenance and requested end-to-end journeys incomplete | PARTIAL |
| 23 | RBAC/deployment | `security/rbac.py`, `backend/Dockerfile`; CI | New flows and deployment assurance not independently complete | PARTIAL |
| 24 | Catalogue | `scripts/verify_engineering_catalogue.py`, `docs/ENGINEERING_CAPABILITY_CATALOGUE.*` | Regenerated inventory only; comprehensive historical audit/schema/roadmap incomplete | PARTIAL |
| 25 | Windows history | `scripts/verify_stack.py`; historical CI | Socket root cause NOT_PROVEN; repeated native diagnostics needed | PARTIAL |
| 26 | Full E2E | `frontend/e2e/`, `ci/test-inventory.json` | Historical 129 do not substitute for requested new journeys | PARTIAL |
| 27 | Exact-SHA CI | `.github/workflows/ci.yml` | Consult CI for final pushed SHA; previous run is historical | PARTIAL |
| 28 | Workspace | baseline and start manifest | Four historical dirs must remain hash/metadata-identical; absent historical artifacts not recovered | PARTIAL |

## Tests and release

A first full local SQLite run reported 1861 passed, 15 skipped and two failures: unit-schema metadata for the new engine and the README test-file-count guard. Both causes were corrected; the focused 27-test regression including both formerly failing tests passed, and five new targeted tests passed. The initial full run is not reclassified as passing. Focused tests and final CI evidence should be read with the final response and exact GitHub run for the final pushed SHA; passing software tests do not establish field readiness. The 129-test E2E inventory was not reduced. The backend inventory increases by exactly five new tests: three engine reference/guard tests, one persisted API test, and one OCR incomplete-input test. No broad assertion or security gate was weakened. The engineering catalogue and roadmap are source-hash-generated declarations, not a replacement for independent assurance.
