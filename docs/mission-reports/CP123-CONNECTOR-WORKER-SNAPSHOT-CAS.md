# CP123 — Connector Worker Claim Snapshot CAS

## Disposition

| Item | Status | Evidence / boundary |
| --- | --- | --- |
| Reject stale connector snapshots at claim time | **DONE (software)** | Claim CAS now compares fencing/config versions and the state fields used for eligibility: status, owner, lease expiry and next-poll time. A regression test forces a status change between the eligibility read and conditional update and verifies no lease/token is granted. |
| Concurrent independent worker claims | **DONE (software test evidence)** | The existing two-worker race uses separately constructed workers and independent database sessions; it verifies one winner and checks the committed owner/token. This is in-process concurrency evidence, not a separate-OS-process stress or production-scale result. |
| Production concurrency/availability | **PARTIAL** | No deployment, production load, or multi-host failure exercise was performed; repository tests are not operational-scale evidence. |

## Implementation

`ConnectorWorker.claim_connector` read the row, checked eligibility, then issued an update guarded by connector ID, fencing token, config version, enabled state and desired state. The claim CAS now also compares the values observed for status, current worker ID, lease expiry and `next_poll_at`. If another transaction changes one of these between read and update, the affected-row count is zero and the claim returns `None`; it cannot overwrite the newer state with a stale owner/token.

An optional internal `before_claim_cas_hook` follows the worker's existing injected poll-commit hook pattern and enables deterministic race simulation in tests. It is not exposed as an API or persisted configuration.

The existing competing-worker test verifies that the winner's worker ID, lease and fencing token are the values actually persisted. The new regression changes the status after the first read without advancing the fencing/config versions; the claim must be refused. This specifically tests the additional status snapshot predicate rather than only the fencing-token check.

## Verification

- Connector worker, WITSML/ETP worker race and fenced-ledger selection: **7 passed, 2 PostgreSQL-marked tests skipped by the default local run**; with `DRILLAI_TEST_POSTGRES=1`, the same selection passed **9 tests**, including both PostgreSQL race tests.
- Backend Ruff and `compileall` passed for changed files; `git diff --check` passed.
- Backend collection increased by one to **1,711 tests across 128 files**. Inventory expectations are 1,711 Linux, 1,697 default plus 14 PostgreSQL skips, and 1,696 Windows plus the identified POSIX-only OCR skip and 14 PostgreSQL deselections. The 14 PostgreSQL node IDs across 11 files remain listed in the manifest.
- No full backend suite, PostgreSQL-enabled suite, multi-process stack, production deployment or exact-final-SHA CI is claimed here.

## Limits

This strengthens the SQL compare-and-swap invariant; it does not prove database failover, network partitions, multi-host scheduling fairness, production throughput or recovery objectives. The hook-driven regression is deterministic software evidence, not operational concurrency qualification. The later CP132 exact-final-SHA Linux/native-Windows certification is recorded in `CP132-FINAL-RELEASE-ASSURANCE.md`; deployment and field evidence remain separate.
