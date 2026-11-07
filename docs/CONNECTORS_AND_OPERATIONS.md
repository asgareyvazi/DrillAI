# Telemetry Connectors, WITSML/ETP Protocols, Worker Runtime & Operations Runbook

This document specifies the production integration architecture, protocol profiles, standards gap matrix,
checkpoint and data disposition invariants, connection-bound SSRF and private rig-network egress controls,
fenced competing-worker execution model, deployment topology, and operator runbooks certified across
CP11–CP16.

---

## 1. Architecture & Data Flow

External and commissioning telemetry sources never write directly to database tables, never implement
a private unit conversion table, and never bypass platform authorization. Every connector ingests
exclusively through `TelemetryService`:

```text
External Rig Store / Stream (WITSML 1.4.1.1 SOAP | ETP 1.2 Binary Avro WS | ETP 1.2 JSON Test WS | Synthetic Plan)
        │
        ▼
Protocol Transport Client (`WitsmlSoapClient` | `EtpBinaryWebSocketClient` | `EtpWebSocketClient` | `ConnectorSyntheticAdapter`)
  • Connection-bound SSRF & DNS-rebinding pinning (`resolve_and_pin_endpoint() -> PinnedEndpoint`):
    validates all resolved A/AAAA records, unpacks IPv4-mapped IPv6/6to4/Teredo, rejects non-canonical/octal/integer
    IPv4 literals, enforces explicit private rig-network CIDR policy, blocks implicit loopback except
    active non-production harness `(host, port)` endpoints, and pins the actual socket connect target to
    `PinnedEndpoint.pinned_ip` while preserving HTTP `Host` and TLS SNI/certificate hostname verification
  • Minimized secret reference resolution (`env:DRILLAI_SECRET_*` | `secret_ref:*` | `secret:*`)
  • Full-payload XML DTD/entity/depth/element bounds & strict SOAP 1.1 `<Envelope>` validation (WITSML)
  • Spec-faithful ETP 1.2 Binary Avro framing (`energistics-tp` subprotocol, Avro binary `MessageHeader` +
    Protocol 0 `Core` & Protocol 1 `ChannelStreaming` bodies, multipart/Acknowledge/ProtocolException bounds)
  • TLS certificate verification (`tls_verify=True` enforced)
        │
        ▼
SourceAdapter (`WitsmlPollingAdapter` | `EtpBinarySubscriptionAdapter` | `EtpSubscriptionAdapter` | `ConnectorSyntheticAdapter`)
  • Emits normalized `ChannelDescriptor` + `SourceFrame` batches bounded by `max_points_per_poll`
  • Computes checkpoint watermarks strictly from processed items (`cursor_timestamp` +
    `completed_at_cursor` for WITSML; per-channel `channel_cursors[channel_key]` for ETP)
  • Records reconciled `ProtocolPollDisposition` accounting for every source record
        │
        ▼
Durable `ConnectorWorker` (`python -m drillai.telemetry.worker`)
  • Fair candidate discovery (`next_poll_at ASC NULLS FIRST, last_poll_at ASC NULLS FIRST, id ASC`)
  • Active-connector in-memory guard preventing overlapping in-process polls
  • Phase 1 (outside DB write lock): CAS lease claim (`fencing_token` + `config_version` +
    `lease_expires_at`), background `_heartbeat_watcher` renewing lease & cancelling in-flight
    poll immediately if preempted (`renew_lease() == False`, enforcing `lease_expires_at > now`)
  • Phase 2 (short atomic DB transaction): verify `worker_id`, `fencing_token`, `config_version`,
    AND `lease_expires_at > now`, ingest frames via `ingest_frames(TelemetryService, adapter, ...)`,
    advance `Connector.cursor`, append `ConnectorRun` with `details["manifest"]`, `checkpoint_status`,
    and `data_outcome`, prune bounded run ledger
        │
        ▼
`TelemetryService`
  • Unit validation & conversion to canonical SI (`Pa`, `N`, `rad/s`, `m3/s`, `m`, `N.m`)
  • Idempotent point deduplication (`(series_id, ts)` + `source_point_id`) & conflict detection
  • Quality classification (`good`, `questionable`, `bad`, `missing`, `suspect`)
  • Transactional outbox emission (`telemetry.received`, `well_state.changed`)
  • Automatic rule evaluation (`AlertEvaluationService`) -> `alert.changed`
        │
        ▼
Per-Well Live Stream (`/api/v1/wells/{well_id}/live/stream`), Operational Monitor & Connector Workspace
```

---

## 2. Supported Protocol Profiles, Checkpoint Semantics & Standards Gap Matrix

| Profile Key | Standard / Version | Transport & Wire Encoding | Implementation / Commissioning Status | Resume / Watermark | Local / Independent Conformance Evidence | External Commercial Vendor Verified |
| --- | --- | --- | --- | --- | --- | --- |
| `witsml.1.4.1.1.soap_http` | Energistics WITSML 1.4.1.1 | HTTP/HTTPS SOAP 1.1 (`WMLS_GetVersion`, `WMLS_GetCap`, `WMLS_GetFromStore`) | **Protocol implementation supported; live commissioning required; not vendor-certified** (`production_supported: true`, `internal_only: false`) | Inclusive `<startDateTimeIndex>` (`cursor_timestamp` + `completed_at_cursor` + `has_more_deferred`) | **Local harness passed** (`LocalWitsmlSoapServer` + 14-scenario SOAP 1.1 conformance suite); no independent external peer | **No** (`external_vendor_verified: false` — no external commercial rig store) |
| `etp.1.2.binary_ws` | Energistics ETP v1.2 | WebSocket (`ws://` / `wss://`, subprotocol `energistics-tp`), **Binary Avro** (`MessageHeader` + Protocol 0 Core & Protocol 1 ChannelStreaming) | **Protocol implementation supported; live commissioning required; not vendor-certified** (`production_supported: true`, `internal_only: false`) | Per-channel `channel_cursors[channel_key]` (`last_timestamp`, `last_index_raw`, `recent_point_ids`) | **Local harness and schema/golden-vector checks passed** (25-scenario binary suite plus independent Avro schema verifier); no external peer | **No** (`external_vendor_verified: false` — no external commercial ETP store) |
| `etp.1.2.json_ws` | Energistics ETP v1.2 / v1.1 (JSON test/debug subset) | WebSocket (`ws://` / `wss://`, subprotocol `energistics-tp`), Avro-JSON / flat JSON text frames | **Internal Test / Debug Only** (`production_supported: false`, `internal_only: true` — refused in production) | Per-channel `channel_cursors[channel_key]` | **Local test harness passed**; no external peer | **No** (`external_vendor_verified: false`) |
| `synthetic.v1` | DrillAI Synthetic v1 | In-process deterministic plan generator | **Commissioning / Test Only** (`production_supported: false`, `internal_only: true`) | `step_index` + `cursor_timestamp` | **Harness verified** | **N/A** (`SYNTHETIC / TEST SOURCE`) |

`read_only` is a typed profile property returned by `GET /connectors/profiles`, not only a sentence in `limitations`. All registered profiles are source-only; WITSML exposes only `WMLS_GetVersion`, `WMLS_GetCap`, and `WMLS_GetFromStore`, while ETP is limited to Protocol 0 Core and Protocol 1 ChannelStreaming (not the ETP Store protocol). The Connectors workspace shows a localized read-only badge for these profiles. This describes the operations DrillAI issues; it does not guarantee that a remote server is well behaved or make the connector vendor-certified.

`production_supported` is an implementation-profile flag, **not** proof of a deployed production instance, external-vendor interoperability, field commissioning, operational acceptance, or production recovery. For WITSML and ETP, only local harness/schema conformance is evidenced here; `external_vendor_verified` and `independent_peer_verified` remain false. External commissioning must follow §5 and retain a redacted result plus transcript digest before any vendor-specific verification claim.

### 2.1 WITSML 1.4.1.1 Checkpoint & Batch-Cap Invariants (`witsml.1.4.1.1.soap_http`)
- **Inclusive `<startDateTimeIndex>`**: Standard WITSML 1.4.1.1 stores treat `<startDateTimeIndex>` as inclusive (`>=`).
- **Mid-Row & Multi-Page Batch Cap Safety**: `WitsmlSoapClient.poll_log_batch()` enforces `max_points_per_poll` during parsing before computing `next_watermark`. If `max_points_per_poll` truncates a multi-curve row or multi-row page mid-flight:
  - `cursor_timestamp` advances only to the timestamp of the last emitted frame.
  - `completed_at_cursor` records the exact `source_point_id`s (`witsml:{channel_key}:{ts}`) and `completed_values_at_cursor` emitted at `cursor_timestamp`.
  - `has_more_deferred` is set to `True` (`checkpoint_status = "partial"`, `data_outcome = "partial_batch_capped"`), and the worker schedules the next poll immediately (`+50 ms`) so remaining curves on the same timestamp row and subsequent rows are ingested on the next poll with zero silent data loss and zero duplicate acceptance.
- **Secondary Depth Unit Conversion**: Secondary depth columns (`MD`, `DEPT`, `DEPTH`, `BITDEPTH`) declared in `ft` or `m` on `<logCurveInfo>` are converted to canonical SI meters (`0.3048 m/ft`) on `SourceFrame.depth_md_si`.

### 2.2 Production ETP 1.2 Binary Avro Profile (`etp.1.2.binary_ws`)
- **Normative Wire Encoding**: Negotiates WebSocket subprotocol `energistics-tp` and exchanges binary WebSocket frames containing an Avro binary-encoded `Energistics.Etp.v12.Datatypes.MessageHeader` (`protocol: int`, `messageType: int`, `correlationId: long`, `messageId: long`, `messageFlags: int`) immediately followed by the Avro binary-encoded message body:
  - **Protocol 0 (`Energistics.Etp.v12.Protocol.Core`)**: `RequestSession` (1), `OpenSession` (2), `CloseSession` (5), `Ping` (8), `Pong` (9), `ProtocolException` (1000, both single `error` union and `errors` map), `Acknowledge` (1001, automatic reply when `messageFlags & 0x10` is set).
  - **Protocol 1 (`Energistics.Etp.v12.Protocol.ChannelStreaming`)**: `ChannelMetadata` (1), `ChannelData` (2), `StartStreaming` (3), `StopStreaming` (4), `TruncateChannels` (5).
- **Per-Channel Watermarks & Dynamic `channelId` Remapping**:
  - `ChannelMetadataRecord.uri` (and `channelName`) is mapped to the stable DrillAI `channel_key` on every session; if a server reassigns numeric session `channelId`s across reconnects (`1 -> 101`, `2 -> 202`), the client remaps `channelId -> uri -> channel_key` without resetting per-channel cursors (`channel_cursors[channel_key]`).
- **Explicit Index & Value Semantics**:
  - Primary time indexes are decoded from `IndexValue` (`long` microseconds/milliseconds/seconds/nanoseconds or `double` seconds per `IndexMetadataRecord.uom`, or `PassIndexedDepth`). Depth-only primary indexes (`MeasuredDepth`, `TrueVerticalDepth`, `PassIndexedDepth`) are rejected fail-closed with `rejected_unsupported_index`.
  - Secondary depth indexes in `m` or `ft` are converted to canonical SI meters (`SourceFrame.depth_md_si`).

### 2.3 Evidence-Based Standards Gap Matrix (CP13)

| Standard / Capability | Status in DrillAI | Evidence / Verification |
| --- | --- | --- |
| **WITSML 1.4.1.1 SOAP 1.1 `WMLS_GetVersion`, `WMLS_GetCap`, `WMLS_GetFromStore` (time-indexed `<log>`)** | **Implemented & Verified** | `backend/src/drillai/telemetry/protocols/witsml.py`, `test_cp13_witsml_and_worker_races.py` (Scenarios 1–14) |
| **WITSML 1.4.1.1 Result codes (`1` complete, `2` partial/more available, `<=0` error) & SOAP Faults** | **Implemented & Verified** | `WitsmlSoapClient`, `test_cp12_protocol_conformance_and_security.py`, `test_cp13_witsml_and_worker_races.py` |
| **WITSML 1.4.1.1 depth-indexed `<log>`, `<trajectory>`, `<mudLog>`, and write operations (`WMLS_AddToStore`)** | **Out of Scope (Fail-Closed)** | Depth-indexed logs raise `ConnectorTransportError("protocol_error")`; connectors are strictly read-only |
| **ETP v1.2 Binary Avro framing (`etp.1.2.binary_ws`) for Protocol 0 (`Core`) & Protocol 1 (`ChannelStreaming`)** | **Implemented & Verified** | `backend/src/drillai/telemetry/protocols/etp_binary.py`, `test_cp13_etp_binary_conformance.py` (25 independent binary conformance scenarios) |
| **ETP v1.2 JSON framing (`etp.1.2.json_ws`)** | **Internal Test / Debug Only** | Marked `internal_test_only: true`, `production_ready: false`; blocked in `DRILLAI_ENV=production` |
| **ETP v1.2 Protocol 3 (`Discovery`), Protocol 4 (`Store`), Protocol 21 (`ChannelSubscribe`), deflate compression** | **Out of Scope** | Explicitly listed in `PROTOCOL_PROFILE_SPECS["etp.1.2.binary_ws"].limitations` |
| **Live External Commercial Vendor Servers (Kongsberg, Pason, Halliburton, SLB)** | **Unverified in CI** | `external_vendor_verified: false` in API and badged in `/connectors` UI and Operational Monitor |

---

## 3. Security, Connection-Bound SSRF Pinning, Private Rig-Network Policy & Secret Minimization

### 3.1 Connection-Bound SSRF, Non-Blocking Async DNS Pinning & Executor Resilience (`PinnedEndpoint`)
- `resolve_and_pin_endpoint_async()` and `validate_connector_endpoint_async()` validate the endpoint without blocking the asyncio event loop (`_DnsPermitGuard`, generation-scoped DNS thread-pool rotation `_rotate_dns_executor_locked`, `_DNS_MAX_QUARANTINED_THREADS = 16`) and return an immutable `PinnedEndpoint` containing `pinned_ip`, `port`, `host_header`, `sni_hostname`, `connect_url`, and `all_resolved_ips`:
  - **DNS Resolution Timeout & Concurrency**: Bounded by `DEFAULT_DNS_TIMEOUT_SECONDS = 3.0s` inside a bounded worker pool (`_DNS_MAX_CONCURRENCY = 8`) with exact-once permit release across success, timeout, cancellation, and submission failure.
  - **Strict Address Validation**: Rejects non-canonical, octal, integer, or shorthand IPv4 literals (`0177.0.0.1`, `2130706433`, `127.1`), unpacks IPv4-mapped IPv6 (`::ffff:127.0.0.1`, `::ffff:169.254.169.254`, `::ffff:10.0.0.1`), 6to4, and Teredo addresses, and validates **every** resolved `A`/`AAAA` record (if any record in a multi-record response is prohibited, the connection is refused).
  - **Socket-Level Pinning**:
    - `WitsmlSoapClient` connects directly to `pinned.connect_url` (pinned IP literal) with `headers={"Host": pinned.host_header}`, `follow_redirects=False`, `trust_env=False`, and `extensions={"sni_hostname": pinned.sni_hostname}` so HTTPX never re-resolves the hostname and TLS verifies the certificate against the original hostname.
    - `EtpBinaryWebSocketClient` and `EtpWebSocketClient` pass `host=pinned.pinned_ip, port=pinned.port, proxy=None, server_hostname=pinned.sni_hostname` to `websockets.asyncio.client.connect()`, binding the TCP socket directly to the validated IP while sending the original `Host` header and TLS SNI hostname.

### 3.2 Durable Private Rig-Network Policy, Fail-Closed Recovery & Redaction
- **No Implicit Loopback**: Ordinary user-supplied connectors cannot connect to `127.0.0.1` or `::1` unless `DRILLAI_CONNECTOR_ALLOW_LOOPBACK=true` is explicitly set. In non-production (`development`/`test`), only active `(host, port)` endpoints registered by `LocalProtocolHarnessManager` in `_HARNESS_OWNED_ENDPOINTS` (or listed in `DRILLAI_CONNECTOR_HARNESS_PORTS`) are reachable on loopback. In `DRILLAI_ENV=production`, harness endpoints and `DRILLAI_CONNECTOR_ALLOW_LOOPBACK=true` are refused at startup across both API and `ConnectorWorker`.
- **Durable Egress Policy State Machine (`EGRESS_POLICY_STATES`)**:
  - Authoritative egress policy is stored in `system_policies` (`SYSTEM_EGRESS_POLICY_KEY = "connector.egress_policy"`, `SYSTEM_EGRESS_POLICY_ORG_ID = "__system__"`).
  - `sync_egress_policy_from_db()` tracks explicit `policy_state` values in `EGRESS_POLICY_STATES`:
    - `"active"` — healthy policy loaded from the database (or initial environment default before any DB policy row is written)
    - `"transient_store_error"` — database read error during sync; fails closed (`ssrf_blocked`) and recovers automatically to `"active"` (setting `recovered_at`) once a valid DB read succeeds
    - `"corrupt_policy_record"` — stored policy JSON fails `validate_egress_policy_candidate()`; fails closed until repaired via `PUT /api/v1/connectors/egress-policy`
    - `"inactive_policy"` — `SystemPolicy.is_active == False`; fails closed until re-activated
    - `"missing_policy_record"` — authoritative database policy row deleted after initialization; fails closed
    - `"forced_test_failure"` — deterministic fault injection state for resilience verification
- **System-Scope Authorization & Role-Based Redaction**:
  - `PUT /api/v1/connectors/egress-policy` requires `system.egress_policy.manage` (`L4`, platform `admin` in platform org scope; tenant `org_admin` is denied `403`).
  - `redact_egress_policy_for_principal()` returns full `allowed_private_cidrs` and `allowed_hosts` to principals holding `system.egress_policy.read`/`system.egress_policy.manage` (or within the same dev/test org scope) and returns a redacted summary (`redacted: true`, `allowed_private_cidr_count`, `allowed_host_count`, empty CIDR/host arrays) to non-privileged callers.

### 3.3 Secret Reference Minimization (`secret_refs`)
- Plaintext credentials in `config` or `secret_refs` are refused (`422 ValidationFailed`).
- Read serializers (`mask_secret_refs`) redact both the secret value (`masked_value: "********"`) and the internal locator string (`ref: "env:********"` or `"secret_ref:********"`, `redacted: true`), and `redact_sensitive_text()` scrubs headers, query tokens, and `DRILLAI_SECRET_*` names from all transport errors, run ledgers, and logs.

---

## 4. Competing-Worker Leases, Expiry Fencing, Connection Milestones & 11-State Run Manifests

### 4.1 Lease Renewal Heartbeat & Phase 1 / Phase 2 Expired-Lease Fencing
- Before Phase 1 network I/O (`adapter.connect()`), `ConnectorWorker.execute_claimed_poll()` verifies `worker_id == self.worker_id`, `fencing_token == claimed.fencing_token`, `config_version == claimed.config_version`, `is_enabled`, `desired_state == 'enabled'`, AND `lease_expires_at > now` (aborting via `await _record_fenced_outcome()` if already expired or preempted).
- During Phase 1 network I/O, `ConnectorWorker` runs a background `_heartbeat_watcher` task that calls `renew_lease()` every `max(0.15s, min(lease_seconds / 3, 2.0s))` to extend `lease_expires_at` while verifying `worker_id`, `fencing_token`, `config_version`, `desired_state == 'enabled'`, AND `lease_expires_at > now`.
- Both Phase 2 commit (`execute_claimed_poll`) and failure recording (`_record_failed_outcome`) verify `worker_id == self.worker_id`, `fencing_token == claimed.fencing_token`, `config_version == claimed.config_version`, AND `lease_expires_at > now` in their pre-check and SQL CAS `WHERE` clauses so an expired worker cannot commit data, advance checkpoints, or overwrite connector status even if no second worker has claimed the connector yet.

### 4.2 Ten-Stage Connection-Test Milestone Pipeline (`ALL_TEST_CONNECTION_STAGES`)
Every `POST /api/v1/connectors/{id}/test` call returns ordered, structured `milestones` across all 10 canonical stages in `ALL_TEST_CONNECTION_STAGES`:
1. `endpoint_policy_validated`
2. `socket_connected`
3. `tls_verified`
4. `protocol_encoding_negotiated`
5. `session_established`
6. `channels_discovered`
7. `mapping_verified`
8. `data_frame_decoded`
9. `disposition_validated`
10. `transaction_committed` (marked `skipped` during non-persistent connection test)

### 4.3 Reconciled Run Disposition Manifest & 11-State `POLL_DATA_OUTCOMES` (`ConnectorRun.details["manifest"]`)
Every poll, preview, and replay operation builds and reconciles a manifest via `build_and_reconcile_poll_manifest()` and `compute_poll_data_outcome()` recording:
- **11-State `POLL_DATA_OUTCOMES` Truth Table**:
  1. `"reconciliation_error"` — protocol or ingestion count equation failed (`reconciled = false`); transaction rolls back telemetry points/revisions and cursor advancement (`persisted = false`, `rolled_back = true`, `checkpoint_status = "rolled_back"`)
  2. `"dry_run_preview"` — non-persistent preview or dry-run replay (`dry_run = true`, `persisted = false`, `checkpoint_status = "unchanged"`)
  3. `"no_data"` — `source_records_discovered == 0` (0 accepted, 0 duplicates, 0 revised, 0 rejected, 0 filtered)
  4. `"rejected_only"` — 0 points accepted/revised/duplicated/filtered and `total_rejected > 0`
  5. `"partial_with_rejections"` — `total_rejected > 0` alongside at least one accepted, revised, duplicate, or filtered record
  6. `"partial_batch_capped"` — `has_more_deferred == true` (`max_points_per_poll` cap reached with zero rejections; `checkpoint_status = "partial"`)
  7. `"filtered_only"` — `total_filtered > 0` (`unmapped_skipped + unsubscribed_skipped`) with 0 accepted, 0 revised, 0 duplicates, and 0 rejected
  8. `"revisions_and_duplicates"` — `points_accepted == 0`, `points_revised > 0`, and `points_duplicates > 0`
  9. `"revisions_only"` — `points_accepted == 0`, `points_revised > 0`, and `points_duplicates == 0`
  10. `"duplicates_only"` — `points_accepted == 0`, `points_revised == 0`, and `points_duplicates > 0`
  11. `"all_valid_accepted"` — `points_accepted > 0`, `total_rejected == 0`, and `has_more_deferred == false` (companion flags `filtered_present`, `revised_present`, and `duplicates_present` indicate whether filtered curves, revisions, or suppressed duplicates accompanied the accepted points)
- **Strict Reconciliation Equations**:
  - `protocol_reconciled`: `source_records_discovered == mapped_frames_emitted + unmapped_skipped + unsubscribed_skipped + watermark_suppressed + in_batch_duplicates + deferred_over_cap + total_protocol_rejected`
  - `ingestion_reconciled`: `mapped_frames_emitted == frames_received == points_accepted + service_duplicates + points_revised + service_rejected`
  - `policy_revision`: records the active egress policy revision enforced during the poll (`manifest-policy-revision` and `worker-policy-stale-badge` in UI).

---

## 5. Deployment Security, Verification Stack & Opt-In External Vendor Interoperability

### 5.1 Hardened `docker-compose.yml` & `.env.example`
1. Copy `.env.example` to `.env` and set a strong `POSTGRES_PASSWORD` (required — `docker-compose.yml` enforces `${POSTGRES_PASSWORD:?...}` with no default fallback password).
2. `postgres` runs on an internal-only Docker network (`drillai_internal`, `internal: true`) with no host port published by default.
3. `backend` and `frontend` bind to `127.0.0.1` by default (`${BIND_ADDRESS:-127.0.0.1}:8000:8000` and `${BIND_ADDRESS:-127.0.0.1}:8080:8080`).
4. `DRILLAI_CONNECTOR_ALLOW_LOOPBACK` defaults to `false`; private rig networks are configured via `DRILLAI_CONNECTOR_ALLOWED_PRIVATE_NETWORKS` and `DRILLAI_CONNECTOR_ALLOWED_HOSTS`.
5. The frontend Nginx proxy has the static peer address `172.31.240.10` on the dedicated `172.31.240.0/28` edge subnet by default. Uvicorn trusts forwarded headers only from that peer (`FORWARDED_ALLOW_IPS`); do not broaden this to `*`. If the subnet overlaps your host network, choose a non-overlapping subnet and keep the proxy address inside it; update `DRILLAI_EDGE_SUBNET`, `DRILLAI_FRONTEND_PROXY_IP`, and `DRILLAI_UVICORN_FORWARDED_ALLOW_IPS` together. Direct client-supplied forwarding headers are not trusted by the authentication rate-limit/audit code, and Nginx access logs omit the raw forwarded-for chain.
6. Treat a successful config parse or local harness as static evidence only. The actual Compose proxy path, trusted peer identity, and deployed edge exposure still require the Docker Compose smoke gate on a Docker-capable host.

```bash
cp .env.example .env
docker compose up --build -d
```

### 5.2 Multi-Process Stack Verification (`scripts/verify_stack.py`), External Interop Runner (`scripts/verify_external_vendor_interop.py`) & Docker Compose CI Smoke Gate
- **Local Multi-Process Stack (`scripts/verify_stack.py`)**: Runs Alembic migrations, starts `uvicorn`, `LocalWitsmlSoapServer`, `LocalEtpBinaryWebSocketServer`, and standalone `python -m drillai.telemetry.worker` processes, verifies 16 scenarios including live WITSML SOAP and ETP 1.2 Binary Avro ingestion, 32-cycle soak metrics (`CP15_SOAK_METRICS`), worker restart recovery without duplicate points, and governed stop state, and prints `STACK_VERIFICATION_OK`.
- **Safe Opt-In External Vendor Interoperability Runner (`scripts/verify_external_vendor_interop.py`)**:
  - `--mode harness` (default): Executes offline WITSML 1.4.1.1 SOAP and ETP 1.2 Binary Avro conformance plus unsupported protocol/compression/message negative checks (`ProtocolException` msgType `1000`), making zero external network calls.
  - `--mode external`: Executes a read-only acceptance probe against operator-supplied `DRILLAI_EXTERNAL_WITSML_URL` and/or `DRILLAI_EXTERNAL_ETP_URL` in staging/rig environments; when unset, makes zero network calls and reports `external_gate_status: "BLOCKED_BY_EXTERNAL_EVIDENCE"` with `external_vendor_verified: false`.
- **Docker Compose CI Smoke Gate (`.github/workflows/ci.yml`)**: Validates `docker compose config -q`, builds the production `backend` and `frontend` Docker images, starts `postgres`, `migrate`, `backend`, `connector-worker`, and `frontend`, verifies `/api/v1/health/ready` and `/api/v1/connectors/profiles` (including `etp.1.2.binary_ws`) through the Nginx proxy container, and tears down cleanly.

---

## 6. Production Database Backup, Restore, Migration Recovery & Operations Runbook (CP18)

### 6.1 Architectural Distinction: SQLite Local/Dev Backup vs PostgreSQL Production Disaster Recovery
DrillAI explicitly distinguishes local development database snapshots from production database disaster recovery:
- **SQLite (`sqlite+aiosqlite:///...`, Local/Dev & Fast Test Mode)**:
  - Uses SQLite's online `sqlite3.Connection.backup(target_conn)` API (or offline file copy when all connections are disposed).
  - Suitable **only** for local developer workstations and ephemeral single-node test runs (`production_dr_capable: false`).
- **PostgreSQL 16 (`postgresql+asyncpg://...`, Production Deployment Mode)**:
  - Uses `pg_dump --format=custom --no-owner --no-privileges` for transactional, MVCC-consistent logical backups (`drillai_backup.dump`) with SHA-256 integrity verification (`sha256sum`), paired with `pg_restore --no-owner --no-privileges --exit-on-error` into a clean target database and post-restore Alembic revision verification (`alembic upgrade head && alembic check`).
  - Certified automatically by `scripts/verify_postgres_dr_drill.py` and `backend/tests/db/test_cp18_postgres_backup_restore.py` (`@pytest.mark.postgres`).

### 6.2 Production PostgreSQL Backup Runbook (`pg_dump`)

```bash
# 1. Export connection URI (or run inside the docker-compose postgres container)
export PG_URI="postgresql://drillai:${POSTGRES_PASSWORD}@127.0.0.1:5432/drillai"
export BACKUP_FILE="drillai_$(date -u +%Y%m%dT%H%M%SZ).dump"

# 2. Take a transactional custom-format logical backup
pg_dump --format=custom --no-owner --no-privileges --file="${BACKUP_FILE}" "${PG_URI}"

# 3. Record byte size and SHA-256 checksum alongside the backup artifact
sha256sum "${BACKUP_FILE}" | tee "${BACKUP_FILE}.sha256"
```

When running under Docker Compose (`docker-compose.yml`):
```bash
docker compose exec -T postgres pg_dump \
  -U "${POSTGRES_USER:-drillai}" \
  -d "${POSTGRES_DB:-drillai}" \
  --format=custom --no-owner --no-privileges > "drillai_backup.dump"
sha256sum drillai_backup.dump > drillai_backup.dump.sha256
```

### 6.3 Production PostgreSQL Restore & Post-Restore Migration Verification Runbook (`pg_restore` + `alembic`)

```bash
# 1. Verify backup archive integrity before restore
sha256sum --check "${BACKUP_FILE}.sha256"
pg_restore --list "${BACKUP_FILE}" >/dev/null

# 2. Stop active writers (connector-worker and backend API) during full database replacement
docker compose stop connector-worker backend

# 3. Recreate clean target database and restore with --exit-on-error
docker compose exec -T postgres psql -U "${POSTGRES_USER:-drillai}" -d postgres \
  -c "DROP DATABASE IF EXISTS drillai_restored WITH (FORCE);" \
  -c "CREATE DATABASE drillai_restored;"

docker compose exec -T postgres pg_restore \
  -U "${POSTGRES_USER:-drillai}" \
  -d drillai_restored \
  --no-owner --no-privileges --exit-on-error < "${BACKUP_FILE}"

# 4. Verify Alembic migration revision state and zero schema drift against the restored database
cd backend
DRILLAI_DATABASE_URL="postgresql+asyncpg://drillai:${POSTGRES_PASSWORD}@127.0.0.1:5432/drillai_restored" \
  ./.venv/bin/python -m alembic current
DRILLAI_DATABASE_URL="postgresql+asyncpg://drillai:${POSTGRES_PASSWORD}@127.0.0.1:5432/drillai_restored" \
  ./.venv/bin/python -m alembic upgrade head
DRILLAI_DATABASE_URL="postgresql+asyncpg://drillai:${POSTGRES_PASSWORD}@127.0.0.1:5432/drillai_restored" \
  ./.venv/bin/python -m alembic check
```

### 6.4 Automated Isolated PostgreSQL DR Drill (`scripts/verify_postgres_dr_drill.py`)
Operators and CI can run the end-to-end PostgreSQL 16 backup, catastrophic-loss simulation, restore, Alembic drift check, multi-tenant boundary verification, and corrupt-archive rejection drill with a single command:

```bash
backend/.venv/bin/python scripts/verify_postgres_dr_drill.py
```

### 6.5 Migration Rollback, Health Probes, Worker Supervision, Secret Rotation & Egress Policy Runbook
- **Migration Rollback Considerations**:
  - Always capture and checksum a `pg_dump --format=custom` backup **before** applying new Alembic revisions in staging or production.
  - Schema-only downgrades can be executed via `python -m alembic downgrade <revision>`, but if a migration transforms or drops columns, restoring the pre-upgrade `pg_dump` archive into a clean target database is the authoritative data-preserving rollback path.
- **Health & Readiness Checks**:
  - Liveness (unauthenticated): `curl -fsS http://127.0.0.1:8000/api/v1/health`
  - Readiness (verifies live database connectivity and dialect): `curl -fsS -H "Authorization: Bearer <TOKEN>" http://127.0.0.1:8000/api/v1/health/ready`
- **Connector Worker Supervision**:
  - Run `python -m drillai.telemetry.worker` under `systemd`, Kubernetes Deployment, or Docker Compose (`connector-worker` service with `restart: unless-stopped`).
  - Graceful shutdown is triggered via `SIGTERM` or `SIGINT`, which sets `worker._stop_event` and disposes database pools cleanly. If a worker crashes abruptly, its lease expires after `connector_worker_lease_seconds` (default `30s`) and the next worker claim increments `fencing_token`, fencing any late writes from the old worker.
- **Secret Rotation (`DRILLAI_SECRET_MASTER_KEY` & `env:` References)**:
  - For `env:DRILLAI_SECRET_*` references, update the environment variable in the secret manager / container runtime and restart `backend` and `connector-worker`.
  - For database-encrypted secrets (`DRILLAI_SECRET_BACKEND=database`), `DRILLAI_SECRET_MASTER_KEY` (Fernet key) is required in production (`ConfigurationError` on startup if missing). When rotating `DRILLAI_SECRET_MASTER_KEY`, re-encrypt stored `SecretRef` ciphertexts within a maintenance window and increment the connector `config_version` via `PATCH /api/v1/connectors/{id}` so running workers reload credentials cleanly.
- **System-Wide Egress Policy Management**:
  - Inspect current policy state (`active`, `transient_store_error`, `corrupt_policy_record`, `inactive_policy`, `missing_policy_record`) and revision: `GET /api/v1/connectors/egress-policy`.
  - Update allowed hostnames and private rig CIDRs atomically with `Idempotency-Key`: `PUT /api/v1/connectors/egress-policy` (requires `admin` role). Both API and worker processes sync the updated revision from PostgreSQL before every outbound connection test or poll.

---

## 7. Document Ingestion, Extracted-Record Review, OCR Benchmark & Report Export

### 7.1 Manual Operation and Event Entry

#### Multi-format and multi-layout drilling-report ingestion

The document uploader accepts PDF, DOCX, XLSX/XLSM, CSV/TSV, TXT, Markdown and LOG files. Daily Report import discovers both `ddr` and `drilling_document` source classifications; the latter was accepted by extraction/import code but absent from the public upload vocabulary and the importer list. The document-list API can filter multiple explicit types without weakening tenant/well scope. Structured tables retain source page/sheet/table/row/cell locators, and the operation extractor locates a header row after bounded title/metadata rows rather than assuming the first row is data. PDF or text operation rows are extracted only from explicit pipe- or tab-delimited tables with a recognizable activity header; visually aligned prose is not guessed into columns. Extracted values remain proposals with their original field-level locators, confidence and review state. The extraction review drawer and DDR import view show linked evidence, quote status and source-locator details beside the payload, so a reviewer can inspect the source coordinates before making an audited decision. Uploading or extracting a report does not approve it or create an authoritative report revision; the document/review and Daily Reports reconciliation workflows must record a human decision. Scanned PDF processing remains dependent on the separately commissioned opt-in OCR runtime in §7.2.

- The bilingual Operations workspace can create actual operations and events through the same authorized API used by integrations. Manual rows carry `source_kind: "manual"` and a client-generated `Idempotency-Key`; the API remains authoritative for scope, RBAC, vocabularies, and validation.
- Manual operation time inputs are interpreted as the operator's local browser time and submitted as UTC instants. API timestamps must carry an explicit timezone offset; timezone-naive timestamps and planned/actual end times before their corresponding starts are rejected. Measured-depth inputs are canonical SI metres; the displayed datum is contextual and is not converted or inferred by the form. An end depth earlier than the start depth is rejected in the UI and again by the server. Event depth corrections are finite and non-negative; zero is a valid measured depth.
- Event kind, NPT classification, and NPT duration are separate claims. A non-NPT event does not acquire a category implicitly. To mark an event as NPT, choose a backend-approved category and enter positive hours; a recorded `not_npt` category is intentionally not offered for an event that is being charged as NPT. The frontend's operation/event/NPT vocabularies are contract-tested against the backend model constants.
- These forms create operator-entered evidence; they do not establish that a record came from a rig or received engineering/field approval.

### 7.2 OCR Runtime and Limits

OCR is opt-in and disabled by default (`DRILLAI_OCR_ENABLED=false`). The standard backend Docker image installs the PDFium Python extra (`backend[ocr]`) plus OS-level Tesseract and English/Persian trained data (`eng` and `fas`); it does not download OCR assets at request time. Native deployments must install Tesseract and every configured language pack themselves. Set `DRILLAI_OCR_ENABLED=true` only after explicit commissioning; `DRILLAI_OCR_EXECUTABLE` and `DRILLAI_OCR_LANGUAGES` default to `tesseract` and `eng+fas`. The app probes `tesseract --version` and `--list-langs` with bounded time/output, checks each configured language, and records the adapter plus actual runtime version. A missing executable/language pack/PDFium renderer is reported as unavailable; it never becomes a silent empty extraction.

Run a deployment-local preflight in the same backend container/venv used by ingestion:

```bash
cd backend
.venv/bin/python -m drillai.ingestion.ocr_runtime --require-runtime
.venv/bin/python -m drillai.ingestion.ocr_runtime --self-test
# Docker Compose alternative: docker compose exec backend python -m drillai.ingestion.ocr_runtime --require-runtime
```

The preflight prints a secret-free JSON dependency/configuration report, including enabled state, configured/installed languages, PDFium version, safe resource limits and a `quality_evaluated: false` marker. It exits non-zero when a required runtime component is missing. The optional `--self-test` additionally renders a fixed synthetic image in memory with the bundled DejaVu font, passes it through the configured Tesseract process, and reports only whether non-empty text was returned and how many lines; recognized text and filesystem paths are never included. This checks the local processing path, not character/word accuracy, a representative corpus, semantic correctness, or engineering acceptance. The flag fails closed if Tesseract, the configured languages, PDFium, or image-rendering support is unavailable. When OCR is enabled, `/api/v1/health/ready` includes the dependency check and reports `degraded` if Tesseract, configured language data or PDFium is unavailable; the bounded probe runs off the event loop and its immutable dependency result is cached for that process. Restart the API after changing the image, executable or language data. When disabled, OCR is explicitly `disabled` and does not block application readiness. Runtime presence or the synthetic canary does not establish OCR accuracy or engineering acceptance.

Defaults are 30 seconds per page, at most 20 blank/image-only pages per PDF, two concurrent Tesseract processes, 200 DPI, and 12,000,000 rendered pixels per page; configuration enforces the documented upper bounds. Uploaded images are passed to Tesseract over stdin (never a shell or user-controlled filename), child environments are allowlisted, TSV output and PDF pixels are bounded, and timeout/concurrency saturation/page limits/unsafe dimensions/OCR failure/truncation remain explicit incomplete page states. Confidence, if supplied, is a word/line transcription signal—not a probability of engineering correctness. Source-critical values still require human reconciliation.

No approved real-world benchmark corpus, gold labels, or measured OCR accuracy is bundled with this repository. Stage operator-approved PDFs and UTF-8 ground truth outside Git and create a schema-version-2 manifest. The manifest pins each PDF and label by SHA-256 and carries corpus identity/version/classification, declared languages, sampling method, ground-truth review protocol, and per-case locale/document class plus `development`, `validation`, or `holdout` split. Split membership and holdout independence must be governed by the corpus operator; the runner reports declarations but cannot verify leakage or independence. Its approval object requires `declaration: "operator_attests_approved"`, a reference, owner role, and timezone-aware `approved_at`; this is operator-attested metadata only. The runner cannot verify consent, governance approval, sampling representativeness, or label adjudication. Use non-identifying case IDs/role references. Run:

```bash
cd backend
.venv/bin/python -m drillai.ingestion.ocr_benchmark \
  --manifest /secure/ocr/manifest-v2.json \
  --corpus-root /secure/ocr/corpus \
  --languages eng+fas \
  --max-concurrency 2 \
  --max-total-edit-cells 250000000 \
  --output /secure/ocr/benchmark-result.json
```

The local-only runner reads no network sources, validates corpus metadata and paths, verifies all pinned file hashes, bounds file/case/edit-distance work, and writes no source paths or recognized/ground-truth transcripts. The report includes manifest/corpus declarations, runtime/adapter versions, per-case hashes/status, weighted CER/WER, per-split totals, and locale/document-class/split strata. Pooled totals intentionally mix declared splits and are labeled as such; use `split_totals.holdout` for operator-declared holdout-only transcription metrics, while remembering that holdout independence is unverified by the runner. `status: complete` means only that every selected case was processed and scored—it is not a quality pass or release approval. Missing runtime exits 2; incomplete/failed evaluation exits non-zero; no metric threshold is embedded. Synthetic-provider tests verify mechanics only and are not OCR results. No Tesseract executable or approved real corpus/ground truth is available in this workspace, so no corpus benchmark or accuracy result is claimed.

### 7.3 Versioned Extracted-Record Review

- `GET /api/v1/documents/{document_id}/records/{record_id}` is tenant/document scoped and returns a focused payload preview capped at 64 KiB. `payload_truncated: true` means the UI must not offer edits to that still-truncated value.
- `PATCH /api/v1/documents/{document_id}/records/{record_id}/review` requires review permission, an `Idempotency-Key`, a `reason`, `decision` (`validate` or `reject`), and `expected_version`. It performs compare-and-swap; a stale concurrent edit returns HTTP 409 and must be reloaded rather than overwritten.
- Corrections preserve the stored JSON field/list shape and validate domain-specific survey/depth values. Payloads above 64 KiB are refused. Corrections cannot change tenant, document, source-record, or provenance identity. The transaction increments `review_version` and writes an audit entry containing the prior validation note (capped), payload digests, changed field names, decision, reviewer, and reason; raw payload content is not copied into the audit.
- Explicit `null` for `observed_at`, `depth_md_si`, or `depth_tvd_si` clears that canonical metadata. Omitted fields are left unchanged. The route does not infer canonical depth from a JSON payload; the operator must correct each intended field. Audit idempotency fingerprints preserve omitted-versus-explicit-null distinctions.

### 7.4 Scoped Report JSON Export

`GET /api/v1/wells/{well_id}/reports/{kind}/export` is a separate `report.export`-authorized route; it serializes the same structured report content as the JSON report API, refuses output over 20 MiB, returns an attachment with `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, and a content digest ETag, and records an audit entry with report kind, period, size, and SHA-256 (not report body content). It is tenant-scoped; viewers without the export action are denied. The frontend validates the JSON envelope through the normal API error/identity boundary and downloads the unmodified response bytes, so the saved file matches the server's ETag and audit digest. This is machine-readable JSON only. PDF/DOCX rendering, provider delivery, and field certification are not implied.

### 7.5 Deterministic retrieval evaluation

Run the versioned, network-free lexical retrieval benchmark from `backend/` with `.venv/bin/python -m drillai.rag.benchmark` (optionally pass `--output /secure/path/report.json`). Version `drillai-rag-synthetic-v2` seeds an isolated SQLite database and exercises the production `retrieve` service on 13 fictional chunks and six development queries, including same-text cross-well/tenant distractors, a quarantined artifact, wrong-section text, conflicting synthetic values, and unknown depth/time. It reports lexical Recall@5/Precision@5/MRR, forbidden-result counts, scope violations, citation coverage, and source-locator coverage. The run recorded in CP118 produced dataset SHA-256 `02eb21e778c497dbfc2575922abb3a27f39996f888406e52ab557d5b8289c61d`: micro Recall@5 1.0, macro Recall@5 over positive queries 1.0, MRR over positive queries 1.0, macro Precision@5 across all queries 0.1333, two expected empty-result cases, zero forbidden retrievals/scope violations, and 1.0 citation/source-locator coverage. Precision uses fixed cutoff five even when fewer than five lexical matches are returned. The benchmark verifies only deterministic lexical filtering/ranking and evidence plumbing on hand-labelled fixtures; unknown depth/time stays excluded from bounded queries and `created_at` is never substituted. This is not representative corpus evidence and does not measure semantic relevance, embedding/hybrid quality, answer grounding, LLM quality, engineering approval, or field performance. See the [CP118 RAG report](mission-reports/CP118-SYNTHETIC-RAG-GROUNDING.md) for scope and caveats.


### 7.6 LLM / embedding configuration and evidence boundaries

- `DRILLAI_LLM_PROVIDER=stub|openai_compatible|ollama` selects one chat mode. Development/test `stub` registers the clearly named `echo` deterministic provider; **production does not register Echo**, so a request without a live provider fails as unavailable rather than producing copy-style stub output. Selecting a live provider replaces Echo; missing credentials or a failed call do not fall back to Echo.
- For live OpenAI-compatible endpoints set `DRILLAI_LLM_BASE_URL` (use the provider's compatible `/v1` base URL), `DRILLAI_LLM_DEFAULT_MODEL`, and inject `DRILLAI_LLM_API_KEY` through the deployment secret mechanism (never commit it). Ollama is allowed to omit the bearer key. These settings select/configure a client; `/api/v1/platform/integrations` does not probe network reachability.
- `DRILLAI_LLM_PRIVACY` is the operator-declared endpoint classification (`local`, `private_cloud`, or `public_cloud`). `DRILLAI_LLM_MAX_PRIVACY` is a separate routing ceiling, defaulting to `private_cloud`; a public-cloud model is excluded until the deployment explicitly raises that ceiling. Classify the endpoint honestly and review tenant-data egress before changing either value.
- `DRILLAI_EMBEDDING_PROVIDER` is independent of chat. The default `deterministic` feature-hash provider is reproducible, but is **not a semantic embedding model**. Live embedding providers use the configured base URL/key and `DRILLAI_EMBEDDING_MODEL`; vector identity also includes provider implementation, endpoint, model, dimension and a hash of the indexed chunk text, so stale or incompatible vectors are not silently reused. Retrieval reports lexical fallback when compatible vectors are absent.
- Advisor structured facts carry their source, source ID, evidence reference and available per-value provenance, including observation/receipt times, time basis, quality, freshness, age and source note. Telemetry latest values retain the internal point ID, external source-point identity, source value/unit and canonical value/unit; the Advisor exposes this conversion context rather than dropping it. NPT facts expose the selected events-versus-operations basis, the separate alternate-basis total (including a differing zero alternate), source case IDs and extracted-record IDs, classification/time/depth bases, and attached evidence links when available; a zero selected total is explicitly not treated as proof of no physical downtime. No more than 10 case facts are shown, with truncation explicitly disclosed while aggregate totals retain the service's full matching population. Operation facts retain actual start/end and duration bases; a terminal status without an accepted `actual_end` basis produces an explicit unknown because lifecycle closure does not establish `ended_at`. The Advisor UI exposes source IDs and provenance. Unknown time is not filled from `created_at`.
- The lexical benchmark is synthetic and does not validate hybrid semantic quality. The generic workflow AI node wraps retrieved/context text as untrusted and applies its output guardrails, but those checks do not establish general semantic entailment or citation validity. Operations Advisor narration has a stricter exact-source-statement match and cannot change structured facts/calculations. Neither path is a substitute for a real-corpus, independently judged evaluation.
- No live LLM/embedding endpoint or API key was configured in this workspace. The provider configuration and fail-closed behaviors were tested locally; no vendor request, live-provider quality, or production capability is claimed.


## Notification outbox: acceptance, recovery, and receipt limits

`OutboundMessage` is a tenant-scoped outbox. The notification worker is opt-in (`DRILLAI_NOTIFICATION_WORKER_ENABLED=false` by default) and dispatches only committed claims. Tune `DRILLAI_NOTIFICATION_POLL_SECONDS` (0.2–60 seconds) and `DRILLAI_NOTIFICATION_BATCH_SIZE` (1–100); inspect the `/health/ready` `notification_outbox` check for enabled/running state, completed cycles, last cycle, and the last error *type*. A workflow's legacy `dispatch_immediately` flag now records an outbox row only; transport I/O is deferred until after the workflow transaction, and requires the worker to be enabled.

The status contract is deliberately narrow:

- `queued` / `sending` describe local outbox progress. `failed_retryable` has a bounded exponential retry schedule; `dead_letter` is the retry ceiling. `failed_terminal` needs configuration/operator action. `reconciliation_required` means a worker lost an in-flight row after the remote outcome became ambiguous; it is never automatically retried.
- `accepted` means only an HTTP 2xx response, SMTP server acceptance, or local in-app outbox acceptance. `sent` is a legacy state and is not retroactively treated as a receipt. `delivery_confirmed` and `external_provider_verified` remain false because no end-recipient callback/read receipt is implemented.
- Webhook payloads include the same idempotency key on retries, but the transport is at-least-once. A partial unique database index prevents more than one queued/sending/retryable row per tenant/idempotency key. Legacy duplicate pending keys cause the additive migration to stop before DDL rather than delete/merge rows; operators must reconcile those rows first.
- Rows stale in `sending` for more than five minutes move to `reconciliation_required`. The tenant-scoped Platform State outbox view omits message body, subject, recipient address and raw transport errors. It exposes status, attempt counts, acceptance time/evidence, the remote provider ID only when supplied for HTTP, and a sanitized error code.
- Manual requeue requires `integration.manage` at action level L4, a 10–500 character audit reason, and explicit duplicate-risk acknowledgement. It is tenant-checked, audited without message content, limited to terminal/dead-letter/ambiguous statuses, and does not claim to discover whether the recipient received an earlier attempt.

Webhook signing/authentication and SMTP passwords resolve through managed secret references; embedded SMTP URL passwords are rejected, production email requires an `smtps://` endpoint with certificate verification, and local non-production SMTP harnesses may use plaintext `smtp://`. Webhook responses are not persisted or parsed for error text, and ledger errors contain only bounded codes/generic messages. The local synthetic HTTP/SMTP test servers and integration test results are not evidence of external provider delivery. Before enabling a production worker, configure outbound egress and secret handling, run a controlled recipient-owned commissioning test, preserve redacted provider evidence separately, and agree on duplicate/reconciliation procedures. Never report transport acceptance as recipient delivery.
