"""End-to-end smoke: does the claimed foundation actually connect?

Run from the repository root:  backend/.venv/bin/python scripts/smoke_e2e.py

context -> ingestion -> evidence -> twin -> workflow -> engine -> AI -> recommendation, all inside
one in-memory SQLite session against the real models, real pipeline and real workflow runtime. No
network access is required: the single outbound (L4) node targets the reserved ``.invalid`` TLD on
purpose, and the check asserts the *failure is recorded on the node*, not that it succeeded.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import traceback

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from drillai.ai.providers import ChatMessage, CompletionRequest
from drillai.ai.router import LlmRouter, TaskProfile
from drillai.ai.tools import ToolContext, invoke_tool
from drillai.context.builder import default_context_builder
from drillai.context.model import ContextPurpose, ContextRequest, ContextScope
from drillai.db.models import (
    Base,
    EvidenceLink,
    ExtractedRecord,
    Organization,
    Project,
    Recommendation,
    RunEvent,
    Well,
)
from drillai.ingestion.pipeline import IngestionPipeline
from drillai.ingestion.storage import LocalBlobStore
from drillai.rag.retriever import RetrievalQuery, retrieve
from drillai.security import catalog as _catalog  # noqa: F401  (registers platform actions)
from drillai.security.actions import ActionLevel, Principal
from drillai.twin.aspects import StateKind
from drillai.twin.service import AspectRevision, TwinService
from drillai.workflow.graph import GraphEdge, GraphNode, WorkflowGraph
from drillai.workflow.runtime import RunScope, WorkflowRuntime
from drillai.workflow.service import WorkflowService

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


async def main() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with factory() as session:
        org = Organization(slug="smoke", name="Smoke Co")
        session.add(org)
        await session.flush()
        project = Project(org_id=org.id, name="Dev")
        session.add(project)
        await session.flush()
        well = Well(org_id=org.id, project_id=project.id, name="NF-12")
        session.add(well)
        await session.flush()

        # ---------------------------------------------------------------- ingestion
        blob_root = tempfile.mkdtemp(prefix="drillai-smoke-blobs-")
        store = LocalBlobStore(blob_root)
        pipeline = IngestionPipeline(session, store, org_id=org.id)
        csv = b"MD (m),Inclination (deg),Azimuth (deg)\n0,0.0,0.0\n500,1.5,45.0\n1000,3.0,90.0\n"
        outcome = await pipeline.ingest(
            csv,
            filename="survey.csv",
            content_type="text/csv",
            project_id=project.id,
            well_id=well.id,
            triggered_by="usr_smoke",
        )
        check("ingestion: document created", outcome.document is not None, outcome.document.id if outcome.document else "")
        check("ingestion: job recorded", outcome.job.status == "succeeded", outcome.job.status)
        records = (
            await session.execute(select(ExtractedRecord).where(ExtractedRecord.well_id == well.id))
        ).scalars().all()
        check("ingestion: records extracted", len(records) >= 1, f"{len(records)} records")
        links = (await session.execute(select(EvidenceLink))).scalars().all()
        check("evidence: links created", len(links) == len(records), f"{len(links)} links")

        # ---------------------------------------------------------------- retriever
        result = await retrieve(
            session,
            org.id,
            RetrievalQuery(text="inclination azimuth survey", mode="hybrid", well_id=well.id, limit=5),
        )
        check("rag: retrieval returns hits", len(result.hits) >= 1, f"{len(result.hits)} hits, mode={result.mode_used}")
        try:
            await retrieve(session, org.id, RetrievalQuery(text="x", mode="lexical"))
            check("rag: unscoped query refused", False, "no error raised")
        except ValueError:
            check("rag: unscoped query refused", True)

        # ---------------------------------------------------------------- twin
        twin = TwinService(session, org_id=org.id)
        await twin.write_aspect(
            well_id=well.id,
            revision=AspectRevision(
                aspect="trajectory",
                state_kind=StateKind.ACTUAL,
                payload={"md_si": 1000.0, "tvd_si": 990.0},
                summary="survey to 1000 m",
                computed_by="engine",
                source_refs=[outcome.document.id],
            ),
        )
        check("twin: aspect written", True)
        current = await twin.current_state(well_id=well.id)
        check("twin: current state resolves", len(current) >= 1, f"{len(current)} aspects")

        # ---------------------------------------------------------------- context
        builder = default_context_builder()
        bundle = await builder.build(
            session,
            ContextRequest(
                scope=ContextScope(org_id=org.id, project_id=project.id, well_id=well.id),
                purpose=ContextPurpose.PROMPT,
                permissions=frozenset({"well.read", "document.read", "engine.run"}),
            ),
        )
        check("context: bundle built", bundle.item_count() >= 1, f"{len(bundle.sections)} sections, {bundle.item_count()} items")
        prompt = builder.render_prompt_payload(bundle)
        check("context: prompt rendered", "ENGINEERING CONTEXT" in prompt, f"{len(prompt)} chars")

        # ---------------------------------------------------------------- tools
        principal = Principal(
            id="usr_smoke",
            kind="user",
            org_id=org.id,
            role_keys=("engineer",),
            permissions=frozenset({"well.read", "document.read", "engine.run"}),
            max_action_level=ActionLevel.DRAFT,
        )
        tool_context = ToolContext(session=session, org_id=org.id, principal=principal, project_id=project.id, well_id=well.id)
        tool_result = await invoke_tool("search_documents", tool_context, {"query": "inclination", "mode": "lexical", "limit": 3})
        check("tools: search_documents executed", tool_result.status == "succeeded", f"{len(tool_result.output['hits'])} hits")
        engine_result = await invoke_tool(
            "run_engine",
            tool_context,
            {"engine_key": "trajectory.minimum_curvature", "inputs": {
                "stations": [
                    {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                    {"md_si": 1000.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                ]
            }},
        )
        check("tools: run_engine executed", engine_result.status == "succeeded", engine_result.output.get("engine_key", ""))

        # ---------------------------------------------------------------- llm router
        router = LlmRouter()
        response, decision = await router.complete(
            CompletionRequest(messages=[ChatMessage(role="user", content="Summarise: md = 1000 m")]),
            session=session,
            org_id=org.id,
            profile=TaskProfile.SUMMARIZE,
            subject_kind="well",
            subject_id=well.id,
        )
        check("ai: router completed", bool(response.text), f"{decision.spec.provider}:{decision.spec.model}")
        from drillai.db.models import LlmCall

        calls = (await session.execute(select(LlmCall))).scalars().all()
        check("ai: call audited", len(calls) == 1, f"{len(calls)} rows")

        # ---------------------------------------------------------------- workflow
        graph = WorkflowGraph(
            nodes=[
                GraphNode(id="context", type="data.load_context", config={"purpose": "workflow"}),
                GraphNode(
                    id="trajectory",
                    type="engineering.run_engine",
                    config={
                        "engine_key": "trajectory.minimum_curvature",
                        "inputs": {
                            "stations": [
                                {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                                {"md_si": 1000.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                                {"md_si": 1300.0, "inclination_deg": 30.0, "azimuth_deg": 0.0},
                            ]
                        },
                    },
                ),
                GraphNode(
                    id="recommend",
                    type="output.recommendation",
                    config={
                        "title": "Hold inclination to section TD",
                        "statement": "Trajectory is within the plan; continue drilling to 1300 m MD.",
                        "rationale": "Max DLS from ${trajectory.result.max_dls_deg_per_30m} deg/30m (SI: degrees per 30 m).",
                        "confidence": 0.7,
                        "confidence_basis": "survey data from surface",
                    },
                ),
            ],
            edges=[
                GraphEdge(id="e1", source="context", target="trajectory"),
                GraphEdge(id="e2", source="trajectory", target="recommend"),
            ],
        )
        service = WorkflowService(session, org_id=org.id, principal=principal)
        supervisor = Principal(
            id="usr_supervisor",
            kind="user",
            org_id=org.id,
            role_keys=("drilling_supervisor",),
            permissions=frozenset({"**"}),
            max_action_level=ActionLevel.EXECUTE_WITH_APPROVAL,
        )
        supervisor_service = WorkflowService(session, org_id=org.id, principal=supervisor)
        supervisor_runtime = WorkflowRuntime(session, org_id=org.id, principal=supervisor)
        try:
            workflow = await service.create(key="smoke-trajectory", name="Smoke trajectory review", graph=graph)
            await service.publish(workflow.id)
            run = await service.start_run(
                workflow.id, scope=RunScope(project_id=project.id, well_id=well.id), services={"llm_router": router}
            )
            check("workflow: run succeeded", run.status == "succeeded", f"status={run.status} error={run.error}")
            node_runs = (await session.execute(select(RunEvent).where(RunEvent.run_id == run.id))).scalars().all()
            check("workflow: events recorded", len(node_runs) >= 5, f"{len(node_runs)} events")
            recs = (await session.execute(select(Recommendation))).scalars().all()
            check("workflow: recommendation persisted", len(recs) == 1, recs[0].title if recs else "")
            from drillai.db.models import EngineRun

            engine_runs = (await session.execute(select(EngineRun))).scalars().all()
            after_workflow = [row for row in engine_runs if row.workflow_run_id == run.id]
            check(
                "workflow: engine run persisted and linked to the run",
                len(after_workflow) == 1,
                f"{len(engine_runs)} total, {len(after_workflow)} linked to this run",
            )
        except Exception as exc:
            traceback.print_exc()
            check("workflow: run succeeded", False, f"{type(exc).__name__}: {exc}")

        # ---------------------------------------------------------------- approval path
        approval_graph = WorkflowGraph(
            nodes=[
                GraphNode(id="start", type="logic.set_variables", config={"variables": {"x": 1}}),
                GraphNode(
                    id="publish",
                    type="integration.http_fetch",
                    config={"url": "https://example.invalid/notify"},
                ),
            ],
            edges=[GraphEdge(id="a1", source="start", target="publish")],
        )
        try:
            approval_workflow = await supervisor_service.create(key="smoke-approval", name="Smoke approval", graph=approval_graph)
            approval_version = await supervisor_service.get_version(approval_workflow.id)
            run2 = await supervisor_runtime.start(
                approval_graph,
                workflow_id=approval_workflow.id,
                workflow_version_id=approval_version.id,
                scope=RunScope(well_id=well.id),
                workflow_key=approval_workflow.key,
            )
            check("workflow: L4 node suspended for approval", run2.status == "waiting_approval", run2.status)
            from drillai.db.models import ApprovalRequest

            approvals = (await session.execute(select(ApprovalRequest))).scalars().all()
            check("workflow: approval row persisted", len(approvals) == 1, approvals[0].status if approvals else "")
            if approvals:
                approvals[0].status = "approved"
                approvals[0].decided_by = "usr_smoke"
                await session.flush()
                resumed = await supervisor_runtime.resume(run2.id, approval_graph, approval_id=approvals[0].id, decided_by="usr_supervisor")
                # The L4 node executes only after approval. In this sandbox the outbound call cannot
                # succeed (no DNS for example.invalid), so what is asserted is that the node RAN and
                # that any failure is recorded on the node — never swallowed.
                from drillai.db.models import NodeRun

                publish_runs = (
                    await session.execute(select(NodeRun).where(NodeRun.run_id == run2.id, NodeRun.node_id == "publish"))
                ).scalars().all()
                ran = resumed.status in {"succeeded", "failed"} and len(publish_runs) == 1
                recorded = publish_runs[0].status in {"succeeded", "failed"}
                if publish_runs[0].status == "failed":
                    recorded = recorded and bool(publish_runs[0].error)
                check("workflow: resume after approval executes the L4 node", ran and recorded,
                      f"{resumed.status}, node={publish_runs[0].status}, error={str(publish_runs[0].error)[:60]}")
        except Exception as exc:
            traceback.print_exc()
            check("workflow: approval path", False, f"{type(exc).__name__}: {exc}")

        await session.commit()

    await engine.dispose()
    shutil.rmtree(blob_root, ignore_errors=True)
    failures = [name for name, ok, _ in RESULTS if not ok]
    print("\n" + ("ALL SMOKE CHECKS PASSED" if not failures else f"FAILURES: {failures}"))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
