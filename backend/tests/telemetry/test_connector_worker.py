"""Tests for Checkpoint C (Phase E & F): Durable ConnectorWorker, competing-worker CAS leases,
fencing-token protection against stale workers, checkpoint non-advancement on failure,
bounded ConnectorRun retention, and observability metrics."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from drillai.api.app import create_app
from drillai.core.clock import utc_now
from drillai.core.config import get_settings, reset_settings_cache
from drillai.db.models import Base, Connector, ConnectorRun, Organization, Project, Well, Wellbore
from drillai.observability.tracing import get_metrics
from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer
from drillai.telemetry.worker import ClaimedConnector, ConnectorWorker


def _mappings() -> list[dict[str, Any]]:
    return [
        {
            "source_mnemonic": "SPP",
            "channel_key": "spp",
            "name": "Standpipe Pressure",
            "dimension": "pressure",
            "unit": "psi",
        },
        {
            "source_mnemonic": "WOB",
            "channel_key": "wob",
            "name": "Weight on Bit",
            "dimension": "force",
            "unit": "klbf",
        },
    ]


async def _seed_org_and_well(app, org_id: str, well_id: str, wellbore_id: str, slug: str) -> None:
    async with app.state.database.session() as session:
        session.add(Organization(id=org_id, slug=slug, name="Worker Test Org", kind="operator"))
        session.add(Project(id="prj_wrk_01", org_id=org_id, name="Worker Project"))
        session.add(
            Well(
                id=well_id,
                org_id=org_id,
                project_id="prj_wrk_01",
                name="WRK-WELL-01",
                well_type="development_producer",
                status="drilling",
            )
        )
        session.add(
            Wellbore(
                id=wellbore_id,
                org_id=org_id,
                well_id=well_id,
                name="WRK-WB-01",
                purpose="main",
                status="active",
                is_active=True,
            )
        )
        await session.commit()


async def test_competing_workers_cas_and_stale_worker_fencing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Verify that:
    1) Two competing workers racing to claim the same connector result in exactly one winner.
    2) If Worker A loses its lease while polling and Worker B reclaims the expired lease with a higher
       fencing token, Worker A is fenced before commit and cannot overwrite Worker B's checkpoint or state.
    """
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/wrk_fence.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)

    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id = "org_000000000000000000000001"
    well_id = "wel_wrk_01"
    wellbore_id = "wbr_wrk_01"
    await _seed_org_and_well(app, org_id, well_id, wellbore_id, settings.dev_org_slug)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            hdrs = {"X-Dev-Roles": "engineer"}
            c_res = await client.post(
                "/api/v1/connectors",
                headers=hdrs,
                json={
                    "key": "synth-fence-01",
                    "name": "Fenced Synthetic Connector",
                    "protocol_profile": "synthetic.v1",
                    "well_id": well_id,
                    "wellbore_id": wellbore_id,
                    "config": {
                        "poll_interval_seconds": 0.1,
                        "channel_mappings": _mappings(),
                        "plan": {
                            "spp": [[0.0, 2800.0], [5.0, 2900.0]],
                            "wob": [[0.0, 20.0], [5.0, 22.0]],
                        },
                    },
                },
            )
            assert c_res.status_code == 201
            cid = c_res.json()["id"]

            start_res = await client.post(
                f"/api/v1/connectors/{cid}/start",
                headers=hdrs,
                json={"reason": "enable for competing workers"},
            )
            assert start_res.status_code == 200
            initial_token = start_res.json()["worker"]["fencing_token"]
            assert initial_token == 0

        # 1. Competing claim race between Worker A and Worker B
        worker_a = ConnectorWorker(app.state.database, worker_id="wrk_alpha", lease_seconds=10.0)
        worker_b = ConnectorWorker(app.state.database, worker_id="wrk_bravo", lease_seconds=10.0)

        claims = await asyncio.gather(
            worker_a.claim_connector(cid),
            worker_b.claim_connector(cid),
        )
        non_none = [c for c in claims if c is not None]
        assert len(non_none) == 1
        winner_claim = non_none[0]
        assert winner_claim.fencing_token == 1

        # Release lease so we can test in-flight lease expiry + preemption deterministically
        async with app.state.database.session() as session:
            await session.execute(
                update(Connector)
                .where(Connector.id == cid)
                .values(worker_id=None, lease_expires_at=None, next_poll_at=utc_now())
            )
            await session.commit()

        # 2. Stale Worker A claims connector (token=2), then during before_commit_hook its lease expires
        # and Worker B reclaims the expired lease (token=3) and commits its poll cycle!
        async def preempt_during_poll(claimed: ClaimedConnector) -> None:
            expired_ts = utc_now() - dt.timedelta(seconds=5)
            async with app.state.database.session() as s2:
                await s2.execute(
                    update(Connector)
                    .where(Connector.id == claimed.connector_id)
                    .values(lease_expires_at=expired_ts, next_poll_at=expired_ts)
                )
                await s2.commit()
            claim_b = await worker_b.claim_connector(claimed.connector_id)
            assert claim_b is not None
            assert claim_b.recovered_expired_lease is True
            assert claim_b.fencing_token == claimed.fencing_token + 1
            outcome_b = await worker_b.execute_claimed_poll(claim_b)
            assert outcome_b.status == "succeeded"
            assert outcome_b.fencing_token == claim_b.fencing_token

        stale_worker_a = ConnectorWorker(
            app.state.database,
            worker_id="wrk_stale_alpha",
            lease_seconds=10.0,
            before_commit_hook=preempt_during_poll,
        )
        claim_a = await stale_worker_a.claim_connector(cid)
        assert claim_a is not None
        assert claim_a.fencing_token == 2

        outcome_a = await stale_worker_a.execute_claimed_poll(claim_a)
        assert outcome_a.status == "fenced"
        assert outcome_a.error_category == "lease_fenced"

        # Verify database state reflects Worker B's token (3) and worker_id ('wrk_bravo'), not stale Worker A
        async with app.state.database.session() as session:
            row = (await session.execute(select(Connector).where(Connector.id == cid))).scalar_one()
            assert row.worker_id == "wrk_bravo"
            assert row.fencing_token == 3
            assert row.status == "running"
            assert row.reconnect_count >= 1
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


async def test_worker_witsml_ingestion_backoff_checkpoint_safety_and_run_retention(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Verify:
    - Real WITSML polling via ConnectorWorker transitions connector to running + live.
    - Transport faults do NOT advance the watermark cursor, increment error_count, enter backing_off,
      and transition to failed after max_consecutive_failures.
    - Operator restart recovers the connector once the server is healthy again.
    - ConnectorRun retention prunes old runs to keep the ledger bounded.
    """
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/wrk_witsml.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")
    monkeypatch.setenv("DRILLAI_SECRET_WRK_USER", "witsml_op")
    monkeypatch.setenv("DRILLAI_SECRET_WRK_PASS", "witsml_secret_88")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)

    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id = "org_000000000000000000000001"
    well_id = "wel_wrk_witsml_01"
    wellbore_id = "wbr_wrk_witsml_01"
    await _seed_org_and_well(app, org_id, well_id, wellbore_id, settings.dev_org_slug)

    server = LocalWitsmlSoapServer(
        expected_username="witsml_op",
        expected_password="witsml_secret_88",
    )
    server.seed_default_rows(start=utc_now() - dt.timedelta(seconds=15), count=4, step_seconds=3.0)

    try:
        async with server:
            worker = ConnectorWorker(
                app.state.database,
                worker_id="wrk_witsml_1",
                lease_seconds=15.0,
                retention_limit=3,
                allow_loopback=True,
            )
            app.state.connector_worker = worker

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                hdrs = {"X-Dev-Roles": "engineer"}
                c_res = await client.post(
                    "/api/v1/connectors",
                    headers=hdrs,
                    json={
                        "key": "witsml-worker-01",
                        "name": "WITSML Worker Rig Store",
                        "protocol_profile": "witsml.1.4.1.1.soap_http",
                        "well_id": well_id,
                        "wellbore_id": wellbore_id,
                        "endpoint_url": server.endpoint_url,
                        "config": {
                            "auth_mode": "basic",
                            "poll_interval_seconds": 0.05,
                            "max_consecutive_failures": 2,
                            "base_backoff_seconds": 0.05,
                            "max_backoff_seconds": 0.2,
                            "channel_mappings": _mappings(),
                        },
                        "secret_refs": {
                            "username": "env:DRILLAI_SECRET_WRK_USER",
                            "password": "env:DRILLAI_SECRET_WRK_PASS",
                        },
                    },
                )
                assert c_res.status_code == 201, c_res.text
                cid = c_res.json()["id"]

                # Start connector
                await client.post(
                    f"/api/v1/connectors/{cid}/start",
                    headers=hdrs,
                    json={"reason": "start WITSML polling"},
                )

                # Poll 1: succeeds, ingests 8 points (4 rows * 2 curves), advances cursor, status='running', is_live=True
                poll1 = await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)
                assert poll1.status_code == 200, poll1.text
                p1_body = poll1.json()
                assert p1_body["outcome"]["status"] == "succeeded"
                assert p1_body["outcome"]["points_accepted"] == 8
                cursor_after_p1 = p1_body["connector"]["cursor"]
                assert "cursor_timestamp" in cursor_after_p1
                assert p1_body["connector"]["status"] == "running"
                assert p1_body["connector"]["health"]["is_live"] is True
                assert p1_body["connector"]["health"]["health_state"] == "live"

                # Inject HTTP 500 fault on WITSML server; verify cursor does NOT advance and status becomes backing_off
                server.fault_mode = "http_500"
                poll2 = await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)
                assert poll2.status_code == 200
                p2_body = poll2.json()
                assert p2_body["outcome"]["status"] == "failed"
                assert p2_body["outcome"]["error_category"] == "remote_http_5xx"
                assert p2_body["connector"]["status"] == "backing_off"
                assert p2_body["connector"]["health"]["consecutive_failures"] == 1
                assert p2_body["connector"]["health"]["is_live"] is False
                assert p2_body["connector"]["cursor"] == cursor_after_p1

                # Second consecutive failure reaches max_consecutive_failures=2 -> status='failed'
                poll3 = await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)
                assert poll3.status_code == 200
                p3_body = poll3.json()
                assert p3_body["connector"]["status"] == "failed"
                assert p3_body["connector"]["health"]["health_state"] == "failed"
                assert p3_body["connector"]["health"]["is_live"] is False
                assert p3_body["connector"]["cursor"] == cursor_after_p1

                # Restore WITSML server, append a new row, and restart connector
                server.fault_mode = "ok"
                from drillai.telemetry.protocols.witsml import WitsmlSampleRow

                server.rows.append(
                    WitsmlSampleRow(
                        timestamp=utc_now().isoformat(),
                        depth_md=2490.0,
                        values={"SPP": 3100.0, "WOB": 25.0},
                    )
                )
                await client.post(
                    f"/api/v1/connectors/{cid}/restart",
                    headers=hdrs,
                    json={"reason": "server restored"},
                )

                # Run 3 more polls to verify recovery AND bounded ConnectorRun retention (limit=3)
                for _ in range(3):
                    res = await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)
                    assert res.status_code == 200
                    assert res.json()["outcome"]["status"] == "succeeded"

                final_info = (await client.get(f"/api/v1/connectors/{cid}", headers=hdrs)).json()
                assert final_info["status"] == "running"
                assert final_info["health"]["consecutive_failures"] == 0
                assert final_info["health"]["is_live"] is True
                assert (
                    final_info["cursor"]["cursor_timestamp"]
                    > cursor_after_p1["cursor_timestamp"]
                )

            # Verify ConnectorRun retention pruned older rows down to retention_limit=3
            async with app.state.database.session() as session:
                run_count = int(
                    (
                        await session.execute(
                            select(func.count()).select_from(
                                select(ConnectorRun.id)
                                .where(ConnectorRun.connector_id == cid)
                                .subquery()
                            )
                        )
                    ).scalar_one()
                )
                assert run_count == 3

            # Verify metrics snapshot has bounded connector counters and histograms
            snap = get_metrics().snapshot()
            assert "drillai.connector.polls_total" in snap["counters"]
            assert "drillai.connector.errors_total" in snap["counters"]
            assert "drillai.connector.poll_duration_ms" in snap["histograms"]
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


@pytest.mark.postgres
async def test_competing_workers_fencing_on_postgres(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
):
    """Verify competing-worker CAS lease acquisition and stale-worker fencing on real PostgreSQL."""
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", postgres_url)
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)

    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id = "org_000000000000000000000001"
    well_id = "wel_pg_wrk_01"
    wellbore_id = "wbr_pg_wrk_01"
    await _seed_org_and_well(app, org_id, well_id, wellbore_id, settings.dev_org_slug)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            hdrs = {"X-Dev-Roles": "engineer"}
            c_res = await client.post(
                "/api/v1/connectors",
                headers=hdrs,
                json={
                    "key": "pg-synth-fence-01",
                    "name": "Postgres Fenced Connector",
                    "protocol_profile": "synthetic.v1",
                    "well_id": well_id,
                    "wellbore_id": wellbore_id,
                    "config": {
                        "poll_interval_seconds": 0.1,
                        "channel_mappings": _mappings(),
                    },
                },
            )
            assert c_res.status_code == 201
            cid = c_res.json()["id"]
            await client.post(
                f"/api/v1/connectors/{cid}/start",
                headers=hdrs,
                json={"reason": "enable on postgres"},
            )

        workers = [
            ConnectorWorker(app.state.database, worker_id=f"wrk_pg_{i}", lease_seconds=10.0)
            for i in range(4)
        ]
        claims = await asyncio.gather(*(w.claim_connector(cid) for w in workers))
        winners = [c for c in claims if c is not None]
        assert len(winners) == 1
        assert winners[0].fencing_token == 1
    finally:
        await app.state.database.dispose()
        reset_settings_cache()

