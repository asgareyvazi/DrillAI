# Telemetry Connectors, WITSML/ETP Protocols, Worker Runtime & Operations Runbook

This document specifies the production integration architecture, protocol profiles, standards gap matrix,
checkpoint and data disposition invariants, security controls, fenced competing-worker execution model,
deployment topology, and operator runbooks certified in CP11 and CP12.

---

## 1. Architecture & Data Flow

External and commissioning telemetry sources never write directly to database tables, never implement
a private unit conversion table, and never bypass platform authorization. Every connector ingests
exclusively through `TelemetryService`:

```text
External Rig Store / Stream (WITSML 1.4.1.1 SOAP | ETP 1.2 WebSocket | Synthetic Plan)
        │
        ▼
Protocol Transport Client (`WitsmlSoapClient` | `EtpWebSocketClient` | `ConnectorSyntheticAdapter`)
  • Registration-time & connect-time SSRF / DNS-rebinding guard (`validate_connector_endpoint`,
    `enforce_connect_time_ssrf`: validates all resolved A/AAAA records, unpacks IPv4-mapped IPv6,
    6to4, and Teredo addresses, and rejects non-canonical/octal/integer IPv4 literals)
  • Minimized secret reference resolution (`env:DRILLAI_SECRET_*` | `secret_ref:*` | `secret:*`)
  • Full-payload XML DTD/entity/depth/element bounds & strict SOAP 1.1 `<Envelope>` validation
  • TLS certificate verification (`tls_verify=True` enforced)
        │
        ▼
SourceAdapter (`WitsmlPollingAdapter` | `EtpSubscriptionAdapter` | `ConnectorSyntheticAdapter`)
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
    poll immediately if preempted (`renew_lease() == False`)
  • Phase 2 (short atomic DB transaction): verify `worker_id`, `fencing_token`, AND `config_version`,
    ingest frames via `ingest_frames(TelemetryService, adapter, ...)`, advance `Connector.cursor`,
    append `ConnectorRun` with `details["manifest"]` and `checkpoint_status`, prune bounded run ledger
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

| Profile Key | Standard / Version | Transport | Auth Modes | Resume / Watermark | Local Harness & Spec Suite Verified | External Commercial Vendor Verified |
| --- | --- | --- | --- | --- | --- | --- |
| `witsml.1.4.1.1.soap_http` | Energistics WITSML 1.4.1.1 | HTTP/HTTPS SOAP 1.1 (`WMLS_GetVersion`, `WMLS_GetCap`, `WMLS_GetFromStore`) | `none`, `basic`, `bearer` | Inclusive `<startDateTimeIndex>` (`cursor_timestamp` + `completed_at_cursor` + `has_more_deferred`) | **Yes** (`LocalWitsmlSoapServer` + independent SOAP 1.1 conformance suite) | **No** (no external vendor server in CI) |
| `etp.1.2.json_ws` | Energistics ETP v1.2 / v1.1 (Avro-JSON & flat JSON framing subset) | WebSocket (`ws://` / `wss://`, subprotocol `energistics-tp`) | `none`, `basic`, `bearer` | Per-channel `channel_cursors[channel_key]` (`last_timestamp`, `last_index_raw`, `recent_point_ids`) | **Yes** (`LocalEtpWebSocketServer` + independent ETP 1.2 Avro-JSON conformance suite) | **No** (no external vendor server in CI) |
| `synthetic.v1` | DrillAI Synthetic v1 | In-process deterministic plan generator | `none` | `step_index` + `cursor_timestamp` | **Yes** | **N/A** (`SYNTHETIC / TEST SOURCE`) |

### 2.1 WITSML 1.4.1.1 Checkpoint & Batch-Cap Invariants (`witsml.1.4.1.1.soap_http`)
- **Inclusive `<startDateTimeIndex>`**: Standard WITSML 1.4.1.1 stores treat `<startDateTimeIndex>` as inclusive (`>=`).
- **Mid-Row & Multi-Page Batch Cap Safety**: `WitsmlSoapClient.poll_log_batch()` enforces `max_points_per_poll` during parsing before computing `next_watermark`. If `max_points_per_poll` truncates a multi-curve row or multi-row page mid-flight:
  - `cursor_timestamp` advances only to the timestamp of the last emitted frame.
  - `completed_at_cursor` records the exact `source_point_id`s (`witsml:{channel_key}:{ts}`) and `completed_values_at_cursor` emitted at `cursor_timestamp`.
  - `has_more_deferred` is set to `True` (`checkpoint_status = "partial"`), and the worker schedules the next poll immediately (`+50 ms`) so remaining curves on the same timestamp row and subsequent rows are ingested on the next poll with zero silent data loss and zero duplicate acceptance.

### 2.2 ETP 1.2 Per-Channel Watermarks & Index Unit Semantics (`etp.1.2.json_ws`)
- **Per-Channel Watermarks (`channel_cursors`)**: Each mapped channel maintains its own independent watermark (`channel_cursors[channel_key] = {last_timestamp, last_index_raw, index_kind, index_uom, recent_point_ids}`). A fast channel advancing to `T3` never suppresses a slower or out-of-order channel arriving at `T1.5` or `T2.5`.
- **Explicit Index Metadata (`EtpIndexMetadata`)**: Numeric primary time indexes are converted strictly using the channel's declared `IndexMetadataRecord` (`indexKind` in `DateTime`/`Time`/`ElapsedTime` and `uom` in `s`, `ms`, `us`/`µs`, `ns`), never a magnitude heuristic. Boolean values, `NaN`/`Inf`, depth-only primary indexes (`indexKind="MeasuredDepth"`), and unknown units are rejected with explicit `ProtocolPollDisposition` counters. Secondary depth indexes are preserved on `SourceFrame.depth_md_si`.

### 2.3 Evidence-Based Standards Gap Matrix (Phase B4)

| Standard / Capability | Status in DrillAI | Evidence / Verification |
| --- | --- | --- |
| **WITSML 1.4.1.1 SOAP 1.1 `WMLS_GetVersion`, `WMLS_GetCap`, `WMLS_GetFromStore` (time-indexed `<log>`)** | **Implemented & Verified** | `backend/src/drillai/telemetry/protocols/witsml.py`, `test_cp12_protocol_conformance_and_security.py`, `test_cp12_data_integrity.py` |
| **WITSML 1.4.1.1 Result codes (`1` complete, `2` partial/more available, `<=0` error) & SOAP Faults** | **Implemented & Verified** | `WitsmlSoapClient`, `test_b1_b3_independent_witsml_1411_soap_conformance_and_negative_fixtures` |
| **WITSML 1.4.1.1 depth-indexed `<log>`, `<trajectory>`, `<mudLog>`, and write operations (`WMLS_AddToStore`)** | **Out of Scope (Fail-Closed)** | Depth-indexed logs raise `ConnectorTransportError("protocol_error")`; connectors are strictly read-only |
| **WITSML 2.1 XML/JSON data objects over ETP Store (Protocol 4)** | **Out of Scope** | Documented in `PROTOCOL_PROFILE_SPECS` limitations |
| **ETP v1.2 Protocol 0 (`Core`: `RequestSession`, `OpenSession`, `Ping`, `Pong`, `ProtocolException` error + errors map, `CloseSession`)** | **Implemented & Verified** | `backend/src/drillai/telemetry/protocols/etp.py`, `test_b1_b2_independent_etp_12_avro_json_conformance_and_lifecycle` |
| **ETP v1.2 / v1.1 Protocol 1 (`ChannelStreaming`: `Start`/`StartStreaming`, `ChannelDescribe`, `ChannelMetadata`, `ChannelData`, `ChannelStreamingStart`, `StopStreaming`) in Avro-JSON (`{header, body}`) and flat JSON** | **Implemented & Verified** | `EtpWebSocketClient`, `test_cp12_protocol_conformance_and_security.py`, `test_cp12_data_integrity.py` |
| **ETP v1.2 Binary Avro framing, Protocol 3 (`Discovery`), Protocol 4 (`Store`), Protocol 21 (`ChannelSubscribe`)** | **Out of Scope** | Explicitly excluded from `PROTOCOL_PROFILE_SPECS["etp.1.2.json_ws"].supported_operations` |
| **Live External Commercial Vendor Servers (Kongsberg, Pason, Halliburton, SLB)** | **Unverified in CI** | `external_vendor_verified: false` in API and badged in `/connectors` UI |

---

## 3. Security, Outbound SSRF/DNS-Rebinding Defense & Secret Minimization

### 3.1 Secret Reference Minimization (`secret_refs`)
- Plaintext credentials in `config` or `secret_refs` are refused (`422 ValidationFailed`).
- Supported locators: `env:DRILLAI_SECRET_<NAME>`, `secret_ref:<key>`, and `secret:<key>` (normalized to `secret_ref:<key>`).
- **Minimization**: Read serializers (`mask_secret_refs`) redact both the secret value (`masked_value: "********"`) and the internal locator string (`ref: "env:********"` or `"secret_ref:********"`, `redacted: true`) so raw environment variable names (`DRILLAI_SECRET_*`) and internal secret keys are never exposed in API responses, UI screens, or audit logs.
- **Redaction (`redact_sensitive_text`)**: Scrubs `Authorization: Basic/Bearer`, `X-API-Key`, URL query parameters (`?token=`, `&api_key=`, `&access_token=`, `&password=`, `&secret=`, `&sig=`), key/value credential assignments, and `DRILLAI_SECRET_*` names from all transport errors, run ledgers, and logs.

### 3.2 Registration-Time & Connect-Time SSRF / DNS-Rebinding Protection
- `validate_connector_endpoint()` (at registration/update) and `enforce_connect_time_ssrf()` (immediately before every outbound HTTP POST or WebSocket handshake):
  - Reject non-canonical, octal, integer, or shorthand IPv4 literals (`0177.0.0.1`, `2130706433`, `127.1`).
  - Unpack IPv4-mapped IPv6 (`::ffff:127.0.0.1`, `::ffff:169.254.169.254`, `::ffff:10.0.0.1`), 6to4 (`2002::/16`), and Teredo (`2001:0000::/32`) addresses and validate both outer and embedded IPv4 addresses.
  - Validate **all** resolved DNS `A`/`AAAA` records — if any record in a multi-record DNS response is loopback, private RFC-1918, link-local, or cloud metadata (`169.254.169.254`), or if a hostname rebinds from a public IP to a private/loopback IP at connect time, the connection is blocked (`ssrf_blocked`, `retryable=False`).

### 3.3 Full-Payload XML Safety & Fail-Closed Channel Mappings
- `parse_xml_safely()` inspects the entire XML payload (case-insensitive) for `<!DOCTYPE`, `<!ENTITY`, and external `SYSTEM`/`PUBLIC` declarations, enforces `MAX_XML_DEPTH = 32` and `MAX_XML_ELEMENTS = 25000`, and requires a valid SOAP `<Envelope>` or WITSML `<logs>` root element.
- `parse_channel_mappings()` fails closed (`ValidationFailed`) if any stored or supplied mapping is malformed, missing required fields, or uses an invalid unit/dimension.

---

## 4. Optimistic Concurrency, Competing-Worker Leases, Heartbeats & Run Manifests

### 4.1 Atomic Optimistic Concurrency (`expected_config_version`)
- `ConnectorService.update()` and `ConnectorService.transition()` execute an atomic SQL compare-and-swap:
  `UPDATE connectors SET ..., config_version = config_version + 1 WHERE id = :id AND org_id = :org_id AND config_version = :expected`
  If `rowcount != 1`, a `409 Conflict` is raised immediately.
- All mutating endpoints (`/connectors`, `PATCH /connectors/{id}`, `start`, `stop`, `restart`, `disable`, `replay`) enforce org-scoped `Idempotency-Key` reservation (`replay_or_reserve` / `complete`), returning `409 Conflict` if the same key is reused with a different request payload.

### 4.2 Lease Renewal Heartbeat & Mid-Poll Preemption
- During Phase 1 network I/O, `ConnectorWorker` runs a background `_heartbeat_watcher` task that calls `renew_lease()` every `max(0.1s, min(lease_seconds / 3, 2.0s))` to extend `lease_expires_at` and update `last_heartbeat_at` while verifying `worker_id`, `fencing_token`, `config_version`, and `desired_state == 'enabled'`.
- If another worker steals an expired lease or an operator stops/disables/updates the connector mid-poll, `renew_lease()` returns `False` and `_heartbeat_watcher` immediately **cancels** the in-flight `adapter.connect()` task and returns `status = "fenced"`.
- Phase 2 commit verifies both `fencing_token == claimed.fencing_token` AND `config_version == claimed.config_version` before committing ingested frames and advancing `Connector.cursor`.

### 4.3 Reconciled Run Disposition Manifest (`ConnectorRun.details["manifest"]`)
Every poll and replay run persists a bounded manifest (`ProtocolPollDisposition.to_dict()`) recording:
- `source_records_discovered`, `mapped_frames_emitted`, `unmapped_skipped`, `unsubscribed_skipped`, `watermark_suppressed`, `in_batch_duplicates`, `deferred_over_cap`,
- `rejected_malformed_timestamp`, `rejected_unsupported_index`, `rejected_invalid_value`, `rejected_non_finite_value`, `rejected_unknown_channel`, `total_protocol_rejected`,
- `reconciled` (`source_records_discovered == mapped_frames_emitted + total_filtered + total_protocol_rejected`),
- `checkpoint_status` (`"complete"`, `"partial"`, `"unchanged"`, `"failed"`),
- `cursor_before` and `cursor_after`.

---

## 5. Deployment Security & Verification Stack

### 5.1 Hardened `docker-compose.yml`
1. Copy `.env.example` to `.env` and set a strong `POSTGRES_PASSWORD` (required — `docker-compose.yml` enforces `${POSTGRES_PASSWORD:?...}` with no default fallback password).
2. `postgres` runs on an internal-only Docker network (`drillai_internal`, `internal: true`) with no host port published by default.
3. `backend` and `frontend` bind to `127.0.0.1` by default (`${BIND_ADDRESS:-127.0.0.1}:8000:8000` and `${BIND_ADDRESS:-127.0.0.1}:8080:8080`).

```bash
cp .env.example .env
docker compose up --build -d
```

### 5.2 Multi-Process Stack Verification vs Docker Compose CI Smoke Gate
- **Local Multi-Process Stack (`scripts/verify_stack.py`)**: Runs Alembic migrations, starts `uvicorn`, `LocalWitsmlSoapServer`, and standalone `python -m drillai.telemetry.worker` processes, verifies live ingestion and worker restart recovery, and prints `STACK_VERIFICATION_OK`.
- **Docker Compose CI Smoke Gate (`.github/workflows/ci.yml`)**: Validates `docker compose config -q`, builds the production `backend` and `frontend` Docker images, starts `postgres`, `migrate`, `backend`, `connector-worker`, and `frontend`, verifies `/api/v1/health/ready` and `/api/v1/connectors/profiles` through the Nginx proxy container, and tears down cleanly.

---

## 6. Operator Runbooks: Reconciliation & Governed Replay Workbench

### 6.1 Inspecting Run Disposition Manifests
1. Open `/connectors` and select a connector.
2. In the **Bounded Connector Run Ledger**, click **Inspect** on any run to view the **Run Data Disposition & Checkpoint Manifest** (`selected-run-manifest`).
3. Verify `Reconciled`, `Checkpoint status` (`complete` vs `partial` when `Deferred over batch cap > 0`), `Source records discovered`, `Mapped frames emitted`, `Points accepted`, `Duplicates suppressed`, `Rejected`, `Unmapped skipped`, `Watermark suppressed`, and `Cursor before` / `Cursor after`.

### 6.2 Executing a Governed Replay / Backfill (`POST /api/v1/connectors/{id}/replay`)
1. In the **Governed Replay & Backfill Workbench** card on `/connectors`:
   - Optionally enter an ISO-8601 UTC start timestamp (`Replay from timestamp`, e.g. `2026-06-01T00:00:00Z`; leave blank to replay from the connector's current cursor).
   - Enter a mandatory **Replay governance reason**.
2. Click **Dry-Run Replay Preview** (`dry_run: true`) first to inspect how many frames would be discovered and emitted without writing points or mutating the connector's cursor (`run_kind = "replay_dry_run"`).
3. Click **Execute Governed Replay** (`dry_run: false`) to backfill missing points into `TelemetryService` (`run_kind = "replay"`). Already-ingested points are automatically deduplicated (`points_duplicates`), the connector's persistent watermark **never regresses** behind `cursor_before`, and an `AuditLog` entry is recorded with the operator's reason.
