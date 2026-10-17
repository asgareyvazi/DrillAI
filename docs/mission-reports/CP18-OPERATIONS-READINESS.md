# CP18 Mission Report — Production Operations & Database Recovery Evidence

- **Baseline Commit**: `3efbb20794f47de30ae341615bfe53e086c36553` (`CP17-WIN`)
- **Branch**: `arena/01a0dca0-drillai` (`main` untouched at `bfa066b28e0071880cb9191a4f1d47fdaa143e04`)

---

## 1. CP18-A: Audit of Existing Recovery Coverage (SQLite vs PostgreSQL)

| Recovery Mechanism | Target Environment | Implementation & Scope | Classification |
| :--- | :--- | :--- | :--- |
| **SQLite File Copy & Online Backup (`sqlite3.Connection.backup`)** | Local developer workstations & fast single-node test suites (`sqlite+aiosqlite:///...`) | Tested in `test_cp16_08_backup_restore_and_migration_recovery_drill` and `test_cp18_02_sqlite_online_backup_api_vs_postgres_dr_distinction`. Copies local SQLite file / pages and re-runs `alembic upgrade head`. | **PASS** (Local/Dev Only — `production_dr_capable: false`) |
| **PostgreSQL 16 Logical Backup (`pg_dump --format=custom`) & Restore (`pg_restore --exit-on-error`)** | Production & staging multi-service deployments (`postgresql+asyncpg://...`) | Implemented and verified in `scripts/verify_postgres_dr_drill.py` and `backend/tests/db/test_cp18_postgres_backup_restore.py` (`@pytest.mark.postgres`). Executes real PostgreSQL 16 `pg_dump` custom archive creation, SHA-256 verification, `DROP DATABASE ... WITH (FORCE)`, `pg_restore` into a clean target database, `alembic upgrade head && alembic check`, 20-table row-count parity, foreign-key relationship & multi-tenant isolation checks, and corrupt-archive rejection. | **PASS** (Production Logical Backup/Restore Certified) |
| **Cloud-Managed Physical PITR / Continuous WAL Archiving (AWS RDS / CloudSQL / Barman)** | External cloud infrastructure | Requires external cloud object storage / managed database infrastructure outside repository CI. | **BLOCKED** (Requires external cloud/infrastructure deployment) |

---

## 2. CP18-B: PostgreSQL 16 Backup, Restore & Migration Drill Evidence

Executed via `backend/.venv/bin/python scripts/verify_postgres_dr_drill.py` and `DRILLAI_TEST_POSTGRES=1 .venv/bin/python -m pytest tests/db/test_cp18_postgres_backup_restore.py -o addopts=""`:

### 2.1 Measured Drill Metrics
- **Database Engine**: PostgreSQL 16.2 (`pg_dump (PostgreSQL) 16.2`, `pg_restore (PostgreSQL) 16.2`)
- **Alembic Revision Verified**: `c9f1a48b2e31` (`alembic_zero_drift_verified: true`, `"No new upgrade operations detected."` both pre-backup on `drillai_dr_source` and post-restore on `drillai_dr_restored`)
- **Backup Format**: `pg_dump_custom` (`--format=custom --no-owner --no-privileges`)
- **Backup Archive Size**: `445,164` bytes
- **Backup SHA-256 Checksum**: `f2eeef075a277048412b3b4c81b4414f4b4c48c662a499f929c2cd516008d491`
- **Backup Duration**: `92.15 ms`
- **Restore Duration**: `1,100.61 ms`

### 2.2 Pre-Backup vs Post-Restore Row-Count Parity (20 Tables Across 2 Tenants)

| Table | Pre-Backup Count (`drillai_dr_source`) | Post-Restore Count (`drillai_dr_restored`) | Parity |
| :--- | :---: | :---: | :---: |
| `organizations` | 2 | 2 | **PASS** |
| `projects` | 2 | 2 | **PASS** |
| `wells` | 2 | 2 | **PASS** |
| `wellbores` | 2 | 2 | **PASS** |
| `well_sections` | 2 | 2 | **PASS** |
| `documents` | 1 | 1 | **PASS** |
| `document_chunks` | 1 | 1 | **PASS** |
| `extracted_records` | 1 | 1 | **PASS** |
| `evidence_links` | 1 | 1 | **PASS** |
| `twin_aspects` | 1 | 1 | **PASS** |
| `twin_snapshots` | 1 | 1 | **PASS** |
| `workflows` | 1 | 1 | **PASS** |
| `workflow_versions` | 1 | 1 | **PASS** |
| `workflow_runs` | 1 | 1 | **PASS** |
| `approval_requests` | 1 | 1 | **PASS** |
| `connectors` | 2 | 2 | **PASS** |
| `time_series` | 1 | 1 | **PASS** |
| `time_series_points` | 5 | 5 | **PASS** |
| `connector_runs` | 1 | 1 | **PASS** |
| `connector_egress_policies` | 1 | 1 | **PASS** |

### 2.3 Post-Restore Application, Tenant Boundary & Negative Corruption Checks
1. **Cold-Start Defect Found & Fixed During CP18 Drill**:
   - **Finding**: Calling `GET /api/v1/connectors` on a newly started FastAPI process against a restored database (before `get_egress_policy()` had been called in that process) raised `AttributeError: 'NoneType' object has no attribute 'get'` at `backend/src/drillai/telemetry/connectors.py:3404` because `connector_out()` dereferenced `_active_egress_policy_snapshot` directly while it was `None`.
   - **Fix**: Updated `connector_out()` in `backend/src/drillai/telemetry/connectors.py` to resolve `(_active_egress_policy_snapshot or get_egress_policy())`, and added regression test `test_cp18_03_connector_out_cold_start_when_egress_snapshot_is_none`.
2. **Post-Restore Multi-Tenant Isolation & FastAPI Verification**:
   - `/api/v1/health/ready` returned `200 OK` with `checks.database.dialect == "postgresql"`.
   - Tenant A (`org-dr-alpha`) listed only `ALPHA-DR-WELL-01` and its WITSML connector (`fencing_token == 4`), restored `TwinSnapshot` (`dr-pre-backup-snapshot`), and received `404` when requesting Tenant B's `BETA-DR-WELL-01`.
   - Tenant B (`org-dr-beta`) listed only `BETA-DR-WELL-01` and its ETP 1.2 Binary connector, and received `404` when requesting Tenant A's `ALPHA-DR-WELL-01`.
   - System egress policy revision `2` (`witsml.alpha-rig.example.com`, `10.42.0.0/16`) was preserved and returned via `GET /api/v1/connectors/egress-policy`.
3. **Corrupt Backup Failure Detection**:
   - Corrupted the `PGDMP` magic header and truncated a copy of the backup archive (`drillai_dr_corrupt.dump`), then ran `pg_restore --exit-on-error`.
   - Verified deterministic failure (`corrupt_backup_failure_detected: true`, `corrupt_restore_exit_code: 1`).

---

## 3. CP18 Operational Readiness Classification Table

| Operational Area | Status | Evidence |
| :--- | :--- | :--- |
| **PostgreSQL 16 Logical Backup (`pg_dump`) & Restore (`pg_restore`)** | **PASS** | `scripts/verify_postgres_dr_drill.py` & `test_cp18_01_postgres_logical_backup_restore_and_migration_recovery_drill` |
| **Post-Restore Alembic Revision & Zero Schema Drift (`alembic check`)** | **PASS** | Verified on both `drillai_dr_source` and `drillai_dr_restored` (`c9f1a48b2e31`) |
| **Corrupted Backup Rejection (`pg_restore --exit-on-error`)** | **PASS** | Verified deterministic non-zero exit code (`1`) on corrupted archive |
| **Operational Runbooks (`docs/CONNECTORS_AND_OPERATIONS.md` Section 6)** | **PASS** | Copy-pasteable `pg_dump`, `pg_restore`, `alembic`, health check, worker supervision, secret rotation, and egress policy runbooks |
| **Cloud Infrastructure PITR / Multi-Region Failover** | **BLOCKED** | Requires external cloud provider / managed PostgreSQL infrastructure |
