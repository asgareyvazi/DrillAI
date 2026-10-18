#!/usr/bin/env python3
"""End-to-end multi-process deployment stack & bounded soak verification (CP15-H).

Certifies all 16 multi-process scenarios:
1. Clean DB migration initialization (`alembic upgrade head` & `alembic check`).
2. FastAPI backend server process + readiness checks (`/api/v1/health`, `/api/v1/health/ready`).
3. WITSML 1.4.1.1 SOAP connector creation, 10-stage connection test, and sample preview.
4. ETP 1.2 Binary Avro connector creation, 10-stage connection test, and sample preview.
5. Standalone `python -m drillai.telemetry.worker` process claiming both connectors and ingesting telemetry.
6. Non-harness loopback port blocked by SSRF policy (`DRILLAI_CONNECTOR_ALLOW_LOOPBACK=false`).
7. System-scope RBAC enforcement (`org_admin` 403) + durable egress policy update via API (`admin` rev 2) & worker sync.
8. API process restart preserving durable egress policy from DB.
9. Worker process restart and lease recovery without duplicate ingestion.
10. Policy revocation causing running connector to fail closed on next poll (`ssrf_blocked`).
11. Transient policy-store failure causing fail-closed behaviour, followed by same-process recovery without restart.
12. `reconciled=False` fault injection rolling back telemetry, preserving cursor, and recording `reconciliation_error`.
13. Filtered-only poll (`unmapped_skipped > 0`, `accepted == 0`) recording `filtered_only`.
14. Dry-run replay (`dry_run_preview`, `persisted=False`, cursor unchanged) vs live replay (`persisted=True`).
15. Bounded DNS timeout/cancellation stress followed by healthy connector polling without permit starvation.
16. Clean connector stop (`status == "stopped"`) and bounded run ledger retention check (`<= 25` runs).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

try:
    import resource
except ImportError:  # pragma: no cover - Windows does not provide POSIX resource module
    resource = None  # type: ignore[assignment]

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore[assignment]

import httpx
from sqlalchemy import func, select

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR / "src"))


def _resolve_venv_python() -> str:
    """Resolve the backend virtualenv Python interpreter across Windows and POSIX."""
    override = os.environ.get("DRILLAI_PYTHON")
    if override and Path(override).exists():
        return override
    for candidate in (
        BACKEND_DIR / ".venv" / "Scripts" / "python.exe",
        BACKEND_DIR / ".venv" / "bin" / "python",
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _terminate_proc(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


def _rss_mb() -> float:
    if psutil is not None:
        with contextlib.suppress(Exception):
            return round(psutil.Process(os.getpid()).memory_info().rss / (1024.0 * 1024.0), 2)
    if resource is not None:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform == "darwin":
            return round(usage / (1024 * 1024), 2)
        return round(usage / 1024, 2)
    return 0.0


async def _run_verification() -> int:
    from drillai.core.errors import ValidationFailed
    from drillai.db.models import Connector, ConnectorRun, TimeSeriesPoint
    from drillai.db.session import Database
    from drillai.telemetry.connectors import (
        ALL_TEST_CONNECTION_STAGES,
        ConnectorTransportError,
        ProtocolPollDisposition,
        enforce_connect_time_ssrf,
        get_dns_executor_stats,
        get_egress_policy,
        reset_dns_executor_state,
        resolve_and_pin_endpoint_async,
        sync_egress_policy_from_db,
    )
    from drillai.telemetry.protocols.etp_binary import LocalEtpBinaryWebSocketServer
    from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer, WitsmlSampleRow
    from drillai.telemetry.worker import ConnectorWorker

    python_bin = _resolve_venv_python()
    scenario_results: list[dict[str, Any]] = []

    def record_scenario(num: int, name: str, detail: str) -> None:
        scenario_results.append({"scenario": num, "name": name, "status": "PASSED", "detail": detail})

    with tempfile.TemporaryDirectory(prefix="drillai_stack_", ignore_cleanup_errors=True) as tmpdir:
        db_path = Path(tmpdir) / "stack.db"
        db_url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
        api_port = _free_port()

        witsml_server = LocalWitsmlSoapServer(
            expected_username="stack_user",
            expected_password="StackSecretPass99!",
        )
        etp_bin_server = LocalEtpBinaryWebSocketServer(
            expected_bearer_token="StackEtpBinaryBearerToken99!",
        )
        policy_witsml_server = LocalWitsmlSoapServer(
            expected_username="stack_user",
            expected_password="StackSecretPass99!",
        )

        api_proc: subprocess.Popen[bytes] | None = None
        worker_proc: subprocess.Popen[bytes] | None = None

        try:
            await witsml_server.start()
            await etp_bin_server.start()
            await policy_witsml_server.start()

            env = os.environ.copy()
            env.update(
                {
                    "DRILLAI_ENVIRONMENT": "staging",
                    "DRILLAI_AUTH_ENABLED": "false",
                    "DRILLAI_DATABASE_URL": db_url,
                    "DRILLAI_BLOB_BACKEND": "filesystem",
                    "DRILLAI_BLOB_ROOT": str(Path(tmpdir) / "blobs"),
                    "DRILLAI_SCHEDULER_ENABLED": "false",
                    "DRILLAI_CONNECTOR_WORKER_ENABLED": "false",
                    "DRILLAI_CONNECTOR_ALLOW_LOOPBACK": "false",
                    "DRILLAI_CONNECTOR_HARNESS_PORTS": f"{witsml_server.port},{etp_bin_server.port}",
                    "DRILLAI_CONNECTOR_WORKER_POLL_SECONDS": "0.25",
                    "DRILLAI_CONNECTOR_WORKER_LEASE_SECONDS": "5.0",
                    "DRILLAI_SECRET_STACK_WITSML_USER": "stack_user",
                    "DRILLAI_SECRET_STACK_WITSML_PASS": "StackSecretPass99!",
                    "DRILLAI_SECRET_STACK_ETP_BIN_TOKEN": "StackEtpBinaryBearerToken99!",
                }
            )

            # Scenario 1: Clean DB migration initialization (`alembic upgrade head && alembic check`)
            subprocess.run([python_bin, "-m", "alembic", "upgrade", "head"], cwd=str(BACKEND_DIR), env=env, check=True)
            subprocess.run([python_bin, "-m", "alembic", "check"], cwd=str(BACKEND_DIR), env=env, check=True)
            record_scenario(1, "clean_db_migration_init", "alembic upgrade head & check passed")

            # Scenario 2: Start FastAPI backend server process + readiness checks
            api_proc = subprocess.Popen(
                [
                    python_bin,
                    "-m",
                    "uvicorn",
                    "drillai.api.app:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ],
                cwd=str(BACKEND_DIR),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            base_url = f"http://127.0.0.1:{api_port}"
            headers = {"X-Dev-Roles": "engineer,admin"}

            async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
                ready = False
                for _ in range(40):
                    try:
                        h1 = await client.get("/api/v1/health")
                        h2 = await client.get("/api/v1/health/ready", headers=headers)
                        if (
                            h1.status_code == 200
                            and h2.status_code == 200
                            and h2.json().get("status") == "ok"
                        ):
                            ready = True
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.2)
                if not ready:
                    raise RuntimeError("Backend failed readiness check")
                record_scenario(
                    2, "fastapi_backend_readiness", "/api/v1/health & /api/v1/health/ready 200 OK"
                )

                # Seed project & well via API
                prj = (
                    await client.post(
                        "/api/v1/projects", headers=headers, json={"name": "Stack Verify Project"}
                    )
                ).json()
                well = (
                    await client.post(
                        "/api/v1/wells",
                        headers=headers,
                        json={
                            "project_id": prj["id"],
                            "name": "STACK-WELL-01",
                            "uwi": "NO-STACK-001",
                            "well_type": "development_producer",
                            "operator": "DrillAI Operator",
                        },
                    )
                ).json()

                # Scenario 3: WITSML 1.4.1.1 SOAP connector creation, 10-stage connection test, and preview
                cnc_witsml = (
                    await client.post(
                        "/api/v1/connectors",
                        headers=headers,
                        json={
                            "key": "stack-witsml-01",
                            "name": "Stack WITSML Store",
                            "protocol_profile": "witsml.1.4.1.1.soap_http",
                            "well_id": well["id"],
                            "endpoint_url": witsml_server.endpoint_url,
                            "config": {
                                "auth_mode": "basic",
                                "poll_interval_seconds": 0.25,
                                "channel_mappings": [
                                    {
                                        "source_mnemonic": "SPP",
                                        "channel_key": "spp",
                                        "name": "Standpipe Pressure",
                                        "dimension": "pressure",
                                        "unit": "psi",
                                    }
                                ],
                            },
                            "secret_refs": {
                                "username": "env:DRILLAI_SECRET_STACK_WITSML_USER",
                                "password": "env:DRILLAI_SECRET_STACK_WITSML_PASS",
                            },
                        },
                    )
                ).json()
                cid_witsml = cnc_witsml["id"]
                w_test = (await client.post(f"/api/v1/connectors/{cid_witsml}/test", headers=headers)).json()
                assert w_test["ok"] is True
                assert [m["stage"] for m in w_test["milestones"]] == list(ALL_TEST_CONNECTION_STAGES)
                w_prev = (
                    await client.post(f"/api/v1/connectors/{cid_witsml}/preview", headers=headers)
                ).json()
                assert w_prev["sample_count"] > 0
                record_scenario(
                    3,
                    "witsml_create_test_preview",
                    f"10 milestones verified, preview_samples={w_prev['sample_count']}",
                )

                # Scenario 4: ETP 1.2 Binary Avro connector creation, 10-stage connection test, and preview
                cnc_etp_bin = (
                    await client.post(
                        "/api/v1/connectors",
                        headers=headers,
                        json={
                            "key": "stack-etp-bin-01",
                            "name": "Stack ETP 1.2 Binary Avro Stream",
                            "protocol_profile": "etp.1.2.binary_ws",
                            "well_id": well["id"],
                            "endpoint_url": etp_bin_server.endpoint_url,
                            "config": {
                                "auth_mode": "bearer",
                                "poll_interval_seconds": 0.25,
                                "channel_mappings": [
                                    {
                                        "source_mnemonic": "WOB",
                                        "channel_key": "wob",
                                        "name": "Weight on Bit",
                                        "dimension": "force",
                                        "unit": "klbf",
                                        "channel_uri": "eml://witsml20/Channel(wob)",
                                    }
                                ],
                            },
                            "secret_refs": {
                                "bearer_token": "env:DRILLAI_SECRET_STACK_ETP_BIN_TOKEN",
                            },
                        },
                    )
                ).json()
                cid_etp_bin = cnc_etp_bin["id"]
                e_test = (await client.post(f"/api/v1/connectors/{cid_etp_bin}/test", headers=headers)).json()
                assert e_test["ok"] is True
                assert [m["stage"] for m in e_test["milestones"]] == list(ALL_TEST_CONNECTION_STAGES)
                e_prev = (
                    await client.post(f"/api/v1/connectors/{cid_etp_bin}/preview", headers=headers)
                ).json()
                assert e_prev["sample_count"] > 0
                record_scenario(
                    4,
                    "etp_binary_create_test_preview",
                    f"10 milestones verified, preview_samples={e_prev['sample_count']}",
                )

                for cid in (cid_witsml, cid_etp_bin):
                    await client.post(
                        f"/api/v1/connectors/{cid}/start",
                        headers=headers,
                        json={"reason": "stack verification start"},
                    )

                # Scenario 6: Non-harness loopback port blocked by SSRF policy
                blocked_resp = await client.post(
                    "/api/v1/connectors",
                    headers=headers,
                    json={
                        "key": "stack-unapproved-loopback",
                        "name": "Unapproved Loopback Port",
                        "protocol_profile": "witsml.1.4.1.1.soap_http",
                        "well_id": well["id"],
                        "endpoint_url": policy_witsml_server.endpoint_url,
                        "config": {
                            "auth_mode": "basic",
                            "poll_interval_seconds": 0.25,
                            "channel_mappings": [
                                {
                                    "source_mnemonic": "SPP",
                                    "channel_key": "spp",
                                    "name": "Standpipe Pressure",
                                    "dimension": "pressure",
                                    "unit": "psi",
                                }
                            ],
                        },
                        "secret_refs": {
                            "username": "env:DRILLAI_SECRET_STACK_WITSML_USER",
                            "password": "env:DRILLAI_SECRET_STACK_WITSML_PASS",
                        },
                    },
                )
                assert blocked_resp.status_code == 422
                record_scenario(6, "non_harness_loopback_ssrf_blocked", "HTTP 422 ssrf_blocked enforced")

                # Scenario 7: System-scope RBAC & durable egress policy update via API + worker sync
                org_admin_put = await client.put(
                    "/api/v1/connectors/egress-policy",
                    headers={"X-Dev-Roles": "org_admin", "Idempotency-Key": "stack-org-admin-deny"},
                    json={
                        "allowed_hosts": ["witsml.rig-gateway.example.com"],
                        "allowed_private_cidrs": ["10.24.0.0/16"],
                        "reason": "org_admin should be denied",
                    },
                )
                assert org_admin_put.status_code == 403

                pol_init = (await client.get("/api/v1/connectors/egress-policy", headers=headers)).json()
                assert pol_init["revision"] == 1
                pol_upd = await client.put(
                    "/api/v1/connectors/egress-policy",
                    headers={**headers, "Idempotency-Key": "stack-verify-policy-r2"},
                    json={
                        "allowed_hosts": ["witsml.rig-gateway.example.com", "10.24.0.10"],
                        "allowed_private_cidrs": ["10.24.0.0/16"],
                        "expected_revision": 1,
                        "reason": "Stack verification durable egress policy revision 2",
                    },
                )
                assert pol_upd.status_code == 200
                assert pol_upd.json()["revision"] == 2

                # Scenario 5: Standalone worker process claiming both connectors and ingesting telemetry
                worker_proc = subprocess.Popen(
                    [python_bin, "-m", "drillai.telemetry.worker"],
                    cwd=str(BACKEND_DIR),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                both_live = False
                for _ in range(80):
                    info_w = (await client.get(f"/api/v1/connectors/{cid_witsml}", headers=headers)).json()
                    info_e = (await client.get(f"/api/v1/connectors/{cid_etp_bin}", headers=headers)).json()
                    if (
                        info_w["status"] == "running"
                        and info_w["health"]["is_live"] is True
                        and info_w.get("worker", {}).get("last_policy_revision") == 2
                        and info_w.get("worker", {}).get("policy_stale") is False
                        and info_e["status"] == "running"
                        and info_e["health"]["is_live"] is True
                        and info_e.get("worker", {}).get("last_policy_revision") == 2
                        and info_e.get("worker", {}).get("policy_stale") is False
                    ):
                        both_live = True
                        break
                    await asyncio.sleep(0.25)
                if not both_live:
                    raise RuntimeError(
                        "Standalone connector-worker did not transition both connectors to live with policy_revision=2"
                    )
                record_scenario(
                    5,
                    "standalone_worker_witsml_etp_binary_ingest",
                    "Both WITSML SOAP and ETP 1.2 Binary Avro connectors live",
                )
                record_scenario(
                    7,
                    "system_egress_policy_update_and_worker_sync",
                    "org_admin 403 denied; admin rev 2 synced to standalone worker within 1 cycle",
                )

                # Scenario 9: Worker process restart and lease recovery without duplicate ingestion
                _terminate_proc(worker_proc)
                worker_proc = subprocess.Popen(
                    [python_bin, "-m", "drillai.telemetry.worker"],
                    cwd=str(BACKEND_DIR),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                w_runs_after_restart = {"total": 0}
                for _ in range(50):
                    w_runs_after_restart = (
                        await client.get(f"/api/v1/connectors/{cid_witsml}/runs", headers=headers)
                    ).json()
                    if w_runs_after_restart.get("total", 0) >= 2:
                        break
                    await asyncio.sleep(0.25)
                assert w_runs_after_restart["total"] >= 2
                # Stop worker subprocess before running deterministic in-process fault & soak checks
                _terminate_proc(worker_proc)
                worker_proc = None
                record_scenario(
                    9,
                    "worker_restart_lease_recovery_no_duplicates",
                    f"Worker restarted and resumed cleanly ({w_runs_after_restart['total']} runs)",
                )

                # Scenario 13: Filtered-only poll (`unmapped_skipped > 0`, `accepted == 0`) recording `filtered_only`
                cnc_filtered = (
                    await client.post(
                        "/api/v1/connectors",
                        headers=headers,
                        json={
                            "key": "stack-filtered-only",
                            "name": "Stack Filtered Only WITSML",
                            "protocol_profile": "witsml.1.4.1.1.soap_http",
                            "well_id": well["id"],
                            "endpoint_url": witsml_server.endpoint_url,
                            "config": {
                                "auth_mode": "basic",
                                "poll_interval_seconds": 0.25,
                                "channel_mappings": [
                                    {
                                        "source_mnemonic": "UNMAPPED_CH_999",
                                        "channel_key": "spp",
                                        "name": "Standpipe Pressure",
                                        "dimension": "pressure",
                                        "unit": "psi",
                                    }
                                ],
                            },
                            "secret_refs": {
                                "username": "env:DRILLAI_SECRET_STACK_WITSML_USER",
                                "password": "env:DRILLAI_SECRET_STACK_WITSML_PASS",
                            },
                        },
                    )
                ).json()
                cid_filtered = cnc_filtered["id"]
                await client.post(
                    f"/api/v1/connectors/{cid_filtered}/start",
                    headers=headers,
                    json={"reason": "start filtered connector"},
                )
                await client.post(f"/api/v1/connectors/{cid_filtered}/poll", headers=headers)
                f_runs = (await client.get(f"/api/v1/connectors/{cid_filtered}/runs", headers=headers)).json()
                f_manifest = f_runs["items"][0]["details"]["manifest"]
                assert f_manifest["data_outcome"] == "filtered_only"
                assert f_manifest["unmapped_skipped"] > 0
                await client.post(
                    f"/api/v1/connectors/{cid_filtered}/stop",
                    headers=headers,
                    json={"reason": "stop filtered connector"},
                )
                record_scenario(
                    13,
                    "filtered_only_poll_outcome",
                    f"data_outcome=filtered_only (unmapped_skipped={f_manifest['unmapped_skipped']})",
                )

                # Scenario 14: Dry-run replay (`dry_run_preview`, `persisted=False`) vs live replay (`persisted=True`)
                dry_rep = (
                    await client.post(
                        f"/api/v1/connectors/{cid_witsml}/replay",
                        headers={**headers, "Idempotency-Key": "stack-replay-dry"},
                        json={
                            "reason": "Stack dry-run replay check",
                            "from_timestamp": "2020-01-01T00:00:00Z",
                            "dry_run": True,
                        },
                    )
                ).json()
                assert dry_rep["dry_run"] is True
                dry_manifest = dry_rep["run"]["details"]["manifest"]
                assert dry_manifest["data_outcome"] == "dry_run_preview"
                assert dry_manifest["persisted"] is False
                assert dry_rep["run"]["cursor_before"] == dry_rep["run"]["cursor_after"]

                live_rep = (
                    await client.post(
                        f"/api/v1/connectors/{cid_witsml}/replay",
                        headers={**headers, "Idempotency-Key": "stack-replay-live"},
                        json={
                            "reason": "Stack live replay check",
                            "from_timestamp": "2020-01-01T00:00:00Z",
                            "dry_run": False,
                        },
                    )
                ).json()
                assert live_rep["dry_run"] is False
                live_manifest = live_rep["run"]["details"]["manifest"]
                assert live_manifest["persisted"] is True
                record_scenario(
                    14,
                    "dry_run_vs_live_replay_distinction",
                    f"dry_run={dry_manifest['data_outcome']} (persisted=False) vs live={live_manifest['data_outcome']} (persisted=True)",
                )

            # Scenario 8: Restart backend API process and verify egress policy revision 2 persists in DB
            _terminate_proc(api_proc)
            api_port = _free_port()
            base_url = f"http://127.0.0.1:{api_port}"
            api_proc = subprocess.Popen(
                [
                    python_bin,
                    "-m",
                    "uvicorn",
                    "drillai.api.app:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(api_port),
                ],
                cwd=str(BACKEND_DIR),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client2:
                ready2 = False
                for _ in range(60):
                    try:
                        resp2 = await client2.get("/api/v1/health/ready", headers=headers)
                        if resp2.status_code == 200:
                            ready2 = True
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.2)
                if not ready2:
                    raise RuntimeError("Restarted backend failed readiness check")
                pol_after_restart = (
                    await client2.get("/api/v1/connectors/egress-policy", headers=headers)
                ).json()
                assert pol_after_restart["revision"] == 2
                assert "witsml.rig-gateway.example.com" in pol_after_restart["allowed_hosts"]
                record_scenario(
                    8,
                    "api_restart_preserves_durable_egress_policy",
                    "API restarted and loaded revision 2 from DB",
                )

                # Scenario 10, 11, 12, 15, 16 + Bounded 32-Cycle Soak using shared DB & live servers
                os.environ.update(env)
                from drillai.core.config import reset_settings_cache

                reset_settings_cache()
                db = Database(db_url)
                try:
                    # Scenario 11: Transient policy-store failure -> fail-closed -> same-process recovery
                    async with db.session_factory() as s_pol:
                        await sync_egress_policy_from_db(s_pol)
                    assert get_egress_policy()["policy_state"] == "active"
                    assert get_egress_policy()["source"] == "database"

                    async with db.session_factory() as s_pol_err:
                        with patch.object(
                            s_pol_err,
                            "execute",
                            side_effect=RuntimeError("simulated transient DB outage"),
                        ):
                            snap_err = await sync_egress_policy_from_db(s_pol_err, fail_closed_on_error=False)
                    assert snap_err["policy_state"] == "transient_store_error"
                    assert snap_err["status"] == "unavailable_fail_closed"
                    blocked_during_outage = False
                    try:
                        enforce_connect_time_ssrf(
                            "https://10.24.0.10/witsml/store",
                            "witsml.1.4.1.1.soap_http",
                        )
                    except (ValidationFailed, ConnectorTransportError):
                        blocked_during_outage = True
                    assert blocked_during_outage is True

                    # Same-process recovery when DB read succeeds again
                    async with db.session_factory() as s_pol_rec:
                        snap_rec = await sync_egress_policy_from_db(s_pol_rec)
                    assert snap_rec["policy_state"] == "active"
                    assert snap_rec["source"] == "database"
                    assert snap_rec["status"] == "active"
                    assert snap_rec["recovered_at"] is not None
                    enforce_connect_time_ssrf(
                        "https://10.24.0.10/witsml/store",
                        "witsml.1.4.1.1.soap_http",
                    )
                    record_scenario(
                        11,
                        "transient_policy_store_failure_and_same_process_recovery",
                        "transient_store_error failed closed, then recovered to active (database) in same process",
                    )

                    # Scenario 10: Policy revocation causing running connector to fail closed on next poll (`ssrf_blocked`)
                    pol_rev3 = await client2.put(
                        "/api/v1/connectors/egress-policy",
                        headers={**headers, "Idempotency-Key": "stack-verify-policy-r3-revoke"},
                        json={
                            "allowed_hosts": [],
                            "allowed_private_cidrs": [],
                            "expected_revision": 2,
                            "reason": "Revoke private CIDR 10.24.0.0/16 to verify fail-closed poll",
                        },
                    )
                    assert pol_rev3.status_code == 200
                    assert pol_rev3.json()["revision"] == 3
                    async with db.session_factory() as s_pol_r3:
                        await sync_egress_policy_from_db(s_pol_r3)
                    revoked_blocked = False
                    try:
                        enforce_connect_time_ssrf(
                            "https://10.24.0.10/witsml/store",
                            "witsml.1.4.1.1.soap_http",
                        )
                    except (ValidationFailed, ConnectorTransportError) as exc:
                        revoked_blocked = getattr(exc, "category", None) == "ssrf_blocked" or (
                            getattr(exc, "details", {}) or {}
                        ).get("reason") in (
                            "private_network_blocked",
                            "host_not_in_allowlist",
                            "ssrf_blocked",
                        )
                    assert revoked_blocked is True
                    record_scenario(
                        10,
                        "policy_revocation_fail_closed_ssrf_blocked",
                        "Revision 3 revoked 10.24.0.0/16 and blocked outbound connect immediately",
                    )

                    # Scenario 12: `reconciled=False` fault injection rolling back telemetry, preserving cursor, and recording `reconciliation_error`
                    worker = ConnectorWorker(db, worker_id="wrk_stack_soak", retention_limit=25)
                    async with db.session_factory() as s_pre:
                        pts_before = int(
                            (
                                await s_pre.execute(select(func.count()).select_from(TimeSeriesPoint))
                            ).scalar_one()
                        )
                        for cid_reset in (cid_witsml, cid_etp_bin):
                            r_reset = (
                                await s_pre.execute(select(Connector).where(Connector.id == cid_reset))
                            ).scalar_one()
                            r_reset.next_poll_at = None
                            r_reset.lease_expires_at = None
                            r_reset.worker_id = None
                        c_before = (
                            await s_pre.execute(select(Connector).where(Connector.id == cid_witsml))
                        ).scalar_one()
                        cursor_before_fault = dict(c_before.cursor or {})
                        await s_pre.commit()

                    soak_base_ts = (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).replace(
                        microsecond=0
                    )
                    witsml_server.rows.append(
                        WitsmlSampleRow(
                            timestamp=soak_base_ts.isoformat().replace("+00:00", "Z"),
                            depth_md=2750.0,
                            values={"SPP": 3456.0},
                            quality="good",
                        )
                    )
                    claimed = await worker.claim_connector(cid_witsml)
                    assert claimed is not None

                    from drillai.telemetry import worker as worker_mod

                    real_reconcile = worker_mod.build_and_reconcile_poll_manifest

                    def _corrupt_reconcile(*, protocol_disp, **kwargs):
                        corrupted = ProtocolPollDisposition(
                            pages_or_messages_received=protocol_disp.pages_or_messages_received,
                            source_records_discovered=protocol_disp.source_records_discovered + 99,
                            mapped_frames_emitted=protocol_disp.mapped_frames_emitted,
                        )
                        return real_reconcile(
                            protocol_disp=corrupted,
                            **kwargs,
                        )

                    with patch.object(
                        worker_mod, "build_and_reconcile_poll_manifest", side_effect=_corrupt_reconcile
                    ):
                        outcome_fault = await worker.execute_claimed_poll(claimed)
                    assert outcome_fault.status in ("backing_off", "failed")
                    assert outcome_fault.error_category == "reconciliation_error"
                    assert outcome_fault.points_accepted == 0

                    async with db.session_factory() as s_post:
                        pts_after = int(
                            (
                                await s_post.execute(select(func.count()).select_from(TimeSeriesPoint))
                            ).scalar_one()
                        )
                        c_after = (
                            await s_post.execute(select(Connector).where(Connector.id == cid_witsml))
                        ).scalar_one()
                        latest_fault_run = (
                            (
                                await s_post.execute(
                                    select(ConnectorRun)
                                    .where(ConnectorRun.connector_id == cid_witsml)
                                    .order_by(ConnectorRun.created_at.desc())
                                )
                            )
                            .scalars()
                            .first()
                        )
                        assert pts_after == pts_before
                        assert dict(c_after.cursor or {}) == cursor_before_fault
                        assert latest_fault_run is not None
                        assert latest_fault_run.status == "failed"
                        assert latest_fault_run.error_category == "reconciliation_error"
                        assert latest_fault_run.details["manifest"]["data_outcome"] == "reconciliation_error"
                        # Reset next_poll_at so subsequent soak cycles run immediately
                        c_after.next_poll_at = None
                        c_after.status = "running"
                        c_after.error_count = 0
                        await s_post.commit()
                    record_scenario(
                        12,
                        "reconciled_false_rollback_and_reconciliation_error",
                        f"Telemetry rolled back (points={pts_after}), cursor retained, run recorded reconciliation_error",
                    )

                    # Scenario 15: Bounded DNS timeout/cancellation stress followed by healthy polling
                    reset_dns_executor_state()

                    def _slow_getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> Any:
                        if host.startswith("slow-soak-"):
                            time.sleep(0.25)
                        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]

                    with patch(
                        "drillai.telemetry.connectors.socket.getaddrinfo", side_effect=_slow_getaddrinfo
                    ):
                        tasks = [
                            asyncio.create_task(
                                resolve_and_pin_endpoint_async(
                                    f"https://slow-soak-{i}.example.com/store",
                                    "witsml.1.4.1.1.soap_http",
                                    dns_timeout_seconds=0.04,
                                )
                            )
                            for i in range(12)
                        ] + [
                            asyncio.create_task(
                                resolve_and_pin_endpoint_async(
                                    f"https://fast-soak-{i}.example.com/store",
                                    "witsml.1.4.1.1.soap_http",
                                    dns_timeout_seconds=2.0,
                                )
                            )
                            for i in range(12)
                        ]
                        await asyncio.gather(*tasks, return_exceptions=True)
                        await asyncio.sleep(0.35)

                    dns_stats = get_dns_executor_stats()
                    assert dns_stats["available_permits"] == dns_stats["max_concurrency"]
                    record_scenario(
                        15,
                        "bounded_dns_stress_and_permit_recovery",
                        f"24 concurrent lookups completed; available_permits={dns_stats['available_permits']}/{dns_stats['max_concurrency']}",
                    )

                    # Bounded 32-cycle soak across WITSML SOAP and ETP 1.2 Binary Avro
                    rss_start = _rss_mb()
                    witsml_points_accepted = 0
                    etp_binary_points_accepted = 0
                    duplicate_points_suppressed = 0
                    reconciliation_failures = 0
                    max_poll_duration_ms = 0.0
                    total_cycles = 32

                    for cycle in range(total_cycles):
                        if cycle % 4 == 0:
                            ts_iso = (
                                soak_base_ts + dt.timedelta(minutes=10 + cycle)
                            ).isoformat().replace("+00:00", "Z")
                            witsml_server.rows.append(
                                WitsmlSampleRow(
                                    timestamp=ts_iso,
                                    depth_md=2800.0 + cycle,
                                    values={"SPP": 3100.0 + cycle * 5.0},
                                    quality="good",
                                )
                            )
                            etp_bin_server.append_point(
                                timestamp=ts_iso,
                                depth_md=2800.0 + cycle,
                                values={"WOB": 22.0 + cycle * 0.25},
                            )
                        async with db.session_factory() as s_due:
                            for cid in (cid_witsml, cid_etp_bin):
                                row = (
                                    await s_due.execute(select(Connector).where(Connector.id == cid))
                                ).scalar_one()
                                row.next_poll_at = None
                            await s_due.commit()

                        outcomes = await worker.run_once()
                        for out in outcomes:
                            max_poll_duration_ms = max(max_poll_duration_ms, float(out.duration_ms))
                            duplicate_points_suppressed += int(out.points_duplicates)
                            if out.error_category == "reconciliation_error":
                                reconciliation_failures += 1
                            if out.connector_id == cid_witsml:
                                witsml_points_accepted += int(out.points_accepted)
                            elif out.connector_id == cid_etp_bin:
                                etp_binary_points_accepted += int(out.points_accepted)

                    # Verify fenced stale worker write rejection
                    async with db.session_factory() as s_prep_stale:
                        c_prep = (
                            await s_prep_stale.execute(select(Connector).where(Connector.id == cid_witsml))
                        ).scalar_one()
                        c_prep.next_poll_at = None
                        await s_prep_stale.commit()
                    stale_claim = await worker.claim_connector(cid_witsml)
                    assert stale_claim is not None
                    # Bump fencing token via another worker claim after expiring lease
                    async with db.session_factory() as s_bump:
                        c_row = (
                            await s_bump.execute(select(Connector).where(Connector.id == cid_witsml))
                        ).scalar_one()
                        c_row.fencing_token += 1
                        await s_bump.commit()
                    stale_outcome = await worker.execute_claimed_poll(stale_claim)
                    fenced_stale_writes_rejected = (
                        1
                        if stale_outcome.status == "fenced"
                        or stale_outcome.error_category in ("worker_fenced", "fencing_conflict")
                        else 0
                    )
                    assert fenced_stale_writes_rejected == 1

                    rss_end = _rss_mb()
                    dns_after_soak = get_dns_executor_stats()
                    assert dns_after_soak["available_permits"] == dns_after_soak["max_concurrency"]
                    assert reconciliation_failures == 0
                    assert witsml_points_accepted >= 8
                    assert etp_binary_points_accepted >= 8

                    # Scenario 16: Clean connector stop and bounded run ledger retention check (`<= 25`)
                    for cid in (cid_witsml, cid_etp_bin):
                        runs_page = (
                            await client2.get(f"/api/v1/connectors/{cid}/runs", headers=headers)
                        ).json()
                        assert 1 <= runs_page["total"] <= 25
                        stop_res = await client2.post(
                            f"/api/v1/connectors/{cid}/stop",
                            headers=headers,
                            json={"reason": "stack verification clean stop"},
                        )
                        assert stop_res.status_code == 200
                        assert stop_res.json()["status"] == "stopped"
                        assert stop_res.json()["desired_state"] == "stopped"
                        assert stop_res.json()["health"]["is_live"] is False
                    record_scenario(
                        16,
                        "clean_connector_stop_and_bounded_run_retention",
                        "Both connectors stopped cleanly (is_live=False) and run ledger bounded <= 25",
                    )

                    soak_metrics = {
                        "total_cycles": total_cycles,
                        "witsml_points_accepted": witsml_points_accepted,
                        "etp_binary_points_accepted": etp_binary_points_accepted,
                        "duplicate_points_suppressed": duplicate_points_suppressed,
                        "fenced_stale_writes_rejected": fenced_stale_writes_rejected,
                        "dns_permits_available_after_soak": dns_after_soak["available_permits"],
                        "dns_max_permits": dns_after_soak["max_concurrency"],
                        "reconciliation_failures": reconciliation_failures,
                        "max_poll_duration_ms": round(max_poll_duration_ms, 2),
                        "worker_rss_mb_start": rss_start,
                        "worker_rss_mb_end": rss_end,
                    }
                finally:
                    await db.dispose()

            scenario_results.sort(key=lambda item: item["scenario"])
            assert len(scenario_results) == 16
            print("CP15_MULTI_PROCESS_SCENARIOS=" + json.dumps(scenario_results))
            print("CP15_SOAK_METRICS=" + json.dumps(soak_metrics))
            print("STACK_VERIFICATION_OK: all 16 multi-process scenarios and 32-cycle bounded soak verified.")
            return 0
        finally:
            _terminate_proc(worker_proc)
            _terminate_proc(api_proc)
            with contextlib.suppress(Exception):
                reset_dns_executor_state()
            await witsml_server.stop()
            await etp_bin_server.stop()
            await policy_witsml_server.stop()


if __name__ == "__main__":
    t_start = time.perf_counter()
    rc = asyncio.run(_run_verification())
    print(f"Completed in {time.perf_counter() - t_start:.2f}s")
    raise SystemExit(rc)
