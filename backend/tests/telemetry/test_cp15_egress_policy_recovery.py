"""CP15 Phase 2 — Durable Egress-Policy Failure Recovery & State Machine Tests.

Covers all 12 mandatory CP15-B recovery scenarios:
1. Temporary DB query failure during `sync_egress_policy_from_db()` causes immediate fail-closed behavior.
2. Subsequent successful DB read in the same process clears transient failure state and loads active policy.
3. Temporary DB query failure followed by a second DB failure remains fail-closed.
4. Corrupt `allowed_hosts` or `allowed_private_cidrs` in DB causes fail-closed behavior even when DB is healthy.
5. Subsequent reads of the same corrupt DB record continue to fail closed.
6. Valid governed update repairing the corrupt DB record recovers same process and worker process.
7. Inactive policy row (`status != "active"`) fails closed until reactivated via governed update.
8. Candidate validation failure during `PUT /api/v1/connectors/egress-policy` preserves active snapshot and revision.
9. Missing or empty `reason` during `PUT /api/v1/connectors/egress-policy` fails with 422 and preserves state.
10. Stale `expected_revision` fails with 409 Conflict and preserves stored revision.
11. Same `Idempotency-Key` with identical payload replays committed update; different payload returns 409 Conflict.
12. Separate worker process experiences policy sync failure, records fail-closed run outcome, and recovers on a
    subsequent poll cycle without worker restart once the DB policy is repaired/active.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select, text, update

from drillai.api.app import create_app
from drillai.core.config import get_settings, reset_settings_cache
from drillai.core.errors import ValidationFailed
from drillai.db.base import Base
from drillai.db.models import (
    Connector,
    ConnectorEgressPolicy,
    ConnectorRun,
    Organization,
    Project,
    Well,
)
from drillai.db.session import Database
from drillai.security.actions import Principal
from drillai.telemetry.connectors import (
    SYSTEM_EGRESS_POLICY_KEY,
    ConnectorService,
    clear_harness_endpoints,
    get_egress_policy,
    publish_committed_egress_policy_snapshot,
    register_harness_endpoint,
    reset_egress_policy,
    set_custom_dns_resolver,
    sync_egress_policy_from_db,
    update_egress_policy_in_db,
    validate_connector_endpoint,
)
from drillai.telemetry.protocols import LocalWitsmlSoapServer


@pytest.fixture(autouse=True)
def _clean_policy_and_dns_state():
    reset_egress_policy()
    clear_harness_endpoints()
    set_custom_dns_resolver(None)
    yield
    reset_egress_policy()
    clear_harness_endpoints()
    set_custom_dns_resolver(None)
    os.environ.pop("DRILLAI_AUTH_ENABLED", None)
    reset_settings_cache()


class _FailingSessionProxy:
    """Wraps a real AsyncSession and forces `execute()` to raise for a configurable number of calls."""

    def __init__(self, session: Any, fail_times: int = 1) -> None:
        self._session = session
        self.remaining_failures = fail_times
        self.calls = 0

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise RuntimeError("simulated transient database connectivity outage")
        return await self._session.execute(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


@pytest.mark.asyncio
async def test_transient_db_failure_fails_closed_then_recovers_in_same_process(
    tmp_path: Path,
) -> None:
    """Scenarios 1, 2, 3: Transient DB read error fails closed immediately, remains fail-closed on a
    second failure, and recovers in the same process once the DB read succeeds.
    """
    db_path = tmp_path / "cp15_transient_recovery.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await db.create_all()
    admin = Principal(id="usr_sys_admin", org_id="org_default", role_keys=("admin",))

    try:
        async with db.session() as session:
            _, snap = await update_egress_policy_in_db(
                session,
                principal=admin,
                actor_org_id=admin.org_id,
                reason="Initialize authoritative policy rev 2",
                allowed_hosts=["witsml.rig-alpha.example"],
                allowed_private_cidrs=["198.18.40.0/24"],
                expected_revision=1,
            )
            await session.commit()
            publish_committed_egress_policy_snapshot(snap)

        assert get_egress_policy(fail_closed=True)["revision"] == 2
        assert get_egress_policy(fail_closed=True)["policy_state"] == "active"

        # Scenario 1 & 3: Two consecutive DB failures keep the process strictly fail-closed
        async with db.session() as session:
            proxy = _FailingSessionProxy(session, fail_times=2)

            with pytest.raises(ValidationFailed) as exc1:
                await sync_egress_policy_from_db(proxy)  # type: ignore[arg-type]
            assert exc1.value.details["reason"] == "ssrf_blocked"
            assert exc1.value.details["policy_state"] == "transient_store_error"

            # Direct get_egress_policy(fail_closed=True) must also fail closed and NOT use stale rev 2
            with pytest.raises(ValidationFailed) as exc_direct:
                get_egress_policy(fail_closed=True)
            assert exc_direct.value.details["policy_state"] == "transient_store_error"

            with pytest.raises(ValidationFailed):
                validate_connector_endpoint(
                    "https://witsml.rig-alpha.example:8443/store",
                    "witsml.1.4.1.1.soap_http",
                )

            # Second consecutive DB failure remains fail-closed
            with pytest.raises(ValidationFailed) as exc2:
                await sync_egress_policy_from_db(proxy)  # type: ignore[arg-type]
            assert exc2.value.details["policy_state"] == "transient_store_error"

            # Scenario 2: Third call succeeds on the same process without restart!
            recovered = await sync_egress_policy_from_db(proxy)  # type: ignore[arg-type]
            assert recovered["status"] == "active"
            assert recovered["policy_state"] == "active"
            assert recovered["revision"] == 2
            assert recovered["allowed_hosts"] == ["witsml.rig-alpha.example"]
            assert recovered["recovered_at"] is not None

            # Subsequent get_egress_policy(fail_closed=True) succeeds
            active = get_egress_policy(fail_closed=True)
            assert active["revision"] == 2
            assert active["policy_state"] == "active"
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_corrupt_and_inactive_policy_rows_fail_closed_until_governed_repair(
    tmp_path: Path,
) -> None:
    """Scenarios 4, 5, 6, 7: Corrupt or inactive DB rows fail closed on repeated reads until repaired
    through a valid governed `update_egress_policy_in_db` mutation, and missing DB rows after database
    initialization never silently fall back to `env_default`.
    """
    db_path = tmp_path / "cp15_corrupt_repair.db"
    db = Database(f"sqlite+aiosqlite:///{db_path}")
    await db.create_all()
    admin = Principal(id="usr_sys_admin", org_id="org_default", role_keys=("admin",))

    try:
        async with db.session() as session:
            _, snap = await update_egress_policy_in_db(
                session,
                principal=admin,
                actor_org_id=admin.org_id,
                reason="Initial valid policy rev 2",
                allowed_hosts=["etp.rig-bravo.example"],
                allowed_private_cidrs=["198.18.41.0/24"],
                expected_revision=1,
            )
            await session.commit()
            publish_committed_egress_policy_snapshot(snap)

        # Scenario 4: Corrupt allowed_private_cidrs in DB
        async with db.session() as session:
            await session.execute(
                text(
                    "UPDATE connector_egress_policies "
                    "SET allowed_private_cidrs = '[\"0.0.0.0/0\", \"not-a-cidr\"]' "
                    "WHERE policy_key = :key"
                ),
                {"key": SYSTEM_EGRESS_POLICY_KEY},
            )
            await session.commit()

        # First read of corrupt record fails closed
        async with db.session() as session:
            with pytest.raises(ValidationFailed) as exc_corrupt1:
                await sync_egress_policy_from_db(session)
            assert exc_corrupt1.value.details["policy_state"] == "corrupt_policy_record"
            assert exc_corrupt1.value.details["policy_error"] == "corrupt_policy_record"

        # Scenario 5: Second read of the same corrupt record continues to fail closed
        async with db.session() as session:
            with pytest.raises(ValidationFailed) as exc_corrupt2:
                await sync_egress_policy_from_db(session)
            assert exc_corrupt2.value.details["policy_state"] == "corrupt_policy_record"

        # Scenario 6: Governed repair via update_egress_policy_in_db repairs the corrupt record to rev 3
        async with db.session() as session:
            before_pol, repaired_snap = await update_egress_policy_in_db(
                session,
                principal=admin,
                actor_org_id=admin.org_id,
                reason="Repair corrupt CIDR list with narrower private subnet",
                allowed_hosts=["etp.rig-bravo.example"],
                allowed_private_cidrs=["198.18.42.0/24"],
                expected_revision=2,
            )
            await session.commit()
            published = publish_committed_egress_policy_snapshot(repaired_snap)

        assert before_pol["policy_state"] == "corrupt_policy_record"
        assert published["revision"] == 3
        assert published["policy_state"] == "active"
        assert published["allowed_private_cidrs"] == ["198.18.42.0/24"]

        # Subsequent sync in same process succeeds
        async with db.session() as session:
            synced = await sync_egress_policy_from_db(session)
            assert synced["revision"] == 3
            assert synced["policy_state"] == "active"

        # Scenario 7: Inactive policy row (`status != "active"`) fails closed until reactivated
        async with db.session() as session:
            await session.execute(
                update(ConnectorEgressPolicy)
                .where(ConnectorEgressPolicy.policy_key == SYSTEM_EGRESS_POLICY_KEY)
                .values(status="disabled")
            )
            await session.commit()

        async with db.session() as session:
            with pytest.raises(ValidationFailed) as exc_inactive:
                await sync_egress_policy_from_db(session)
            assert exc_inactive.value.details["policy_state"] == "inactive_policy"
            assert exc_inactive.value.details["policy_status"] == "disabled"

        # Reactivate via governed update (rev 3 -> rev 4)
        async with db.session() as session:
            before_inact, reactivated_snap = await update_egress_policy_in_db(
                session,
                principal=admin,
                actor_org_id=admin.org_id,
                reason="Reactivate global egress policy after maintenance window",
                allowed_hosts=["etp.rig-bravo.example"],
                allowed_private_cidrs=["198.18.42.0/24"],
                expected_revision=3,
            )
            await session.commit()
            publish_committed_egress_policy_snapshot(reactivated_snap)

        assert before_inact["policy_state"] == "inactive_policy"
        async with db.session() as session:
            synced_active = await sync_egress_policy_from_db(session)
            assert synced_active["revision"] == 4
            assert synced_active["status"] == "active"
            assert synced_active["policy_state"] == "active"

        # Missing authoritative database row after database policy was active must fail closed (never env_default)
        async with db.session() as session:
            await session.execute(
                delete(ConnectorEgressPolicy).where(
                    ConnectorEgressPolicy.policy_key == SYSTEM_EGRESS_POLICY_KEY
                )
            )
            await session.commit()

        async with db.session() as session:
            with pytest.raises(ValidationFailed) as exc_missing:
                await sync_egress_policy_from_db(session)
            assert exc_missing.value.details["policy_state"] == "missing_policy_record"
            assert exc_missing.value.details["policy_error"] == "missing_authoritative_policy_row"
    finally:
        await db.dispose()


@pytest.mark.asyncio
async def test_api_put_egress_policy_validation_cas_idempotency_and_corrupt_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenarios 6, 8, 9, 10, 11 via HTTP API (`PUT /api/v1/connectors/egress-policy`)."""
    db_path = tmp_path / "cp15_api_policy.db"
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_CONNECTOR_WORKER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "false")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)
    db = app.state.database
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db.session() as session:
        session.add(
            Organization(
                id="org_cp15_api",
                slug=settings.dev_org_slug,
                name="CP15 API Org",
            )
        )
        await session.commit()

    headers = {"X-Dev-Roles": "admin"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            # Valid initial update (rev 1 -> rev 2) with Idempotency-Key
            r1 = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={**headers, "Idempotency-Key": "idem-cp15-pol-1"},
                json={
                    "allowed_hosts": ["rig-1.witsml.example"],
                    "allowed_private_cidrs": ["198.18.50.0/24"],
                    "expected_revision": 1,
                    "reason": "Allow rig-1 subnet",
                },
            )
            assert r1.status_code == 200, r1.text
            assert r1.json()["revision"] == 2
            assert r1.json()["policy_state"] == "active"

            # Scenario 11a: Same Idempotency-Key with identical payload replays rev 2
            r1_replay = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={**headers, "Idempotency-Key": "idem-cp15-pol-1"},
                json={
                    "allowed_hosts": ["rig-1.witsml.example"],
                    "allowed_private_cidrs": ["198.18.50.0/24"],
                    "expected_revision": 1,
                    "reason": "Allow rig-1 subnet",
                },
            )
            assert r1_replay.status_code == 200
            assert r1_replay.json()["revision"] == 2

            # Scenario 11b: Same Idempotency-Key with different payload -> 409 Conflict
            r1_conflict = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={**headers, "Idempotency-Key": "idem-cp15-pol-1"},
                json={
                    "allowed_hosts": ["rig-2.witsml.example"],
                    "allowed_private_cidrs": ["198.18.50.0/24"],
                    "expected_revision": 1,
                    "reason": "Different payload on same idempotency key",
                },
            )
            assert r1_conflict.status_code == 409

            # Scenario 8: Candidate validation failure (e.g. wildcard / broad CIDR) preserves rev 2
            r_invalid = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={**headers, "Idempotency-Key": "idem-cp15-pol-invalid"},
                json={
                    "allowed_hosts": ["*.wildcard.example"],
                    "allowed_private_cidrs": ["10.0.0.0/8"],
                    "expected_revision": 2,
                    "reason": "Attempt invalid wildcard and /8 CIDR",
                },
            )
            assert r_invalid.status_code == 422
            active_after_invalid = get_egress_policy(fail_closed=True)
            assert active_after_invalid["revision"] == 2
            assert active_after_invalid["allowed_hosts"] == ["rig-1.witsml.example"]

            # Scenario 9: Missing or whitespace-only reason fails with 422 and preserves rev 2
            r_no_reason = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={**headers, "Idempotency-Key": "idem-cp15-pol-noreason"},
                json={
                    "allowed_hosts": ["rig-1.witsml.example"],
                    "allowed_private_cidrs": ["198.18.50.0/24"],
                    "expected_revision": 2,
                    "reason": "   ",
                },
            )
            assert r_no_reason.status_code == 422
            assert get_egress_policy(fail_closed=True)["revision"] == 2

            # Scenario 10: Stale expected_revision fails with 409 Conflict and preserves rev 2
            r_stale = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={**headers, "Idempotency-Key": "idem-cp15-pol-stale"},
                json={
                    "allowed_hosts": ["rig-1.witsml.example"],
                    "allowed_private_cidrs": ["198.18.51.0/24"],
                    "expected_revision": 1,
                    "reason": "Stale expected_revision=1 when stored is 2",
                },
            )
            assert r_stale.status_code == 409
            assert r_stale.json()["error"]["details"]["stored_revision"] == 2

            # Scenario 6 via HTTP API: Corrupt DB row repaired via PUT /api/v1/connectors/egress-policy
            async with db.session() as session:
                await session.execute(
                    text(
                        "UPDATE connector_egress_policies "
                        "SET allowed_hosts = '[\"*.corrupt.example\"]' "
                        "WHERE policy_key = :key"
                    ),
                    {"key": SYSTEM_EGRESS_POLICY_KEY},
                )
                await session.commit()

            r_get_corrupt = await client.get("/api/v1/connectors/egress-policy", headers=headers)
            assert r_get_corrupt.status_code == 422
            assert r_get_corrupt.json()["error"]["details"]["policy_state"] == "corrupt_policy_record"
            assert get_egress_policy(fail_closed=False)["status"] == "unavailable_fail_closed"
            assert get_egress_policy(fail_closed=False)["policy_state"] == "corrupt_policy_record"

            r_repair = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={**headers, "Idempotency-Key": "idem-cp15-pol-repair"},
                json={
                    "allowed_hosts": ["rig-repaired.witsml.example"],
                    "allowed_private_cidrs": ["198.18.52.0/24"],
                    "expected_revision": 2,
                    "reason": "Repair corrupt allowed_hosts via governed API mutation",
                },
            )
            assert r_repair.status_code == 200, r_repair.text
            assert r_repair.json()["revision"] == 3
            assert r_repair.json()["status"] == "active"
            assert r_repair.json()["policy_state"] == "active"
            assert r_repair.json()["allowed_hosts"] == ["rig-repaired.witsml.example"]
    finally:
        await db.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_separate_worker_process_fails_closed_then_recovers_without_restart(
    postgres_url: str,
    tmp_path: Path,
) -> None:
    """Scenario 12 (PostgreSQL + separate worker OS process):
    A separate long-lived worker OS process experiences a policy sync failure (`status="disabled"`),
    records a fail-closed ConnectorRun (`error_category="validation"`), and then on its next poll cycle
    after the policy is repaired in PostgreSQL, recovers automatically without restarting the worker process.
    """
    db = Database(postgres_url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    admin = Principal(id="usr_pg_admin", org_id="org_default", role_keys=("admin", "engineer"))

    server = LocalWitsmlSoapServer(expected_bearer_token="tok-cp15-pg")
    await server.start()
    register_harness_endpoint(server.host, server.port)

    try:
        async with db.session() as session:
            await session.execute(delete(ConnectorRun))
            await session.execute(delete(Connector))
            await session.execute(delete(ConnectorEgressPolicy))
            proj = Project(
                id="prj_cp15_pg_rec",
                org_id="org_default",
                code="CP15-PG-REC",
                name="CP15 PG Recovery Project",
            )
            well = Well(
                id="well_cp15_pg_rec",
                org_id="org_default",
                project_id=proj.id,
                name="CP15 PG Recovery Well",
                uwi="CP15-PG-REC-001",
            )
            session.add_all([proj, well])
            await session.flush()

            # Initialize policy revision 2 then mark it inactive ("disabled") to trigger fail-closed sync
            _, snap = await update_egress_policy_in_db(
                session,
                principal=admin,
                actor_org_id=admin.org_id,
                reason="Initial policy allowing 10.24.0.0/16",
                allowed_hosts=["witsml.pg-worker.corp"],
                allowed_private_cidrs=["10.24.0.0/16"],
                expected_revision=1,
            )
            publish_committed_egress_policy_snapshot(snap)

            svc = ConnectorService(session, "org_default", principal=admin)
            created = await svc.create(
                key="pg-worker-recovery-witsml",
                name="PG Worker Recovery Connector",
                protocol_profile="witsml.1.4.1.1.soap_http",
                well_id=well.id,
                endpoint_url=server.endpoint_url,
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
                secret_refs={"bearer_token": "env:DRILLAI_SECRET_CP15_PG_TOK"},
            )
            cid = created["id"]
            await svc.transition(
                cid,
                action="start",
                reason="Start connector for PostgreSQL worker recovery test",
            )
            await session.execute(
                update(ConnectorEgressPolicy)
                .where(ConnectorEgressPolicy.policy_key == SYSTEM_EGRESS_POLICY_KEY)
                .values(status="disabled")
            )
            await session.commit()

        # Spawn a single long-lived worker subprocess that runs two cycles:
        # Cycle 1: policy is disabled -> worker fails closed and writes signal file "cycle1_done"
        # Wait for parent process to repair policy in PostgreSQL and write "policy_repaired"
        # Cycle 2: SAME worker instance runs `worker.run_once()` again -> recovers and ingests telemetry!
        cycle1_file = tmp_path / "cycle1_done.txt"
        repaired_file = tmp_path / "policy_repaired.txt"
        worker_script = tmp_path / "run_two_cycle_worker.py"
        worker_script.write_text(
            textwrap.dedent(
                f"""
                import asyncio
                import datetime as dt
                from pathlib import Path
                from sqlalchemy import update
                from drillai.db.base import utc_now
                from drillai.db.models import Connector
                from drillai.db.session import Database
                from drillai.telemetry.connectors import register_harness_endpoint
                from drillai.telemetry.worker import ConnectorWorker

                async def main() -> None:
                    register_harness_endpoint({server.host!r}, {server.port!r})
                    db = Database({postgres_url!r})
                    worker = ConnectorWorker(db, worker_id="wrk-cp15-pg-longlived")
                    try:
                        outcomes1 = await worker.run_once()
                        assert len(outcomes1) == 1, f"Expected 1 outcome in cycle 1, got {{outcomes1}}"
                        assert outcomes1[0].status == "failed", f"Expected failed in cycle 1, got {{outcomes1[0]}}"
                        Path({str(cycle1_file)!r}).write_text(outcomes1[0].error_category or "failed")

                        for _ in range(100):
                            if Path({str(repaired_file)!r}).exists():
                                break
                            await asyncio.sleep(0.05)

                        async with db.session() as session:
                            await session.execute(
                                update(Connector)
                                .where(Connector.id == {cid!r})
                                .values(next_poll_at=utc_now() - dt.timedelta(seconds=1))
                            )
                            await session.commit()

                        outcomes2 = await worker.run_once()
                        assert len(outcomes2) == 1, f"Expected 1 outcome in cycle 2, got {{outcomes2}}"
                        assert outcomes2[0].status == "succeeded", f"Expected succeeded in cycle 2, got {{outcomes2[0]}}"
                        assert worker.last_policy_revision == 3, f"Expected rev 3, got {{worker.last_policy_revision}}"
                    finally:
                        await db.dispose()

                if __name__ == "__main__":
                    asyncio.run(main())
                """
            ),
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
        env["DRILLAI_ENVIRONMENT"] = "test"
        env["DRILLAI_CONNECTOR_ALLOW_LOOPBACK"] = "true"
        env["DRILLAI_SECRET_CP15_PG_TOK"] = "tok-cp15-pg"
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(worker_script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Wait for Cycle 1 to complete in the child worker process
        for _ in range(100):
            if cycle1_file.exists():
                break
            await asyncio.sleep(0.05)
        assert cycle1_file.exists(), "Worker subprocess did not finish cycle 1"

        # Verify cycle 1 recorded a fail-closed ssrf_blocked run in PostgreSQL
        async with db.session() as session:
            runs_c1 = (
                await session.execute(
                    select(ConnectorRun)
                    .where(ConnectorRun.connector_id == cid)
                    .order_by(ConnectorRun.started_at.asc())
                )
            ).scalars().all()
            assert len(runs_c1) == 1
            assert runs_c1[0].status == "failed"
            assert runs_c1[0].error_category == "ssrf_blocked"

            # Now repair/reactivate the policy in PostgreSQL to revision 3
            _, repaired_snap = await update_egress_policy_in_db(
                session,
                principal=admin,
                actor_org_id=admin.org_id,
                reason="Reactivate policy rev 3 for long-lived worker subprocess",
                allowed_hosts=["witsml.pg-worker.corp"],
                allowed_private_cidrs=["10.24.0.0/16"],
                expected_revision=2,
            )
            await session.commit()
            publish_committed_egress_policy_snapshot(repaired_snap)

        repaired_file.write_text("ok", encoding="utf-8")
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20.0)
        assert proc.returncode == 0, f"Worker subprocess failed:\nSTDOUT: {stdout.decode()}\nSTDERR: {stderr.decode()}"

        # Verify Cycle 2 succeeded in the SAME worker process and persisted revision 3
        async with db.session() as session:
            runs_c2 = (
                await session.execute(
                    select(ConnectorRun)
                    .where(ConnectorRun.connector_id == cid)
                    .order_by(ConnectorRun.started_at.asc())
                )
            ).scalars().all()
            assert len(runs_c2) == 2
            assert runs_c2[0].status == "failed"
            assert runs_c2[1].status == "succeeded"
            assert runs_c2[1].points_accepted > 0

            conn_row = await session.get(Connector, cid)
            assert conn_row is not None
            assert conn_row.status == "running"
            assert (conn_row.attributes or {}).get("last_policy_revision") == 3
            assert ((runs_c2[1].details or {}).get("manifest") or {}).get("policy_revision") == 3
    finally:
        await server.stop()
        await db.dispose()
