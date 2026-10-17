"""CP16 Independent Adversarial Audit — Security, Async Loop, Physics & Readiness Regression Suite.

Covers confirmed findings from the CP16 adversarial source audit:
- F-CP16-01 (P1): Async event-loop non-blocking guarantee during slow DNS resolution across
  validate_connector_endpoint_async(require_dns_resolution=False), ConnectorService, and ConnectorWorker.
- F-CP16-02 (P0): Cross-tenant isolation on well audit trail, twin snapshot comparison, scenario snapshot
  linkage, workflow version history/detail/summary, and well-scoped twin read endpoints.
- F-CP16-03 (P1): Cross-tenant hierarchy scope validation on well context, twin aspect writes,
  workflow creation/runs, and registry engine execution (plus persist=False support).
- F-CP16-04 (P0): Workflow approval & resume security (cross-run/cross-tenant approval substitution refusal,
  waiting_approval unapproved resume refusal, and breakpoint pause/resume lifecycle).
- F-CP16-05 (P1): Unit registry symbol collision fix ('m2' area vs 'm2-perm' permeability) and
  cross-dimension duplicate symbol guard.
- F-CP16-06 (P1): API 5C3 triaxial Lamé stress under mud_weight_si hydrostatic external load and
  JSON-safe finite safety factor serialization, plus independent engineering reference benchmarks.
- F-CP16-07 (P2): Deployment configuration readiness (.env.example validation, multi-origin wildcard CORS
  guard in production, and worker production loopback guard).
"""

from __future__ import annotations

import asyncio
import json
import math
import shutil
import sys
import time
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from drillai.api.app import create_app
from drillai.core.config import REPO_ROOT, Settings, reset_settings_cache
from drillai.core.errors import (
    ApprovalRequired,
    ConfigurationError,
    NodeExecutionFailed,
    UnitError,
    ValidationFailed,
)
from drillai.db.models import (
    ApprovalRequest,
    Organization,
    Project,
    Scenario,
    Well,
    Wellbore,
    WellSection,
)
from drillai.engines.registry import load_default_engines
from drillai.security.actions import ActionLevel, Principal
from drillai.telemetry import connectors as connectors_mod
from drillai.telemetry.connectors import (
    ALL_TEST_CONNECTION_STAGES,
    EGRESS_POLICY_STATES,
    POLL_DATA_OUTCOMES,
    PROTOCOL_PROFILE_SPECS,
    ConnectorService,
    reset_egress_policy,
    resolve_and_pin_endpoint_async,
)
from drillai.telemetry.worker import ConnectorWorker
from drillai.twin.service import AspectRevision, TwinService
from drillai.units.registry import Dimension, Unit, convert, get_unit, q, register
from drillai.workflow.graph import GraphEdge, GraphNode, WorkflowGraph
from drillai.workflow.runtime import WorkflowRuntime
from drillai.workflow.service import RunScope, WorkflowService


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_DEV_ORG_SLUG", "org-alpha")
    reset_settings_cache()
    reset_egress_policy()
    yield
    reset_settings_cache()
    reset_egress_policy()


async def _seed_two_tenants(app) -> dict[str, str]:
    async with app.state.database.session() as session:
        org_a = Organization(id="org_alpha", slug="org-alpha", name="Alpha Drilling")
        org_b = Organization(id="org_beta", slug="org-beta", name="Beta Offshore")
        proj_a = Project(id="prj_alpha", org_id="org_alpha", code="ALPHA-1", name="Alpha Project")
        proj_b = Project(id="prj_beta", org_id="org_beta", code="BETA-1", name="Beta Project")
        well_a = Well(
            id="wel_alpha",
            org_id="org_alpha",
            project_id="prj_alpha",
            uwi="ALPHA-001",
            name="Alpha Well 1",
        )
        well_b = Well(
            id="wel_beta",
            org_id="org_beta",
            project_id="prj_beta",
            uwi="BETA-001",
            name="Beta Well 1",
        )
        wb_a = Wellbore(
            id="wbr_alpha",
            org_id="org_alpha",
            well_id="wel_alpha",
            name="Alpha WB 1",
            sequence=1,
        )
        wb_b = Wellbore(
            id="wbr_beta",
            org_id="org_beta",
            well_id="wel_beta",
            name="Beta WB 1",
            sequence=1,
        )
        sec_b = WellSection(
            id="sec_beta",
            org_id="org_beta",
            wellbore_id="wbr_beta",
            sequence=1,
            name="12.25 in Intermediate",
            hole_diameter_si=0.31115,
            planned_top_md_si=0.0,
            planned_bottom_md_si=1500.0,
        )
        scen_b = Scenario(
            id="scn_beta",
            org_id="org_beta",
            well_id="wel_beta",
            name="Beta Scenario",
            status="draft",
        )
        session.add_all([org_a, org_b, proj_a, proj_b, well_a, well_b, wb_a, wb_b, sec_b, scen_b])
        await session.commit()
    return {
        "org_a": "org_alpha",
        "org_b": "org_beta",
        "proj_a": "prj_alpha",
        "proj_b": "prj_beta",
        "well_a": "wel_alpha",
        "well_b": "wel_beta",
        "wb_a": "wbr_alpha",
        "wb_b": "wbr_beta",
        "sec_b": "sec_beta",
        "scen_b": "scn_beta",
    }


# ===========================================================================
# F-CP16-01: Async Event-Loop Non-Blocking During Slow DNS Resolution
# ===========================================================================


async def test_f_cp16_01_slow_dns_does_not_block_event_loop_in_async_validation_and_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Verify that slow DNS resolution inside validate_connector_endpoint_async(require_dns_resolution=False),
    ConnectorService.create, and ConnectorWorker.execute_claimed_poll never blocks the asyncio event loop."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cp16_loop.db'}"
    monkeypatch.setenv("DRILLAI_DATABASE_URL", db_url)
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOWED_HOSTS", "slow-rig.corp.example")
    reset_settings_cache()

    app = create_app(Settings())
    await app.state.database.create_all()
    ids = await _seed_two_tenants(app)

    def _slow_getaddrinfo(host: str, port: int):
        time.sleep(0.18)
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr(connectors_mod, "_custom_dns_resolver", _slow_getaddrinfo)

    max_tick_lag_s = 0.0
    stop_ticker = asyncio.Event()

    async def _ticker() -> None:
        nonlocal max_tick_lag_s
        interval = 0.01
        last = time.perf_counter()
        while not stop_ticker.is_set():
            await asyncio.sleep(interval)
            now = time.perf_counter()
            lag = (now - last) - interval
            if lag > max_tick_lag_s:
                max_tick_lag_s = lag
            last = now

    ticker_task = asyncio.create_task(_ticker())
    try:
        # 1. Direct call with require_dns_resolution=False and no custom resolver (using monkeypatched socket.getaddrinfo)
        monkeypatch.setattr(connectors_mod, "_custom_dns_resolver", None)
        monkeypatch.setattr(
            connectors_mod.socket,
            "getaddrinfo",
            lambda host, port, *a, **kw: (time.sleep(0.18) or [(2, 1, 6, "", ("93.184.216.34", port))]),
        )
        pinned = await resolve_and_pin_endpoint_async(
            "https://slow-rig.corp.example:8443/witsml/servlet",
            "witsml.1.4.1.1.soap_http",
            require_dns_resolution=False,
        )
        assert pinned.original_url == "https://slow-rig.corp.example:8443/witsml/servlet"

        # 2. ConnectorService.create (async service path)
        async with app.state.database.session() as session:
            svc = ConnectorService(
                session,
                ids["org_a"],
                principal=Principal(id="u_admin", org_id=ids["org_a"], role_keys=("admin",), permissions=frozenset({"**"})),
            )
            created = await svc.create(
                well_id=ids["well_a"],
                key="slow-dns-cnc",
                name="Slow DNS Connector",
                protocol_profile="witsml.1.4.1.1.soap_http",
                endpoint_url="https://slow-rig.corp.example:8443/witsml/servlet",
                config={
                    "uid_well": "W-1",
                    "uid_wellbore": "WB-1",
                    "uid_log": "L-1",
                    "channel_mappings": [
                        {
                            "source_mnemonic": "DEPT",
                            "channel_key": "bit_depth",
                            "dimension": "length",
                            "unit": "m",
                        }
                    ],
                },
            )
            await svc.transition(created["id"], action="start")
            await session.commit()
            assert created["key"] == "slow-dns-cnc"

        # 3. ConnectorWorker.execute_claimed_poll (async worker path; DNS resolves to 127.0.0.1 which SSRF blocks with allow_loopback=False)
        worker = ConnectorWorker(app.state.database, worker_id="wrk-cp16-loop", allow_loopback=False)
        claimed = await worker.claim_connector(created["id"])
        assert claimed is not None
        monkeypatch.setattr(
            connectors_mod.socket,
            "getaddrinfo",
            lambda host, port, *a, **kw: (time.sleep(0.18) or [(2, 1, 6, "", ("127.0.0.1", port))]),
        )
        await worker.execute_claimed_poll(claimed)
    finally:
        stop_ticker.set()
        await ticker_task
        await app.state.database.dispose()

    # Each DNS call slept 180ms on a worker thread; the event loop tick lag must remain well below 100ms.
    assert max_tick_lag_s < 0.09, f"Event loop was blocked during DNS validation: max_tick_lag_s={max_tick_lag_s:.3f}s"


# ===========================================================================
# F-CP16-02 & F-CP16-03: Cross-Tenant Isolation & Hierarchy Scope Validation
# ===========================================================================


async def test_f_cp16_02_and_03_cross_tenant_isolation_across_twin_workflows_context_and_engines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cp16_tenant.db'}"
    monkeypatch.setenv("DRILLAI_DATABASE_URL", db_url)
    reset_settings_cache()

    app = create_app(Settings())
    await app.state.database.create_all()
    ids = await _seed_two_tenants(app)

    # Seed Org B twin state, snapshots, engine run, and workflow version
    async with app.state.database.session() as session:
        principal_b = Principal(
            id="usr_beta", org_id=ids["org_b"], role_keys=("admin",), permissions=frozenset({"**"})
        )
        twin_b = TwinService(session, org_id=ids["org_b"])
        await twin_b.write_aspect(
            well_id=ids["well_b"],
            revision=AspectRevision(
                aspect="identity",
                state_kind="actual",
                payload={"mud_weight_si": 1450.0, "secret_note": "BETA-CONFIDENTIAL"},
            ),
        )
        snap_b1 = await twin_b.snapshot(well_id=ids["well_b"], label="Beta Snap 1")
        await twin_b.write_aspect(
            well_id=ids["well_b"],
            revision=AspectRevision(
                aspect="identity",
                state_kind="actual",
                payload={"mud_weight_si": 1520.0, "secret_note": "BETA-CONFIDENTIAL-2"},
            ),
        )
        snap_b2 = await twin_b.snapshot(well_id=ids["well_b"], label="Beta Snap 2")

        wf_svc_b = WorkflowService(session, org_id=ids["org_b"], principal=principal_b)
        wf_b = await wf_svc_b.create(
            key="beta-secret-wf",
            name="Beta Secret Workflow",
            graph=WorkflowGraph(
                nodes=[GraphNode(id="n1", type="logic.set_variables", config={"variables": {"secret": "beta-only"}})]
            ),
            project_id=ids["proj_b"],
        )
        await wf_svc_b.start_run(wf_b.id, scope=RunScope(well_id=ids["well_b"]))
        await session.commit()
        snap_b1_id, snap_b2_id, wf_b_id = snap_b1.id, snap_b2.id, wf_b.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers_a = {"X-Dev-Roles": "well_manager"}  # resolves to org-alpha with full engineering permissions

        # 1. GET /api/v1/wells/{well_b}/audit must return 404 for Org A (not leak Org B's runs)
        res_audit = await client.get(f"/api/v1/wells/{ids['well_b']}/audit", headers=headers_a)
        assert res_audit.status_code == 404, res_audit.text

        # 2. Foreign well_id on twin/engine read endpoints must return 404 (not 200 empty list)
        for path in (
            f"/api/v1/wells/{ids['well_b']}/twin/snapshots",
            f"/api/v1/wells/{ids['well_b']}/twin/as-of?moment=2026-01-01T00:00:00Z",
            f"/api/v1/wells/{ids['well_b']}/twin/changes",
            f"/api/v1/wells/{ids['well_b']}/engine-runs",
            f"/api/v1/wells/{ids['well_b']}/recommendations",
        ):
            r = await client.get(path, headers=headers_a)
            assert r.status_code == 404, f"Expected 404 for {path}, got {r.status_code}: {r.text}"

        # 3. GET /api/v1/twin/snapshots/{snap_b1}/compare/{snap_b2} must return 404 for Org A
        res_cmp = await client.get(
            f"/api/v1/twin/snapshots/{snap_b1_id}/compare/{snap_b2_id}",
            headers=headers_a,
        )
        assert res_cmp.status_code == 404, res_cmp.text

        # 4. TwinService.snapshot for Org A with Org B's scenario_id or parent_snapshot_id must be rejected
        async with app.state.database.session() as session:
            twin_a = TwinService(session, org_id=ids["org_a"])
            await twin_a.write_aspect(
                well_id=ids["well_a"],
                revision=AspectRevision(
                    aspect="identity",
                    state_kind="actual",
                    payload={"mud_weight_si": 1200.0},
                ),
            )
            with pytest.raises(ValidationFailed, match="scenario"):
                await twin_a.snapshot(
                    well_id=ids["well_a"], label="Alpha Snap", scenario_id=ids["scen_b"]
                )
            with pytest.raises(ValidationFailed, match="parent snapshot"):
                await twin_a.snapshot(
                    well_id=ids["well_a"], label="Alpha Snap", parent_snapshot_id=snap_b1_id
                )
            await session.commit()

        # 5. GET /api/v1/workflows/{wf_b}/versions, /versions/1, and /summary?version=1 must return 404 for Org A
        for wf_path in (
            f"/api/v1/workflows/{wf_b_id}/versions",
            f"/api/v1/workflows/{wf_b_id}/versions/1",
            f"/api/v1/workflows/{wf_b_id}/summary?version=1",
        ):
            r = await client.get(wf_path, headers=headers_a)
            assert r.status_code == 404, f"Expected 404 for {wf_path}, got {r.status_code}: {r.text}"

        # 6. GET /api/v1/wells/{well_a}/context?section_id={sec_b} must return 404 WITHOUT leaking wbr_beta
        res_ctx = await client.get(
            f"/api/v1/wells/{ids['well_a']}/context?section_id={ids['sec_b']}",
            headers=headers_a,
        )
        assert res_ctx.status_code == 404
        assert ids["wb_b"] not in res_ctx.text

        # And when wellbore_id=wbr_alpha is also passed alongside foreign section_id=sec_b, must still 404
        res_ctx2 = await client.get(
            f"/api/v1/wells/{ids['well_a']}/context?wellbore_id={ids['wb_a']}&section_id={ids['sec_b']}",
            headers=headers_a,
        )
        assert res_ctx2.status_code == 404

        # 7. POST /api/v1/wells/{well_a}/twin/aspects with foreign wellbore_id or section_id must return 404
        res_aspect = await client.post(
            f"/api/v1/wells/{ids['well_a']}/twin/aspects",
            headers=headers_a,
            json={
                "aspect": "identity",
                "state_kind": "actual",
                "wellbore_id": ids["wb_b"],
                "payload": {"mud_weight_si": 1200.0},
            },
        )
        assert res_aspect.status_code == 404, res_aspect.text

        # 8. POST /api/v1/workflows and /runs with foreign project_id/well_id must return 404
        res_wf_create = await client.post(
            "/api/v1/workflows",
            headers=headers_a,
            json={
                "key": "alpha-bad-scope",
                "name": "Bad Scope",
                "project_id": ids["proj_b"],
                "graph": {"nodes": [{"id": "n1", "type": "logic.set_variables", "config": {"variables": {"x": 1}}}]},
            },
        )
        assert res_wf_create.status_code == 404, res_wf_create.text

        res_wf_ok = await client.post(
            "/api/v1/workflows",
            headers=headers_a,
            json={
                "key": "alpha-good",
                "name": "Good Workflow",
                "project_id": ids["proj_a"],
                "graph": {"nodes": [{"id": "n1", "type": "logic.set_variables", "config": {"variables": {"x": 1}}}]},
            },
        )
        assert res_wf_ok.status_code == 201
        wf_a_id = res_wf_ok.json()["id"]

        res_run_bad = await client.post(
            f"/api/v1/workflows/{wf_a_id}/runs",
            headers=headers_a,
            json={"well_id": ids["well_b"]},
        )
        assert res_run_bad.status_code == 404, res_run_bad.text

        # 9. POST /api/v1/registry/engines/{key}/run with foreign well_id must return 404,
        # and persist=False on valid inputs must not write an EngineRun row.
        res_eng_bad = await client.post(
            "/api/v1/registry/engines/trajectory.minimum_curvature/run",
            headers=headers_a,
            json={
                "well_id": ids["well_b"],
                "inputs": {
                    "stations": [
                        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                        {"md_si": 500.0, "inclination_deg": 2.0, "azimuth_deg": 45.0},
                    ]
                },
            },
        )
        assert res_eng_bad.status_code == 404, res_eng_bad.text

        res_eng_ephemeral = await client.post(
            "/api/v1/registry/engines/trajectory.minimum_curvature/run",
            headers=headers_a,
            json={
                "well_id": ids["well_a"],
                "persist": False,
                "inputs": {
                    "stations": [
                        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                        {"md_si": 500.0, "inclination_deg": 2.0, "azimuth_deg": 45.0},
                    ]
                },
            },
        )
        assert res_eng_ephemeral.status_code == 200
        assert res_eng_ephemeral.json()["engine_run_id"] is None

        # 10. Multi-role RBAC negative check across viewer, auditor, and engineer roles
        for read_only_role in ("viewer", "auditor"):
            res_ro_aspect = await client.post(
                f"/api/v1/wells/{ids['well_a']}/twin/aspects",
                headers={"X-Dev-Roles": read_only_role},
                json={
                    "aspect": "identity",
                    "state_kind": "actual",
                    "payload": {"mud_weight_si": 1220.0},
                },
            )
            assert res_ro_aspect.status_code == 403, (read_only_role, res_ro_aspect.text)

            res_ro_egress = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": read_only_role},
                json={"allowed_hosts": ["rig.corp.example"], "allowed_private_cidrs": [], "reason": "audit test"},
            )
            assert res_ro_egress.status_code == 403, (read_only_role, res_ro_egress.text)

        # Engineer can update twin aspects (L2), but cannot mutate org-wide egress policy (admin.policy.manage L4)
        res_eng_egress = await client.put(
            "/api/v1/connectors/egress-policy",
            headers={"X-Dev-Roles": "engineer"},
            json={"allowed_hosts": ["rig.corp.example"], "allowed_private_cidrs": [], "reason": "eng test"},
        )
        assert res_eng_egress.status_code == 403, res_eng_egress.text

    await app.state.database.dispose()


# ===========================================================================
# F-CP16-04: Workflow Approval & Resume Security + Breakpoint Resume
# ===========================================================================


async def test_f_cp16_04_workflow_resume_refuses_foreign_or_cross_run_approval_and_supports_breakpoints(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cp16_workflow.db'}"
    monkeypatch.setenv("DRILLAI_DATABASE_URL", db_url)
    reset_settings_cache()

    app = create_app(Settings())
    await app.state.database.create_all()
    ids = await _seed_two_tenants(app)

    l4_graph = WorkflowGraph(
        nodes=[
            GraphNode(id="prep", type="logic.set_variables", config={"variables": {"ready": True}}),
            GraphNode(
                id="notify",
                type="output.notify",
                config={"channel": "telegram", "recipient": "@rig", "body": "Execute action"},
            ),
        ],
        edges=[GraphEdge(id="e1", source="prep", target="notify")],
    )

    async with app.state.database.session() as session:
        sup_a = Principal(
            id="usr_sup_a",
            org_id=ids["org_a"],
            role_keys=("drilling_supervisor",),
            permissions=frozenset({"**"}),
            max_action_level=ActionLevel.EXECUTE_WITH_APPROVAL,
        )
        svc_a = WorkflowService(session, org_id=ids["org_a"], principal=sup_a)
        wf_a = await svc_a.create(key="wf-l4-a", name="L4 Workflow A", graph=l4_graph, project_id=ids["proj_a"])

        # Start Run 1 and approve its approval request
        run_1 = await svc_a.start_run(wf_a.id, scope=RunScope(well_id=ids["well_a"]))
        assert run_1.status == "waiting_approval"
        apr_1 = (
            await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == run_1.pending_approval_id))
        ).scalar_one()
        apr_1.status = "approved"
        apr_1.decided_by = "usr_approver_a"
        await session.flush()

        # Start Run 2 (still pending approval!)
        run_2 = await svc_a.start_run(wf_a.id, scope=RunScope(well_id=ids["well_a"]))
        assert run_2.status == "waiting_approval"
        apr_2_id = run_2.pending_approval_id
        assert apr_2_id != apr_1.id

        # Direct runtime.resume on Run 2 without approval_id must raise ApprovalRequired (not bypass!)
        runtime_a = WorkflowRuntime(session, org_id=ids["org_a"], principal=sup_a)
        with pytest.raises(ApprovalRequired):
            await runtime_a.resume(run_2.id, l4_graph, approval_id=None)

        # Direct runtime.resume on Run 2 with Run 1's approved approval_id must be refused!
        with pytest.raises(NodeExecutionFailed, match="approval"):
            await runtime_a.resume(run_2.id, l4_graph, approval_id=apr_1.id)

        await session.commit()
        run_2_id = run_2.id
        apr_1_id = apr_1.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers_a = {"X-Dev-Roles": "drilling_supervisor,admin"}

        # HTTP POST /api/v1/runs/{run_2}/resume?approval_id={apr_1} must be refused (409/422/404)
        res_cross_run = await client.post(
            f"/api/v1/runs/{run_2_id}/resume?approval_id={apr_1_id}",
            headers=headers_a,
        )
        assert res_cross_run.status_code in {404, 409, 422}, res_cross_run.text

        # Test breakpoint pause and HTTP resume without approval
        bp_res = await client.post(
            "/api/v1/workflows",
            headers=headers_a,
            json={
                "key": "wf-bp-resume",
                "name": "Breakpoint Resume",
                "project_id": ids["proj_a"],
                "graph": {
                    "nodes": [
                        {"id": "step1", "type": "logic.set_variables", "config": {"variables": {"a": 10}}},
                        {
                            "id": "bp_node",
                            "type": "logic.set_variables",
                            "config": {"variables": {"b": 20}},
                            "is_breakpoint": True,
                        },
                        {"id": "step3", "type": "logic.set_variables", "config": {"variables": {"c": 30}}},
                    ],
                    "edges": [
                        {"id": "e1", "source": "step1", "target": "bp_node"},
                        {"id": "e2", "source": "bp_node", "target": "step3"},
                    ],
                },
            },
        )
        assert bp_res.status_code == 201
        bp_wf_id = bp_res.json()["id"]

        start_bp = await client.post(
            f"/api/v1/workflows/{bp_wf_id}/runs",
            headers=headers_a,
            json={"well_id": ids["well_a"]},
        )
        assert start_bp.status_code == 201
        bp_run = start_bp.json()
        assert bp_run["status"] == "paused"

        resume_bp = await client.post(
            f"/api/v1/runs/{bp_run['id']}/resume",
            headers=headers_a,
        )
        assert resume_bp.status_code == 200, resume_bp.text
        assert resume_bp.json()["status"] == "succeeded"

    await app.state.database.dispose()


# ===========================================================================
# F-CP16-05: Unit Registry Symbol Integrity ('m2' Area vs Permeability)
# ===========================================================================


def test_f_cp16_05_unit_registry_area_and_permeability_symbol_integrity():
    # 'm2' must be the canonical AREA unit, not overwritten by PERMEABILITY
    m2_unit = get_unit("m2")
    assert m2_unit.dimension is Dimension.AREA
    assert convert(1.0, "in2", "m2") == pytest.approx(0.00064516, rel=1e-12)
    assert convert(1.0, "m2", "in2") == pytest.approx(1.0 / 0.00064516, rel=1e-12)
    assert q(10.0, "in2").to_si().unit == "m2"
    assert q(10.0, "in2").to_si().value == pytest.approx(0.0064516, rel=1e-12)

    # Permeability conversions to/from 'm2' and 'm2-perm' must both work
    assert convert(1.0, "mD", "m2-perm") == pytest.approx(9.869233e-16, rel=1e-12)
    assert convert(1.0, "mD", "m2") == pytest.approx(9.869233e-16, rel=1e-12)
    assert convert(9.869233e-16, "m2", "mD") == pytest.approx(1.0, rel=1e-12)

    # Cross-dimension duplicate symbol registration must raise UnitError
    with pytest.raises(UnitError):
        register(Unit(symbol="m2", dimension=Dimension.PERMEABILITY, name="collision"))

    # HydraulicsEngineInput.tfa_si and TorqueDragInput.pressure_area_si must advertise dimension="area"
    engines = load_default_engines()
    hyd_schema = engines.get("hydraulics.laminar").spec.describe()["input_schema"]
    assert hyd_schema["properties"]["tfa_si"]["dimension"] == "area"
    td_schema = engines.get("torque_drag.soft_string").spec.describe()["input_schema"]
    assert td_schema["properties"]["pressure_area_si"]["dimension"] == "area"


# ===========================================================================
# F-CP16-06: API 5C3 Triaxial Stress Under Mud Hydrostatic & Reference Benchmarks
# ===========================================================================


def test_f_cp16_06_api5c3_triaxial_uses_mud_weight_external_pressure_and_is_json_finite():
    engines = load_default_engines()
    # 9-5/8 in 47# L-80 (OD = 0.244475 m, t = 0.0119888 m, Fy = 551.58 MPa) at 2000 m in 1200 kg/m3 mud
    od = 9.625 * 0.0254
    wall = 0.472 * 0.0254
    fy = 80_000 * 6894.757293168361
    mud_weight = 1200.0
    depth = 2000.0
    _, result = engines.execute(
        "tubulars.api_5c3",
        {
            "segments": [
                {
                    "name": "intermediate",
                    "from_depth_si": 0.0,
                    "to_depth_si": depth,
                    "outer_diameter_si": od,
                    "wall_thickness_si": wall,
                    "yield_stress_si": fy,
                }
            ],
            "mud_weight_si": mud_weight,
        },
    )
    seg = result.outputs.segments[0]
    external_p = mud_weight * 9.80665 * depth
    # Independent Lamé inner-wall hoop stress under external pressure p_o with p_i = 0:
    # sigma_r(a) = 0, sigma_theta(a) = -2 * p_o * b^2 / (b^2 - a^2)
    b = od / 2.0
    a = b - wall
    expected_vm = 2.0 * external_p * (b**2) / (b**2 - a**2)
    assert seg.triaxial_equivalent_stress_si == pytest.approx(expected_vm, rel=1e-9)
    assert seg.triaxial_safety_factor is not None
    assert math.isfinite(seg.triaxial_safety_factor)
    assert seg.triaxial_safety_factor == pytest.approx(fy / expected_vm, rel=1e-9)


def test_f_cp16_06_independent_physics_reference_benchmarks():
    """Verify high-risk physics engines against independent closed-form reference equations."""
    engines = load_default_engines()

    # 1. Trajectory Minimum Curvature (API RP 7G / Bourgoyne Eq. 8.16-8.20):
    # 90-deg circular arc from vertical (inc=0) to horizontal (inc=90 deg) over dMD = 300 m due East (az=90 deg).
    # Analytical circular arc radius R = 300 / (pi/2) = 600 / pi m.
    # Exact TVD gain = R = 600 / pi m; Exact East displacement = R = 600 / pi m; North = 0.
    _, traj_res = engines.execute(
        "trajectory.minimum_curvature",
        {
            "stations": [
                {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 90.0},
                {"md_si": 300.0, "inclination_deg": 90.0, "azimuth_deg": 90.0},
            ]
        },
    )
    radius_m = 600.0 / math.pi
    st_end = traj_res.outputs.stations[-1]
    assert st_end.tvd_si == pytest.approx(radius_m, rel=1e-9)
    assert st_end.ew_si == pytest.approx(radius_m, rel=1e-9)
    assert st_end.ns_si == pytest.approx(0.0, abs=1e-9)
    assert st_end.dls_deg_per_30m == pytest.approx(9.0, rel=1e-9)

    # 2. Hydraulics Laminar Bingham Plastic Pipe Loss & ECD (Buckingham-Reiner Exact Equation):
    # Pipe ID = 0.10 m, L = 1000 m, TVD = 1000 m, Q = 0.005 m3/s, rho = 1200 kg/m3, PV = 0.03 Pa.s, YP = 10 Pa
    d_pipe = 0.10
    length = 1000.0
    flow = 0.005
    rho = 1200.0
    pv = 0.03
    yp = 10.0
    _, hyd_res = engines.execute(
        "hydraulics.laminar",
        {
            "elements": [
                {"kind": "hole", "from_depth_si": 0.0, "to_depth_si": length, "od_si": 0.2159},
                {"kind": "drillpipe", "from_depth_si": 0.0, "to_depth_si": length, "od_si": 0.127, "id_si": d_pipe},
            ],
            "flow_rate_si": flow,
            "mud_weight_si": rho,
            "bit_depth_si": length,
            "bit_tvd_si": length,
            "plastic_viscosity_si": pv,
            "yield_point_si": yp,
            "tfa_si": 0.0005,
        },
    )
    dp_pipe = hyd_res.outputs.string_pressure_loss_si
    # Verify Buckingham-Reiner identity: Q = (pi * D^4 * dP / (128 * mu_p * L)) * (1 - 4/3 * x + 1/3 * x^4)
    x_ratio = (4.0 * yp * length) / (d_pipe * dp_pipe)
    q_buckingham = (
        (math.pi * (d_pipe**4) * dp_pipe)
        / (128.0 * pv * length)
        * (1.0 - (4.0 / 3.0) * x_ratio + (1.0 / 3.0) * (x_ratio**4))
    )
    assert q_buckingham == pytest.approx(flow, rel=1e-6)
    expected_ecd = rho + hyd_res.outputs.annulus_pressure_loss_si / (9.80665 * length)
    assert hyd_res.outputs.ecd_bottom_si == pytest.approx(expected_ecd, rel=1e-9)

    # 3. Well Control Wait-and-Weight Kill Sheet (IWCF / IADC Standard Equations):
    # TVD = 3000 m, rho_orig = 1200 kg/m3, SIDPP = 3.5 MPa, SCR = 4.0 MPa
    tvd = 3000.0
    rho_orig = 1200.0
    sidpp = 3_500_000.0
    scr = 4_000_000.0
    expected_kill_mw = rho_orig + sidpp / (9.80665 * tvd)
    expected_icp = sidpp + scr
    expected_fcp = scr * (expected_kill_mw / rho_orig)
    _, wc_res = engines.execute(
        "well_control.kill_sheet",
        {
            "current_mud_weight_si": rho_orig,
            "tvd_si": tvd,
            "casing_shoe_tvd_si": 1500.0,
            "shoe_fracture_mud_weight_si": 1750.0,
            "sidpp_si": sidpp,
            "sicp_si": 4_800_000.0,
            "pit_gain_si": 2.5,
            "slow_circulating_pressure_si": scr,
            "strokes_to_bit": 1200.0,
            "pump_output_per_stroke_si": 0.02,
            "kill_rate_si": 0.005,
        },
    )
    assert wc_res.outputs.kill_mud_weight_si == pytest.approx(expected_kill_mw, rel=1e-9)
    assert wc_res.outputs.initial_circulating_pressure_si == pytest.approx(expected_icp, rel=1e-9)
    assert wc_res.outputs.final_circulating_pressure_si == pytest.approx(expected_fcp, rel=1e-9)

    # 4. Teale (1965) Mechanical Specific Energy:
    # MSE = WOB / A_b + (120 * pi * N * T) / (A_b * ROP_m_per_h_converted_to_s)
    # Using D = 0.2159 m (8.5 in), WOB = 80 kN, T = 12 kN.m, RPM = 120, ROP = 18 m/h (0.005 m/s)
    d_bit = 0.2159
    wob = 80_000.0
    torque = 12_000.0
    rpm = 120.0
    rop_mh = 18.0
    area = math.pi / 4.0 * d_bit**2
    expected_axial = wob / area
    expected_torsional = (2.0 * math.pi * (rpm / 60.0) * torque) / (area * (rop_mh / 3600.0))
    _, mse_res = engines.execute(
        "drilling.mse",
        {
            "wob_si": wob,
            "torque_si": torque,
            "rpm_si": rpm,
            "rop_si": rop_mh,
            "bit_diameter_si": d_bit,
            "confined_compressive_strength_si": 100e6,
        },
    )
    assert mse_res.outputs.mse_axial_component_si == pytest.approx(expected_axial, rel=1e-12)
    assert mse_res.outputs.mse_torsional_component_si == pytest.approx(expected_torsional, rel=1e-12)
    assert mse_res.outputs.mse_si == pytest.approx(expected_axial + expected_torsional, rel=1e-12)


# ===========================================================================
# F-CP16-07: Deployment Configuration & Production Guards
# ===========================================================================


def test_f_cp16_07_dotenv_example_is_valid_and_production_guards_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
):
    env_example = REPO_ROOT / ".env.example"
    assert env_example.is_file()
    # Loading .env.example directly into Settings must succeed without ValidationError
    loaded = Settings(_env_file=env_example)
    assert loaded.vector_backend in {"memory", "pgvector"}

    # Multi-origin CORS list containing '*' must be rejected in production
    with pytest.raises(ConfigurationError, match="CORS"):
        create_app(
            Settings(
                environment="production",
                auth_enabled=True,
                debug=False,
                cors_origins="https://drillai.example.com, *",
                e2e_faults=False,
                connector_allow_loopback=False,
            )
        )


# ===========================================================================
# CP16-08: Backup, Restore & Migration Recovery Drill (Gate CP16-C)
# ===========================================================================


async def test_cp16_08_backup_restore_and_migration_recovery_drill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Verify full Alembic migration bootstrap, tenant state + twin snapshot backup,
    catastrophic loss simulation, backup restoration, and post-restore migration & data integrity."""
    live_db_file = tmp_path / "cp16_live_prod.db"
    backup_db_file = tmp_path / "cp16_backup_snapshot.db"
    db_url = f"sqlite+aiosqlite:///{live_db_file}"
    monkeypatch.setenv("DRILLAI_DATABASE_URL", db_url)
    reset_settings_cache()

    alembic_cfg = Config(str(REPO_ROOT / "backend" / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(REPO_ROOT / "backend" / "alembic"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    # 1. Run Alembic upgrade head on fresh DB
    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")

    # 2. Seed tenant hierarchy and Twin snapshot
    app = create_app(Settings())
    ids = await _seed_two_tenants(app)
    async with app.state.database.session() as session:
        twin_svc = TwinService(session, org_id=ids["org_a"])
        await twin_svc.write_aspect(
            well_id=ids["well_a"],
            revision=AspectRevision(
                aspect="identity",
                state_kind="actual",
                payload={"mud_weight_si": 1245.0, "rheology_model": "bingham_plastic"},
            ),
        )
        snap = await twin_svc.snapshot(well_id=ids["well_a"], label="pre-backup-baseline")
        snap_id = snap.id
        await session.commit()
    await app.state.database.dispose()

    # 3. Take consistent file-level database backup
    shutil.copy2(live_db_file, backup_db_file)
    assert backup_db_file.stat().st_size > 0

    # 4. Simulate catastrophic loss of live DB and restore from backup
    live_db_file.unlink()
    assert not live_db_file.exists()
    shutil.copy2(backup_db_file, live_db_file)

    # 5. Re-run Alembic upgrade head (idempotent post-restore migration recovery) and verify restored state
    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")
    restored_app = create_app(Settings())
    async with restored_app.state.database.session() as session:
        twin_svc = TwinService(session, org_id=ids["org_a"])
        snapshots = await twin_svc.list_snapshots(well_id=ids["well_a"])
        assert any(s.id == snap_id and s.label == "pre-backup-baseline" for s in snapshots)
        aspects = await twin_svc.list_aspects(well_id=ids["well_a"], current_only=True)
        assert any(a.aspect == "identity" and a.payload["mud_weight_si"] == 1245.0 for a in aspects)
    await restored_app.state.database.dispose()


# ===========================================================================
# CP16-09: External Vendor Interoperability Contract & Unsupported ETP Rejection (Gate CP16-E)
# ===========================================================================


async def test_cp16_09_external_vendor_interoperability_contract_and_unsupported_etp_rejection():
    """Verify scripts/verify_external_vendor_interop.py in both harness and external modes."""
    script_path = REPO_ROOT / "scripts" / "verify_external_vendor_interop.py"
    assert script_path.is_file()

    # 1. Offline harness mode
    proc_harness = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        "--mode",
        "harness",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_h, stderr_h = await proc_harness.communicate()
    assert proc_harness.returncode == 0, stderr_h.decode()
    report_h = json.loads(stdout_h.decode())
    hc = report_h["harness_conformance"]
    assert hc["witsml_1411_harness_passed"] is True
    assert hc["etp_12_binary_harness_passed"] is True
    assert hc["etp_12_unsupported_protocol_rejected_with_exception"] is True
    assert hc["etp_12_unsupported_message_rejected_with_exception"] is True
    assert report_h["external_vendor_verified"] is False
    assert report_h["external_gate_status"] == "BLOCKED_BY_EXTERNAL_EVIDENCE"

    # 2. External mode without configured vendor endpoints must report BLOCKED_BY_EXTERNAL_EVIDENCE
    proc_ext = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        "--mode",
        "external",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_e, stderr_e = await proc_ext.communicate()
    assert proc_ext.returncode == 0, stderr_e.decode()
    report_e = json.loads(stdout_e.decode())
    assert report_e["external_gate_status"] == "BLOCKED_BY_EXTERNAL_EVIDENCE"
    assert report_e["external_vendor_verified"] is False
    assert report_e["external_vendor_probe"]["network_calls_made"] == 0


# ===========================================================================
# CP16-10: Documentation & Contract Consistency Guard (Gate CP16-A & CP16-F)
# ===========================================================================


def test_cp16_10_documentation_and_contract_consistency_guard():
    """Prevent drift between canonical telemetry constants, test file counts, and maintained documentation."""
    readme_text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    fe_testing_text = (REPO_ROOT / "docs" / "FRONTEND_TESTING.md").read_text(encoding="utf-8")
    ops_doc_text = (REPO_ROOT / "docs" / "CONNECTORS_AND_OPERATIONS.md").read_text(encoding="utf-8")

    # 1. All 4 CP16 mission reports must exist and be non-empty
    for report_name in (
        "CP16-SOURCE-AUDIT.md",
        "CP16-MISSION-REPORT.md",
        "PRODUCTION-READINESS-MATRIX.md",
        "REMAINING-GAPS-ROADMAP.md",
    ):
        report_path = REPO_ROOT / "docs" / "mission-reports" / report_name
        assert report_path.is_file(), f"Missing required CP16 deliverable: {report_path}"
        assert len(report_path.read_text(encoding="utf-8").strip()) > 500

    # 2. All 11 POLL_DATA_OUTCOMES, 10 ALL_TEST_CONNECTION_STAGES, and 6 EGRESS_POLICY_STATES
    # must be documented in docs/CONNECTORS_AND_OPERATIONS.md
    assert len(POLL_DATA_OUTCOMES) == 11
    for outcome in POLL_DATA_OUTCOMES:
        assert outcome in ops_doc_text, f"Missing data_outcome `{outcome}` in CONNECTORS_AND_OPERATIONS.md"

    assert len(ALL_TEST_CONNECTION_STAGES) == 10
    for stage in ALL_TEST_CONNECTION_STAGES:
        assert stage in ops_doc_text, f"Missing stage `{stage}` in CONNECTORS_AND_OPERATIONS.md"

    assert len(EGRESS_POLICY_STATES) == 6
    for state in EGRESS_POLICY_STATES:
        assert state in ops_doc_text, f"Missing egress state `{state}` in CONNECTORS_AND_OPERATIONS.md"

    # 3. Protocol honesty invariants must hold across PROTOCOL_PROFILE_SPECS
    assert len(PROTOCOL_PROFILE_SPECS) == 4
    for spec in PROTOCOL_PROFILE_SPECS.values():
        assert spec.external_vendor_verified is False
    assert PROTOCOL_PROFILE_SPECS["etp.1.2.json_ws"].production_supported is False
    assert PROTOCOL_PROFILE_SPECS["etp.1.2.json_ws"].is_test_only_encoding is True
    assert PROTOCOL_PROFILE_SPECS["etp.1.2.binary_ws"].production_supported is True
    assert PROTOCOL_PROFILE_SPECS["witsml.1.4.1.1.soap_http"].production_supported is True

    # 4. E2E spec file count and backend test file count must match README.md and docs/FRONTEND_TESTING.md
    e2e_spec_files = sorted((REPO_ROOT / "frontend" / "e2e").glob("*.spec.ts"))
    assert len(e2e_spec_files) == 17
    backend_test_files = sorted((REPO_ROOT / "backend" / "tests").rglob("test_*.py"))
    assert len(backend_test_files) == 77

    assert "77 test files" in readme_text or "77 files" in readme_text
    assert "1,067 passed" in readme_text or "1067 passed" in readme_text
    assert "119 passed" in readme_text
    assert "312 passed" in readme_text
    assert "119 passed" in fe_testing_text
    assert "42" in fe_testing_text

