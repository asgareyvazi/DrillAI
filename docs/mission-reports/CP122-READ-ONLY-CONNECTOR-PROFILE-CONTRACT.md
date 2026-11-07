# CP122 — Read-Only WITSML/ETP Connector Profile Contract

## Disposition

| Item | Status | Evidence / boundary |
| --- | --- | --- |
| Declare read-only behavior in the protocol catalogue | **DONE (software)** | Each registered source profile now exposes `read_only: true` in `GET /connectors/profiles`. The declaration is visible in the Connectors page in English and Persian. |
| Guard the published remote operation surface | **DONE (software)** | Existing WITSML operations remain `WMLS_GetVersion`, `WMLS_GetCap`, and `WMLS_GetFromStore`; ETP remains Protocol 0 Core + Protocol 1 ChannelStreaming. A registry API test rejects write/store operations from the published operation allowlists. |
| External vendor, independent peer, or field qualification | **BLOCKED_EXTERNAL** | No external endpoint was contacted. `external_vendor_verified` and `independent_peer_verified` remain false; local harness tests do not substitute for vendor interoperability. |

## Implementation

`ProtocolProfileSpec` now serializes an explicit `read_only` capability instead of leaving the direction of data flow implicit in English `limitations` text. All four registered profiles (synthetic, WITSML 1.4.1.1 SOAP, ETP 1.2 Binary Avro, and internal ETP JSON test/debug) are source-only. The WITSML and ETP adapters still perform their existing polling/subscription operations; CP122 adds no write capability.

The Connectors workspace shows a localized read-only badge on each capability card. The backend registry/API test checks all profiles, the exact WITSML Get operation surface, ETP's `channel_streaming` object subset, and the absence of common WITSML/ETP Store mutation verbs. Existing SOAP/ETP protocol and SSRF tests exercise the transport behavior independently.

The field describes actions implemented by DrillAI. It cannot guarantee behavior of a remote server. The ETP `TruncateChannels` notification is retained as a protocol message in ChannelStreaming and is not advertised as a Store write operation.

## Verification

- Backend connector registry, WITSML/ETP conformance and SSRF selection: **26 passed**.
- Changed-file Ruff, `compileall`, and `git diff --check` passed.
- Frontend typecheck and lint passed; `ConnectorsPage.test.tsx`: **7 passed**, including localized read-only badges for WITSML and ETP.
- No test was added, so the reviewed backend inventory remains **1,710 collected across 128 files**, including 14 PostgreSQL node IDs across 11 files.

## Limits

A catalogue property and local protocol tests are software evidence only. No commercial WITSML or ETP server, live endpoint, vendor transcript, operational handoff, production deployment, or external peer certification was used. Do not interpret `read_only` as a vendor guarantee or as external acceptance. Exact-final-SHA Linux/native-Windows certification remains required at CP132.
