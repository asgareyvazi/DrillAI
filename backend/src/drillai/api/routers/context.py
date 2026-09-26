"""Engineering context: one bundle, assembled on request, for every consumer.

The endpoint exists so that a UI, an agent, a workflow node and a prompt builder all read the same
structure — scope, sections, citations, token budget, omissions and redactions. Two query
parameters carry the safety property the mission cares about: ``section_id`` (and the depth
window) narrow the bundle to the hole section in question, so an 8½" question cannot be answered
with 12¼" data.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, get_db, require
from drillai.context.builder import default_context_builder, registered_section_keys
from drillai.context.model import ContextPurpose, ContextRequest, ContextScope, UnitSystemName
from drillai.core.errors import NotFound, ValidationFailed
from drillai.db.models import Well, Wellbore, WellSection
from drillai.security.actions import authorize

router = APIRouter(tags=["context"])


@router.get("/wells/{well_id}/context", summary="Assemble the engineering context for a well")
async def well_context(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    purpose: ContextPurpose = ContextPurpose.PROMPT,
    sections: Annotated[list[str] | None, Query()] = None,
    wellbore_id: str | None = None,
    section_id: str | None = None,
    operation_id: str | None = None,
    depth_from_si: float | None = Query(default=None, description="Measured depth window start, metres"),
    depth_to_si: float | None = Query(default=None, description="Measured depth window end, metres"),
    period_from: dt.datetime | None = None,
    period_to: dt.datetime | None = None,
    unit_system: UnitSystemName = UnitSystemName.SI,
    locale: str | None = None,
    max_items_per_section: int = Query(default=50, ge=1, le=500),
    token_budget: int | None = Query(default=None, ge=64),
    render_prompt: bool = Query(default=False, description="Also return the rendered prompt payload"),
) -> dict[str, Any]:
    authorize(auth.principal, "context.read")
    well = (
        await session.execute(select(Well).where(Well.id == well_id, Well.org_id == auth.org_id))
    ).scalar_one_or_none()
    if well is None:
        raise NotFound(f"well {well_id!r} not found")
    if section_id is not None and wellbore_id is None:
        # A section belongs to a wellbore: resolving it here keeps every provider scoped
        # consistently instead of each one inferring the parent.
        section = (
            await session.execute(select(WellSection).where(WellSection.id == section_id))
        ).scalar_one_or_none()
        if section is None:
            raise NotFound(f"section {section_id!r} not found")
        wellbore_id = section.wellbore_id
    if wellbore_id is not None:
        wellbore = (
            await session.execute(
                select(Wellbore).where(Wellbore.id == wellbore_id, Wellbore.well_id == well.id)
            )
        ).scalar_one_or_none()
        if wellbore is None:
            raise NotFound(f"wellbore {wellbore_id!r} not found on well {well_id!r}")
    if depth_from_si is not None and depth_to_si is not None and depth_to_si <= depth_from_si:
        raise ValidationFailed(
            "depth_to_si must be deeper than depth_from_si",
            details={"depth_from_si": depth_from_si, "depth_to_si": depth_to_si},
        )
    if sections:
        unknown = sorted(set(sections) - set(registered_section_keys()))
        if unknown:
            raise ValidationFailed(
                "unknown context sections requested", details={"unknown": unknown}
            )

    request = ContextRequest(
        scope=ContextScope(
            org_id=auth.org_id or "",
            project_id=well.project_id,
            well_id=well.id,
            wellbore_id=wellbore_id,
            section_id=section_id,
            operation_id=operation_id,
            depth_from_si=depth_from_si,
            depth_to_si=depth_to_si,
            period_from=period_from,
            period_to=period_to,
        ),
        purpose=purpose,
        sections=sections,
        unit_system=unit_system,
        locale=locale or auth.locale,
        max_items_per_section=max_items_per_section,
        token_budget=token_budget,
        # The bundle is assembled with exactly the permissions the caller holds, so what a
        # consumer can see is decided once, here, instead of by each provider.
        permissions=auth.principal.permissions,
    )
    builder = default_context_builder()
    bundle = await builder.build(session, request)
    payload: dict[str, Any] = {
        "bundle": bundle.model_dump(mode="json"),
        "section_count": len(bundle.sections),
        "item_count": bundle.item_count(),
        "citations": bundle.all_cites(),
    }
    if render_prompt:
        payload["prompt"] = builder.render_prompt_payload(bundle)
    return payload


@router.get("/context/sections", summary="Section catalogue (what can appear in a bundle)")
async def context_sections(
    _: Annotated[AuthContext, Depends(require("registry.read"))],
) -> dict[str, Any]:
    from drillai.context.builder import section_catalogue

    items = section_catalogue()
    return {"items": items, "total": len(items), "purposes": [purpose.value for purpose in ContextPurpose]}


@router.get("/context/purposes", summary="Per-purpose defaults (sections and token budget)")
async def context_purposes(
    _: Annotated[AuthContext, Depends(require("registry.read"))],
) -> dict[str, Any]:
    from drillai.context.builder import DEFAULT_TOKEN_BUDGET, PURPOSE_SECTIONS

    return {
        "items": [
            {
                "purpose": purpose.value,
                "sections": list(PURPOSE_SECTIONS[purpose]),
                "default_token_budget": DEFAULT_TOKEN_BUDGET[purpose],
            }
            for purpose in ContextPurpose
        ]
    }
