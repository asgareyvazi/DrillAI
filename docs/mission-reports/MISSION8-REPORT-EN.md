# Mission 8 — release recovery status

**NOT_ACCEPTED.** This is a partial engineering recovery, not a release, field qualification or full completion of the requested mission. Evidence below is synthetic software evidence, not a substitute for independent engineering, customer or vendor verification.

## Completed in this increment

- Re-audited event NPT interval eligibility: generic duration is no longer used as proof of an explicit NPT interval. The legacy recorded-row sum retains its prior fallback for compatibility and is explicitly labelled as potentially non-NPT and overlapping, distinct from interval union. Per-row hours source and provenance are visible in English and Persian; absent duration is null, not a measured zero. Naive or inverted reconciliation windows fail closed.
- Backend tests exercise persisted event behavior, including generic-only, explicit revision, and missing duration; UI component tests cover the source list and warnings. A synthetic real-browser journey creates an isolated project and well, writes events through the API, reads them back, and observes 4.5 recorded versus 3 union hours, then verifies an additional generic-only duration makes union unavailable. It does **not** establish a governed approval/report/revision workflow or field truth.
- Windows timeout diagnostic instrumentation and three independent targeted invocations were added without changing assertions, retries or timeouts. On diagnostic SHA `7acd3c75176dff556030573a51458a44bdce6a35`, targeted invocations passed but the mandatory full Windows frontend suite still timed out at 5 seconds in the same authoring test. The available annotation records stages 1–4 and stage 5 start; these incomplete fragments do not establish cause or justify a timeout change. Windows backend, stack and 129-browser-test run passed there; the offline vendor harness reports `network_calls_made=0`.
- Preserved historical case-level OCR metrics, start state, preliminary matrices and 32-case native-library exploratory data. No OCR quality change was deployed; native-library subset results are not comparable certification.

## Validation and blockers

The preceding diagnostic [CI run 45015175530](https://github.com/asgareyvazi/DrillAI/actions/runs/45015175530) failed on both platforms: Linux deployment-image OCR and full Windows frontend. OCR evaluated all 64 unchanged cases: global CER 0.1084, WER 0.1551, critical exact 308/384 = 0.8021; Persian CER 0.2295, WER 0.2934, critical 118/192. The required global and per-stratum gates are not met. Frozen dataset SHA-256 is `31aca80b4323fe470d34705e36038296ef620adb7525373f5196c9f561b6735d`.

Current local frontend typecheck/lint and 420 tests passed; NPT focused backend 55 passed; the isolated browser journey passed. The E2E shape check lists 130 tests across 20 files. Full local SQLite backend regression and new exact-final-SHA Linux/native Windows CI are separate gates and their results must be inspected; the prior run does not certify this SHA. End-to-end OCR deadline, renderer/layout isolation, Linux/Windows process-tree cleanup, and independent real-document holdout are not completed.

## Capability and evidence boundaries

Manual/import DDR, governed capacity, survey-linked ECD, uncertainty-aware anti-collision, structured program/procedure revision review and report, end-to-end NPT approvals, AI/RAG and telemetry have not all been independently completed and verified in this increment. The preliminary 30-item recovery and capability matrices are not an exhaustive historical audit. The 16-engine assurance file is a traceability inventory, not an independent re-audit or field validation. `well_control.kill_sheet` and `tubulars.api_5c3` remain `NEEDS_FIELD_VALIDATION`; proximity remains geometric only, not ISCWSA-compliant separation assurance.

Nine protected paths had hashes consistent with prior evidence but all had different mtimes **before** Mission 8 began. The start snapshot documents the observed state; no historical timestamp restoration is claimed. Four historical missing generated artifacts and original overwritten build bytes cannot be reconstructed. See baseline and manifests. External field evidence, customer authorization, an independent holdout and live vendor acceptance remain genuine blockers.

## Next work

Find the Windows full-suite timeout cause from complete native logs and fix without weakening tests; restore OCR accuracy against frozen deployed 64-case scoring and every stratum, then build a hard lifecycle deadline and process-tree termination on both OS. Complete NPT identity/review/revision/approval/report governance and real browser-to-DB tests. Independently audit and finish each engineering workflow and all 16 engines; verify authorized external evidence. Run all regressions and inspect both CI jobs on the exact final pushed SHA before any acceptance.
