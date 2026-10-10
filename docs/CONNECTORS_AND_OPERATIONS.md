# Telemetry Connectors, WITSML/ETP Protocols, Worker Runtime & Operations Runbook

This document specifies the production integration architecture, protocol profiles, security controls,
fenced competing-worker execution model, deployment topology, and operator runbooks introduced in CP11.

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
  • SSRF endpoint & DNS/IP validation (`validate_connector_endpoint`)
  • Secret reference resolution (`env:DRILLAI_SECRET_*` | `secret_ref:*`)
  • TLS certificate verification (`tls_verify=True` enforced)
        │
        ▼
SourceAdapter (`WitsmlPollingAdapter` | `EtpSubscriptionAdapter` | `ConnectorSyntheticAdapter`)
  • Emits normalized `ChannelDescriptor` + `SourceFrame` batches
  • Computes next source watermark (`cursor_timestamp`, `uid_well`, `uid_log`, `last_session_id`)
        │
        ▼
Durable `ConnectorWorker` (`python -m drillai.telemetry.worker`)
  • Phase 1 (outside DB write lock): CAS lease claim (`fencing_token` + `lease_expires_at`),
    protocol connect & frame fetch
  • Phase 2 (short atomic DB transaction): verify `worker_id` & `fencing_token` CAS,
    ingest frames via `ingest_frames(TelemetryService, adapter, ...)`, advance `Connector.cursor`,
    append `ConnectorRun`, prune bounded run ledger, emit `connector.changed` outbox event
        │
        ▼
`TelemetryService`
  • Unit validation & conversion to canonical SI (`Pa`, `N`, `rad/s`, `m3/s`, `m`, `N.m`)
  • Idempotent point deduplication (`(series_id, ts)` + `source_point_id`)
  • Quality classification (`good`, `questionable`, `bad`, `missing`, `suspect`)
  • Transactional outbox emission (`telemetry.received`, `well_state.changed`)
  • Automatic rule evaluation (`AlertEvaluationService`) -> `alert.changed`
        │
        ▼
Per-Well Live Stream (`/api/v1/wells/{well_id}/live/stream`) & Operational Monitor UI
```

---

## 2. Supported Protocol Profiles & Verification Honesty

| Profile Key | Standard / Version | Transport | Auth Modes | Resume / Watermark | Local Harness Verified | External Commercial Vendor Verified |
| --- | --- | --- | --- | --- | --- | --- |
| `witsml.1.4.1.1.soap_http` | Energistics WITSML 1.4.1.1 | HTTP/HTTPS SOAP 1.1 (`WMLS_GetFromStore`) | `none`, `basic`, `bearer` | `<startDateTimeIndex>` (`cursor_timestamp` ISO-8601 UTC) | **Yes** (`LocalWitsmlSoapServer` over TCP) | **No** (no external vendor server in CI) |
| `etp.1.2.json_ws` | Energistics ETP v1.2 (JSON framing subset) | WebSocket (`ws://` / `wss://`, subprotocol `energistics-tp`) | `none`, `basic`, `bearer` | `Start.startTimestamp` (`cursor_timestamp` ISO-8601 UTC) | **Yes** (`LocalEtpWebSocketServer` over TCP) | **No** (no external vendor server in CI) |
| `synthetic.v1` | DrillAI Synthetic v1 | In-process deterministic plan generator | `none` | `step_index` + `cursor_timestamp` | **Yes** | **N/A** (`SYNTHETIC / TEST SOURCE`) |

### Explicit Scope & Limitations
1. **`witsml.1.4.1.1.soap_http`**:
   - Implements read-only `WMLS_GetFromStore` polling for time-indexed `<log>` objects (`<logCurveInfo>` + comma-delimited `<logData><mnemonicList>/<unitList>/<data>`).
   - Handles WITSML positive status codes (`1` complete, `2` partial/truncated with bounded page continuation) and negative error codes (`<Result> < 0`) or `<soap:Fault>`.
   - Depth-indexed logs, trajectory/mudLog/bhaRun objects, and write operations (`WMLS_AddToStore`, `WMLS_UpdateInStore`, `WMLS_DeleteFromStore`) are out of scope.
2. **`etp.1.2.json_ws`**:
   - Implements ETP v1.2 Protocol 0 (`Core`: `RequestSession`, `OpenSession`, `Ping`, `Pong`, `ProtocolException`, `CloseSession`) and Protocol 1 (`ChannelStreaming`: `Start`, `ChannelMetadata`, `ChannelData`) using JSON message framing over WebSocket (`energistics-tp`).
   - Binary Avro framing and ETP Discovery/Store protocols (Protocols 3/4) are out of scope.
3. **Verification Honesty**:
   - Automated tests in CI verify real TCP/HTTP/WebSocket client-server exchanges against `LocalWitsmlSoapServer` and `LocalEtpWebSocketServer`.
   - External commercial vendor servers (e.g. Kongsberg, Pason, Halliburton, SLB live rig stores) are **not** connected in CI and are explicitly badged as `External commercial vendor unverified in CI` in both API metadata (`external_vendor_verified: false`) and the operator UI.

---

## 3. Security, SSRF Protection & Secret References

### 3.1 Secret References (`secret_refs`)
Plaintext passwords, bearer tokens, or API keys are rejected (`422 ValidationFailed`) if supplied in
`config` or `secret_refs`. Credentials must be referenced via locator strings:
- **Environment locator**: `env:DRILLAI_SECRET_<NAME>` (must start with `DRILLAI_SECRET_`, uppercase alphanumeric/underscore).
- **Platform SecretRef table locator**: `secret_ref:<key>` (resolved tenant-scoped from `secret_refs` table).

Every read serializer (`GET /api/v1/connectors`, `GET /api/v1/connectors/{id}`) masks configured secret
values as `"********"`. Error messages, run diagnostics, and structured logs pass through
`redact_sensitive_text()` so credentials never leak into logs, audit records, or UI banners.

### 3.2 SSRF & Transport Security (`validate_connector_endpoint`)
- Allowed schemes: `http://`, `https://` for WITSML; `ws://`, `wss://` for ETP; `synthetic://` for `synthetic.v1`. `file://`, `ftp://`, `gopher://`, and URLs with embedded `user:pass@host` credentials are unconditionally rejected.
- Cloud metadata and link-local addresses (`169.254.169.254`, `fe80::/10`, `metadata.google.internal`, `metadata.azure.com`) are unconditionally blocked in all environments.
- Loopback (`127.0.0.0/8`, `::1`, `localhost`) and RFC-1918 private networks (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`) are blocked by default. Loopback can only be enabled in non-production (`DRILLAI_CONNECTOR_ALLOW_LOOPBACK=true`) for local protocol harness testing; in `DRILLAI_ENVIRONMENT=production`, `DRILLAI_CONNECTOR_ALLOW_LOOPBACK=true` causes fail-closed startup refusal.
- TLS certificate verification (`tls_verify: true`) is mandatory; `tls_verify: false` is rejected.

---

## 4. Competing-Worker Lease, Fencing Token & Checkpoint Safety

### 4.1 Lease Acquisition & Monotonic Fencing Token
`ConnectorWorker` claims due connectors (`is_enabled=True`, `desired_state='enabled'`, `next_poll_at <= now`, and either unleased or `lease_expires_at <= now`) using an atomic compare-and-swap (CAS) `UPDATE`:
- Increments `Connector.fencing_token` by `+1`.
- Sets `worker_id`, `last_heartbeat_at = now`, and `lease_expires_at = now + lease_seconds`.
- If a prior worker's lease expired (`lease_expires_at <= now`), the new worker increments `reconnect_count` and records metric `drillai_connector_lease_recoveries_total`.

### 4.2 Two-Phase Poll Execution & Stale-Worker Fencing
1. **Phase 1 (Network I/O outside DB write transaction)**:
   The worker loads connector config and executes `await adapter.connect()` over the network without holding a database write transaction.
2. **Phase 2 (Short atomic write transaction with fencing verification)**:
   The worker opens a short DB write session, verifies that `Connector.worker_id == self.worker_id` and `Connector.fencing_token == claimed.fencing_token` and `Connector.desired_state == 'enabled'`, ingests frames through `TelemetryService`, and executes a fenced `UPDATE ... WHERE id = :id AND worker_id = :wid AND fencing_token = :token`.
   - If another worker stole the lease after lease expiry or an operator stopped/disabled the connector while Phase 1 was in flight, the stale worker rolls back its transaction, discards the poll result, and increments `drillai_connector_fenced_stale_commits_total`.
   - Source watermarks (`Connector.cursor`) advance **only** inside the same transaction that commits ingested `TimeSeriesPoint` rows. If ingestion fails, the transaction rolls back and `Connector.cursor` remains at `cursor_before`.

---

## 5. Non-Conflated Health Semantics

The platform separates four distinct operational dimensions:
1. **Process Readiness (`/api/v1/health/ready`)**: Whether the FastAPI process, database, migration head, and embedded worker loop are healthy.
2. **Desired Intent (`desired_state`)**: Operator-governed target state (`enabled`, `stopped`, `disabled`).
3. **Observed Runtime Status (`status`)**: Worker execution state (`configured`, `starting`, `running`, `backing_off`, `degraded`, `failed`, `stopped`, `disabled`).
4. **Operational Health & Data Freshness (`health.health_state`, `health.is_live`, `health.data_freshness`)**:
   - `is_live` is `true` **only** when `desired_state == 'enabled'`, `status == 'running'`, `error_count == 0`, `data_freshness == 'fresh'`, and `has_low_quality == false`.
   - Stopped or disabled connectors report `stopped` / `disabled` (never `LIVE`).
   - Running connectors whose latest telemetry is older than `DRILLAI_TELEMETRY_FRESH_SECONDS` report `stale_data`.
   - Running connectors whose latest batch contains untrustworthy quality (`bad`, `missing`, `suspect`) report `low_quality_data`.

---

## 6. Deployment & Verification Stack

### 6.1 Multi-Container Compose Stack
Copy `.env.example` to `.env` and start the reproducible stack:

```bash
cp .env.example .env
docker compose up --build -d
```

Services defined in `docker-compose.yml`:
- `postgres`: PostgreSQL 16 with `pg_isready` healthcheck.
- `migrate`: One-shot `python -m alembic upgrade head && python -m alembic check` init container.
- `backend`: FastAPI server (`0.0.0.0:8080`) with `/api/v1/health/ready` healthcheck.
- `connector-worker`: Dedicated `python -m drillai.telemetry.worker` process with independent worker heartbeat and lease management.
- `frontend`: Multi-stage Vite production build served by Nginx (`0.0.0.0:5173`), proxying `/api/` and WebSocket `/api/v1/wells/*/live/stream` to `backend:8080`.

### 6.2 Automated Multi-Process Stack Verification
To verify clean migration, backend readiness, dedicated `connector-worker` process polling against a real local WITSML 1.4.1.1 SOAP server, telemetry ingestion, and restart recovery without Docker daemon privileges:

```bash
backend/.venv/bin/python scripts/verify_stack.py
```

---

## 7. Operator Runbooks

### 7.1 Onboarding a New Well & Telemetry Connector
1. Ensure required credentials are provided as environment variables (`export DRILLAI_SECRET_RIG01_USER=...`, `export DRILLAI_SECRET_RIG01_PASS=...`) on both `backend` and `connector-worker`.
2. Navigate to `/connectors` in the workspace (or click **Manage Connectors** from the Well Cockpit Operational Monitor).
3. Select the target Well, choose `witsml.1.4.1.1.soap_http` or `etp.1.2.json_ws`, enter the HTTPS/WSS endpoint URL, and enter secret references (`env:DRILLAI_SECRET_RIG01_USER`, `env:DRILLAI_SECRET_RIG01_PASS`).
4. Map source mnemonics (e.g. `SPP`, `WOB`, `RPM`) to canonical platform channel keys (`spp`, `wob`, `rpm`), dimensions (`pressure`, `force`, `rotary_speed`), and source units (`psi`, `klbf`, `rpm`).
5. Click **Register Connector**, then click **Test Connection** and **Preview Samples** to verify connectivity, credentials, and unit normalization without writing to time-series storage.
6. Click **Start** to enable durable worker ingestion.

### 7.2 Diagnosing Failing, Backing-Off, or Stale Connectors
- **`error_category = auth_failure`**: Verify the `env:DRILLAI_SECRET_*` variable is set in the `connector-worker` environment and matches the rig server's credentials.
- **`error_category = tls_error`**: Verify the rig endpoint presents a valid certificate chain trusted by the system CA bundle. Never set `tls_verify=false`.
- **`error_category = ssrf_blocked`**: The endpoint resolved to a loopback, link-local, or private RFC-1918 IP not listed in `DRILLAI_CONNECTOR_ALLOWED_HOSTS`. Add the approved rig hostname to `DRILLAI_CONNECTOR_ALLOWED_HOSTS`.
- **`status = backing_off` / `failed`**: Inspect the **Connector Run Ledger** table on `/connectors` for `error_category` and redacted `error_message`. Once the upstream issue is resolved, click **Restart** (with an operational reason) to reset `error_count` and trigger immediate polling.
- **`health_state = low_quality_data`**: The upstream rig store is transmitting `-999.25` null values or `bad`/`suspect` quality flags. Inspect sensor calibration at the rig; DrillAI preserves the points with `is_trustworthy=false` and suppresses false threshold alerts.
