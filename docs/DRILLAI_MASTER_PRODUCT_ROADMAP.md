# DrillAI master product roadmap and evidence index

This is the canonical product-development roadmap. Earlier checkpoint reports and `mission-reports/REMAINING-GAPS-ROADMAP.md` are historical supporting evidence; their historical green statuses do not override newer failed runs. This document is an evolving inventory, **not** a declaration that the complete historical source audit or final catalogue has been finished.

## Purpose and non-negotiable boundaries

DrillAI is intended to connect scoped well data, engineering calculations, documents, operations, workflows and human-reviewed decisions. Software tests are not field acceptance, a safety case, external vendor certification or authorization to operate equipment. AI cannot supply missing engineering inputs, invent citations, approve revisions or control a rig. Confidential source material must not be sent to an external provider without deployment policy authorization. `well_control.kill_sheet` and `tubulars.api_5c3` remain `NEEDS_FIELD_VALIDATION`. Missing station timestamps must not be derived from a DDR report period.

## Baseline and current evidence

The 2026-10-20 mission baseline is remote commit `36f0add49392d89ceedba21c2a0acedcdefebe2c`; the main ref is `bfa066b28e0071880cb9191a4f1d47fdaa143e04`. GitHub commit/run metadata reports later dates than the mission system date; the timestamps are recorded as source metadata, not independently verified present dates. Baseline investigation and workspace preservation are documented in [phase 0 evidence](mission-reports/MASTER-MISSION-PHASE0-BASELINE.md). Subsequent implementation and exact-SHA results are recorded in the [interim progress report](mission-reports/MASTER-MISSION-PROGRESS-AND-REMAINING-WORK.md) and [latest CI observations](mission-reports/CONTINUOUS-LATEST-CI-OBSERVATIONS.md).

On baseline [CI run 44248737740](https://github.com/asgareyvazi/DrillAI/actions/runs/44248737740), Linux and native Windows standard Playwright suites each reported 128/128 and Linux CP150 reported 1/1; **both jobs failed supply-chain verification**, so their backend steps and Linux Compose/OCR were skipped. The continuation's starting SHA `44473defe536c8f9a62a913d4300b4d2936727eb` was independently tested by [run 44262402202](https://github.com/asgareyvazi/DrillAI/actions/runs/44262402202): both backend suites and browser suites passed, as did Linux CP150 and Compose probes, but supply-chain and deployment OCR failed. Subsequent changes need their own exact-SHA run. This is not an accepted release. The earlier local complete Playwright run had 127 passes and one auth-oracle failure; its cause remains open. See [CP155–CP166](mission-reports/CP155-CP166-PRODUCT-AND-ASSURANCE.md), [CP153](mission-reports/CP153-WINDOWS-SURVEY-TIMESTAMP-REGRESSION.md), and the [continuation evidence](mission-reports/CONTINUOUS-MISSION-BASELINE-AND-RETRIEVAL.md).

[Continuation OCR evidence and corrections](mission-reports/AUTONOMOUS-CONTINUATION-OCR-DIAGNOSTICS.md) documents a reproduced RAQM double-reordering defect in the synthetic renderer, its correction, and an independent production TSV quote/provenance corruption fix. The corrected local emulator still fails Persian quality; no deployment-image or holdout acceptance is claimed. Exact-SHA [CI 44884105571](https://github.com/asgareyvazi/DrillAI/actions/runs/44884105571) on `5a34dac` failed deployment OCR and Windows Playwright (`ERR_NO_BUFFER_SPACE`), while Linux/Windows backends, frontend units and security passed.

CP135 synthetic OCR remains `NOT_ACCEPTED`: unchanged global CER ≤0.15, WER ≤0.20 and critical exact ≥0.90, with critical exact ≥0.90 in every language/layout/scan stratum and all cases evaluated. CP154's best documented local 150-DPI metrics are 0.097746/0.144330/0.809896 globally and 0.207663/0.275510/0.640625 for Persian. These are not deployment-image or field metrics. A separate [offline Tesseract.js exploratory comparison](mission-reports/CONTINUOUS-OCR-TESSERACTJS-COMPARISON.md) also failed, is not a holdout or production-adapter measurement, and was not adopted. A [native-library synthetic diagnostic](mission-reports/CONTINUOUS-TESSEROCR-SYNTHETIC-DIAGNOSIS.md) localized fixture-specific field and layout failures but is not deployment-image evidence. See [CP154](mission-reports/CP154-PERSIAN-OCR-AND-CI-GATES.md).

The latest continuation began at `db29fe22b78cd9af9939e19ff0f44f0c27686e95`. Explicit ref fetch and unshallowing resolved the earlier incomplete local ancestry: the main merge base is `bfa066b28e0071880cb9191a4f1d47fdaa143e04`, with the authorized branch 276 ahead and zero behind at that checkpoint. See the corrected [forensic inventory and audit](mission-reports/AUTONOMOUS-CONTINUATION-FORENSICS-AND-AUDIT.md). [Exact-SHA CI 44879534008](https://github.com/asgareyvazi/DrillAI/actions/runs/44879534008) passed security, Linux/Windows backend and browser sub-gates but failed deployment OCR; it did not establish release acceptance. A source-order hazard was repaired and verified, but the exact historical CP153 incident trigger remains unknown. Local audit success is not field acceptance.

## Fluids and run-stream continuation

[Fluids and stream evidence](mission-reports/AUTONOMOUS-CONTINUATION-FLUIDS-AND-STREAM.md) records independently reproduced numerical-boundary and incomplete-bottoms-up defects, versioned repairs to two existing engines, and an independently reproduced socket churn fix. It does not claim that socket churn caused the native Windows error, that two full engineering vertical slices are complete, or that the OCR/release gates passed. The [runtime self-declaration snapshot](mission-reports/AUTONOMOUS-ENGINE-REGISTRY-85396bc.json) describes implementation revision `85396bc` only; it is not the requested independent audit or complete catalogue. The final matrix must be rerun on the actual final SHA.

## Mission 4 partial pressure and provenance safeguards

[Mission 4 evidence and reopening matrix](mission-reports/AUTONOMOUS-MISSION4-PARTIAL.md) documents versioned hydraulic unknown handling, explicitly declared pressure terms, fail-closed optimisation applicability, and unverified interval declaration entry. These do not complete the governed capacity or hydraulics workflows. The [15-engine inventory](mission-reports/AUTONOMOUS-MISSION4-ENGINE-INVENTORY.md) is not the requested independent full audit. Persian deployment OCR, a real holdout, and the historical Windows root cause remain open.

## Capability map (initial source inspection; further audit required)

| Area | Observed implementation and evidence | Current boundary |
|---|---|---|
| Assets and well hierarchy | `backend/src/drillai/assets/`, `api/routers/assets.py`, `frontend/src/pages/master-data/`; API and browser tests | Software implementation; field identity must be explicitly scoped. |
| DDR, operations, NPT | `drilling/daily_reports.py`, `drilling/ddr.py`, `drilling/npt.py`, `operations/`, `api/routers/daily_reports.py`, `frontend/src/pages/wells/`; tests and CP136–CP138 | Structured drafts, provenance and review exist; not all requested DDR fields/workflows have been audited. |
| Survey | `engines/trajectory.py`, `drilling/surveys.py`, `drilling/ddr.py`, `frontend/src/pages/wells/SurveysWorkspace.tsx`, CP115/116/141/153 | A [source-order repair](mission-reports/AUTONOMOUS-SURVEY-SOURCE-ORDER.md) addresses a reproduced query-order hazard and fails closed on ambiguous row evidence; the native Windows backend passed on fix SHA `8f013eb`, but the exact historical cause and later-SHA verification remain open. Anti-collision not claimed. |
| Program/procedure | `engineering/products.py`, `engineering/service.py`, `engineering/exports.py`, `api/routers/engineering_products.py`, `ProgramsProceduresWorkspace.tsx`, CP155–CP166 | Scoped structured revisions, reviews and synthetic pilot; not an automatic full well-design system. |
| Intake and OCR | `ingestion/parsers.py`, `ingestion/ocr*.py`, CP135/154 | PDF, DOCX, XLSX/XLSM, CSV/TSV and text parsers listed in source; optional scanned PDF OCR. LAS, DLIS and standalone-image support not asserted. Persian threshold failure persists. |
| Retrieval and Advisor | `rag/retriever.py`, `rag/benchmark.py`, `ai/tools.py`, `drilling/advisor.py`, CP139/140 and [continuation retrieval assurance](mission-reports/CONTINUOUS-MISSION-BASELINE-AND-RETRIEVAL.md) | Scoped Unicode/Persian lexical search and project/operation tool filtering have focused tests. Real embeddings, independent semantic/hybrid and Persian holdout benchmark evidence remain necessary. |
| Telemetry and workflows | `telemetry/`, `workflow/`, `ConnectorsPage.tsx`, `WorkflowStudio.tsx`, CP122–130/145–148 | Local harness coverage is not external commercial vendor interoperability or delivery evidence. |
| Reporting | `drilling/structured_report_exports.py`, `drilling/daily_report_exports.py`, `engineering/exports.py`, `ReportsWorkspace.tsx` | Existing exports must be assessed individually; requested full lifecycle report coverage is incomplete. |
| Security and operations | `security/`, `.github/workflows/ci.yml`, `scripts/verify_supply_chain_and_secrets.py` | Exact checkpoint CI is red. The later live advisory API reports the historical `picomatch` advisory withdrawn and a fresh full local audit passed; its external metadata dates conflict with the mission clock, and new exact-SHA Linux/Windows checks remain required. See [forensic and audit evidence](mission-reports/AUTONOMOUS-CONTINUATION-FORENSICS-AND-AUDIT.md). External customer, field and vendor acceptance are not measured. |

### Runtime engine registry (generated declarations)

The following table is generated from the runtime registry and source files by `scripts/verify_engine_roadmap.py`; CI checks for drift. Source SHA-256 normalizes CRLF to LF so checkout line endings do not masquerade as source changes. A [full self-declaration snapshot at `85396bc`](mission-reports/AUTONOMOUS-ENGINE-REGISTRY-85396bc.json) records contracts, references and assumptions. Neither table nor snapshot is independent engineering validation. Last completed exact-SHA runs at the mission baseline were [44898691159](https://github.com/asgareyvazi/DrillAI/actions/runs/44898691159) and [44900279810](https://github.com/asgareyvazi/DrillAI/actions/runs/44900279810) on `b6175b6bea858ee5b86f1dac99ed94f41c23756e`; both failed OCR. Later source changes require new exact-SHA evidence. The full independent and historical/future catalogue remains open.

<!-- BEGIN GENERATED ENGINE REGISTRY -->
| Key | Version | Registry declaration | Source | Source SHA-256 (LF normalized) |
|---|---|---|---|---|
| `drilling.mse` | 1.1.1 | formula_derived | `backend/src/drillai/engines/mechanics.py` | `9feee5be5f8602d92a0d407188d8eb1aabd7b6218b5cc3a3bdd7a7bbbd4c4b27` |
| `hydraulics.laminar` | 1.6.0 | formula_derived | `backend/src/drillai/engines/fluids.py` | `f6be6cb2d37f56a063d3710c78b21ab031fe7b6659e31e06b80116ca3afac0fc` |
| `integrity.barrier_envelope` | 1.1.0 | formula_derived | `backend/src/drillai/engines/integrity.py` | `33c8ff117e649ac14a09aec8f10ceb6b25d2b5cdf1ae6396094c2310a0afbf2d` |
| `offsets.similarity` | 1.1.0 | formula_derived | `backend/src/drillai/engines/offsets.py` | `e64e0d7f57bda6ffd680590d607a5d620d9945f1d455d933033c417474dbd06a` |
| `optimization.pareto` | 1.1.0 | formula_derived | `backend/src/drillai/engines/optimization.py` | `aacf9a131edcc5e65896dbf82a3ebdb50f14d25baf5daa126c2617c0c76d84e8` |
| `optimization.sweep` | 1.1.0 | formula_derived | `backend/src/drillai/engines/optimization.py` | `aacf9a131edcc5e65896dbf82a3ebdb50f14d25baf5daa126c2617c0c76d84e8` |
| `readiness.assessment` | 1.1.0 | formula_derived | `backend/src/drillai/engines/readiness.py` | `25e60115f5ee24361b224b42fe7b332d2cc1d85c53e11c1f97a20e53533e2a8d` |
| `schematic.well_model` | 1.1.0 | formula_derived | `backend/src/drillai/engines/schematic.py` | `b938f647de4b90ff989762a8565a22b9b6a6c9dd0f647c7e1e6a85260c751200` |
| `torque_drag.soft_string` | 1.1.0 | formula_derived | `backend/src/drillai/engines/mechanics.py` | `9feee5be5f8602d92a0d407188d8eb1aabd7b6218b5cc3a3bdd7a7bbbd4c4b27` |
| `trajectory.geometric_proximity` | 1.0.0 | formula_derived | `backend/src/drillai/engines/proximity.py` | `5a74d30345800db19f354b4b74b438e6fabac50124542e79d6b9f9d61a0d57f5` |
| `trajectory.minimum_curvature` | 1.3.0 | formula_derived | `backend/src/drillai/engines/trajectory.py` | `c6a6fb1063b9d69b87f9d5d284f38eb25b431b6cfe3f5525378727befc2d493c` |
| `tubulars.api_5c3` | 1.1.0 | needs_field_validation | `backend/src/drillai/engines/tubulars.py` | `3bdc50ef9b0b7044710ea49ffca1d3ad55dbd4759571b96219f9590bb61f5a36` |
| `twin.reconciliation` | 1.2.0 | formula_derived | `backend/src/drillai/engines/twin.py` | `5c56256b8f813f531e4aa9e1f771679f93e9297f332d3054e00cda56dc464386` |
| `well_control.kill_sheet` | 1.2.0 | needs_field_validation | `backend/src/drillai/engines/wellcontrol.py` | `00ad181423c078d74c72dac07a870aaecc2be5d7050f7f0993b86c5b6f560dc8` |
| `wellbore.capacity` | 1.4.0 | formula_derived | `backend/src/drillai/engines/fluids.py` | `f6be6cb2d37f56a063d3710c78b21ab031fe7b6659e31e06b80116ca3afac0fc` |
<!-- END GENERATED ENGINE REGISTRY -->

## Ordered mission plan and acceptance

| Phase | Status at baseline | Next acceptance requirement |
|---|---|---|
| 0. Baseline/source audit | `PARTIAL` | Complete source-to-report reconciliation, including historical catalogue and workspace ownership. |
| 1. CI/security/Windows/OCR | `NOT_ACCEPTED` | Remediate unsuppressed audit; diagnose CP153 natively; verify full exact-SHA matrix, Compose and deployment OCR. |
| 2. Engine assurance | `PARTIAL` | Complete per-engine equation/input/unit/error/source audit and independent comparisons without field-validation claims. |
| 3. Data/DDR/OCR | `PARTIAL` | Expand audited formats and review lifecycle; meet unchanged OCR criteria on every stratum. |
| 4. Program/procedure studio | `PARTIAL` | Complete source-backed design basis, governed calculation and exact-revision end-to-end workflow with real output evidence. |
| 5. AI/knowledge | `PARTIAL` | Real embeddings and measured scoped hybrid retrieval, grounded tool invocation, abstention and authorized local/enterprise profiles. [Public OGAI model scan](mission-reports/CONTINUOUS-OGAI-PUBLIC-MODEL-SCAN.md) is card/file-list inspection only; inference remains `NOT_MEASURED`. |
| 6. Agents/studio | `PLANNED` | Actual useful governed specialist-agent workflows, not placeholder cards. |
| 7. Live operations/twin | `PARTIAL` | Tested source-aware telemetry and reconciliation; external interop separately evidenced. |
| 8–9. Lifecycle/reporting | `PARTIAL` | Domain-specific models, calculations, tests and reports rather than decorative pages. |
| 10–11. UX/enterprise/deployment | `PARTIAL` | Complete bilingual accessibility and documented reproducible supported deployments, recovery and security. |
| 12. Competitive benchmarking | `NOT_MEASURED` | [Initial public-source scan](mission-reports/CONTINUOUS-PUBLIC-COMPETITOR-SCAN.md) separates vendor claims from DrillAI source; controlled independent comparisons and comprehensive survey remain outstanding. |
| 13. Integrated verification | `NOT_ACCEPTED` | Full backend, browser, security, deployment and same-SHA Linux/native Windows evidence. |
| 14. Final capability catalogue | `PARTIAL` | [Runtime metadata snapshot](mission-reports/CONTINUOUS-ENGINE-METADATA-SNAPSHOT.md) records all 14 self-declared engine contracts; exhaustive historical/current/future evidence-indexed audit and release verdict remain outstanding. |

Dependencies: Phase 1 safety gates precede final certification; field validation and external interoperability cannot be substituted by synthetic evidence. Phase 2 engineering assurance is prerequisite to claiming new calculations as validated. Later phases may progress independently when external dependencies block earlier ones, but their status cannot be promoted beyond demonstrated evidence.
