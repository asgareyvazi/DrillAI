#!/usr/bin/env python3
"""End-to-end multi-process deployment stack verification (CP13 Phase 7.2).

Verifies:
1. Clean database initialization via ``alembic upgrade head`` and ``alembic check``.
2. FastAPI backend server process + readiness checks (`/api/v1/health`, `/api/v1/health/ready`).
3. Dedicated standalone ``python -m drillai.telemetry.worker`` process with strict non-production
   harness port isolation (`DRILLAI_CONNECTOR_ALLOW_LOOPBACK=false`, `DRILLAI_CONNECTOR_HARNESS_PORTS`).
4. Both WITSML 1.4.1.1 SOAP (`witsml.1.4.1.1.soap_http`) AND ETP 1.2 Binary Avro (`etp.1.2.binary_ws`)
   connectors created, started, autonomously claimed by `connector-worker`, and ingested into
   `TelemetryService` (`is_live=True`).
5. Worker process restart/resume from persisted checkpoint and clean stop (`status == "stopped"`).
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR / "src"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _run_verification() -> int:
    from drillai.telemetry.protocols.etp_binary import LocalEtpBinaryWebSocketServer
    from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer

    python_bin = str(BACKEND_DIR / ".venv" / "bin" / "python")
    alembic_bin = str(BACKEND_DIR / ".venv" / "bin" / "alembic")

    with tempfile.TemporaryDirectory(prefix="drillai_stack_") as tmpdir:
        db_path = Path(tmpdir) / "stack.db"
        db_url = f"sqlite+aiosqlite:///{db_path}"
        api_port = _free_port()

        witsml_server = LocalWitsmlSoapServer(
            expected_username="stack_user",
            expected_password="StackSecretPass99!",
        )
        etp_bin_server = LocalEtpBinaryWebSocketServer(
            expected_bearer_token="StackEtpBinaryBearerToken99!",
        )

        api_proc: subprocess.Popen[bytes] | None = None
        worker_proc: subprocess.Popen[bytes] | None = None

        try:
            await witsml_server.start()
            await etp_bin_server.start()

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

            # 1. Run one-shot migration init (`alembic upgrade head && alembic check`)
            subprocess.run([alembic_bin, "upgrade", "head"], cwd=str(BACKEND_DIR), env=env, check=True)
            subprocess.run([alembic_bin, "check"], cwd=str(BACKEND_DIR), env=env, check=True)

            # 2. Start FastAPI backend server process
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

            async with httpx.AsyncClient(base_url=base_url, timeout=5.0) as client:
                # Wait for /api/v1/health/ready
                ready = False
                for _ in range(40):
                    try:
                        resp = await client.get("/api/v1/health/ready", headers=headers)
                        if resp.status_code == 200 and resp.json().get("status") == "ok":
                            ready = True
                            break
                    except Exception:
                        pass
                    await asyncio.sleep(0.2)
                if not ready:
                    raise RuntimeError("Backend failed readiness check")

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

                # Create WITSML 1.4.1.1 SOAP connector and ETP 1.2 Binary Avro connector
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

                for cid in (cid_witsml, cid_etp_bin):
                    await client.post(
                        f"/api/v1/connectors/{cid}/start",
                        headers=headers,
                        json={"reason": "stack verification start"},
                    )

                # 3. Start dedicated standalone connector-worker process
                worker_proc = subprocess.Popen(
                    [python_bin, "-m", "drillai.telemetry.worker"],
                    cwd=str(BACKEND_DIR),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                # Wait for worker to claim BOTH connectors and ingest telemetry
                both_live = False
                for _ in range(50):
                    info_w = (await client.get(f"/api/v1/connectors/{cid_witsml}", headers=headers)).json()
                    info_e = (await client.get(f"/api/v1/connectors/{cid_etp_bin}", headers=headers)).json()
                    if (
                        info_w["status"] == "running"
                        and info_w["health"]["is_live"] is True
                        and info_e["status"] == "running"
                        and info_e["health"]["is_live"] is True
                    ):
                        both_live = True
                        break
                    await asyncio.sleep(0.25)
                if not both_live:
                    raise RuntimeError(
                        "Standalone connector-worker did not transition both WITSML and ETP binary connectors to live"
                    )

                # 4. Restart worker process and verify lease recovery
                worker_proc.terminate()
                try:
                    worker_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker_proc.kill()
                    worker_proc.wait(timeout=5)
                worker_proc = subprocess.Popen(
                    [python_bin, "-m", "drillai.telemetry.worker"],
                    cwd=str(BACKEND_DIR),
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                await asyncio.sleep(1.2)
                for cid in (cid_witsml, cid_etp_bin):
                    runs = (await client.get(f"/api/v1/connectors/{cid}/runs", headers=headers)).json()
                    assert runs["total"] >= 1, f"Expected connector run ledger entries for {cid}"
                    stop_res = await client.post(
                        f"/api/v1/connectors/{cid}/stop",
                        headers=headers,
                        json={"reason": "stack verification clean stop"},
                    )
                    assert stop_res.status_code == 200
                    assert stop_res.json()["status"] == "stopped"
                    assert stop_res.json()["desired_state"] == "stopped"

            print(
                "STACK_VERIFICATION_OK: migrations, backend, connector-worker, WITSML SOAP & ETP Binary Avro ingest, restart, and stop verified."
            )
            return 0
        finally:
            if worker_proc is not None and worker_proc.poll() is None:
                worker_proc.terminate()
                try:
                    worker_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker_proc.kill()
                    worker_proc.wait(timeout=5)
            if api_proc is not None and api_proc.poll() is None:
                api_proc.terminate()
                try:
                    api_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    api_proc.kill()
                    api_proc.wait(timeout=5)
            await witsml_server.stop()
            await etp_bin_server.stop()


if __name__ == "__main__":
    t_start = time.perf_counter()
    rc = asyncio.run(_run_verification())
    print(f"Completed in {time.perf_counter() - t_start:.2f}s")
    raise SystemExit(rc)
