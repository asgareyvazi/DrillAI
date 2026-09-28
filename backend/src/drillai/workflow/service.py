"""Workflow definition lifecycle: create, version, validate, publish, fork.

Versioning rules that matter in practice:

* a published version is **immutable** — editing produces a new version, so a run can always be
  reproduced from the version it recorded;
* publishing requires a valid graph (zero errors); warnings are stored on the version and shown
  in the editor rather than blocking a deliberate choice;
* the graph hash is computed from the graph itself (``WorkflowVersion.graph_hash``), so
  "same graph, different id" is detectable and a duplicate save is a no-op instead of a new row;
* forking a system-default template produces an org-owned editable copy with ``forked_from_id``
  set, which is how "everything default, everything editable" is enforced structurally.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import UTC
from drillai.core.errors import Conflict, NotFound, ValidationFailed, WorkflowDefinitionInvalid
from drillai.db.models import Workflow, WorkflowRun, WorkflowVersion
from drillai.security.actions import Principal
from drillai.workflow.graph import ValidationReport, WorkflowGraph, graph_summary, validate_graph
from drillai.workflow.nodes import registered_node_types
from drillai.workflow.runtime import RunScope, WorkflowRuntime

__all__ = ["WorkflowService"]


class WorkflowService:
    """Persistence for workflow definitions and versions."""

    def __init__(self, session: AsyncSession, *, org_id: str, principal: Principal) -> None:
        self.session = session
        self.org_id = org_id
        self.principal = principal

    # ------------------------------------------------------------------ definitions
    async def create(
        self,
        *,
        key: str,
        name: str,
        graph: WorkflowGraph | None = None,
        description: str | None = None,
        category: str | None = None,
        domain_pack: str = "drilling",
        project_id: str | None = None,
        tags: list[str] | None = None,
        is_template: bool = False,
    ) -> Workflow:
        existing = (
            await self.session.execute(
                select(Workflow).where(Workflow.org_id == self.org_id, Workflow.key == key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise Conflict(f"workflow key {key!r} already exists", details={"workflow_id": existing.id})
        workflow = Workflow(
            org_id=self.org_id,
            key=key,
            name=name,
            description=description,
            category=category,
            domain_pack=domain_pack,
            project_id=project_id,
            status="draft",
            current_version=0,
            is_template=is_template,
            is_editable=True,
            tags=tags or [],
            created_by=self.principal.id,
            permissions=["workflow.read", "workflow.edit"],
            action_level="L2",
        )
        self.session.add(workflow)
        await self.session.flush()
        if graph is not None:
            await self.save_version(workflow.id, graph, notes="initial version")
        return workflow

    async def save_version(
        self,
        workflow_id: str,
        graph: WorkflowGraph,
        *,
        notes: str | None = None,
        change_reason: str | None = None,
        publish: bool = False,
    ) -> WorkflowVersion:
        workflow = await self._workflow(workflow_id)
        report = validate_graph(graph, node_types=registered_node_types())
        if publish and not report.is_valid:
            raise WorkflowDefinitionInvalid(
                "cannot publish an invalid workflow",
                details={"issues": [issue.model_dump() for issue in report.errors]},
            )
        graph_hash = graph.hash()
        latest = (
            await self.session.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == workflow_id)
                .order_by(WorkflowVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is not None and latest.graph_hash == graph_hash:
            # Saving an unchanged graph is a no-op: version numbers must mean something.
            return latest
        version = WorkflowVersion(
            org_id=self.org_id,
            workflow_id=workflow_id,
            version=(latest.version if latest else 0) + 1,
            graph=graph.model_dump(mode="json", exclude_none=True),
            graph_hash=graph_hash,
            notes=notes,
            change_reason=change_reason,
            validation=report.model_dump(mode="json"),
            node_count=report.node_count,
            edge_count=report.edge_count,
            created_by=self.principal.id,
        )
        self.session.add(version)
        await self.session.flush()
        workflow.current_version = version.version
        if publish:
            self._publish(workflow, version)
        await self.session.flush()
        return version

    async def publish(self, workflow_id: str, version: int | None = None) -> WorkflowVersion:
        workflow = await self._workflow(workflow_id)
        version_row = await self.get_version(workflow_id, version)
        report = validate_graph(
            WorkflowGraph.model_validate(version_row.graph), node_types=registered_node_types()
        )
        if not report.is_valid:
            raise WorkflowDefinitionInvalid(
                "cannot publish an invalid workflow",
                details={"issues": [issue.model_dump() for issue in report.errors]},
            )
        self._publish(workflow, version_row)
        await self.session.flush()
        return version_row

    def _publish(self, workflow: Workflow, version: WorkflowVersion) -> None:
        version.published_at = dt.datetime.now(tz=UTC)
        version.published_by = self.principal.id
        workflow.published_version_id = version.id
        workflow.status = "published"
        workflow.current_version = version.version

    async def fork(
        self,
        workflow_id: str,
        *,
        key: str,
        name: str,
        project_id: str | None = None,
    ) -> Workflow:
        """Copy any workflow (including a system default) into an editable org-owned workflow."""
        source = await self._workflow(workflow_id)
        graph = await self.get_graph(workflow_id)
        forked = await self.create(
            key=key,
            name=name,
            graph=graph,
            description=f"Fork of {source.key}",
            category=source.category,
            domain_pack=source.domain_pack,
            project_id=project_id or source.project_id,
            tags=list(source.tags or []),
        )
        forked.forked_from_id = source.id
        await self.session.flush()
        return forked

    # ------------------------------------------------------------------ reads
    async def _workflow(self, workflow_id: str) -> Workflow:
        workflow = (
            await self.session.execute(
                select(Workflow).where(Workflow.id == workflow_id, Workflow.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if workflow is None:
            raise NotFound(f"workflow {workflow_id!r} not found")
        return workflow

    async def get_version(self, workflow_id: str, version: int | None = None) -> WorkflowVersion:
        stmt = select(WorkflowVersion).where(WorkflowVersion.workflow_id == workflow_id)
        if version is None:
            workflow = await self._workflow(workflow_id)
            if workflow.published_version_id:
                stmt = stmt.where(WorkflowVersion.id == workflow.published_version_id)
            else:
                stmt = stmt.order_by(WorkflowVersion.version.desc()).limit(1)
        else:
            stmt = stmt.where(WorkflowVersion.version == version)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFound(f"workflow {workflow_id!r} has no matching version")
        return row

    async def get_graph(self, workflow_id: str, version: int | None = None) -> WorkflowGraph:
        row = await self.get_version(workflow_id, version)
        return WorkflowGraph.model_validate(row.graph)

    async def validate(self, graph: WorkflowGraph) -> ValidationReport:
        return validate_graph(graph, node_types=registered_node_types())

    async def start_run(
        self,
        workflow_id: str,
        *,
        scope: RunScope | None = None,
        inputs: dict[str, Any] | None = None,
        version: int | None = None,
        trigger_type: str = "manual",
        trigger_ref: str | None = None,
        is_dry_run: bool = False,
        services: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        """Run a saved workflow, recording the version it executed.

        Resolution order is published version, then latest: an operator pressing *run* means the
        released definition, while a draft run (preview) passes an explicit ``version``.
        """
        workflow = await self._workflow(workflow_id)
        version_row = await self.get_version(workflow_id, version)
        graph = WorkflowGraph.model_validate(version_row.graph)
        runtime = WorkflowRuntime(
            self.session,
            org_id=self.org_id,
            principal=self.principal,
            services=services,
        )
        return await runtime.start(
            graph,
            workflow_id=workflow.id,
            workflow_version_id=version_row.id,
            workflow_key=workflow.key,
            version=version_row.version,
            scope=scope,
            inputs=inputs,
            trigger_type=trigger_type,
            trigger_ref=trigger_ref,
            is_dry_run=is_dry_run,
        )

    async def list_workflows(self, *, project_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        stmt = select(Workflow).where(Workflow.org_id == self.org_id).order_by(Workflow.name).limit(limit)
        if project_id:
            stmt = stmt.where(Workflow.project_id == project_id)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [
            {
                "id": row.id,
                "key": row.key,
                "name": row.name,
                "status": row.status,
                "category": row.category,
                "current_version": row.current_version,
                "published_version_id": row.published_version_id,
                "is_template": row.is_template,
                "is_system_default": row.is_system_default,
                "is_editable": row.is_editable,
                "tags": list(row.tags or []),
            }
            for row in rows
        ]

    async def history(self, workflow_id: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = (
            await self.session.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == workflow_id)
                .order_by(WorkflowVersion.version.desc())
                .limit(limit)
            )
        ).scalars().all()
        return [
            {
                "id": row.id,
                "version": row.version,
                "graph_hash": row.graph_hash,
                "node_count": row.node_count,
                "edge_count": row.edge_count,
                "published_at": row.published_at.isoformat() if row.published_at else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "created_by": row.created_by,
                "change_reason": row.change_reason,
                "notes": row.notes,
                "validation_errors": len(
                    [issue for issue in (row.validation or {}).get("issues", []) if issue.get("severity") == "error"]
                ),
            }
            for row in rows
        ]

    async def summarise(self, workflow_id: str, version: int | None = None) -> dict[str, Any]:
        graph = await self.get_graph(workflow_id, version)
        return graph_summary(graph, validate_graph(graph, node_types=registered_node_types()))


def ensure_valid(report: ValidationReport) -> None:
    if not report.is_valid:
        raise ValidationFailed(
            "workflow definition is invalid",
            details={"issues": [issue.model_dump() for issue in report.errors]},
        )
