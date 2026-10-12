# Telemetry Connectors, WITSML/ETP Protocols, Worker Runtime & Operations Runbook

This document specifies the production integration architecture, protocol profiles, standards gap matrix,
checkpoint and data disposition invariants, connection-bound SSRF and private rig-network egress controls,
fenced competing-worker execution model, deployment topology, and operator runbooks certified in CP11,
CP12, and CP13.

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

| Profile Key | Standard / Version | Transport & Wire Encoding | Production Status | Resume / Watermark | Independent Conformance Suite Verified | External Commercial Vendor Verified |
| --- | --- | --- | --- | --- | --- | --- |
| `witsml.1.4.1.1.soap_http` | Energistics WITSML 1.4.1.1 | HTTP/HTTPS SOAP 1.1 (`WMLS_GetVersion`, `WMLS_GetCap`, `WMLS_GetFromStore`) | **Production Certified** (`production_ready: true`, `internal_test_only: false`) | Inclusive `<startDateTimeIndex>` (`cursor_timestamp` + `completed_at_cursor` + `has_more_deferred`) | **Yes** (`LocalWitsmlSoapServer` + 14-scenario SOAP 1.1 conformance suite) | **No** (`false` — no external commercial rig store in CI) |
| `etp.1.2.binary_ws` | Energistics ETP v1.2 | WebSocket (`ws://` / `wss://`, subprotocol `energistics-tp`), **Binary Avro** (`MessageHeader` + Protocol 0 Core & Protocol 1 ChannelStreaming binary records) | **Production Certified** (`production_ready: true`, `internal_test_only: false`) | Per-channel `channel_cursors[channel_key]` (`last_timestamp`, `last_index_raw`, `recent_point_ids`) | **Yes** (25-scenario independent Avro binary encoder/server conformance suite) | **No** (`false` — no external commercial ETP store in CI) |
| `etp.1.2.json_ws` | Energistics ETP v1.2 / v1.1 (JSON test/debug subset) | WebSocket (`ws://` / `wss://`, subprotocol `energistics-tp`), Avro-JSON / flat JSON text frames | **Internal Test / Debug Only** (`production_ready: false`, `internal_test_only: true` — refused in `DRILLAI_ENV=production`) | Per-channel `channel_cursors[channel_key]` | **Yes** (`LocalEtpWebSocketServer` + Avro-JSON test suite) | **No** (`false`) |
| `synthetic.v1` | DrillAI Synthetic v1 | In-process deterministic plan generator | **Commissioning / Test Only** (`production_ready: false`, `internal_test_only: true`) | `step_index` + `cursor_timestamp` | **Yes** | **N/A** (`SYNTHETIC / TEST SOURCE`) |

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

### 3.1 Connection-Bound SSRF & DNS-Rebinding Protection (`PinnedEndpoint`)
- `resolve_and_pin_endpoint()` validates the endpoint and returns an immutable `PinnedEndpoint` containing `pinned_ip`, `port`, `host_header`, `sni_hostname`, `connect_url`, and `is_tls`:
  - **DNS Resolution Timeout**: Bounded by `DNS_RESOLVE_TIMEOUT_SECONDS = 3.0s` inside a worker thread (`ssrf_blocked` on timeout).
  - **Strict Address Validation**: Rejects non-canonical, octal, integer, or shorthand IPv4 literals (`0177.0.0.1`, `2130706433`, `127.1`), unpacks IPv4-mapped IPv6 (`::ffff:127.0.0.1`, `::ffff:169.254.169.254`, `::ffff:10.0.0.1`), 6to4, and Teredo addresses, and validates **every** resolved `A`/`AAAA` record (if any record in a multi-record response is prohibited, the connection is refused).
  - **Socket-Level Pinning**:
    - `WitsmlSoapClient` connects directly to `pinned.connect_url` (pinned IP literal) with `headers={"Host": pinned.host_header}`, `follow_redirects=False`, `trust_env=False`, and `extensions={"sni_hostname": pinned.sni_hostname}` so HTTPX never re-resolves the hostname and TLS verifies the certificate against the original hostname.
    - `EtpBinaryWebSocketClient` and `EtpWebSocketClient` pass `host=pinned.pinned_ip, port=pinned.port, proxy=None, server_hostname=pinned.sni_hostname` to `websockets.asyncio.client.connect()`, binding the TCP socket directly to the validated IP while sending the original `Host` header and TLS SNI hostname.

### 3.2 Explicit Private Rig-Network Policy & Loopback Isolation
- **No Implicit Loopback**: Ordinary user-supplied connectors cannot connect to `127.0.0.1` or `::1` unless `DRILLAI_CONNECTOR_ALLOW_LOOPBACK=true` is explicitly set. In non-production (`development`/`test`), only active `(host, port)` endpoints registered by `LocalProtocolHarnessManager` in `_HARNESS_OWNED_ENDPOINTS` (or listed in `DRILLAI_CONNECTOR_HARNESS_PORTS`) are reachable on loopback. In `DRILLAI_ENV=production`, harness endpoints are completely disabled (`403 Forbidden`).
- **Private Rig-Network CIDR Governance (`GET / PUT /api/v1/connectors/egress-policy`)**:
  - Private RFC-1918 and CGNAT addresses (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `100.64.0.0/10`) are blocked by default unless covered by an approved CIDR in `DRILLAI_CONNECTOR_ALLOWED_PRIVATE_NETWORKS` or configured via `PUT /api/v1/connectors/egress-policy` (admin-only `L4`, audited with operator reason).
  - Loopback (`127.0.0.0/8`, `::1/128`), link-local/cloud metadata (`169.254.0.0/16`, `fe80::/10`), and multicast/unspecified CIDRs are rejected even if submitted to `PUT /api/v1/connectors/egress-policy`, and allowlisted hostnames (`allowed_hosts`) can never bypass loopback or metadata IP blocks.

### 3.3 Secret Reference Minimization (`secret_refs`)
- Plaintext credentials in `config` or `secret_refs` are refused (`422 ValidationFailed`).
- Read serializers (`mask_secret_refs`) redact both the secret value (`masked_value: "********"`) and the internal locator string (`ref: "env:********"` or `"secret_ref:********"`, `redacted: true`), and `redact_sensitive_text()` scrubs headers, query tokens, and `DRILLAI_SECRET_*` names from all transport errors, run ledgers, and logs.

---

## 4. Competing-Worker Leases, Expiry Fencing, Connection Milestones & Run Manifests

### 4.1 Lease Renewal Heartbeat & Expired-Lease Fencing
- During Phase 1 network I/O, `ConnectorWorker` runs a background `_heartbeat_watcher` task that calls `renew_lease()` every `max(0.1s, min(lease_seconds / 3, 2.0s))` to extend `lease_expires_at` while verifying `worker_id`, `fencing_token`, `config_version`, `desired_state == 'enabled'`, AND `lease_expires_at > now`.
- Both Phase 2 commit (`execute_claimed_poll`) and failure recording (`_record_failed_outcome`) verify `worker_id == self.worker_id`, `fencing_token == claimed.fencing_token`, `config_version == claimed.config_version`, AND `lease_expires_at > now` in their pre-check and SQL CAS `WHERE` clauses so an expired worker cannot commit data, advance checkpoints, or overwrite connector status even if no second worker has claimed the connector yet.

### 4.2 Ten-Stage Connection-Test Milestone Pipeline (`POST /api/v1/connectors/{id}/test`)
Every connection test returns ordered, structured `milestones`:
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

### 4.3 Reconciled Run Disposition Manifest & `data_outcome` (`ConnectorRun.details["manifest"]`)
Every poll and replay run persists a bounded manifest (`ProtocolPollDisposition.to_dict()`) recording:
- `data_outcome`:
  - `"all_valid_accepted"` — all mapped frames accepted cleanly
  - `"partial_with_rejections"` — valid points accepted alongside protocol or ingestion rejections
  - `"rejected_only"` — source records discovered, 0 points accepted, >=1 rejected
  - `"partial_batch_capped"` — batch capped at `max_points_per_poll` (`has_more_deferred = true`)
  - `"no_data"` — 0 points accepted and 0 rejected
- `reconciled` (`source_records_discovered == mapped_frames_emitted + total_filtered + total_protocol_rejected`),
- `checkpoint_status` (`"complete"`, `"partial"`, `"unchanged"`, `"failed"`),
- `cursor_before` and `cursor_after` (including per-channel `channel_cursors` for ETP).

---

## 5. Deployment Security & Verification Stack

### 5.1 Hardened `docker-compose.yml`
1. Copy `.env.example` to `.env` and set a strong `POSTGRES_PASSWORD` (required — `docker-compose.yml` enforces `${POSTGRES_PASSWORD:?...}` with no default fallback password).
2. `postgres` runs on an internal-only Docker network (`drillai_internal`, `internal: true`) with no host port published by default.
3. `backend` and `frontend` bind to `127.0.0.1` by default (`${BIND_ADDRESS:-127.0.0.1}:8000:8000` and `${BIND_ADDRESS:-127.0.0.1}:8080:8080`).
4. `DRILLAI_CONNECTOR_ALLOW_LOOPBACK` defaults to `false`; private rig networks are configured via `DRILLAI_CONNECTOR_ALLOWED_PRIVATE_NETWORKS` and `DRILLAI_CONNECTOR_ALLOWED_HOSTS`.

```bash
cp .env.example .env
docker compose up --build -d
```

### 5.2 Multi-Process Stack Verification (`scripts/verify_stack.py`) & Docker Compose CI Smoke Gate
- **Local Multi-Process Stack (`scripts/verify_stack.py`)**: Runs Alembic migrations, starts `uvicorn`, `LocalWitsmlSoapServer`, `LocalEtpBinaryWebSocketServer`, and standalone `python -m drillai.telemetry.worker` processes, verifies live WITSML SOAP and ETP 1.2 Binary Avro ingestion, worker restart recovery without duplicate points, and governed stop state, and prints `STACK_VERIFICATION_OK`.
- **Docker Compose CI Smoke Gate (`.github/workflows/ci.yml`)**: Validates `docker compose config -q`, builds the production `backend` and `frontend` Docker images, starts `postgres`, `migrate`, `backend`, `connector-worker`, and `frontend`, verifies `/api/v1/health/ready` and `/api/v1/connectors/profiles` (including `etp.1.2.binary_ws`) through the Nginx proxy container, and tears down cleanly.
