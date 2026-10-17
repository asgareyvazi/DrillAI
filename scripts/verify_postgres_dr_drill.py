#!/usr/bin/env python3
"""Production PostgreSQL Logical Backup, Restore & Migration Recovery Drill (CP18).

Executes an isolated, repeatable end-to-end PostgreSQL disaster-recovery drill:
1. Starts an isolated PostgreSQL 16 instance (via embedded `pgserver` or `--postgres-uri`).
2. Creates a clean source database (`drillai_dr_source`) and runs `alembic upgrade head` + `alembic check`.
3. Seeds multi-tenant operational data across two organizations (`org-dr-alpha`, `org-dr-beta`):
   projects, wells, wellbores, well sections, documents, chunks, extracted records, evidence links,
   digital twin aspect revisions & snapshots, workflows, runs, approval requests, telemetry connectors,
   time-series channels, time-series points, connector runs, and durable connector egress policy.
4. Captures pre-backup row counts, entity relationships, and Alembic revision state.
5. Executes `pg_dump --format=custom --no-owner --no-privileges` to produce a custom-format archive,
   recording archive byte size, SHA-256 checksum, and backup duration (ms).
6. Drops `drillai_dr_source` (simulating catastrophic primary DB loss) and creates an empty target
   database (`drillai_dr_restored`).
7. Executes `pg_restore --no-owner --no-privileges --exit-on-error` into `drillai_dr_restored`,
   recording restore duration (ms).
8. Runs `alembic upgrade head` and `alembic check` against `drillai_dr_restored` to certify zero
   post-restore schema/migration drift.
9. Verifies 100% row-count parity, foreign-key relationship integrity, strict cross-tenant isolation,
   and live FastAPI application reads against `drillai_dr_restored`.
10. Exercises negative failure detection by attempting `pg_restore --exit-on-error` with a corrupted
    archive and verifying deterministic non-zero failure detection.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
SRC_DIR = BACKEND_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _resolve_pg_binaries() -> tuple[str, str]:
    """Resolve pg_dump and pg_restore from pgserver bundle or system PATH."""
    try:
        from pgserver._commands import POSTGRES_BIN_PATH

        pg_dump = POSTGRES_BIN_PATH / ("pg_dump.exe" if sys.platform == "win32" else "pg_dump")
        pg_restore = POSTGRES_BIN_PATH / ("pg_restore.exe" if sys.platform == "win32" else "pg_restore")
        if pg_dump.exists() and pg_restore.exists():
            return str(pg_dump), str(pg_restore)
    except ImportError:
        pass

    sys_dump = shutil.which("pg_dump")
    sys_restore = shutil.which("pg_restore")
    if sys_dump and sys_restore:
        return sys_dump, sys_restore
    raise RuntimeError("Neither pgserver POSTGRES_BIN_PATH nor system pg_dump/pg_restore is available.")


def _alembic_config(async_db_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", async_db_url)
    return cfg


def _replace_db_name(uri: str, new_db_name: str) -> str:
    """Replace the database name in a libpq or SQLAlchemy PostgreSQL URI while preserving query params."""
    if "?" in uri:
        base, query = uri.split("?", 1)
        prefix = base.rsplit("/", 1)[0]
        return f"{prefix}/{new_db_name}?{query}"
    prefix = uri.rsplit("/", 1)[0]
    return f"{prefix}/{new_db_name}"


async def _recreate_database(admin_async_url: str, db_name: str) -> None:
    eng = create_async_engine(admin_async_url, isolation_level="AUTOCOMMIT")
    try:
        async with eng.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        await eng.dispose()


async def _drop_database(admin_async_url: str, db_name: str) -> None:
    eng = create_async_engine(admin_async_url, isolation_level="AUTOCOMMIT")
    try:
        async with eng.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
    finally:
        await eng.dispose()


async def _collect_table_counts_and_revision(async_db_url: str) -> tuple[dict[str, int], str]:
    from drillai.db.models import (
        ApprovalRequest,
        Connector,
        ConnectorEgressPolicy,
        ConnectorRun,
        Document,
        DocumentChunk,
        EvidenceLink,
        ExtractedRecord,
        Organization,
        Project,
        TimeSeries,
        TimeSeriesPoint,
        TwinAspect,
        TwinSnapshot,
        Well,
        Wellbore,
        WellSection,
        Workflow,
        WorkflowRun,
        WorkflowVersion,
    )

    models = [
        ("organizations", Organization),
        ("projects", Project),
        ("wells", Well),
        ("wellbores", Wellbore),
        ("well_sections", WellSection),
        ("documents", Document),
        ("document_chunks", DocumentChunk),
        ("extracted_records", ExtractedRecord),
        ("evidence_links", EvidenceLink),
        ("twin_aspects", TwinAspect),
        ("twin_snapshots", TwinSnapshot),
        ("workflows", Workflow),
        ("workflow_versions", WorkflowVersion),
        ("workflow_runs", WorkflowRun),
        ("approval_requests", ApprovalRequest),
        ("connectors", Connector),
        ("time_series", TimeSeries),
        ("time_series_points", TimeSeriesPoint),
        ("connector_runs", ConnectorRun),
        ("connector_egress_policies", ConnectorEgressPolicy),
    ]
    eng = create_async_engine(async_db_url, pool_size=2, max_overflow=0)
    try:
        counts: dict[str, int] = {}
        async with eng.connect() as conn:
            for table_name, model in models:
                cnt = (await conn.execute(select(func.count()).select_from(model))).scalar_one()
                counts[table_name] = int(cnt)
            rev = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
        return counts, str(rev)
    finally:
        await eng.dispose()


async def _seed_multi_tenant_dr_dataset(async_db_url: str) -> dict[str, Any]:
    from drillai.db.models import (
        ApprovalRequest,
        Connector,
        ConnectorEgressPolicy,
        ConnectorRun,
        Document,
        DocumentChunk,
        EvidenceLink,
        ExtractedRecord,
        Organization,
        Project,
        TimeSeries,
        TimeSeriesPoint,
        Well,
        Wellbore,
        WellSection,
        Workflow,
        WorkflowRun,
        WorkflowVersion,
    )
    from drillai.db.session import Database
    from drillai.twin.service import AspectRevision, TwinService

    db = Database(async_db_url)
    now = dt.datetime(2026, 10, 17, 12, 0, 0, tzinfo=dt.UTC)
    try:
        async with db.session_factory() as session:
            org_a = Organization(slug="org-dr-alpha", name="DR Alpha Operating Co", kind="operator")
            org_b = Organization(slug="org-dr-beta", name="DR Beta Drilling Ltd", kind="operator")
            session.add_all([org_a, org_b])
            await session.flush()

            prj_a = Project(org_id=org_a.id, name="Alpha North Sea Asset", code="ALPHA-NS")
            prj_b = Project(org_id=org_b.id, name="Beta Permian Asset", code="BETA-PB")
            session.add_all([prj_a, prj_b])
            await session.flush()

            well_a = Well(
                org_id=org_a.id,
                project_id=prj_a.id,
                name="ALPHA-DR-WELL-01",
                uwi="NO-DR-ALPHA-001",
                well_type="development_producer",
                status="drilling",
                operator="DR Alpha Operating Co",
            )
            well_b = Well(
                org_id=org_b.id,
                project_id=prj_b.id,
                name="BETA-DR-WELL-01",
                uwi="US-DR-BETA-001",
                well_type="exploration",
                status="drilling",
                operator="DR Beta Drilling Ltd",
            )
            session.add_all([well_a, well_b])
            await session.flush()

            wb_a = Wellbore(
                org_id=org_a.id,
                well_id=well_a.id,
                name="Alpha Mainbore",
                purpose="original",
                sequence=1,
                is_active=True,
                planned_td_md_si=3500.0,
                planned_td_tvd_si=3200.0,
            )
            wb_b = Wellbore(
                org_id=org_b.id,
                well_id=well_b.id,
                name="Beta Mainbore",
                purpose="original",
                sequence=1,
                is_active=True,
                planned_td_md_si=4100.0,
                planned_td_tvd_si=3900.0,
            )
            session.add_all([wb_a, wb_b])
            await session.flush()

            sec_a = WellSection(
                org_id=org_a.id,
                wellbore_id=wb_a.id,
                sequence=1,
                name='12-1/4" Intermediate',
                kind="intermediate",
                hole_diameter_si=0.31115,
                planned_top_md_si=800.0,
                planned_bottom_md_si=2400.0,
            )
            sec_b = WellSection(
                org_id=org_b.id,
                wellbore_id=wb_b.id,
                sequence=1,
                name='8-1/2" Production',
                kind="production",
                hole_diameter_si=0.2159,
                planned_top_md_si=2400.0,
                planned_bottom_md_si=4100.0,
            )
            session.add_all([sec_a, sec_b])
            await session.flush()

            doc_a = Document(
                org_id=org_a.id,
                project_id=prj_a.id,
                well_id=well_a.id,
                wellbore_id=wb_a.id,
                section_id=sec_a.id,
                doc_type="ddr",
                title="Alpha Daily Drilling Report #1",
                status="processed",
                checksum="a" * 64,
            )
            session.add(doc_a)
            await session.flush()

            chunk_a = DocumentChunk(
                org_id=org_a.id,
                document_id=doc_a.id,
                well_id=well_a.id,
                wellbore_id=wb_a.id,
                section_id=sec_a.id,
                doc_type="ddr",
                chunk_index=0,
                text="Drilling ahead 12-1/4 in section from 1800 m to 2150 m with 1260 kg/m3 OBM.",
                token_estimate=16,
                embedding=[0.25, 0.5, 0.75, 1.0],
                embedding_model="deterministic-hash-v1",
                embedding_dim=4,
            )
            rec_a = ExtractedRecord(
                org_id=org_a.id,
                document_id=doc_a.id,
                project_id=prj_a.id,
                well_id=well_a.id,
                wellbore_id=wb_a.id,
                section_id=sec_a.id,
                record_type="mud_properties",
                payload_schema_key="mud_properties.v1",
                payload={"mud_weight_si": 1260.0, "pv_si": 0.022, "yp_si": 9.5},
                confidence=0.98,
                validation_state="validated",
            )
            session.add_all([chunk_a, rec_a])
            await session.flush()

            ev_a = EvidenceLink(
                org_id=org_a.id,
                well_id=well_a.id,
                subject_kind="extracted_record",
                subject_id=rec_a.id,
                evidence_kind="document_excerpt",
                evidence_id=doc_a.id,
                document_id=doc_a.id,
                excerpt="1260 kg/m3 OBM",
                confidence=0.98,
            )
            session.add(ev_a)

            # Digital Twin state + snapshot for Tenant A
            twin_a = TwinService(session, org_id=org_a.id)
            await twin_a.write_aspect(
                well_id=well_a.id,
                revision=AspectRevision(
                    aspect="identity",
                    state_kind="actual",
                    payload={"mud_weight_si": 1260.0, "ecd_at_td_si": 1315.0},
                ),
            )
            snap_a = await twin_a.snapshot(well_id=well_a.id, label="dr-pre-backup-snapshot")

            # Workflow + Run + Pending Approval for Tenant A
            wf_a = Workflow(
                org_id=org_a.id,
                project_id=prj_a.id,
                key="alpha-dr-workflow",
                name="Alpha DR Verification Workflow",
                status="published",
                current_version=1,
            )
            session.add(wf_a)
            await session.flush()

            wfv_a = WorkflowVersion(
                org_id=org_a.id,
                workflow_id=wf_a.id,
                version=1,
                graph={"nodes": [{"id": "n1", "type": "human.approval", "config": {}}], "edges": []},
                graph_hash="b" * 64,
            )
            session.add(wfv_a)
            await session.flush()

            wfr_a = WorkflowRun(
                org_id=org_a.id,
                workflow_id=wf_a.id,
                workflow_version_id=wfv_a.id,
                workflow_key=wf_a.key,
                project_id=prj_a.id,
                well_id=well_a.id,
                wellbore_id=wb_a.id,
                section_id=sec_a.id,
                status="waiting_approval",
                initiated_by="usr_alpha_eng",
            )
            session.add(wfr_a)
            await session.flush()

            appr_a = ApprovalRequest(
                org_id=org_a.id,
                kind="workflow_node",
                subject_kind="workflow_run",
                subject_id=wfr_a.id,
                run_id=wfr_a.id,
                node_id="n1",
                project_id=prj_a.id,
                well_id=well_a.id,
                title="Approve casing running speed profile",
                action_level="L3",
                status="pending",
                requested_by="usr_alpha_eng",
                requested_at=now,
            )
            session.add(appr_a)
            await session.flush()
            wfr_a.pending_approval_id = appr_a.id

            # Telemetry Connectors + TimeSeries + TimeSeriesPoints + ConnectorRun + Egress Policy
            cnc_a = Connector(
                org_id=org_a.id,
                project_id=prj_a.id,
                well_id=well_a.id,
                key="alpha-witsml-dr",
                name="Alpha WITSML Store",
                provider="witsml",
                protocol_profile="witsml.1.4.1.1.soap_http",
                endpoint_url="https://witsml.alpha-rig.example.com/store",
                status="running",
                desired_state="running",
                config_version=2,
                fencing_token=4,
                config={"auth_mode": "basic", "poll_interval_seconds": 5.0},
            )
            cnc_b = Connector(
                org_id=org_b.id,
                project_id=prj_b.id,
                well_id=well_b.id,
                key="beta-etp-dr",
                name="Beta ETP 1.2 Binary Stream",
                provider="etp",
                protocol_profile="etp.1.2.binary_ws",
                endpoint_url="wss://etp.beta-rig.example.com/etp",
                status="stopped",
                desired_state="stopped",
                config_version=1,
                fencing_token=1,
                config={"auth_mode": "bearer", "poll_interval_seconds": 5.0},
            )
            session.add_all([cnc_a, cnc_b])
            await session.flush()

            ts_a = TimeSeries(
                org_id=org_a.id,
                well_id=well_a.id,
                wellbore_id=wb_a.id,
                scope_token=f"well:{well_a.id}",
                channel_key="spp",
                name="Standpipe Pressure",
                dimension="pressure",
                unit="Pa",
                source="witsml.1.4.1.1.soap_http",
                point_count=5,
            )
            session.add(ts_a)
            await session.flush()

            pts_a = [
                TimeSeriesPoint(
                    series_id=ts_a.id,
                    ts=now + dt.timedelta(seconds=idx),
                    value=18_500_000.0 + idx * 10_000.0,
                    quality="good",
                    sequence=idx,
                    depth_md_si=2100.0 + idx,
                    dedup_key=f"dr-spp-{idx}",
                )
                for idx in range(1, 6)
            ]
            session.add_all(pts_a)

            crun_a = ConnectorRun(
                org_id=org_a.id,
                connector_id=cnc_a.id,
                well_id=well_a.id,
                worker_id="wrk-dr-01",
                fencing_token=4,
                config_version=2,
                run_kind="poll",
                status="succeeded",
                started_at=now,
                finished_at=now + dt.timedelta(seconds=1),
                duration_ms=1000,
                frames_received=5,
                points_accepted=5,
                details={"manifest": {"data_outcome": "all_valid_accepted", "persisted": True}},
            )
            session.add(crun_a)

            from drillai.telemetry.connectors import SYSTEM_EGRESS_POLICY_KEY

            pol = ConnectorEgressPolicy(
                policy_key=SYSTEM_EGRESS_POLICY_KEY,
                allowed_hosts=["witsml.alpha-rig.example.com", "etp.beta-rig.example.com"],
                allowed_private_cidrs=["10.42.0.0/16"],
                revision=2,
                updated_by_id="admin_dr_drill",
                reason="CP18 DR drill baseline policy",
            )
            session.add(pol)
            await session.commit()

            return {
                "org_a_id": org_a.id,
                "org_b_id": org_b.id,
                "well_a_id": well_a.id,
                "well_b_id": well_b.id,
                "connector_a_id": cnc_a.id,
                "connector_b_id": cnc_b.id,
                "workflow_a_id": wf_a.id,
                "workflow_run_a_id": wfr_a.id,
                "approval_a_id": appr_a.id,
                "snapshot_a_id": snap_a.id,
                "document_a_id": doc_a.id,
            }
    finally:
        await db.dispose()


async def _verify_restored_relationships_and_tenant_isolation(
    restored_async_url: str, seeded_ids: dict[str, Any]
) -> dict[str, Any]:
    from drillai.api.app import create_app
    from drillai.core.config import Settings, reset_settings_cache
    from drillai.telemetry.connectors import reset_egress_policy
    from drillai.twin.service import TwinService

    saved_env = {
        k: os.environ.get(k)
        for k in (
            "DRILLAI_ENVIRONMENT",
            "DRILLAI_AUTH_ENABLED",
            "DRILLAI_DATABASE_URL",
            "DRILLAI_DEV_ORG_SLUG",
        )
    }
    os.environ["DRILLAI_ENVIRONMENT"] = "development"
    os.environ["DRILLAI_AUTH_ENABLED"] = "false"
    os.environ["DRILLAI_DATABASE_URL"] = restored_async_url
    reset_settings_cache()
    reset_egress_policy()

    app = create_app(Settings(database_url=restored_async_url, auth_enabled=False))
    try:
        async with app.state.database.session() as session:
            twin_a = TwinService(session, org_id=seeded_ids["org_a_id"])
            snaps_a = await twin_a.list_snapshots(well_id=seeded_ids["well_a_id"])
            assert len(snaps_a) == 1
            assert snaps_a[0].id == seeded_ids["snapshot_a_id"]
            assert snaps_a[0].label == "dr-pre-backup-snapshot"

            # Cross-tenant TwinService read from org_b for well_a must return empty/blocked
            twin_b = TwinService(session, org_id=seeded_ids["org_b_id"])
            snaps_cross = await twin_b.list_snapshots(well_id=seeded_ids["well_a_id"])
            assert snaps_cross == []

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://dr-verify.local") as client:
            headers_sup = {"X-Dev-Roles": "drilling_supervisor,admin"}

            # Tenant A context (dev_org_slug = org-dr-alpha)
            os.environ["DRILLAI_DEV_ORG_SLUG"] = "org-dr-alpha"
            reset_settings_cache()

            # Health & readiness against restored PostgreSQL DB
            r_ready = await client.get("/api/v1/health/ready", headers=headers_sup)
            assert r_ready.status_code == 200
            assert r_ready.json()["status"] == "ok"
            assert r_ready.json()["checks"]["database"]["dialect"] == "postgresql"

            # Tenant A sees only ALPHA-DR-WELL-01 and gets 404 on BETA-DR-WELL-01
            wells_a = (await client.get("/api/v1/wells", headers=headers_sup)).json()
            assert [w["id"] for w in wells_a["items"]] == [seeded_ids["well_a_id"]]
            cross_well_b = await client.get(f"/api/v1/wells/{seeded_ids['well_b_id']}", headers=headers_sup)
            assert cross_well_b.status_code == 404

            # Connectors & Egress Policy restored for Tenant A
            cncs_a = (await client.get("/api/v1/connectors", headers=headers_sup)).json()
            assert [c["id"] for c in cncs_a["items"]] == [seeded_ids["connector_a_id"]]
            assert cncs_a["items"][0]["worker"]["fencing_token"] == 4

            pol = (await client.get("/api/v1/connectors/egress-policy", headers=headers_sup)).json()
            assert pol["revision"] == 2
            assert "witsml.alpha-rig.example.com" in pol["allowed_hosts"]
            assert "10.42.0.0/16" in pol["allowed_private_cidrs"]

            # Tenant B context (dev_org_slug = org-dr-beta)
            os.environ["DRILLAI_DEV_ORG_SLUG"] = "org-dr-beta"
            reset_settings_cache()

            # Tenant B sees only BETA-DR-WELL-01 and gets 404 on ALPHA-DR-WELL-01
            wells_b = (await client.get("/api/v1/wells", headers=headers_sup)).json()
            assert [w["id"] for w in wells_b["items"]] == [seeded_ids["well_b_id"]]
            cross_well_a = await client.get(f"/api/v1/wells/{seeded_ids['well_a_id']}", headers=headers_sup)
            assert cross_well_a.status_code == 404
            cncs_b = (await client.get("/api/v1/connectors", headers=headers_sup)).json()
            assert [c["id"] for c in cncs_b["items"]] == [seeded_ids["connector_b_id"]]

        return {
            "health_ready_status": "ok",
            "database_dialect": "postgresql",
            "tenant_a_isolated": True,
            "tenant_b_isolated": True,
            "twin_snapshot_restored": True,
            "egress_policy_revision_restored": 2,
        }
    finally:
        await app.state.database.dispose()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        reset_settings_cache()
        reset_egress_policy()


async def run_postgres_dr_drill(external_postgres_uri: str | None = None) -> dict[str, Any]:
    pg_dump_bin, pg_restore_bin = _resolve_pg_binaries()
    server = None
    pg_temp_dir = None

    try:
        if external_postgres_uri:
            base_sync_uri = external_postgres_uri.replace("postgresql+asyncpg://", "postgresql://")
        else:
            import pgserver

            pg_temp_dir = tempfile.mkdtemp(prefix="drillai_cp18_pg_")
            server = pgserver.get_server(pg_temp_dir)
            base_sync_uri = server.get_uri()

        admin_async_url = base_sync_uri.replace("postgresql://", "postgresql+asyncpg://")
        source_sync_uri = _replace_db_name(base_sync_uri, "drillai_dr_source")
        source_async_url = source_sync_uri.replace("postgresql://", "postgresql+asyncpg://")
        restored_sync_uri = _replace_db_name(base_sync_uri, "drillai_dr_restored")
        restored_async_url = restored_sync_uri.replace("postgresql://", "postgresql+asyncpg://")
        corrupt_sync_uri = _replace_db_name(base_sync_uri, "drillai_dr_corrupt_check")

        with tempfile.TemporaryDirectory(prefix="drillai_cp18_artifacts_", ignore_cleanup_errors=True) as art_dir:
            art_path = Path(art_dir)
            backup_file = art_path / "drillai_dr_backup.dump"
            corrupt_file = art_path / "drillai_dr_corrupt.dump"

            # 1. Create source DB and apply full Alembic migration chain
            await _recreate_database(admin_async_url, "drillai_dr_source")
            src_cfg = _alembic_config(source_async_url)
            await asyncio.to_thread(command.upgrade, src_cfg, "head")
            await asyncio.to_thread(command.check, src_cfg)

            # 2. Seed multi-tenant dataset and record pre-backup counts
            seeded_ids = await _seed_multi_tenant_dr_dataset(source_async_url)
            pre_counts, pre_revision = await _collect_table_counts_and_revision(source_async_url)

            # 3. Logical backup via pg_dump --format=custom
            t0_dump = time.perf_counter()
            dump_proc = subprocess.run(
                [
                    pg_dump_bin,
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    f"--file={backup_file}",
                    source_sync_uri,
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            backup_duration_ms = round((time.perf_counter() - t0_dump) * 1000.0, 2)
            if dump_proc.returncode != 0:
                raise RuntimeError(f"pg_dump failed ({dump_proc.returncode}): {dump_proc.stderr}")

            backup_bytes = backup_file.stat().st_size
            assert backup_bytes > 0
            backup_sha256 = hashlib.sha256(backup_file.read_bytes()).hexdigest()

            # 4. Simulate catastrophic loss of source DB & create fresh target DB
            await _drop_database(admin_async_url, "drillai_dr_source")
            await _recreate_database(admin_async_url, "drillai_dr_restored")

            # 5. Restore backup into fresh target DB via pg_restore
            t0_restore = time.perf_counter()
            restore_proc = subprocess.run(
                [
                    pg_restore_bin,
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                    f"--dbname={restored_sync_uri}",
                    str(backup_file),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            restore_duration_ms = round((time.perf_counter() - t0_restore) * 1000.0, 2)
            if restore_proc.returncode != 0:
                raise RuntimeError(f"pg_restore failed ({restore_proc.returncode}): {restore_proc.stderr}")

            # 6. Post-restore Alembic upgrade head & zero-drift check
            dst_cfg = _alembic_config(restored_async_url)
            await asyncio.to_thread(command.upgrade, dst_cfg, "head")
            await asyncio.to_thread(command.check, dst_cfg)

            # 7. Verify row counts, relationships, tenant boundaries & live API reads
            post_counts, post_revision = await _collect_table_counts_and_revision(restored_async_url)
            assert post_counts == pre_counts, f"Row count mismatch: {post_counts} != {pre_counts}"
            assert post_revision == pre_revision

            app_verification = await _verify_restored_relationships_and_tenant_isolation(
                restored_async_url, seeded_ids
            )

            # 8. Negative drill: corrupt backup archive must fail pg_restore deterministically
            await _recreate_database(admin_async_url, "drillai_dr_corrupt_check")
            raw_bytes = bytearray(backup_file.read_bytes())
            # Corrupt magic header ("PGDMP") and truncate payload
            raw_bytes[:5] = b"BAD!!"
            corrupt_file.write_bytes(bytes(raw_bytes[: max(64, len(raw_bytes) // 4)]))
            corrupt_proc = subprocess.run(
                [
                    pg_restore_bin,
                    "--no-owner",
                    "--no-privileges",
                    "--exit-on-error",
                    f"--dbname={corrupt_sync_uri}",
                    str(corrupt_file),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            corrupt_detected = corrupt_proc.returncode != 0
            assert corrupt_detected is True, "pg_restore unexpectedly succeeded on a corrupted archive"

            # Cleanup drill databases
            await _drop_database(admin_async_url, "drillai_dr_restored")
            await _drop_database(admin_async_url, "drillai_dr_corrupt_check")

            return {
                "drill_status": "PASSED",
                "database_engine": "PostgreSQL 16 (pg_dump / pg_restore custom format)",
                "alembic_revision": post_revision,
                "alembic_zero_drift_verified": True,
                "backup_artifact": {
                    "format": "pg_dump_custom",
                    "size_bytes": backup_bytes,
                    "sha256": backup_sha256,
                    "backup_duration_ms": backup_duration_ms,
                    "restore_duration_ms": restore_duration_ms,
                },
                "pre_backup_counts": pre_counts,
                "post_restore_counts": post_counts,
                "counts_match_exactly": post_counts == pre_counts,
                "application_verification": app_verification,
                "corrupt_backup_failure_detected": corrupt_detected,
                "corrupt_restore_exit_code": corrupt_proc.returncode,
            }
    finally:
        if server is not None:
            server.cleanup()
        if pg_temp_dir is not None:
            shutil.rmtree(pg_temp_dir, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres-uri",
        default=os.environ.get("DRILLAI_POSTGRES_DRILL_URL"),
        help="Optional external PostgreSQL URI (defaults to isolated embedded pgserver instance)",
    )
    args = parser.parse_args(argv)
    report = asyncio.run(run_postgres_dr_drill(args.postgres_uri))
    print(json.dumps(report, indent=2))
    print("POSTGRES_DR_DRILL_OK: backup, restore, alembic check, tenant isolation, and corruption detection verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
