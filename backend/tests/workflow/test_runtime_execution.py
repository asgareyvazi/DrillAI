"""Workflow runtime: execution, routing, retries, suspension and authorization.

These are integration tests: they run real graphs against the real models in an in-memory
database, so "the workflow engine works" is a measured statement rather than a claim.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from drillai.core.errors import PermissionDenied, ValidationFailed, WorkflowDefinitionInvalid
from drillai.db.models import (
    ApprovalRequest,
    EngineRun,
    NodeRun,
    Organization,
    OutboundMessage,
    Project,
    Recommendation,
    RunEvent,
    Well,
    WorkflowRun,
)
from drillai.security import catalog as _catalog  # noqa: F401  (registers platform actions)
from drillai.security.actions import ActionLevel, Principal
from drillai.workflow.graph import GraphEdge, GraphNode, WorkflowGraph
from drillai.workflow.nodes import NodeContext, NodeOutcome, NodeSpec, register_node_type
from drillai.workflow.runtime import RunScope, WorkflowRuntime
from drillai.workflow.service import WorkflowService

SURVEY = {
    "stations": [
        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": 1000.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": 1300.0, "inclination_deg": 30.0, "azimuth_deg": 0.0},
    ]
}


@pytest.fixture
async def project(session):
    org = Organization(slug="wf", name="Workflow Org")
    session.add(org)
    await session.flush()
    row = Project(org_id=org.id, name="WF project")
    session.add(row)
    await session.flush()
    well = Well(org_id=org.id, project_id=row.id, name="WF-1")
    session.add(well)
    await session.flush()
    return {"org": org, "project": row, "well": well}


@pytest.fixture
def engineer() -> Principal:
    return Principal(
        id="usr_engineer",
        kind="user",
        role_keys=("engineer",),
        permissions=frozenset({"well.*", "document.*", "engine.*", "workflow.*", "twin.*", "**"}),
        max_action_level=ActionLevel.DRAFT,
    )


@pytest.fixture
def supervisor() -> Principal:
    return Principal(
        id="usr_supervisor",
        kind="user",
        role_keys=("drilling_supervisor",),
        permissions=frozenset({"**"}),
        max_action_level=ActionLevel.EXECUTE_WITH_APPROVAL,
    )


async def _start(session, project, principal, graph: WorkflowGraph, key: str = "wf-test"):
    service = WorkflowService(session, org_id=project["org"].id, principal=principal)
    workflow = await service.create(key=key, name=f"Workflow {key}", graph=graph)
    version = await service.get_version(workflow.id)
    runtime = WorkflowRuntime(session, org_id=project["org"].id, principal=principal)
    run = await runtime.start(
        graph,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        workflow_key=workflow.key,
        scope=RunScope(project_id=project["project"].id, well_id=project["well"].id),
    )
    return run, workflow, runtime


# --------------------------------------------------------------------------- happy path


async def test_linear_workflow_runs_and_persists_every_step(session, project, engineer):
    graph = WorkflowGraph(
        nodes=[
            GraphNode(id="ctx", type="data.load_context", config={"purpose": "workflow"}),
            GraphNode(
                id="engine",
                type="engineering.run_engine",
                config={"engine_key": "trajectory.minimum_curvature", "inputs": {"stations": SURVEY["stations"]}},
            ),
            GraphNode(
                id="recommend",
                type="output.recommendation",
                config={
                    "title": "Hold to section TD",
                    "statement": "Continue drilling to 1300 m MD keeping inclination below 30 degrees.",
                    "rationale": "Maximum DLS from the engine run is stated in the engine output.",
                    "confidence": 0.6,
                    "confidence_basis": "single survey run",
                },
            ),
        ],
        edges=[GraphEdge(id="e1", source="ctx", target="engine"), GraphEdge(id="e2", source="engine", target="recommend")],
    )
    run, workflow, _runtime = await _start(session, project, engineer, graph)

    assert run.status == "succeeded", run.error
    assert run.step_count == 3
    assert run.finished_at is not None and run.duration_ms is not None
    assert run.resolved_node_ids == ["ctx", "engine", "recommend"]
    assert run.workflow_key == workflow.key and run.version == 1

    node_runs = (await session.execute(select(NodeRun).where(NodeRun.run_id == run.id))).scalars().all()
    assert {row.node_id for row in node_runs} == {"ctx", "engine", "recommend"}
    assert all(row.status == "succeeded" for row in node_runs)

    events = (await session.execute(select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq))).scalars().all()
    types = [event.type for event in events]
    assert types[0] == "run_started" and types[-1] == "run_completed"
    assert types.count("node_started") == 3 and types.count("node_succeeded") == 3
    assert [event.seq for event in events] == list(range(1, len(events) + 1))

    engine_runs = (await session.execute(select(EngineRun))).scalars().all()
    assert len(engine_runs) == 1
    assert engine_runs[0].engine_key == "trajectory.minimum_curvature"
    assert engine_runs[0].workflow_run_id == run.id
    assert engine_runs[0].inputs_hash and engine_runs[0].outputs_hash
    assert engine_runs[0].is_feasible is True

    recommendations = (await session.execute(select(Recommendation))).scalars().all()
    assert len(recommendations) == 1
    assert recommendations[0].status == "draft"  # a human decides whether to adopt it
    assert recommendations[0].engine_run_ids == [engine_runs[0].id]
    assert recommendations[0].workflow_run_id == run.id


async def test_node_inputs_inherit_engine_outputs_through_references(session, project, engineer):
    graph = WorkflowGraph(
        nodes=[
            GraphNode(
                id="engine",
                type="engineering.run_engine",
                config={"engine_key": "trajectory.minimum_curvature", "inputs": {"stations": SURVEY["stations"]}},
            ),
            GraphNode(
                id="report",
                type="output.report",
                config={
                    "title": "Trajectory summary",
                    "sections": {"Result": "TVD at TD: ${engine.result.total_tvd_si} m"},
                },
            ),
        ],
        edges=[GraphEdge(id="e1", source="engine", target="report")],
    )
    run, _workflow, _runtime = await _start(session, project, engineer, graph)
    assert run.status == "succeeded", run.error
    report_node = (await session.execute(select(NodeRun).where(NodeRun.node_id == "report"))).scalars().one()
    markdown = report_node.outputs["markdown"]
    assert "1286" in markdown  # the interpolated TVD from the trajectory engine
    artifacts = (await session.execute(select(NodeRun).where(NodeRun.run_id == run.id))).scalars().all()
    assert report_node.status == "succeeded" and artifacts


# --------------------------------------------------------------------------- routing


async def test_branch_routes_only_the_matching_edge(session, project, engineer):
    graph = WorkflowGraph(
        nodes=[
            GraphNode(
                id="engine",
                type="engineering.run_engine",
                config={"engine_key": "trajectory.minimum_curvature", "inputs": {"stations": SURVEY["stations"], "max_dls_limit": 1.0, "max_inc_limit": 45.0}},
            ),
            GraphNode(id="branch", type="logic.branch", config={"condition": "${engine.is_feasible} == false", "true_branch": "violated"}),
            GraphNode(id="clean", type="output.report", config={"title": "Within limits"}),
            GraphNode(id="violated", type="output.report", config={"title": "Limit exceeded"}),
        ],
        edges=[
            GraphEdge(id="e1", source="engine", target="branch"),
            GraphEdge(id="e2", source="branch", target="violated", label="violated"),
            GraphEdge(id="e3", source="branch", target="clean", label="clean"),
        ],
    )
    run, _workflow, _runtime = await _start(session, project, engineer, graph)
    assert run.status == "succeeded", run.error
    node_runs = (await session.execute(select(NodeRun).where(NodeRun.run_id == run.id))).scalars().all()
    statuses = {row.node_id: row.status for row in node_runs}
    # The engine flagged the DLS limit, so the "violated" branch ran and the other one was skipped.
    assert statuses["violated"] == "succeeded"
    assert statuses["clean"] == "skipped"
    skipped = next(row for row in node_runs if row.node_id == "clean")
    assert "not reached" in (skipped.resolution or "")


async def test_edge_condition_uses_the_source_outcome(session, project, engineer):
    graph = WorkflowGraph(
        nodes=[
            GraphNode(id="stats", type="analytics.series_stats", config={"values": [1.0, 2.0, 3.0, 40.0], "thresholds": {"high": 10.0}}),
            GraphNode(id="flagged", type="output.report", config={"title": "Outliers found"}),
            GraphNode(id="quiet", type="output.report", config={"title": "Nothing to report"}),
        ],
        edges=[
            GraphEdge(id="e1", source="stats", target="flagged", condition="${stats.exceedance.high} > 0"),
            GraphEdge(id="e2", source="stats", target="quiet", condition="${stats.exceedance.high} == 0"),
        ],
    )
    run, _workflow, _runtime = await _start(session, project, engineer, graph)
    assert run.status == "succeeded", run.error
    node_runs = {row.node_id: row.status for row in (await session.execute(select(NodeRun).where(NodeRun.run_id == run.id))).scalars().all()}
    assert node_runs["flagged"] == "succeeded" and node_runs["quiet"] == "skipped"


# --------------------------------------------------------------------------- failures


async def test_failing_node_stops_the_run_and_records_the_error(session, project, engineer):
    graph = WorkflowGraph(
        nodes=[
            GraphNode(id="ctx", type="data.load_context", config={"purpose": "workflow"}),
            GraphNode(id="engine", type="engineering.run_engine", config={"engine_key": "does.not.exist"}),
            GraphNode(id="report", type="output.report", config={"title": "Never reached"}),
        ],
        edges=[GraphEdge(id="e1", source="ctx", target="engine"), GraphEdge(id="e2", source="engine", target="report")],
    )
    run, _workflow, _runtime = await _start(session, project, engineer, graph)

    # A node failure is an *outcome* of the run (recorded, returned), not an exception into the
    # caller's stack: the run object is the record a UI and an auditor both read.
    assert run.status == "failed"
    assert run.status == "failed"
    assert run.error_node_id == "engine" and run.error
    failed = (await session.execute(select(NodeRun).where(NodeRun.status == "failed"))).scalars().one()
    assert failed.node_id == "engine" and failed.error_code
    # the unreached node is recorded as skipped, never silently dropped
    node_ids = {row.node_id for row in (await session.execute(select(NodeRun))).scalars().all()}
    assert node_ids == {"ctx", "engine", "report"}


async def test_on_error_continue_lets_the_run_finish(session, project, engineer):
    graph = WorkflowGraph(
        nodes=[
            GraphNode(id="engine", type="engineering.run_engine", config={"engine_key": "does.not.exist"}, on_error="continue"),
            GraphNode(id="report", type="output.report", config={"title": "Report after failure"}),
        ],
        edges=[GraphEdge(id="e1", source="engine", target="report")],
    )
    run, _workflow, _runtime = await _start(session, project, engineer, graph)
    assert run.status == "succeeded"
    failed = (await session.execute(select(NodeRun).where(NodeRun.node_id == "engine"))).scalars().one()
    assert failed.status == "failed" and failed.resolution == "on_error_continue"
    assert failed.outputs.get("failed") is True


async def test_retry_attempts_are_recorded_and_evented(session, project, engineer):
    calls = {"count": 0}

    async def flaky(context: NodeContext, config) -> NodeOutcome:  # type: ignore[no-untyped-def]
        calls["count"] += 1
        if calls["count"] < 3:
            raise RuntimeError(f"transient failure {calls['count']}")
        return NodeOutcome(outputs={"attempts": calls["count"]})

    register_node_type(
        NodeSpec(key="test.flaky", name="Flaky test node", family="logic", description="Fails twice, then succeeds", executor=flaky),
        replace=True,
    )
    graph = WorkflowGraph(
        nodes=[GraphNode(id="flaky", type="test.flaky", retry={"max_attempts": 3})],
    )
    run, _workflow, _runtime = await _start(session, project, engineer, graph, key="wf-retry")
    assert run.status == "succeeded", run.error
    assert calls["count"] == 3
    row = (await session.execute(select(NodeRun).where(NodeRun.node_id == "flaky"))).scalars().one()
    assert row.attempt == 3 and row.status == "succeeded" and row.outputs["attempts"] == 3
    events = (await session.execute(select(RunEvent).where(RunEvent.run_id == run.id))).scalars().all()
    assert sum(1 for event in events if event.type == "node_retrying") == 2


async def test_node_timeout_is_enforced(session, project, engineer):
    import asyncio

    async def slow(context: NodeContext, config) -> NodeOutcome:  # type: ignore[no-untyped-def]
        await asyncio.sleep(5)
        return NodeOutcome(outputs={})

    register_node_type(
        NodeSpec(key="test.slow", name="Slow test node", family="logic", description="Sleeps", executor=slow),
        replace=True,
    )
    graph = WorkflowGraph(nodes=[GraphNode(id="slow", type="test.slow")])
    runtime_principal = engineer
    service = WorkflowService(session, org_id=project["org"].id, principal=runtime_principal)
    workflow = await service.create(key="wf-timeout", name="Timeout", graph=graph)
    version = await service.get_version(workflow.id)
    runtime = WorkflowRuntime(session, org_id=project["org"].id, principal=runtime_principal, node_timeout_seconds=0.05)
    run = await runtime.start(
        graph,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        scope=RunScope(well_id=project["well"].id),
    )
    assert run.status == "timed_out", run.error
    assert "timed out" in (run.error or "")
    node_run = (await session.execute(select(NodeRun).where(NodeRun.node_id == "slow"))).scalars().one()
    assert node_run.error_code == "node.timeout"


# --------------------------------------------------------------------------- authorization


def test_run_requires_a_persisted_definition_version(session):
    """An ad-hoc graph with no workflow/version cannot be run: the run would be unreproducible."""
    import asyncio

    runtime = WorkflowRuntime(session, org_id="org_x", principal=Principal(id="u", permissions=frozenset({"**"})))
    graph = WorkflowGraph(nodes=[GraphNode(id="ctx", type="data.load_context")])
    with pytest.raises(ValidationFailed):
        asyncio.get_event_loop().run_until_complete(
            runtime.start(graph, workflow_id="", workflow_version_id="")  # type: ignore[arg-type]
        )


async def test_high_action_node_is_blocked_below_the_ceiling(session, project, engineer):
    graph = WorkflowGraph(
        nodes=[GraphNode(id="notify", type="output.notify", config={"channel": "email", "recipient": "ops@example.com", "body": "hi"})],
    )
    with pytest.raises(PermissionDenied):
        await _start(session, project, engineer, graph, key="wf-blocked")
    # nothing outside the platform happened, and no run was left behind in a misleading state
    assert (await session.execute(select(OutboundMessage))).scalars().all() == []


async def test_l4_node_suspends_for_approval_then_resumes(session, project, supervisor):
    graph = WorkflowGraph(
        nodes=[
            GraphNode(id="prepare", type="logic.set_variables", config={"variables": {"note": "ready"}}),
            GraphNode(id="notify", type="output.notify", config={"channel": "telegram", "recipient": "@ops", "body": "Plan ready"}),
        ],
        edges=[GraphEdge(id="e1", source="prepare", target="notify")],
    )
    run, _workflow, runtime = await _start(session, project, supervisor, graph, key="wf-approval")
    assert run.status == "waiting_approval"
    assert run.pending_approval_id is not None
    assert (await session.execute(select(OutboundMessage))).scalars().all() == []  # nothing sent yet

    approval = (await session.execute(select(ApprovalRequest))).scalars().one()
    assert approval.status == "pending"
    assert approval.node_id == "notify"
    assert approval.action_level == "L4"
    assert approval.required_role == "approver"
    assert approval.request_payload["node_type"] == "output.notify"
    assert approval.description

    # A decision must be recorded before the run continues.
    from drillai.core.errors import ApprovalRequired

    with pytest.raises(ApprovalRequired):
        await runtime.resume(run.id, graph, approval_id=approval.id)

    approval.status = "approved"
    approval.decided_by = "usr_supervisor"
    approval.decided_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()
    resumed = await runtime.resume(run.id, graph, approval_id=approval.id, decided_by="usr_supervisor")
    assert resumed.status == "succeeded", resumed.error
    messages = (await session.execute(select(OutboundMessage))).scalars().all()
    assert len(messages) == 1
    assert messages[0].status == "queued"
    assert messages[0].idempotency_key == f"{run.id}:notify"
    events = {event.type for event in (await session.execute(select(RunEvent).where(RunEvent.run_id == run.id))).scalars().all()}
    assert {"approval_requested", "run_paused", "approval_decided", "run_resumed"} <= events


async def test_a_human_approval_node_suspends_the_run_and_records_what_it_asked_for(
    session, project, supervisor
):
    """A `human.approval` node must really stop the run.

    The node declares an approval request; before this test existed the runtime discarded that
    request and the run continued, so a graph that a designer had deliberately gated on a human
    decision completed unattended.

    The runner is a supervisor because the authorization gate still applies: a principal may not
    drive a run containing work above its own action-level ceiling, approval or not. The approval is
    a separate control *on top of* that — what it adds is a recorded human decision, taken by a
    different principal, before the gated work proceeds.
    """
    graph = WorkflowGraph(
        nodes=[
            GraphNode(id="prepare", type="logic.set_variables", config={"variables": {"note": "ready"}}),
            GraphNode(
                id="approve",
                type="human.approval",
                config={
                    "title": "Approve the daily plan",
                    "description": "Review the computed state before the report is issued.",
                    "action_level": "L3",
                    "required_role": "drilling_supervisor",
                    "risk_notes": ["The report reaches the rig site once approved."],
                    "evidence_refs": ["doc_ddr_2026-03-15"],
                },
            ),
            GraphNode(id="finish", type="logic.set_variables", config={"variables": {"done": True}}),
        ],
        edges=[
            GraphEdge(id="e1", source="prepare", target="approve"),
            GraphEdge(id="e2", source="approve", target="finish"),
        ],
    )
    run, _workflow, runtime = await _start(session, project, supervisor, graph, key="wf-human-approval")
    assert run.status == "waiting_approval", "the run must stop at the human approval node"
    assert run.pending_approval_id is not None

    # Scoped to this run: the database is shared by the file, so an unscoped select would also see
    # approvals raised by the tests above.
    approval = (
        await session.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == run.id))
    ).scalars().one()
    assert approval.title == "Approve the daily plan"
    assert approval.description.startswith("Review the computed state")
    assert approval.action_level == "L3"
    assert approval.required_role == "drilling_supervisor"
    assert approval.risk_notes and "rig site" in approval.risk_notes
    assert approval.evidence_refs == ["doc_ddr_2026-03-15"]
    assert approval.expires_at is not None
    # The node that asked for the decision is recorded as waiting, not as having succeeded.
    waiting = (
        await session.execute(select(NodeRun).where(NodeRun.run_id == run.id, NodeRun.node_id == "approve"))
    ).scalars().one()
    assert waiting.status == "waiting_approval"
    # The node after the gate never ran, and is recorded as skipped rather than left absent.
    finish = (
        await session.execute(select(NodeRun).where(NodeRun.run_id == run.id, NodeRun.node_id == "finish"))
    ).scalars().all()
    assert [row.status for row in finish] == ["skipped"]

    approval.status = "approved"
    approval.decided_by = "usr_supervisor"
    approval.decision_note = "Approved after review"
    approval.decided_at = dt.datetime.now(tz=dt.UTC)
    await session.flush()

    resumed = await runtime.resume(run.id, graph, approval_id=approval.id, decided_by="usr_supervisor")
    assert resumed.status == "succeeded", resumed.error
    # The waiting row is completed on resume rather than left dangling.
    rows = (
        await session.execute(select(NodeRun).where(NodeRun.run_id == run.id, NodeRun.node_id == "approve"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "succeeded"
    assert rows[0].outputs.get("decision") == "approved"
    assert resumed.variables.get("done") is True


async def test_a_rejected_human_approval_stops_the_run_before_the_next_node(session, project, supervisor):
    graph = WorkflowGraph(
        nodes=[
            GraphNode(id="approve", type="human.approval", config={"title": "Approve the plan"}),
            GraphNode(id="finish", type="logic.set_variables", config={"variables": {"done": True}}),
        ],
        edges=[GraphEdge(id="e1", source="approve", target="finish")],
    )
    run, _workflow, runtime = await _start(session, project, supervisor, graph, key="wf-human-reject")
    approval = (
        await session.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == run.id))
    ).scalars().one()
    approval.status = "rejected"
    approval.decided_by = "usr_supervisor"
    await session.flush()

    result = await runtime.resume(run.id, graph, approval_id=approval.id)
    assert result.status == "cancelled"
    assert "rejected" in (result.error or "")
    assert result.variables.get("done") is None


async def test_an_approval_node_above_the_callers_ceiling_is_still_refused(session, project, engineer):
    """An approval node is not a bypass: an L2 caller may not drive a graph that proposes at L3.

    The decision is what a *different* principal contributes; the caller's own ceiling still governs
    what the caller may set in motion.
    """
    graph = WorkflowGraph(
        nodes=[GraphNode(id="approve", type="human.approval", config={"title": "Approve at L3", "action_level": "L3"})]
    )
    with pytest.raises(PermissionDenied):
        await _start(session, project, engineer, graph, key="wf-approval-ceiling")


async def test_rejected_approval_cancels_the_run_and_sends_nothing(session, project, supervisor):
    graph = WorkflowGraph(
        nodes=[GraphNode(id="notify", type="output.notify", config={"channel": "email", "recipient": "ops@example.com", "body": "hi"})],
    )
    run, _workflow, runtime = await _start(session, project, supervisor, graph, key="wf-reject")
    approval = (await session.execute(select(ApprovalRequest))).scalars().one()
    approval.status = "rejected"
    approval.decided_by = "usr_supervisor"
    await session.flush()
    result = await runtime.resume(run.id, graph, approval_id=approval.id)
    assert result.status == "cancelled"
    assert "rejected" in (result.error or "")
    assert (await session.execute(select(OutboundMessage))).scalars().all() == []


async def test_dry_run_does_not_write_outside_the_platform(session, project, supervisor):
    graph = WorkflowGraph(
        nodes=[
            GraphNode(id="prepare", type="logic.set_variables", config={"variables": {"x": 1}}),
            GraphNode(id="notify", type="output.notify", config={"channel": "email", "recipient": "ops@example.com", "body": "hi"}),
        ],
        edges=[GraphEdge(id="e1", source="prepare", target="notify")],
    )
    service = WorkflowService(session, org_id=project["org"].id, principal=supervisor)
    workflow = await service.create(key="wf-dry", name="Dry run", graph=graph)
    version = await service.get_version(workflow.id)
    run = await service.start_run(workflow.id, version=version.version, is_dry_run=True, scope=RunScope(well_id=project["well"].id))
    # The L4 gate still applies (authorization is never skipped), but nothing is queued.
    assert run.status == "waiting_approval"


async def test_loop_back_edge_is_bounded_and_reported(session, project, engineer):
    graph = WorkflowGraph(
        nodes=[
            GraphNode(id="step", type="logic.set_variables", config={"variables": {"n": 1}}),
        ],
        edges=[GraphEdge(id="loop", source="step", target="step", is_loop_back=True)],
    )
    run, _workflow, _runtime = await _start(session, project, engineer, graph, key="wf-loop")
    assert run.status == "failed"
    assert "loop-back" in (run.error or "")
    assert run.attributes["max_loop_iterations"] == 3


async def test_breakpoint_pauses_the_run_at_the_marked_node(session, project, engineer):
    graph = WorkflowGraph(
        nodes=[
            GraphNode(id="first", type="logic.set_variables", config={"variables": {"a": 1}}),
            GraphNode(id="gate", type="logic.set_variables", config={"variables": {"b": 2}}, is_breakpoint=True),
            GraphNode(id="last", type="output.report", config={"title": "After the breakpoint"}),
        ],
        edges=[GraphEdge(id="e1", source="first", target="gate"), GraphEdge(id="e2", source="gate", target="last")],
    )
    run, _workflow, _runtime = await _start(session, project, engineer, graph, key="wf-breakpoint")
    assert run.status == "paused"
    assert run.cursor_node_id == "gate"
    events = [event.type for event in (await session.execute(select(RunEvent).where(RunEvent.run_id == run.id))).scalars().all()]
    assert "breakpoint_hit" in events
    skipped = (await session.execute(select(NodeRun).where(NodeRun.status == "skipped"))).scalars().all()
    assert [row.node_id for row in skipped] == ["last"]


async def test_invalid_graph_is_rejected_before_anything_runs(session, project, engineer):
    graph = WorkflowGraph(nodes=[GraphNode(id="a", type="not.a.node")])
    with pytest.raises(WorkflowDefinitionInvalid):
        await _start(session, project, engineer, graph, key="wf-invalid")
    assert (await session.execute(select(WorkflowRun))).scalars().all() == []
