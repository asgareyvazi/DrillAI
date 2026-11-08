# CP134 — Source-Level Product Audit and Priority Fixes

- **Audit date:** 2026-11-08
- **Repository:** `asgareyvazi/DrillAI`
- **Authorized branch:** `arena/9d8f5568-drillai`
- **Audit baseline:** `12bce5b4001d54ac47590700d1ad0240830d47e2` (CP133 documentation reconciliation)
- **Disposition:** `DONE` for source audit, capability map, and the bounded Advisor evidence-query correction below. This is not field, vendor, customer, production, or qualified-engineering acceptance.

## Audit method and scope

The audit followed user-visible paths from frontend routes and API handlers through service validation, authorization, persistence, provenance, workers and tests. It sampled the DDR, document/OCR, RAG/Advisor, survey/engineering, operations/NPT, reporting, connectors, notifications, migrations/recovery, security and bilingual operator surfaces. Existing tests and exact-SHA evidence were checked as software evidence only; no live provider, vendor endpoint, field document, customer account or operational system was contacted.

The capability table is deliberately evidence-bounded. A locally exercised API or browser path is not elevated to field acceptance, and a configured adapter is not reported as a provider or vendor result. The review identified a bounded query fan-out in Advisor evidence assembly and prioritized the larger remaining repository-executable gaps for their assigned follow-on phases.

## Capability-to-evidence matrix

| Capability | Current source and observed software evidence | Audit disposition / next work |
| --- | --- | --- |
| Daily drilling report lifecycle | `db/models/daily_reports.py`, `drilling/daily_reports.py`, `api/routers/daily_reports.py`, `frontend/src/pages/wells/DailyReportsWorkspace.tsx`; immutable revisions, review/reconciliation, compare-and-swap writes, source links, audit and tenant scope are covered by API, migration, frontend and browser tests. | Software lifecycle exists. CP136–CP138 add the requested upload/review/promotion, report correction/history and cross-scope readback assurance; real report interpretation remains field-dependent. |
| OCR intake and evaluation | `ingestion/parsers.py`, `ingestion/ocr.py`, `ingestion/ocr_synthetic_quality.py`; the pinned CP130 corpus contains 32 clean, one-page English synthetic cases. Exact-SHA CP132 ran the image-only PDF path in the deployment image. | The mechanism is real but its current corpus is too small and does not stratify language/layout/scan conditions. CP135 must provide 60+ varied generated cases, actual-runtime language checks, stratified metrics and per-case, transcript-free evidence. Existing English synthetic metrics are not field accuracy. |
| Retrieval and RAG evaluation | `rag/retriever.py` exposes distinct retrieval modes; `rag/benchmark.py` currently builds 160 synthetic chunks and 106 distinct queries, but every benchmark query requests lexical retrieval. Local benchmark run: Recall@5 1.0 on 104 positive synthetic queries, 2 expected-empty queries, zero forbidden/cross-scope hits, and 1.0 citation/source-locator coverage. | The result is useful development-fixture evidence, not semantic retrieval. CP139 must scale to at least 500 chunks and 250 distinct queries, evaluate retrieval modes separately, and report the required scope/stratified metrics without fake embeddings. |
| Operations Advisor | `drilling/advisor.py` returns typed facts, versioned calculations, evidence, inference, recommendations and unknowns; source-scope, freshness, abstention and safety-engine limitations have tests. Echo is deterministic; no live model quality run is claimed. | A source-level N+1 evidence lookup was fixed in CP134 (details below). CP140 must strengthen claim-to-citation, freshness and safe-abstention checks; lexical/stub behavior remains distinct from live-provider quality. |
| Survey and engineering calculations | `api/routers/surveys.py` and `drilling/surveys.py` provide manual and reviewed-source imports, explicit datum/reference frames, immutable revisions, exact-depth comparisons and provenance-preserving CSV. The registered trajectory engine is versioned and unit-aware. | Software survey workflow exists. CP141 completes its required browser lifecycle and explicit reference checks; CP142 supplies at least three independently verified calculation families. No formula suite is a qualified engineering sign-off; `well_control.kill_sheet` and `tubulars.api_5c3` remain `NEEDS_FIELD_VALIDATION`. |
| Operations and NPT analytics | `operations`, `drilling/npt.py`, daily-report contracts and timeline/Advisor paths retain source/time bases and unknown states. | CP143 must verify KPI definitions, denominators, attribution, unverified periods and missing-data behavior against persisted synthetic records; no inferred cause or measured field performance is allowed. |
| Reports and exports | Revision-bound DDR export APIs in `drilling/daily_report_exports.py` support JSON/PDF/DOCX/XLSX with selected immutable revision, provenance, safety notice and audit. The separate generic `ReportsWorkspace` still states that document rendering is not implemented. | The DDR exporters are software evidence, not end-to-end report acceptance for every report workflow. CP144 closes the requested real PDF/XLSX lifecycle, content/metadata validation and Persian RTL checks without collapsing facts, calculations, evidence and recommendations. |
| Connectors | `telemetry/connectors.py`, WITSML/ETP adapters, bounded workers and local protocol harnesses implement authorization, scope, checkpoints, fencing and egress controls. | CP145 can harden retry/recovery and readback paths. No commercial endpoint, secret, vendor transcript or field interoperability has been exercised; keep external commissioning `BLOCKED_EXTERNAL`. |
| Notifications | `workflow/notifications.py` and the outbox worker distinguish queue, transport acceptance, sent/failed/reconciliation states; current tests use local fakes. | CP146 must keep queued/accepted/sent/delivered/bounced/failed/unknown semantically distinct. No provider acceptance or recipient-delivery receipt is claimed. |
| Database, workers and recovery | Alembic migrations, PostgreSQL-backed constraints/concurrency tests, worker fencing and backup/restore test paths exist. | CP147 must execute recovery, bounded-resource and worker-restart failure paths. CI/database drills do not establish production RPO/RTO or backup restorability in an operator environment. |
| Security and tenant isolation | Shared action authorization, service-level scope checks, composite keys, audit and adversarial API/migration tests are present across the reviewed surfaces. | CP148 extends tests at real service/API boundaries. This audit is not an independent penetration test or external security certification. |
| Bilingual/accessibility | Shared English/Persian catalogues, RTL/LTR isolation, accessible component patterns and browser tests exist. | CP149 must verify operator-facing labels, keyboard/assistive interactions, RTL/LTR behavior and persisted-state readback across the audited workflow. No human language-acceptance study is claimed. |
| Integrated browser pilot | CP131 is a persisted, API-backed synthetic pilot in one synthetic well. | CP150 must add an isolated two-well synthetic pilot with real APIs, distinct scope, browser journeys and database readback; CP131 is not evidence for cross-well isolation by itself. |
| Release assurance | Exact-SHA CP132 run `42481107515` passed Linux/native Windows for SHA `286945c9d627ba58a4f765b3f28cc65a5e0003ae`. | Each later source or documentation commit needs its own evidence. CP151 owns final exact-SHA Linux/native-Windows CI and the assurance report; this CP132 run is not inherited by later commits. |

## Priority defect found and fixed

`OperationsAdvisor._evidence_for` first selected the evidence links, then issued a separate document query for every linked item to resolve its title and re-check tenant/quarantine scope. With the method's default limit of 25, a single evidence section could require up to 26 sequential database round trips. The extra queries were safety checks, not an intentional per-row transaction boundary.

The query now selects the evidence link and joined document identity/title together, retaining `org_id`, subject, and `evidence_document_not_quarantined_condition()` predicates in the same SQL statement. A defensive guard still refuses a non-null link whose joined document is absent. The API payload fields, ordering, 25-item bound, and safe omission behavior are unchanged.

Regression coverage extends the existing Advisor evidence isolation test: one valid same-tenant document is returned, a hostile cross-tenant link is excluded, the foreign title is not exposed, and SQLAlchemy's execution event verifies the evidence lookup uses exactly one `SELECT` for both links.

## Verification

- Focused backend selection: `test_advisor_evidence_is_tenant_scoped_in_one_bounded_query` plus `test_advisor_cp130_grounding.py` — **3 passed**.
- Full backend suite with embedded PostgreSQL enabled: `DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest -q` — **1,740 tests completed successfully, zero skips**; only the existing `XDG_RUNTIME_DIR` warning was emitted.
- Ruff on the changed service/test and `compileall` — passed.
- CP16 maintained-documentation/count consistency guard — **1 passed** after adding the CP133 run record.
- `git diff --check` and explicit trailing-whitespace checks on the new audit/reconciliation reports — passed.
- Exact-SHA CP133 Actions run `42505266483` tested `12bce5b`; Linux succeeded and native Windows failed the real-browser gate at `identity-permissions.spec.ts:152` with the annotated `net::ERR_NO_BUFFER_SPACE` transport failure. Its log retrieval returned EOF and no further root cause is established. This historical failure does not certify or invalidate the separately tested CP134 source fix.
- Exact-SHA CP134 Actions run [`42507488169`](https://github.com/asgareyvazi/DrillAI/actions/runs/42507488169) tested commit `915c623df532a08434881e67486aa14fb87ce688` and **passed Linux and native Windows**. Linux reported 1,740 backend passes/0 skips, 400 frontend tests, and 127 Playwright passes/0 retries; Windows reported 1,724 backend passes, one identified POSIX-only skip and 15 PostgreSQL deselections, 400 frontend tests, and 127 Playwright passes/0 retries. Deployment-image smoke and all acceptance gates passed; only failure-only artifact uploads were skipped. The image ran the prior CP130 32-case English synthetic OCR corpus; this does not measure CP135 or field accuracy.

## Priority register and disposition

| Priority | Finding | Disposition |
| --- | --- | --- |
| P2 — performance | Advisor evidence title resolution had bounded but avoidable N+1 query fan-out. | **Fixed in CP134**; tenant/quarantine predicates retained; one-query regression passes. |
| P1 — evaluation coverage | OCR quality evidence has only 32 clean English synthetic cases without layout/language strata. | **Open; CP135** implements 60+ varied generated cases and runtime-gated strata. |
| P1 — retrieval assurance | Current RAG evaluation is lexical-only and below the requested 500-chunk/250-query scale. | **Open; CP139** adds scale and separately reported modes; no semantic pass may be inferred from lexical results. |
| P1 — evidence-grounded answers | Advisor structure is safe, but no live-provider semantic-quality result exists. | **Open; CP140** hardens evidence/freshness/abstention; provider evaluation remains external unless authorized and actually run. |
| P1 — complete operational reporting | Generic report rendering remains absent; DDR export coverage is not every report workflow. | **Open; CP144** validates PDF/XLSX through the product lifecycle. |
| P1 — integrated isolation | Existing CP131 browser pilot exercises one well. | **Open; CP150** implements the separate two-well pilot and readback. |
| External/field — not a code defect | Live vendor commissioning, recipient delivery, approved real OCR corpus, qualified field/engineering acceptance and production recovery objectives are not present as evidence. | **Blocked or not measured**; no synthetic or CI result will be relabeled as external acceptance. |

Historical failures remain attributed to their original SHAs. `CP134 DONE` means this source-level audit, the bounded Advisor query correction, and the listed focused/full software checks only; it is not a declaration that the follow-on phases or external acceptance are complete.
