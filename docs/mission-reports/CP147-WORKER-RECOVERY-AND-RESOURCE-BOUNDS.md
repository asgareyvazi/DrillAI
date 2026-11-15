# CP147 — Worker Recovery, Resource Bounds, and Database Drill

**Date:** 2026-10-20

**Status:** Notification worker startup limits are enforced both in application Settings and direct construction. Local worker recovery, migration, and isolated embedded-PostgreSQL backup/restore failure paths passed. Production disaster-recovery objectives and operator-environment restorability are not established.

**Evidence class:** Deterministic software tests with synthetic notification rows, an isolated temporary embedded PostgreSQL server, generated fixture data, and local database/migration tooling. No vendor, recipient, customer, or production system was contacted.

## Implemented and exercised

- Notification polling must be finite and within **0.2–60 seconds**; batch size must be an exact integer within **1–100**. Settings reject NaN/Infinity, booleans, and out-of-range values; direct `NotificationWorker` construction applies equivalent checks and rejects fractional batch overrides before starting.
- Existing recovery behavior remains conservative: a worker-interrupted `sending` row is not silently retried after its remote outcome becomes ambiguous. Stale sends become `reconciliation_required`, retain duplicate-risk acknowledgement requirements, and can be requeued only through the audited tenant-scoped path.
- The PostgreSQL drill uses a temporary embedded server and isolated source/restore databases. It runs Alembic upgrade/check, writes synthetic multi-tenant rows, takes a custom-format `pg_dump`, verifies checksum/size, restores into a clean target, compares row counts and relationships, checks tenant-isolated application readback and migration revision/drift, and confirms a corrupted archive fails restore. The SQLite online-backup test remains explicitly a local/dev mechanism.

## Verification observed

- `tests/workflow/test_notification_worker.py`: **12 passed, 1 PostgreSQL-marked skipped**. Includes worker configuration bounds, due-row dispatch, stale-send reconciliation, and a simulated process interruption/recovery path.
- `DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest -o addopts='' -q tests/db/test_cp18_postgres_backup_restore.py tests/db/test_migrations_postgres.py`: **5 passed**, with one non-failing `XDG_RUNTIME_DIR` fallback warning.
- Ruff and `compileall` passed on the changed Python modules; `git diff --check` passed.
- Backend collection: **1,771 tests across 137 files**. The reviewed PostgreSQL inventory remains **15 node IDs across 12 files**. Current manifest expectations are Linux 1,771/0 skipped, default SQLite 1,756/15 PostgreSQL-only skips, and Windows 1,755/1 identified POSIX-only skip/15 PostgreSQL deselections. These are reviewed expectations, not full-platform observations.
- No test establishes production RPO/RTO, backup schedule coverage, retention, infrastructure loss behavior, restore operator competency, or recovery performance objectives.
- Fresh combined revalidation with the notification API and CP148/CP26 authorization tests passed **21 tests, 1 PostgreSQL-marked skip**, including the 12 notification-worker passes and one reviewed PostgreSQL-only skip. The separate embedded-PostgreSQL restore/migration selection's 5-pass result above is the previously recorded drill evidence.

## Limits and disposition

All database destruction/recreation in the CP18 drill occurs inside its temporary embedded PostgreSQL instance; no configured operator database or user test data is targeted. Test durations and synthetic row parity are software evidence only. No production or customer backup was accessed, no real production restore was attempted, and no RPO/RTO guarantee is made. SMTP/webhook delivery, provider responses, field operations, and customer acceptance are outside this phase. Keep `well_control.kill_sheet` and `tubulars.api_5c3` at `NEEDS_FIELD_VALIDATION`; no autonomous rig control is implied. Exact-final-SHA Linux/native-Windows certification and the integrated browser pilot remain CP151 work.
