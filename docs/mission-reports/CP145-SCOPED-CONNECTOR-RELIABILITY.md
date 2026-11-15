# CP145 — Scoped Connector Runtime Reliability

**Date:** 2026-10-20

**Status:** Bounded worker/runtime configuration and fail-closed persisted-config validation are implemented. The complete local telemetry test package passed with only its four reviewed PostgreSQL-marked tests skipped. The full backend suite, PostgreSQL-enabled run, exact-SHA CI, and field/vendor acceptance are not established here.

**Evidence class:** deterministic software tests with synthetic connector rows and local protocol harnesses. No rig historian, vendor endpoint, customer system, or external provider was contacted.

## Scope and behavior

- Connector polling (`0.05–3,600s`), timeout (`0.2–60s`), and backoff (`0.01 <= base <= max <= 3,600s`) values must be finite and bounded. Point caps (`1–500`) and failure thresholds (`1–50`) are exact integers. Optional protocol budgets are validated as exact bounded integers: retries `0–5`, WITSML return nodes `1–500`, WITSML pages `1–10`, and ETP data items/message rate `1–500`. Synthetic `loop` is required to be a boolean when present. Booleans and fractional values do not pass as integers; exact integer strings remain supported.
- `Settings` and direct `ConnectorWorker` construction enforce worker limits: lease `0.45–300` seconds, poll interval `0.05–60` seconds, concurrency `1–64`, and per-connector `ConnectorRun` retention `1–10,000`. Invalid settings fail closed instead of starting a tight loop, creating leases that expire immediately, creating excessive parallel work, or disabling ledger pruning.
- Before a claimed worker does endpoint policy/DNS validation, secret resolution, or adapter construction, it validates the connector's persisted configuration with the same normalizer used by the connector service. Create/update validate settings before endpoint DNS; connection test, preview and replay validate persisted rows before endpoint checks, secret lookup, or adapter construction. This catches malformed legacy or out-of-band rows before transport work.
- Invalid persisted configuration is recorded with the bounded category `invalid_config` and a generic, sanitized error. The connector enters terminal `failed` state with no scheduled retry until an authorized operator repairs and restarts it. The failed run records the unchanged cursor before/after; no adapter is constructed and no channel or point is written.
- The adapter builder independently validates persisted config for non-worker callers as well. This does not authorize a vendor operation or change the connector's configured endpoint.

## Verification observed

- Full local `tests/telemetry` selection: **302 passed, 4 PostgreSQL-marked skipped** in 113.80 seconds, with two non-failing pytest/JUnit `record_property` warnings. The suite includes local protocol harnesses, tenant/scope boundaries, worker lease/fencing and recovery, ingestion/checkpoint reconciliation, SSRF/DNS executor tests, bounded telemetry queries, and the new configuration regressions.
- Focused connector registry/worker files: **12 passed, 1 PostgreSQL-marked skipped**. This selection includes strict finite/integer bounds, invalid worker startup limits, and a persisted `Infinity` timeout regression that asserts no endpoint-validation/DNS call, secret lookup, adapter creation, point write, or cursor advance.
- Eight-file connector scope/recovery selection: **29 passed, 3 PostgreSQL-marked skipped** in 24.31 seconds. It covers competing claims, stale fencing, run retention, WITSML/ETP local harnesses, error backoff, egress-policy recovery, and reconciliation.
- Full backend collection: **1,769 tests across 137 files** (`pytest --collect-only`, 2026-10-20). The reviewed PostgreSQL inventory remains **15 node IDs across 12 files**. Manifest expectations are Linux 1,769/0 skipped, default SQLite 1,754 passed/15 PostgreSQL-only skips, and Windows 1,753 passed/one identified POSIX-only skip/15 PostgreSQL deselections. These are inventory expectations, not observed full-backend results.
- Full backend Ruff and `compileall` passed. The CP16 documentation/count guard passed; E2E shape verification accepted 128 tests across 20 files; backend manifest arithmetic reconciled, and its PostgreSQL checker confirmed all 15 reviewed node IDs across 12 files. `git diff --check` passed.

## Limits and disposition

The local synthetic and protocol-harness checks do not establish vendor interoperability, field measurement quality, customer acceptance, production reliability, or operational readiness. No HTTP, SOAP, WebSocket or SMTP request to a vendor was made. Application-level leases and retry bounds do not establish production RPO/RTO, and this phase does not claim a production recovery exercise. Connector identity remains tenant/well/wellbore/operation-scoped; runtime validation does not invent missing scope, units, timestamps, or source provenance. Keep `well_control.kill_sheet` and `tubulars.api_5c3` at `NEEDS_FIELD_VALIDATION`; no autonomous rig control is authorized or implied. The CP142 report-date follow-up remains unresolved. Full backend, Linux/native-Windows exact-final-SHA, synthetic two-well browser/API/DB pilot, and final assurance gates remain for CP151.
