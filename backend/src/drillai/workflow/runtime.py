"""Workflow runtime.

The runtime owns everything a node must not know about: traversal, branch routing, loop bounds,
retries, breakpoints, tracing, run/node/event/artefact persistence, and *suspension for human
approval*.

Durability is the point of the design. A run that is waiting for a person is a row in the
database, not a coroutine parked in memory: ``WorkflowRun.status = waiting_approval`` plus an
``ApprovalRequest`` row means a restart, a deploy or an hour-long wait changes nothing. That is
also why approval state is never held in the runtime object.

Traversal semantics (deliberately small and documented, because a workflow engine that is
"clever" is a workflow engine nobody can debug):

* a node runs when it is an entry node, or when at least one incoming edge was taken;
* an edge is taken when its ``condition`` evaluates truthy, or (no condition) when its source
  did not branch;
* a node that returns ``branch`` routes explicitly: only edges whose ``condition``/``label``
  equals the branch value are taken;
* loop-back edges re-enqueue their target, bounded by ``settings.max_loop_iterations``;
* unreached nodes are recorded as ``skipped`` — never silently dropped.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import UTC
from drillai.core.errors import (
    ApprovalRequired,
    NodeExecutionFailed,
    PermissionDenied,
    ValidationFailed,
    WorkflowDefinitionInvalid,
)
from drillai.core.ids import new_id
from drillai.core.logging import get_logger, log_context
from drillai.db.models import ApprovalRequest, NodeRun, RunArtifact, RunEvent, WorkflowRun
from drillai.observability.tracing import get_tracer
from drillai.security.actions import ActionLevel, Principal, envelope_allows
from drillai.workflow.expressions import ExpressionError, evaluate_condition, resolve_references
from drillai.workflow.graph import GraphNode, WorkflowGraph, validate_graph
from drillai.workflow.nodes import NodeContext, NodeOutcome, NodeSpec, node_spec, registered_node_types

logger = get_logger(__name__)

__all__ = ["RunScope", "WorkflowRuntime"]


@dataclass
class RunScope:
    project_id: str | None = None
    well_id: str | None = None
    wellbore_id: str | None = None
    section_id: str | None = None
    operation_id: str | None = None


@dataclass
class _Traversal:
    """Mutable traversal state (kept out of the persistence model)."""

    taken_edges: set[str] = field(default_factory=set)
    executed: dict[str, list[NodeRun]] = field(default_factory=dict)
    node_outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    loop_counts: dict[str, int] = field(default_factory=dict)
    visited: set[str] = field(default_factory=set)
    skipped: set[str] = field(default_factory=set)
    branch_taken: dict[str, str | None] = field(default_factory=dict)


class WorkflowRuntime:
    """Executes a :class:`WorkflowGraph` inside one session."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        org_id: str,
        principal: Principal,
        services: dict[str, Any] | None = None,
        max_loop_iterations: int = 3,
        node_timeout_seconds: float | None = 300.0,
    ) -> None:
        self.session = session
        self.org_id = org_id
        self.principal = principal
        self.services = services or {}
        self.max_loop_iterations = max_loop_iterations
        self.node_timeout_seconds = node_timeout_seconds
        self._event_seq = 0

    # ------------------------------------------------------------------ public API

    async def start(
        self,
        graph: WorkflowGraph,
        *,
        workflow_id: str,
        workflow_version_id: str,
        scope: RunScope | None = None,
        inputs: dict[str, Any] | None = None,
        workflow_key: str | None = None,
        version: int = 1,
        trigger_type: str = "manual",
        trigger_ref: str | None = None,
        is_dry_run: bool = False,
        max_loop_iterations: int | None = None,
    ) -> WorkflowRun:
        """Validate the graph, create the run row, and execute until it finishes, pauses or waits.

        ``workflow_id`` and ``workflow_version_id`` are required: a run that does not reference the
        definition version it executed cannot be reproduced or audited, and both columns are NOT
        NULL for exactly that reason. Use :meth:`WorkflowService.start_run` to run a saved
        workflow — it resolves the version and passes it through.
        """
        if not workflow_id or not workflow_version_id:
            raise ValidationFailed(
                "a workflow run must reference a persisted workflow definition version",
                details={"workflow_id": workflow_id, "workflow_version_id": workflow_version_id},
            )
        report = validate_graph(graph, node_types=registered_node_types())
        if not report.is_valid:
            raise WorkflowDefinitionInvalid(
                "workflow graph is invalid",
                details={"issues": [issue.model_dump() for issue in report.errors]},
            )
        scope = scope or RunScope()
        run = WorkflowRun(
            org_id=self.org_id,
            workflow_id=workflow_id,
            workflow_version_id=workflow_version_id,
            workflow_key=workflow_key or f"inline:{graph.hash()[:12]}",
            version=version,
            status="queued",
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            trigger_payload={},
            project_id=scope.project_id,
            well_id=scope.well_id,
            wellbore_id=scope.wellbore_id,
            operation_id=scope.operation_id,
            context={"scope": scope.__dict__},
            inputs=inputs or {},
            outputs={},
            variables={},
            started_at=dt.datetime.now(tz=UTC),
            initiated_by=self.principal.id,
            initiated_by_kind=self.principal.kind,
            is_dry_run=is_dry_run,
            breakpoints=sorted(node.id for node in graph.nodes if node.is_breakpoint),
            resolved_node_ids=[],
            metrics={},
            attributes={"graph_hash": graph.hash(), "max_loop_iterations": max_loop_iterations or self.max_loop_iterations},
        )
        self.session.add(run)
        await self.session.flush()
        await self._event(run, "run_started", message=f"run started at v{version}")
        return await self._execute(run, graph, RunScope(**run.context["scope"]))

    async def resume(
        self,
        run_id: str,
        graph: WorkflowGraph,
        *,
        approval_id: str | None = None,
        decision: str | None = None,
        decided_by: str | None = None,
    ) -> WorkflowRun:
        """Resume a waiting/paused run, recording the human decision that unblocked it."""
        run = (await self.session.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))).scalar_one_or_none()
        if run is None:
            raise NodeExecutionFailed(f"workflow run {run_id!r} not found")
        if run.status not in {"waiting_approval", "paused"}:
            raise NodeExecutionFailed(f"run {run_id!r} is {run.status}, not waiting or paused")

        approvals = dict((run.attributes or {}).get("approvals") or {})
        if approval_id:
            approval = (
                await self.session.execute(select(ApprovalRequest).where(ApprovalRequest.id == approval_id))
            ).scalar_one_or_none()
            if approval is None:
                raise NodeExecutionFailed(f"approval {approval_id!r} not found")
            if approval.status == "pending":
                raise ApprovalRequired(f"approval {approval_id!r} has not been decided yet")
            if approval.status != "approved":
                run.status = "cancelled"
                run.finished_at = dt.datetime.now(tz=UTC)
                run.error = f"approval {approval.id} was {approval.status}"
                await self._event(run, "run_cancelled", message=run.error, node_id=approval.node_id)
                await self.session.flush()
                return run
            approvals[approval.node_id or ""] = approval.id
            run.approved_by = approval.decided_by
            await self._event(
                run,
                "approval_decided",
                message=f"approval {approval.id} {approval.status}",
                node_id=approval.node_id,
                payload={"decision_note": approval.decision_note, "decided_by": approval.decided_by},
            )
        run.attributes = {**(run.attributes or {}), "approvals": approvals}
        run.status = "running"
        run.pending_approval_id = None
        await self._event(run, "run_resumed", message=f"resumed with decision {decision or 'approved'}")
        await self.session.flush()
        return await self._execute(run, graph, RunScope(**(run.context or {}).get("scope", {})))

    # ------------------------------------------------------------------ traversal

    async def _execute(self, run: WorkflowRun, graph: WorkflowGraph, scope: RunScope) -> WorkflowRun:
        started = time.perf_counter()
        run.status = "running"
        state = _Traversal()
        order = _topological(graph)
        cursor = run.cursor_node_id
        pending: list[tuple[str, int]] = [(node_id, 0) for node_id in order]
        invalidate_from = cursor
        # Resume replay: a suspended run starts a new traversal, so the routing decisions of the
        # nodes that already ran are restored from their persisted NodeRun rows (outputs and branch)
        # rather than re-executing them. Without this, every node downstream of an approved node
        # would look unreachable on resume.
        await self._restore_state(run, state)

        while pending:
            node_id, loop_index = pending.pop(0)
            node = graph.node_map().get(node_id)
            if node is None:
                continue
            if node_id in state.executed and loop_index == 0 and node_id in (run.resolved_node_ids or []):
                # Already ran in an earlier pass: replay its routing, do not run it again.
                self._replay_route(node, graph, state)
                continue
            if not self._should_run(node, graph, state, run, loop_index, invalidate_from):
                if node_id not in state.visited:
                    await self._record_skipped(run, node, reason="not reached by any taken edge")
                    state.skipped.add(node_id)
                continue

            outcome = await self._run_node(run, node, graph, state, scope, loop_index)
            if outcome is None:
                # The run stopped here: failed, timed out or suspended for a human decision. Either
                # way the rest of the graph must be recorded as skipped — a node that never ran has
                # to be visible as "not run", never simply absent.
                await self._record_remaining_skipped(run, graph, pending, reason=f"run stopped at {node.id}")
                await self._finish(run, started)
                return run
            state.executed.setdefault(node_id, []).append(self._last_node_run)
            if self._last_node_run.status == "succeeded":
                state.branch_taken[node_id] = outcome.branch
            if outcome.outputs:
                # A failed-but-continued node also publishes its error envelope.
                state.node_outputs[node_id] = outcome.outputs
            if self._last_node_run.status in {"succeeded", "failed"}:
                state.visited.add(node_id)
            self._apply_branch(node, outcome, graph, state)

            if outcome.stop:
                await self._event(run, "run_completed", message=f"stopped at {node.id}", node_id=node.id)
                run.status = "succeeded"
                break

            # Loop-back edges re-enqueue their targets.
            for edge in graph.edges:
                if not edge.is_loop_back or edge.source != node.id:
                    continue
                if edge.id and edge.id not in state.taken_edges:
                    continue
                target_loop = state.loop_counts.get(edge.target, 0) + 1
                state.loop_counts[edge.target] = target_loop
                if target_loop > self.max_loop_iterations:
                    run.status = "failed"
                    run.error = f"loop-back edge {edge.source}->{edge.target} exceeded {self.max_loop_iterations} iterations"
                    run.error_node_id = node.id
                    await self._event(run, "run_failed", message=run.error, node_id=node.id, level="error")
                    await self._record_remaining_skipped(run, graph, pending, reason="loop bound exceeded")
                    await self._finish(run, started)
                    return run
                pending.append((edge.target, target_loop))

            if node.is_breakpoint and node.id not in (run.attributes or {}).get("resumed_breakpoints", []):
                run.status = "paused"
                run.cursor_node_id = node.id
                await self._event(run, "breakpoint_hit", message=f"breakpoint at {node.id}", node_id=node.id)
                await self._record_remaining_skipped(run, graph, pending, reason=f"paused at breakpoint {node.id}")
                await self._finish(run, started)
                return run

        if run.status == "running":
            run.status = "succeeded"
            await self._event(run, "run_completed", message="all reachable nodes completed")
        run.outputs = {**run.outputs, **{node_id: payload for node_id, payload in state.node_outputs.items() if payload}}
        run.variables = {**run.variables, "node_outputs": list(state.node_outputs)}
        await self._finish(run, started)
        return run

    async def _restore_state(self, run: WorkflowRun, state: _Traversal) -> None:
        """Rebuild node outputs and routing decisions from persisted node runs (resume path)."""
        if not run.resolved_node_ids:
            return
        rows = (
            await self.session.execute(
                select(NodeRun).where(NodeRun.run_id == run.id, NodeRun.status == "succeeded")
            )
        ).scalars().all()
        for row in rows:
            state.executed.setdefault(row.node_id, []).append(row)
            if row.outputs:
                state.node_outputs[row.node_id] = dict(row.outputs)
            state.visited.add(row.node_id)
            state.branch_taken[row.node_id] = row.branch_taken

    def _replay_route(self, node: GraphNode, graph: WorkflowGraph, state: _Traversal) -> None:
        outcome = NodeOutcome(outputs=state.node_outputs.get(node.id, {}), branch=state.branch_taken.get(node.id))
        self._apply_branch(node, outcome, graph, state)

    def _should_run(
        self,
        node: GraphNode,
        graph: WorkflowGraph,
        state: _Traversal,
        run: WorkflowRun,
        loop_index: int,
        invalidate_from: str | None,
    ) -> bool:
        if loop_index > 0:
            return True
        if node.id in state.visited:
            return False
        incoming = [edge for edge in graph.edges if edge.target == node.id and not edge.is_loop_back]
        if not incoming:
            # Entry node: run, unless this is a resume and the node was already resolved.
            return node.id not in (run.resolved_node_ids or []) or invalidate_from == node.id
        return any(edge.id in state.taken_edges for edge in incoming)

    def _apply_branch(self, node: GraphNode, outcome: NodeOutcome, graph: WorkflowGraph, state: _Traversal) -> None:
        # Loop-back edges are taken when their source ran without failing (that is what "loop back
        # and try again" means); a condition on the edge can still veto it.
        for edge in graph.edges:
            if edge.source != node.id or not edge.is_loop_back:
                continue
            key = edge.id or f"{edge.source}->{edge.target}:loop"
            if edge.condition:
                try:
                    if not evaluate_condition(edge.condition, _ConditionContext(outcome, state)):
                        continue
                except ExpressionError as exc:
                    raise NodeExecutionFailed(
                        f"loop-back condition {edge.condition!r} on {edge.source}->{edge.target} failed: {exc}"
                    ) from exc
            state.taken_edges.add(key)
            if edge.id:
                state.taken_edges.add(edge.id)
        outgoing = [edge for edge in graph.edges if edge.source == node.id and not edge.is_loop_back]
        for edge in outgoing:
            key = edge.id or f"{edge.source}->{edge.target}:{edge.condition or ''}"
            if outcome.branch is not None:
                take = (bool(edge.condition) and edge.condition == outcome.branch) or (bool(edge.label) and edge.label == outcome.branch)
            elif edge.condition:
                try:
                    take = evaluate_condition(edge.condition, _ConditionContext(outcome, state))
                except ExpressionError as exc:
                    raise NodeExecutionFailed(
                        f"edge condition {edge.condition!r} on {edge.source}->{edge.target} failed: {exc}"
                    ) from exc
            else:
                take = True
            if take:
                state.taken_edges.add(key)
                if edge.id:
                    state.taken_edges.add(edge.id)

    # ------------------------------------------------------------------ node execution

    _last_node_run: NodeRun

    async def _run_node(
        self,
        run: WorkflowRun,
        node: GraphNode,
        graph: WorkflowGraph,
        state: _Traversal,
        scope: RunScope,
        loop_index: int,
    ) -> NodeOutcome | None:
        spec = node_spec(node.type)
        # 1. authorization — before config parsing, so an unauthorized caller learns nothing.
        try:
            self._authorize(run, node, spec)
        except ApprovalRequired as exc:
            await self._suspend(run, node, spec, state, exc)
            return None
        except PermissionDenied:
            raise

        try:
            config = spec.config_model.model_validate(
                resolve_references(node.config, _ReferenceContext(run, state, node, loop_index))
            )
        except Exception as exc:
            raise NodeExecutionFailed(f"node {node.id} config invalid: {exc}") from exc

        context = NodeContext(
            session=self.session,
            org_id=self.org_id,
            principal=self.principal,
            run_id=run.id,
            node_id=node.id,
            node_name=node.name or spec.name,
            inputs=resolve_references(node.inputs, _ReferenceContext(run, state, node, loop_index)),
            variables=dict(run.variables or {}),
            node_outputs=dict(state.node_outputs),
            project_id=scope.project_id,
            well_id=scope.well_id,
            wellbore_id=scope.wellbore_id,
            section_id=scope.section_id,
            operation_id=scope.operation_id,
            loop_index=loop_index,
            approval_payload=self._approval_payload(run, node),
            services=self.services,
            dry_run=run.is_dry_run,
        )
        if spec.executor is None:
            raise WorkflowDefinitionInvalid(f"node type {spec.key!r} has no executor")

        max_attempts = 1
        backoff = 0.0
        if node.retry:
            max_attempts = int(node.retry.get("max_attempts", 1))
            backoff = float(node.retry.get("backoff_seconds", 0.0))

        # A node that was waiting for approval already has a NodeRun row recording the wait. On
        # resume that same row is completed instead of leaving a dangling "waiting_approval" entry
        # beside the real execution (the UI shows one row per node execution).
        node_run = (
            await self.session.execute(
                select(NodeRun)
                .where(NodeRun.run_id == run.id, NodeRun.node_id == node.id, NodeRun.status == "waiting_approval")
                .order_by(NodeRun.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if node_run is not None:
            node_run.status = "running"
            node_run.inputs = context.inputs
            node_run.resolution = "approved"
            node_run.started_at = dt.datetime.now(tz=UTC)
        else:
            node_run = NodeRun(
                org_id=self.org_id,
                run_id=run.id,
                node_id=node.id,
                node_type=node.type,
                node_name=node.name or spec.name,
                status="running",
                attempt=1,
                max_attempts=max_attempts,
                inputs=context.inputs,
                logs=[],
                metrics={},
                branch_taken=None,
                loop_index=loop_index,
                resolution=None,
                is_breakpoint=node.is_breakpoint,
                started_at=dt.datetime.now(tz=UTC),
            )
            self.session.add(node_run)
        await self.session.flush()
        self._last_node_run = node_run
        await self._event(run, "node_started", message=f"{node.id} ({spec.key})", node_id=node.id, node_run_id=node_run.id)

        tracer = get_tracer()
        with log_context(workflow_run=run.id, workflow_node=node.id, node_type=spec.key):
            for attempt in range(1, max_attempts + 1):
                node_run.attempt = attempt
                with tracer.span(
                    f"workflow.node.{spec.key}",
                    attributes={"workflow.run_id": run.id, "workflow.node_id": node.id, "node.attempt": attempt},
                ) as span:
                    started = time.perf_counter()
                    try:
                        outcome = await asyncio.wait_for(
                            spec.executor(context, config),
                            timeout=node.timeout_seconds or self.node_timeout_seconds,
                        )
                    except TimeoutError:
                        # A timeout is an outcome of the run, not a programming error: it is
                        # recorded on the node, the run is marked timed out, and the caller reads
                        # the result from the run — nothing is raised into the caller's stack.
                        node_run.status = "failed"
                        node_run.error = f"timed out after {node.timeout_seconds or self.node_timeout_seconds}s"
                        node_run.error_code = "node.timeout"
                        node_run.finished_at = dt.datetime.now(tz=UTC)
                        node_run.duration_ms = (time.perf_counter() - started) * 1000
                        await self.session.flush()
                        await self._event(run, "node_failed", message=node_run.error, node_id=node.id, node_run_id=node_run.id, level="error")
                        await self._fail_run(run, node, node_run.error, status="timed_out")
                        return None
                    except Exception as exc:
                        span.record_error(exc)
                        node_run.error = str(exc)[:4000]
                        node_run.error_code = type(exc).__name__
                        node_run.finished_at = dt.datetime.now(tz=UTC)
                        node_run.duration_ms = (time.perf_counter() - started) * 1000
                        if attempt < max_attempts:
                            node_run.status = "retrying"
                            await self.session.flush()
                            await self._event(
                                run,
                                "node_retrying",
                                message=f"attempt {attempt} failed: {node_run.error}",
                                node_id=node.id,
                                node_run_id=node_run.id,
                                level="warning",
                            )
                            if backoff:
                                # Bounded, in-process backoff only. Long waits belong in durable
                                # scheduling, not in a sleeping coroutine.
                                await asyncio.sleep(min(backoff * attempt, 30.0))
                            continue
                        node_run.status = "failed"
                        await self.session.flush()
                        await self._event(
                            run, "node_failed", message=node_run.error, node_id=node.id, node_run_id=node_run.id, level="error"
                        )
                        if node.on_error == "continue":
                            await self._event(
                                run,
                                "log",
                                message=f"continuing after failure of {node.id} (on_error=continue)",
                                node_id=node.id,
                                level="warning",
                            )
                            node_run.resolution = "on_error_continue"
                            outcome = NodeOutcome(
                                outputs={"error": node_run.error, "error_code": node_run.error_code, "failed": True}
                            )
                            # The node stays "failed" (it did fail) and its error envelope is kept as
                            # the node's outputs, so downstream conditions can branch on ${node.failed}.
                            node_run.outputs = outcome.outputs
                            await self.session.flush()
                            break
                        await self._fail_run(run, node, node_run.error)
                        return None

                node_run.status = "succeeded"
                node_run.outputs = outcome.outputs
                node_run.branch_taken = outcome.branch
                node_run.duration_ms = (time.perf_counter() - started) * 1000
                node_run.finished_at = dt.datetime.now(tz=UTC)
                node_run.metrics = {"duration_ms": node_run.duration_ms, "attempts": attempt}
                if outcome.notes:
                    node_run.logs = [*list(node_run.logs or []), *outcome.notes]
                if outcome.engine_run_id:
                    node_run.engine_run_id = outcome.engine_run_id
                if outcome.llm_call_id:
                    node_run.llm_call_id = outcome.llm_call_id
                await self.session.flush()
                run.resolved_node_ids = sorted({*(run.resolved_node_ids or []), node.id})
                run.step_count = (run.step_count or 0) + 1
                if outcome.variables:
                    run.variables = {**(run.variables or {}), **outcome.variables}
                    await self._event(
                        run,
                        "variable_set",
                        message=f"{node.id} set {sorted(outcome.variables)}",
                        node_id=node.id,
                        payload={"keys": sorted(outcome.variables)},
                    )
                if outcome.artifacts:
                    await self._persist_artifacts(run, node_run, outcome)
                await self._event(run, "node_succeeded", message=f"{node.id} finished", node_id=node.id, node_run_id=node_run.id)
                self._last_node_run = node_run
                return outcome

        self._last_node_run = node_run
        return outcome

    def _authorize(self, run: WorkflowRun, node: GraphNode, spec: NodeSpec) -> ActionLevel:
        level = ActionLevel(node.action_level) if node.action_level else spec.action_level
        if self.principal.max_action_level.rank < level.rank:
            raise PermissionDenied(
                f"principal ceiling {self.principal.max_action_level} is below node {node.id} level {level.value}",
                details={"node_id": node.id, "node_type": spec.key, "required_level": level.value},
            )
        if level is ActionLevel.AUTHORIZED_AUTOMATION:
            envelope = node.config.get("envelope") or {}
            allowed, reason = envelope_allows(
                envelope,
                key=node.config.get("envelope_key"),
                context={"well_id": run.well_id, "project_id": run.project_id},
            )
            if not allowed:
                raise ApprovalRequired(
                    f"node {node.id} claims autonomous execution but is not inside an envelope: {reason}",
                    details={"node_id": node.id, "reason": reason},
                )
        if level.rank >= ActionLevel.EXECUTE_WITH_APPROVAL.rank:
            approvals = (run.attributes or {}).get("approvals") or {}
            if node.id not in approvals:
                raise ApprovalRequired(f"node {node.id} ({spec.key}) requires approval before execution")
        return level

    async def _suspend(
        self,
        run: WorkflowRun,
        node: GraphNode,
        spec: NodeSpec,
        state: _Traversal,
        error: ApprovalRequired,
    ) -> None:
        payload = error.details or {}
        approval = ApprovalRequest(
            org_id=self.org_id,
            kind="workflow_node",
            subject_kind="workflow_node",
            subject_id=f"{run.id}:{node.id}",
            run_id=run.id,
            node_run_id=None,
            node_id=node.id,
            project_id=run.project_id,
            well_id=run.well_id,
            title=f"Approve {node.name or spec.name}",
            description=f"Node {node.id} ({spec.key}) at action level {spec.action_level.value} requires a decision.",
            request_payload={"node_type": spec.key, "config": node.config, "inputs": node.inputs},
            # ``proposed_action`` and ``risk_notes`` are human-readable text columns (what the
            # approver reads in the inbox); the structured form lives in ``request_payload``.
            proposed_action=json.dumps(
                {"action_level": spec.action_level.value, "node_id": node.id, "node_type": spec.key},
                sort_keys=True,
            ),
            action_level=spec.action_level.value,
            risk_notes=None,
            required_role="approver",
            evidence_refs=[],
            requested_by=self.principal.id,
            requested_by_kind=self.principal.kind,
            requested_at=dt.datetime.now(tz=UTC),
            status="pending",
            notification_state={"state": "pending", "attempts": 0},
            attributes={"reason": payload.get("reason")},
        )
        self.session.add(approval)
        await self.session.flush()
        node_run = NodeRun(
            org_id=self.org_id,
            run_id=run.id,
            node_id=node.id,
            node_type=node.type,
            node_name=node.name or spec.name,
            status="waiting_approval",
            attempt=1,
            max_attempts=1,
            inputs=dict(node.inputs),
            resolution="awaiting_approval",
            started_at=dt.datetime.now(tz=UTC),
        )
        self.session.add(node_run)
        await self.session.flush()
        approval.node_run_id = node_run.id
        run.status = "waiting_approval"
        run.pending_approval_id = approval.id
        run.cursor_node_id = node.id
        run.outputs = {**run.outputs, **{nid: payload for nid, payload in state.node_outputs.items() if payload}}
        await self.session.flush()
        await self._event(
            run,
            "approval_requested",
            message=f"approval {approval.id} requested for {node.id}",
            node_id=node.id,
            node_run_id=node_run.id,
        )
        await self._event(run, "run_paused", message="run suspended pending human decision", node_id=node.id)

    async def _fail_run(self, run: WorkflowRun, node: GraphNode | None, message: str, *, status: str = "failed") -> None:
        run.status = status
        run.error = message
        run.error_node_id = node.id if node else None
        await self.session.flush()
        await self._event(run, "run_failed", message=message, node_id=node.id if node else None, level="error")

    async def _record_remaining_skipped(
        self, run: WorkflowRun, graph: WorkflowGraph, pending: list[tuple[str, int]], *, reason: str
    ) -> None:
        """Record every node that was still queued when the run stopped."""
        nodes = graph.node_map()
        for node_id, _loop_index in pending:
            node = nodes.get(node_id)
            if node is None or node_id in (run.resolved_node_ids or []):
                continue
            existing = (
                await self.session.execute(
                    select(NodeRun).where(NodeRun.run_id == run.id, NodeRun.node_id == node_id)
                )
            ).scalars().all()
            if existing:
                continue
            await self._record_skipped(run, node, reason=reason)

    async def _record_skipped(self, run: WorkflowRun, node: GraphNode, *, reason: str) -> None:
        node_run = NodeRun(
            org_id=self.org_id,
            run_id=run.id,
            node_id=node.id,
            node_type=node.type,
            node_name=node.name,
            status="skipped",
            attempt=0,
            max_attempts=0,
            resolution=reason,
            started_at=dt.datetime.now(tz=UTC),
            finished_at=dt.datetime.now(tz=UTC),
        )
        self.session.add(node_run)
        await self.session.flush()
        await self._event(run, "node_skipped", message=f"{node.id} skipped: {reason}", node_id=node.id, node_run_id=node_run.id)

    async def _persist_artifacts(self, run: WorkflowRun, node_run: NodeRun, outcome: NodeOutcome) -> None:
        for artifact in outcome.artifacts:
            row = RunArtifact(
                org_id=self.org_id,
                run_id=run.id,
                node_run_id=node_run.id,
                node_id=node_run.node_id,
                kind=artifact.get("kind", "document"),
                name=artifact.get("name") or node_run.node_name,
                content_type=artifact.get("content_type"),
                payload=artifact.get("payload") or {},
                recommendation_id=artifact.get("recommendation_id"),
                engine_run_id=artifact.get("engine_run_id"),
                evidence_refs=artifact.get("evidence_refs") or [],
            )
            self.session.add(row)
            await self.session.flush()
            await self._event(
                run,
                "artifact_created",
                message=f"{row.kind} {row.name}",
                node_id=node_run.node_id,
                node_run_id=node_run.id,
                payload={"artifact_id": row.id},
            )

    def _approval_payload(self, run: WorkflowRun, node: GraphNode) -> dict[str, Any] | None:
        approvals = (run.attributes or {}).get("approvals") or {}
        approval_id = approvals.get(node.id)
        if not approval_id:
            return None
        return {"status": "approved", "approval_id": approval_id, "decided_by": run.approved_by}

    async def _finish(self, run: WorkflowRun, started: float) -> None:
        run.finished_at = dt.datetime.now(tz=UTC)
        run.duration_ms = (time.perf_counter() - started) * 1000
        run.metrics = {
            **(run.metrics or {}),
            "steps": run.step_count or 0,
            "duration_ms": run.duration_ms,
        }
        await self.session.flush()

    async def _event(
        self,
        run: WorkflowRun,
        event_type: str,
        *,
        message: str,
        node_id: str | None = None,
        node_run_id: str | None = None,
        payload: dict[str, Any] | None = None,
        level: str = "info",
    ) -> None:
        self._event_seq += 1
        from drillai.observability.tracing import current_trace

        trace = current_trace()
        row = RunEvent(
            org_id=self.org_id,
            run_id=run.id,
            seq=self._event_seq,
            type=event_type,
            node_id=node_id,
            node_run_id=node_run_id,
            message=message,
            payload=payload or {},
            level=level,
            occurred_at=dt.datetime.now(tz=UTC),
            trace_id=trace.trace_id if trace else None,
            span_id=trace.span_id if trace else None,
        )
        self.session.add(row)
        await self.session.flush()


# --------------------------------------------------------------------------- helpers


def _topological(graph: WorkflowGraph) -> list[str]:
    from drillai.workflow.graph import topological_order

    return topological_order(graph)


@dataclass
class _ReferenceContext:
    """Adapter exposing run/variables/node_outputs to the expression module."""

    run: WorkflowRun
    state: _Traversal
    node: GraphNode
    loop_index: int = 0

    @property
    def run_id(self) -> str:
        return self.run.id

    @property
    def node_id(self) -> str:
        return self.node.id

    @property
    def variables(self) -> dict[str, Any]:
        return self.run.variables or {}

    @property
    def inputs(self) -> dict[str, Any]:
        return self.run.inputs or {}

    @property
    def node_outputs(self) -> dict[str, dict[str, Any]]:
        return self.state.node_outputs


@dataclass
class _ConditionContext:
    """Adapter for an edge condition: it may inspect the source node's outcome."""

    outcome: NodeOutcome
    state: _Traversal

    @property
    def run_id(self) -> str:
        return ""

    @property
    def node_id(self) -> str:
        return ""

    @property
    def loop_index(self) -> int:
        return 0

    @property
    def variables(self) -> dict[str, Any]:
        return {}

    @property
    def inputs(self) -> dict[str, Any]:
        return {"result": self.outcome.outputs}

    @property
    def node_outputs(self) -> dict[str, dict[str, Any]]:
        return {"result": self.outcome.outputs, **self.state.node_outputs}


def new_run_id() -> str:
    return new_id("wrn")


class RunSummary(BaseModel):
    run_id: str
    status: str
    steps: int
    duration_ms: float | None
    error: str | None = None
    pending_approval_id: str | None = None
    cursor_node_id: str | None = None
