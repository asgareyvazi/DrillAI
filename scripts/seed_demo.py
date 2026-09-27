#!/usr/bin/env python
"""Seed a local development database with a demonstrable drilling scenario.

This script is a *development fixture*, not production code, and it says so in the data it writes:
the well is named accordingly and the sample report is the clearly labelled synthetic DDR from
``tests/fixtures`` (redeployed as a product sample under ``samples/ddr``).

It deliberately drives the **HTTP API** rather than writing rows directly. That means everything the
UI will later read is produced by the same endpoints the UI calls, through the same authorization,
evidence and audit paths — there is no second, privileged way into the database that could drift
from the real one.

Usage (from the repository root)::

    backend/.venv/bin/python scripts/seed_demo.py --reset
    backend/.venv/bin/python scripts/seed_demo.py --database-url sqlite+aiosqlite:///./.data/demo.db

The script prints the IDs it created so a developer can paste them into the UI or curl them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# The synthetic fixture itself carries the SYNTHETIC label; it is imported rather than copied so
# there is exactly one definition of the sample DDR in the repository.
sys.path.insert(0, str(BACKEND_ROOT / "tests"))


def _sample_csv() -> bytes:
    from fixtures.synthetic_ddr import SYNTHETIC_DDR_CSV

    return SYNTHETIC_DDR_CSV


def _sample_text() -> bytes:
    from fixtures.synthetic_ddr import SYNTHETIC_DDR_TEXT

    return SYNTHETIC_DDR_TEXT


class SeedError(RuntimeError):
    """A step of the seed failed; the HTTP status and payload are included verbatim."""


class Api:
    """Minimal async HTTP client with the development identity header."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        expect: tuple[int, ...] = (200, 201),
        **kwargs: Any,
    ) -> dict[str, Any]:
        response = await self._client.request(method, path, json=json_body, **kwargs)
        if response.status_code not in expect:
            raise SeedError(
                f"{method} {path} -> {response.status_code}\n{response.text[:2000]}"
            )
        if not response.content:
            return {}
        return response.json()

    async def upload(self, path: str, *, files: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(path, files=files, data=data)
        if response.status_code not in (200, 201):
            raise SeedError(f"POST {path} -> {response.status_code}\n{response.text[:2000]}")
        return response.json()



# --------------------------------------------------------------------------- workflow fixtures
#
# The demo workflow is the product's default "Daily Drilling Intelligence" path, expressed with the
# platform's own node types: load the engineering context, read the well state, compute NPT, stop for
# a human decision, then publish a report. The approval node is deliberately at L3 so the run cannot
# complete without a recorded human decision — that is the governance contract, not a demo trick.
APPROVAL_WORKFLOW_NODES = [
    {
        "id": "load_context",
        "type": "data.load_context",
        "name": "Load engineering context",
        "config": {"purpose": "workflow", "include_evidence": True},
    },
    {
        "id": "well_state",
        "type": "drilling.well_state",
        "name": "Well drilling state",
        "config": {"include_missing": True},
    },
    {
        "id": "npt_summary",
        "type": "drilling.npt_summary",
        "name": "NPT summary and Pareto",
        "config": {"basis": "events", "include_offsets": True},
    },
    {
        "id": "approve_plan",
        "type": "human.approval",
        "name": "Approve daily plan",
        "config": {
            "title": "Approve the daily drilling intelligence summary",
            "description": "Review the computed state and NPT attribution before the report is issued.",
            "risk_notes": ["Report is issued to the rig site once approved."],
        },
    },
    {
        "id": "daily_report",
        "type": "output.report",
        "name": "Daily drilling report",
        "config": {
            "title": "Daily drilling intelligence summary",
            "include_context_summary": True,
            "sections": {
                "Summary": "Well state and NPT produced by the platform's own engines.",
            },
        },
    },
]

APPROVAL_WORKFLOW_EDGES = [
    {"id": "e1", "source": "load_context", "target": "well_state"},
    {"id": "e2", "source": "well_state", "target": "npt_summary"},
    {"id": "e3", "source": "npt_summary", "target": "approve_plan"},
    {"id": "e4", "source": "approve_plan", "target": "daily_report"},
]


def _node_positions(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give each node editor coordinates so the graph opens laid out rather than stacked."""
    return [
        {**node, "position": {"x": 80 + index * 240, "y": 120}}
        for index, node in enumerate(nodes)
    ]


def failing_workflow_graph() -> dict[str, Any]:
    """A workflow whose second node fails deterministically: a registered engine with no inputs.

    This exists so the failure path is exercised by the automated suite with a real backend
    rejection rather than a simulated one.
    """
    return {
        "nodes": _node_positions(
            [
                {
                    "id": "load_context",
                    "type": "data.load_context",
                    "name": "Load engineering context",
                    "config": {"purpose": "workflow"},
                },
                {
                    "id": "invalid_engine_call",
                    "type": "engineering.run_engine",
                    "name": "Hydraulics with no inputs (expected to fail)",
                    "config": {"engine_key": "hydraulics.laminar", "inputs": {}},
                },
            ]
        ),
        "edges": [{"id": "e1", "source": "load_context", "target": "invalid_engine_call"}],
        "variables": {},
    }


async def _seed_workflows(api: Api, well_id: str) -> dict[str, Any]:
    """Create, validate, publish the demo workflows. Returns their identifiers."""
    approval_graph = {
        "nodes": _node_positions(APPROVAL_WORKFLOW_NODES),
        "edges": APPROVAL_WORKFLOW_EDGES,
        "variables": {"well_id": well_id},
        "description": "Load context, compute well state and NPT, require a human decision, report.",
    }

    validation = await api.call("POST", "/workflows/validate", json_body=approval_graph)
    if not validation["validation"]["is_valid"]:
        raise SeedError(f"the demo workflow graph does not validate: {validation['validation']}")

    workflow = await api.call(
        "POST",
        "/workflows",
        json_body={
            "key": "daily-drilling-intelligence",
            "name": "Daily Drilling Intelligence",
            "description": (
                "The product's default daily loop: engineering context, well state, NPT Pareto, "
                "human approval, report."
            ),
            "category": "drilling",
            "tags": ["drilling", "daily", "demo"],
        },
    )
    saved = await api.call(
        "PUT",
        f"/workflows/{workflow['id']}/graph",
        json_body={
            "graph": approval_graph,
            "notes": "seeded demo definition",
            "change_reason": "initial definition",
        },
    )
    published = await api.call(
        "POST",
        f"/workflows/{workflow['id']}/publish",
        json_body={},
        expect=(200, 201, 202, 204),
    )

    failing = await api.call(
        "POST",
        "/workflows",
        json_body={
            "key": "failing-node-demo",
            "name": "Failing node (demo)",
            "description": "Exercises the failure path: an engine is called without its required inputs.",
            "category": "drilling",
            "tags": ["drilling", "demo", "failure"],
        },
    )
    await api.call(
        "PUT",
        f"/workflows/{failing['id']}/graph",
        json_body={"graph": failing_workflow_graph(), "change_reason": "seeded failing definition"},
    )

    return {
        "workflow_id": workflow["id"],
        "workflow_key": workflow["key"],
        "workflow_version": saved.get("version"),
        "published": published,
        "failing_workflow_id": failing["id"],
    }


async def seed(database_url: str, *, reset: bool, blob_dir: pathlib.Path) -> dict[str, Any]:
    os.environ["DRILLAI_ENVIRONMENT"] = "development"
    os.environ["DRILLAI_AUTH_ENABLED"] = "false"
    os.environ["DRILLAI_DATABASE_URL"] = database_url
    os.environ["DRILLAI_BLOB_BACKEND"] = "filesystem"
    os.environ["DRILLAI_BLOB_ROOT"] = str(blob_dir.resolve())
    os.environ["DRILLAI_SCHEDULER_ENABLED"] = "false"
    os.environ["DRILLAI_LOG_JSON"] = "false"

    from drillai.core.config import get_settings, reset_settings_cache

    reset_settings_cache()
    settings = get_settings()

    from drillai.api.app import _warm_registries, create_app
    from drillai.db.models import Base

    application = create_app(settings)
    engine = application.state.database.engine
    if reset:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    application.state.catalogues = _warm_registries()

    import httpx

    summary: dict[str, Any] = {"database_url": database_url}
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://seed.local/api/v1",
        # The demo seeding principal acts as the engineering supervisor: publishing a workflow and
        # approving a plan are exactly the supervised actions the seeded data demonstrates, and the
        # server still evaluates every request against this principal's permissions.
        headers={"X-Dev-Roles": "drilling_supervisor,admin"},
        timeout=120.0,
    ) as http:
        try:
            api = Api(http)
            existing = await api.call("GET", "/wells")
            if existing.get("total") and not reset:
                summary["skipped"] = "database already contains wells; pass --reset to rebuild"
                return summary

            project = (
                await api.call(
                    "POST",
                    "/projects",
                    json_body={
                        "name": "Synthetic Development Project",
                        "code": "SYN-DEV",
                        "operator": "DrillAI (synthetic)",
                        "country": "NO",
                        "phase": "drilling",
                        "description": "Development fixture created by scripts/seed_demo.py",
                    },
                )
            )
            well = (
                await api.call(
                    "POST",
                    "/wells",
                    json_body={
                        "project_id": project["id"],
                        "name": "SYNTH-DEMO-01 (synthetic data)",
                        "well_type": "development",
                        "operator": "DrillAI (synthetic)",
                        "total_depth_planned_si": 3200.0,
                        "objectives": "Exercise the DDR → state → NPT → engines → recommendation chain.",
                        "tags": ["synthetic", "demo"],
                    },
                )
            )
            wellbore = (
                await api.call(
                    "POST",
                    f"/wells/{well['id']}/wellbores",
                    json_body={
                        "name": "Main bore",
                        "purpose": "production",
                        "sequence": 1,
                        "planned_td_md_si": 3200.0,
                        "planned_td_tvd_si": 2980.0,
                    },
                )
            )
            drilling_section = (
                await api.call(
                    "POST",
                    f"/wellbores/{wellbore['id']}/sections",
                    json_body={
                        "sequence": 3,
                        "name": '8-1/2" section',
                        "kind": "production",
                        "hole_diameter_nominal": '8-1/2"',
                        "hole_diameter_si": 0.2159,
                        "planned_top_md_si": 2400.0,
                        "planned_bottom_md_si": 3200.0,
                        "is_planned_only": False,
                    },
                )
            )

            upload = await api.upload(
                "/documents",
                files={"file": ("synthetic_ddr_2026-03-15.csv", _sample_csv(), "text/csv")},
                data={
                    "well_id": well["id"],
                    "wellbore_id": wellbore["id"],
                    "section_id": drilling_section["id"],
                    "doc_type": "ddr",
                    "title": "Daily Drilling Report 2026-03-15 (SYNTHETIC)",
                },
            )
            document = upload.get("document", upload)
            processing = await api.call("POST", f"/documents/{document['id']}/process", json_body={})

            second = await api.upload(
                "/documents",
                files={"file": ("synthetic_ddr_2026-03-16.txt", _sample_text(), "text/plain")},
                data={
                    "well_id": well["id"],
                    "wellbore_id": wellbore["id"],
                    "section_id": drilling_section["id"],
                    "doc_type": "ddr",
                    "title": "Daily Drilling Report 2026-03-16 (SYNTHETIC, narrative)",
                },
            )
            second_document = second.get("document", second)
            second_processing = await api.call(
                "POST", f"/documents/{second_document['id']}/process", json_body={}
            )

            hydraulics = await api.call(
                "POST",
                "/registry/engines/hydraulics.laminar/run",
                json_body={
                    "well_id": well["id"],
                    "wellbore_id": wellbore["id"],
                    "section_id": drilling_section["id"],
                    "inputs": {
                        "elements": [
                            {
                                "kind": "drillpipe",
                                "name": "5in drillpipe",
                                "from_depth_si": 0.0,
                                "to_depth_si": 2400.0,
                                "od_si": 0.127,
                                "id_si": 0.1086,
                            },
                            {
                                "kind": "hole",
                                "name": '8-1/2" hole',
                                "from_depth_si": 0.0,
                                "to_depth_si": 2400.0,
                                "od_si": 0.2159,
                            },
                        ],
                        "bit_depth_si": 2400.0,
                        "flow_rate_si": 0.035,
                        "mud_weight_si": 1240.0,
                        "plastic_viscosity_si": 0.018,
                        "yield_point_si": 8.0,
                        "bit_diameter_si": 0.2159,
                        "tfa_si": 0.00045,
                        "nozzle_count": 3,
                        "pore_pressure_gradient_si": 12500.0,
                        "fracture_gradient_si": 15500.0,
                    },
                },
            )

            optimisation = await api.call(
                "POST",
                f"/wells/{well['id']}/engineering/optimise",
                json_body={
                    "section_id": drilling_section["id"],
                    "parameters": [
                        {
                            "name": "flow_rate_si",
                            "min_si": 0.025,
                            "max_si": 0.045,
                            "unit": "m3/s",
                            "steps": 5,
                        },
                        {
                            "name": "mud_weight_si",
                            "min_si": 1220.0,
                            "max_si": 1300.0,
                            "unit": "kg/m3",
                            "steps": 3,
                        },
                    ],
                    "hydraulics_inputs": {
                        "elements": [
                            {
                                "kind": "drillpipe",
                                "name": "5in drillpipe",
                                "from_depth_si": 0.0,
                                "to_depth_si": 2400.0,
                                "od_si": 0.127,
                                "id_si": 0.1086,
                            },
                            {
                                "kind": "hole",
                                "name": '8-1/2" hole',
                                "from_depth_si": 0.0,
                                "to_depth_si": 2400.0,
                                "od_si": 0.2159,
                            },
                        ],
                        "bit_depth_si": 2400.0,
                        "flow_rate_si": 0.035,
                        "mud_weight_si": 1240.0,
                        "plastic_viscosity_si": 0.018,
                        "yield_point_si": 8.0,
                        "bit_diameter_si": 0.2159,
                        "tfa_si": 0.00045,
                        "nozzle_count": 3,
                        "pore_pressure_gradient_si": 12500.0,
                        "fracture_gradient_si": 15500.0,
                    },
                    "limits": {"spp_si": 25.0e6},
                    "objectives": [
                        {"key": "ecd_margin_si", "sense": "maximize"},
                        {"key": "spp_utilisation", "sense": "minimize"},
                        {"key": "annular_velocity_m_s", "sense": "maximize"},
                    ],
                    "samples": 15,
                    "title": "Synthetic demo: flow rate and mud weight sweep",
                },
            )

            workflows = await _seed_workflows(api, well["id"])

            state = await api.call("GET", f"/wells/{well['id']}/state")
            npt = await api.call("GET", f"/wells/{well['id']}/npt")
            timeline = await api.call("GET", f"/wells/{well['id']}/timeline")
            advisor = await api.call(
                "POST", f"/wells/{well['id']}/advisor", json_body={"question": "where_are_we"}
            )

            summary.update(
                {
                    "project_id": project["id"],
                    "well_id": well["id"],
                    "wellbore_id": wellbore["id"],
                    "section_id": drilling_section["id"],
                    "document_ids": [document["id"], second_document["id"]],
                    "processing": {
                        "first": processing["processing"]["records_promoted"],
                        "second": second_processing["processing"]["records_promoted"],
                    },
                    "hydraulics_engine_run_id": hydraulics.get("engine_run_id"),
                    "optimization_run_id": optimisation["optimisation"]["optimization_run_id"],
                    "recommendation_id": optimisation["optimisation"]["recommendation_id"],
                    "state_counts": state["state"]["counts"],
                    "npt_total_hours": npt["npt"]["total_hours"],
                    "timeline_entries": timeline["count"],
                    "advisor_facts": len(advisor["answer"]["facts"]),
                    "advisor_calculations": len(advisor["answer"]["calculations"]),
                    "advisor_evidence": len(advisor["answer"]["evidence"]),
                    **workflows,
                }
            )
        finally:
            await application.state.database.dispose()

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DRILLAI_DATABASE_URL", "sqlite+aiosqlite:///./.data/drillai.db"),
        help="SQLAlchemy URL for the development database (default: ./.data/drillai.db)",
    )
    parser.add_argument("--reset", action="store_true", help="drop and recreate the schema first")
    parser.add_argument(
        "--blob-dir",
        default=".data/blobs",
        help="filesystem backing store for uploaded documents",
    )
    args = parser.parse_args()

    result = asyncio.run(seed(args.database_url, reset=args.reset, blob_dir=pathlib.Path(args.blob_dir)))
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
