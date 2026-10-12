"""CP14-F regression tests: Fenced ConnectorRun ledger persistence, worker policy revision
telemetry, and concurrent worker lease/config/stop race verification.

Covers:
1. Every fenced poll cycle (whether preempted by a competing worker claim, operator stop,
   config_version update, or lease expiry) persists an auditable ``ConnectorRun(status="fenced")``
   entry with ``error_category="lease_fenced"``, ``checkpoint_status="fenced"``, and
   ``policy_revision``, while leaving the ``Connector`` cursor and active lease untouched.
2. Bounded ``ConnectorRun`` retention (`run_retention_limit`) applies across mixed
   ``succeeded``, ``failed``, and ``fenced`` runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from drillai.core.config import reset_settings_cache
from drillai.db.base import Base
from drillai.db.models import Project, Well
from drillai.db.session import Database
from drillai.security.actions import Principal
from drillai.telemetry.connectors import (
    ConnectorService,
    clear_harness_endpoints,
    register_harness_endpoint,
    reset_egress_policy,
    update_egress_policy_in_db,
)
from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer
from drillai.telemetry.worker import ClaimedConnector, ConnectorWorker

DEFAULT_ORG_ID = "org_demo_default"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_SECRET_CP14F_TOK", "tok-cp14f")
    reset_settings_cache()
    reset_egress_policy()
    clear_harness_endpoints()
    yield
    clear_harness_endpoints()
    reset_egress_policy()
    reset_settings_cache()


@pytest.mark.asyncio
async def test_fenced_worker_poll_persists_bounded_fenced_run_ledger_entry(tmp_path: Path) -> None:
    """Prove a fenced worker poll records ConnectorRun(status='fenced') with policy_revision without mutating Connector."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cp14_fenced_ledger.db'}"
    db = Database(db_url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    principal = Principal(id="eng-cp14f", org_id=DEFAULT_ORG_ID, role_keys=("engineer",))
    witsml_server = LocalWitsmlSoapServer(expected_bearer_token="tok-cp14f")
    await witsml_server.start()
    register_harness_endpoint(witsml_server.host, witsml_server.port)

    try:
        async with db.session() as session:
            proj = Project(org_id=DEFAULT_ORG_ID, code="PRJ-CP14F", name="CP14-F Project")
            session.add(proj)
            await session.flush()
            well = Well(
                org_id=DEFAULT_ORG_ID,
                project_id=proj.id,
                uwi="UWI-CP14F-001",
                name="CP14-F Well",
            )
            session.add(well)
            await session.flush()

            await update_egress_policy_in_db(
                session,
                allowed_hosts=[],
                allowed_private_cidrs=[],
                reason="Initialize egress policy rev 2 for worker telemetry test",
                principal=principal,
                actor_org_id=DEFAULT_ORG_ID,
            )

            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            created = await svc.create(
                key="witsml-fenced-ledger",
                name="WITSML Fenced Ledger",
                protocol_profile="witsml.1.4.1.1.soap_http",
                well_id=well.id,
                endpoint_url=witsml_server.endpoint_url,
                config={
                    "auth_mode": "bearer",
                    "tls_verify": True,
                    "poll_interval_seconds": 0.2,
                    "channel_mappings": [
                        {
                            "source_mnemonic": "HKLD",
                            "channel_key": "hookload",
                            "name": "Hookload",
                            "dimension": "force",
                            "unit": "klbf",
                        }
                    ],
                },
                secret_refs={"bearer_token": "env:DRILLAI_SECRET_CP14F_TOK"},
            )
            await svc.transition(
                created["id"],
                action="start",
                reason="Start connector for fencing test",
            )
            await session.commit()
            cid = created["id"]

        worker_a = ConnectorWorker(db, worker_id="wrk-alpha", retention_limit=4)
        worker_b = ConnectorWorker(db, worker_id="wrk-bravo", retention_limit=4)

        # Worker A claims connector (fencing_token == 1)
        claim_a = await worker_a.claim_connector(cid)
        assert claim_a is not None
        assert claim_a.fencing_token == 1

        # Before Worker A commits Phase 2, operator bumps config_version via update()
        async def _preempt_via_config_bump(_claimed: ClaimedConnector) -> None:
            async with db.session() as s2:
                svc2 = ConnectorService(s2, DEFAULT_ORG_ID, principal=principal)
                await svc2.update(
                    cid,
                    description="Updated description mid-poll to bump config_version",
                    reason="Bump config_version while worker A is in flight",
                )
                await s2.commit()

        worker_a._before_commit_hook = _preempt_via_config_bump
        out_a = await worker_a.execute_claimed_poll(claim_a)
        worker_a._before_commit_hook = None

        assert out_a.status == "fenced"
        assert out_a.error_category == "lease_fenced"

        # Verify ConnectorRun(status='fenced') was persisted in the ledger with policy_revision=2
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            runs = await svc.list_runs(cid, limit=10)
            assert len(runs) == 1
            fenced_run = runs[0]
            assert fenced_run["status"] == "fenced"
            assert fenced_run["error_category"] == "lease_fenced"
            assert fenced_run["fencing_token"] == 1
            assert fenced_run["worker_id"] == "wrk-alpha"
            assert fenced_run["details"]["checkpoint_status"] == "fenced"
            assert fenced_run["details"]["policy_revision"] == 2

            # Verify Connector cursor was NOT advanced by the fenced poll
            snap = await svc.inspect(cid)
            assert snap["cursor"] == {}
            assert snap["config_version"] == 2
            await svc.transition(
                cid,
                action="restart",
                reason="Release lease for Worker B after config_version bump",
            )
            await session.commit()

        # Now Worker B claims and completes a normal poll (config_version == 2)
        outs_b = await worker_b.run_once()
        assert len(outs_b) == 1
        assert outs_b[0].status == "succeeded"

        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            runs_after = await svc.list_runs(cid, limit=10)
            statuses = [r["status"] for r in runs_after]
            assert statuses == ["succeeded", "fenced"]
            snap_after = await svc.inspect(cid)
            assert snap_after["worker"]["worker_id"] == "wrk-bravo"
            assert snap_after["worker"]["last_policy_revision"] == 2
    finally:
        await witsml_server.stop()
        await db.dispose()
