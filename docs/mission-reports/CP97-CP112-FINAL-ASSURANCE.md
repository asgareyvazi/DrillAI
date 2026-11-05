# CP97–CP112 Final Assurance

## Scope and conclusion

This report records the repository-feasible implementation and evidence for CP97–CP111, the updated CP76–CP96 capability matrix, the daily-report governance record, and the CP112 release-certification requirement. The implementation preserves tenant isolation, authorization, immutable revision and source provenance, compare-and-swap behavior, safety limits, and honest evidence boundaries. The mission matrix and this report distinguish local software verification from exact-SHA CI and from evidence that requires real operators, approved corpora, external services, qualified engineering review, or production systems.

The CP110/111 candidate tree at exact SHA `43d6231e6b2c5929632a6d41e851bc0a2b23606c` passed the full local backend suites (PostgreSQL-enabled and default), frontend unit/type/lint/build gates, migration checks, inventory verification, Ruff, `compileall`, and the supply-chain/secret scan. Its Windows CI failure is recorded below and is not overwritten by the Linux result. The subsequent diagnostic-only follow-up adds sanitized Windows JUnit failure reporting; its CP32 contract test, changed-file Ruff, and `compileall` passed, but the follow-up's full suite is subject to its own exact-SHA run. The current E2E inventory is shape-verified; local Playwright was intentionally not run against preserved `.e2e` data. Exact-final-SHA Linux/native-Windows workflows, including their fresh-stack and real-browser gates, are the only certification of the commit containing this report. The final delivery message identifies that exact commit, run, and job IDs. This report is deliberately not amended after the run to claim certification for a different SHA.

## Mission disposition

| Mission | Implementation and evidence disposition |
| --- | --- |
| CP97 | Reconciled the existing audit and CI history against source and preserved repository state. This was a baseline review, not a certification of later code. |
| CP98 | Established one versioned, explicit unit contract across 14 engines, persisted results, APIs, workflows, reports, exports, and bilingual UI. Unknown units remain unknown; display conversion does not alter canonical values or hashes. `well_control.kill_sheet` and `tubulars.api_5c3` remain `needs_field_validation`. The exact-SHA Linux job passed; native-Windows E2E failed, with the failing assertion unavailable. |
| CP99 | Added bounded OCR runtime self-test and governed corpus-evaluation tooling. Synthetic tests do not establish OCR quality. No local Tesseract executable, approved real corpus, or adjudicated labels are available. CP99's exact-SHA Linux and Windows jobs passed, but that SHA does not certify later work. |
| CP100 | Completed multi-format drilling-daily-report extraction/review and source-locator propagation into human reconciliation. The original exact-SHA Windows browser job failed; a diagnostic-only descendant passed both platforms but did not explain or retroactively certify the original failure. |
| CP101 | Added source-linked activity classification and controlled references without inferred source facts. Its exact-SHA run failed the reviewed inventory gate because the manifest lagged one test; that SHA is not certified. |
| CP102 | Bound NPT duration/accounting to explicit source, approved revision, and fingerprint. The corrected-manifest exact-SHA run passed Linux but failed native-Windows SQLite tests; logs did not expose a cause. No descendant result retroactively certifies that SHA. |
| CP103 | Added read-only report-period integrity with explicit approved revision bounds. Exact-SHA Linux and native-Windows jobs passed, including browser E2E, on CP103 only. |
| CP104 | Added source-backed, immutable report-identity reconciliation with exact-path evidence and human decisions. Exact-SHA Linux and native-Windows jobs passed, including browser E2E, on CP104 only. |
| CP105 | Added same-date ordering only from explicit distinct period-start instants and retained unassessed barriers. The exact run was canceled before browser completion; it is not a pass. |
| CP106 | Added case-sensitive exact report-number search within existing tenant/well/wellbore scope. Both exact-SHA browser jobs failed on a strict-mode selector matching two rows; CP107 corrected the selector. |
| CP107 | Added wellbore-scoped list filtering and bilingual UI. The original exact-SHA Windows backend run failed with no recoverable node/root cause. A diagnostics-only descendant passed both platforms and browser E2E, but does not explain or retroactively certify the original SHA. |
| CP108 | Added composite organization/well/wellbore constraints with a preflight that refuses legacy mismatches instead of repairing data. The implementation SHA's native-Windows SQLite failure remains unexplained; diagnostics-only descendant `8456654` passed Linux and Windows; later docs-only descendant `c15f155` passed Linux but failed Windows SQLite. Neither descendant diagnoses or retroactively certifies the original implementation SHA. |
| CP109 | Added revision-scoped typed reconciliation targets and preserved the corrected fixture rule: start with five rows, remove the two invalid synthetic orphans after preflight refusal, then verify three remain. Exact-SHA run `41927953419` passed Linux job `126917235243` and native-Windows job `126917234959`, including both real-browser gates. |
| CP110 | Added tenant/document-scoped foreign keys for report evidence links to documents, extracted records, regions, and pages. Migration `d8f3b4c2a917` preflights legacy violations and refuses without inference or rewriting. The payload and frontend contract remain unchanged. |
| CP111 | Added same-report/tenant composite constraints for supersession and latest/latest-approved revision pointers. Migration `e2b4c6d8f013` preflights missing/cross-report pointers and refuses without repairing history. This is a source-backed gap selected from static code review; no CP111-specific acceptance definition was found in the checked mission/operator documents, so it is not described as a confirmed external requirement. |
| CP112 | Consolidates assurance, updates the matrix and certifies the report-containing exact SHA. The associated workflow's exact run/job identifiers and outcome are stated in the final delivery message; a green Linux and native-Windows result is required. |

Detailed implementation notes, caveats, and historical CI references for CP97–CP111 remain in [`CP76-CP96-CAPABILITY-MATRIX.md`](CP76-CP96-CAPABILITY-MATRIX.md). The run history is intentionally not collapsed into a single retrospective “passed” statement.

## CP110 and CP111 implementation detail

### CP110 — tenant- and document-scoped evidence

The ORM and migration bind declared evidence to the same organization and, when provided, the same document. A region or page requires a declared document; a record and region declared alongside a document must belong to it. Existing single-ID foreign keys remain. The service still performs report well/wellbore authorization, quarantine and validation-state checks, region/page checks, and human review. An otherwise documentless extracted record cannot be made to appear as evidence for an unrelated declared document.

Migration `d8f3b4c2a917` checks all affected source-link tables before DDL, emits aggregate violation counts, and refuses on cross-tenant, cross-document, region-without-document, or page-without-document legacy rows. The migration does not substitute, discard, or infer source evidence. SQLite migration fixtures verify refusal without version advancement, explicit fixture correction, successful upgrade, direct database rejection, and data-preserving downgrade/re-upgrade. The PostgreSQL migration-chain test exercises synthetic links on revisions, activities, measurements, and reconciliation rows.

### CP111 — same-report revision lineage

Composite unique targets support a self-FK from a correction's `supersedes_revision_id` to a revision under the same organization and report. Two deferrable composite FKs bind `latest_revision_number` and the nullable `latest_approved_revision_number` to revisions of that same report. The model marks both pointer constraints `use_alter=True` to resolve the mutually dependent table-order cycle while retaining SQLite enforcement; the migration and model/schema checks pass. Preflight counts missing latest/latest-approved targets and cross-report supersession links and fails before schema changes. No data is guessed or rewritten.

Because these pointers now have deferred integrity constraints, manual and import report creation insert the initial revision inside the same savepoint as the report. This keeps SQLite from validating a pointer when releasing a savepoint before its target revision has been inserted; an outer request failure still rolls back the import. Existing service checks continue to govern correction ancestry, revision CAS, authorization, approval, and lifecycle transitions. The database guarantees same-report/tenant target existence; it does not independently establish that an approved pointer names an `approved`-status revision, that a pointer is the maximum revision number, or that supersession is acyclic/chronologically valid. Those remain service-governed semantics. No public payload or frontend contract changed.

The migration and API fixtures are synthetic. No real report, operator decision, or engineering acceptance was used or inferred.

## Local verification

### Backend and database

- On candidate SHA `43d6231e6b2c5929632a6d41e851bc0a2b23606c`, the full PostgreSQL-enabled backend suite passed **1,680 passed, 0 skipped**, with one non-failing `RuntimeDirWarning` because `XDG_RUNTIME_DIR` is unset. `scripts/verify_backend_test_inventory.py --platform linux` accepted the result and verified the reviewed 14 PostgreSQL node IDs across 11 files.
- On that same candidate tree, the full default backend suite passed **1,666 passed, 14 PostgreSQL-only skips**, with zero failures. The inventory verifier accepted the exact default totals.
- The first full-suite run after adding CP110/CP111 found one CP16 documentation-count assertion (1,679 passed); the README, testing guide, exact-count guard, and manifest were reconciled to 1,680 tests / 123 backend test files, then both full suites passed. No tests were removed, skipped broadly, xfailed, or deselected to obtain a pass.
- The later diagnostic-only follow-up has not yet been run through the complete backend suite. Its existing CP32 CI-contract test, affected-file Ruff, and `compileall` passed locally; the exact follow-up Actions run is the authority for the entire committed tree.
- Daily-reports API module: **22 passed**. CP110/CP111 SQLite migration tests, the focused event-time/notification migration selections, and the PostgreSQL migration-chain test passed. PostgreSQL assertions directly reject invalid latest/latest-approved pointers and cross-report supersession; `alembic check` reported no drift.
- Full backend Ruff passed on the diagnostics-only follow-up. Full `compileall` and a fresh SQLite `alembic upgrade head`/`alembic check` passed on the candidate tree; Alembic reported “No new upgrade operations detected.” After the follow-up, `compileall` passed on the changed verifier/test files, and the CP32 diagnostic contract test passed. The follow-up's staged diff is checked before commit; the complete test result is determined by its exact-SHA workflow.

### Frontend, supply chain, and evidence boundaries

- Frontend Vitest: **388 passed across 38 files**. Typecheck, ESLint, and production build passed. Vite emitted its existing non-failing advisory for the >500 kB main JavaScript chunk.
- E2E inventory shape: **124 tests / 18 files** (Chromium 117, faults 4, auth 3). This is collection/shape verification, **not** real-browser execution. No local Playwright run used the preserved `.e2e` databases; the final Linux and native-Windows CI gates must execute the actual browser suites.
- `scripts/verify_supply_chain_and_secrets.py` passed on the freshly bootstrapped local environment: 9 full-SHA-pinned Actions, 542 scanned workspace text files, `pip-audit`, full frontend `npm audit --audit-level=moderate`, production `npm audit --omit=dev --audit-level=high`, and security-document checks. A previous CP108 local scan against a preserved `pip 23.0.1` environment had advisories; that historical finding is not erased. The current fresh environment's scan is a separate result.
- Local end-to-end browser, multi-process stack, Docker Compose, and vendor-harness outcomes are not inferred from unit tests or E2E shape. The exact-SHA workflow is authoritative for its own gates; its final run and jobs are identified in the completion message.

## Historical CI: preserved failures and exact-SHA scope

The following distinctions remain material and are also recorded in the capability matrix:

- CP98 run `41798295423`: Linux passed; native-Windows Playwright failed, and the exact assertion could not be recovered. It remains failed/unexplained.
- CP99 run `41802513722`: Linux and native Windows passed on the CP99 SHA only. It establishes no OCR-accuracy result.
- CP100 run `41810084277`: Linux passed; native-Windows Playwright failed. Diagnostic-only descendant run `41812967315` passed both platforms but did not explain or retroactively certify the original SHA.
- CP101 run `41815622464`: both platform inventory gates failed because the reviewed manifest lagged one test; later runs do not retroactively certify CP101.
- CP102 run `41818116645`: Linux passed including browser E2E; Windows SQLite tests failed and downstream gates were skipped. Logs did not reveal a cause.
- CP103 run `41822291067` and CP104 run `41829810555`: exact-SHA Linux/native-Windows jobs passed, including browser E2E, on their respective SHAs only.
- CP105 run `41834333302`: canceled before browser completion; cancellation is not success.
- CP106 run `41836351234`: both browser jobs failed on a strict-mode locator; CP107 corrected the selector.
- CP107 run `41842149924`: Windows backend SQLite failure, cause unknown. Diagnostic-only descendant run `41845191298` passed both platforms including E2E, without diagnosing or retroactively certifying the original SHA.
- CP108 run `41852513867` on `b6af66b`: Linux passed; Windows backend SQLite failed and the uploaded artifact could not be retrieved from the permitted egress environment. Diagnostics-only descendant `8456654` passed both platforms; docs-only descendant `c15f155` later passed Linux but failed Windows backend tests. Neither diagnoses or retroactively certifies `b6af66b`.
- CP109 run `41927953419` tested exact SHA `01b3d3e4815c71892ca255d5f97f313bad8394ad`; Linux job `126917235243` and native-Windows job `126917234959` both passed, including real-browser E2E.
- The first CP112 candidate, exact SHA `43d6231e6b2c5929632a6d41e851bc0a2b23606c`, was tested by run `42119610919`. Linux job `127568207993` passed all gates, including real-browser E2E. Native-Windows job `127568208534` failed the backend SQLite suite: **2 failed, 1,663 passed, 1 expected skip, 14 PostgreSQL deselections**; downstream Windows stack/vendor/inventory/browser gates were skipped. The failure summary contained no node IDs. Artifact `windows-backend-tests-42119610919-1` (ID `13458666551`) and the raw job log could not be retrieved because GitHub redirected to a storage host outside the permitted egress environment. The failure cause remains unknown; this SHA is not certified. The follow-up adds a temporary JUnit XML input to the sanitized inventory diagnostic so a subsequent failure can expose only stable test locations and exception classes, never assertion messages or values.

No unexplained failure is presented as fixed, no descendant pass is substituted for an earlier SHA, and no uncompleted/canceled gate is described as a pass.

## Safety, provenance, and external-evidence limits

- The platform remains advisory. Software calculations, report approval, and synthetic tests do not authorize operations. `well_control.kill_sheet` and `tubulars.api_5c3` remain `needs_field_validation`.
- Unknown engineering units stay explicit; no name-based unit inference is used. Stored canonical values and hashes are preserved. Survey compatibility is not inferred, and unknown/magnetic frames are not silently converted.
- Daily-report revisions and source links remain immutable/versioned and tenant-scoped. A browser/API success does not establish report correctness, real-document OCR quality, or a field fact.
- No approved real OCR corpus, labels, live OCR-quality evaluation, live LLM-quality result, external-vendor traffic, production RPO/RTO test, qualified engineering acceptance, field validation, end-user acceptance, or independent penetration test is claimed.
- SMTP/HTTP provider acceptance is not an end-recipient delivery receipt. No recipient identity or secret-reference detail is added to persisted errors; no raw SMTP/HTTP exception, request body, or secret is persisted.
- A shared injected `AsyncSession` test is not separate-worker concurrency evidence. No guessed rig endpoint or autonomous rig-control action is introduced.
- `RunMonitor.EventLog` does not substitute `created_at` for a missing source `occurred_at`; lifecycle closure does not establish `ended_at`.

## Exact-final-SHA record

This document is part of the commit submitted for final certification. The workflow's provenance step records that commit SHA and the Linux/native-Windows jobs run against it. The exact run and job IDs, conclusions, and final SHA are stated in the delivery message after the workflows finish; this document is not edited afterward to claim that a different SHA was certified. CP112 closes only if both required platform jobs and their required backend, frontend, inventory, supply-chain, stack, and real-browser gates succeed on that exact commit. Historical CI results above do not substitute.
