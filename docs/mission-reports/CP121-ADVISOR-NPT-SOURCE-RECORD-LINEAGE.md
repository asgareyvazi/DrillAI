# CP121 — Advisor NPT Source-Record and Evidence-Lineage Completion

## Disposition

| Capability | Status | Boundary |
| --- | --- | --- |
| Carry source-record identity in NPT cases | **DONE (software)** | `NptEvent` now retains the existing `source_record_id` from its event or operation row through the NPT summary API and Advisor provenance. No new identifier is inferred. |
| Surface attached evidence for Advisor NPT cases | **DONE (software)** | The Advisor's evidence section resolves the event/operation source IDs against existing tenant-scoped `EvidenceLink` rows, subject to quarantine checks. Missing links stay missing. |
| External source or field correctness | **NOT ESTABLISHED** | Tests use the repository's synthetic DDR fixture; this does not establish the correctness of any real NPT classification, duration, or field observation. |

## Implementation

The NPT `NptEvent` contract already carried the event/operation ID, optional direct evidence reference and source document. It omitted the domain row's `source_record_id`, even though both `Event` and `Operation` persist it. CP121 adds that field to the dataclass, `to_dict()`, both row-to-case mappings, and the typed frontend `NptCase` contract. The Advisor copies the ID into each case fact's provenance.

The case fact's `source_id` remains the actual NPT event/operation ID. The `source_record_id` is a distinct pointer back to the extracted source row; it is not substituted for the event ID or fabricated as an `evidence_ref`. Existing `EvidenceLink` records are surfaced separately in the Advisor evidence section, where each item retains its linked document, page/excerpt locator and event/operation subject identity. Tenant and quarantine filters remain in the evidence query.

## Verification

- The CP121 Advisor/API/state-freshness/telemetry-latest selection passed **64 tests**, including the DDR-backed NPT summary and Advisor answer checks. Tests verify both event/source-record identity and the attached event evidence's document and subject.
- Backend Ruff and `compileall` passed for all changed backend files; `git diff --check` passed.
- Frontend typecheck and lint passed; **20 tests across four engineering workspace files** passed.
- No new test case was added, so the reviewed backend inventory remains **1,710 collected tests across 128 files**, with 14 PostgreSQL node IDs across 11 files. Inventory validation was performed after CP120; a final full suite and final-SHA inventory gate remain part of CP132.

## Limits

A source-record link proves lineage to a stored extraction proposal, not that the proposal is correct or approved as a physical fact. An evidence link proves an attachment relationship, not independent adjudication. No live OCR/RAG/provider or external NPT data was used; no field/engineering acceptance is claimed. Exact-final-SHA Linux and native-Windows certification remains a CP132 gate.
