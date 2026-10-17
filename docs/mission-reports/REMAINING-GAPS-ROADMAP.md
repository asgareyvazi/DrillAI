# DrillAI CP16 — Remaining Gaps & Post-CP16 Engineering Roadmap

All in-scope P0, P1, and P2 engineering, security, async event-loop, physics, deployment, and documentation findings identified during the CP16 adversarial audit (**F-CP16-01 through F-CP16-08**) have been closed with regression tests. This document records the remaining external dependencies, configuration-shaped infrastructure boundaries, and future roadmap items with complete honesty.

---

## 1. Blocked by External Evidence (`BLOCKED_BY_EXTERNAL_EVIDENCE`)

| Gap ID | Area | Current State | Why Blocked in Sandbox / CI | Operator Execution Path |
| --- | --- | --- | --- | --- |
| **GAP-EXT-01** | **Live Commercial WITSML 1.4.1.1 Store Interoperability (`witsml.1.4.1.1.soap_http`)** | `conformance_verified: true`, `local_harness_verified: true`, **`external_vendor_verified: false`** | CI and the Arena sandbox have no network route or license credentials to a live third-party commercial WITSML 1.4.1.1 rig server (Kongsberg SiteCom, Pason, Halliburton Sperry, SLB). | Provision network access in a staging/rig environment, set `DRILLAI_EXTERNAL_WITSML_URL`, `DRILLAI_EXTERNAL_WITSML_USERNAME`, and `DRILLAI_EXTERNAL_WITSML_PASSWORD`, and execute `backend/.venv/bin/python scripts/verify_external_vendor_interop.py --mode external --require-external`. |
| **GAP-EXT-02** | **Live Commercial ETP v1.2 Store Interoperability (`etp.1.2.binary_ws`)** | `conformance_verified: true`, `local_harness_verified: true`, **`external_vendor_verified: false`** | CI and the Arena sandbox have no network route or license credentials to a live third-party commercial ETP v1.2 server. | Provision network access in a staging/rig environment, set `DRILLAI_EXTERNAL_ETP_URL` and `DRILLAI_EXTERNAL_ETP_TOKEN`, and execute `backend/.venv/bin/python scripts/verify_external_vendor_interop.py --mode external --require-external`. |

---

## 2. Explicit Configuration & Infrastructure Boundaries

| Gap ID | Area | Current Implementation | Production Activation Requirement |
| --- | --- | --- | --- |
| **GAP-INF-01** | **Vector Retrieval Backend (`DRILLAI_VECTOR_BACKEND`)** | Defaults to deterministic in-memory vector search (`memory`) alongside lexical/hybrid and metadata-scoped retrieval. | Set `DRILLAI_VECTOR_BACKEND=pgvector` against a PostgreSQL instance with the `vector` extension installed and indexed. |
| **GAP-INF-02** | **Outbound Notification Delivery (`output.notify`)** | Workflow `output.notify` nodes enforce L4 approval gates and record structured notification audit payloads via deterministic adapter stubs. | Configure production webhook/SMTP/Telegram/WhatsApp credentials in deployment environment variables when external alerting is enabled. |
| **GAP-INF-03** | **LLM & Embedding Providers (`DRILLAI_LLM_PROVIDER`)** | Defaults to deterministic, offline-verifiable provider (`stub` / `deterministic`) while enforcing prompt budgets, citations, and L2 agent ceilings. | Configure enterprise LLM/embedding provider endpoints and API keys via secret references (`secret_ref:*` or `env:DRILLAI_SECRET_*`). |

---

## 3. Intentionally Out-of-Scope Protocol & Physics Extensions (Future Roadmap)

1. **WITSML Write Operations & Depth-Indexed Logs**:
   - DrillAI telemetry connectors are strictly read-only (`WMLS_GetVersion`, `WMLS_GetCap`, `WMLS_GetFromStore` on time-indexed `<log>` objects). `WMLS_AddToStore`, `WMLS_UpdateInStore`, `WMLS_DeleteFromStore`, and depth-indexed log polling are intentionally excluded and fail closed.
2. **Additional ETP v1.2 Protocols & Compression**:
   - `etp.1.2.binary_ws` implements ETP v1.2 Protocol 0 (`Core`) and Protocol 1 (`ChannelStreaming`) with uncompressed binary Avro frames over subprotocol `energistics-tp`. Protocol 3 (`Discovery`), Protocol 4 (`Store`), Protocol 21 (`ChannelSubscribe`), and `gzip`/`lz4` payload compression are rejected with `ProtocolException` (messageType `1000`).
3. **Advanced Non-Steady-State & Finite-Element Physics**:
   - Current engineering engines implement deterministic industry-standard analytical formulations (API TR 5C3 pipe-body ratings, Johancsik soft-string torque & drag, Bingham/Power-Law steady-state laminar hydraulics, Wait-and-Weight kill sheet). Coupled thermal-multiphase transient hydraulics, finite-element stiff-string buckling, and thread/coupling connection ratings remain future domain-pack extensions.
