"""Projects, fields, rigs, wells, wellbores and sections — the master-data API.

The hierarchy is the spine of the platform: every document, evidence link, twin aspect, engine run
and workflow run hangs off it, and *scope* (which well, wellbore, section) is what makes context
assembly retrieval unambiguous.

This module is deliberately thin. It validates the wire shape, resolves the caller's organization,
declares the permission and the action, and then hands the work to :class:`~drillai.assets.AssetService`
— which is where the vocabulary, the life-cycle rules, the lineage rules, the uniqueness scopes and
the audit ledger live. A rule that only exists in a router is a rule the domain layer will eventually
disagree with, and a mutation that only exists in a router leaves no ledger entry.

Everything a caller can change is classified before it is exposed (``assets/identity.py``): some
fields are editable, some identify the record, some belong to another part of the domain (status to
the life-cycle service, ``rig_id`` to rig assignment, ``twin_state`` to the twin), and some are
derived from the record itself. A caller that sends a field it may not set is told *which* of those it
is, by name, rather than having the key silently dropped.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import AfterValidator, BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, get_db, require
from drillai.api.serializers import (
    audit_log_out,
    field_out,
    project_out,
    rig_out,
    section_out,
    well_out,
    wellbore_out,
)
from drillai.assets import AssetService
from drillai.assets.lifecycle import (
    SECTION_TRANSITIONS,
    WELL_TRANSITIONS,
    WELLBORE_TRANSITIONS,
    allowed_transitions,
)
from drillai.core.errors import NotFound, ValidationFailed
from drillai.core.idempotency import complete, replay_or_reserve
from drillai.db.models import Project
from drillai.security.actions import authorize

router = APIRouter(tags=["assets"])


def _require_offset(value: Any) -> Any:
    """Refuse a timestamp with no timezone offset, in words, at the API boundary.

    The column type stores UTC instants and rejects naïve values, so a date-only string
    ("2026-01-15") would otherwise reach the ORM and surface as a server error. Guessing the offset
    would be worse than refusing it: the event would silently land in whatever timezone the server
    happens to run in. This runs *after* parsing, because that is the point at which "2026-01-15" has
    become a datetime and can be asked whether it knows its zone.
    """
    if isinstance(value, dt.datetime) and value.tzinfo is None:
        raise ValueError(
            "must include a timezone offset (the platform stores UTC instants), for example 2026-01-15T00:00:00Z"
        )
    return value


# --- request bodies -------------------------------------------------------------------------


class _CreateBody(BaseModel):
    """Base for create bodies: strict about unknown keys, explicit about the ones it will not accept.

    ``extra="forbid"`` matters because the router maps these bodies to service calls by name — an
    ignored key would be a caller's instruction silently dropped. Fields the *domain* owns are declared
    here (so they can be explained) and removed from the mapping at the call site (so they cannot be
    passed by accident); see ``_refuse_derived``.
    """

    model_config = ConfigDict(extra="forbid")


class ProjectCreate(_CreateBody):
    """A project. ``status`` is not accepted: a project starts active and is closed deliberately."""

    name: str = Field(min_length=1, max_length=200)
    code: str | None = Field(default=None, max_length=60)
    operator: str | None = Field(default=None, max_length=200)
    country: str | None = None
    basin: str | None = None
    phase: str | None = None
    description: str | None = None


class ProjectPatch(BaseModel):
    """A partial edit of a project's own master data."""

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    code: str | None = None
    operator: str | None = None
    country: str | None = None
    basin: str | None = None
    phase: str | None = None
    description: str | None = None
    expected_updated_at: Annotated[dt.datetime | None, AfterValidator(_require_offset)] = None

    def changes(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump(exclude_unset=True).items() if k != "expected_updated_at"}


class FieldCreate(_CreateBody):
    project_id: str
    name: str = Field(min_length=1, max_length=200)
    country: str | None = None
    basin: str | None = None
    water_depth_si: float | None = None
    centroid_lat: float | None = Field(default=None, ge=-90, le=90)
    centroid_lon: float | None = Field(default=None, ge=-180, le=180)
    notes: str | None = None
    aliases: list[str] = Field(default_factory=list)
    status: str | None = Field(
        default=None,
        description="Not accepted on create: a field is created active and retired deliberately.",
    )


class FieldPatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    country: str | None = None
    basin: str | None = None
    water_depth_si: float | None = None
    centroid_lat: float | None = None
    centroid_lon: float | None = None
    notes: str | None = None
    aliases: list[str] | None = None
    expected_updated_at: Annotated[dt.datetime | None, AfterValidator(_require_offset)] = None

    def changes(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump(exclude_unset=True).items() if k != "expected_updated_at"}


class WellCreate(_CreateBody):
    """A well's identity, as it is known when the well is planned.

    ``well_type`` and ``elevation_datum`` default to canonical members of their vocabularies; a value
    outside the vocabulary is refused with the accepted set in the error, because "development" is a
    word the platform deliberately does not use (see ``assets/vocabulary.py``).
    """

    project_id: str
    name: str = Field(min_length=1, max_length=200)
    field_id: str | None = None
    uwi: str | None = Field(default=None, max_length=80, description="Operator/regulator well identifier")
    api_number: str | None = Field(
        default=None, max_length=40, description="Regulatory API number, where used"
    )
    well_type: str | None = None
    operator: str | None = None
    rig_id: str | None = None
    is_offshore: bool = False
    surface_lat: float | None = Field(default=None, ge=-90, le=90)
    surface_lon: float | None = Field(default=None, ge=-180, le=180)
    kb_elevation_si: float | None = Field(default=None, description="Kelly-bushing elevation, metres")
    ground_elevation_si: float | None = Field(
        default=None, description="Ground or sea-floor elevation, metres"
    )
    water_depth_si: float | None = Field(default=None, description="Water depth, metres")
    elevation_datum: str | None = None
    slot: str | None = None
    pad_name: str | None = None
    total_depth_planned_si: float | None = Field(default=None, description="Planned TD, metres MD")
    spud_date: Annotated[dt.datetime | None, AfterValidator(_require_offset)] = None
    objectives: str | None = None
    target_formations: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    status: str | None = Field(
        default=None, description="Not accepted on create: a well is created planned and moves by transition."
    )
    twin_state: str | None = Field(
        default=None, description="Not accepted: twin state is written by the twin."
    )


class WellPatch(BaseModel):
    """A partial edit of a well.

    Every editable field is declared with its type so that a malformed value ("tags": "not-a-list") is
    refused at the boundary instead of reaching a JSON column. ``extra="allow"`` lets the remaining
    keys through to the domain, which is the only place that can explain *why* ``status`` or
    ``twin_state`` is not editable here.
    """

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    uwi: str | None = None
    api_number: str | None = None
    field_id: str | None = None
    well_type: str | None = None
    operator: str | None = None
    is_offshore: bool | None = None
    surface_lat: float | None = None
    surface_lon: float | None = None
    kb_elevation_si: float | None = None
    ground_elevation_si: float | None = None
    water_depth_si: float | None = None
    elevation_datum: str | None = None
    slot: str | None = None
    pad_name: str | None = None
    total_depth_planned_si: float | None = None
    spud_date: Annotated[dt.datetime | None, AfterValidator(_require_offset)] = None
    release_date: Annotated[dt.datetime | None, AfterValidator(_require_offset)] = None
    objectives: str | None = None
    target_formations: list[str] | None = None
    tags: list[str] | None = None
    reason: str | None = Field(default=None, description="Why the edit was made; recorded in the ledger.")
    expected_updated_at: Annotated[dt.datetime | None, AfterValidator(_require_offset)] = None

    def changes(self) -> dict[str, Any]:
        payload = self.model_dump(exclude_unset=True)
        payload.pop("expected_updated_at", None)
        payload.pop("reason", None)
        return payload


class WellboreCreate(_CreateBody):
    """A wellbore. ``sequence`` defaults to the next free position rather than always 1."""

    name: str = Field(min_length=1, max_length=120)
    purpose: str | None = None
    sequence: int | None = Field(default=None, ge=1)
    parent_wellbore_id: str | None = Field(
        default=None, description="The hole this one was drilled from; required for sidetrack/bypass/reentry"
    )
    planned_td_md_si: float | None = None
    planned_td_tvd_si: float | None = None
    kickoff_md_si: float | None = None
    datum: str | None = None
    status: str | None = Field(default=None, description="Not accepted on create; wellbores start planned.")
    is_active: bool | None = Field(
        default=None, description="Not accepted on create; activation is explicit."
    )


class WellborePatch(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    purpose: str | None = None
    parent_wellbore_id: str | None = None
    planned_td_md_si: float | None = None
    planned_td_tvd_si: float | None = None
    actual_td_md_si: float | None = None
    actual_td_tvd_si: float | None = None
    kickoff_md_si: float | None = None
    datum: str | None = None
    reason: str | None = None
    expected_updated_at: Annotated[dt.datetime | None, AfterValidator(_require_offset)] = None

    def changes(self) -> dict[str, Any]:
        payload = self.model_dump(exclude_unset=True)
        payload.pop("expected_updated_at", None)
        payload.pop("reason", None)
        return payload


class SectionCreate(_CreateBody):
    """A hole section.

    The as-drilled depths are accepted at creation because a section imported from a drilling report
    *was* drilled; ``is_planned_only`` is not accepted, because it is derived from exactly those depths
    and a caller-settable flag would let a section claim to be drilled while carrying only a plan.
    """

    sequence: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=120)
    kind: str | None = None
    hole_diameter_nominal: str | None = Field(default=None, description='Display nominal, e.g. 8-1/2"')
    hole_diameter_si: float | None = Field(default=None, description="Hole diameter, metres")
    planned_top_md_si: float | None = None
    planned_bottom_md_si: float | None = None
    actual_top_md_si: float | None = None
    actual_bottom_md_si: float | None = None
    current_md_si: float | None = None
    is_planned_only: bool | None = Field(
        default=None, description="Not accepted: derived from the as-drilled depths (record them instead)."
    )
    status: str | None = Field(default=None, description="Not accepted on create; sections start planned.")


class SectionPatch(BaseModel):
    """A partial edit of a hole section: a plan revision, or a recorded as-drilled measurement."""

    model_config = ConfigDict(extra="allow")

    name: str | None = None
    kind: str | None = None
    hole_diameter_si: float | None = None
    hole_diameter_nominal: str | None = None
    planned_top_md_si: float | None = None
    planned_bottom_md_si: float | None = None
    actual_top_md_si: float | None = None
    actual_bottom_md_si: float | None = None
    current_md_si: float | None = None
    casing_od_si: float | None = None
    casing_od_nominal: str | None = None
    casing_weight_si: float | None = None
    casing_grade: str | None = None
    casing_connection: str | None = None
    casing_top_md_si: float | None = None
    casing_shoe_md_si: float | None = None
    cement_top_md_si: float | None = None
    cement_planned_top_md_si: float | None = None
    mud_weight_si: float | None = None
    mud_weight_min_si: float | None = None
    mud_weight_max_si: float | None = None
    pore_pressure_gradient_si: float | None = None
    fracture_gradient_si: float | None = None
    collapse_gradient_si: float | None = None
    lot_fit_equivalent_mw_si: float | None = None
    pressure_source: str | None = None
    notes: str | None = None
    expected_updated_at: Annotated[dt.datetime | None, AfterValidator(_require_offset)] = None

    def changes(self) -> dict[str, Any]:
        return {k: v for k, v in self.model_dump(exclude_unset=True).items() if k != "expected_updated_at"}


class LifecycleRequest(BaseModel):
    """A requested life-cycle transition."""

    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1, description="The state to move to")
    reason: str | None = Field(default=None, max_length=500)


class RigAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rig_id: str | None = Field(default=None, description="The rig to attach, or null to detach")
    reason: str | None = Field(default=None, max_length=500)


# --- helpers --------------------------------------------------------------------------------


def _service(session: AsyncSession, auth: AuthContext, request: Request) -> AssetService:
    """The domain service for this request, carrying the identity of the caller for the ledger."""
    return AssetService(
        session,
        org_id=auth.org_id or "",
        principal=auth.principal,
        request_id=getattr(request.state, "request_id", None),
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


def _refuse_derived(payload: BaseModel, *, derived: dict[str, str]) -> None:
    """Refuse a derived or life-cycle-managed field sent to a create endpoint, with its owner named."""
    values = payload.model_dump(exclude_unset=True)
    for field, owner in derived.items():
        if values.get(field) is None:
            continue
        raise ValidationFailed(
            f"{field} cannot be set here: it is owned by {owner}",
            details={"field": field, "owned_by": owner, "sent": values[field]},
        )


async def _replay(
    session: AsyncSession, *, org_id: str, key: str | None, scope: str, payload: Any
) -> dict[str, Any] | None:
    """Return the answer this exact request already received, if it is a retry."""
    return await replay_or_reserve(session, org_id=org_id, key=key, scope=scope, payload=payload)


async def _remember(
    session: AsyncSession, *, org_id: str, key: str | None, scope: str, response: dict[str, Any]
) -> dict[str, Any]:
    """Store the response for a later retry of the same keyed request."""
    await complete(session, org_id=org_id, key=key, scope=scope, response=response)
    return response


def _with_options(
    row: Any, serializer: Any, transitions: dict[str, frozenset[str]] | None = None
) -> dict[str, Any]:
    """A resource plus the transitions it may legally move to next.

    The legal next states are the server's answer, not the client's guess: the frontend renders the
    life-cycle menu from this list instead of carrying a second copy of the transition table, which is
    the drift that makes an interface offer a button the API will refuse.
    """
    payload = serializer(row)
    if transitions is not None:
        payload["allowed_transitions"] = allowed_transitions(transitions, row.status)
    return payload


# --- projects -------------------------------------------------------------------------------


@router.get("/projects", summary="List projects")
async def list_projects(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("project.read"))],
) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(Project).where(Project.org_id == auth.org_id).order_by(Project.created_at)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [project_out(row) for row in rows], "total": len(rows)}


@router.post("/projects", summary="Create a project", status_code=201)
async def create_project(
    payload: ProjectCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("project.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "project.create")
    org_id = auth.org_id or ""
    body = payload.model_dump()
    scope = "asset.project.create"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    project = await _service(session, auth, request).create_project(**body)
    return await _remember(
        session, org_id=org_id, key=idempotency_key, scope=scope, response=project_out(project)
    )


@router.get("/projects/{project_id}", summary="One project")
async def get_project(
    project_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("project.read"))],
) -> dict[str, Any]:
    service = _service(session, auth, request)
    project = await service.get_project(project_id)
    return {**project_out(project), "well_count": await service.project_well_count(project_id)}


@router.patch("/projects/{project_id}", summary="Edit a project")
async def update_project(
    project_id: str,
    payload: ProjectPatch,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("project.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "project.update")
    org_id = auth.org_id or ""
    body = payload.changes()
    scope = f"asset.project.update:{project_id}"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    project = await _service(session, auth, request).update_project(
        project_id, body, expected_updated_at=payload.expected_updated_at
    )
    return await _remember(
        session, org_id=org_id, key=idempotency_key, scope=scope, response=project_out(project)
    )


# --- rigs -----------------------------------------------------------------------------------


@router.get("/rigs", summary="List rigs (reference data for well assignment)")
async def list_rigs(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    service = AssetService(session, org_id=auth.org_id or "")
    rows, total = await service.list_rigs(limit=limit, offset=offset)
    return {"items": [rig_out(row) for row in rows], "total": total, "limit": limit, "offset": offset}


# --- fields ---------------------------------------------------------------------------------


@router.get("/fields", summary="List fields")
async def list_fields(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("field.read"))],
    project_id: OptionalFilter = None,
    q: str | None = Query(default=None, max_length=80, description="Match the name or an alias"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    service = AssetService(session, org_id=auth.org_id or "")
    rows, total = await service.list_fields(project_id=project_id, query=q, limit=limit, offset=offset)
    return {"items": [field_out(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.post("/fields", summary="Create a field", status_code=201)
async def create_field(
    payload: FieldCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("field.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "field.create")
    derived = {"status": "the field register (a field is created active)"}
    _refuse_derived(payload, derived=derived)
    org_id = auth.org_id or ""
    body = payload.model_dump(exclude=set(derived))
    scope = "asset.field.create"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    field = await _service(session, auth, request).create_field(**body)
    return await _remember(
        session, org_id=org_id, key=idempotency_key, scope=scope, response=field_out(field)
    )


@router.get("/fields/{field_id}", summary="One field")
async def get_field(
    field_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("field.read"))],
) -> dict[str, Any]:
    field = await _service(session, auth, request).get_field(field_id)
    return field_out(field)


@router.patch("/fields/{field_id}", summary="Edit a field")
async def update_field(
    field_id: str,
    payload: FieldPatch,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("field.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "field.update")
    org_id = auth.org_id or ""
    body = payload.changes()
    scope = f"asset.field.update:{field_id}"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    field = await _service(session, auth, request).update_field(
        field_id, body, expected_updated_at=payload.expected_updated_at
    )
    return await _remember(
        session, org_id=org_id, key=idempotency_key, scope=scope, response=field_out(field)
    )


# --- wells ----------------------------------------------------------------------------------


@router.get("/wells", summary="List wells")
async def list_wells(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    project_id: OptionalFilter = None,
    field_id: OptionalFilter = None,
    status: OptionalFilter = None,
    well_type: OptionalFilter = None,
    q: str | None = Query(
        default=None, max_length=80, description="Match the name, UWI, API number or operator"
    ),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    service = AssetService(session, org_id=auth.org_id or "")
    rows, total = await service.list_wells(
        project_id=project_id,
        field_id=field_id,
        status=status,
        well_type=well_type,
        query=q,
        limit=limit,
        offset=offset,
    )
    return {"items": [well_out(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.post("/wells", summary="Create a well", status_code=201)
async def create_well(
    payload: WellCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "well.create")
    derived = {
        "status": "the well lifecycle (POST /wells/{well_id}/lifecycle)",
        "twin_state": "the twin, which writes it when a snapshot is taken",
    }
    _refuse_derived(payload, derived=derived)
    org_id = auth.org_id or ""
    body = payload.model_dump(exclude=set(derived))
    scope = "asset.well.create"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    service = _service(session, auth, request)
    well = await service.create_well(**body)
    # The rig pointer is kept true in both directions, which is why it is not a plain column write.
    if payload.rig_id:
        await service.assign_rig(well.id, payload.rig_id, reason="assigned when the well was created")
    response = _with_options(well, well_out, WELL_TRANSITIONS)
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.get("/wells/{well_id}", summary="One well")
async def get_well(
    well_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    service = _service(session, auth, request)
    well = await service.get_well(well_id)
    wellbores = await service.list_wellbores(well_id)
    return {
        **_with_options(well, well_out, WELL_TRANSITIONS),
        "wellbores": [_with_options(row, wellbore_out, WELLBORE_TRANSITIONS) for row in wellbores],
    }


@router.get(
    "/wells/{well_id}/structure", summary="The well's structure: wellbores, sections and context counts"
)
async def get_well_structure(
    well_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    """The whole hierarchy in one read, with what hangs off each node.

    The cockpit's ``GET /wells/{well_id}`` answers "what is this well"; this answers "what is inside
    it and is anything missing". It exists as one endpoint rather than three because the alternative
    is a client asking for each wellbore's sections in turn (an N+1 the server should not invite), and
    because context integrity is a property of the tree as a whole — a document attached to a wellbore
    that no longer exists is not visible from any single node.
    """
    service = _service(session, auth, request)
    well = await service.get_well(well_id)
    wellbores = await service.list_wellbores(well_id)
    counts = await service.well_context_counts(well_id)

    tree = []
    for wellbore in wellbores:
        sections = await service.list_sections(wellbore.id)
        tree.append(
            {
                **_with_options(wellbore, wellbore_out, WELLBORE_TRANSITIONS),
                "sections": [section_out(row) for row in sections],
                # What hangs off *this* wellbore. The well-level counts are on the structure response,
                # so a reader can see both "this hole has two documents" and "the well has three".
                "counts": {key: value["by_wellbore"].get(wellbore.id, 0) for key, value in counts.items()},
            }
        )

    field = await service.get_field(well.field_id) if well.field_id else None
    rig = None
    if well.rig_id:
        rigs, _ = await service.list_rigs()
        rig = next((row for row in rigs if row.id == well.rig_id), None)

    return {
        "well": {
            **_with_options(well, well_out, WELL_TRANSITIONS),
            "active_wellbore_id": next((row.id for row in wellbores if row.is_active), None),
        },
        "field": field_out(field) if field is not None else None,
        "rig": rig_out(rig) if rig is not None else None,
        "wellbores": tree,
        "counts": {key: value["total"] for key, value in counts.items()},
    }


@router.patch("/wells/{well_id}", summary="Edit a well")
async def update_well(
    well_id: str,
    payload: WellPatch,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    """Rename, re-identify, re-scope or re-datum a well.

    A rename keeps ``well.id``, which is what every document, operation, evidence link, twin aspect and
    engine run points at: the edit changes how the well is *named*, never which record it is. Moving a
    well to another field is validated against the well's own project; moving it between projects is
    refused, because the historical records hanging off it carry their own project id and would then
    disagree with the well.
    """
    authorize(auth.principal, "well.update")
    org_id = auth.org_id or ""
    body = payload.changes()
    scope = f"asset.well.update:{well_id}"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    well = await _service(session, auth, request).update_well(
        well_id, body, reason=payload.reason, expected_updated_at=payload.expected_updated_at
    )
    response = _with_options(well, well_out, WELL_TRANSITIONS)
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.post("/wells/{well_id}/lifecycle", summary="Transition a well's lifecycle state")
async def transition_well(
    well_id: str,
    payload: LifecycleRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    """Move the well between operational states.

    There is no route that sets ``status`` as an ordinary field, and that is the point: a state change
    is a governed act with its own action, its own permission and its own ledger entry, and the legal
    edges live in the domain (``assets/lifecycle.py``) rather than in a form.
    """
    authorize(auth.principal, "well.lifecycle")
    org_id = auth.org_id or ""
    body = {"target": payload.target, "reason": payload.reason}
    scope = f"asset.well.lifecycle:{well_id}"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    well = await _service(session, auth, request).transition_well(
        well_id, payload.target, reason=payload.reason
    )
    response = _with_options(well, well_out, WELL_TRANSITIONS)
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.post("/wells/{well_id}/rig", summary="Attach or detach the rig working a well")
async def assign_rig(
    well_id: str,
    payload: RigAssignment,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    """Give the well a rig, or take it away.

    A rig has one current well, so this is a two-sided write: assigning keeps ``Rig.current_well_id``
    true, and detaching clears it. Letting a form set the foreign key directly is how the two records
    came to disagree about where the rig was.
    """
    authorize(auth.principal, "well.assign_rig")
    org_id = auth.org_id or ""
    body = {"rig_id": payload.rig_id}
    scope = f"asset.well.assign_rig:{well_id}"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    well = await _service(session, auth, request).assign_rig(well_id, payload.rig_id, reason=payload.reason)
    response = _with_options(well, well_out, WELL_TRANSITIONS)
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.get("/wells/{well_id}/audit-log", summary="The governance ledger for a well")
async def well_audit_log(
    well_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Every recorded mutation of this well and its parts: who, what, before and after.

    Distinct from ``GET /wells/{well_id}/audit`` (twin router), which reports *machine* activity —
    engine runs, tool calls, LLM calls. This is the master-data ledger, and it is readable with
    ``well.read`` because it is the history of a well the caller may already read; every row is scoped
    to that well and this organization.
    """
    service = _service(session, auth, request)
    rows = await service.well_history(well_id, limit=limit)
    return {"items": [audit_log_out(row) for row in rows], "total": len(rows)}


# --- wellbores ------------------------------------------------------------------------------


@router.get("/wells/{well_id}/wellbores", summary="Wellbores of a well")
async def list_wellbores(
    well_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    rows = await _service(session, auth, request).list_wellbores(well_id)
    return {
        "items": [_with_options(row, wellbore_out, WELLBORE_TRANSITIONS) for row in rows],
        "total": len(rows),
    }


@router.post("/wells/{well_id}/wellbores", summary="Create a wellbore", status_code=201)
async def create_wellbore(
    well_id: str,
    payload: WellboreCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "wellbore.create")
    derived = {
        "status": "the wellbore lifecycle (POST /wellbores/{wellbore_id}/lifecycle)",
        "is_active": "hole activation (POST /wellbores/{wellbore_id}/activate)",
    }
    _refuse_derived(payload, derived=derived)
    org_id = auth.org_id or ""
    body = payload.model_dump(exclude=set(derived))
    scope = f"asset.wellbore.create:{well_id}"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    wellbore = await _service(session, auth, request).create_wellbore(well_id, **body)
    response = _with_options(wellbore, wellbore_out, WELLBORE_TRANSITIONS)
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.get("/wellbores/{wellbore_id}", summary="One wellbore")
async def get_wellbore(
    wellbore_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    wellbore = await _service(session, auth, request).get_wellbore(wellbore_id)
    return _with_options(wellbore, wellbore_out, WELLBORE_TRANSITIONS)


@router.get("/wellbores/{wellbore_id}/lineage", summary="A wellbore's lineage, oldest first")
async def get_wellbore_lineage(
    wellbore_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    """The parent chain, read from the recorded relation (never parsed out of a name like ``ST-1``)."""
    chain = await _service(session, auth, request).wellbore_lineage(wellbore_id)
    return {
        "items": [_with_options(row, wellbore_out, WELLBORE_TRANSITIONS) for row in chain],
        "total": len(chain),
        "root_id": chain[0].id,
        "wellbore_id": wellbore_id,
    }


@router.patch("/wellbores/{wellbore_id}", summary="Edit a wellbore")
async def update_wellbore(
    wellbore_id: str,
    payload: WellborePatch,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    """Edit a wellbore's master data, including its lineage.

    Re-parenting is validated: the parent must exist, belong to the same well, and not create a cycle.
    A ``sidetrack`` with no parent is refused rather than accepted with the relation missing — the
    relation is data, and a hole that branched off another hole has to say which one.
    """
    authorize(auth.principal, "wellbore.update")
    org_id = auth.org_id or ""
    body = payload.changes()
    scope = f"asset.wellbore.update:{wellbore_id}"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    wellbore = await _service(session, auth, request).update_wellbore(
        wellbore_id, body, expected_updated_at=payload.expected_updated_at
    )
    response = _with_options(wellbore, wellbore_out, WELLBORE_TRANSITIONS)
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.post("/wellbores/{wellbore_id}/activate", summary="Make this the hole being drilled")
async def activate_wellbore(
    wellbore_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
    reason: str | None = Query(default=None, max_length=500),
) -> dict[str, Any]:
    """Designate the current hole. Exactly one wellbore per well is active, atomically."""
    authorize(auth.principal, "wellbore.activate")
    org_id = auth.org_id or ""
    scope = f"asset.wellbore.activate:{wellbore_id}"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload={})
    ) is not None:
        return replayed
    wellbore = await _service(session, auth, request).activate_wellbore(wellbore_id, reason=reason)
    response = _with_options(wellbore, wellbore_out, WELLBORE_TRANSITIONS)
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.post("/wellbores/{wellbore_id}/lifecycle", summary="Transition a wellbore's lifecycle state")
async def transition_wellbore(
    wellbore_id: str,
    payload: LifecycleRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "wellbore.lifecycle")
    org_id = auth.org_id or ""
    body = {"target": payload.target, "reason": payload.reason}
    scope = f"asset.wellbore.lifecycle:{wellbore_id}"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    wellbore = await _service(session, auth, request).transition_wellbore(
        wellbore_id, payload.target, reason=payload.reason
    )
    response = _with_options(wellbore, wellbore_out, WELLBORE_TRANSITIONS)
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


# --- sections -------------------------------------------------------------------------------


async def _scoped_section(service: AssetService, wellbore_id: str, section_id: str) -> Any:
    """Load a section *through* its wellbore, so a mismatched pair is a 404 rather than an edit."""
    section = await service.get_section(section_id)
    if section.wellbore_id != wellbore_id:
        raise NotFound(
            f"section {section_id!r} is not part of wellbore {wellbore_id!r}",
            details={
                "section_id": section_id,
                "section_wellbore_id": section.wellbore_id,
                "wellbore_id": wellbore_id,
            },
        )
    return section


@router.get("/wellbores/{wellbore_id}/sections", summary="Sections of a wellbore")
async def list_sections(
    wellbore_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    rows = await _service(session, auth, request).list_sections(wellbore_id)
    return {"items": [section_out(row) for row in rows], "total": len(rows)}


@router.post("/wellbores/{wellbore_id}/sections", summary="Create a hole section", status_code=201)
async def create_section(
    wellbore_id: str,
    payload: SectionCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "section.create")
    derived = {
        "is_planned_only": "the record itself, derived from the as-drilled depths (send them instead)",
        "status": "the section lifecycle (POST /wellbores/{wellbore_id}/sections/{section_id}/lifecycle)",
    }
    _refuse_derived(payload, derived=derived)
    org_id = auth.org_id or ""
    body = payload.model_dump(exclude=set(derived))
    scope = f"asset.section.create:{wellbore_id}"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    section = await _service(session, auth, request).create_section(wellbore_id, **body)
    response = _with_options(section, section_out, SECTION_TRANSITIONS)
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.patch("/wellbores/{wellbore_id}/sections/{section_id}", summary="Edit a hole section")
async def update_section(
    wellbore_id: str,
    section_id: str,
    payload: SectionPatch,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    """Edit the plan, or record what was drilled.

    The two are the same request because they are the same form; the difference the API enforces is
    that a plan edit never writes an as-drilled value, a recorded as-drilled value cannot be cleared by
    an edit, and a plan revised after the hole was drilled is recorded as such. ``is_planned_only``
    follows the recorded depths and cannot be set by the caller.
    """
    authorize(auth.principal, "section.update")
    org_id = auth.org_id or ""
    body = payload.changes()
    scope = f"asset.section.update:{section_id}"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    service = _service(session, auth, request)
    await _scoped_section(service, wellbore_id, section_id)
    section = await service.update_section(section_id, body, expected_updated_at=payload.expected_updated_at)
    response = _with_options(section, section_out, SECTION_TRANSITIONS)
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.post(
    "/wellbores/{wellbore_id}/sections/{section_id}/lifecycle",
    summary="Transition a hole section's lifecycle state",
)
async def transition_section(
    wellbore_id: str,
    section_id: str,
    payload: LifecycleRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("well.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    """Move the section between planned, drilling, drilled and abandoned.

    Recording a section as *drilled* requires its as-drilled bottom: the alternative is a claim about
    hole geometry that no record supports, and every reader of the section would then have to guess how
    deep it went.
    """
    authorize(auth.principal, "section.lifecycle")
    org_id = auth.org_id or ""
    body = {"target": payload.target, "reason": payload.reason}
    scope = f"asset.section.lifecycle:{section_id}"
    if (
        replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)
    ) is not None:
        return replayed
    service = _service(session, auth, request)
    await _scoped_section(service, wellbore_id, section_id)
    section = await service.transition_section(section_id, payload.target, reason=payload.reason)
    response = _with_options(section, section_out, SECTION_TRANSITIONS)
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
