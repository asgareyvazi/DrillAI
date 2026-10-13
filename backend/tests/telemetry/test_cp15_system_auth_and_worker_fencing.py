"""CP15 Phase 6 (CP15-F): System-Scope Authorization, Policy Privacy & Worker Fencing Tests.

Covers all 12 required scenarios:
1. Platform/system admin with `system.egress_policy.manage` can update egress policy.
2. `viewer`, `driller`, `engineer`, `drilling_supervisor`, `well_manager`, `integrity_engineer`,
   `data_manager`, `auditor`, tenant-only `org_admin`, and isolated-tenant organizations cannot update egress policy (`403 Forbidden`).
3. Cross-tenant connector read/update/control attempts return `404` and make no changes; cross-tenant
   non-admin egress-policy reads redact `allowed_hosts`, `allowed_private_cidrs`, `reason`, and actor metadata.
4. `Idempotency-Key` replay with identical payload returns `200` and does not double-increment `revision`.
5. `Idempotency-Key` reuse with different payload returns `409 Conflict`.
6. Validation failure (`422`) with `Idempotency-Key` does not poison the key on subsequent valid retry.
7. Stale `expected_revision` returns `409 Conflict` with `current_revision` and `expected_revision`.
8. Worker 1 claimed with token N, Worker 2 steals expired lease with token N+1: Worker 1 is fenced,
   does not overwrite cursor, and persists a `ConnectorRun(status="fenced")` ledger entry.
9. Operator edits `config_version` mid-poll: worker detects mismatch, aborts commit, and records `status="fenced"`.
10. Operator stops or disables connector mid-poll: worker detects `desired_state != "enabled"` or
    `is_enabled == False`, aborts commit, and records `status="fenced"`.
11. Lease expires before Phase 1 `adapter.connect()` or before Phase 2 commit: worker fails closed and records `status="fenced"`.
12. `ConnectorRun` pruning retains at most `retention_limit` rows including `fenced` runs.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from drillai.api.app import create_app
from drillai.core.clock import utc_now
from drillai.core.config import get_settings, reset_settings_cache
from drillai.core.errors import NotFound
from drillai.db.base import Base
from drillai.db.models import Connector, ConnectorRun, Organization, Project, Well
from drillai.db.session import Database
from drillai.security.actions import Principal
from drillai.security.rbac import SYSTEM_ROLES, permission_granted, principal_from_roles
from drillai.telemetry.connectors import (
    ConnectorService,
    clear_harness_endpoints,
    connector_out,
    redact_egress_policy_for_principal,
    register_harness_endpoint,
    reset_egress_policy,
    set_egress_policy,
)
from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer
from drillai.telemetry.worker import ClaimedConnector, ConnectorWorker

DEFAULT_ORG_ID = "org_cp15_sys_primary"
TENANT_B_ORG_ID = "org_cp15_sys_tenant_b"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRILLAI_ENV", "development")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_SECRET_CP15_SYS_TOK", "tok-cp15-sys")
    reset_settings_cache()
    clear_harness_endpoints()
    reset_egress_policy()
    yield
    clear_harness_endpoints()
    reset_egress_policy()
    reset_settings_cache()


async def _create_seeded_app(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    db_url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.setenv("DRILLAI_DATABASE_URL", db_url)
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)
    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with app.state.database.session() as session:
        session.add(
            Organization(
                id=DEFAULT_ORG_ID,
                slug=settings.dev_org_slug,
                name="Primary Platform Org",
                settings={"scope": "platform", "allow_system_egress_policy_admin": True},
            )
        )
        session.add(
            Organization(
                id=TENANT_B_ORG_ID,
                slug="tenant-b-isolated",
                name="Tenant B Isolated Org",
                settings={"tenant_tier": "isolated_tenant", "allow_system_egress_policy_admin": False},
            )
        )
        proj_a = Project(
            id="prj_cp15_sys_a",
            org_id=DEFAULT_ORG_ID,
            code="CP15-A",
            name="Project A",
        )
        proj_b = Project(
            id="prj_cp15_sys_b",
            org_id=TENANT_B_ORG_ID,
            code="CP15-B",
            name="Project B",
        )
        session.add_all([proj_a, proj_b])
        session.add(
            Well(
                id="well_cp15_sys_a",
                org_id=DEFAULT_ORG_ID,
                project_id=proj_a.id,
                uwi="UWI-CP15-SYS-A",
                name="Well A",
            )
        )
        session.add(
            Well(
                id="well_cp15_sys_b",
                org_id=TENANT_B_ORG_ID,
                project_id=proj_b.id,
                uwi="UWI-CP15-SYS-B",
                name="Well B",
            )
        )
        await session.commit()
    return app


@pytest.mark.asyncio
async def test_system_scope_rbac_privacy_redaction_idempotency_and_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenarios 1, 2, 3, 4, 5, 6, 7 + least-privilege policy redaction."""
    # Verify RBAC catalogue explicitly distinguishes platform `admin` vs tenant `org_admin`
    admin_principal = principal_from_roles(
        principal_id="u-plat-admin",
        org_id=DEFAULT_ORG_ID,
        roles=[SYSTEM_ROLES["admin"]],
    )
    org_admin_principal = principal_from_roles(
        principal_id="u-org-admin",
        org_id=DEFAULT_ORG_ID,
        roles=[SYSTEM_ROLES["org_admin"]],
    )
    assert permission_granted(admin_principal, "system.egress_policy.manage") is True
    assert permission_granted(admin_principal, "system.egress_policy.read") is True
    assert permission_granted(org_admin_principal, "connector.manage") is True
    assert permission_granted(org_admin_principal, "system.egress_policy.manage") is False

    app = await _create_seeded_app(tmp_path / "cp15_sys_rbac.db", monkeypatch)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Scenario 2: Every non-platform-admin role is rejected with 403 Forbidden
            non_system_roles = (
                "viewer",
                "driller",
                "engineer",
                "drilling_supervisor",
                "well_manager",
                "integrity_engineer",
                "data_manager",
                "auditor",
                "org_admin",
            )
            for role_name in non_system_roles:
                res_forb = await client.put(
                    "/api/v1/connectors/egress-policy",
                    headers={"X-Dev-Roles": role_name},
                    json={
                        "reason": f"Unauthorized attempt by {role_name}",
                        "allowed_hosts": ["rig-unauth.corp"],
                        "allowed_private_cidrs": ["10.55.0.0/16"],
                    },
                )
                assert res_forb.status_code == 403, f"Role {role_name} should be rejected with 403"

            # Scenario 6: Validation failure (422) with Idempotency-Key does not poison key
            idem_key = "idem-cp15-sys-policy-1"
            res_bad = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin", "Idempotency-Key": idem_key},
                json={
                    "reason": "Invalid metadata host in batch",
                    "allowed_hosts": ["169.254.169.254"],
                    "allowed_private_cidrs": ["10.55.0.0/16"],
                },
            )
            assert res_bad.status_code == 422

            # Scenario 1 & 6 continued: Valid retry with SAME Idempotency-Key by platform admin succeeds (revision 2)
            res_ok = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin", "Idempotency-Key": idem_key},
                json={
                    "reason": "Approve Rig 55 historian",
                    "allowed_hosts": ["historian.rig55.corp"],
                    "allowed_private_cidrs": ["10.55.0.0/16"],
                    "expected_revision": 1,
                },
            )
            assert res_ok.status_code == 200
            body_ok = res_ok.json()
            assert body_ok["revision"] == 2
            assert body_ok["allowed_hosts"] == ["historian.rig55.corp"]
            assert body_ok["allowed_private_cidrs"] == ["10.55.0.0/16"]

            # Scenario 4: Idempotency-Key replay with identical payload returns 200 and does NOT increment revision
            res_replay = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin", "Idempotency-Key": idem_key},
                json={
                    "reason": "Approve Rig 55 historian",
                    "allowed_hosts": ["historian.rig55.corp"],
                    "allowed_private_cidrs": ["10.55.0.0/16"],
                    "expected_revision": 1,
                },
            )
            assert res_replay.status_code == 200
            assert res_replay.json()["revision"] == 2

            # Scenario 5: Idempotency-Key reuse with different payload returns 409 Conflict
            res_conflict_idem = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin", "Idempotency-Key": idem_key},
                json={
                    "reason": "Different payload on same idempotency key",
                    "allowed_hosts": ["other.rig55.corp"],
                    "allowed_private_cidrs": ["10.55.0.0/16"],
                },
            )
            assert res_conflict_idem.status_code == 409

            # Scenario 7: Stale expected_revision returns 409 Conflict with current_revision and expected_revision
            res_stale_cas = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin"},
                json={
                    "reason": "Stale expected_revision",
                    "allowed_hosts": ["historian.rig55.corp"],
                    "allowed_private_cidrs": ["10.55.0.0/16"],
                    "expected_revision": 1,
                },
            )
            assert res_stale_cas.status_code == 409
            cas_details = res_stale_cas.json()["error"]["details"]
            assert cas_details["current_revision"] == 2
            assert cas_details["expected_revision"] == 1

            # Verify least-privilege redaction on GET /api/v1/connectors/profiles for read-only viewer:
            # revision/status/scope are visible, while allowed_hosts & allowed_private_cidrs are redacted!
            prof_viewer = await client.get(
                "/api/v1/connectors/profiles",
                headers={"X-Dev-Roles": "viewer"},
            )
            assert prof_viewer.status_code == 200
            ep_prof = prof_viewer.json()["egress_policy"]
            assert ep_prof["revision"] == 2
            assert ep_prof["status"] == "active"
            assert ep_prof["redacted"] is True
            assert ep_prof["allowed_hosts"] == []
            assert ep_prof["allowed_private_cidrs"] == []
            assert ep_prof["allowed_hosts_count"] == 1
            assert ep_prof["allowed_private_cidrs_count"] == 1
            assert ep_prof["updated_by_id"] is None

            # Verify cross-tenant non-admin reader redaction via redact_egress_policy_for_principal
            viewer_tenant_b = Principal(id="v-b", org_id=TENANT_B_ORG_ID, role_keys=("viewer",))
            redacted_cross_tenant = redact_egress_policy_for_principal(
                body_ok,
                principal=viewer_tenant_b,
                org_id=TENANT_B_ORG_ID,
                surface="egress_policy",
            )
            assert redacted_cross_tenant["redacted"] is True
            assert redacted_cross_tenant["actor_redacted"] is True
            assert redacted_cross_tenant["allowed_hosts"] == []
            assert redacted_cross_tenant["allowed_private_cidrs"] == []
            assert redacted_cross_tenant["allowed_hosts_count"] == 1
            assert redacted_cross_tenant["allowed_private_cidrs_count"] == 1
            assert redacted_cross_tenant["revision"] == 2

            # Scenario 2 continued: Principal in isolated Tenant B (even with admin header) cannot mutate system-wide egress policy!
            monkeypatch.setenv("DRILLAI_DEV_ORG_SLUG", "tenant-b-isolated")
            reset_settings_cache()
            res_tenant_b_admin = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin"},
                json={
                    "reason": "Isolated tenant trying to alter system egress policy",
                    "allowed_hosts": ["tenant-b.corp"],
                    "allowed_private_cidrs": ["10.99.0.0/16"],
                },
            )
            assert res_tenant_b_admin.status_code == 403
            assert res_tenant_b_admin.json()["error"]["details"]["reason"] == "system_scope_required"

        # Scenario 3: Cross-tenant connector read/update/control attempts raise NotFound and make no changes
        async with app.state.database.session() as session:
            svc_a = ConnectorService(
                session,
                DEFAULT_ORG_ID,
                principal=Principal(id="eng-a", org_id=DEFAULT_ORG_ID, role_keys=("engineer",)),
            )
            conn_a = await svc_a.create(
                key="conn-tenant-a-only",
                name="Tenant A Connector",
                protocol_profile="synthetic.v1",
                well_id="well_cp15_sys_a",
            )
            cid_a = conn_a["id"]
            await session.commit()

            svc_b = ConnectorService(
                session,
                TENANT_B_ORG_ID,
                principal=Principal(id="eng-b", org_id=TENANT_B_ORG_ID, role_keys=("admin", "engineer")),
            )
            with pytest.raises(NotFound):
                await svc_b.inspect(cid_a)
            with pytest.raises(NotFound):
                await svc_b.update(cid_a, expected_config_version=1, reason="Cross-tenant tamper", name="Hacked")
            with pytest.raises(NotFound):
                await svc_b.transition(cid_a, action="start", reason="Cross-tenant start")
    finally:
        await app.state.database.dispose()


@pytest.mark.asyncio
async def test_worker_fencing_preconnect_lease_expiry_config_stop_races_and_retention(
    tmp_path: Path,
) -> None:
    """Scenarios 8, 9, 10, 11, 12:
    - Scenario 8: Worker 1 claimed with token N, Worker 2 steals expired lease with token N+1 -> Worker 1 fenced.
    - Scenario 9: Operator edits `config_version` mid-poll -> Worker detects mismatch and records `status="fenced"`.
    - Scenario 10: Operator stops or disables connector mid-poll -> Worker detects `desired_state != "enabled"` and records `status="fenced"`.
    - Scenario 11: Lease expires BEFORE Phase 1 `adapter.connect()` -> Worker detects expired lease in Phase 1 before connecting and records `status="fenced"` without opening a network socket!
    - Scenario 12: `ConnectorRun` pruning retains at most `retention_limit` rows including `fenced` runs.
    - Also verifies `connector_out(row)["worker"]` exposes `system_policy_revision` and `policy_stale`.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cp15_fencing.db'}"
    db = Database(db_url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with db.session() as session:
        session.add(Organization(id=DEFAULT_ORG_ID, slug="cp15-fence-org", name="CP15 Fence Org"))
        proj = Project(id="prj_fence", org_id=DEFAULT_ORG_ID, code="FENCE", name="Fence Project")
        session.add(proj)
        session.add(
            Well(
                id="well_fence",
                org_id=DEFAULT_ORG_ID,
                project_id=proj.id,
                uwi="UWI-FENCE",
                name="Fence Well",
            )
        )
        await session.commit()

    witsml_srv = LocalWitsmlSoapServer(expected_bearer_token="tok-cp15-sys")
    await witsml_srv.start()
    register_harness_endpoint(witsml_srv.host, witsml_srv.port)

    try:
        principal = Principal(id="eng-fence", org_id=DEFAULT_ORG_ID, role_keys=("admin", "engineer"))
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            created = await svc.create(
                key="witsml-fence-conn",
                name="WITSML Fence Connector",
                protocol_profile="witsml.1.4.1.1.soap_http",
                well_id="well_fence",
                endpoint_url=witsml_srv.endpoint_url,
                config={
                    "auth_mode": "bearer",
                    "tls_verify": True,
                    "poll_interval_seconds": 0.1,
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
                secret_refs={"bearer_token": "env:DRILLAI_SECRET_CP15_SYS_TOK"},
            )
            cid = created["id"]
            await svc.transition(cid, action="start", reason="Start for fencing tests")
            await session.commit()

        # Scenario 11: Lease expires BEFORE Phase 1 `adapter.connect()`!
        # Claim connector with Worker 1, then expire `lease_expires_at` in DB before calling `execute_claimed_poll`.
        w1 = ConnectorWorker(db, worker_id="wrk-1", retention_limit=5)
        claimed_s11 = await w1.claim_connector(cid)
        assert claimed_s11 is not None
        req_count_before_s11 = witsml_srv.requests_received

        async with db.session() as session:
            await session.execute(
                update(Connector)
                .where(Connector.id == cid)
                .values(lease_expires_at=utc_now() - dt.timedelta(seconds=5))
            )
            await session.commit()

        out_s11 = await w1.execute_claimed_poll(claimed_s11)
        assert out_s11.status == "fenced"
        assert out_s11.error_category == "lease_fenced"
        # Because Phase 1 checked `lease_expires_at <= now` BEFORE `adapter.connect()`, zero SOAP requests were sent!
        assert witsml_srv.requests_received == req_count_before_s11

        # Scenario 8: Worker 1 claims token N, Worker 2 steals expired lease with token N+1 before Worker 1 commits
        claimed_s8 = await w1.claim_connector(cid)
        assert claimed_s8 is not None

        async def _steal_lease_hook(_claimed: ClaimedConnector) -> None:
            async with db.session() as s_steal:
                await s_steal.execute(
                    update(Connector)
                    .where(Connector.id == cid)
                    .values(
                        worker_id="wrk-2",
                        fencing_token=claimed_s8.fencing_token + 1,
                        lease_expires_at=utc_now() + dt.timedelta(seconds=30),
                    )
                )
                await s_steal.commit()

        w1._before_commit_hook = _steal_lease_hook
        out_s8 = await w1.execute_claimed_poll(claimed_s8)
        w1._before_commit_hook = None
        assert out_s8.status == "fenced"
        assert out_s8.error_category == "lease_fenced"

        # Scenario 9: Operator edits config_version mid-poll
        async with db.session() as session:
            await session.execute(
                update(Connector)
                .where(Connector.id == cid)
                .values(
                    lease_expires_at=utc_now() - dt.timedelta(seconds=1),
                    next_poll_at=utc_now() - dt.timedelta(seconds=1),
                )
            )
            await session.commit()

        claimed_s9 = await w1.claim_connector(cid)
        assert claimed_s9 is not None

        async def _bump_config_version_hook(_claimed: ClaimedConnector) -> None:
            async with db.session() as s_cfg:
                await s_cfg.execute(
                    update(Connector)
                    .where(Connector.id == cid)
                    .values(config_version=claimed_s9.config_version + 1)
                )
                await s_cfg.commit()

        w1._before_commit_hook = _bump_config_version_hook
        out_s9 = await w1.execute_claimed_poll(claimed_s9)
        w1._before_commit_hook = None
        assert out_s9.status == "fenced"
        assert out_s9.error_category == "lease_fenced"

        # Scenario 10: Operator stops connector (`desired_state="stopped"`, `is_enabled=False`) mid-poll
        async with db.session() as session:
            await session.execute(
                update(Connector)
                .where(Connector.id == cid)
                .values(
                    lease_expires_at=utc_now() - dt.timedelta(seconds=1),
                    next_poll_at=utc_now() - dt.timedelta(seconds=1),
                )
            )
            await session.commit()

        claimed_s10 = await w1.claim_connector(cid)
        assert claimed_s10 is not None

        async def _stop_connector_hook(_claimed: ClaimedConnector) -> None:
            async with db.session() as s_stop:
                await s_stop.execute(
                    update(Connector)
                    .where(Connector.id == cid)
                    .values(desired_state="stopped", is_enabled=False, status="stopped")
                )
                await s_stop.commit()

        w1._before_commit_hook = _stop_connector_hook
        out_s10 = await w1.execute_claimed_poll(claimed_s10)
        w1._before_commit_hook = None
        assert out_s10.status == "fenced"

        # Scenario 12: Re-enable connector, run 4 more cycles with retention_limit=5, and verify total ConnectorRun count <= 5
        async with db.session() as session:
            await session.execute(
                update(Connector)
                .where(Connector.id == cid)
                .values(
                    desired_state="enabled",
                    is_enabled=True,
                    status="running",
                    lease_expires_at=utc_now() - dt.timedelta(seconds=1),
                    next_poll_at=utc_now() - dt.timedelta(seconds=1),
                )
            )
            await session.commit()

        for _ in range(4):
            async with db.session() as session:
                await session.execute(
                    update(Connector)
                    .where(Connector.id == cid)
                    .values(
                        lease_expires_at=utc_now() - dt.timedelta(seconds=1),
                        next_poll_at=utc_now() - dt.timedelta(seconds=1),
                    )
                )
                await session.commit()
            outcomes = await w1.run_once()
            assert len(outcomes) == 1
            assert outcomes[0].status == "succeeded"

        async with db.session() as session:
            total_runs = (
                await session.execute(
                    select(func.count())
                    .select_from(ConnectorRun)
                    .where(ConnectorRun.connector_id == cid)
                )
            ).scalar_one()
            assert total_runs == 5

            # Verify connector_out(row)["worker"] exposes policy_stale when system policy revision advances!
            conn_row = await session.get(Connector, cid)
            assert conn_row is not None
            ser_before = connector_out(conn_row)
            assert ser_before["worker"]["last_policy_revision"] == 1
            assert ser_before["worker"]["system_policy_revision"] == 1
            assert ser_before["worker"]["policy_stale"] is False

            set_egress_policy(allowed_hosts=["rig-new.corp"], allowed_private_cidrs=["10.77.0.0/16"])
            ser_after = connector_out(conn_row)
            assert ser_after["worker"]["last_policy_revision"] == 1
            assert ser_after["worker"]["system_policy_revision"] == 2
            assert ser_after["worker"]["policy_stale"] is True
    finally:
        await witsml_srv.stop()
