"""CP14 Phase 2 regression suite: Durable, atomic, versioned, and cross-process consistent
connector outbound egress policy governance (`F-CP14-01` and `F-CP14-02`).

Covers all 14 required verification scenarios from CP14 Phase 2.5:
1. Deterministic initial baseline exposed by `GET /api/v1/connectors/egress-policy` and `/profiles`.
2. Admin policy update persists to `connector_egress_policies` in DB and survives API process restart.
3. Separate `ConnectorWorker` subprocess reads the durable DB policy and permits an approved private rig endpoint.
4. Subsequent admin policy revocation causes the next `ConnectorWorker` subprocess poll to reject the endpoint with `ssrf_blocked`.
5. Invalid host in a batch update with a valid CIDR leaves both host list and CIDR list unchanged (atomic candidate validation).
6. Invalid CIDR in a batch update with a valid host leaves both host list and CIDR list unchanged.
7. Simulated audit/commit failure rolls back DB and leaves in-memory active policy snapshot unchanged.
8. Non-admin roles (`viewer`, `engineer`) receive `403 Forbidden` with zero DB or audit mutations.
9. Missing reason or malformed host/CIDR syntax returns `422` with zero DB or audit mutations.
10. Duplicate request with same `Idempotency-Key` and same payload replays the original revision without creating duplicate revisions or audit rows across system scope.
11. Same `Idempotency-Key` with different payload is rejected (`409 Conflict`) without mutating policy.
12. Failed validation with an `Idempotency-Key` does not poison or cache a failed state.
13. Concurrent updates with stale `expected_revision` (and SQL CAS races) fail deterministically with `409 Conflict`.
14. Corrupt, inactive, or unavailable policy store fails closed and never falls back to permissive egress.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from drillai.api.app import create_app
from drillai.core.config import get_settings, reset_settings_cache
from drillai.core.errors import Conflict, ValidationFailed
from drillai.db.base import Base
from drillai.db.models import (
    AuditLog,
    ConnectorEgressPolicy,
    Organization,
    Project,
    Well,
)
from drillai.security.actions import Principal
from drillai.telemetry.connectors import (
    SYSTEM_EGRESS_POLICY_KEY,
    SYSTEM_EGRESS_POLICY_SCOPE,
    ConnectorTransportError,
    clear_harness_endpoints,
    enforce_connect_time_ssrf,
    get_egress_policy,
    reset_egress_policy,
    resolve_and_pin_endpoint,
    set_custom_dns_resolver,
    set_egress_policy,
    set_egress_policy_store_failure_for_test,
    sync_egress_policy_from_db,
    update_egress_policy_in_db,
    validate_egress_policy_candidate,
)


@pytest.fixture(autouse=True)
def _clean_policy_state():
    set_custom_dns_resolver(None)
    clear_harness_endpoints()
    reset_egress_policy()
    set_egress_policy_store_failure_for_test(False)
    yield
    set_custom_dns_resolver(None)
    clear_harness_endpoints()
    reset_egress_policy()
    set_egress_policy_store_failure_for_test(False)
    reset_settings_cache()


async def _create_seeded_app(db_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_CONNECTOR_WORKER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "false")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)
    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with app.state.database.session() as session:
        org = (
            await session.execute(select(Organization).where(Organization.id == "org_cp14_egress"))
        ).scalar_one_or_none()
        if org is None:
            session.add(
                Organization(id="org_cp14_egress", slug=settings.dev_org_slug, name="CP14 Egress Org")
            )
            session.add(
                Project(
                    id="prj_cp14_egress",
                    org_id="org_cp14_egress",
                    code="cp14-egress",
                    name="CP14 Egress Project",
                )
            )
            session.add(
                Well(
                    id="wel_cp14_egress",
                    org_id="org_cp14_egress",
                    project_id="prj_cp14_egress",
                    uwi="CP14-E1",
                    name="CP14 Egress Well",
                )
            )
            await session.commit()
    return app


def test_atomic_candidate_validation_rejects_partial_mutation_in_memory():
    """Cases 5 & 6 (unit level):
    - Invalid host in a batch update with a valid CIDR leaves BOTH host list and CIDR list unchanged.
    - Invalid CIDR in a batch update with a valid host leaves BOTH host list and CIDR list unchanged.
    - Wildcard hosts, URL schemes/paths, and non-canonical numeric hosts are rejected.
    """
    baseline = get_egress_policy()
    assert baseline["allowed_hosts"] == []
    assert baseline["allowed_private_cidrs"] == []
    assert baseline["revision"] == 1
    assert baseline["scope"] == SYSTEM_EGRESS_POLICY_SCOPE

    # Case 5: Valid CIDR + prohibited host -> neither is mutated
    with pytest.raises(ValidationFailed) as exc_host:
        set_egress_policy(
            allowed_private_cidrs=["10.24.0.0/16"],
            allowed_hosts=["169.254.169.254"],
        )
    assert exc_host.value.details["field"] == "allowed_hosts"
    after_host_err = get_egress_policy()
    assert after_host_err["allowed_private_cidrs"] == []
    assert after_host_err["allowed_hosts"] == []
    assert after_host_err["revision"] == 1

    # Case 6: Valid host + prohibited/public CIDR -> neither is mutated
    with pytest.raises(ValidationFailed) as exc_cidr:
        set_egress_policy(
            allowed_hosts=["historian.rig24.corp"],
            allowed_private_cidrs=["0.0.0.0/0"],
        )
    assert exc_cidr.value.details["field"] == "allowed_private_cidrs"
    after_cidr_err = get_egress_policy()
    assert after_cidr_err["allowed_hosts"] == []
    assert after_cidr_err["allowed_private_cidrs"] == []
    assert after_cidr_err["revision"] == 1

    # Malformed host syntax (wildcards, schemes, paths, non-canonical numeric IPv4)
    for bad_host in (
        "*.rig24.corp",
        "https://historian.rig24.corp/store",
        "historian.rig24.corp/path",
        "user@historian.rig24.corp",
        "0177.0.0.1",
        "2130706433",
        "localhost",
        "metadata.google.internal",
        "service.internal",
        "historian.rig24.corp:99999",
    ):
        with pytest.raises(ValidationFailed):
            validate_egress_policy_candidate(
                allowed_hosts=[bad_host],
                allowed_private_cidrs=["10.24.0.0/16"],
            )


async def test_durable_persistence_across_api_restart_and_atomic_validation_rollbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cases 1, 2, 5, 6, 7, 8, 9, 10, 11, 12, 13:
    Verify full HTTP API lifecycle, persistence across process restart, atomic rollback on commit/audit
    failure, RBAC, idempotency replay/conflict/unpoisoned failure, and optimistic concurrency (`expected_revision`).
    """
    db_path = tmp_path / "cp14_egress_durable.db"
    app1 = await _create_seeded_app(db_path, monkeypatch)

    try:
        async with AsyncClient(transport=ASGITransport(app=app1), base_url="http://test") as client:
            # Case 1: Initial policy state is deterministic and exposed on both endpoints
            init_res = await client.get(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "viewer"},
            )
            assert init_res.status_code == 200
            init_body = init_res.json()
            assert init_body["policy_key"] == SYSTEM_EGRESS_POLICY_KEY
            assert init_body["scope"] == SYSTEM_EGRESS_POLICY_SCOPE
            assert init_body["status"] == "active"
            assert init_body["source"] == "env_default"
            assert init_body["revision"] == 1
            assert init_body["allowed_hosts"] == []
            assert init_body["allowed_private_cidrs"] == []

            prof_res = await client.get(
                "/api/v1/connectors/profiles",
                headers={"X-Dev-Roles": "viewer"},
            )
            assert prof_res.status_code == 200
            assert prof_res.json()["egress_policy"]["revision"] == 1
            assert prof_res.json()["egress_policy"]["scope"] == SYSTEM_EGRESS_POLICY_SCOPE

            # Case 8: Non-admin roles (viewer, engineer) receive 403 and make no DB/audit changes
            for non_admin_role in ("viewer", "engineer"):
                forb = await client.put(
                    "/api/v1/connectors/egress-policy",
                    headers={"X-Dev-Roles": non_admin_role},
                    json={
                        "reason": "unauthorized attempt",
                        "allowed_hosts": ["historian.rig24.corp"],
                        "allowed_private_cidrs": ["10.24.0.0/16"],
                    },
                )
                assert forb.status_code == 403

            # Case 9: Missing reason or malformed host/CIDR syntax returns 422 and makes no DB/audit changes
            empty_reason_res = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin"},
                json={
                    "reason": "",
                    "allowed_hosts": ["historian.rig24.corp"],
                },
            )
            assert empty_reason_res.status_code == 422

            # Cases 5, 6, & 12: Invalid host in a batch update with a valid CIDR + Idempotency-Key
            # fails with 422, leaves policy unchanged, and does NOT poison the Idempotency-Key!
            idem_key_unpoisoned = "idem-cp14-atomic-check-1"
            bad_batch_res = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin", "Idempotency-Key": idem_key_unpoisoned},
                json={
                    "reason": "Batch with valid CIDR and prohibited metadata host",
                    "allowed_private_cidrs": ["10.24.0.0/16"],
                    "allowed_hosts": ["historian.rig24.corp", "169.254.169.254"],
                },
            )
            assert bad_batch_res.status_code == 422
            assert get_egress_policy()["allowed_private_cidrs"] == []
            assert get_egress_policy()["allowed_hosts"] == []
            assert get_egress_policy()["revision"] == 1

            bad_cidr_batch_res = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin"},
                json={
                    "reason": "Batch with valid host and public CIDR",
                    "allowed_hosts": ["historian.rig24.corp"],
                    "allowed_private_cidrs": ["10.24.0.0/16", "8.8.8.0/24"],
                },
            )
            assert bad_cidr_batch_res.status_code == 422
            assert get_egress_policy()["allowed_hosts"] == []
            assert get_egress_policy()["allowed_private_cidrs"] == []

            # Case 12 continued: Retrying with the SAME Idempotency-Key and valid payload succeeds!
            ok_res = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin", "Idempotency-Key": idem_key_unpoisoned},
                json={
                    "reason": "Approve Rig 24 historian and OT subnet",
                    "allowed_hosts": ["Historian.Rig24.Corp.", "etp.rig24.corp:8443"],
                    "allowed_private_cidrs": ["10.24.0.0/16"],
                    "expected_revision": 1,
                },
            )
            assert ok_res.status_code == 200
            rev2_body = ok_res.json()
            assert rev2_body["revision"] == 2
            assert rev2_body["source"] == "database"
            assert rev2_body["allowed_hosts"] == ["historian.rig24.corp", "etp.rig24.corp:8443"]
            assert rev2_body["allowed_private_cidrs"] == ["10.24.0.0/16"]
            assert rev2_body["reason"] == "Approve Rig 24 historian and OT subnet"
            assert rev2_body["updated_at"] is not None

            # Case 10: Duplicate request with same Idempotency-Key and same payload replays revision 2
            replay_res = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin", "Idempotency-Key": idem_key_unpoisoned},
                json={
                    "reason": "Approve Rig 24 historian and OT subnet",
                    "allowed_hosts": ["Historian.Rig24.Corp.", "etp.rig24.corp:8443"],
                    "allowed_private_cidrs": ["10.24.0.0/16"],
                },
            )
            assert replay_res.status_code == 200
            assert replay_res.json()["revision"] == 2
            assert replay_res.json()["policy_id"] == rev2_body["policy_id"]

            # Case 11: Same Idempotency-Key with different payload is rejected with 409 Conflict
            mismatch_res = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin", "Idempotency-Key": idem_key_unpoisoned},
                json={
                    "reason": "Different payload under same idempotency key",
                    "allowed_hosts": ["other.rig24.corp"],
                    "allowed_private_cidrs": ["10.24.0.0/16"],
                },
            )
            assert mismatch_res.status_code == 409
            assert get_egress_policy()["revision"] == 2

            # Case 13: Stale expected_revision (1 when current is 2) fails with 409 Conflict
            stale_rev_res = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin"},
                json={
                    "reason": "Attempt update with stale expected_revision=1",
                    "allowed_hosts": ["stale.rig24.corp"],
                    "allowed_private_cidrs": ["10.24.0.0/16"],
                    "expected_revision": 1,
                },
            )
            assert stale_rev_res.status_code == 409
            assert stale_rev_res.json()["error"]["details"]["expected_revision"] == 1
            assert stale_rev_res.json()["error"]["details"]["stored_revision"] == 2

            # Case 7: Simulated audit failure rolls back DB transaction and leaves active policy at revision 2
            with patch(
                "drillai.api.routers.connectors.record_audit",
                side_effect=RuntimeError("simulated audit sink failure"),
            ), pytest.raises(RuntimeError, match="simulated audit sink failure"):
                await client.put(
                    "/api/v1/connectors/egress-policy",
                    headers={"X-Dev-Roles": "admin"},
                    json={
                        "reason": "Should roll back completely on audit failure",
                        "allowed_hosts": ["rollback.rig24.corp"],
                        "allowed_private_cidrs": ["10.99.0.0/16"],
                        "expected_revision": 2,
                    },
                )

            # Verify in-memory snapshot and DB row remain at revision 2 with original Rig 24 values
            assert get_egress_policy()["revision"] == 2
            assert get_egress_policy()["allowed_hosts"] == [
                "historian.rig24.corp",
                "etp.rig24.corp:8443",
            ]
            assert get_egress_policy()["allowed_private_cidrs"] == ["10.24.0.0/16"]

        async with app1.state.database.session() as session:
            db_row = (
                await session.execute(
                    select(ConnectorEgressPolicy).where(
                        ConnectorEgressPolicy.policy_key == SYSTEM_EGRESS_POLICY_KEY
                    )
                )
            ).scalar_one()
            assert db_row.revision == 2
            assert db_row.allowed_hosts == ["historian.rig24.corp", "etp.rig24.corp:8443"]
            assert db_row.allowed_private_cidrs == ["10.24.0.0/16"]
            audits = (
                (
                    await session.execute(
                        select(AuditLog).where(AuditLog.action == "connector.egress_policy.update")
                    )
                )
                .scalars()
                .all()
            )
            assert len(audits) == 1
    finally:
        await app1.state.database.dispose()

    # Case 2: Clear in-memory state completely (simulating process restart) and boot a brand-new app instance
    reset_egress_policy()
    assert get_egress_policy()["source"] == "env_default"
    assert get_egress_policy()["allowed_hosts"] == []

    app2 = await _create_seeded_app(db_path, monkeypatch)
    try:
        async with AsyncClient(transport=ASGITransport(app=app2), base_url="http://test") as client2:
            after_restart = await client2.get(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "viewer"},
            )
            assert after_restart.status_code == 200
            body2 = after_restart.json()
            assert body2["source"] == "database"
            assert body2["revision"] == 2
            assert body2["allowed_hosts"] == ["historian.rig24.corp", "etp.rig24.corp:8443"]
            assert body2["allowed_private_cidrs"] == ["10.24.0.0/16"]
            assert body2["reason"] == "Approve Rig 24 historian and OT subnet"
    finally:
        await app2.state.database.dispose()


async def test_concurrent_sql_cas_race_and_fail_closed_policy_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cases 13 & 14:
    - Concurrent SQL CAS race on `ConnectorEgressPolicy` allows exactly one winner and raises `Conflict` for the loser.
    - Corrupt, disabled, or unavailable policy store fails closed and blocks outbound connections.
    """
    db_path = tmp_path / "cp14_cas_and_fail_closed.db"
    app = await _create_seeded_app(db_path, monkeypatch)
    principal = Principal(
        id="usr_admin_cas",
        org_id="org_cp14_egress",
        display_name="Admin CAS",
        role_keys=("admin",),
    )

    try:
        # Seed revision 2 in DB
        async with app.state.database.session() as s_init:
            _, _snap = await update_egress_policy_in_db(
                s_init,
                principal=principal,
                actor_org_id="org_cp14_egress",
                reason="Initial DB revision 2",
                allowed_hosts=["rig1.corp"],
                allowed_private_cidrs=["10.10.0.0/16"],
                expected_revision=1,
            )
            await s_init.commit()

        # Open two independent sessions that both read revision 2 and attempt to CAS-update to revision 3
        async with app.state.database.session() as s1, app.state.database.session() as s2:
            await sync_egress_policy_from_db(s1)
            _, _snap1 = await update_egress_policy_in_db(
                s1,
                principal=principal,
                actor_org_id="org_cp14_egress",
                reason="Winner update to rev 3",
                allowed_hosts=["rig1.corp", "rig2.corp"],
                allowed_private_cidrs=["10.10.0.0/16"],
                expected_revision=2,
            )
            await s1.commit()

            with pytest.raises(Conflict) as cas_exc:
                await update_egress_policy_in_db(
                    s2,
                    principal=principal,
                    actor_org_id="org_cp14_egress",
                    reason="Stale concurrent update",
                    allowed_hosts=["rig3.corp"],
                    allowed_private_cidrs=["10.30.0.0/16"],
                    expected_revision=2,
                )
            assert cas_exc.value.details["expected_revision"] == 2

        # Case 14a: Simulated policy store failure fails closed immediately
        set_egress_policy_store_failure_for_test(True, "simulated_db_outage")
        with pytest.raises(ValidationFailed) as fc_exc:
            resolve_and_pin_endpoint(
                "https://rig1.corp/witsml/store",
                "witsml.1.4.1.1.soap_http",
                resolver=lambda h, p: ["10.10.1.5"],
            )
        assert fc_exc.value.details["reason"] == "ssrf_blocked"

        with pytest.raises(ConnectorTransportError) as transport_fc_exc:
            enforce_connect_time_ssrf(
                "https://rig1.corp/witsml/store",
                "witsml.1.4.1.1.soap_http",
                resolver=lambda h, p: ["10.10.1.5"],
            )
        assert transport_fc_exc.value.category == "ssrf_blocked"
        set_egress_policy_store_failure_for_test(False)

        # Case 14b: Inactive/disabled policy status in DB fails closed on sync
        async with app.state.database.session() as s_corrupt:
            await s_corrupt.execute(
                text("UPDATE connector_egress_policies SET status = 'disabled' WHERE policy_key = 'global'")
            )
            await s_corrupt.commit()

        async with app.state.database.session() as s_check:
            with pytest.raises(ValidationFailed) as status_exc:
                await sync_egress_policy_from_db(s_check)
            assert status_exc.value.details["reason"] == "ssrf_blocked"
    finally:
        await app.state.database.dispose()


async def test_multi_process_api_and_connector_worker_enforce_durable_egress_policy_and_revocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cases 3 & 4:
    Prove across SEPARATE OS PROCESSES sharing a SQLite database that:
    1. Admin policy update via the API (`PUT /api/v1/connectors/egress-policy` approving
       `allowed_hosts=["rig24-witsml.private.corp"]` and `allowed_private_cidrs=["10.24.0.0/16"]`)
       is observed by a separate `ConnectorWorker` subprocess, which connects and ingests telemetry
       frames while recording `last_policy_revision == 2`.
    2. Subsequent admin policy update via the API revoking `allowed_private_cidrs=[]` (revision 3)
       causes the next `ConnectorWorker` subprocess poll to reject the connector endpoint with
       `error_category == "ssrf_blocked"` and `last_policy_revision == 3`.
    """
    from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer

    db_path = tmp_path / "cp14_multiprocess_egress.db"
    app = await _create_seeded_app(db_path, monkeypatch)

    # Start a real local WITSML SOAP server in the parent test process; the child worker process
    # will resolve `rig24-witsml.private.corp` via a test DNS hook to `10.24.12.8` for policy
    # validation or connect to the local server when approved.
    witsml_srv = LocalWitsmlSoapServer()
    await witsml_srv.start()
    srv_url = witsml_srv.endpoint_url
    srv_port = witsml_srv.port

    worker_script = tmp_path / "run_worker_cycle.py"
    worker_script.write_text(
        f"""
import asyncio
import json
import os
import sys
import httpx

from drillai.core.config import get_settings, reset_settings_cache
from drillai.db.session import Database
from drillai.telemetry.connectors import enforce_connect_time_ssrf, set_custom_dns_resolver
from drillai.telemetry.protocols.witsml import WitsmlSoapClient
from drillai.telemetry.worker import ConnectorWorker

# Simulate a private rig DNS record `rig24-witsml.private.corp -> 10.24.12.8`
set_custom_dns_resolver(lambda host, port: ["10.24.12.8"])

# Route pinned 10.24.12.8:{srv_port} TCP traffic to the local WITSML SOAP test server ({srv_url})
# ONLY after `enforce_connect_time_ssrf` has validated and pinned `10.24.12.8` against the DB egress policy.
async def _routed_post_soap(self, envelope: str, *, soap_action: str = "Store.WMLS_GetFromStore") -> str:
    pinned = enforce_connect_time_ssrf(self._endpoint_url, "witsml.1.4.1.1.soap_http")
    assert pinned.pinned_ip == "10.24.12.8"
    assert pinned.is_private_rig is True
    headers = self._build_headers(soap_action=soap_action)
    headers["Host"] = pinned.host_header
    async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
        resp = await client.post(
            {srv_url!r},
            content=envelope.encode("utf-8"),
            headers=headers,
        )
        return resp.text
WitsmlSoapClient._post_soap = _routed_post_soap

async def main():
    reset_settings_cache()
    settings = get_settings()
    db = Database(settings=settings)
    try:
        worker = ConnectorWorker(db, worker_id="wrk_subproc_cp14", allow_loopback=False)
        outcomes = await worker.run_once()
        print("WORKER_OUTCOME_JSON:" + json.dumps([
            {{
                "connector_id": o.connector_id,
                "status": o.status,
                "error_category": o.error_category,
                "error_message": o.error_message,
                "points_accepted": o.points_accepted,
                "policy_revision": worker.last_policy_revision,
            }}
            for o in outcomes
        ]))
    finally:
        await db.dispose()

if __name__ == "__main__":
    asyncio.run(main())
""".strip(),
        encoding="utf-8",
    )

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Step 1: Admin approves `rig24-witsml.private.corp` and `10.24.0.0/16` (revision 2)
            put_rev2 = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin"},
                json={
                    "reason": "Approve Rig 24 private WITSML historian for worker subprocess",
                    "allowed_hosts": ["rig24-witsml.private.corp"],
                    "allowed_private_cidrs": ["10.24.0.0/16"],
                    "expected_revision": 1,
                },
            )
            assert put_rev2.status_code == 200
            assert put_rev2.json()["revision"] == 2

            # Create and start a connector pointing to `https://rig24-witsml.private.corp:{srv_port}/witsml/store`
            set_custom_dns_resolver(lambda host, port: ["10.24.12.8"])
            create_res = await client.post(
                "/api/v1/connectors",
                headers={"X-Dev-Roles": "engineer"},
                json={
                    "key": "rig24-private-witsml",
                    "name": "Rig 24 Private WITSML",
                    "protocol_profile": "witsml.1.4.1.1.soap_http",
                    "well_id": "wel_cp14_egress",
                    "endpoint_url": f"https://rig24-witsml.private.corp:{srv_port}/witsml/store",
                    "config": {
                        "auth_mode": "none",
                        "poll_interval_seconds": 0.1,
                        "channel_mappings": [
                            {
                                "source_mnemonic": "SPP",
                                "channel_key": "spp",
                                "name": "Standpipe Pressure",
                                "unit": "psi",
                                "dimension": "pressure",
                            },
                            {
                                "source_mnemonic": "WOB",
                                "channel_key": "wob",
                                "name": "Weight on Bit",
                                "unit": "klbf",
                                "dimension": "force",
                            },
                        ],
                    },
                },
            )
            assert create_res.status_code == 201
            cid = create_res.json()["id"]

            start_res = await client.post(
                f"/api/v1/connectors/{cid}/start",
                headers={"X-Dev-Roles": "engineer"},
                json={"reason": "Start Rig 24 connector"},
            )
            assert start_res.status_code == 200

            # Step 2: Run the separate ConnectorWorker OS subprocess while revision 2 is active.
            # Notice: Under revision 2, `validate_connector_endpoint` in Phase 1 PASSES `ssrf_blocked`
            # because the child process loaded revision 2 from SQLite (it only fails on TCP connect to
            # non-routable 10.24.12.8 with timeout/network error, NOT `ssrf_blocked`).
            env = {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src"),
                "DRILLAI_ENVIRONMENT": "test",
                "DRILLAI_AUTH_ENABLED": "false",
                "DRILLAI_DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
                "DRILLAI_CONNECTOR_ALLOW_LOOPBACK": "false",
            }
            proc1 = await asyncio.create_subprocess_exec(
                sys.executable,
                str(worker_script),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout1_b, stderr1_b = await asyncio.wait_for(proc1.communicate(), timeout=20.0)
            assert proc1.returncode == 0, stderr1_b.decode("utf-8", errors="replace")
            stdout1 = stdout1_b.decode("utf-8")
            line1 = next(
                line for line in stdout1.splitlines() if line.startswith("WORKER_OUTCOME_JSON:")
            )
            outcomes1 = json.loads(line1.split("WORKER_OUTCOME_JSON:", 1)[1])
            assert len(outcomes1) == 1
            assert outcomes1[0]["policy_revision"] == 2
            assert outcomes1[0]["status"] == "succeeded", f"Unexpected worker failure: {outcomes1[0]}"
            assert outcomes1[0]["points_accepted"] > 0
            assert outcomes1[0]["error_category"] is None

            # Reset connector `next_poll_at` so it is immediately due for the next worker cycle
            async with app.state.database.session() as s_due:
                await s_due.execute(
                    text("UPDATE connectors SET next_poll_at = NULL, status = 'running' WHERE id = :cid"),
                    {"cid": cid},
                )
                await s_due.commit()

            # Step 3: Admin revokes `allowed_private_cidrs` via API (revision 3)
            put_rev3 = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin"},
                json={
                    "reason": "Revoke Rig 24 private CIDR segment",
                    "allowed_hosts": ["rig24-witsml.private.corp"],
                    "allowed_private_cidrs": [],
                    "expected_revision": 2,
                },
            )
            assert put_rev3.status_code == 200
            assert put_rev3.json()["revision"] == 3

            # Step 4: Run a separate ConnectorWorker OS subprocess and verify it loads revision 3
            # from the database and immediately blocks the poll with `error_category == "ssrf_blocked"`!
            proc2 = await asyncio.create_subprocess_exec(
                sys.executable,
                str(worker_script),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout2_b, stderr2_b = await asyncio.wait_for(proc2.communicate(), timeout=20.0)
            assert proc2.returncode == 0, stderr2_b.decode("utf-8", errors="replace")
            stdout2 = stdout2_b.decode("utf-8")
            line2 = next(
                line for line in stdout2.splitlines() if line.startswith("WORKER_OUTCOME_JSON:")
            )
            outcomes2 = json.loads(line2.split("WORKER_OUTCOME_JSON:", 1)[1])
            assert len(outcomes2) == 1
            assert outcomes2[0]["policy_revision"] == 3
            assert outcomes2[0]["status"] == "failed"
            assert outcomes2[0]["error_category"] == "ssrf_blocked"

            # Inspect connector via API and verify worker recorded last_policy_revision == 3
            detail_res = await client.get(
                f"/api/v1/connectors/{cid}",
                headers={"X-Dev-Roles": "viewer"},
            )
            assert detail_res.status_code == 200
            detail_body = detail_res.json()
            assert detail_body["worker"]["last_policy_revision"] == 3
            assert detail_body["health"]["last_error_category"] == "ssrf_blocked"
    finally:
        await witsml_srv.stop()
        await app.state.database.dispose()
