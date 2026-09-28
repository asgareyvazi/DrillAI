"""Workflow definitions: list, inspect, draft, validate, publish, fork, run.

The editor contract is explicit here:

* the palette (``/registry/node-types``) describes every node type with its JSON schema, so a
  client renders a form from the schema rather than from hard-coded knowledge;
* a saved graph is validated server-side (``validate_graph``) *and* the validation report is
  persisted with the version — a definition that cannot run cannot be published;
* saving an unchanged graph is a no-op, so version numbers mean something;
* runs always reference the persisted version they executed.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, get_db, require
from drillai.api.serializers import run_out, workflow_out
from drillai.core.errors import NotFound, ValidationFailed
from drillai.db.models import Workflow, WorkflowRun, WorkflowVersion
from drillai.security.actions import authorize
from drillai.workflow.graph import WorkflowGraph, graph_summary, validate_graph
from drillai.workflow.nodes import registered_node_types
from drillai.workflow.runtime import RunScope
from drillai.workflow.service import WorkflowService

router = APIRouter(tags=["workflows"])


class WorkflowCreate(BaseModel):
    key: str = Field(min_length=2, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    category: str | None = None
    domain_pack: str = "drilling"
    project_id: str | None = None
    tags: list[str] = Field(default_factory=list)
    graph: dict[str, Any] | None = Field(default=None, description="Initial version; omit to start empty")


class GraphSave(BaseModel):
    graph: dict[str, Any]
    notes: str | None = None
    change_reason: str | None = None
    publish: bool = False


class RunStart(BaseModel):
    project_id: str | None = None
    well_id: str | None = None
    wellbore_id: str | None = None
    section_id: str | None = None
    operation_id: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    version: int | None = Field(default=None, description="Run this version; default = published, else latest")
    is_dry_run: bool = False
    trigger_type: str = "manual"


def _validation_payload(stored: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a stored validation report in the shape the API documents.

    ``is_valid`` is a computed field, so every report produced *now* carries it. Rows written before
    that field existed are still valid rows, and answering with a shape the client cannot rely on
    would push the missing boolean onto the UI: it is filled in here instead, from the same rule the
    model uses. The stored row itself is left alone.
    """
    if stored is None:
        return None
    if "is_valid" in stored:
        return stored
    issues = stored.get("issues") or []
    derived = dict(stored)
    derived["is_valid"] = not any(issue.get("severity") == "error" for issue in issues)
    return derived


def _service(session: AsyncSession, auth: AuthContext) -> WorkflowService:
    return WorkflowService(session, org_id=auth.org_id or "", principal=auth.principal)


@router.get("/workflows", summary="List workflow definitions")
async def list_workflows(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
    project_id: OptionalFilter = None,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Workflow)
            .where(Workflow.org_id == auth.org_id)
            .order_by(Workflow.name)
            .limit(limit)
        )
    ).scalars().all()
    items = [workflow_out(row) for row in rows]
    if project_id is not None:
        items = [item for item in items if item["project_id"] == project_id]
    return {"items": items, "total": len(items)}


@router.post("/workflows", summary="Create a workflow definition", status_code=201)
async def create_workflow(
    payload: WorkflowCreate,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "workflow.draft")
    graph = None
    if payload.graph is not None:
        graph = WorkflowGraph.model_validate(payload.graph)
        report = validate_graph(graph, node_types=registered_node_types())
        if not report.is_valid:
            raise ValidationFailed(
                "the initial graph is not valid",
                details={"issues": [issue.model_dump() for issue in report.errors]},
            )
    workflow = await _service(session, auth).create(
        key=payload.key,
        name=payload.name,
        graph=graph,
        description=payload.description,
        category=payload.category,
        domain_pack=payload.domain_pack,
        project_id=payload.project_id,
        tags=payload.tags,
    )
    return workflow_out(workflow)


@router.get("/workflows/{workflow_id}", summary="Workflow definition, latest or published graph")
async def get_workflow(
    workflow_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
    version: int | None = Query(default=None, description="Omit for published, else latest"),
    include_graph: bool = True,
) -> dict[str, Any]:
    service = _service(session, auth)
    workflow = (
        await session.execute(
            select(Workflow).where(Workflow.id == workflow_id, Workflow.org_id == auth.org_id)
        )
    ).scalar_one_or_none()
    if workflow is None:
        raise NotFound(f"workflow {workflow_id!r} not found")
    payload: dict[str, Any] = {"workflow": workflow_out(workflow)}
    version_row = await service.get_version(workflow_id, version)
    payload["version"] = {
        "id": version_row.id,
        "version": version_row.version,
        "graph_hash": version_row.graph_hash,
        "node_count": version_row.node_count,
        "edge_count": version_row.edge_count,
        "published_at": version_row.published_at.isoformat() if version_row.published_at else None,
        "validation": _validation_payload(version_row.validation),
        "notes": version_row.notes,
    }
    if include_graph:
        payload["graph"] = version_row.graph
    payload["summary"] = await service.summarise(workflow_id, version_row.version)
    return payload


@router.get("/workflows/{workflow_id}/versions", summary="Version history")
async def workflow_versions(
    workflow_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    history = await _service(session, auth).history(workflow_id, limit=limit)
    return {"items": history, "total": len(history)}


@router.get("/workflows/{workflow_id}/versions/{version}", summary="One version, with its graph")
async def workflow_version_detail(
    workflow_id: str,
    version: int,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
) -> dict[str, Any]:
    service = _service(session, auth)
    row = await service.get_version(workflow_id, version)
    return {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "version": row.version,
        "graph": row.graph,
        "graph_hash": row.graph_hash,
        "validation": _validation_payload(row.validation),
        "notes": row.notes,
        "change_reason": row.change_reason,
        "published_at": row.published_at.isoformat() if row.published_at else None,
        "published_by": row.published_by,
    }


@router.post("/workflows/validate", summary="Validate a graph without saving it")
async def validate_workflow_graph(
    graph: dict[str, Any],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
) -> dict[str, Any]:
    """Editor-side validation: returns the same report the save path would produce.

    Checking before saving matters because ``${...}`` typos, unreachable nodes and unknown node
    types are cheap to catch here and expensive to discover mid-run.
    """
    parsed = WorkflowGraph.model_validate(graph)
    report = validate_graph(parsed, node_types=registered_node_types())
    return {"validation": report.model_dump(mode="json"), "summary": graph_summary(parsed, report)}


@router.put("/workflows/{workflow_id}/graph", summary="Save a new version of the graph")
async def save_workflow_graph(
    workflow_id: str,
    payload: GraphSave,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "workflow.draft")
    if payload.publish:
        authorize(auth.principal, "workflow.publish")
    graph = WorkflowGraph.model_validate(payload.graph)
    version = await _service(session, auth).save_version(
        workflow_id,
        graph,
        notes=payload.notes,
        change_reason=payload.change_reason,
        publish=payload.publish,
    )
    return {
        "id": version.id,
        "version": version.version,
        "graph_hash": version.graph_hash,
        "node_count": version.node_count,
        "edge_count": version.edge_count,
        "validation": _validation_payload(version.validation),
        "published_at": version.published_at.isoformat() if version.published_at else None,
    }


@router.post("/workflows/{workflow_id}/publish", summary="Publish a version")
async def publish_workflow(
    workflow_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
    version: int | None = None,
) -> dict[str, Any]:
    authorize(auth.principal, "workflow.publish")
    row = await _service(session, auth).publish(workflow_id, version)
    return {
        "id": row.id,
        "workflow_id": workflow_id,
        "version": row.version,
        "graph_hash": row.graph_hash,
        "published_at": row.published_at.isoformat() if row.published_at else None,
    }


@router.post("/workflows/{workflow_id}/fork", summary="Fork into a new editable workflow", status_code=201)
async def fork_workflow(
    workflow_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
    key: str = Query(pattern=r"^[a-z0-9][a-z0-9_.-]*$"),
    name: str | None = None,
    project_id: OptionalFilter = None,
) -> dict[str, Any]:
    """Everything Default, Everything Editable: a default can be inspected, forked and edited."""
    authorize(auth.principal, "workflow.draft")
    service = _service(session, auth)
    source = (
        await session.execute(
            select(Workflow).where(Workflow.id == workflow_id, Workflow.org_id == auth.org_id)
        )
    ).scalar_one_or_none()
    if source is None:
        raise NotFound(f"workflow {workflow_id!r} not found")
    forked = await service.fork(
        workflow_id, key=key, name=name or f"{source.name} (fork)", project_id=project_id
    )
    return workflow_out(forked)


@router.post("/workflows/{workflow_id}/runs", summary="Start a run", status_code=201)
async def start_run(
    workflow_id: str,
    payload: RunStart,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
) -> dict[str, Any]:
    """Start a run of a saved definition.

    Authorization happens twice, at different granularities: starting the run is ``workflow.run``
    (L2), and every node is checked as it executes against its own action level — a node at L4
    suspends the run and raises an approval naming exactly what will be done. The run row records
    the definition version, so the execution is reproducible.
    """
    authorize(auth.principal, "workflow.run")
    run = await _service(session, auth).start_run(
        workflow_id,
        scope=RunScope(
            project_id=payload.project_id,
            well_id=payload.well_id,
            wellbore_id=payload.wellbore_id,
            section_id=payload.section_id,
            operation_id=payload.operation_id,
        ),
        inputs=payload.inputs,
        version=payload.version,
        trigger_type=payload.trigger_type,
        is_dry_run=payload.is_dry_run,
    )
    return run_out(run)


@router.get("/workflows/{workflow_id}/runs", summary="Runs of a workflow")
async def workflow_runs(
    workflow_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
    status: OptionalFilter = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    stmt = select(WorkflowRun).where(
        WorkflowRun.workflow_id == workflow_id, WorkflowRun.org_id == auth.org_id
    )
    if status:
        stmt = stmt.where(WorkflowRun.status == status)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await session.execute(stmt.order_by(WorkflowRun.created_at.desc()).limit(limit).offset(offset))
    ).scalars().all()
    return {"items": [run_out(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/workflows/{workflow_id}/summary", summary="Graph summary (nodes by family, features)")
async def workflow_summary(
    workflow_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
    version: int | None = None,
) -> dict[str, Any]:
    return await _service(session, auth).summarise(workflow_id, version)


@router.get("/workflow-versions/{version_id}/graph", summary="Graph of a version id")
async def graph_by_version_id(
    version_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("workflow.read"))],
) -> dict[str, Any]:
    row = (
        await session.execute(
            select(WorkflowVersion).where(
                WorkflowVersion.id == version_id, WorkflowVersion.org_id == auth.org_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(f"workflow version {version_id!r} not found")
    return {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "version": row.version,
        "graph": row.graph,
        "validation": _validation_payload(row.validation),
    }
