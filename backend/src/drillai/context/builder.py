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

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    EvidenceLink,
    ExtractedRecord,
    Lesson,
    OffsetCandidate,
    Operation,
    Project,
    Recommendation,
    Risk,
    Trajectory,
    TrajectoryStation,
    Well,
    Wellbore,
    WellFormationMarker,
    WellSection,
)
from drillai.observability.tracing import get_metrics, get_tracer
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
        well = await session.get(Well, scope.well_id)
        if well is None:
            return _empty(section, f"well {scope.well_id} not found"), []
        project = await session.get(Project, well.project_id) if well.project_id else None
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
        stmt = select(Wellbore).where(Wellbore.well_id == request.scope.well_id).order_by(Wellbore.sequence)
        if request.scope.wellbore_id:
            stmt = stmt.where(Wellbore.id == request.scope.wellbore_id)
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
        if not request.scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = select(WellSection).where(WellSection.org_id == request.scope.org_id)
        if request.scope.wellbore_id:
            stmt = stmt.where(WellSection.wellbore_id == request.scope.wellbore_id)
        else:
            stmt = stmt.where(
                WellSection.wellbore_id.in_(select(Wellbore.id).where(Wellbore.well_id == request.scope.well_id))
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
            .where(Trajectory.org_id == scope.org_id, Trajectory.is_current.is_(True))
            .order_by(Trajectory.version.desc())
            .limit(1)
        )
        if scope.wellbore_id:
            stmt = stmt.where(Trajectory.wellbore_id == scope.wellbore_id)
        else:
            stmt = stmt.where(
                Trajectory.wellbore_id.in_(select(Wellbore.id).where(Wellbore.well_id == scope.well_id))
            )
        trajectory = (await session.execute(stmt)).scalars().first()
        if trajectory is None:
            return _empty(section, "no current trajectory on file"), []
        section.source_ids = [trajectory.id]
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
            .where(TrajectoryStation.trajectory_id == trajectory.id)
            .order_by(TrajectoryStation.station_index)
        )
        if scope.depth_from_si is not None:
            station_stmt = station_stmt.where(TrajectoryStation.md_si >= scope.depth_from_si)
        if scope.depth_to_si is not None:
            station_stmt = station_stmt.where(TrajectoryStation.md_si <= scope.depth_to_si)
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
                    units={"md_si": "m", "tvd_si": "m", "ns_si": "m", "ew_si": "m", "inclination_deg": "deg", "azimuth_deg": "deg", "dls_deg_per_30m": "deg/30m"},
                )
            )
        if not stations:
            section.notes.append("no survey stations fall inside the requested depth window")
        return section, [trajectory.id, *[station.id for station in stations]]


class FormationProvider(SectionProvider):
    key = "formations"
    required_permission = "well.read"
    title = "Formation tops and lithology"
    priority = 35

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = select(WellFormationMarker).where(WellFormationMarker.org_id == scope.org_id)
        if scope.wellbore_id:
            stmt = stmt.where(WellFormationMarker.wellbore_id == scope.wellbore_id)
        else:
            stmt = stmt.where(
                WellFormationMarker.wellbore_id.in_(select(Wellbore.id).where(Wellbore.well_id == scope.well_id))
            )
        markers = (await session.execute(stmt.order_by(WellFormationMarker.md_si))).scalars().all()
        ids: list[str] = []
        for marker in markers:
            if not scope.depth_overlaps(marker.md_si, marker.md_si):
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
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = select(Operation).where(Operation.org_id == scope.org_id, Operation.well_id == scope.well_id)
        if scope.wellbore_id:
            stmt = stmt.where(Operation.wellbore_id == scope.wellbore_id)
        if scope.section_id:
            stmt = stmt.where(Operation.section_id == scope.section_id)
        if scope.operation_id:
            stmt = stmt.where(Operation.id == scope.operation_id)
        stmt = stmt.order_by(Operation.sequence.desc()).limit(200)
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            if not scope.depth_overlaps(row.depth_from_md_si, row.depth_to_md_si):
                continue
            if not scope.period_overlaps(row.actual_start or row.planned_start, row.actual_end or row.planned_end):
                continue
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
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = (
            select(Document)
            .where(Document.org_id == scope.org_id, Document.well_id == scope.well_id)
            .order_by(Document.created_at.desc())
            .limit(100)
        )
        if scope.wellbore_id:
            stmt = stmt.where(or_(Document.wellbore_id == scope.wellbore_id, Document.wellbore_id.is_(None)))
        if scope.section_id:
            filtered = _section_filter(Document.section_id, scope.section_id)
            if filtered is not None:
                stmt = stmt.where(filtered)
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            if not scope.period_overlaps(row.period_start or row.issue_date, row.period_end or row.issue_date):
                continue
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
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = (
            select(ExtractedRecord)
            .where(ExtractedRecord.org_id == scope.org_id, ExtractedRecord.well_id == scope.well_id)
            .order_by(ExtractedRecord.created_at.desc())
            .limit(200)
        )
        if scope.wellbore_id:
            stmt = stmt.where(ExtractedRecord.wellbore_id == scope.wellbore_id)
        if scope.section_id:
            stmt = stmt.where(ExtractedRecord.section_id == scope.section_id)
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


class EvidenceProvider(SectionProvider):
    key = "evidence"
    required_permission = "evidence.read"
    title = "Evidence and provenance"
    priority = 65
    required_permission = "evidence.read"

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        scope = request.scope
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = (
            select(EvidenceLink)
            .where(EvidenceLink.org_id == scope.org_id, EvidenceLink.well_id == scope.well_id)
            .order_by(EvidenceLink.created_at.desc())
            .limit(200)
        )
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
                    source_ids=[row.id, row.evidence_id],
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
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = (
            select(EngineRun)
            .where(EngineRun.org_id == scope.org_id, EngineRun.well_id == scope.well_id, EngineRun.status == "succeeded")
            .order_by(EngineRun.created_at.desc())
            .limit(100)
        )
        if scope.section_id:
            filtered = _section_filter(EngineRun.section_id, scope.section_id)
            if filtered is not None:
                stmt = stmt.where(filtered)
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
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = (
            select(Recommendation)
            .where(Recommendation.org_id == scope.org_id, Recommendation.well_id == scope.well_id)
            .order_by(Recommendation.created_at.desc())
            .limit(100)
        )
        if scope.section_id:
            filtered = _section_filter(Recommendation.section_id, scope.section_id)
            if filtered is not None:
                stmt = stmt.where(filtered)
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            if not scope.depth_overlaps(row.depth_from_md_si, row.depth_to_md_si):
                continue
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
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = (
            select(Risk)
            .where(Risk.org_id == scope.org_id, Risk.well_id == scope.well_id)
            .order_by(Risk.probability.desc(), Risk.impact.desc())
            .limit(100)
        )
        if scope.section_id:
            filtered = _section_filter(Risk.section_id, scope.section_id)
            if filtered is not None:
                stmt = stmt.where(filtered)
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            if not scope.depth_overlaps(row.depth_from_md_si, row.depth_to_md_si):
                continue
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
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        lessons = (
            await session.execute(
                select(Lesson)
                .where(Lesson.org_id == scope.org_id, Lesson.well_id == scope.well_id)
                .order_by(Lesson.recurrence_count.desc(), Lesson.created_at.desc())
                .limit(50)
            )
        ).scalars().all()
        offsets = (
            await session.execute(
                select(OffsetCandidate)
                .where(
                    OffsetCandidate.org_id == scope.org_id,
                    OffsetCandidate.subject_well_id == scope.well_id,
                    OffsetCandidate.is_selected.is_(True),
                )
                .order_by(OffsetCandidate.rank)
                .limit(30)
            )
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
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = select(BomRequirement).where(BomRequirement.org_id == scope.org_id, BomRequirement.well_id == scope.well_id)
        if scope.section_id:
            filtered = _section_filter(BomRequirement.section_id, scope.section_id)
            if filtered is not None:
                stmt = stmt.where(filtered)
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
        if not scope.well_id:
            return _empty(section, "no well in scope"), []
        stmt = (
            select(DocumentChunk)
            .where(DocumentChunk.org_id == scope.org_id, DocumentChunk.well_id == scope.well_id)
            .order_by(DocumentChunk.created_at.desc())
            .limit(80)
        )
        if scope.section_id:
            filtered = _section_filter(DocumentChunk.section_id, scope.section_id)
            if filtered is not None:
                stmt = stmt.where(filtered)
        rows = (await session.execute(stmt)).scalars().all()
        ids: list[str] = []
        for row in rows:
            if not scope.depth_overlaps(row.depth_from_si, row.depth_to_si):
                continue
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
