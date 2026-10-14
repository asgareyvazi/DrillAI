# DrillAI CP15 Mission Report — CP14 Release Closure, Durable Egress Recovery, Fail-Closed Reconciliation, DNS Executor Resilience & Full Production Certification

## 1. Executive Summary

Checkpoint 15 (`CP15`) completes the end-to-end hardening, resilience verification, and full-stack certification of the DrillAI industrial telemetry connector platform on branch `arena/01a0dca0-drillai` (`main` untouched at `bfa066b28e0071880cb9191a4f1d47fdaa143e04`):

1. **Gate 0 (`CP14-H`) Release Closure:** Reconciled the local CP14-H documentation commit (`9b67ac5aee93240b2e3dab652432f2ea61e38db0`, parent `b8c8ec5899bf94293f875d8c2e87d8e14115b9c2`), published it to `refs/heads/arena/01a0dca0-drillai`, verified via `git ls-remote`, and certified the exact SHA on GitHub Actions CI Run **`38255799539`** (`conclusion: success` in `23m56s`).
2. **Source-Backed Adversarial Audit (`CP15-A`):** Audited the published `CP14-H` baseline and catalogued 10 findings (`F-CP15-01` through `F-CP15-10`) in `docs/mission-reports/CP15-SOURCE-AUDIT.md`.
3. **Durable Egress-Policy Recovery State Machine (`CP15-B`):** Replaced the sticky `_egress_policy_store_failed` boolean in `backend/src/drillai/telemetry/connectors.py` with an explicit recovery state machine (`EGRESS_POLICY_STATES`) that remains strictly fail-closed during DB outages or corrupt/inactive rows, automatically recovers in the same OS process when subsequent DB reads succeed, allows governed admin repair via `PUT /api/v1/connectors/egress-policy`, and never falls back to `env_default` once a database policy has been observed.
4. **Fail-Closed Manifest Reconciliation Transaction Invariant (`CP15-C`):** Enforced `reconciled == True` as a hard transaction commit gate across `ConnectorWorker.execute_claimed_poll()`, `ConnectorService.replay()`, `ConnectorService.preview()`, and `ConnectorService.test_connection()`. Any count mismatch, negative count, boolean count, or per-channel sum discrepancy rolls back `TimeSeriesPoint` inserts/revisions, preserves `cursor_before`, and records a durable diagnostic `ConnectorRun(status="failed", error_category="reconciliation_error")` with `data_outcome="reconciliation_error"`, `persisted=False`, and `rolled_back=True`.
5. **11-State `data_outcome` Truth Table & Dry-Run Non-Persistence (`CP15-D`):** Formalized `POLL_DATA_OUTCOMES` (`reconciliation_error`, `dry_run_preview`, `no_data`, `rejected_only`, `partial_with_rejections`, `partial_batch_capped`, `filtered_only`, `revisions_and_duplicates`, `revisions_only`, `duplicates_only`, `all_valid_accepted`) and companion manifest flags (`filtered_present`, `revised_present`, `duplicates_present`, `persisted`, `dry_run`, `rolled_back`).
6. **DNS Executor Exact-Once Permit Lifecycle & Self-Healing Pool Rotation (`CP15-E`):** Implemented `_DnsPermitGuard` (`release_once()`), `_submit_dns_lookup_task()`, `_handle_dns_future_abort()`, and `_rotate_dns_executor_locked()` with bounded quarantine (`_DNS_MAX_QUARANTINED_THREADS = 16`), guaranteeing zero permit leaks when queued or running DNS futures time out or cancel and recovering automatically when worker threads are wedged in synchronous `socket.getaddrinfo()`.
7. **System-Scope Authorization, Policy Privacy & Pre-Connect Worker Fencing (`CP15-F`):** Added `org_admin` and explicit `system.egress_policy.manage` / `system.egress_policy.read` permissions, restricted `PUT /api/v1/connectors/egress-policy` to platform-scope administrators, redacted private host/CIDR details for cross-tenant viewers via `redact_egress_policy_for_principal()`, awaited all `_record_fenced_outcome()` calls, and enforced pre-connect `lease_expires_at <= now` checks in `ConnectorWorker.execute_claimed_poll()`.
8. **Protocol Conformance, Bilingual Operator UI & Multi-Process Soak (`CP15-G` & `CP15-H`):** Synchronized per-channel counters and `prev_channel_recent` on ETP 1.2 `TruncateChannels`, implemented `_IndependentSchemaAvroVerifier` against 12 official Energistics ETP v1.2 `.avsc` schemas and 7 golden wire vectors, surfaced all 11 `data_outcome` states, persistence/rollback badges, egress policy state/redaction badges, and stale-worker-policy warnings in EN/FA (`ConnectorsPage.tsx`, `OperationalMonitor.tsx`), added real-stack Playwright Journeys 40–42 (`119` E2E tests total), and expanded `scripts/verify_stack.py` to 16 multi-process scenarios plus a 32-cycle bounded soak.

---

## 2. Gate 0 (`CP14-H`) Reconciliation & CI Closure Proof

| Item | Evidence |
| --- | --- |
| **Prior Published Remote Tip (`CP14-G`)** | `b8c8ec5899bf94293f875d8c2e87d8e14115b9c2` (certified on GitHub Actions Run `38230898896`, `conclusion: success` in `30m25s`) |
| **Reconciled & Published `CP14-H` Commit SHA** | `9b67ac5aee93240b2e3dab652432f2ea61e38db0` (`docs(cp14): finalize CP14 source audit, mission report, and verification ledger (CP14-H)`) |
| **`git ls-remote origin refs/heads/arena/01a0dca0-drillai`** | `9b67ac5aee93240b2e3dab652432f2ea61e38db0 refs/heads/arena/01a0dca0-drillai` |
| **`git ls-remote origin refs/heads/main`** | `bfa066b28e0071880cb9191a4f1d47fdaa143e04 refs/heads/main` (untouched) |
| **GitHub Actions CI Run ID & URL** | Run **`38255799539`** (`https://github.com/asgareyvazi/DrillAI/actions/runs/38255799539`, Job ID `114833418775`) |
| **Exact `head_sha` Under Test** | `9b67ac5aee93240b2e3dab652432f2ea61e38db0` |
| **Conclusion & Duration** | `completed` / `success` (`23m56s`, all 10 verification gates green, 0 failed, 0 skipped) |

---

## 3. CP15 Source Audit Findings & Fixes

All 10 findings from `docs/mission-reports/CP15-SOURCE-AUDIT.md` have been resolved and verified by regression tests:

| Finding ID | Severity | Summary | Resolution & Checkpoint SHA |
| --- | --- | --- | --- |
| **F-CP15-01** | **P0** | Sticky `_egress_policy_store_failed` permanently locked out DB policy reads and admin repairs after a single transient DB error | **FIXED (`6ecb6b372acf9e3fd73e636d883fb69af7f45e9c` — CP15-B):** Explicit `EGRESS_POLICY_STATES` state machine with automatic same-process recovery on valid DB read and admin repair support via `update_egress_policy_in_db()`. |
| **F-CP15-02** | **P0** | `reconciled=False` in `execute_claimed_poll()` and `replay()` still committed `TimeSeriesPoint` rows, advanced `Connector.cursor`, and recorded `ConnectorRun(status="succeeded")` | **FIXED (`d02508f8fd524713c338149b012a724b8b5eb55f` — CP15-C):** Fail-closed transaction rollback on `reconciled=False`, preserving `cursor_before` and recording `ConnectorRun(status="failed", error_category="reconciliation_error")`. |
| **F-CP15-03** | **P1** | `compute_poll_data_outcome()` lacked `reconciliation_error`, `dry_run_preview`, and `revisions_and_duplicates` states and companion presence flags | **FIXED (`d02508f8fd524713c338149b012a724b8b5eb55f` — CP15-D & `84a3fff` — CP15-G1):** Formal 11-state `POLL_DATA_OUTCOMES` truth table and companion flags (`filtered_present`, `revised_present`, `duplicates_present`, `persisted`, `dry_run`, `rolled_back`). |
| **F-CP15-04** | **P0** | Queued DNS futures cancelled before execution leaked `_DNS_SEMAPHORE` permits and wedged worker threads had no pool rotation | **FIXED (`63ecebbf09c1b39b734ede2e43b83e1bafd268e6` — CP15-E):** Exact-once `_DnsPermitGuard` (`release_once()`) and generation-scoped `_rotate_dns_executor_locked()` with bounded quarantine (`_DNS_MAX_QUARANTINED_THREADS = 16`). |
| **F-CP15-05** | **P1** | `PUT /api/v1/connectors/egress-policy` lacked explicit system permission & platform-org check; `GET` exposed unredacted hosts/CIDRs to cross-tenant viewers | **FIXED (`f1384f44e97762470341dee42e4f47ef1d6cbb5d` — CP15-F):** Added `org_admin`, `system.egress_policy.manage`, `system.egress_policy.read`, `_authorize_system_egress_policy_update()`, and `redact_egress_policy_for_principal()`. |
| **F-CP15-06** | **P1** | Unawaited `self._record_fenced_outcome(...)` in `worker.py` pre-connect checks and missing pre-connect `lease_expires_at <= now` check | **FIXED (`f1384f44e97762470341dee42e4f47ef1d6cbb5d` — CP15-F):** Awaited all `_record_fenced_outcome()` calls and enforced pre-connect `lease_expires_at <= now` fencing. |
| **F-CP15-07** | **P1** | ETP 1.2 `TruncateChannels` left `per_source_channel` out of sync with global counters and `test_cp14_etp_binary_normative.py` lacked a schema-driven Avro datum verifier | **FIXED (`f1384f44e97762470341dee42e4f47ef1d6cbb5d` & `eaaa29d74d6ece9da6e9e2799cca0e69bdc8dcde` — CP15-G / CP15-H1):** Synchronized `per_source_channel` and `prev_channel_recent` on `TruncateChannels`; added `_IndependentSchemaAvroVerifier` over 12 official ETP v1.2 `.avsc` schemas and 7 golden vectors. |
| **F-CP15-08** | **P1** | UI did not render connection-time `manifest-policy-revision`, persistence/rollback badge, egress `policy_state`, or stale worker policy badge | **FIXED (`84a3fff2dbbed907f808a8bb2b308c95b0890c8f` & `ced758947d3b267454118e67d1b6e73dc427ae58` — CP15-G1 / CP15-G2):** Rendered all CP15 badges and states in `ConnectorsPage.tsx` and `OperationalMonitor.tsx` with full EN/FA parity and Playwright Journeys 40–42. |
| **F-CP15-09** | **P1** | `scripts/verify_stack.py` did not cover CIDR revocation, same-process policy store recovery, `reconciled=False` rollback, DNS stress, or bounded soak | **FIXED (`eaaa29d74d6ece9da6e9e2799cca0e69bdc8dcde` — CP15-H1):** Expanded `scripts/verify_stack.py` to 16 multi-process scenarios + 32-cycle soak. |
| **F-CP15-10** | **P2** | Local verification in CP14-G used a `-g` Playwright filter before CI | **FIXED (CP15-H2):** Executed the full unfiltered 119-test Playwright E2E suite locally (`npm run e2e`) as well as on GitHub Actions CI. |

---

## 4. Durable Egress-Policy Recovery State Machine Matrix (`backend/tests/telemetry/test_cp15_egress_policy_recovery.py`)

| Scenario | Description | Expected State Transition & Enforcement | Result |
| --- | --- | --- | --- |
| `1` | Initial startup with empty DB table (`no database policy ever loaded`) | `policy_state="active"`, `policy_health_state="healthy_env_default"`, `source="env_default"` | **PASSED** |
| `2` | Valid `system` policy row in `connector_egress_policies` (`revision >= 1`) | `policy_state="active"`, `policy_health_state="healthy_db_synced"`, `source="database"` | **PASSED** |
| `3` | Transient DB read error during `sync_egress_policy_from_db()` | Transitions to `policy_state="transient_store_error"`, `status="unavailable_fail_closed"`; private/allowlisted hosts fail closed (`ssrf_blocked`) | **PASSED** |
| `4` | Subsequent successful DB read after transient DB outage in the same OS process | Automatically recovers to `policy_state="active"`, `source="database"`, sets `recovered_at`, and re-enables approved hosts/CIDRs without restart | **PASSED** |
| `5` | Corrupt hostname or CIDR (`not-a-cidr`) stored in `connector_egress_policies` | Transitions to `policy_state="corrupt_policy_record"`, `status="unavailable_fail_closed"`; remains fail-closed across repeated reads | **PASSED** |
| `6` | Inactive policy row (`status="disabled"` / non-active) or `revision < 1` | Transitions to `policy_state="inactive_policy"` / `corrupt_policy_record`; fails closed | **PASSED** |
| `7` | Missing `system` row after a `database` policy was previously loaded in-process (`_ever_synced_db_policy=True`) | Transitions to `policy_state="corrupt_policy_record"` (`missing_policy_row`); refuses to silently downgrade to `env_default` | **PASSED** |
| `8` | Governed admin `update_egress_policy_in_db()` repairing a corrupt or inactive row | Validates new payload, increments revision, commits, and restores `policy_state="active"` in-process | **PASSED** |
| `9` | Uncommitted `update_egress_policy_in_db()` rolled back by caller transaction | In-process snapshot is NOT updated until `publish_committed_egress_policy_snapshot()` runs after `session.commit()` | **PASSED** |
| `10` | Forced test failure (`set_egress_policy_store_failure_for_test(True)`) vs `reset_egress_policy()` | Forced test override stays isolated from live DB recovery and clears cleanly on `False` / `reset_egress_policy()` | **PASSED** |
| `11` | `ConnectorWorker` polling during transient policy-store outage then recovering on next cycle | Poll 1 fails closed (`ssrf_blocked`); Poll 2 recovers `policy_state="active"` and ingests telemetry without worker restart | **PASSED** |
| `12` | Real PostgreSQL (`@pytest.mark.postgres`) + separate subprocess worker recovery across policy revision updates & transient store fault | Subprocess worker syncs revisions from PostgreSQL, fails closed on fault, and recovers cleanly | **PASSED** |

---

## 5. Fail-Closed Manifest Reconciliation & Transaction Rollback Matrix (`backend/tests/telemetry/test_cp15_reconciliation_and_outcomes.py`)

| Scenario | Injected Discrepancy / Execution Path | Telemetry DB Writes | Cursor State | Recorded `ConnectorRun` / Result | Result |
| --- | --- | --- | --- | --- | --- |
| `1` | `source_records_discovered != mapped_frames_emitted + unmapped_skipped + watermark_suppressed + total_protocol_rejected` | Rolled back (`0` persisted) | Unchanged (`cursor_before`) | `status="failed"`, `error_category="reconciliation_error"`, `data_outcome="reconciliation_error"` | **PASSED** |
| `2` | `mapped_frames_emitted != frames_received` | Rolled back (`0` persisted) | Unchanged (`cursor_before`) | `status="failed"`, `error_category="reconciliation_error"` | **PASSED** |
| `3` | `frames_received != points_accepted + points_revised + points_duplicates + points_rejected` | Rolled back (`0` persisted) | Unchanged (`cursor_before`) | `status="failed"`, `error_category="reconciliation_error"` | **PASSED** |
| `4` | `total_protocol_rejected != sum(rejection breakdown counters)` | Rolled back (`0` persisted) | Unchanged (`cursor_before`) | `status="failed"`, `error_category="reconciliation_error"` | **PASSED** |
| `5` | Negative counter in `ProtocolPollDisposition` (`unmapped_skipped = -1`) | Rolled back (`0` persisted) | Unchanged (`cursor_before`) | `reconciled=False`, `non_negative_counts_ok=False` | **PASSED** |
| `6` | Boolean counter passed as integer (`points_accepted = True`) | Rolled back (`0` persisted) | Unchanged (`cursor_before`) | Rejected by `_is_non_negative_int()` (`isinstance(v, bool)` check) | **PASSED** |
| `7` | Per-source-channel `discovered` sum disagrees with global `source_records_discovered` | Rolled back (`0` persisted) | Unchanged (`cursor_before`) | `reconciled=False`, `per_channel_protocol_ok=False` | **PASSED** |
| `8` | Per-source-channel internal identity (`discovered != emitted + unmapped + watermark + rejected`) | Rolled back (`0` persisted) | Unchanged (`cursor_before`) | `reconciled=False`, `per_channel_protocol_ok=False` | **PASSED** |
| `9` | Per-channel `IngestReport` sum disagrees with global `AdapterIngestReport` | Rolled back (`0` persisted) | Unchanged (`cursor_before`) | `reconciled=False`, `per_channel_ingest_ok=False` | **PASSED** |
| `10` | `ConnectorWorker.execute_claimed_poll()` with new valid points when `reconciled=False` | Rolled back (`TimeSeriesPoint` count unchanged) | `Connector.cursor == cursor_before` | `Connector.status="backing_off"`, `ConnectorRun(status="failed", error_category="reconciliation_error")` | **PASSED** |
| `11` | `ConnectorWorker.execute_claimed_poll()` with revised points when `reconciled=False` | Rolled back (prior `TimeSeriesPoint.value_si` & `revision` intact) | `Connector.cursor == cursor_before` | `ConnectorRun(status="failed")`, `rolled_back=True`, `persisted=False` | **PASSED** |
| `12` | Subsequent healthy poll after a `reconciled=False` rollback | Commits cleanly (`points_accepted > 0`) | Advances from `cursor_before` to `new_cursor` | `Connector.status="running"`, `data_outcome="all_valid_accepted"` | **PASSED** |
| `13` | `ConnectorService.replay(dry_run=False)` when `reconciled=False` | Rolled back (`0` persisted) | Unchanged (`cursor_before`) | Raises `ValidationFailed` / records failed replay run with `reconciliation_error` | **PASSED** |
| `14` | `ConnectorService.replay(dry_run=True)` when `reconciled=False` | `0` persisted (`dry_run=True`) | Unchanged (`cursor_before`) | `reconciled=False`, `data_outcome="reconciliation_error"` | **PASSED** |
| `15` | `ConnectorService.preview()` when `reconciled=False` | Non-persistent preview | Unchanged | Raises `ConnectorTransportError(category="reconciliation_error")` | **PASSED** |
| `16` | `ConnectorService.test_connection()` when `reconciled=False` | Non-persistent test | Unchanged | `ok=False`, `error_category="reconciliation_error"`, stage `sample_frame_decoded` failed | **PASSED** |
| `17` | `ConnectorService.replay(dry_run=True)` when `reconciled=True` and valid frames exist | `0` persisted (`TimeSeriesPoint` count unchanged) | Unchanged (`cursor_before`) | `data_outcome="dry_run_preview"`, `persisted=False`, `dry_run=True` | **PASSED** |
| `18` | `ConnectorService.replay(dry_run=False)` when `reconciled=True` and valid frames exist | Persisted (`TimeSeriesPoint` rows committed) | Monotonically updated | `data_outcome="all_valid_accepted"`, `persisted=True`, `dry_run=False` | **PASSED** |
| `19` | Real PostgreSQL (`@pytest.mark.postgres`) transaction rollback on `reconciled=False` | Zero leaked `TimeSeriesPoint` rows in PostgreSQL | `Connector.cursor` unchanged in PostgreSQL | `ConnectorRun` persisted with `status="failed"` and `data_outcome="reconciliation_error"` | **PASSED** |

---

## 6. `data_outcome` Truth Table & Dry-Run Non-Persistence Verification

`POLL_DATA_OUTCOMES` (`backend/src/drillai/telemetry/connectors.py`) defines 11 canonical outcomes evaluated in strict deterministic precedence order:

| Precedence | `data_outcome` | Governing Predicate | `persisted` | EN UI Label (`frontend/src/i18n/en.ts`) | FA UI Label (`frontend/src/i18n/fa.ts`) |
| --- | --- | --- | --- | --- | --- |
| `1` | `reconciliation_error` | `reconciled is False` | `False` (`rolled_back=True`) | `Reconciliation error (rolled back, 0 persisted)` | `خطای تطبیق مانیفست (تراکنش بازگردانی شد؛ ۰ نقطه ذخیره شد)` |
| `2` | `dry_run_preview` | `dry_run is True` and `source_records_discovered > 0` and `total_rejected == 0` and `mapped_frames_emitted > 0` | `False` (`dry_run=True`) | `Dry-run preview (not persisted)` | `پیش‌نمایش آزمایشی (ذخیره نشده)` |
| `3` | `no_data` | `source_records_discovered == 0` and `frames_received == 0` | `True` (poll) / `False` (dry-run) | `No new source records` | `بدون رکورد جدید در مبدأ` |
| `4` | `rejected_only` | `total_rejected > 0` and `accepted == 0` and `revised == 0` and `duplicates == 0` | `True` | `Rejected only (0 accepted)` | `فقط ردشده (بدون پذیرش)` |
| `5` | `partial_with_rejections` | `total_rejected > 0` and `(accepted > 0 or revised > 0 or duplicates > 0)` | `True` | `Partial: accepted with rejections` | `جزئی: پذیرش همراه با موارد ردشده` |
| `6` | `partial_batch_capped` | `has_more_deferred or truncated_by_max_points` (`checkpoint_status == "partial"`) | `True` | `Partial: batch cap reached (more pending)` | `جزئی: سقف دسته پر شد (ادامه در چرخهٔ بعد)` |
| `7` | `filtered_only` | `accepted == 0` and `revised == 0` and `duplicates == 0` and `(unmapped_skipped + watermark_suppressed) > 0` | `True` | `Filtered only (unmapped/unsubscribed, 0 accepted)` | `فقط فیلترشده (بدون نگاشت/اشتراک، ۰ پذیرش)` |
| `8` | `revisions_and_duplicates` | `accepted == 0` and `revised > 0` and `duplicates > 0` | `True` | `Revisions & duplicates only (0 new points)` | `فقط اصلاحیه‌ها و داده‌های تکراری (۰ نقطه جدید)` |
| `9` | `revisions_only` | `accepted == 0` and `revised > 0` and `duplicates == 0` | `True` | `Revisions only (existing points updated)` | `فقط اصلاحیه (به‌روزرسانی نقاط موجود)` |
| `10` | `duplicates_only` | `accepted == 0` and `revised == 0` and `duplicates > 0` | `True` | `Duplicates only (0 newly accepted)` | `فقط تکراری (بدون پذیرش جدید)` |
| `11` | `all_valid_accepted` | `accepted > 0` and `total_rejected == 0` and not capped (companion flags `filtered_present`, `revised_present`, `duplicates_present` explicitly record any filtered/revised/duplicate records alongside accepted points) | `True` | `All valid points accepted` | `پذیرش کامل نقاط معتبر` |

---

## 7. DNS Executor Saturation, Timeout, Cancellation & Recovery Matrix (`backend/tests/telemetry/test_cp15_dns_executor_resilience.py`)

| Scenario | Description | Verified Invariant | Result |
| --- | --- | --- | --- |
| `1` | Normal synchronous & async DNS resolution (`resolve_and_pin_endpoint`, `resolve_and_pin_endpoint_async`) | Permit acquired once and released once (`available_permits == 8`, `permits_in_use == 0`) | **PASSED** |
| `2` | `socket.gaierror` / DNS lookup failure | Raises `ValidationFailed(reason="dns_resolution_failed")`; permit released once (`available_permits == 8`) | **PASSED** |
| `3` | Single lookup timeout (`dns_timeout_seconds` exceeded) | Raises `ValidationFailed(reason="timeout")`; permit released exactly once when worker finishes or rotates | **PASSED** |
| `4` | Queued lookup cancelled **before** worker thread starts (`fut.cancel()` succeeds on queued future) | `_handle_dns_future_abort()` releases permit immediately; zero permit leak (`available_permits == 8`) | **PASSED** |
| `5` | Caller task cancelled **while** worker thread is executing `socket.getaddrinfo()` | Re-raises `asyncio.CancelledError` to caller; worker completion releases permit exactly once via `_DnsPermitGuard.release_once()` | **PASSED** |
| `6` | Executor submission failure (`executor.submit` raises `RuntimeError`) | `_submit_dns_lookup_task()` catches submission error and calls `permit_guard.release_once()` immediately | **PASSED** |
| `7` | Semaphore acquisition timeout when all 8 permits are held | Fails fast with `ValidationFailed(reason="timeout")` (`semaphore_timeouts` incremented) without blocking event loop | **PASSED** |
| `8` | All 4 worker threads wedged in slow `getaddrinfo()` beyond timeout | `_rotate_dns_executor_locked()` replaces `_DNS_EXECUTOR` (incrementing `executor_generation` and `executor_replacements`) so subsequent fast lookups succeed immediately | **PASSED** |
| `9` | Quarantined thread cap (`_DNS_MAX_QUARANTINED_THREADS = 16`) | Refuses unbounded OS thread creation when `quarantined_threads >= 16`; recovers automatically once wedged threads exit | **PASSED** |
| `10` | Double-release prevention (`_DnsPermitGuard.release_once()` called from both abort handler and worker `finally`) | `available_permits` never exceeds `_DNS_MAX_CONCURRENCY` (`8`) | **PASSED** |
| `11` | 32-task concurrent mix of fast, slow-timeout, `gaierror`, and caller-cancelled lookups | Event loop stays responsive; after completion `available_permits == 8` and `permits_in_use == 0` | **PASSED** |
| `12` | `WitsmlSoapClient` under DNS timeout and subsequent recovery | Fails closed with `ConnectorTransportError(category="dns_failure")` during timeout, then succeeds on next poll | **PASSED** |
| `13` | `EtpWebSocketClient` under DNS timeout and subsequent recovery | Fails closed with `ConnectorTransportError(category="dns_failure")`, then succeeds on next poll | **PASSED** |
| `14` | `EtpBinaryWebSocketClient` under DNS timeout and subsequent recovery | Fails closed with `ConnectorTransportError(category="dns_failure")`, then succeeds on next poll | **PASSED** |
| `15` | `ConnectorService.test_connection()` & `preview()` under DNS timeout | Records failed `dns_resolved` milestone (`category="dns_failure"`) with zero leaked permits | **PASSED** |
| `16` | `ConnectorService.replay()` under DNS timeout and subsequent retry | Fails closed without advancing cursor, then succeeds cleanly once DNS recovers | **PASSED** |

---

## 8. System-Scope Authorization, Policy Privacy & PostgreSQL Worker Fencing Matrix (`backend/tests/telemetry/test_cp15_system_auth_and_worker_fencing.py`)

| Scenario | Description | Result |
| --- | --- | --- |
| `1` | `viewer`, `operator`, `engineer`, `well_manager`, and `org_admin` attempting `PUT /api/v1/connectors/egress-policy` | **PASSED** (`403 Forbidden` — `security.permission_denied`) |
| `2` | Principal with `"admin"` role inside a non-platform tenant organization (`Organization(kind="tenant")`) attempting `PUT /api/v1/connectors/egress-policy` | **PASSED** (`403 Forbidden` — rejected by `_authorize_system_egress_policy_update()`) |
| `3` | Platform `admin` in platform organization updating `PUT /api/v1/connectors/egress-policy` with `Idempotency-Key` and `expected_revision` | **PASSED** (`200 OK`, revision incremented, idempotent replay returns cached response, conflicting payload with same key returns `409`, stale `expected_revision` returns `409`) |
| `4` | Cross-tenant `viewer` calling `GET /api/v1/connectors/egress-policy` and `GET /api/v1/connectors/profiles` | **PASSED** (`redacted: true`, `allowed_hosts: []`, `allowed_private_cidrs: []`, counts & revision preserved, `updated_by_id: null`) |
| `5` | Same-org `viewer` / `engineer` / platform `admin` calling `GET /api/v1/connectors/egress-policy` | **PASSED** (full `allowed_hosts` and `allowed_private_cidrs` returned with `redacted: false`) |
| `6` | Duplicate in-process connector dispatch (`claimed.connector_id in worker._active_connectors`) | **PASSED** (`await self._record_fenced_outcome(...)` returns `PollCycleOutcome(status="fenced")` and records `ConnectorRun(status="fenced")`) |
| `7` | Pre-connect lease expiry (`row.lease_expires_at <= now_phase1`) or `config_version` / `fencing_token` mismatch before `adapter.connect()` | **PASSED** (aborts before network I/O and awaits `_record_fenced_outcome(...)` with `poll_policy_revision`) |
| `8` | Embedded PostgreSQL (`@pytest.mark.postgres`) concurrent competing workers + mid-poll lease expiry & config update fencing | **PASSED** (stale worker fenced with `0` duplicate telemetry writes and bounded `ConnectorRun(status="fenced")` ledger entry) |

---

## 9. Protocol & Golden-Vector Conformance Matrix

| Profile Key | Protocol Standard | Wire Encoding | Status & Honesty Classification | Conformance & Provenance Verification |
| --- | --- | --- | --- | --- |
| `witsml.1.4.1.1.soap_http` | Energistics WITSML v1.4.1.1 (`http://www.witsml.org/message/120`, `http://www.witsml.org/schemas/1series`) | SOAP 1.1 XML over HTTP/HTTPS (`WMLS_GetVersion`, `WMLS_GetCap`, `WMLS_GetFromStore`) | `production` (`internal_only: false`, `conformance_verified: true`, `independent_peer_verified: false`, `external_vendor_verified: false`) | 14-scenario WITSML 1.4.1.1 suite (`test_cp13_witsml_and_worker_races.py`) + 19-scenario reconciliation suite (`test_cp15_reconciliation_and_outcomes.py`) |
| `etp.1.2.binary_ws` | Energistics ETP v1.2 (`energistics-tp` subprotocol, Protocol 0 `Core` & Protocol 1 `ChannelStreaming`) | Binary Avro datum framing (`MessageHeader` + body in single binary WebSocket frame) | `production` (`internal_only: false`, `conformance_verified: true`, `independent_peer_verified: false`, `external_vendor_verified: false`) | 25-scenario binary suite (`test_cp13_etp_binary_conformance.py`) + 15-scenario normative & `_IndependentSchemaAvroVerifier` suite (`test_cp14_etp_binary_normative.py`) over 12 official Energistics ETP v1.2 `.avsc` schemas (`apache-2.0` provenance in `etp12_official_schemas_and_vectors.json`) and 7 golden wire vectors + per-channel `TruncateChannels` reconciliation |
| `etp.1.2.json_ws` | Energistics ETP v1.2 JSON framing | JSON WebSocket frames | `internal_test_only` (`internal_only: true`, blocked in `production` unless `DRILLAI_CONNECTOR_ALLOW_INTERNAL_PROFILES=true`) | Verified for internal harness/debug workflows and blocked in production environments |

---

## 10. Multi-Process Stack & Bounded Soak Results (`scripts/verify_stack.py`)

Executed `backend/.venv/bin/python scripts/verify_stack.py` (spawning real `alembic`, `uvicorn`, `python -m drillai.telemetry.worker`, `LocalWitsmlSoapServer`, and `LocalEtpBinaryWebSocketServer` processes):

### 10.1 16 Multi-Process Scenarios

| # | Scenario Key | Outcome | Verified Detail |
| --- | --- | --- | --- |
| `1` | `clean_db_migration_init` | **PASSED** | `alembic upgrade head` & `alembic check` passed on fresh SQLite DB |
| `2` | `fastapi_backend_readiness` | **PASSED** | `/api/v1/health` & `/api/v1/health/ready` returned `200 OK` |
| `3` | `witsml_create_test_preview` | **PASSED** | 10 milestones verified, `preview_samples=6` |
| `4` | `etp_binary_create_test_preview` | **PASSED** | 10 milestones verified, `preview_samples=6` |
| `5` | `standalone_worker_witsml_etp_binary_ingest` | **PASSED** | Standalone `connector-worker` process transitioned both WITSML SOAP and ETP 1.2 Binary Avro connectors to `running` (`is_live=True`) |
| `6` | `non_harness_loopback_ssrf_blocked` | **PASSED** | Non-harness loopback port rejected with `HTTP 422` (`ssrf_blocked`) |
| `7` | `system_egress_policy_update_and_worker_sync` | **PASSED** | `org_admin` rejected (`403`); platform `admin` revision 2 synced to standalone worker (`last_policy_revision=2`, `policy_stale=False`) within 1 poll cycle |
| `8` | `api_restart_preserves_durable_egress_policy` | **PASSED** | Backend API restarted and served durable revision 2 from `connector_egress_policies` |
| `9` | `worker_restart_lease_recovery_no_duplicates` | **PASSED** | Worker process restarted, recovered lease, and resumed polling with 0 duplicate points |
| `10` | `policy_revocation_fail_closed_ssrf_blocked` | **PASSED** | Revision 3 revoked `10.24.0.0/16` and blocked outbound connection immediately (`ssrf_blocked`) |
| `11` | `transient_policy_store_failure_and_same_process_recovery` | **PASSED** | Simulated DB read error transitioned to `transient_store_error` (fail-closed), then recovered in-process to `active` (`database`) on next read |
| `12` | `reconciled_false_rollback_and_reconciliation_error` | **PASSED** | Injected `reconciled=False` rolled back uncommitted `TimeSeriesPoint` write (`points=12`), preserved `cursor_before`, and recorded `ConnectorRun(status="failed", error_category="reconciliation_error")` |
| `13` | `filtered_only_poll_outcome` | **PASSED** | Unmapped channel poll recorded `data_outcome="filtered_only"` (`unmapped_skipped=30`) |
| `14` | `dry_run_vs_live_replay_distinction` | **PASSED** | Dry-run replay recorded `data_outcome="dry_run_preview"` (`persisted=False`, cursor unchanged); live replay recorded `persisted=True` |
| `15` | `bounded_dns_stress_and_permit_recovery` | **PASSED** | 24 concurrent fast + slow-timeout DNS lookups completed with `available_permits=8/8` |
| `16` | `clean_connector_stop_and_bounded_run_retention` | **PASSED** | Both connectors stopped cleanly (`status="stopped"`, `is_live=False`) and run ledger bounded `<= 25` |

### 10.2 Bounded 32-Cycle Soak Metrics

```json
{
  "total_cycles": 32,
  "witsml_points_accepted": 9,
  "etp_binary_points_accepted": 8,
  "duplicate_points_suppressed": 328,
  "fenced_stale_writes_rejected": 1,
  "dns_permits_available_after_soak": 8,
  "dns_max_permits": 8,
  "reconciliation_failures": 0,
  "max_poll_duration_ms": 71.49,
  "worker_rss_mb_start": 96.9,
  "worker_rss_mb_end": 102.06
}
```

---

## 11. Exact Verification Commands, Counts & Outputs

| Gate | Command Executed | Exact Result |
| --- | --- | --- |
| **Backend Lint** | `cd backend && .venv/bin/ruff check src tests` | `All checks passed!` (`0` errors) |
| **Database Migrations** | `cd backend && DRILLAI_DATABASE_URL="sqlite+aiosqlite:///$(mktemp -u /tmp/drillai-mig-XXXXXX.db)" .venv/bin/alembic upgrade head && .venv/bin/alembic check` | Upgraded `base -> c9f1a48b2e31`; `No new upgrade operations detected.` |
| **Backend Unit, Integration & PostgreSQL Suite** | `cd backend && DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest -o addopts=""` | **`1057 passed, 1 warning in 882.21s (0:14:42)`** (`0` failed, `0` skipped, including all `@pytest.mark.postgres` suites) |
| **Multi-Process Stack & 32-Cycle Soak** | `backend/.venv/bin/python scripts/verify_stack.py` | **`STACK_VERIFICATION_OK: all 16 multi-process scenarios and 32-cycle bounded soak verified. Completed in 18.78s`** |
| **Frontend Typecheck** | `cd frontend && ./node_modules/.bin/tsc -b` | `0` errors |
| **Frontend Lint** | `cd frontend && npm run lint` | `eslint .` — `0` errors, `0` warnings |
| **Frontend Unit & Component Tests** | `cd frontend && npm test` | **`29 passed (29)` test files, `312 passed (312)` tests** (`0` failed, `0` skipped, `29.55s`) |
| **Frontend Production Build** | `cd frontend && npm run build` | `tsc -b && vite build` — `286 modules transformed`, `built in 3.18s` |
| **Full Unfiltered Playwright E2E Suite** | `cd frontend && npm run e2e` | **`119 passed (4.0m)`** (`0` failed, `0` skipped across `chromium`, `chromium-faults`, and `chromium-auth`, including **42 real-stack connector journeys** in `frontend/e2e/connectors-platform.spec.ts`) |

---

## 12. Git Checkpoint & CI Publication Ledger (`arena/01a0dca0-drillai`)

| Checkpoint | Commit SHA | Summary | Remote `git ls-remote` Verified | GitHub Actions CI Run |
| --- | --- | --- | --- | --- |
| **Gate 0 (`CP14-H`)** | `9b67ac5aee93240b2e3dab652432f2ea61e38db0` | Reconciled and published CP14-H documentation closure | **Yes** | Run **`38255799539`** (`conclusion: success`, `23m56s`) |
| **CP15-A** | `01805b160729b4f902fe09b127fc038577ed3282` | Certified CP14-H on CI and published CP15 source-backed audit register (`docs/mission-reports/CP15-SOURCE-AUDIT.md`) | **Yes** | Superseded by subsequent checkpoints |
| **CP15-B** | `6ecb6b372acf9e3fd73e636d883fb69af7f45e9c` | Durable egress-policy recovery state machine & 12-scenario PostgreSQL/worker recovery suite (`test_cp15_egress_policy_recovery.py`) | **Yes** | Superseded by subsequent checkpoints |
| **CP15-C & CP15-D** | `d02508f8fd524713c338149b012a724b8b5eb55f` | Fail-closed manifest reconciliation rollback & 11-state `POLL_DATA_OUTCOMES` truth table (`test_cp15_reconciliation_and_outcomes.py`) | **Yes** | Superseded by subsequent checkpoints |
| **CP15-E** | `63ecebbf09c1b39b734ede2e43b83e1bafd268e6` | Exact-once `_DnsPermitGuard` & generation-scoped DNS executor recovery (`test_cp15_dns_executor_resilience.py`) | **Yes** | Superseded by subsequent checkpoints |
| **CP15-F & Backend CP15-G** | `f1384f44e97762470341dee42e4f47ef1d6cbb5d` | System-scope egress policy RBAC, cross-tenant redaction, pre-connect worker fencing, ETP 1.2 `TruncateChannels` per-channel reconciliation & `_IndependentSchemaAvroVerifier` | **Yes** | Superseded by subsequent checkpoints |
| **CP15-G Types** | `eaca032513c924c603c12653c54a1e9376ad3b61` | Frontend TypeScript contracts for 11-state `data_outcome`, `policy_state`, `policy_stale`, `persisted`, `rolled_back` | **Yes** | Superseded by subsequent checkpoints |
| **CP15-G1** | `84a3fff2dbbed907f808a8bb2b308c95b0890c8f` | Rendered 11-state `data_outcome`, persistence state, egress policy state, redaction, and stale worker policy in EN/FA UI (`ConnectorsPage.tsx`, `OperationalMonitor.tsx`, `ConnectorsPage.test.tsx`) | **Yes** | Superseded by CP15-H1 |
| **CP15-G2** | `ced758947d3b267454118e67d1b6e73dc427ae58` | Added real-stack Playwright E2E Journeys 40–42 (`connectors-platform.spec.ts`) for dry-run vs live replay, system egress RBAC, stale worker policy sync, and Persian FA RTL | **Yes** | Superseded by CP15-H1 |
| **CP15-H1** | `eaaa29d74d6ece9da6e9e2799cca0e69bdc8dcde` | 16-scenario multi-process stack verification and 32-cycle soak (`scripts/verify_stack.py`) | **Yes** | Run **`38382687361`** (`conclusion: success`, `24m00s`) |
| **CP15-H2** | `HEAD` (parent `eaaa29d74d6ece9da6e9e2799cca0e69bdc8dcde`) | Future-safe soak timestamps in `scripts/verify_stack.py` (`witsml_points_accepted=9`, `etp_binary_points_accepted=8`) and finalized CP15 documentation (`CP15-SOURCE-AUDIT.md`, `CP15-MISSION-REPORT.md`, `FRONTEND-MISSION.md`) | **Yes (verified on push)** | Certified on push |
