"""Tool registry and execution.

A *tool* is a typed, permissioned capability that a workflow node, a skill or an agent can call.
Tools are the only way an LLM-driven component may touch the platform: the model chooses which
tool to use and with what arguments, the platform decides whether that is allowed and records
what happened. This is the boundary that keeps "the model decided to change the mud weight"
from being an unexplainable event.

Every invocation:

* resolves the tool and validates its inputs against a JSON schema;
* authorizes it through :mod:`drillai.security.actions` (permission + action level + approval);
* executes it with a timeout;
* records a ``ToolCall`` row (inputs, outputs, side effects, action level, approval id, timing,
  idempotency key) whether it succeeds or fails.

Tools that reach outside the platform are registered at ``L4`` (approval required) unless an org
explicitly enables an L5 envelope; the default is the safe one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import inspect
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import UTC
from drillai.core.errors import NodeTimeout, PermissionDenied, ValidationFailed
from drillai.core.logging import get_logger
from drillai.observability.tracing import gen_ai_attributes, get_tracer
from drillai.security.actions import ActionLevel, Principal, authorize, register_action

logger = get_logger(__name__)

__all__ = [
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "invoke_tool",
    "register_tool",
    "registered_tools",
    "tool_catalogue",
    "tool_schemas_for_llm",
]

ToolExecutor = Callable[[Any, Any], Awaitable[Any]]


@dataclass(frozen=True)
class ToolSpec:
    """A callable capability."""

    key: str
    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel] | None = None
    permission: str = ""
    action_level: ActionLevel = ActionLevel.OBSERVE
    side_effects: tuple[str, ...] = ()
    timeout_seconds: float = 60.0
    idempotent: bool = True
    requires_approval: bool = False
    tags: tuple[str, ...] = ()
    version: str = "1.0.0"

    @property
    def resolved_permission(self) -> str:
        return self.permission or f"tool:{self.key}"

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "permission": self.resolved_permission,
            "action_level": self.action_level.value,
            "side_effects": list(self.side_effects),
            "idempotent": self.idempotent,
            "requires_approval": self.requires_approval or self.action_level.rank >= ActionLevel.EXECUTE_WITH_APPROVAL.rank,
            "tags": list(self.tags),
            "inputs": self.input_model.model_json_schema(),
            "outputs": self.output_model.model_json_schema() if self.output_model else None,
        }


@dataclass
class ToolContext:
    """Everything a tool needs, passed explicitly (no import-time globals)."""

    session: AsyncSession
    org_id: str
    principal: Principal
    project_id: str | None = None
    well_id: str | None = None
    wellbore_id: str | None = None
    section_id: str | None = None
    operation_id: str | None = None
    workflow_run_id: str | None = None
    node_run_id: str | None = None
    agent_run_id: str | None = None
    approval_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    key: str
    status: str
    output: dict[str, Any]
    duration_ms: float
    action_level: ActionLevel
    side_effects: list[str] = field(default_factory=list)
    error: str | None = None
    tool_call_id: str | None = None


_TOOLS: dict[str, tuple[ToolSpec, ToolExecutor]] = {}


def register_tool(spec: ToolSpec, executor: ToolExecutor, *, replace: bool = False) -> ToolSpec:
    """Register a tool *and* its security action.

    Registering the action here is what keeps a single source of truth for action levels: the
    authorization gate defaults unknown actions to L4, so a tool that is not in the catalogue
    could never run below approval. ``replace=True`` keeps the action level in step when a tool
    is re-registered (for example by a plugin overriding a default).
    """
    if spec.key in _TOOLS and not replace:
        raise ValidationFailed(f"tool {spec.key!r} is already registered")
    register_action(
        f"tool.{spec.key}",
        spec.action_level,
        f"Invoke the {spec.name} tool",
        permission=spec.resolved_permission,
        replace=True,
    )
    _TOOLS[spec.key] = (spec, executor)
    return spec


def registered_tools() -> dict[str, ToolSpec]:
    return {key: spec for key, (spec, _) in _TOOLS.items()}


def tool_catalogue() -> list[dict[str, Any]]:
    return [spec.describe() for _, (spec, _) in sorted(_TOOLS.items())]


def tool_schemas_for_llm(keys: list[str] | None = None) -> list[dict[str, Any]]:
    """OpenAI-style tool schemas, filtered by key (used for tool-calling models)."""
    selected = keys or list(_TOOLS)
    schemas: list[dict[str, Any]] = []
    for key in selected:
        entry = _TOOLS.get(key)
        if entry is None:
            continue
        spec, _ = entry
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": spec.key,
                    "description": f"{spec.name}: {spec.description} (action level {spec.action_level.value})",
                    "parameters": spec.input_model.model_json_schema(),
                },
            }
        )
    return schemas


async def invoke_tool(
    key: str,
    context: ToolContext,
    payload: dict[str, Any] | BaseModel,
    *,
    idempotency_key: str | None = None,
) -> ToolResult:
    entry = _TOOLS.get(key)
    if entry is None:
        raise ValidationFailed(
            f"tool {key!r} is not registered", details={"known_tools": sorted(_TOOLS)}
        )
    spec, executor = entry

    # 1. authorization before anything else — including input validation, so an unauthorized
    #    caller cannot probe a tool's schema.
    level = authorize(
        context.principal,
        f"tool.{key}",
        approval_id=context.approval_id,
        required_permission=spec.resolved_permission,
        context={"well_id": context.well_id, "project_id": context.project_id},
    )

    data = payload.model_dump() if isinstance(payload, BaseModel) else payload
    try:
        validated = spec.input_model.model_validate(data)
    except Exception as exc:
        raise ValidationFailed(f"tool {key!r} input invalid: {exc}") from exc

    from drillai.db.models import ToolCall

    row = ToolCall(
        org_id=context.org_id,
        tool_key=spec.key,
        tool_version=spec.version,
        status="running",
        agent_run_id=context.agent_run_id,
        workflow_run_id=context.workflow_run_id,
        node_run_id=context.node_run_id,
        well_id=context.well_id,
        inputs=data,
        side_effects=list(spec.side_effects),
        action_level=level.value,
        approval_id=context.approval_id,
        approved_by=context.principal.id if context.approval_id else None,
        permission_decision={
            "allowed": True,
            "permission": spec.resolved_permission,
            "role_keys": list(context.principal.role_keys),
            "level": level.value,
        },
        attempt=1,
        idempotency_key=idempotency_key,
        started_at=dt.datetime.now(tz=UTC),
    )
    context.session.add(row)
    await context.session.flush()

    started = time.perf_counter()
    tracer = get_tracer()
    with tracer.span(
        f"tool.{spec.key}",
        attributes=gen_ai_attributes(operation="execute_tool", tool_name=spec.key, extra={"action.level": level.value}),
    ) as span:
        try:
            result = executor(validated, context)
            output = await asyncio.wait_for(result, timeout=spec.timeout_seconds) if inspect.isawaitable(result) else result
        except TimeoutError as exc:
            row.status = "failed"
            row.error = f"timed out after {spec.timeout_seconds}s"
            row.error_code = "tool.timeout"
            row.finished_at = dt.datetime.now(tz=UTC)
            await context.session.flush()
            span.record_error(row.error)
            raise NodeTimeout(f"tool {key!r} timed out after {spec.timeout_seconds}s") from exc
        except PermissionDenied:
            raise
        except Exception as exc:
            row.status = "failed"
            row.error = str(exc)[:2000]
            row.error_code = "tool.execution_failed"
            row.finished_at = dt.datetime.now(tz=UTC)
            await context.session.flush()
            span.record_error(exc)
            raise
        duration_ms = (time.perf_counter() - started) * 1000
        output_payload = (
            output.model_dump(mode="json") if isinstance(output, BaseModel) else (output if isinstance(output, dict) else {"result": output})
        )
        row.status = "succeeded"
        row.outputs = output_payload
        row.duration_ms = duration_ms
        row.finished_at = dt.datetime.now(tz=UTC)
        await context.session.flush()
        return ToolResult(
            key=spec.key,
            status="succeeded",
            output=output_payload,
            duration_ms=duration_ms,
            action_level=level,
            side_effects=list(spec.side_effects),
            tool_call_id=row.id,
        )


# --------------------------------------------------------------------------- common tools


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchDocumentsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2)
    mode: str = Field(default="hybrid", pattern="^(lexical|semantic|hybrid|metadata)$")
    limit: int = Field(default=10, ge=1, le=50)
    doc_type: str | None = None
    depth_from_si: float | None = None
    depth_to_si: float | None = None
    period_from: dt.datetime | None = None
    period_to: dt.datetime | None = None


class EngineRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine_key: str
    inputs: dict[str, Any]


async def _search_documents(payload: SearchDocumentsInput, context: ToolContext) -> dict[str, Any]:
    from drillai.rag.retriever import RetrievalQuery, retrieve

    query = RetrievalQuery(
        text=payload.query,
        mode=payload.mode,
        limit=payload.limit,
        well_id=context.well_id,
        wellbore_id=context.wellbore_id,
        section_id=context.section_id,
        doc_type=payload.doc_type,
        depth_from_si=payload.depth_from_si,
        depth_to_si=payload.depth_to_si,
        period_from=payload.period_from,
        period_to=payload.period_to,
    )
    result = await retrieve(context.session, context.org_id, query)
    # ``retrieve`` returns a RetrievalResult (hits + mode actually used + notes). Passing the
    # object straight through keeps the caller honest about which mode answered the query.
    return {
        "hits": [hit.model_dump() for hit in result.hits],
        "hit_count": len(result.hits),
        "mode_requested": payload.mode,
        "mode_used": result.mode_used,
        "filters_applied": result.filters_applied,
        "notes": result.notes,
    }


async def _list_wells(_payload: EmptyInput, context: ToolContext) -> dict[str, Any]:
    from sqlalchemy import select

    from drillai.db.models import Well

    stmt = select(Well).where(Well.org_id == context.org_id).order_by(Well.name).limit(100)
    if context.project_id:
        stmt = stmt.where(Well.project_id == context.project_id)
    wells = (await context.session.execute(stmt)).scalars().all()
    return {"wells": [{"id": well.id, "name": well.name, "status": well.status, "uwi": well.uwi} for well in wells]}


async def _run_engine(payload: EngineRunInput, context: ToolContext) -> dict[str, Any]:
    """Run a deterministic engine. This tool is the *only* sanctioned way for an agent to compute.

    The execution is persisted as an ``EngineRun`` (with input/output hashes), so a number an
    agent quotes is reproducible from the audit trail rather than only visible in the chat log.
    """
    from drillai.engines.service import execute_engine

    execution = await execute_engine(
        context.session,
        engine_key=payload.engine_key,
        inputs=payload.inputs,
        org_id=context.org_id,
        subject_kind="well" if context.well_id else "none",
        subject_id=context.well_id or context.workflow_run_id,
        project_id=context.project_id,
        well_id=context.well_id,
        wellbore_id=context.wellbore_id,
        section_id=context.section_id,
        operation_id=context.operation_id,
        input_source_kind="agent",
        input_source_id=context.workflow_run_id or context.agent_run_id,
        triggered_by="agent",
        triggered_by_id=context.principal.id,
        workflow_run_id=context.workflow_run_id,
        node_run_id=context.node_run_id,
        agent_run_id=context.agent_run_id,
    )
    return execution.to_payload()


def install_default_tools() -> None:
    register_tool(
        ToolSpec(
            key="search_documents",
            name="Search documents",
            description="Retrieve passages from well documents with metadata filters (section, depth, period).",
            input_model=SearchDocumentsInput,
            permission="document.read",
            action_level=ActionLevel.OBSERVE,
            tags=("rag", "documents"),
        ),
        _search_documents,
        replace=True,
    )
    register_tool(
        ToolSpec(
            key="list_wells",
            name="List wells",
            description="List wells the caller may see, in the current project when set.",
            input_model=EmptyInput,
            permission="well.read",
            action_level=ActionLevel.OBSERVE,
            tags=("data",),
        ),
        _list_wells,
        replace=True,
    )
    register_tool(
        ToolSpec(
            key="run_engine",
            name="Run engineering engine",
            description=(
                "Execute a deterministic engineering engine and return its outputs, feasibility and "
                "constraint violations. Use this instead of computing numbers yourself."
            ),
            input_model=EngineRunInput,
            permission="engine.run",
            action_level=ActionLevel.DRAFT,
            tags=("engineering",),
        ),
        _run_engine,
        replace=True,
    )


install_default_tools()
