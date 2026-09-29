"""Run inspection, approvals and the live event stream.

A run is the unit of accountability: it pins the definition version, records every node attempt,
every engine run and every approval, and can be resumed after a restart because its routing state
is persisted. These endpoints expose that state — including *why* a node was skipped — and the
approval decision path, which is the only place a suspended run may continue.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, current_auth, get_db, require
from drillai.api.serializers import approval_out, node_run_out, run_event_out, run_out
from drillai.core.clock import UTC
from drillai.core.errors import Conflict, NotFound, ValidationFailed
from drillai.db.models import ApprovalRequest, NodeRun, RunArtifact, RunEvent, Workflow, WorkflowRun
from drillai.security.actions import authorize
from drillai.workflow.graph import WorkflowGraph
from drillai.workflow.runtime import WorkflowRuntime
from drillai.workflow.service import WorkflowService

router = APIRouter(tags=["runs"])

RUN_STATUSES = ("queued", "running", "succeeded", "failed", "timed_out", "paused", "waiting_approval", "cancelled")


class ApprovalDecision(BaseModel):
    """A recorded human decision.

    ``note`` is the justification and is echoed back as ``decision_note`` (the server's two names for
    the two directions); ``conditions`` are the terms the decision is granted under. Both are part of
    the audit record, so both are typed here rather than left to whatever a client sends: a condition
    is a statement, and the stored column holds a list of them.
    """

    decision: str = Field(pattern="^(approved|rejected)$")
    note: str | None = Field(default=None, max_length=2000)
    conditions: list[str] = Field(
        default_factory=list, description="Conditions the approval is granted under"
    )
    resume: bool = Field(default=True, description="Continue the suspended run immediately")


@router.get("/runs", summary="List runs")
async def list_runs(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
    status: OptionalFilter = None,
    well_id: OptionalFilter = None,
    project_id: OptionalFilter = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    stmt = select(WorkflowRun).where(WorkflowRun.org_id == auth.org_id)
    if status:
        stmt = stmt.where(WorkflowRun.status == status)
    if well_id:
        stmt = stmt.where(WorkflowRun.well_id == well_id)
    if project_id:
        stmt = stmt.where(WorkflowRun.project_id == project_id)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await session.execute(stmt.order_by(WorkflowRun.created_at.desc()).limit(limit).offset(offset))
    ).scalars().all()
    return {"items": [run_out(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/runs/{run_id}", summary="One run: nodes, events, artefacts, outputs")
async def get_run(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
    include_events: bool = True,
    include_node_payloads: bool = True,
) -> dict[str, Any]:
    run = await _get_run(session, auth, run_id)
    payload: dict[str, Any] = {"run": run_out(run)}
    workflow = (
        await session.execute(select(Workflow).where(Workflow.id == run.workflow_id))
    ).scalar_one_or_none()
    payload["workflow"] = (
        {"id": workflow.id, "key": workflow.key, "name": workflow.name, "status": workflow.status}
        if workflow is not None
        else None
    )
    node_runs = (
        await session.execute(select(NodeRun).where(NodeRun.run_id == run_id).order_by(NodeRun.created_at))
    ).scalars().all()
    payload["node_runs"] = [
        _node_run_payload(row, include_payloads=include_node_payloads) for row in node_runs
    ]
    artifacts = (
        await session.execute(select(RunArtifact).where(RunArtifact.run_id == run_id))
    ).scalars().all()
    payload["artifacts"] = [
        {
            "id": row.id,
            "node_id": row.node_id,
            "node_run_id": row.node_run_id,
            "kind": row.kind,
            "name": row.name,
            "content_type": row.content_type,
            "row_count": row.row_count,
            "byte_size": row.byte_size,
            "evidence_refs": list(row.evidence_refs or []),
            "document_id": row.document_id,
            "recommendation_id": row.recommendation_id,
            "engine_run_id": row.engine_run_id,
            "payload": row.payload,
        }
        for row in artifacts
    ]
    if include_events:
        events = (
            await session.execute(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq))
        ).scalars().all()
        payload["events"] = [run_event_out(row) for row in events]
    if run.pending_approval_id:
        approval = (
            await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == run.pending_approval_id))
        ).scalar_one_or_none()
        payload["pending_approval"] = approval_out(approval) if approval is not None else None
    payload["resumable"] = run.status in {"waiting_approval", "paused"}
    return payload


@router.get("/runs/{run_id}/nodes", summary="Node runs of a run")
async def run_nodes(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
) -> dict[str, Any]:
    await _get_run(session, auth, run_id)
    rows = (
        await session.execute(select(NodeRun).where(NodeRun.run_id == run_id).order_by(NodeRun.created_at))
    ).scalars().all()
    return {"items": [node_run_out(row) for row in rows], "total": len(rows)}


@router.get("/runs/{run_id}/events", summary="Event log of a run")
async def run_events(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
    after_seq: int = Query(default=0, ge=0, description="Return events with a higher sequence number"),
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict[str, Any]:
    await _get_run(session, auth, run_id)
    rows = (
        await session.execute(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
            .order_by(RunEvent.seq)
            .limit(limit)
        )
    ).scalars().all()
    return {"items": [run_event_out(row) for row in rows], "total": len(rows), "after_seq": after_seq}


@router.post("/runs/{run_id}/resume", summary="Resume a suspended run")
async def resume_run(
    run_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
    approval_id: str | None = None,
) -> dict[str, Any]:
    """Continue a run that is waiting for approval.

    Resuming requires the approval to have been *decided*: the runtime refuses to continue on a
    pending one, so there is no path that executes an unapproved action.
    """
    authorize(auth.principal, "workflow.run")
    run = await _get_run(session, auth, run_id)
    if run.status not in {"waiting_approval", "paused"}:
        raise Conflict(
            f"run {run_id!r} is {run.status} and cannot be resumed",
            details={"status": run.status, "resumable": ["waiting_approval", "paused"]},
        )
    effective_approval = approval_id or run.pending_approval_id
    if effective_approval is None:
        raise ValidationFailed("no approval is associated with this run and none was supplied")
    approval = (
        await session.execute(select(ApprovalRequest).where(ApprovalRequest.id == effective_approval))
    ).scalar_one_or_none()
    if approval is None:
        raise NotFound(f"approval {effective_approval!r} not found")
    if approval.status == "pending":
        raise Conflict(
            "the approval has not been decided yet",
            details={"approval_id": approval.id, "status": approval.status},
        )
    service = WorkflowService(session, org_id=auth.org_id or "", principal=auth.principal)
    graph_row = await service.get_version(run.workflow_id, run.version)
    runtime = WorkflowRuntime(session, org_id=auth.org_id or "", principal=auth.principal)
    resumed = await runtime.resume(
        run_id,
        WorkflowGraph.model_validate(graph_row.graph),
        approval_id=approval.id,
        decision=approval.status,
        decided_by=approval.decided_by,
    )
    return run_out(resumed)


# --------------------------------------------------------------------------- approvals


@router.get("/approvals", summary="Approval inbox")
async def list_approvals(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
    status: str = Query(default="pending"),
    run_id: OptionalFilter = None,
    well_id: OptionalFilter = None,
    project_id: OptionalFilter = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Approval requests, filtered by state and scope.

    ``status="any"`` is what a run monitor uses: the approval a run went through has to remain
    readable *after* the decision, because the note and the conditions are the record of why the run
    continued (or did not). ``run_id`` scopes that read to the run being displayed.
    """
    stmt = select(ApprovalRequest).where(ApprovalRequest.org_id == auth.org_id)
    if status != "any":
        stmt = stmt.where(ApprovalRequest.status == status)
    if run_id:
        stmt = stmt.where(ApprovalRequest.run_id == run_id)
    if well_id:
        stmt = stmt.where(ApprovalRequest.well_id == well_id)
    if project_id:
        stmt = stmt.where(ApprovalRequest.project_id == project_id)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await session.execute(stmt.order_by(ApprovalRequest.requested_at.desc()).limit(limit).offset(offset))
    ).scalars().all()
    now = dt.datetime.now(tz=UTC)
    return {
        "items": [{**approval_out(row), "overdue": bool(row.expires_at and row.expires_at < now)} for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/approvals/{approval_id}", summary="One approval request")
async def get_approval(
    approval_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
) -> dict[str, Any]:
    approval = (
        await session.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.id == approval_id, ApprovalRequest.org_id == auth.org_id
            )
        )
    ).scalar_one_or_none()
    if approval is None:
        raise NotFound(f"approval {approval_id!r} not found")
    payload: dict[str, Any] = {"approval": approval_out(approval)}
    if approval.run_id:
        run = (
            await session.execute(select(WorkflowRun).where(WorkflowRun.id == approval.run_id))
        ).scalar_one_or_none()
        if run is not None:
            payload["run"] = run_out(run)
            nodes = (
                await session.execute(
                    select(NodeRun).where(NodeRun.run_id == run.id).order_by(NodeRun.created_at)
                )
            ).scalars().all()
            payload["upstream_node_runs"] = [
                node_run_out(node) for node in nodes if node.status == "succeeded"
            ]
    return payload


@router.post("/approvals/{approval_id}/decide", summary="Record an approval decision")
async def decide_approval(
    approval_id: str,
    payload: ApprovalDecision,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
) -> dict[str, Any]:
    """Decide a pending approval, optionally resuming the suspended run.

    Controls applied here: the caller must hold ``action:approval.decide`` and an action-level
    ceiling of at least L4; the approval must still be pending; and the decider may not be the
    requester (separation of duties) unless they are an administrator acting on their own run —
    which is refused rather than warned about.
    """
    authorize(auth.principal, "approval.decide")
    approval = (
        await session.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.id == approval_id, ApprovalRequest.org_id == auth.org_id
            )
        )
    ).scalar_one_or_none()
    if approval is None:
        raise NotFound(f"approval {approval_id!r} not found")
    if approval.status != "pending":
        raise Conflict(
            f"approval {approval_id!r} was already {approval.status}",
            details={"status": approval.status, "decided_by": approval.decided_by},
        )
    if approval.requested_by == auth.principal.id:
        raise Conflict(
            "the requester cannot decide their own approval request (separation of duties)",
            details={"requested_by": approval.requested_by, "principal": auth.principal.id},
        )
    approval.status = payload.decision
    approval.decided_by = auth.principal.id
    approval.decided_at = dt.datetime.now(tz=UTC)
    approval.decision_note = payload.note
    approval.conditions = payload.conditions
    approval.notification_state = {**(approval.notification_state or {}), "decision": payload.decision}
    await session.flush()

    resumed: dict[str, Any] | None = None
    if payload.resume and payload.decision == "approved" and approval.run_id:
        run = (
            await session.execute(select(WorkflowRun).where(WorkflowRun.id == approval.run_id))
        ).scalar_one_or_none()
        if run is not None and run.status in {"waiting_approval", "paused"}:
            authorize(auth.principal, "workflow.run")
            service = WorkflowService(session, org_id=auth.org_id or "", principal=auth.principal)
            version_row = await service.get_version(run.workflow_id, run.version)
            runtime = WorkflowRuntime(session, org_id=auth.org_id or "", principal=auth.principal)
            resumed_row = await runtime.resume(
                run.id,
                WorkflowGraph.model_validate(version_row.graph),
                approval_id=approval.id,
                decision=payload.decision,
                decided_by=auth.principal.id,
            )
            resumed = run_out(resumed_row)
    elif payload.resume and payload.decision == "rejected" and approval.run_id:
        run = (
            await session.execute(select(WorkflowRun).where(WorkflowRun.id == approval.run_id))
        ).scalar_one_or_none()
        if run is not None and run.status in {"waiting_approval", "paused"}:
            run.status = "cancelled"
            run.finished_at = dt.datetime.now(tz=UTC)
            run.error = f"approval {approval.id} was rejected by {auth.principal.id}"
            session.add(
                RunEvent(
                    org_id=auth.org_id or "",
                    run_id=run.id,
                    seq=await _next_seq(session, run.id),
                    type="run_cancelled",
                    node_id=approval.node_id,
                    message=run.error,
                    level="warning",
                    occurred_at=dt.datetime.now(tz=UTC),
                )
            )
            await session.flush()
            resumed = run_out(run)
    return {"approval": approval_out(approval), "resumed_run": resumed}


# --------------------------------------------------------------------------- websocket


@router.websocket("/runs/{run_id}/events/stream")
async def stream_run_events(websocket: WebSocket, run_id: str, after_seq: int = 0) -> None:
    """Stream run events over WebSocket.

    The durable log is the ``run_events`` table; this socket tails it and pushes new rows. That is
    deliberate: a dropped connection loses the stream, never the history, and a client that
    reconnects with ``after_seq`` resumes without gaps. Authentication reuses the HTTP principal —
    a browser passes the bearer token as a query parameter because the WebSocket API cannot set
    headers, and in development the identity may arrive the same way.

    Frames: ``stream_opened`` once, then one ``run_event`` per row in sequence order, then
    ``stream_closed`` with the terminal status (the socket closes), or ``stream_idle`` after a long
    quiet period so a client can reconnect. A refusal closes with 4401 (not authorized), 4403
    (``workflow.read`` missing) or 4404 (run not found).
    """
    await websocket.accept()
    settings = websocket.app.state.settings
    database = websocket.app.state.database
    token = websocket.query_params.get("token")
    # Headers first (a native client can set them), then the query string, which is the only channel a
    # browser has during a WebSocket handshake. `current_auth` accepts the latter in development only.
    dev_roles = websocket.headers.get("x-dev-roles") or websocket.query_params.get("dev_roles")
    principal_ok = False
    try:
        async with database.session() as session:
            headers = {}
            if token:
                headers["authorization"] = f"Bearer {token}"
            if dev_roles:
                headers["x-dev-roles"] = dev_roles
            request = _HeaderShim(headers, dict(websocket.query_params))
            auth = await current_auth(request, session)  # type: ignore[arg-type]
            if not auth.principal.has_permission("workflow.read"):
                await websocket.close(code=4403, reason="workflow.read is required")
                return
            run = (
                await session.execute(
                    select(WorkflowRun).where(
                        WorkflowRun.id == run_id, WorkflowRun.org_id == auth.org_id
                    )
                )
            ).scalar_one_or_none()
            if run is None:
                await websocket.close(code=4404, reason="run not found")
                return
            principal_ok = True
            await websocket.send_json(
                {"type": "stream_opened", "run_id": run_id, "status": run.status, "after_seq": after_seq}
            )
    except Exception as exc:  # authentication/authorization failures close the socket
        await websocket.close(code=4401, reason=f"not authorized: {exc}")
        return
    if not principal_ok:  # pragma: no cover - defensive
        return

    sequence = after_seq
    idle_ticks = 0
    disconnect = asyncio.create_task(_watch_client_disconnect(websocket))
    try:
        while True:
            # Wait for the poll interval *or* the client leaving, whichever happens first: a browser
            # that closes the tab must free its stream immediately, not at the next tick.
            done, _pending = await asyncio.wait(
                {disconnect}, timeout=settings.run_event_stream_poll_seconds
            )
            if disconnect in done:
                return
            async with database.session() as session:
                rows = (
                    await session.execute(
                        select(RunEvent)
                        .where(RunEvent.run_id == run_id, RunEvent.seq > sequence)
                        .order_by(RunEvent.seq)
                        .limit(200)
                    )
                ).scalars().all()
                run = (
                    await session.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))
                ).scalar_one_or_none()
                for row in rows:
                    sequence = max(sequence, row.seq)
                    await websocket.send_json({"type": "run_event", "event": run_event_out(row)})
                if run is not None and run.status in {"succeeded", "failed", "timed_out", "cancelled"}:
                    await websocket.send_json({"type": "stream_closed", "status": run.status})
                    await websocket.close()
                    return
                idle_ticks = idle_ticks + 1 if not rows else 0
                if idle_ticks > 300:  # ~5 minutes without activity: let the client reconnect
                    await websocket.send_json({"type": "stream_idle", "last_seq": sequence})
                    idle_ticks = 0
    except WebSocketDisconnect:
        return
    except Exception as exc:  # pragma: no cover - transport level
        with_json = json.dumps({"type": "stream_error", "message": str(exc)})
        with contextlib.suppress(Exception):
            await websocket.send_text(with_json)
            await websocket.close(code=1011)
    finally:
        # Stop the watcher, and wait for it without re-raising its own cancellation into this frame:
        # `asyncio.wait` returns the task's state instead of propagating it, so the only cancellation
        # that can travel out of here is a genuine cancellation *of this handler* — which must not be
        # swallowed by cleanup code.
        disconnect.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait({disconnect})


async def _watch_client_disconnect(websocket: WebSocket) -> None:
    """Return as soon as the client goes away.

    A handler that only *sends* cannot notice a dropped browser: nothing fails until the next write,
    so an abandoned socket would keep reading the event log for a run nobody is watching — a task
    that outlives its client, polling the database once per interval, indefinitely. Reading the socket
    is what makes the disconnect observable, and it is noticed within one poll rather than never.

    Any receive failure means the same thing here (the transport is gone), and the caller treats the
    completion of this task as the end of the stream.
    """
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except Exception:  # pragma: no cover - transport level; a failed receive is a dead client
        return


class _HeaderShim:
    """Minimal request-like object so the HTTP auth dependency can be reused for WebSockets.

    A WebSocket handshake cannot carry arbitrary request headers from a browser, so the query string
    is part of the shim: that is where a browser hands over its bearer token, and — in development
    only — the identity it wants to act as. The dependency itself decides whether either is trusted.
    """

    def __init__(self, headers: dict[str, str], query: dict[str, str] | None = None) -> None:
        self.headers = headers
        self.query_params = query or {}

    @property
    def url(self) -> Any:  # pragma: no cover - only used by error handlers
        return None


async def _get_run(session: AsyncSession, auth: AuthContext, run_id: str) -> WorkflowRun:
    run = (
        await session.execute(
            select(WorkflowRun).where(WorkflowRun.id == run_id, WorkflowRun.org_id == auth.org_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise NotFound(f"run {run_id!r} not found")
    return run


def _node_run_payload(row: NodeRun, *, include_payloads: bool) -> dict[str, Any]:
    payload = node_run_out(row)
    if not include_payloads:
        payload.pop("inputs", None)
        payload.pop("outputs", None)
    payload["skipped_or_failed_reason"] = row.error
    payload["notes"] = (row.attributes or {}).get("notes")
    return payload


async def _next_seq(session: AsyncSession, run_id: str) -> int:
    current = (
        await session.execute(
            select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
        )
    ).scalar_one()
    return int(current or 0) + 1
