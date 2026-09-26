"""Projects, wells, wellbores and sections.

The well hierarchy is the spine of the platform: every document, evidence link, twin aspect,
engine run and workflow run hangs off it, and *scope* (which well, wellbore, section) is what makes
context assembly and retrieval unambiguous. Writes here are draft-level acts (L2) recorded through
the action gate rather than ad-hoc permission strings.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, get_db, require
from drillai.api.serializers import project_out, section_out, well_out, wellbore_out
from drillai.core.errors import NotFound, ValidationFailed
from drillai.core.ids import new_id
from drillai.db.models import Project, Well, Wellbore, WellSection
from drillai.security.actions import authorize

router = APIRouter(tags=["assets"])


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    code: str | None = None
    operator: str | None = None
    country: str | None = None
    basin: str | None = None
    phase: str = "planning"
    description: str | None = None


class WellCreate(BaseModel):
    project_id: str
    name: str = Field(min_length=1, max_length=200)
    uwi: str | None = None
    well_type: str = "development"
    operator: str | None = None
    is_offshore: bool = False
    total_depth_planned_si: float | None = Field(default=None, description="Planned TD, metres MD")
    objectives: str | None = None
    tags: list[str] = Field(default_factory=list)


class WellboreCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    purpose: str = "production"
    sequence: int = 1
    planned_td_md_si: float | None = None
    planned_td_tvd_si: float | None = None


class SectionCreate(BaseModel):
    sequence: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    kind: str = "intermediate"
    hole_diameter_nominal: str | None = Field(default=None, description='Display nominal, e.g. 8-1/2"')
    hole_diameter_si: float | None = Field(default=None, description="Hole diameter, metres")
    planned_top_md_si: float | None = None
    planned_bottom_md_si: float | None = None
    is_planned_only: bool = True


async def _get(session: AsyncSession, model: type, org_id: str, row_id: str, label: str) -> Any:
    row = (
        await session.execute(select(model).where(model.id == row_id, model.org_id == org_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFound(f"{label} {row_id!r} not found")
    return row


# --------------------------------------------------------------------------- projects


@router.get("/projects", summary="List projects")
async def list_projects(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("project.read"))],
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Project).where(Project.org_id == auth.org_id).order_by(Project.created_at)
        )
    ).scalars().all()
    return {"items": [project_out(row) for row in rows], "total": len(rows)}


@router.post("/projects", summary="Create a project", status_code=201)
async def create_project(
    payload: ProjectCreate,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("project.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "project.create")
    project = Project(
        id=new_id("prj"),
        org_id=auth.org_id,
        name=payload.name,
        code=payload.code,
        operator=payload.operator,
        country=payload.country,
        basin=payload.basin,
        phase=payload.phase,
        description=payload.description,
        status="active",
    )
    session.add(project)
    await session.flush()
    return project_out(project)


@router.get("/projects/{project_id}", summary="One project")
async def get_project(
    project_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("project.read"))],
) -> dict[str, Any]:
    project = await _get(session, Project, auth.org_id or "", project_id, "project")
    well_count = (
        await session.execute(
            select(func.count()).select_from(Well).where(Well.project_id == project_id)
        )
    ).scalar_one()
    return {**project_out(project), "well_count": well_count}


# --------------------------------------------------------------------------- wells


@router.get("/wells", summary="List wells")
async def list_wells(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    project_id: OptionalFilter = None,
    status: OptionalFilter = None,
    name_contains: str | None = Query(default=None, max_length=80),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    stmt = select(Well).where(Well.org_id == auth.org_id)
    if project_id:
        stmt = stmt.where(Well.project_id == project_id)
    if status:
        stmt = stmt.where(Well.status == status)
    if name_contains:
        stmt = stmt.where(Well.name.ilike(f"%{name_contains}%"))
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (
        await session.execute(stmt.order_by(Well.name).limit(limit).offset(offset))
    ).scalars().all()
    return {"items": [well_out(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.post("/wells", summary="Create a well", status_code=201)
async def create_well(
    payload: WellCreate,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "well.create")
    await _get(session, Project, auth.org_id or "", payload.project_id, "project")
    well = Well(
        id=new_id("wel"),
        org_id=auth.org_id,
        project_id=payload.project_id,
        name=payload.name,
        uwi=payload.uwi,
        well_type=payload.well_type,
        operator=payload.operator,
        is_offshore=payload.is_offshore,
        status="planned",
        total_depth_planned_si=payload.total_depth_planned_si,
        objectives=payload.objectives,
        tags=payload.tags,
    )
    session.add(well)
    await session.flush()
    return well_out(well)


@router.get("/wells/{well_id}", summary="One well")
async def get_well(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    well = await _get(session, Well, auth.org_id or "", well_id, "well")
    wellbores = (
        await session.execute(select(Wellbore).where(Wellbore.well_id == well_id).order_by(Wellbore.sequence))
    ).scalars().all()
    return {**well_out(well), "wellbores": [wellbore_out(row) for row in wellbores]}


# --------------------------------------------------------------------------- wellbores / sections


@router.get("/wells/{well_id}/wellbores", summary="Wellbores of a well")
async def list_wellbores(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    await _get(session, Well, auth.org_id or "", well_id, "well")
    rows = (
        await session.execute(select(Wellbore).where(Wellbore.well_id == well_id).order_by(Wellbore.sequence))
    ).scalars().all()
    return {"items": [wellbore_out(row) for row in rows], "total": len(rows)}


@router.post("/wells/{well_id}/wellbores", summary="Create a wellbore", status_code=201)
async def create_wellbore(
    well_id: str,
    payload: WellboreCreate,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "well.create")
    await _get(session, Well, auth.org_id or "", well_id, "well")
    wellbore = Wellbore(
        id=new_id("wlb"),
        org_id=auth.org_id,
        well_id=well_id,
        name=payload.name,
        purpose=payload.purpose,
        sequence=payload.sequence,
        status="planned",
        planned_td_md_si=payload.planned_td_md_si,
        planned_td_tvd_si=payload.planned_td_tvd_si,
        is_active=payload.sequence == 1,
    )
    session.add(wellbore)
    await session.flush()
    return wellbore_out(wellbore)


@router.get("/wellbores/{wellbore_id}/sections", summary="Sections of a wellbore")
async def list_sections(
    wellbore_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    wellbore = await _get(session, Wellbore, auth.org_id or "", wellbore_id, "wellbore")
    rows = (
        await session.execute(
            select(WellSection)
            .where(WellSection.wellbore_id == wellbore.id)
            .order_by(WellSection.sequence)
        )
    ).scalars().all()
    return {"items": [section_out(row) for row in rows], "total": len(rows)}


@router.post("/wellbores/{wellbore_id}/sections", summary="Create a hole section", status_code=201)
async def create_section(
    wellbore_id: str,
    payload: SectionCreate,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "well.create")
    wellbore = await _get(session, Wellbore, auth.org_id or "", wellbore_id, "wellbore")
    if (
        payload.planned_top_md_si is not None
        and payload.planned_bottom_md_si is not None
        and payload.planned_bottom_md_si <= payload.planned_top_md_si
    ):
        raise ValidationFailed(
            "planned_bottom_md_si must be deeper than planned_top_md_si",
            details={"top": payload.planned_top_md_si, "bottom": payload.planned_bottom_md_si},
        )
    section = WellSection(
        id=new_id("sec"),
        org_id=auth.org_id,
        wellbore_id=wellbore.id,
        sequence=payload.sequence,
        name=payload.name,
        kind=payload.kind,
        status="planned",
        hole_diameter_si=payload.hole_diameter_si,
        hole_diameter_nominal=payload.hole_diameter_nominal,
        planned_top_md_si=payload.planned_top_md_si,
        planned_bottom_md_si=payload.planned_bottom_md_si,
        is_planned_only=payload.is_planned_only,
    )
    session.add(section)
    await session.flush()
    return section_out(section)
