# CP140 — Advisor Claim Grounding, Scope, Freshness, and Abstention

**Disposition:** repository-executable Advisor grounding work is implemented and locally verified. This is software evidence only. It is not a live-provider semantic-quality result, field validation, qualified engineering review, vendor verification, customer acceptance, or deployment evidence.

## Implementation

The Advisor now emits an `advisor-grounding-v1` summary and claim-level citations for structured facts, calculations, inferences, and recommendations. Citations identify the source kind, source row or measurement-point ID, evidence reference, locator, and requested/recorded scope. Engine-run citations retain the run and engine version; inference citations point to their supported claims or scoped records; recommendations identify the persisted recommendation record and remain explicitly non-executable advice.

Source scope is carried from telemetry and extracted-record state into the Advisor. Claims with missing source identifiers, explicit project/well scope conflicts, missing engine-run IDs, or no traceable support are withheld from their positive-claim section and surfaced as explicit unknowns. Evidence attachment checks well, project, wellbore, section, and operation scope. Evidence lookup remains bounded and uses a single scoped SQL query; it excludes quarantined, unanchored organization-wide, cross-well and cross-tenant records. Citation metadata retains evidence-link and document scope, page/region locator, quote-check method, confidence, and excerpt. A verified-quote flag describes text occurrence only—not the truth of a claim, extraction accuracy, or engineering meaning.

Telemetry citations identify the measurement point rather than relying only on the channel ID. Freshness is reported from the source observation contract (`fresh`, `stale`, `missing`, or `unknown`); absent assessment is shown as `not_assessed`. `generated_at` remains answer-assembly time, not source observation time. Stored engine results are also marked not currently revalidated. No freshness time, scope, unit, or provenance is inferred from ingestion or report-generation time.

The Advisor UI and API types expose claim citations, scope, freshness, and narrative source references in English and Persian. Provider narration remains optional and cannot modify the structured sections; the existing safe-copy guard allows only complete supplied statements and returns citation references for the statements it repeats.

## Local verification

- Focused Advisor, CP130/CP140 grounding, drilling API, freshness, telemetry RBAC, and NPT selection: **59 passed**.
- Added six backend grounding regressions, covering claim-level citation completeness, stale measurement point identity and freshness, cross-well/unscoped evidence exclusion and bounded query count, bore/section citation scope, recorded scope conflict abstention, and source-free claim abstention. Existing tenant-scope tests were extended to reject same-tenant other-well excerpts.
- Frontend: **402 passed in 40 Vitest files**; targeted Advisor/EvidencePanel UI selection **11 passed**; `npm run typecheck`, `npm run lint`, and the production build passed.
- Backend Ruff on changed modules/tests, Python compilation, and staged/working-tree whitespace checks passed.
- Full backend inventory: **1,751 tests across 134 files**. PostgreSQL-enabled local run: **1,751 passed, 0 skipped** in 616.58 seconds; default SQLite run: **1,736 passed, 15 PostgreSQL-only skips** in 558.29 seconds. The only warning was `RuntimeDirWarning` because `XDG_RUNTIME_DIR` was unset. The reviewed inventory verifier matched all 15 PostgreSQL node IDs across 12 files on both runs.
- Full frontend Vitest suite: **402 passed in 40 files**. `npm run typecheck`, `npm run lint`, and `npm run build` passed. Vite reported the repository's existing large-chunk advisory; the build completed successfully.
- The first PostgreSQL-enabled full run exposed one CP16 documentation-count guard failure: CP140's added test file had increased the inventory to 134 files while the maintained documentation still said 133. The README, testing guide, manifest, and exact frontend test-count assertion were updated to current measured counts; the fresh full reruns then passed. No assertion was skipped, suppressed, or weakened.
- The current Windows manifest split is **1,735 passed, 1 identified POSIX-only skip, 15 PostgreSQL deselections**. It remains an expectation until exact-SHA native-Windows CI; it is not reported as locally observed.

## Evidence boundaries

All CP140 records and test fixtures are synthetic or repository-local. No live AI provider, external endpoint, field system, customer data, or operator decision was used. Citation and quote checks provide traceability and textual-occurrence evidence, not independent adjudication or semantic entailment. Lexical, echo, or stub behavior is not semantic retrieval or live-AI evidence. No autonomous rig control or external endpoint was added. `well_control.kill_sheet` and `tubulars.api_5c3` remain `NEEDS_FIELD_VALIDATION`. Exact-SHA Linux/native-Windows certification of the complete final program is reserved for CP151 after all implementation and documentation changes.
