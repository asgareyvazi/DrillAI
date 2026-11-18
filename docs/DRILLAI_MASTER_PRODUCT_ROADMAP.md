# DrillAI master product roadmap and evidence index

This is the canonical product-development roadmap. Earlier checkpoint reports and `mission-reports/REMAINING-GAPS-ROADMAP.md` are historical supporting evidence; their historical green statuses do not override newer failed runs. This document is an evolving inventory, **not** a declaration that the complete historical source audit or final catalogue has been finished.

## Purpose and non-negotiable boundaries

DrillAI is intended to connect scoped well data, engineering calculations, documents, operations, workflows and human-reviewed decisions. Software tests are not field acceptance, a safety case, external vendor certification or authorization to operate equipment. AI cannot supply missing engineering inputs, invent citations, approve revisions or control a rig. Confidential source material must not be sent to an external provider without deployment policy authorization. `well_control.kill_sheet` and `tubulars.api_5c3` remain `NEEDS_FIELD_VALIDATION`. Missing station timestamps must not be derived from a DDR report period.

## Baseline and current evidence

The 2026-10-20 mission baseline is remote commit `36f0add49392d89ceedba21c2a0acedcdefebe2c`; the main ref is `bfa066b28e0071880cb9191a4f1d47fdaa143e04`. GitHub commit/run metadata reports later dates than the mission system date; the timestamps are recorded as source metadata, not independently verified present dates. Baseline investigation and workspace preservation are documented in [phase 0 evidence](mission-reports/MASTER-MISSION-PHASE0-BASELINE.md). Subsequent implementation and exact-SHA results are recorded in the [interim progress report](mission-reports/MASTER-MISSION-PROGRESS-AND-REMAINING-WORK.md).

On baseline [CI run 44248737740](https://github.com/asgareyvazi/DrillAI/actions/runs/44248737740), Linux and native Windows standard Playwright suites each reported 128/128 and Linux CP150 reported 1/1; **both jobs failed supply-chain verification**, so their backend steps and Linux Compose/OCR were skipped. The continuation's starting SHA `44473defe536c8f9a62a913d4300b4d2936727eb` was independently tested by [run 44262402202](https://github.com/asgareyvazi/DrillAI/actions/runs/44262402202): both backend suites and browser suites passed, as did Linux CP150 and Compose probes, but supply-chain and deployment OCR failed. Subsequent changes need their own exact-SHA run. This is not an accepted release. The earlier local complete Playwright run had 127 passes and one auth-oracle failure; its cause remains open. See [CP155–CP166](mission-reports/CP155-CP166-PRODUCT-AND-ASSURANCE.md), [CP153](mission-reports/CP153-WINDOWS-SURVEY-TIMESTAMP-REGRESSION.md), and the [continuation evidence](mission-reports/CONTINUOUS-MISSION-BASELINE-AND-RETRIEVAL.md).

CP135 synthetic OCR remains `NOT_ACCEPTED`: unchanged global CER ≤0.15, WER ≤0.20 and critical exact ≥0.90, with critical exact ≥0.90 in every language/layout/scan stratum and all cases evaluated. CP154's best documented local 150-DPI metrics are 0.097746/0.144330/0.809896 globally and 0.207663/0.275510/0.640625 for Persian. These are not deployment-image or field metrics. A separate [offline Tesseract.js exploratory comparison](mission-reports/CONTINUOUS-OCR-TESSERACTJS-COMPARISON.md) also failed, is not a holdout or production-adapter measurement, and was not adopted. See [CP154](mission-reports/CP154-PERSIAN-OCR-AND-CI-GATES.md).

## Capability map (initial source inspection; further audit required)

| Area | Observed implementation and evidence | Current boundary |
|---|---|---|
| Assets and well hierarchy | `backend/src/drillai/assets/`, `api/routers/assets.py`, `frontend/src/pages/master-data/`; API and browser tests | Software implementation; field identity must be explicitly scoped. |
| DDR, operations, NPT | `drilling/daily_reports.py`, `drilling/ddr.py`, `drilling/npt.py`, `operations/`, `api/routers/daily_reports.py`, `frontend/src/pages/wells/`; tests and CP136–CP138 | Structured drafts, provenance and review exist; not all requested DDR fields/workflows have been audited. |
| Survey | `engines/trajectory.py`, `drilling/surveys.py`, `frontend/src/pages/wells/SurveysWorkspace.tsx`, CP115/116/141/153 | Historical Windows provenance regression root cause not established; anti-collision not claimed. |
| Program/procedure | `engineering/products.py`, `engineering/service.py`, `engineering/exports.py`, `api/routers/engineering_products.py`, `ProgramsProceduresWorkspace.tsx`, CP155–CP166 | Scoped structured revisions, reviews and synthetic pilot; not an automatic full well-design system. |
| Intake and OCR | `ingestion/parsers.py`, `ingestion/ocr*.py`, CP135/154 | PDF, DOCX, XLSX/XLSM, CSV/TSV and text parsers listed in source; optional scanned PDF OCR. LAS, DLIS and standalone-image support not asserted. Persian threshold failure persists. |
| Retrieval and Advisor | `rag/retriever.py`, `rag/benchmark.py`, `ai/tools.py`, `drilling/advisor.py`, CP139/140 and [continuation retrieval assurance](mission-reports/CONTINUOUS-MISSION-BASELINE-AND-RETRIEVAL.md) | Scoped Unicode/Persian lexical search and project/operation tool filtering have focused tests. Real embeddings, independent semantic/hybrid and Persian holdout benchmark evidence remain necessary. |
| Telemetry and workflows | `telemetry/`, `workflow/`, `ConnectorsPage.tsx`, `WorkflowStudio.tsx`, CP122–130/145–148 | Local harness coverage is not external commercial vendor interoperability or delivery evidence. |
| Reporting | `drilling/structured_report_exports.py`, `drilling/daily_report_exports.py`, `engineering/exports.py`, `ReportsWorkspace.tsx` | Existing exports must be assessed individually; requested full lifecycle report coverage is incomplete. |
| Security and operations | `security/`, `.github/workflows/ci.yml`, `scripts/verify_supply_chain_and_secrets.py` | Exact baseline CI is red; `picomatch` advisory currently blocks full-dependency audit. External customer, field and vendor acceptance are not measured. |

### Runtime engine registry at baseline

The runtime registry reports the following keys and versions. `formula_derived` refers to the registry's classification, not independent or field validation. Detailed equations, assumptions, independent examples and per-engine source/tests still require the Phase 2 audit. A registry namespace collision fix and its focused evidence are documented in [engine registry assurance](mission-reports/MASTER-MISSION-ENGINE-REGISTRY-ASSURANCE.md). A separate [non-finite execution boundary](mission-reports/CONTINUOUS-ENGINE-NONFINITE-BOUNDARY.md) rejects tested invalid input and output; neither change constitutes completion of the per-engine audit or field validation.

| Key | Version | Registry status | Source module |
|---|---|---|---|
| `drilling.mse` | 1.1.0 | formula_derived | `engines/mechanics.py` |
| `hydraulics.laminar` | 1.2.0 | formula_derived | `engines/fluids.py` |
| `integrity.barrier_envelope` | 1.1.0 | formula_derived | `engines/integrity.py` |
| `offsets.similarity` | 1.1.0 | formula_derived | `engines/offsets.py` |
| `optimization.pareto` | 1.1.0 | formula_derived | `engines/optimization.py` |
| `optimization.sweep` | 1.1.0 | formula_derived | `engines/optimization.py` |
| `readiness.assessment` | 1.1.0 | formula_derived | `engines/readiness.py` |
| `schematic.well_model` | 1.1.0 | formula_derived | `engines/schematic.py` |
| `torque_drag.soft_string` | 1.1.0 | formula_derived | `engines/mechanics.py` |
| `trajectory.minimum_curvature` | 1.3.0 | formula_derived | `engines/trajectory.py` |
| `tubulars.api_5c3` | 1.1.0 | needs_field_validation | `engines/tubulars.py` |
| `twin.reconciliation` | 1.2.0 | formula_derived | `engines/twin.py` |
| `well_control.kill_sheet` | 1.2.0 | needs_field_validation | `engines/wellcontrol.py` |
| `wellbore.capacity` | 1.1.0 | formula_derived | `engines/fluids.py` |

## Ordered mission plan and acceptance

| Phase | Status at baseline | Next acceptance requirement |
|---|---|---|
| 0. Baseline/source audit | `PARTIAL` | Complete source-to-report reconciliation, including historical catalogue and workspace ownership. |
| 1. CI/security/Windows/OCR | `NOT_ACCEPTED` | Remediate unsuppressed audit; diagnose CP153 natively; verify full exact-SHA matrix, Compose and deployment OCR. |
| 2. Engine assurance | `PARTIAL` | Complete per-engine equation/input/unit/error/source audit and independent comparisons without field-validation claims. |
| 3. Data/DDR/OCR | `PARTIAL` | Expand audited formats and review lifecycle; meet unchanged OCR criteria on every stratum. |
| 4. Program/procedure studio | `PARTIAL` | Complete source-backed design basis, governed calculation and exact-revision end-to-end workflow with real output evidence. |
| 5. AI/knowledge | `PARTIAL` | Real embeddings and measured scoped hybrid retrieval, grounded tool invocation, abstention and authorized local/enterprise profiles. |
| 6. Agents/studio | `PLANNED` | Actual useful governed specialist-agent workflows, not placeholder cards. |
| 7. Live operations/twin | `PARTIAL` | Tested source-aware telemetry and reconciliation; external interop separately evidenced. |
| 8–9. Lifecycle/reporting | `PARTIAL` | Domain-specific models, calculations, tests and reports rather than decorative pages. |
| 10–11. UX/enterprise/deployment | `PARTIAL` | Complete bilingual accessibility and documented reproducible supported deployments, recovery and security. |
| 12. Competitive benchmarking | `NOT_MEASURED` | Public-source comparison separating advertisements from independent verification. |
| 13. Integrated verification | `NOT_ACCEPTED` | Full backend, browser, security, deployment and same-SHA Linux/native Windows evidence. |
| 14. Final capability catalogue | `PARTIAL` | Exhaustive historical/current/future evidence-indexed engineering inventory and release verdict. |

Dependencies: Phase 1 safety gates precede final certification; field validation and external interoperability cannot be substituted by synthetic evidence. Phase 2 engineering assurance is prerequisite to claiming new calculations as validated. Later phases may progress independently when external dependencies block earlier ones, but their status cannot be promoted beyond demonstrated evidence.
