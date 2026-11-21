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

### Runtime engine registry at baseline

A refreshed [runtime snapshot at `11e6cd2`](mission-reports/AUTONOMOUS-ENGINE-REGISTRY-SNAPSHOT.md) contains 14 self-declared schemas and source hashes; the complete independent and historical/future catalogue remains open. The baseline table below retains its original versions. `formula_derived` refers to the registry's classification, not independent or field validation. Detailed equations, assumptions, independent examples and per-engine source/tests still require the Phase 2 audit. A registry namespace collision fix and its focused evidence are documented in [engine registry assurance](mission-reports/MASTER-MISSION-ENGINE-REGISTRY-ASSURANCE.md). A separate [non-finite execution boundary](mission-reports/CONTINUOUS-ENGINE-NONFINITE-BOUNDARY.md) rejects tested invalid input and output; [MSE 1.1.1 numerical guards](mission-reports/CONTINUOUS-MSE-NUMERICAL-FAIL-CLOSED.md) reject tested underflow and overflow. These changes do not constitute completion of the per-engine audit or field validation. The table below retains baseline versions, rather than claiming a new snapshot was audited.

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
| 5. AI/knowledge | `PARTIAL` | Real embeddings and measured scoped hybrid retrieval, grounded tool invocation, abstention and authorized local/enterprise profiles. [Public OGAI model scan](mission-reports/CONTINUOUS-OGAI-PUBLIC-MODEL-SCAN.md) is card/file-list inspection only; inference remains `NOT_MEASURED`. |
| 6. Agents/studio | `PLANNED` | Actual useful governed specialist-agent workflows, not placeholder cards. |
| 7. Live operations/twin | `PARTIAL` | Tested source-aware telemetry and reconciliation; external interop separately evidenced. |
| 8–9. Lifecycle/reporting | `PARTIAL` | Domain-specific models, calculations, tests and reports rather than decorative pages. |
| 10–11. UX/enterprise/deployment | `PARTIAL` | Complete bilingual accessibility and documented reproducible supported deployments, recovery and security. |
| 12. Competitive benchmarking | `NOT_MEASURED` | [Initial public-source scan](mission-reports/CONTINUOUS-PUBLIC-COMPETITOR-SCAN.md) separates vendor claims from DrillAI source; controlled independent comparisons and comprehensive survey remain outstanding. |
| 13. Integrated verification | `NOT_ACCEPTED` | Full backend, browser, security, deployment and same-SHA Linux/native Windows evidence. |
| 14. Final capability catalogue | `PARTIAL` | [Runtime metadata snapshot](mission-reports/CONTINUOUS-ENGINE-METADATA-SNAPSHOT.md) records all 14 self-declared engine contracts; exhaustive historical/current/future evidence-indexed audit and release verdict remain outstanding. |

Dependencies: Phase 1 safety gates precede final certification; field validation and external interoperability cannot be substituted by synthetic evidence. Phase 2 engineering assurance is prerequisite to claiming new calculations as validated. Later phases may progress independently when external dependencies block earlier ones, but their status cannot be promoted beyond demonstrated evidence.
