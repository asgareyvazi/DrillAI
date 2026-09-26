"""Registration surfaces: engines, workflow node types, tools, actions, context sections, extractors.

These endpoints are what make the platform's extension points inspectable instead of documented:
a UI (or an integrator) can enumerate exactly what exists, with the JSON schema of every input, the
action level, the declared limitations and the required permission — before calling anything.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, get_db, require
from drillai.core.errors import NotFound
from drillai.engines.service import engine_catalogue, execute_engine
from drillai.security.actions import ACTION_LEVEL_DESCRIPTIONS, ActionLevel, authorize, registered_actions

router = APIRouter(tags=["registry"])


@router.get("/registry/engines", summary="Engine catalogue")
async def list_engines(_: Annotated[AuthContext, Depends(require("registry.read"))]) -> dict[str, Any]:
    engines = engine_catalogue()
    return {"items": engines, "total": len(engines)}


@router.get("/registry/engines/{engine_key}", summary="One engine, with input/output schemas")
async def get_engine(
    engine_key: str, _: Annotated[AuthContext, Depends(require("registry.read"))]
) -> dict[str, Any]:
    from drillai.engines.registry import load_default_engines

    registry = load_default_engines()
    if not registry.has(engine_key):
        raise NotFound(f"engine {engine_key!r} is not registered", details={"known": registry.keys()})
    return registry.get(engine_key).spec.describe()


class EngineRunRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict, description="Canonical SI inputs, validated by the engine.")
    well_id: str | None = None
    wellbore_id: str | None = None
    section_id: str | None = None
    operation_id: str | None = None
    project_id: str | None = None
    persist: bool = Field(default=True, description="Persist the EngineRun audit row (default on).")


@router.post("/registry/engines/{engine_key}/run", summary="Execute an engine on explicit inputs")
async def run_engine(
    engine_key: str,
    payload: EngineRunRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("registry.read"))],
) -> dict[str, Any]:
    """Execute one engine.

    The inputs are *explicit*: nothing is inferred from the well unless the caller passes it, and
    the result carries the engine version, declared limitations, constraint violations and the
    hashes of what went in and out. There is no path here that lets a language model substitute
    for the calculation.
    """
    from drillai.engines.registry import load_default_engines

    registry = load_default_engines()
    if not registry.has(engine_key):
        raise NotFound(f"engine {engine_key!r} is not registered", details={"known": registry.keys()})
    spec = registry.get(engine_key).spec
    authorize(auth.principal, "engine.run", required_permission="engine.run")
    execution = await execute_engine(
        session,
        engine_key=engine_key,
        inputs=payload.inputs,
        org_id=_org(auth),
        subject_kind="well" if payload.well_id else "none",
        subject_id=payload.well_id or _org(auth),
        project_id=payload.project_id,
        well_id=payload.well_id,
        wellbore_id=payload.wellbore_id,
        section_id=payload.section_id,
        operation_id=payload.operation_id,
        input_source_kind="api",
        input_source_id=request.headers.get("X-Request-ID"),
        triggered_by="api",
        triggered_by_id=auth.principal.id,
        registry=registry,
    )
    body = execution.to_payload()
    body["engine"] = {"key": spec.key, "version": spec.version, "validation_status": spec.validation_status}
    return body


@router.get("/registry/node-types", summary="Workflow node type palette")
async def list_node_types(
    _: Annotated[AuthContext, Depends(require("registry.read"))],
) -> dict[str, Any]:
    from drillai.workflow.nodes import node_types_for_ui

    items = node_types_for_ui()
    families: dict[str, list[str]] = {}
    for item in items:
        families.setdefault(item["family"], []).append(item["key"])
    return {"items": items, "families": families, "total": len(items)}


@router.get("/registry/node-types/{node_type}", summary="One node type, with its config schema")
async def get_node_type(
    node_type: str, _: Annotated[AuthContext, Depends(require("registry.read"))]
) -> dict[str, Any]:
    from drillai.workflow.nodes import node_spec

    return node_spec(node_type).describe()


@router.get("/registry/tools", summary="Agent tool catalogue")
async def list_tools(_: Annotated[AuthContext, Depends(require("registry.read"))]) -> dict[str, Any]:
    from drillai.ai.tools import tool_catalogue

    items = tool_catalogue()
    return {"items": items, "total": len(items)}


@router.get("/registry/actions", summary="Action catalogue and level semantics")
async def list_actions(_: Annotated[AuthContext, Depends(require("registry.read"))]) -> dict[str, Any]:
    actions = [
        {
            "key": key,
            "level": action.level.value,
            "description": action.description,
            "permission": action.permission,
        }
        for key, action in sorted(registered_actions().items())
    ]
    return {
        "items": actions,
        "total": len(actions),
        "levels": {
            level.value: ACTION_LEVEL_DESCRIPTIONS.get(level.value, "") for level in ActionLevel
        },
    }


@router.get("/registry/context-sections", summary="Context section catalogue")
async def list_context_sections(
    _: Annotated[AuthContext, Depends(require("registry.read"))],
) -> dict[str, Any]:
    from drillai.context.builder import section_catalogue

    items = section_catalogue()
    return {"items": items, "total": len(items)}


@router.get("/registry/extractors", summary="Document extractor catalogue")
async def list_extractors(
    _: Annotated[AuthContext, Depends(require("registry.read"))],
) -> dict[str, Any]:
    from drillai.ingestion.extractors import extractor_catalogue

    items = extractor_catalogue()
    return {"items": items, "total": len(items)}


@router.get("/registry/units", summary="Unit registry (dimensions, canonical units, conversions)")
async def list_units(
    _: Annotated[AuthContext, Depends(require("registry.read"))], dimension: str | None = None
) -> dict[str, Any]:
    from drillai.units.registry import catalogue

    return catalogue(dimension)


def _org(auth: AuthContext) -> str:
    if auth.org_id is None:  # pragma: no cover - every authenticated request carries an org
        raise NotFound("the authenticated principal has no organisation scope")
    return auth.org_id
