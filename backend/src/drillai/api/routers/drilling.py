"""Drilling intelligence API: cockpit, timeline, NPT, DDR processing, advisor, reports, optimisation.

These endpoints are what the product's workspaces read. They add no new data of their own: each one
composes the domain services in :mod:`drillai.drilling` over the same tables the rest of the
platform uses, so the UI and the workflow runtime see identical numbers.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.ai.providers import ProviderRegistry, provider_registry
from drillai.ai.router import LlmRouter, RoutingPolicy
from drillai.api.deps import AuthContext, OptionalFilter, get_db, require
from drillai.drilling.advisor import ADVISOR_QUESTIONS, OperationsAdvisor
from drillai.drilling.ddr import DdrProcessor
from drillai.drilling.dependencies import DependencyGraphService
from drillai.drilling.npt import NptService
from drillai.drilling.optimisation import COMPUTED_OBJECTIVES, NOT_EVALUATED, DrillingOptimisationService
from drillai.drilling.reporting import REPORT_KINDS, ReportingService
from drillai.drilling.state import KPI_CHANNELS, WellStateService
from drillai.drilling.timeline import TIMELINE_KINDS, TimelineService
from drillai.security.actions import authorize

router = APIRouter(tags=["drilling"])


# --------------------------------------------------------------------------- schemas


class ProcessDdrRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str | None = None
    dry_run: bool = Field(
        default=False,
        description="compute exactly what would be promoted, writing nothing",
    )


class OptimiseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameters: list[dict[str, Any]] = Field(min_length=1, description="sweep decision variables")
    hydraulics_inputs: dict[str, Any] = Field(
        description="base inputs for hydraulics.laminar; candidate variables override matching keys"
    )
    torque_drag_inputs: dict[str, Any] | None = None
    limits: dict[str, float] = Field(
        default_factory=dict,
        description="declared rig limits, e.g. {'spp_si': 25e6, 'torque_si': 40000}",
    )
    objectives: list[dict[str, Any]] = Field(
        default_factory=list,
        description=f"objective keys restricted to what engines compute: {sorted(COMPUTED_OBJECTIVES)}",
    )
    section_id: str | None = None
    samples: int = Field(default=24, ge=2, le=500)
    title: str | None = None
    persist_recommendation: bool = True


class AdvisorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(default="where_are_we", description="one of the catalogue questions")
    use_llm: bool = Field(
        default=False,
        description=(
            "ask the configured LLM to narrate the computed sections; the LLM cannot alter facts "
            "or calculations and receives no numeric task"
        ),
    )


# --------------------------------------------------------------------------- cockpit


@router.get("/wells/{well_id}/state", summary="Drilling state of a well")
async def well_state(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    """Everything the Well Cockpit shows: progress, operation, measured values, NPT, risks, gaps."""
    authorize(auth.principal, "context.read", context={"well_id": well_id})
    state = await WellStateService(session, auth.org_id).state(well_id)
    return {"state": state}


@router.get("/wells/{well_id}/timeline", summary="Merged well timeline")
async def well_timeline(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    kinds: Annotated[list[str] | None, Query(description="filter to specific entry kinds")] = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: int = Query(default=500, ge=1, le=2000),
) -> dict[str, Any]:
    unknown = [kind for kind in (kinds or []) if kind not in TIMELINE_KINDS]
    if unknown:
        from drillai.core.errors import ValidationFailed

        raise ValidationFailed(
            "unknown timeline kinds", details={"unknown": unknown, "known": list(TIMELINE_KINDS)}
        )
    entries = await TimelineService(session, auth.org_id).build(
        well_id, since=since, until=until, kinds=kinds, limit=limit
    )
    return {
        "entries": [entry.to_dict() for entry in entries],
        "count": len(entries),
        "kinds_available": list(TIMELINE_KINDS),
    }


@router.get("/wells/{well_id}/npt", summary="NPT intelligence for a well")
async def well_npt(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    basis: str = Query(default="events", pattern="^(events|operations)$"),
    include_offsets: bool = True,
) -> dict[str, Any]:
    summary = await NptService(session, auth.org_id).summarise(
        well_id, basis=basis, include_offsets=include_offsets
    )
    return {"npt": summary.to_dict()}


@router.get("/wells/{well_id}/kpis", summary="KPI catalogue for a well")
async def well_kpis(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    """Which KPIs the platform tracks, which have values, and where each value came from."""
    measured = await WellStateService(session, auth.org_id).measured_values(well_id)
    present = {item.key: item.to_dict() for item in measured}
    return {
        "kpis": [
            {
                "key": key,
                "label": label,
                "present": key in present,
                "value": present.get(key, {}).get("value"),
                "unit": present.get(key, {}).get("unit"),
                "source": present.get(key, {}).get("source"),
                "quality": present.get(key, {}).get("quality"),
                "evidence_ref": present.get(key, {}).get("evidence_ref"),
            }
            for key, label in sorted(KPI_CHANNELS.items())
        ],
        "present_count": len(present),
        "total": len(KPI_CHANNELS),
    }


# --------------------------------------------------------------------------- DDR processing


@router.post("/documents/{document_id}/process", summary="Promote a DDR into structured drilling data")
async def process_document(
    document_id: str,
    payload: ProcessDdrRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("document.ingest"))],
) -> dict[str, Any]:
    """Run the DDR → operations/events/survey/twin pipeline for one document."""
    authorize(
        auth.principal,
        "document.ingest",
        approval_id=None,
        context={"document_id": document_id},
    )
    processor = DdrProcessor(session, auth.org_id, principal=auth.principal)
    report = await processor.process(payload.document_id or document_id, dry_run=payload.dry_run)
    return {"processing": report.to_dict()}


# --------------------------------------------------------------------------- advisor


@router.get("/advisor/questions", summary="Questions the Operations Advisor can answer")
async def advisor_questions(
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    return {
        "questions": [{"key": key, "description": text} for key, text in ADVISOR_QUESTIONS.items()],
        "contract": {
            "facts": "read from recorded rows, each with its table/field source",
            "calculations": "engine outputs with engine key and version",
            "evidence": "documents, pages, excerpts, extraction method and confidence",
            "inference": "reasoning beyond the records, with the basis it rests on",
            "recommendation": "proposed actions with assumptions, constraints and approval state",
            "unknown": "what the platform does not know and how it could be obtained",
        },
    }


@router.post("/wells/{well_id}/advisor", summary="Ask the Operations Advisor about a well")
async def ask_advisor(
    well_id: str,
    payload: AdvisorRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "context.read", context={"well_id": well_id})
    router_instance: LlmRouter | None = None
    if payload.use_llm:
        authorize(auth.principal, "rag.search", context={"well_id": well_id})
        registry: ProviderRegistry = provider_registry()
        router_instance = LlmRouter(registry=registry, policy=RoutingPolicy())
    # The question is validated against the catalogue here, at the boundary, so a client typo is a
    # 422 naming the valid questions instead of an unhandled domain error surfacing as a 500.
    if payload.question not in ADVISOR_QUESTIONS:
        from drillai.core.errors import ValidationFailed

        raise ValidationFailed(
            "unknown advisor question",
            details={
                "unknown": [payload.question],
                "known": sorted(ADVISOR_QUESTIONS),
                "hint": "GET /api/v1/advisor/questions returns the catalogue with descriptions",
            },
        )
    advisor = OperationsAdvisor(session, auth.org_id, llm_router=router_instance, actor=auth.principal.id)
    answer = await advisor.ask(well_id, payload.question)
    return {"answer": answer.to_dict()}


# --------------------------------------------------------------------------- reports


@router.get("/reports/kinds", summary="Report kinds the platform can assemble")
async def report_kinds(
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    return {
        "kinds": [{"key": key, "description": text} for key, text in REPORT_KINDS.items()],
        "note": (
            "a report is a structured payload separating facts, calculations, evidence, "
            "recommendations and assumptions; document rendering is not implemented"
        ),
    }


@router.get("/wells/{well_id}/reports/{kind}", summary="Build a report payload")
async def build_report(
    well_id: str,
    kind: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    period_from: dt.datetime | None = None,
    period_to: dt.datetime | None = None,
) -> dict[str, Any]:
    report = await ReportingService(session, auth.org_id).build(
        kind, well_id, period_from=period_from, period_to=period_to
    )
    return {"report": report.to_dict()}


# --------------------------------------------------------------------------- dependencies


@router.get("/engineering/dependencies", summary="Engineering dependency graph")
async def dependency_graph(
    auth: Annotated[AuthContext, Depends(require("engine.run"))],
) -> dict[str, Any]:
    """Port-level graph derived from engine declarations — no hard-coded wiring."""
    return {"graph": DependencyGraphService(session=None, org_id=auth.org_id).graph()}  # type: ignore[arg-type]


@router.get("/wells/{well_id}/engineering/impact", summary="What goes stale when an input changes")
async def engineering_impact(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("engine.run"))],
    ports: Annotated[list[str], Query(description="context ports that changed")],
) -> dict[str, Any]:
    service = DependencyGraphService(session, auth.org_id)
    return {"impact": await service.impact(well_id, ports)}


# --------------------------------------------------------------------------- optimisation


@router.get("/engineering/optimisation/objectives", summary="Objectives the platform can compute")
async def optimisation_objectives(
    auth: Annotated[AuthContext, Depends(require("engine.run"))],
) -> dict[str, Any]:
    return {
        "computable": [
            {"key": key, **value} for key, value in sorted(COMPUTED_OBJECTIVES.items())
        ],
        "not_evaluated": NOT_EVALUATED,
        "note": (
            "only objectives an engine actually computes may be optimised; ROP and MSE are absent "
            "because no model exists and guessing them is forbidden"
        ),
    }


@router.post("/wells/{well_id}/engineering/optimise", summary="Run a drilling parameter optimisation")
async def run_optimisation(
    well_id: str,
    payload: OptimiseRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("engine.run"))],
) -> dict[str, Any]:
    authorize(
        auth.principal,
        "optimization.run",
        approval_id=None,
        context={"well_id": well_id, "objectives": payload.objectives},
    )
    service = DrillingOptimisationService(session, auth.org_id, actor_id=auth.principal.id)
    result = await service.optimise(
        well_id=well_id,
        parameters=payload.parameters,
        hydraulics_inputs=payload.hydraulics_inputs,
        torque_drag_inputs=payload.torque_drag_inputs,
        limits=payload.limits,
        objectives=payload.objectives,
        section_id=payload.section_id,
        samples=payload.samples,
        title=payload.title,
        persist_recommendation=payload.persist_recommendation,
    )
    return {"optimisation": result}


@router.get(
    "/engineering/optimisation/{optimization_run_id}",
    summary="Why this candidate, and why not the others",
)
async def optimisation_explanation(
    optimization_run_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("engine.run"))],
) -> dict[str, Any]:
    service = DrillingOptimisationService(session, auth.org_id)
    return {"explanation": await service.explain(optimization_run_id)}


__all__ = ["OptionalFilter", "advisor_questions", "router", "well_state"]
