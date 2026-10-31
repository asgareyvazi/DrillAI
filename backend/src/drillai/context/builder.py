"""Engineering context assembly.

A :class:`ContextBuilder` is given a session and a registry of *section providers*. Each provider
is responsible for one section, declares the permission it needs, and returns a
:class:`ContextSection`. The builder:

* runs providers concurrently and independently (one failing provider must not blank the page);
* applies the request scope (well/section/depth/time) — providers that ignore scope are a bug,
  and the section tests assert the behaviour;
* enforces the token budget by dropping the lowest-priority sections and marking them;
* records what was omitted, which permissions filtered what, and which redactions were applied;
* renders a deterministic prompt payload in which **every value carries a citation**, so a
  language model can quote but not invent.

Providers are registered by key, so a domain pack (completion, intervention, integrity) adds
sections without touching the builder.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from typing import Any

from sqlalchemy import and_, case, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from drillai.context.model import (
    ContextBundle,
    ContextItem,
    ContextPurpose,
    ContextRequest,
    ContextSection,
)
from drillai.core.errors import ContextError
from drillai.core.ids import new_id
from drillai.core.logging import get_logger
from drillai.db.models import (
    BomRequirement,
    Document,
    DocumentChunk,
    EngineRun,
    Event,
    EvidenceLink,
    ExtractedRecord,
    Lesson,
    OffsetCandidate,
    Operation,
    Project,
    RawArtifact,
    Recommendation,
    Risk,
    Trajectory,
    TrajectoryStation,
    Well,
    Wellbore,
    WellFormationMarker,
    WellSection,
)
from drillai.documents.access import (
    chunk_document_scope_conditions,
    document_not_quarantined_condition,
    evidence_document_not_quarantined_condition,
    extracted_record_scope_conditions,
    extracted_record_trust_condition,
    operational_source_conditions,
    trajectory_source_conditions,
    trajectory_station_evidence_document_condition,
)
from drillai.observability.tracing import get_metrics, get_tracer
from drillai.operations.time_windows import operation_time_expressions
from drillai.security.rbac import permissions_match
from drillai.twin.service import TwinService

logger = get_logger(__name__)

__all__ = [
    "ContextBuilder",
    "SectionProvider",
    "SectionResult",
    "default_context_builder",
    "register_section_provider",
    "registered_section_keys",
    "section_catalogue",
]

SectionResult = tuple[ContextSection, list[str]]


class SectionProvider:
    """Base class for context sections. Subclasses implement :meth:`build`."""

    key: str = "unnamed"
    title: str = "Unnamed"
    required_permission: str = "context.read"
    #: lower runs first; the budget drops from the end
    priority: int = 100
    #: sections are skipped unless the request asks for them (None = default set)
    default_included: bool = True

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:  # pragma: no cover
        raise NotImplementedError


_PROVIDERS: dict[str, SectionProvider] = {}


def register_section_provider(provider: SectionProvider, *, replace: bool = False) -> None:
    if provider.key in _PROVIDERS and not replace:
        raise ContextError(f"section provider {provider.key!r} is already registered")
    _PROVIDERS[provider.key] = provider


def registered_section_keys() -> list[str]:
    return sorted(_PROVIDERS)


def section_catalogue() -> list[dict[str, Any]]:
    """Inspectable description of every registered section (what a UI or integrator can ask for).

    The permission each section requires is part of the catalogue on purpose: a client must be
    able to explain *before* calling why a section may come back redacted.
    """
    return [
        {
            "key": provider.key,
            "title": provider.title,
            "required_permission": provider.required_permission,
            "priority": provider.priority,
            "default_included": provider.default_included,
            "purposes": sorted(
                purpose.value for purpose, keys in PURPOSE_SECTIONS.items() if provider.key in keys
            ),
        }
        for provider in _ordered(_PROVIDERS.values())
    ]


def _ordered(providers: Iterable[SectionProvider]) -> list[SectionProvider]:
    return sorted(providers, key=lambda provider: (provider.priority, provider.key))


def _trim(section: ContextSection, limit: int) -> ContextSection:
    if limit <= 0 or len(section.items) <= limit:
        return section
    section.omitted_items = len(section.items) - limit
    section.items = section.items[:limit]
    section.truncated = True
    section.notes.append(f"showing {limit} of {limit + section.omitted_items} items (most recent first)")
    return section


def _empty(section: ContextSection, reason: str) -> ContextSection:
    section.empty_reason = reason
    return section


def _estimate_tokens(text: str) -> int:
    """Cheap, provider-independent estimate: ~4 characters per token."""
    return max(1, len(text) // 4)


# --------------------------------------------------------------------------- providers

def _section_filter(column, section_id: str | None):
    """Match a section-scoped column.

    When the caller asks for a specific section, only rows carrying *that* section are included.
    Rows with no section (well-level documents, well-wide recommendations) are deliberately
    excluded: including them is how a question about the 8½" section silently pulls in every
    well-level document and every recommendation ever raised on the well. When no section is in
    scope, nothing is filtered.
    """
    if not section_id:
        return None
    return column == section_id




class WellIdentityProvider(SectionProvider):
    key = "well_identity"
    required_permission = "well.read"
    title = "Well identity and datums"
    priority = 10

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        well_stmt = select(Well).where(Well.id == scope.well_id, Well.org_id == scope.org_id)
        if scope.project_id:
            well_stmt = well_stmt.where(Well.project_id == scope.project_id)
        well = (await session.execute(well_stmt)).scalar_one_or_none()
        if well is None:
            return _empty(section, f"well {scope.well_id} not found in the requested organization/project"), []
        project = None
        if well.project_id:
            project = (
                await session.execute(
                    select(Project).where(Project.id == well.project_id, Project.org_id == scope.org_id)
                )
            ).scalar_one_or_none()
        section.items.append(
            ContextItem(
                kind="well",
                id=well.id,
                label=well.name,
                data={
                    "name": well.name,
                    "uwi": well.uwi,
                    "api_number": well.api_number,
                    "well_type": well.well_type,
                    "status": well.status,
                    "project": project.name if project else None,
                    "operator": well.operator or (project.operator if project else None),
                    "field": None,
                    "country": project.country if project else None,
                    "surface_lat": well.surface_lat,
                    "surface_lon": well.surface_lon,
                    "is_offshore": well.is_offshore,
                    "target_formations": well.target_formations,
                    "objectives": well.objectives,
                },
                source_kind="database",
                source_ids=[well.id],
                units={
                    "kb_elevation_si": "m",
                    "ground_elevation_si": "m",
                    "water_depth_si": "m",
                    "total_depth_planned_si": "m",
                },
            )
        )
        section.items.append(
            ContextItem(
                kind="datums",
                id=well.id,
                label="Datums and elevations",
                data={
                    "kb_elevation_si": well.kb_elevation_si,
                    "ground_elevation_si": well.ground_elevation_si,
                    "water_depth_si": well.water_depth_si,
                    "elevation_datum": well.elevation_datum,
                    "rig_id": well.rig_id,
                    "total_depth_planned_si": well.total_depth_planned_si,
                },
                source_kind="database",
                source_ids=[well.id],
                units={"kb_elevation_si": "m", "ground_elevation_si": "m", "water_depth_si": "m", "total_depth_planned_si": "m"},
                notes=["depth zero is the rotary table / kelly bushing unless stated otherwise"],
            )
        )
        return section, [well.id]


class WellboresProvider(SectionProvider):
    key = "wellbores"
    required_permission = "well.read"
    title = "Wellbores"
    priority = 20

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        if not request.scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = (
            select(Wellbore)
            .join(Well, Well.id == Wellbore.well_id)
            .where(
                Wellbore.org_id == request.scope.org_id,
                Well.org_id == request.scope.org_id,
                Well.id == request.scope.well_id,
            )
            .order_by(Wellbore.sequence)
        )
        if request.scope.project_id:
            stmt = stmt.where(Well.project_id == request.scope.project_id)
        if request.scope.wellbore_id:
            stmt = stmt.where(Wellbore.id == request.scope.wellbore_id)
        if request.scope.section_id:
            stmt = stmt.where(
                Wellbore.id.in_(
                    select(WellSection.wellbore_id).where(
                        WellSection.id == request.scope.section_id,
                        WellSection.org_id == request.scope.org_id,
                    )
                )
            )
        if request.scope.operation_id:
            stmt = stmt.where(
                Wellbore.id.in_(
                    select(Operation.wellbore_id).where(
                        Operation.id == request.scope.operation_id,
                        Operation.org_id == request.scope.org_id,
                        Operation.well_id == request.scope.well_id,
                    )
                )
            )
        rows = (await session.execute(stmt)).scalars().all()
        if not rows:
            return _empty(section, "no wellbores recorded for this well"), []
        for row in rows:
            section.items.append(
                ContextItem(
                    kind="wellbore",
                    id=row.id,
                    label=row.name,
                    data={
                        "name": row.name,
                        "purpose": row.purpose,
                        "sequence": row.sequence,
                        "status": row.status,
                        "datum": row.datum,
                        "planned_td_md_si": row.planned_td_md_si,
                        "planned_td_tvd_si": row.planned_td_tvd_si,
                        "actual_td_md_si": row.actual_td_md_si,
                        "actual_td_tvd_si": row.actual_td_tvd_si,
                        "is_active": row.is_active,
                    },
                    source_kind="database",
                    source_ids=[row.id],
                    units={"planned_td_md_si": "m", "planned_td_tvd_si": "m", "actual_td_md_si": "m", "actual_td_tvd_si": "m"},
                )
            )
        return section, [row.id for row in rows]


class WellSectionsProvider(SectionProvider):
    key = "well_sections"
    required_permission = "well.read"
    title = "Hole sections and casing design"
    priority = 25

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        if scope.formation_id is not None or scope.period_from is not None or scope.period_to is not None:
            return _empty(section, "well sections have no formation link or historical version for this scope"), []
        stmt = (
            select(WellSection)
            .join(Wellbore, Wellbore.id == WellSection.wellbore_id)
            .join(Well, Well.id == Wellbore.well_id)
            .where(
                WellSection.org_id == scope.org_id,
                Wellbore.org_id == scope.org_id,
                Well.org_id == scope.org_id,
                Well.id == scope.well_id,
            )
        )
        if scope.project_id:
            stmt = stmt.where(Well.project_id == scope.project_id)
        if scope.wellbore_id:
            stmt = stmt.where(WellSection.wellbore_id == scope.wellbore_id)
        if scope.section_id:
            stmt = stmt.where(WellSection.id == scope.section_id)
        if scope.operation_id:
            stmt = stmt.where(
                WellSection.id.in_(
                    select(Operation.section_id).where(
                        Operation.id == scope.operation_id,
                        Operation.org_id == scope.org_id,
                        *operational_source_conditions(Operation),
                        Operation.well_id == scope.well_id,
                        Operation.section_id.is_not(None),
                    )
                )
            )
        rows = (await session.execute(stmt.order_by(WellSection.sequence))).scalars().all()
        ids: list[str] = []
        for row in rows:
            if request.scope.section_id and row.id != request.scope.section_id:
                continue
            planned_top = row.planned_top_md_si or row.casing_shoe_md_si
            planned_bottom = row.planned_bottom_md_si or row.actual_bottom_md_si
            if not request.scope.depth_overlaps(planned_top, planned_bottom) and not request.scope.depth_overlaps(
                row.actual_top_md_si, row.actual_bottom_md_si
            ):
                continue
            ids.append(row.id)
            section.items.append(
                ContextItem(
                    kind="hole_section",
                    id=row.id,
                    label=row.name,
                    data={
                        "name": row.name,
                        "sequence": row.sequence,
                        "kind": row.kind,
                        "status": row.status,
                        "hole_diameter_si": row.hole_diameter_si,
                        "hole_diameter_nominal": row.hole_diameter_nominal,
                        "planned_top_md_si": row.planned_top_md_si,
                        "planned_bottom_md_si": row.planned_bottom_md_si,
                        "actual_top_md_si": row.actual_top_md_si,
                        "actual_bottom_md_si": row.actual_bottom_md_si,
                        "casing_od_si": row.casing_od_si,
                        "casing_weight_si": row.casing_weight_si,
                        "casing_grade": row.casing_grade,
                        "casing_connection": row.casing_connection,
                        "casing_shoe_md_si": row.casing_shoe_md_si,
                        "cement_top_md_si": row.cement_top_md_si,
                        "mud_weight_si": row.mud_weight_si,
                        "mud_weight_min_si": row.mud_weight_min_si,
                        "mud_weight_max_si": row.mud_weight_max_si,
                        "pore_pressure_gradient_si": row.pore_pressure_gradient_si,
                        "fracture_gradient_si": row.fracture_gradient_si,
                        "lot_fit_equivalent_mw_si": row.lot_fit_equivalent_mw_si,
                        "pressure_source": row.pressure_source,
                    },
                    source_kind="database",
                    source_ids=[row.id],
                    units={
                        "hole_diameter_si": "m",
                        "planned_top_md_si": "m",
                        "planned_bottom_md_si": "m",
                        "actual_top_md_si": "m",
                        "actual_bottom_md_si": "m",
                        "casing_od_si": "m",
                        "casing_weight_si": "kg/m",
                        "casing_shoe_md_si": "m",
                        "cement_top_md_si": "m",
                        "mud_weight_si": "kg/m3",
                        "mud_weight_min_si": "kg/m3",
                        "mud_weight_max_si": "kg/m3",
                        "pore_pressure_gradient_si": "kg/m3",
                        "fracture_gradient_si": "kg/m3",
                    },
                    notes=[row.notes] if row.notes else [],
                )
            )
        if not section.items:
            return _empty(section, "no hole sections match the requested scope"), []
        return section, ids


class TrajectoryProvider(SectionProvider):
    key = "trajectory"
    required_permission = "well.read"
    title = "Trajectory (survey)"
    priority = 30

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = (
            select(Trajectory)
            .join(Wellbore, Wellbore.id == Trajectory.wellbore_id)
            .join(Well, Well.id == Wellbore.well_id)
            .where(
                Trajectory.org_id == scope.org_id,
                Trajectory.is_current.is_(True),
                Wellbore.org_id == scope.org_id,
                Well.org_id == scope.org_id,
                Well.id == scope.well_id,
                *trajectory_source_conditions(Trajectory),
            )
            .order_by(Trajectory.version.desc())
            .limit(1)
        )
        if scope.project_id:
            stmt = stmt.where(Well.project_id == scope.project_id)
        if scope.wellbore_id:
            stmt = stmt.where(Trajectory.wellbore_id == scope.wellbore_id)
        if scope.formation_id:
            stmt = stmt.where(Trajectory.target_formation_id == scope.formation_id)
        section_range = None
        if scope.section_id:
            scoped_section = (
                await session.execute(
                    select(WellSection)
                    .join(Wellbore, Wellbore.id == WellSection.wellbore_id)
                    .join(Well, Well.id == Wellbore.well_id)
                    .where(
                        WellSection.id == scope.section_id,
                        WellSection.org_id == scope.org_id,
                        Wellbore.org_id == scope.org_id,
                        Well.org_id == scope.org_id,
                        Well.id == scope.well_id,
                        *([Well.project_id == scope.project_id] if scope.project_id else []),
                        *([Wellbore.id == scope.wellbore_id] if scope.wellbore_id else []),
                    )
                )
            ).scalar_one_or_none()
            if scoped_section is None:
                return _empty(section, "requested section not found in this well/project"), []
            # A section determines its wellbore even when the caller omitted wellbore_id. Resolve
            # that scope before selecting the current trajectory; otherwise a different bore's
            # survey could win the version-ordered limit for this well.
            stmt = stmt.where(Trajectory.wellbore_id == scoped_section.wellbore_id)
            section_top = (
                scoped_section.actual_top_md_si
                if scoped_section.actual_top_md_si is not None
                else scoped_section.planned_top_md_si
            )
            section_bottom = (
                scoped_section.actual_bottom_md_si
                if scoped_section.actual_bottom_md_si is not None
                else scoped_section.planned_bottom_md_si
            )
            if section_top is None and section_bottom is None:
                return _empty(section, "requested section has no depth interval for survey filtering"), []
            section_range = (section_top, section_bottom)

        operation_range = None
        if scope.operation_id:
            scoped_operation = (
                await session.execute(
                    select(Operation)
                    .join(Well, Well.id == Operation.well_id)
                    .join(Wellbore, Wellbore.id == Operation.wellbore_id)
                    .outerjoin(WellSection, WellSection.id == Operation.section_id)
                    .where(
                        Operation.id == scope.operation_id,
                        Operation.org_id == scope.org_id,
                        *operational_source_conditions(Operation),
                        Operation.well_id == scope.well_id,
                        Well.org_id == scope.org_id,
                        Wellbore.org_id == scope.org_id,
                        Wellbore.well_id == Operation.well_id,
                        or_(
                            Operation.section_id.is_(None),
                            and_(
                                WellSection.id == Operation.section_id,
                                WellSection.org_id == scope.org_id,
                                WellSection.wellbore_id == Operation.wellbore_id,
                            ),
                        ),
                        *([Well.project_id == scope.project_id] if scope.project_id else []),
                        *([Operation.project_id == scope.project_id] if scope.project_id else []),
                        *([Operation.wellbore_id == scope.wellbore_id] if scope.wellbore_id else []),
                        *([Operation.section_id == scope.section_id] if scope.section_id else []),
                    )
                )
            ).scalar_one_or_none()
            if scoped_operation is None:
                return _empty(section, "requested operation not found in this well/project"), []
            # Operations have their own wellbore locator. Apply it before selecting a trajectory,
            # even if this request did not redundantly include wellbore_id.
            stmt = stmt.where(Trajectory.wellbore_id == scoped_operation.wellbore_id)
            operation_range = (scoped_operation.depth_from_md_si, scoped_operation.depth_to_md_si)
            if operation_range == (None, None):
                return _empty(section, "requested operation has no depth interval for survey filtering"), []

        trajectory = (await session.execute(stmt)).scalars().first()
        if trajectory is None:
            return _empty(section, "no current trajectory on file for this scope"), []
        section.source_ids = [trajectory.id]
        if (
            scope.depth_from_si is None
            and scope.depth_to_si is None
            and scope.period_from is None
            and scope.period_to is None
            and section_range is None
            and operation_range is None
        ):
            section.items.append(
                ContextItem(
                kind="trajectory_summary",
                id=trajectory.id,
                label=trajectory.name,
                data={
                    "kind": trajectory.kind,
                    "datum": trajectory.datum,
                    "version": trajectory.version,
                    "station_count": trajectory.station_count,
                    "total_md_si": trajectory.total_md_si,
                    "total_tvd_si": trajectory.total_tvd_si,
                    "max_inclination_deg": trajectory.max_inclination_deg,
                    "validation": trajectory.validation,
                },
                source_kind="database",
                source_ids=[trajectory.id],
                units={"total_md_si": "m", "total_tvd_si": "m", "max_inclination_deg": "deg"},
            )
        )
        station_stmt = (
            select(TrajectoryStation)
            .where(
                TrajectoryStation.org_id == scope.org_id,
                TrajectoryStation.trajectory_id == trajectory.id,
                TrajectoryStation.wellbore_id == trajectory.wellbore_id,
            )
            .order_by(TrajectoryStation.station_index)
        )
        if scope.depth_from_si is not None:
            station_stmt = station_stmt.where(TrajectoryStation.md_si >= scope.depth_from_si)
        if scope.depth_to_si is not None:
            station_stmt = station_stmt.where(TrajectoryStation.md_si <= scope.depth_to_si)
        if scope.period_from is not None:
            station_stmt = station_stmt.where(
                TrajectoryStation.measured_at.is_not(None),
                TrajectoryStation.measured_at >= scope.period_from,
            )
        if scope.period_to is not None:
            station_stmt = station_stmt.where(
                TrajectoryStation.measured_at.is_not(None),
                TrajectoryStation.measured_at <= scope.period_to,
            )
        for interval in (section_range, operation_range):
            if interval is None:
                continue
            interval_start, interval_end = interval
            if interval_start is not None:
                station_stmt = station_stmt.where(TrajectoryStation.md_si >= interval_start)
            if interval_end is not None:
                station_stmt = station_stmt.where(TrajectoryStation.md_si <= interval_end)
        stations = (await session.execute(station_stmt)).scalars().all()
        for station in stations:
            section.items.append(
                ContextItem(
                    kind="trajectory_station",
                    id=station.id,
                    label=f"MD {station.md_si:.1f} m",
                    data={
                        "md_si": station.md_si,
                        "inclination_deg": station.inclination_deg,
                        "azimuth_deg": station.azimuth_deg,
                        "tvd_si": station.tvd_si,
                        "ns_si": station.ns_si,
                        "ew_si": station.ew_si,
                        "dls_deg_per_30m": station.dls_deg_per_30m,
                        "source": station.source,
                        "method": station.method,
                        "engine_version": station.engine_version,
                    },
                    source_kind="database",
                    source_ids=[station.id],
                    cites=[station.evidence_ref] if station.evidence_ref else [],
                    observed_at=station.measured_at,
                    units={"md_si": "m", "tvd_si": "m", "ns_si": "m", "ew_si": "m", "inclination_deg": "deg", "azimuth_deg": "deg", "dls_deg_per_30m": "deg/30m"},
                )
            )
        if not stations:
            section.notes.append("no survey stations match the requested depth/time window")
        return section, [trajectory.id, *[station.id for station in stations]]


class FormationProvider(SectionProvider):
    key = "formations"
    required_permission = "well.read"
    title = "Formation tops and lithology"
    priority = 35

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id and not scope.project_id:
            return _empty(section, "no well or project in scope"), []
        if scope.period_from is not None or scope.period_to is not None:
            return _empty(section, "formation markers do not carry validated observation or validity times"), []
        stmt = (
            select(WellFormationMarker)
            .join(Wellbore, Wellbore.id == WellFormationMarker.wellbore_id)
            .join(Well, Well.id == Wellbore.well_id)
            .where(
                WellFormationMarker.org_id == scope.org_id,
                Wellbore.org_id == scope.org_id,
                Well.org_id == scope.org_id,
            )
        )
        if scope.well_id:
            stmt = stmt.where(Well.id == scope.well_id)
        if scope.project_id:
            stmt = stmt.where(Well.project_id == scope.project_id)
        if scope.wellbore_id:
            stmt = stmt.where(WellFormationMarker.wellbore_id == scope.wellbore_id)
        if scope.formation_id:
            stmt = stmt.where(WellFormationMarker.formation_id == scope.formation_id)
        section_range = None
        if scope.section_id:
            scoped_section = (
                await session.execute(
                    select(WellSection)
                    .join(Wellbore, Wellbore.id == WellSection.wellbore_id)
                    .join(Well, Well.id == Wellbore.well_id)
                    .where(
                        WellSection.id == scope.section_id,
                        WellSection.org_id == scope.org_id,
                        Wellbore.org_id == scope.org_id,
                        Well.org_id == scope.org_id,
                        *([Well.id == scope.well_id] if scope.well_id else []),
                        *([Well.project_id == scope.project_id] if scope.project_id else []),
                        *([Wellbore.id == scope.wellbore_id] if scope.wellbore_id else []),
                    )
                )
            ).scalar_one_or_none()
            if scoped_section is None:
                return _empty(section, "requested section not found in this well/project"), []
            section_top = (
                scoped_section.actual_top_md_si
                if scoped_section.actual_top_md_si is not None
                else scoped_section.planned_top_md_si
            )
            section_bottom = (
                scoped_section.actual_bottom_md_si
                if scoped_section.actual_bottom_md_si is not None
                else scoped_section.planned_bottom_md_si
            )
            section_range = (section_top, section_bottom)
            if section_top is None and section_bottom is None:
                return _empty(section, "requested section has no depth interval to scope formation markers"), []
            stmt = stmt.where(WellFormationMarker.wellbore_id == scoped_section.wellbore_id)

        operation_range = None
        if scope.operation_id:
            scoped_operation = (
                await session.execute(
                    select(Operation).where(
                        Operation.id == scope.operation_id,
                        Operation.org_id == scope.org_id,
                        *operational_source_conditions(Operation),
                        *([Operation.well_id == scope.well_id] if scope.well_id else []),
                        *([Operation.project_id == scope.project_id] if scope.project_id else []),
                        *([Operation.wellbore_id == scope.wellbore_id] if scope.wellbore_id else []),
                        *([Operation.section_id == scope.section_id] if scope.section_id else []),
                    )
                )
            ).scalar_one_or_none()
            if scoped_operation is None:
                return _empty(section, "requested operation not found in this well/project"), []
            operation_range = (scoped_operation.depth_from_md_si, scoped_operation.depth_to_md_si)
            if operation_range == (None, None):
                return _empty(section, "requested operation has no depth interval to scope formation markers"), []
            stmt = stmt.where(WellFormationMarker.wellbore_id == scoped_operation.wellbore_id)

        markers = (await session.execute(stmt.order_by(WellFormationMarker.md_si))).scalars().all()
        ids: list[str] = []
        for marker in markers:
            if not scope.depth_overlaps(marker.md_si, marker.md_si):
                continue
            if section_range is not None:
                section_top, section_bottom = section_range
                marker_depth = marker.md_si
                if marker_depth is None or (
                    section_top is not None and marker_depth < section_top
                ) or (section_bottom is not None and marker_depth > section_bottom):
                    continue
            if operation_range is not None:
                operation_top, operation_bottom = operation_range
                marker_depth = marker.md_si
                if marker_depth is None or (
                    operation_top is not None and marker_depth < operation_top
                ) or (operation_bottom is not None and marker_depth > operation_bottom):
                    continue
            ids.append(marker.id)
            section.items.append(
                ContextItem(
                    kind="formation_marker",
                    id=marker.id,
                    label=f"MD {marker.md_si:.1f} m",
                    data={
                        "formation_id": marker.formation_id,
                        "kind": marker.kind,
                        "md_si": marker.md_si,
                        "tvd_si": marker.tvd_si,
                        "tvdss_si": marker.tvdss_si,
                        "thickness_si": marker.thickness_si,
                        "source": marker.source,
                        "confidence": marker.confidence,
                        "uncertainty_si": marker.uncertainty_si,
                        "notes": marker.notes,
                    },
                    source_kind="database",
                    source_ids=[marker.id],
                    cites=[marker.evidence_ref] if marker.evidence_ref else [],
                    confidence=marker.confidence,
                    units={"md_si": "m", "tvd_si": "m", "tvdss_si": "m", "thickness_si": "m", "uncertainty_si": "m"},
                )
            )
        if not section.items:
            return _empty(section, "no formation markers in the requested interval"), []
        return section, ids


class TwinAspectProvider(SectionProvider):
    """Current twin state — planned/actual/predicted/recommended kept distinct."""

    key = "twin_state"
    required_permission = "twin.read"
    title = "Digital twin state"
    priority = 40

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        if (
            scope.operation_id is not None
            or scope.formation_id is not None
            or scope.depth_from_si is not None
            or scope.depth_to_si is not None
            or scope.period_from is not None
            or scope.period_to is not None
        ):
            return _empty(section, "current twin aspects have no operation/formation/depth/time locator"), []
        well_stmt = select(Well).where(Well.id == scope.well_id, Well.org_id == scope.org_id)
        if scope.project_id:
            well_stmt = well_stmt.where(Well.project_id == scope.project_id)
        if (await session.execute(well_stmt)).scalar_one_or_none() is None:
            return _empty(section, "well not found in the requested organization/project"), []
        service = TwinService(session, org_id=scope.org_id)
        state = await service.current_state(
            well_id=scope.well_id, wellbore_id=scope.wellbore_id, section_id=scope.section_id
        )
        ids: list[str] = []
        for aspect_key in sorted(state):
            for state_kind in sorted(state[aspect_key]):
                revision = state[aspect_key][state_kind]
                ids.append(revision.id)
                section.items.append(
                    ContextItem(
                        kind=f"twin.{aspect_key}",
                        id=revision.id,
                        label=f"{aspect_key} ({state_kind})",
                        data=revision.payload,
                        state_kind=state_kind,
                        source_kind="twin",
                        source_ids=[revision.id, *revision.source_refs],
                        cites=list(revision.evidence_refs),
                        confidence=revision.confidence,
                        data_quality=revision.data_quality,
                        observed_at=revision.computed_at,
                        notes=list(revision.assumptions),
                    )
                )
        if not section.items:
            return _empty(section, "the twin holds no current state for this scope"), []
        return section, ids


class OperationsProvider(SectionProvider):
    key = "operations"
    required_permission = "well.read"
    title = "Operations and NPT"
    priority = 45

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id and not scope.project_id:
            return _empty(section, "no well or project in scope"), []
        if scope.formation_id is not None:
            return _empty(section, "operations do not carry a formation locator"), []
        stmt = select(Operation).where(
            Operation.org_id == scope.org_id,
            *operational_source_conditions(Operation),
        )
        if scope.well_id:
            stmt = stmt.where(Operation.well_id == scope.well_id)
        if scope.project_id:
            stmt = stmt.where(Operation.project_id == scope.project_id)
        if scope.wellbore_id:
            stmt = stmt.where(Operation.wellbore_id == scope.wellbore_id)
        if scope.section_id:
            stmt = stmt.where(Operation.section_id == scope.section_id)
        if scope.operation_id:
            stmt = stmt.where(Operation.id == scope.operation_id)
        if scope.depth_from_si is not None or scope.depth_to_si is not None:
            stmt = stmt.where(
                *_depth_scope_conditions(scope, Operation.depth_from_md_si, Operation.depth_to_md_si)
            )
        if scope.period_from is not None or scope.period_to is not None:
            operation_start, operation_end = _operation_period_expressions()
            stmt = stmt.where(*_interval_scope_conditions(scope, operation_start, operation_end))
        stmt = stmt.order_by(Operation.sequence.desc()).limit(200)
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            ids.append(row.id)
            section.items.append(
                ContextItem(
                    kind="operation",
                    id=row.id,
                    label=row.name,
                    data={
                        "code": row.code,
                        "operation_class": row.operation_class,
                        "kind": row.kind,
                        "phase": row.phase,
                        "status": row.status,
                        "sequence": row.sequence,
                        "depth_from_md_si": row.depth_from_md_si,
                        "depth_to_md_si": row.depth_to_md_si,
                        "planned_start": row.planned_start.isoformat() if row.planned_start else None,
                        "actual_start": row.actual_start.isoformat() if row.actual_start else None,
                        "planned_duration_hours": row.planned_duration_hours,
                        "actual_duration_hours": row.actual_duration_hours,
                        "npt_hours": row.npt_hours,
                        "is_productive": row.is_productive,
                        "data_quality": row.data_quality,
                        "remarks": row.remarks,
                    },
                    source_kind="database",
                    source_ids=[row.id],
                    units={"depth_from_md_si": "m", "depth_to_md_si": "m", "planned_duration_hours": "h", "actual_duration_hours": "h", "npt_hours": "h"},
                )
            )
        if not section.items:
            return _empty(section, "no operations match the requested scope"), []
        return section, ids


class DocumentsProvider(SectionProvider):
    key = "documents"
    required_permission = "document.read"
    title = "Documents on file"
    priority = 55

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        section.source_kind = "document"
        scope = request.scope
        if not scope.well_id and not scope.project_id:
            return _empty(section, "no well or project in scope"), []
        if scope.formation_id is not None or scope.depth_from_si is not None or scope.depth_to_si is not None:
            return _empty(section, "documents do not carry formation/depth locators for this narrowed scope"), []
        stmt = (
            select(Document)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(
                Document.org_id == scope.org_id,
                document_not_quarantined_condition(),
            )
            .order_by(Document.created_at.desc())
            .limit(100)
        )
        if scope.well_id:
            stmt = stmt.where(Document.well_id == scope.well_id)
        if scope.project_id:
            stmt = stmt.where(Document.project_id == scope.project_id)
        if scope.wellbore_id:
            stmt = stmt.where(Document.wellbore_id == scope.wellbore_id)
        if scope.section_id:
            filtered = _section_filter(Document.section_id, scope.section_id)
            if filtered is not None:
                stmt = stmt.where(filtered)
        if scope.operation_id:
            stmt = stmt.where(Document.operation_id == scope.operation_id)
        if scope.period_from is not None or scope.period_to is not None:
            document_has_period = or_(Document.period_start.is_not(None), Document.period_end.is_not(None))
            doc_start = case((document_has_period, Document.period_start), else_=Document.issue_date)
            doc_end = case((document_has_period, Document.period_end), else_=Document.issue_date)
            stmt = stmt.where(*_interval_scope_conditions(scope, doc_start, doc_end))
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            ids.append(row.id)
            section.items.append(
                ContextItem(
                    kind="document",
                    id=row.id,
                    label=row.title,
                    data={
                        "doc_type": row.doc_type,
                        "document_number": row.document_number,
                        "revision": row.revision,
                        "status": row.status,
                        "issue_date": row.issue_date.isoformat() if row.issue_date else None,
                        "period_start": row.period_start.isoformat() if row.period_start else None,
                        "period_end": row.period_end.isoformat() if row.period_end else None,
                        "page_count": row.page_count,
                        "language": row.language,
                        "extraction_summary": row.extraction_summary,
                    },
                    source_kind="document",
                    source_ids=[row.id],
                    cites=[row.id],
                )
            )
        if not section.items:
            return _empty(section, "no documents on file for this well in the requested window"), []
        return section, ids


class ExtractedRecordsProvider(SectionProvider):
    key = "extracted_records"
    required_permission = "document.read"
    title = "Structured records extracted from documents"
    priority = 60

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id and not scope.project_id:
            return _empty(section, "no well or project in scope"), []
        if scope.formation_id is not None:
            return _empty(section, "extracted records do not carry a formation locator"), []
        stmt = (
            select(ExtractedRecord)
            .join(Document, Document.id == ExtractedRecord.document_id)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(
                ExtractedRecord.org_id == scope.org_id,
                Document.org_id == scope.org_id,
                document_not_quarantined_condition(),
                *extracted_record_scope_conditions(),
                extracted_record_trust_condition(),
            )
            .order_by(ExtractedRecord.created_at.desc())
            .limit(200)
        )
        if scope.project_id:
            stmt = stmt.where(ExtractedRecord.project_id == scope.project_id)
        if scope.well_id:
            stmt = stmt.where(ExtractedRecord.well_id == scope.well_id)
        if scope.wellbore_id:
            stmt = stmt.where(ExtractedRecord.wellbore_id == scope.wellbore_id)
        if scope.section_id:
            stmt = stmt.where(ExtractedRecord.section_id == scope.section_id)
        if scope.operation_id:
            stmt = stmt.where(ExtractedRecord.operation_id == scope.operation_id)
        if scope.depth_from_si is not None or scope.depth_to_si is not None:
            stmt = stmt.where(*_depth_scope_conditions(scope, ExtractedRecord.depth_md_si, ExtractedRecord.depth_md_si))
        if scope.period_from is not None or scope.period_to is not None:
            stmt = stmt.where(*_interval_scope_conditions(scope, ExtractedRecord.observed_at, ExtractedRecord.observed_at))
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            if not scope.depth_overlaps(row.depth_md_si, row.depth_md_si):
                continue
            if not scope.period_overlaps(row.observed_at, row.observed_at):
                continue
            ids.append(row.id)
            section.items.append(
                ContextItem(
                    kind=f"record.{row.record_type}",
                    id=row.id,
                    label=f"{row.record_type} p.{row.page_number}",
                    data=row.payload,
                    source_kind="document",
                    source_ids=[row.id, row.document_id],
                    cites=[row.document_id],
                    confidence=row.confidence,
                    data_quality=row.validation_state,
                    observed_at=row.observed_at,
                    units=row.unit_context,
                    notes=list(row.quality_flags or []),
                )
            )
        if not section.items:
            return _empty(section, "no extracted records in the requested scope"), []
        return section, ids


def _depth_scope_conditions(scope, start, end):
    """SQL depth overlap that excludes rows with no depth evidence when a window is requested."""
    low = func.coalesce(start, end)
    high = func.coalesce(end, start)
    conditions = []
    if scope.depth_from_si is not None:
        conditions.append(and_(high.is_not(None), high >= scope.depth_from_si))
    if scope.depth_to_si is not None:
        conditions.append(and_(low.is_not(None), low <= scope.depth_to_si))
    return conditions


def _interval_scope_conditions(scope, start, end):
    """SQL interval overlap with fail-closed handling for a missing time range."""
    low = func.coalesce(start, end)
    high = func.coalesce(end, start)
    conditions = [low.is_not(None), high.is_not(None)]
    if scope.period_from is not None:
        conditions.append(high >= scope.period_from)
    if scope.period_to is not None:
        conditions.append(low <= scope.period_to)
    return conditions


def _source_document_available(document_id, org_id: str):
    """A subject derived from a document is hidden when that source is quarantined or cross-tenant."""
    source_document = aliased(Document)
    source_artifact = aliased(RawArtifact)
    return or_(
        document_id.is_(None),
        select(source_document.id)
        .outerjoin(source_artifact, source_artifact.id == source_document.raw_artifact_id)
        .where(
            source_document.id == document_id,
            source_document.org_id == org_id,
            or_(
                source_document.raw_artifact_id.is_(None),
                and_(
                    source_artifact.id == source_document.raw_artifact_id,
                    source_artifact.org_id == source_document.org_id,
                    source_artifact.is_quarantined.is_(False),
                ),
            ),
        )
        .exists(),
    )


def _operation_period_expressions():
    """Share the coherent actual-vs-planned interval rule with the operational read APIs."""
    return operation_time_expressions()


def _evidence_scope_condition(scope):
    """Require an evidence source (document, extracted record, or operation) to prove scope.

    EvidenceLink stores a well id but can be inconsistently linked to a source from another scope.
    For every requested dimension, including well-only scope, each branch proves that the linked
    source belongs to that scope; a source without
    enough metadata is intentionally excluded.
    """
    narrowed = any(
        value is not None
        for value in (
            scope.well_id,
            scope.project_id,
            scope.wellbore_id,
            scope.section_id,
            scope.operation_id,
            scope.formation_id,
            scope.depth_from_si,
            scope.depth_to_si,
            scope.period_from,
            scope.period_to,
        )
    )
    if not narrowed:
        return None

    document_conditions = [
        Document.id == EvidenceLink.document_id,
        Document.org_id == scope.org_id,
        or_(EvidenceLink.subject_kind != "document", EvidenceLink.subject_id == Document.id),
    ]
    if scope.well_id is not None:
        document_conditions.append(Document.well_id == scope.well_id)
    if scope.project_id is not None:
        document_conditions.append(Document.project_id == scope.project_id)
    if scope.wellbore_id is not None:
        document_conditions.append(Document.wellbore_id == scope.wellbore_id)
    if scope.section_id is not None:
        document_conditions.append(Document.section_id == scope.section_id)
    if scope.operation_id is not None:
        document_conditions.append(Document.operation_id == scope.operation_id)
    if scope.formation_id is not None or scope.depth_from_si is not None or scope.depth_to_si is not None:
        # Document metadata has no formation or depth locator, so it cannot prove those filters.
        document_conditions.append(false())
    if scope.period_from is not None or scope.period_to is not None:
        document_has_period = or_(Document.period_start.is_not(None), Document.period_end.is_not(None))
        document_start = case((document_has_period, Document.period_start), else_=Document.issue_date)
        document_end = case((document_has_period, Document.period_end), else_=Document.issue_date)
        document_conditions.extend(_interval_scope_conditions(scope, document_start, document_end))

    record_subject = or_(
        EvidenceLink.subject_kind == "extracted_record",
        EvidenceLink.subject_kind.startswith("extracted_record."),
    )
    record_conditions = [
        ExtractedRecord.id == EvidenceLink.subject_id,
        record_subject,
        ExtractedRecord.document_id == EvidenceLink.document_id,
        ExtractedRecord.org_id == scope.org_id,
        EvidenceLink.well_id == ExtractedRecord.well_id,
        extracted_record_trust_condition(),
        *extracted_record_scope_conditions(),
    ]
    if scope.well_id is not None:
        record_conditions.append(ExtractedRecord.well_id == scope.well_id)
    if scope.project_id is not None:
        record_conditions.append(ExtractedRecord.project_id == scope.project_id)
    if scope.wellbore_id is not None:
        record_conditions.append(ExtractedRecord.wellbore_id == scope.wellbore_id)
    if scope.section_id is not None:
        record_conditions.append(ExtractedRecord.section_id == scope.section_id)
    if scope.operation_id is not None:
        record_conditions.append(ExtractedRecord.operation_id == scope.operation_id)
    if scope.formation_id is not None:
        record_conditions.append(false())
    if scope.depth_from_si is not None:
        record_conditions.extend(
            (ExtractedRecord.depth_md_si.is_not(None), ExtractedRecord.depth_md_si >= scope.depth_from_si)
        )
    if scope.depth_to_si is not None:
        record_conditions.extend(
            (ExtractedRecord.depth_md_si.is_not(None), ExtractedRecord.depth_md_si <= scope.depth_to_si)
        )
    if scope.period_from is not None:
        record_conditions.extend(
            (ExtractedRecord.observed_at.is_not(None), ExtractedRecord.observed_at >= scope.period_from)
        )
    if scope.period_to is not None:
        record_conditions.extend(
            (ExtractedRecord.observed_at.is_not(None), ExtractedRecord.observed_at <= scope.period_to)
        )

    operation_conditions = [
        Operation.id == EvidenceLink.subject_id,
        EvidenceLink.subject_kind == "operation",
        EvidenceLink.well_id == Operation.well_id,
        Operation.org_id == scope.org_id,
        *operational_source_conditions(Operation),
        _source_document_available(Operation.source_document_id, scope.org_id),
        select(Well.id)
        .where(
            Well.id == Operation.well_id,
            Well.org_id == scope.org_id,
            or_(Operation.project_id.is_(None), Well.project_id == Operation.project_id),
        )
        .exists(),
        or_(
            Operation.wellbore_id.is_(None),
            select(Wellbore.id)
            .where(
                Wellbore.id == Operation.wellbore_id,
                Wellbore.org_id == scope.org_id,
                Wellbore.well_id == Operation.well_id,
            )
            .exists(),
        ),
        or_(
            Operation.section_id.is_(None),
            select(WellSection.id)
            .where(
                WellSection.id == Operation.section_id,
                WellSection.org_id == scope.org_id,
                WellSection.wellbore_id == Operation.wellbore_id,
            )
            .exists(),
        ),
    ]
    operation_conditions.append(
        or_(
            EvidenceLink.document_id.is_(None),
            and_(
                Document.org_id == scope.org_id,
                or_(Document.project_id.is_(None), Document.project_id == Operation.project_id),
                or_(Document.well_id.is_(None), Document.well_id == Operation.well_id),
                or_(Document.wellbore_id.is_(None), Document.wellbore_id == Operation.wellbore_id),
                or_(Document.section_id.is_(None), Document.section_id == Operation.section_id),
                or_(Document.operation_id.is_(None), Document.operation_id == Operation.id),
            ),
        )
    )
    if scope.well_id is not None:
        operation_conditions.append(Operation.well_id == scope.well_id)
    if scope.project_id is not None:
        operation_conditions.append(Operation.project_id == scope.project_id)
    if scope.wellbore_id is not None:
        operation_conditions.append(Operation.wellbore_id == scope.wellbore_id)
    if scope.section_id is not None:
        operation_conditions.append(Operation.section_id == scope.section_id)
    if scope.operation_id is not None:
        operation_conditions.append(Operation.id == scope.operation_id)
    if scope.formation_id is not None:
        operation_conditions.append(false())
    if scope.depth_from_si is not None or scope.depth_to_si is not None:
        depth_start = func.coalesce(Operation.depth_from_md_si, Operation.depth_to_md_si)
        depth_end = func.coalesce(Operation.depth_to_md_si, Operation.depth_from_md_si)
        operation_conditions.append(depth_start.is_not(None))
        operation_conditions.append(depth_end.is_not(None))
        if scope.depth_from_si is not None:
            operation_conditions.append(depth_end >= scope.depth_from_si)
        if scope.depth_to_si is not None:
            operation_conditions.append(depth_start <= scope.depth_to_si)
    if scope.period_from is not None or scope.period_to is not None:
        operation_start, operation_end = _operation_period_expressions()
        operation_conditions.extend(_interval_scope_conditions(scope, operation_start, operation_end))

    event_conditions = [
        Event.id == EvidenceLink.subject_id,
        EvidenceLink.subject_kind == "event",
        EvidenceLink.well_id == Event.well_id,
        Event.org_id == scope.org_id,
        *operational_source_conditions(Event),
        _source_document_available(Event.source_document_id, scope.org_id),
        select(Well.id)
        .where(
            Well.id == Event.well_id,
            Well.org_id == scope.org_id,
            or_(Event.project_id.is_(None), Well.project_id == Event.project_id),
        )
        .exists(),
        or_(
            Event.wellbore_id.is_(None),
            select(Wellbore.id)
            .where(
                Wellbore.id == Event.wellbore_id,
                Wellbore.org_id == scope.org_id,
                Wellbore.well_id == Event.well_id,
            )
            .exists(),
        ),
        or_(
            Event.section_id.is_(None),
            select(WellSection.id)
            .where(
                WellSection.id == Event.section_id,
                WellSection.org_id == scope.org_id,
                WellSection.wellbore_id == Event.wellbore_id,
            )
            .exists(),
        ),
        or_(
            Event.operation_id.is_(None),
            select(Operation.id)
            .where(
                Operation.id == Event.operation_id,
                Operation.org_id == scope.org_id,
                Operation.well_id == Event.well_id,
                or_(Event.project_id.is_(None), Operation.project_id == Event.project_id),
                or_(Event.wellbore_id.is_(None), Operation.wellbore_id == Event.wellbore_id),
                or_(Event.section_id.is_(None), Operation.section_id == Event.section_id),
            )
            .correlate_except(Operation)
            .exists(),
        ),
    ]
    event_conditions.append(
        or_(
            EvidenceLink.document_id.is_(None),
            and_(
                Document.org_id == scope.org_id,
                or_(Document.project_id.is_(None), Document.project_id == Event.project_id),
                or_(Document.well_id.is_(None), Document.well_id == Event.well_id),
                or_(Document.wellbore_id.is_(None), Document.wellbore_id == Event.wellbore_id),
                or_(Document.section_id.is_(None), Document.section_id == Event.section_id),
                or_(Document.operation_id.is_(None), Document.operation_id == Event.operation_id),
            ),
        )
    )
    if scope.well_id is not None:
        event_conditions.append(Event.well_id == scope.well_id)
    if scope.project_id is not None:
        event_conditions.append(Event.project_id == scope.project_id)
    if scope.wellbore_id is not None:
        event_conditions.append(Event.wellbore_id == scope.wellbore_id)
    if scope.section_id is not None:
        event_conditions.append(Event.section_id == scope.section_id)
    if scope.operation_id is not None:
        event_conditions.append(Event.operation_id == scope.operation_id)
    if scope.formation_id is not None:
        event_conditions.append(false())
    if scope.depth_from_si is not None:
        event_conditions.extend((Event.depth_md_si.is_not(None), Event.depth_md_si >= scope.depth_from_si))
    if scope.depth_to_si is not None:
        event_conditions.extend((Event.depth_md_si.is_not(None), Event.depth_md_si <= scope.depth_to_si))
    if scope.period_from is not None or scope.period_to is not None:
        event_conditions.extend(_interval_scope_conditions(scope, Event.occurred_at, Event.ended_at))

    station_stmt = (
        select(TrajectoryStation.id)
        .join(Trajectory, Trajectory.id == TrajectoryStation.trajectory_id)
        .join(Wellbore, Wellbore.id == TrajectoryStation.wellbore_id)
        .join(Well, Well.id == Wellbore.well_id)
        .where(
            TrajectoryStation.id == EvidenceLink.subject_id,
            TrajectoryStation.org_id == scope.org_id,
            EvidenceLink.subject_kind == "trajectory_station",
            Trajectory.org_id == scope.org_id,
            _source_document_available(Trajectory.source_document_id, scope.org_id),
            *trajectory_source_conditions(Trajectory),
            trajectory_station_evidence_document_condition(),
            Trajectory.wellbore_id == TrajectoryStation.wellbore_id,
            Wellbore.org_id == scope.org_id,
            Well.org_id == scope.org_id,
            Well.id == EvidenceLink.well_id,
            or_(
                EvidenceLink.document_id.is_(None),
                and_(
                    Document.org_id == scope.org_id,
                    or_(Document.project_id.is_(None), Document.project_id == Well.project_id),
                    or_(Document.well_id.is_(None), Document.well_id == Well.id),
                    or_(Document.wellbore_id.is_(None), Document.wellbore_id == TrajectoryStation.wellbore_id),
                ),
            ),
        )
    )
    if scope.well_id is not None:
        station_stmt = station_stmt.where(Well.id == scope.well_id)
    if scope.project_id is not None:
        station_stmt = station_stmt.where(Well.project_id == scope.project_id)
    if scope.wellbore_id is not None:
        station_stmt = station_stmt.where(TrajectoryStation.wellbore_id == scope.wellbore_id)
    if scope.formation_id is not None:
        station_stmt = station_stmt.where(Trajectory.target_formation_id == scope.formation_id)
    if scope.section_id is not None:
        section_start = func.coalesce(WellSection.actual_top_md_si, WellSection.planned_top_md_si)
        section_end = func.coalesce(WellSection.actual_bottom_md_si, WellSection.planned_bottom_md_si)
        station_stmt = (
            station_stmt.join(
                WellSection,
                and_(
                    WellSection.id == scope.section_id,
                    WellSection.wellbore_id == TrajectoryStation.wellbore_id,
                ),
            )
            .where(
                WellSection.org_id == scope.org_id,
                section_start.is_not(None),
                section_end.is_not(None),
                TrajectoryStation.md_si >= section_start,
                TrajectoryStation.md_si <= section_end,
            )
        )
    if scope.operation_id is not None:
        operation_start_depth = func.coalesce(Operation.depth_from_md_si, Operation.depth_to_md_si)
        operation_end_depth = func.coalesce(Operation.depth_to_md_si, Operation.depth_from_md_si)
        station_stmt = (
            station_stmt.join(
                Operation,
                and_(
                    Operation.id == scope.operation_id,
                    Operation.well_id == Well.id,
                    Operation.project_id == Well.project_id,
                    Operation.wellbore_id == TrajectoryStation.wellbore_id,
                ),
            )
            .where(
                Operation.org_id == scope.org_id,
                *operational_source_conditions(Operation),
                operation_start_depth.is_not(None),
                operation_end_depth.is_not(None),
                TrajectoryStation.md_si >= operation_start_depth,
                TrajectoryStation.md_si <= operation_end_depth,
            )
        )
    if scope.depth_from_si is not None:
        station_stmt = station_stmt.where(TrajectoryStation.md_si >= scope.depth_from_si)
    if scope.depth_to_si is not None:
        station_stmt = station_stmt.where(TrajectoryStation.md_si <= scope.depth_to_si)
    if scope.period_from is not None:
        station_stmt = station_stmt.where(
            TrajectoryStation.measured_at.is_not(None),
            TrajectoryStation.measured_at >= scope.period_from,
        )
    if scope.period_to is not None:
        station_stmt = station_stmt.where(
            TrajectoryStation.measured_at.is_not(None),
            TrajectoryStation.measured_at <= scope.period_to,
        )
    station_condition = and_(
        EvidenceLink.subject_kind == "trajectory_station",
        station_stmt.correlate_except(TrajectoryStation, Trajectory, Wellbore, Well, WellSection, Operation).exists(),
    )

    document_subject = and_(
        EvidenceLink.subject_kind != "operation",
        EvidenceLink.subject_kind != "event",
        EvidenceLink.subject_kind != "trajectory_station",
        ~record_subject,
    )
    return or_(
        and_(document_subject, and_(*document_conditions)),
        and_(record_subject, and_(*record_conditions)),
        and_(EvidenceLink.subject_kind == "operation", and_(*operation_conditions)),
        and_(EvidenceLink.subject_kind == "event", and_(*event_conditions)),
        station_condition,
    )


class EvidenceProvider(SectionProvider):
    key = "evidence"
    required_permission = "evidence.read"
    title = "Evidence and provenance"
    priority = 65

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id and not scope.project_id:
            return _empty(section, "no well or project in scope"), []
        stmt = (
            select(EvidenceLink)
            .outerjoin(Document, Document.id == EvidenceLink.document_id)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .outerjoin(
                ExtractedRecord,
                and_(
                    ExtractedRecord.id == EvidenceLink.subject_id,
                    or_(
                        EvidenceLink.subject_kind == "extracted_record",
                        EvidenceLink.subject_kind.startswith("extracted_record."),
                    ),
                ),
            )
            .outerjoin(
                Operation,
                and_(Operation.id == EvidenceLink.subject_id, EvidenceLink.subject_kind == "operation"),
            )
            .outerjoin(Event, and_(Event.id == EvidenceLink.subject_id, EvidenceLink.subject_kind == "event"))
            .where(
                EvidenceLink.org_id == scope.org_id,
                evidence_document_not_quarantined_condition(),
            )
            .order_by(EvidenceLink.created_at.desc())
            .limit(200)
        )
        if scope.well_id:
            stmt = stmt.where(EvidenceLink.well_id == scope.well_id)
        else:
            project_wells = select(Well.id).where(
                Well.org_id == scope.org_id,
                Well.project_id == scope.project_id,
            )
            stmt = stmt.where(
                or_(EvidenceLink.well_id.in_(project_wells), Document.project_id == scope.project_id)
            )
        scope_condition = _evidence_scope_condition(scope)
        if scope_condition is not None:
            stmt = stmt.where(scope_condition)
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            ids.append(row.id)
            section.items.append(
                ContextItem(
                    kind="evidence_link",
                    id=row.id,
                    label=f"{row.subject_kind}:{row.subject_id} ← {row.evidence_kind}",
                    data={
                        "subject_kind": row.subject_kind,
                        "subject_id": row.subject_id,
                        "evidence_kind": row.evidence_kind,
                        "evidence_id": row.evidence_id,
                        "document_id": row.document_id,
                        "page_number": row.page_number,
                        "locator": row.locator,
                        "excerpt": (row.excerpt or "")[:600] or None,
                        "method": row.method,
                        "quote_verified": row.quote_verified,
                    },
                    source_kind="document",
                    source_ids=[row.id, *([row.evidence_id] if row.evidence_id else [])],
                    cites=[row.document_id] if row.document_id else [],
                    confidence=row.confidence,
                )
            )
        if not section.items:
            return _empty(section, "no evidence links recorded in this scope"), []
        return section, ids


class EngineResultsProvider(SectionProvider):
    key = "engine_results"
    required_permission = "engine.read"
    title = "Engineering engine results"
    priority = 70

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id and not scope.project_id:
            return _empty(section, "no well or project in scope"), []
        if scope.formation_id is not None or scope.depth_from_si is not None or scope.depth_to_si is not None:
            return _empty(section, "engine runs do not carry a validated formation/depth locator"), []
        stmt = select(EngineRun).where(EngineRun.org_id == scope.org_id, EngineRun.status == "succeeded")
        if scope.project_id:
            stmt = stmt.where(EngineRun.project_id == scope.project_id)
        if scope.well_id:
            stmt = stmt.where(EngineRun.well_id == scope.well_id)
        if scope.wellbore_id:
            stmt = stmt.where(EngineRun.wellbore_id == scope.wellbore_id)
        if scope.section_id:
            stmt = stmt.where(EngineRun.section_id == scope.section_id)
        if scope.operation_id:
            stmt = stmt.where(EngineRun.operation_id == scope.operation_id)
        if scope.period_from is not None or scope.period_to is not None:
            observed = func.coalesce(EngineRun.finished_at, EngineRun.started_at, EngineRun.created_at)
            stmt = stmt.where(*_interval_scope_conditions(scope, observed, observed))
        stmt = stmt.order_by(EngineRun.created_at.desc()).limit(100)
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            ids.append(row.id)
            section.items.append(
                ContextItem(
                    kind=f"engine_run.{row.engine_key}",
                    id=row.id,
                    label=f"{row.engine_key} v{row.engine_version}",
                    data={
                        "engine_key": row.engine_key,
                        "engine_version": row.engine_version,
                        "inputs": row.inputs,
                        "outputs": row.outputs,
                        "assumptions": row.assumptions,
                        "limitations": row.limitations,
                        "warnings": row.warnings,
                        "is_feasible": row.is_feasible,
                        "constraint_violations": row.constraint_violations,
                        "units": row.units,
                    },
                    source_kind="engine",
                    source_ids=[row.id],
                    data_quality="verified" if row.is_feasible else "fair",
                    observed_at=row.finished_at or row.created_at,
                )
            )
        if not section.items:
            return _empty(section, "no engine results recorded in this scope"), []
        return section, ids


class RecommendationsProvider(SectionProvider):
    key = "recommendations"
    required_permission = "recommendation.read"
    title = "Recommendations and their status"
    priority = 75

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id and not scope.project_id:
            return _empty(section, "no well or project in scope"), []
        if scope.formation_id is not None:
            return _empty(section, "recommendations do not carry a formation locator"), []
        stmt = select(Recommendation).where(Recommendation.org_id == scope.org_id)
        if scope.project_id:
            stmt = stmt.where(Recommendation.project_id == scope.project_id)
        if scope.well_id:
            stmt = stmt.where(Recommendation.well_id == scope.well_id)
        if scope.wellbore_id:
            stmt = stmt.where(Recommendation.wellbore_id == scope.wellbore_id)
        if scope.section_id:
            stmt = stmt.where(Recommendation.section_id == scope.section_id)
        if scope.operation_id:
            stmt = stmt.where(Recommendation.operation_id == scope.operation_id)
        if scope.depth_from_si is not None or scope.depth_to_si is not None:
            stmt = stmt.where(
                *_depth_scope_conditions(scope, Recommendation.depth_from_md_si, Recommendation.depth_to_md_si)
            )
        if scope.period_from is not None or scope.period_to is not None:
            # Creation time is not a substitute for a declared recommendation validity window.
            stmt = stmt.where(Recommendation.valid_from.is_not(None))
            if scope.period_from is not None:
                stmt = stmt.where(
                    or_(Recommendation.valid_to.is_(None), Recommendation.valid_to >= scope.period_from)
                )
            if scope.period_to is not None:
                stmt = stmt.where(Recommendation.valid_from <= scope.period_to)
        stmt = stmt.order_by(Recommendation.created_at.desc()).limit(100)
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            ids.append(row.id)
            section.items.append(
                ContextItem(
                    kind="recommendation",
                    id=row.id,
                    label=row.title,
                    data={
                        "domain": row.domain,
                        "kind": row.kind,
                        "statement": row.statement,
                        "parameters": row.parameters,
                        "rationale": row.rationale,
                        "why_not": row.why_not,
                        "assumptions": row.assumptions,
                        "constraints_applied": row.constraints_applied,
                        "alternatives": row.alternatives,
                        "sensitivities": row.sensitivities,
                        "uncertainty": row.uncertainty,
                        "confidence": row.confidence,
                        "confidence_basis": row.confidence_basis,
                        "status": row.status,
                        "action_level": row.action_level,
                        "engine_run_ids": row.engine_run_ids,
                        "offset_well_ids": row.offset_well_ids,
                    },
                    source_kind="engine",
                    source_ids=[row.id, *(row.engine_run_ids or [])],
                    confidence=row.confidence,
                    data_quality=row.data_quality,
                )
            )
        if not section.items:
            return _empty(section, "no recommendations in this scope"), []
        return section, ids


class RiskProvider(SectionProvider):
    key = "risks"
    required_permission = "well.read"
    title = "Risks"
    priority = 80

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id and not scope.project_id:
            return _empty(section, "no well or project in scope"), []
        if scope.period_from is not None or scope.period_to is not None:
            return _empty(section, "risks do not carry validated observation or validity times"), []
        stmt = select(Risk).where(Risk.org_id == scope.org_id)
        if scope.project_id:
            stmt = stmt.where(Risk.project_id == scope.project_id)
        if scope.well_id:
            stmt = stmt.where(Risk.well_id == scope.well_id)
        if scope.wellbore_id:
            stmt = stmt.where(Risk.wellbore_id == scope.wellbore_id)
        if scope.section_id:
            stmt = stmt.where(Risk.section_id == scope.section_id)
        if scope.operation_id:
            stmt = stmt.where(Risk.operation_id == scope.operation_id)
        if scope.formation_id:
            stmt = stmt.where(Risk.formation_id == scope.formation_id)
        if scope.depth_from_si is not None or scope.depth_to_si is not None:
            stmt = stmt.where(*_depth_scope_conditions(scope, Risk.depth_from_md_si, Risk.depth_to_md_si))
        stmt = stmt.order_by(Risk.probability.desc(), Risk.impact.desc()).limit(100)
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            ids.append(row.id)
            section.items.append(
                ContextItem(
                    kind="risk",
                    id=row.id,
                    label=row.title,
                    data={
                        "category": row.category,
                        "description": row.description,
                        "cause": row.cause,
                        "consequence": row.consequence,
                        "probability": row.probability,
                        "impact": row.impact,
                        "severity": row.severity,
                        "mitigation": row.mitigation,
                        "contingency": row.contingency,
                        "status": row.status,
                        "depth_from_md_si": row.depth_from_md_si,
                        "depth_to_md_si": row.depth_to_md_si,
                        "source": row.source,
                    },
                    source_kind="database",
                    source_ids=[row.id],
                    cites=[row.evidence_ref] if row.evidence_ref else [],
                    units={"depth_from_md_si": "m", "depth_to_md_si": "m"},
                )
            )
        if not section.items:
            return _empty(section, "no risks captured for this scope"), []
        return section, ids


class LessonsProvider(SectionProvider):
    key = "lessons"
    required_permission = "well.read"
    title = "Lessons learned and offsets"
    priority = 85

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id and not scope.project_id:
            return _empty(section, "no well or project in scope"), []
        if (
            scope.formation_id is not None
            or scope.depth_from_si is not None
            or scope.depth_to_si is not None
            or scope.period_from is not None
            or scope.period_to is not None
        ):
            return _empty(section, "lessons do not carry validated formation/depth/time locators"), []
        lesson_stmt = select(Lesson).where(Lesson.org_id == scope.org_id)
        if scope.project_id:
            lesson_stmt = lesson_stmt.where(Lesson.project_id == scope.project_id)
        if scope.well_id:
            lesson_stmt = lesson_stmt.where(Lesson.well_id == scope.well_id)
        if scope.wellbore_id:
            lesson_stmt = lesson_stmt.where(Lesson.wellbore_id == scope.wellbore_id)
        if scope.section_id:
            lesson_stmt = lesson_stmt.where(Lesson.section_id == scope.section_id)
        if scope.operation_id:
            lesson_stmt = lesson_stmt.where(Lesson.operation_id == scope.operation_id)
        lessons = (
            await session.execute(
                lesson_stmt.order_by(Lesson.recurrence_count.desc(), Lesson.created_at.desc()).limit(50)
            )
        ).scalars().all()
        offset_stmt = (
            select(OffsetCandidate)
            .join(Well, Well.id == OffsetCandidate.subject_well_id)
            .where(
                OffsetCandidate.org_id == scope.org_id,
                Well.org_id == scope.org_id,
                OffsetCandidate.is_selected.is_(True),
            )
        )
        if scope.project_id:
            offset_stmt = offset_stmt.where(Well.project_id == scope.project_id)
        if scope.well_id:
            offset_stmt = offset_stmt.where(OffsetCandidate.subject_well_id == scope.well_id)
        if scope.wellbore_id:
            offset_stmt = offset_stmt.where(OffsetCandidate.subject_wellbore_id == scope.wellbore_id)
        if scope.section_id:
            offset_stmt = offset_stmt.where(OffsetCandidate.subject_section_id == scope.section_id)
        if scope.operation_id:
            offset_stmt = offset_stmt.where(false())
        offsets = (
            await session.execute(offset_stmt.order_by(OffsetCandidate.rank).limit(30))
        ).scalars().all()
        ids: list[str] = []
        for row in lessons:
            ids.append(row.id)
            section.items.append(
                ContextItem(
                    kind="lesson",
                    id=row.id,
                    label=row.title,
                    data={
                        "category": row.category,
                        "description": row.description,
                        "recommendation": row.recommendation,
                        "recurrence_count": row.recurrence_count,
                        "is_recurring": row.is_recurring,
                        "status": row.status,
                        "applicability": row.applicability,
                    },
                    source_kind="database",
                    source_ids=[row.id],
                    cites=[row.evidence_ref] if row.evidence_ref else [],
                )
            )
        for row in offsets:
            ids.append(row.id)
            section.items.append(
                ContextItem(
                    kind="offset_candidate",
                    id=row.id,
                    label=row.candidate_name or row.candidate_well_id,
                    data={
                        "candidate_well_id": row.candidate_well_id,
                        "overall_similarity": row.overall_similarity,
                        "rank": row.rank,
                        "weight": row.weight,
                        "criteria": row.criteria,
                        "criteria_weights": row.criteria_weights,
                        "rationale": row.rationale,
                        "exclusions": row.exclusions,
                        "comparison": row.comparison,
                        "lessons": row.lessons,
                        "data_quality": row.data_quality,
                        "engine_run_id": row.engine_run_id,
                    },
                    source_kind="engine",
                    source_ids=[row.id, *([row.engine_run_id] if row.engine_run_id else [])],
                )
            )
        if not section.items:
            return _empty(section, "no lessons or selected offsets for this well"), []
        return section, ids


class MaterialsProvider(SectionProvider):
    key = "materials"
    required_permission = "well.read"
    title = "BOM, consumables and readiness"
    priority = 90

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id and not scope.project_id:
            return _empty(section, "no well or project in scope"), []
        if scope.formation_id is not None or scope.depth_from_si is not None or scope.depth_to_si is not None:
            return _empty(section, "BOM requirements do not carry validated formation/depth locators"), []
        stmt = (
            select(BomRequirement)
            .join(Well, Well.id == BomRequirement.well_id)
            .where(BomRequirement.org_id == scope.org_id, Well.org_id == scope.org_id)
        )
        if scope.project_id:
            stmt = stmt.where(Well.project_id == scope.project_id)
        if scope.well_id:
            stmt = stmt.where(BomRequirement.well_id == scope.well_id)
        if scope.wellbore_id:
            stmt = stmt.where(BomRequirement.wellbore_id == scope.wellbore_id)
        if scope.section_id:
            stmt = stmt.where(BomRequirement.section_id == scope.section_id)
        if scope.operation_id:
            stmt = stmt.where(BomRequirement.operation_id == scope.operation_id)
        if scope.period_from is not None or scope.period_to is not None:
            stmt = stmt.where(*_interval_scope_conditions(scope, BomRequirement.required_by, BomRequirement.required_by))
        rows = (await session.execute(stmt.order_by(BomRequirement.category, BomRequirement.description))).scalars().all()
        ids: list[str] = []
        for row in rows:
            ids.append(row.id)
            section.items.append(
                ContextItem(
                    kind="bom_requirement",
                    id=row.id,
                    label=row.description,
                    data={
                        "category": row.category,
                        "specification": row.specification,
                        "quantity": row.quantity,
                        "unit": row.unit,
                        "contingency_quantity": row.contingency_quantity,
                        "required_by": row.required_by.isoformat() if row.required_by else None,
                        "status": row.status,
                        "availability": row.availability,
                        "lead_time_days": row.lead_time_days,
                        "is_available": row.is_available,
                        "is_certified": row.is_certified,
                        "risk_if_missing": row.risk_if_missing,
                        "source": row.source,
                    },
                    source_kind="database",
                    source_ids=[row.id],
                    cites=[row.evidence_ref] if row.evidence_ref else [],
                    units={"quantity": row.unit or "1", "contingency_quantity": row.unit or "1", "lead_time_days": "d"},
                )
            )
        if not section.items:
            return _empty(section, "no BOM requirements recorded for this scope"), []
        return section, ids


class DocumentChunkProvider(SectionProvider):
    """Lexical/semantic retrieval hits, used by RAG and by prompt sections that need quotes."""

    key = "document_chunks"
    required_permission = "document.read"
    title = "Relevant document passages"
    priority = 95

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id and not scope.project_id:
            return _empty(section, "no well or project in scope"), []
        stmt = (
            select(DocumentChunk)
            .join(Document, Document.id == DocumentChunk.document_id)
            .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
            .where(
                DocumentChunk.org_id == scope.org_id,
                Document.org_id == scope.org_id,
                document_not_quarantined_condition(),
                *chunk_document_scope_conditions(),
            )
            .order_by(DocumentChunk.created_at.desc())
            .limit(80)
        )
        if scope.project_id:
            stmt = stmt.where(Document.project_id == scope.project_id)
        if scope.well_id:
            stmt = stmt.where(DocumentChunk.well_id == scope.well_id)
        if scope.wellbore_id:
            stmt = stmt.where(DocumentChunk.wellbore_id == scope.wellbore_id)
        if scope.section_id:
            stmt = stmt.where(DocumentChunk.section_id == scope.section_id)
        if scope.operation_id:
            stmt = stmt.where(DocumentChunk.operation_id == scope.operation_id)
        if scope.formation_id:
            stmt = stmt.where(DocumentChunk.formation_id == scope.formation_id)
        if scope.depth_from_si is not None or scope.depth_to_si is not None:
            stmt = stmt.where(*_depth_scope_conditions(scope, DocumentChunk.depth_from_si, DocumentChunk.depth_to_si))
        if scope.period_from is not None or scope.period_to is not None:
            stmt = stmt.where(*_interval_scope_conditions(scope, DocumentChunk.period_start, DocumentChunk.period_end))
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            ids.append(row.id)
            section.items.append(
                ContextItem(
                    kind="document_chunk",
                    id=row.id,
                    label=f"{row.doc_type or 'document'} p.{row.page_number}",
                    data={
                        "text": row.text,
                        "kind": row.kind,
                        "page_number": row.page_number,
                        "document_id": row.document_id,
                        "region_id": row.region_id,
                        "depth_from_si": row.depth_from_si,
                        "depth_to_si": row.depth_to_si,
                    },
                    source_kind="document",
                    source_ids=[row.id, row.document_id],
                    cites=[row.document_id],
                    units={"depth_from_si": "m", "depth_to_si": "m"},
                )
            )
        if not section.items:
            return _empty(section, "no indexed passages for this scope"), []
        return section, ids


# --------------------------------------------------------------------------- builder


#: Default section sets by purpose. A purpose never sees everything: a dashboard does not need
#: 200 document chunks, and a prompt does not need the whole raw document list.
PURPOSE_SECTIONS: dict[ContextPurpose, tuple[str, ...]] = {
    ContextPurpose.PROMPT: (
        "well_identity",
        "well_sections",
        "twin_state",
        "formations",
        "engine_results",
        "recommendations",
        "risks",
        "lessons",
        "extracted_records",
        "documents",
        "evidence",
        "document_chunks",
    ),
    ContextPurpose.AGENT: (
        "well_identity",
        "wellbores",
        "well_sections",
        "trajectory",
        "twin_state",
        "formations",
        "operations",
        "engine_results",
        "recommendations",
        "risks",
        "lessons",
        "materials",
        "evidence",
        "document_chunks",
    ),
    ContextPurpose.ENGINE: ("well_identity", "well_sections", "twin_state", "formations"),
    ContextPurpose.WORKFLOW: (
        "well_identity",
        "well_sections",
        "twin_state",
        "operations",
        "engine_results",
        "recommendations",
        "documents",
    ),
    ContextPurpose.DASHBOARD: (
        "well_identity",
        "well_sections",
        "twin_state",
        "operations",
        "recommendations",
        "risks",
        "materials",
    ),
    ContextPurpose.RAG: ("well_identity", "well_sections", "document_chunks", "extracted_records", "evidence"),
    ContextPurpose.EXPORT: ("well_identity", "wellbores", "well_sections", "trajectory", "twin_state", "operations"),
}

DEFAULT_TOKEN_BUDGET: dict[ContextPurpose, int | None] = {
    ContextPurpose.PROMPT: 24000,
    ContextPurpose.AGENT: 60000,
    ContextPurpose.ENGINE: None,
    ContextPurpose.WORKFLOW: 32000,
    ContextPurpose.DASHBOARD: 12000,
    ContextPurpose.RAG: 16000,
    ContextPurpose.EXPORT: None,
}


class ContextBuilder:
    """Assembles :class:`ContextBundle` objects from registered providers."""

    def __init__(self, providers: Iterable[SectionProvider] | None = None) -> None:
        self.providers = _ordered(providers if providers is not None else _PROVIDERS.values())

    async def build(self, session: AsyncSession, request: ContextRequest) -> ContextBundle:
        allowed = [provider for provider in self.providers if _permitted(provider, request)]
        requested = set(request.sections) if request.sections is not None else set(PURPOSE_SECTIONS[request.purpose])
        selected = [provider for provider in allowed if provider.key in requested]
        unknown = requested - {provider.key for provider in self.providers}
        if unknown:
            raise ContextError(f"unknown context sections requested: {', '.join(sorted(unknown))}")
        # A section dropped by permissions is reported, never silently missing: a caller must be
        # able to tell "there is no data" from "you are not allowed to see the data".
        redacted = [
            provider.key
            for provider in self.providers
            if provider.key in requested and provider not in allowed
        ]

        metrics = get_metrics()
        bundle = ContextBundle(
            bundle_id=new_id("ctx"),
            scope=request.scope,
            purpose=request.purpose,
            unit_system=request.unit_system,
            locale=request.locale,
            twin_snapshot_id=request.twin_snapshot_id,
            token_budget=request.token_budget or DEFAULT_TOKEN_BUDGET[request.purpose],
            permissions_applied=sorted(request.permissions),
            redactions=[f"{key}: permission not held" for key in sorted(redacted)],
        )

        tracer = get_tracer()
        sections: list[ContextSection] = []
        source_ids: list[str] = []
        for provider in selected:
            with tracer.span(
                f"context.section.{provider.key}",
                attributes={"context.purpose": str(request.purpose), "context.section": provider.key},
            ) as span:
                try:
                    section, ids = await provider.build(session, request)
                except Exception as exc:
                    span.record_error(exc)
                    metrics.counter("drillai.context.provider_errors", "context provider failures").inc(
                        1, section=provider.key
                    )
                    logger.warning(
                        "context provider failed",
                        extra={"extra_fields": {"section": provider.key, "error": str(exc)}},
                    )
                    section = ContextSection(
                        key=provider.key,
                        title=provider.title,
                        empty_reason=f"section unavailable: {exc}",
                    )
                    ids = []
                span.set_attributes({"context.items": len(section.items), "context.omitted": section.omitted_items})
            if provider.key == "documents" and not request.include_documents:
                continue
            if provider.key == "evidence" and not request.include_evidence:
                continue
            if provider.key == "engine_results" and not request.include_engine_results:
                continue
            if not section.items and section.empty_reason is None:
                section.empty_reason = "no data in scope"
            sections.append(_trim(section, request.max_items_per_section))
            source_ids.extend(ids)

        truncated_sections: list[ContextSection] = []
        estimated = 0
        if bundle.token_budget is not None:
            # Walk sections in priority order; once the budget is spent the rest are omitted.
            for section in sections:
                cost = _section_tokens(section)
                if estimated + cost > bundle.token_budget:
                    bundle.omitted_sections.append(section.key)
                    bundle.truncated = True
                    metrics.counter("drillai.context.sections_omitted", "sections dropped by the token budget").inc(
                        1, section=section.key
                    )
                    continue
                estimated += cost
                truncated_sections.append(section)
            sections = truncated_sections
        else:
            estimated = sum(_section_tokens(section) for section in sections)

        bundle.sections = sections
        bundle.token_estimate = estimated
        bundle.limitations = [
            "only data visible to the requesting principal is included",
            "values are stored in canonical SI units; the requested display system is applied at render time",
        ]
        if bundle.omitted_sections:
            bundle.notes.append(
                "sections omitted to respect the token budget: " + ", ".join(bundle.omitted_sections)
            )
        if any(section.truncated for section in sections):
            bundle.notes.append("some sections were truncated to the per-section item limit")
        metrics.counter("drillai.context.built", "context bundles built").inc(1, purpose=str(request.purpose))
        metrics.histogram("drillai.context.tokens", "estimated context tokens").observe(
            estimated, purpose=str(request.purpose)
        )
        return bundle

    def render_prompt_payload(self, bundle: ContextBundle, *, max_chars: int | None = None) -> str:
        """Deterministic, citation-carrying rendering of a bundle for an LLM prompt.

        The rendering is intentionally plain text with one line per fact, prefixed by a citation
        marker. Every engineering number in a prompt therefore has a traceable origin, and a
        model that produces a number without one can be rejected mechanically.
        """
        lines: list[str] = []
        scope = bundle.scope
        lines.append("ENGINEERING CONTEXT")
        lines.append(f"scope: well={scope.well_id or '-'} wellbore={scope.wellbore_id or '-'} "
                     f"section={scope.section_id or '-'} operation={scope.operation_id or '-'}")
        if scope.depth_from_si is not None or scope.depth_to_si is not None:
            lines.append(
                f"depth window: {_fmt(scope.depth_from_si)}–{_fmt(scope.depth_to_si)} m (MD)"
            )
        if scope.period_from or scope.period_to:
            lines.append(f"period: {_fmt(scope.period_from)} – {_fmt(scope.period_to)}")
        lines.append(f"units: {bundle.unit_system} (values stored in SI)")
        lines.append("")
        for section in bundle.sections:
            lines.append(f"## {section.title} [{section.key}]")
            if section.empty_reason and not section.items:
                lines.append(f"(no data: {section.empty_reason})")
            for item in section.items:
                cite = f" [{','.join(item.cites)}]" if item.cites else ""
                state = f" ({item.state_kind})" if item.state_kind else ""
                lines.append(f"- {item.kind}{state}: {item.label or item.id or ''}{cite}")
                for key, value in sorted(item.data.items()):
                    if value is None or value == [] or value == {}:
                        continue
                    unit = f" {item.units[key]}" if key in item.units else ""
                    lines.append(f"    {key} = {_compact(value)}{unit}")
            if section.truncated:
                lines.append(f"    … {section.omitted_items} further item(s) omitted")
            lines.append("")
        if bundle.assumptions:
            lines.append("ASSUMPTIONS")
            lines.extend(f"- {assumption}" for assumption in bundle.assumptions)
        if bundle.limitations:
            lines.append("LIMITATIONS")
            lines.extend(f"- {limitation}" for limitation in bundle.limitations)
        payload = "\n".join(lines)
        if max_chars is not None and len(payload) > max_chars:
            return payload[: max_chars - 40] + "\n\n[context truncated by size limit]"
        return payload


def _permitted(provider: SectionProvider, request: ContextRequest) -> bool:
    """Is this section visible to the requesting principal?

    ``request.permissions`` is the principal's permission set and may contain role patterns
    (``well.*``), so it is evaluated with the same matcher the security layer uses: a section must
    not be visible in one layer and invisible in another. An empty set means an internal caller
    (workflow, engine, CLI) whose authorization was already established by the action gate, and
    then every read-only section is available.
    """
    permissions = request.permissions
    if not permissions:
        return provider.required_permission.endswith(".read")
    return permissions_match(permissions, provider.required_permission)


def _section_tokens(section: ContextSection) -> int:
    total = _estimate_tokens(section.title) + 8
    for item in section.items:
        total += _estimate_tokens(str(item.data)) + 12
    return total


def _compact(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:,.6g}"
    if isinstance(value, (list, tuple)):
        if len(value) > 8:
            return ", ".join(str(item) for item in value[:8]) + f", … (+{len(value) - 8})"
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return ", ".join(f"{key}={_compact(val)}" for key, val in sorted(value.items())[:8])
    return str(value)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, float):
        return f"{value:,.3f}"
    return str(value)


# --------------------------------------------------------------------------- defaults


def _default_providers() -> tuple[SectionProvider, ...]:
    return (
        WellIdentityProvider(),
        WellboresProvider(),
        WellSectionsProvider(),
        TrajectoryProvider(),
        FormationProvider(),
        TwinAspectProvider(),
        OperationsProvider(),
        DocumentsProvider(),
        ExtractedRecordsProvider(),
        EvidenceProvider(),
        EngineResultsProvider(),
        RecommendationsProvider(),
        RiskProvider(),
        LessonsProvider(),
        MaterialsProvider(),
        DocumentChunkProvider(),
    )


def install_default_providers() -> None:
    for provider in _default_providers():
        register_section_provider(provider, replace=True)


install_default_providers()

_default_builder: ContextBuilder | None = None


def default_context_builder() -> ContextBuilder:
    global _default_builder
    if _default_builder is None:
        _default_builder = ContextBuilder()
    return _default_builder


def provider_is_registered(key: str) -> bool:
    return key in _PROVIDERS
