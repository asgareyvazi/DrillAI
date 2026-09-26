"""Digital well twin: aspects, state kinds, snapshots and history.

The twin's contract is that a *planned* casing shoe, an *actual* one, the *current* interpretation,
the *historical* record and a *recommended* or *predicted* value are different rows with different
provenance — never one mutable number. These endpoints expose exactly that structure: reads by
scope, writes that must declare a state kind and a source, snapshot/compare, and change history.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, get_db, require
from drillai.api.serializers import twin_aspect_out
from drillai.core.errors import NotFound, ValidationFailed
from drillai.db.models import Well
from drillai.security.actions import authorize
from drillai.twin.aspects import ASPECTS_BY_KEY, StateKind, aspect_keys
from drillai.twin.service import AspectRevision, TwinService

router = APIRouter(tags=["twin"])


class AspectWrite(BaseModel):
    """Write one aspect revision. ``state_kind`` and ``computed_by`` are mandatory on purpose."""

    aspect: str
    state_kind: StateKind
    payload: dict[str, Any]
    summary: str | None = None
    confidence: Annotated[float | None, Field(ge=0, le=1)] = None
    data_quality: str = "unknown"
    computed_by: str = "user"
    engine_run_id: str | None = None
    source_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    wellbore_id: str | None = None
    section_id: str | None = None
    valid_from: dt.datetime | None = None
    valid_to: dt.datetime | None = None


@router.get("/twin/aspects", summary="Aspect catalogue (state kinds, schema, scope)")
async def twin_aspect_catalogue(
    _: Annotated[AuthContext, Depends(require("twin.read"))],
) -> dict[str, Any]:
    items = [
        {
            "key": definition.key,
            "name": definition.name,
            "description": definition.description,
            "schema_key": definition.schema_key,
            "state_kinds": [kind.value for kind in definition.state_kinds],
            "scope": definition.scope,
            "unit_hint": definition.unit_hint,
        }
        for definition in ASPECTS_BY_KEY.values()
    ]
    return {"items": items, "total": len(items), "state_kinds": [kind.value for kind in StateKind]}


@router.get("/wells/{well_id}/twin", summary="Current twin state for a well")
async def well_twin(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("twin.read"))],
    wellbore_id: OptionalFilter = None,
    section_id: OptionalFilter = None,
    aspect: OptionalFilter = None,
    include_history: bool = Query(default=False, description="Include superseded revisions"),
) -> dict[str, Any]:
    authorize(auth.principal, "twin.read")
    well = (
        await session.execute(select(Well).where(Well.id == well_id, Well.org_id == auth.org_id))
    ).scalar_one_or_none()
    if well is None:
        raise NotFound(f"well {well_id!r} not found")
    service = TwinService(session, org_id=auth.org_id or "")
    aspects = await service.list_aspects(
        well_id=well_id,
        wellbore_id=wellbore_id,
        section_id=section_id,
        aspect_key=aspect,
        current_only=not include_history,
        include_superseded=include_history,
    )
    state = await service.current_state(
        well_id=well_id, wellbore_id=wellbore_id, section_id=section_id
    )
    payload: dict[str, Any] = {
        "well_id": well_id,
        "twin_state": well.twin_state,
        "aspects": [twin_aspect_out(row) for row in aspects],
        # ``current_state`` is keyed aspect → state_kind → revision: the planned, actual, current,
        # predicted and recommended values of the same aspect are separate entries by construction.
        "current_state": {
            key: {kind.value if hasattr(kind, "value") else str(kind): twin_aspect_out(row) for kind, row in by_kind.items()}
            for key, by_kind in state.items()
        },
    }
    if include_history:
        if aspect:
            history = await service.history(well_id=well_id, aspect_key=aspect)
            payload["history"] = {aspect: [twin_aspect_out(row) for row in history]}
        else:
            payload["history"] = {
                key: [twin_aspect_out(row) for row in await service.history(well_id=well_id, aspect_key=key)]
                for key in sorted(state)
            }
        payload["history_note"] = (
            "history is per aspect and per state kind; superseded revisions keep their valid_from/"
            "valid_to interval, so a value is never overwritten in place"
        )
    return payload


@router.post("/wells/{well_id}/twin/aspects", summary="Write a twin aspect revision", status_code=201)
async def write_aspect(
    well_id: str,
    payload: AspectWrite,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("twin.read"))],
) -> dict[str, Any]:
    """Write an aspect. Requires ``action:twin.update`` (L2) — a twin write is a draft-level act.

    The payload is validated against the aspect's declared schema; an aspect whose scope is
    ``section`` cannot be written without a section, so the twin cannot accumulate values whose
    scope is unknown.
    """
    authorize(auth.principal, "twin.update")
    if payload.aspect not in aspect_keys():
        raise ValidationFailed(
            f"unknown twin aspect {payload.aspect!r}", details={"known": list(aspect_keys())}
        )
    well = (
        await session.execute(select(Well).where(Well.id == well_id, Well.org_id == auth.org_id))
    ).scalar_one_or_none()
    if well is None:
        raise NotFound(f"well {well_id!r} not found")
    revision = AspectRevision(
        aspect=payload.aspect,
        state_kind=payload.state_kind,
        payload=payload.payload,
        summary=payload.summary,
        confidence=payload.confidence,
        data_quality=payload.data_quality,
        computed_by=payload.computed_by,
        engine_run_id=payload.engine_run_id,
        source_refs=payload.source_refs,
        evidence_refs=payload.evidence_refs,
        assumptions=payload.assumptions,
        valid_from=payload.valid_from,
        valid_to=payload.valid_to,
    )
    service = TwinService(session, org_id=auth.org_id or "")
    row = await service.write_aspect(
        well_id=well_id,
        revision=revision,
        wellbore_id=payload.wellbore_id,
        section_id=payload.section_id,
    )
    return twin_aspect_out(row)


@router.get("/wells/{well_id}/twin/snapshots", summary="Twin snapshots")
async def list_snapshots(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("twin.read"))],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    service = TwinService(session, org_id=auth.org_id or "")
    rows = await service.list_snapshots(well_id=well_id, limit=limit)
    return {
        "items": [
            {
                "id": row.id,
                "label": row.label,
                "kind": row.kind,
                "as_of": row.as_of.isoformat() if row.as_of else None,
                "completeness": row.completeness,
                "twin_state": row.twin_state,
                "summary": row.summary,
                "scenario_id": row.scenario_id,
                "parent_snapshot_id": row.parent_snapshot_id,
                "aspect_count": len(row.aspect_ids or []),
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.get("/wells/{well_id}/twin/as-of", summary="Twin state as of a moment in time")
async def twin_as_of(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("twin.read"))],
    moment: dt.datetime = Query(description="ISO-8601 instant; returns the revisions valid then"),
    aspect: OptionalFilter = None,
) -> dict[str, Any]:
    """Time travel over the twin: what was believed at ``moment``.

    Implemented over the same revision rows (``valid_from``/``valid_to``), not a separate
    store, so an as-of answer cannot disagree with the history it came from.
    """
    service = TwinService(session, org_id=auth.org_id or "")
    rows = await service.as_of(well_id=well_id, moment=moment, aspect_key=aspect)
    return {
        "well_id": well_id,
        "as_of": moment.isoformat(),
        "aspects": [twin_aspect_out(row) for row in rows],
        "total": len(rows),
    }


@router.get("/twin/snapshots/{snapshot_id}/compare/{other_id}", summary="Compare two snapshots")
async def compare_snapshots(
    snapshot_id: str,
    other_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("twin.read"))],
) -> dict[str, Any]:
    service = TwinService(session, org_id=auth.org_id or "")
    return await service.compare_snapshots(snapshot_id, other_id)


@router.get("/wells/{well_id}/twin/changes", summary="Twin change history")
async def twin_changes(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("twin.read"))],
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    service = TwinService(session, org_id=auth.org_id or "")
    rows = await service.change_history(well_id=well_id, limit=limit)
    return {
        "items": [
            {
                "id": row.id,
                "change_type": row.change_type,
                "subject_kind": row.subject_kind,
                "subject_id": row.subject_id,
                "field_paths": list(row.field_paths or []),
                "before": row.before,
                "after": row.after,
                "reason": row.reason,
                "actor_kind": row.actor_kind,
                "actor_id": row.actor_id,
                "source": row.source,
                "impact": row.impact,
                "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
            }
            for row in rows
        ],
        "total": len(rows),
    }


@router.get("/wells/{well_id}/engine-runs", summary="Engine runs recorded for a well")
async def well_engine_runs(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("engine.read"))],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """Every engine execution against this well, with hashes — agent and API runs included."""
    from drillai.api.serializers import engine_run_out
    from drillai.db.models import EngineRun

    rows = (
        await session.execute(
            select(EngineRun)
            .where(EngineRun.well_id == well_id, EngineRun.org_id == auth.org_id)
            .order_by(EngineRun.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {"items": [engine_run_out(row) for row in rows], "total": len(rows)}


@router.get("/wells/{well_id}/recommendations", summary="Recommendations for a well")
async def well_recommendations(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("recommendation.read"))],
    status: OptionalFilter = None,
) -> dict[str, Any]:
    from drillai.api.serializers import recommendation_out
    from drillai.db.models import Recommendation

    stmt = select(Recommendation).where(
        Recommendation.well_id == well_id, Recommendation.org_id == auth.org_id
    )
    if status:
        stmt = stmt.where(Recommendation.status == status)
    rows = (await session.execute(stmt.order_by(Recommendation.created_at.desc()))).scalars().all()
    return {"items": [recommendation_out(row) for row in rows], "total": len(rows)}


@router.get("/wells/{well_id}/audit", summary="Audit trail for a well (engine runs, tool calls, LLM calls)")
async def well_audit(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("evidence.read"))],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    """One chronological view of what the platform *did* for this well.

    This is the answer to "what has been run here, by whom, and with which model/tool?" without
    joining five tables by hand. Indices are on ``well_id`` for each source table.
    """
    from drillai.db.models import EngineRun, LlmCall, NodeRun, ToolCall, WorkflowRun

    engine_rows = (
        await session.execute(
            select(EngineRun).where(EngineRun.well_id == well_id).order_by(EngineRun.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    run_rows = (
        await session.execute(
            select(WorkflowRun).where(WorkflowRun.well_id == well_id).order_by(WorkflowRun.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    tool_rows = (
        await session.execute(
            select(ToolCall).where(ToolCall.well_id == well_id).order_by(ToolCall.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    llm_rows = (
        await session.execute(
            select(LlmCall).where(LlmCall.well_id == well_id).order_by(LlmCall.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    node_rows = (
        await session.execute(
            select(NodeRun)
            .join(WorkflowRun, NodeRun.run_id == WorkflowRun.id)
            .where(WorkflowRun.well_id == well_id)
            .order_by(NodeRun.created_at.desc())
            .limit(limit)
        )
    ).scalars().all()
    return {
        "well_id": well_id,
        "engine_runs": [
            {
                "id": row.id,
                "engine_key": row.engine_key,
                "engine_version": row.engine_version,
                "status": row.status,
                "is_feasible": row.is_feasible,
                "inputs_hash": row.inputs_hash,
                "outputs_hash": row.outputs_hash,
                "triggered_by": row.triggered_by,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in engine_rows
        ],
        "workflow_runs": [
            {
                "id": row.id,
                "workflow_key": row.workflow_key,
                "version": row.version,
                "status": row.status,
                "initiated_by": row.initiated_by,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            }
            for row in run_rows
        ],
        "node_runs": [
            {
                "id": row.id,
                "run_id": row.run_id,
                "node_id": row.node_id,
                "node_type": row.node_type,
                "status": row.status,
                "duration_ms": row.duration_ms,
            }
            for row in node_rows
        ],
        "tool_calls": [
            {
                "id": row.id,
                "tool_key": row.tool_key,
                "status": row.status,
                "action_level": row.action_level,
                "duration_ms": row.duration_ms,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in tool_rows
        ],
        "llm_calls": [
            {
                "id": row.id,
                "provider": row.provider,
                "model": row.model,
                "status": row.status,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "routing_reason": row.routing_reason,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in llm_rows
        ],
        "counts": {
            "engine_runs": len(engine_rows),
            "workflow_runs": len(run_rows),
            "node_runs": len(node_rows),
            "tool_calls": len(tool_rows),
            "llm_calls": len(llm_rows),
        },
    }
