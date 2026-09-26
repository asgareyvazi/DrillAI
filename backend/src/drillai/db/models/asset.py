"""Asset hierarchy and the physical description of a well.

``Organization → Project → Field → Well → Wellbore → Section`` mirrors how the industry
actually governs assets (identical to the OSDU master-data split of ``Well`` and
``Wellbore``): a *well* is the legal/physical asset with a surface location and a UWI,
a *wellbore* is one drilled hole (the original hole, a sidetrack, a re-drill). The
distinction matters constantly in practice — a well can have several wellbores, each with
its own trajectory, sections, formations and operations.

Trajectory is deliberately *not* denormalised into the well row: plans, as-drilled and
revised trajectories coexist, and every survey station carries its own source and
quality — an offset-comparison or collision-avoidance result is only defensible if the
survey provenance is preserved.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import Boolean, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from drillai.db.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    JsonType,
    Money,
    OrgScopedMixin,
    TextType,
    TimestampMixin,
    UtcDateTime,
)

WELL_TYPES = (
    "exploration",
    "appraisal",
    "development_producer",
    "development_injector",
    "observation",
    "water_source",
    "disposal",
    "sidetrack",
    "reentry",
)
WELL_STATUSES = (
    "planned",
    "permitting",
    "drilling",
    "completing",
    "producing",
    "shut_in",
    "intervention",
    "suspended",
    "abandoned",
    "p&a",
)
WELLBORE_PURPOSES = ("original", "sidetrack", "bypass", "reentry", "reamed", "pilot", "contingency")
SECTION_KINDS = (
    "conductor",
    "surface",
    "intermediate",
    "production",
    "liner",
    "tieback",
    "open_hole",
    "rathole",
)
TRAJECTORY_KINDS = ("plan", "design", "actual", "revised", "proposed", "interpolated", "anti_collision")
SURVEY_SOURCES = ("plan", "mwd", "ems", "gyro", "north_seeking_gyro", "tie_in", "interpolated", "manual")
DATUMS = ("rkb", "msl", "gl", "cf", "derrick_floor")


class Project(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    __tablename__ = "projects"
    id_prefix = "prj"

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    code: Mapped[str | None] = mapped_column(String(60), index=True)
    operator: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(80))
    basin: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(40), default="active", nullable=False)
    phase: Mapped[str | None] = mapped_column(String(60))
    description: Mapped[str | None] = mapped_column(TextType)
    budget_usd: Mapped[Decimal | None] = mapped_column(Money)
    settings: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    datum_policy: Mapped[str] = mapped_column(
        String(32), default="rkb", nullable=False, comment="default trajectory datum for the project"
    )

    fields: Mapped[list[Field]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Field(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    __tablename__ = "fields"
    id_prefix = "fld"

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    aliases: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    country: Mapped[str | None] = mapped_column(String(80))
    basin: Mapped[str | None] = mapped_column(String(120))
    water_depth_si: Mapped[float | None] = mapped_column(Float)
    centroid_lat: Mapped[float | None] = mapped_column(Float)
    centroid_lon: Mapped[float | None] = mapped_column(Float)
    geo_meta: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="active", nullable=False)
    notes: Mapped[str | None] = mapped_column(TextType)

    project: Mapped[Project] = relationship(back_populates="fields")
    wells: Mapped[list[Well]] = relationship(back_populates="field")


class Rig(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Rig master data. Rig limits are inputs to feasibility, so they are modelled."""

    __tablename__ = "rigs"
    id_prefix = "rig"

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    contractor: Mapped[str | None] = mapped_column(String(200))
    rig_type: Mapped[str] = mapped_column(String(40), default="land", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="available", nullable=False)
    country: Mapped[str | None] = mapped_column(String(80))
    current_well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # Limits in SI; see docs/ENGINE_CONTRACTS.md for the recognised keys.
    limits: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    capabilities: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    contract_start: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    day_rate_usd: Mapped[Decimal | None] = mapped_column(Money)


class Well(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """The legal/physical well asset (surface location, UWI, datums, objectives)."""

    __tablename__ = "wells"
    id_prefix = "wel"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    field_id: Mapped[str | None] = mapped_column(ForeignKey("fields.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    uwi: Mapped[str | None] = mapped_column(String(80), index=True)
    api_number: Mapped[str | None] = mapped_column(String(40))
    well_type: Mapped[str] = mapped_column(String(40), default="development_producer", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="planned", nullable=False, index=True)
    spud_date: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    release_date: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    # Datums: all elevations in SI metres, referenced to the stated datum.
    surface_lat: Mapped[float | None] = mapped_column(Float)
    surface_lon: Mapped[float | None] = mapped_column(Float)
    kb_elevation_si: Mapped[float | None] = mapped_column(Float, comment="rotary kelly bushing elevation, m")
    ground_elevation_si: Mapped[float | None] = mapped_column(Float, comment="ground elevation, m")
    water_depth_si: Mapped[float | None] = mapped_column(Float)
    elevation_datum: Mapped[str] = mapped_column(String(24), default="msl", nullable=False)
    slot: Mapped[str | None] = mapped_column(String(40))
    pad_name: Mapped[str | None] = mapped_column(String(120))
    is_offshore: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rig_id: Mapped[str | None] = mapped_column(ForeignKey("rigs.id", ondelete="SET NULL"), index=True)
    operator: Mapped[str | None] = mapped_column(String(200))
    total_depth_planned_si: Mapped[float | None] = mapped_column(Float)
    twin_state: Mapped[str] = mapped_column(
        String(32), default="planned", nullable=False, comment="planned|drilling|completed|production|abandoned"
    )
    objectives: Mapped[str | None] = mapped_column(TextType)
    target_formations: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    is_demo_fixture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    project: Mapped[Project] = relationship()
    field: Mapped[Field | None] = relationship(back_populates="wells")
    wellbores: Mapped[list[Wellbore]] = relationship(
        back_populates="well", cascade="all, delete-orphan", order_by="Wellbore.sequence"
    )


class Wellbore(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    __tablename__ = "wellbores"
    id_prefix = "wlb"

    well_id: Mapped[str] = mapped_column(ForeignKey("wells.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    purpose: Mapped[str] = mapped_column(String(40), default="original", nullable=False)
    sequence: Mapped[int] = mapped_column(default=1, nullable=False)
    parent_wellbore_id: Mapped[str | None] = mapped_column(ForeignKey("wellbores.id", ondelete="SET NULL"), index=True)
    status: Mapped[str] = mapped_column(String(40), default="planned", nullable=False)
    kickoff_md_si: Mapped[float | None] = mapped_column(Float)
    planned_td_md_si: Mapped[float | None] = mapped_column(Float)
    planned_td_tvd_si: Mapped[float | None] = mapped_column(Float)
    actual_td_md_si: Mapped[float | None] = mapped_column(Float)
    actual_td_tvd_si: Mapped[float | None] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    datum: Mapped[str] = mapped_column(String(24), default="rkb", nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)

    well: Mapped[Well] = relationship(back_populates="wellbores")
    sections: Mapped[list[WellSection]] = relationship(
        back_populates="wellbore", cascade="all, delete-orphan", order_by="WellSection.sequence"
    )
    trajectories: Mapped[list[Trajectory]] = relationship(
        back_populates="wellbore", cascade="all, delete-orphan"
    )


class Formation(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Formation catalog — shared across a project so casing seats, parameters and risks
    can be tied to stratigraphy instead of to a single well (SLB "dynamic template"
    pattern, see ARCHITECTURE_RESEARCH.md)."""

    __tablename__ = "formations"
    id_prefix = "frm"
    __table_args__ = (UniqueConstraint("org_id", "name"),)

    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"), index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    aliases: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    parent_formation_id: Mapped[str | None] = mapped_column(ForeignKey("formations.id", ondelete="SET NULL"))
    lithology: Mapped[str | None] = mapped_column(String(120))
    age: Mapped[str | None] = mapped_column(String(120))
    depth_reference_si: Mapped[float | None] = mapped_column(Float)
    characteristics: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    is_reservoir: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WellFormationMarker(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """A formation top/base as encountered (or predicted) in a specific wellbore.

    ``source`` and ``confidence`` are mandatory because offset projection and log
    interpretation are not equivalent evidence.
    """

    __tablename__ = "well_formation_markers"
    id_prefix = "wfm"

    wellbore_id: Mapped[str] = mapped_column(ForeignKey("wellbores.id", ondelete="CASCADE"), nullable=False, index=True)
    formation_id: Mapped[str] = mapped_column(ForeignKey("formations.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="top", nullable=False)
    md_si: Mapped[float | None] = mapped_column(Float)
    tvd_si: Mapped[float | None] = mapped_column(Float)
    tvdss_si: Mapped[float | None] = mapped_column(Float)
    thickness_si: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40), default="plan", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    uncertainty_si: Mapped[float | None] = mapped_column(Float)
    evidence_ref: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(TextType)


class WellSection(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A hole section: planned and actual geometry, casing, mud window, pressures.

    Both planned and actual values live here (distinct columns) because comparing plan
    vs actual at section level is a core engineering workflow. Where a value is an
    *interpretation* (pore pressure, fracture gradient) the source is recorded.
    """

    __tablename__ = "well_sections"
    id_prefix = "sec"
    __table_args__ = (UniqueConstraint("wellbore_id", "sequence"),)

    wellbore_id: Mapped[str] = mapped_column(ForeignKey("wellbores.id", ondelete="CASCADE"), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(default=1, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="intermediate", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="planned", nullable=False)
    hole_diameter_si: Mapped[float | None] = mapped_column(Float)
    hole_diameter_nominal: Mapped[str | None] = mapped_column(String(40), comment='e.g. 8.5"')
    planned_top_md_si: Mapped[float | None] = mapped_column(Float)
    planned_bottom_md_si: Mapped[float | None] = mapped_column(Float)
    actual_top_md_si: Mapped[float | None] = mapped_column(Float)
    actual_bottom_md_si: Mapped[float | None] = mapped_column(Float)
    current_md_si: Mapped[float | None] = mapped_column(Float)
    casing_od_si: Mapped[float | None] = mapped_column(Float)
    casing_od_nominal: Mapped[str | None] = mapped_column(String(40))
    casing_weight_si: Mapped[float | None] = mapped_column(Float, comment="linear mass, kg/m")
    casing_grade: Mapped[str | None] = mapped_column(String(40))
    casing_connection: Mapped[str | None] = mapped_column(String(60))
    casing_top_md_si: Mapped[float | None] = mapped_column(Float)
    casing_shoe_md_si: Mapped[float | None] = mapped_column(Float)
    cement_top_md_si: Mapped[float | None] = mapped_column(Float)
    cement_planned_top_md_si: Mapped[float | None] = mapped_column(Float)
    mud_weight_si: Mapped[float | None] = mapped_column(Float, comment="current/planned MW, kg/m3")
    mud_weight_min_si: Mapped[float | None] = mapped_column(Float)
    mud_weight_max_si: Mapped[float | None] = mapped_column(Float)
    pore_pressure_gradient_si: Mapped[float | None] = mapped_column(Float, comment="Pa/m")
    fracture_gradient_si: Mapped[float | None] = mapped_column(Float, comment="Pa/m")
    collapse_gradient_si: Mapped[float | None] = mapped_column(Float, comment="Pa/m")
    lot_fit_equivalent_mw_si: Mapped[float | None] = mapped_column(Float)
    pressure_source: Mapped[str | None] = mapped_column(String(60), comment="how pressures were derived")
    is_planned_only: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    notes: Mapped[str | None] = mapped_column(TextType)

    wellbore: Mapped[Wellbore] = relationship(back_populates="sections")


class Trajectory(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A named trajectory (plan / as-drilled / revised / proposed) for a wellbore.

    Trajectories are versioned artefacts, never overwritten: an offset study or a
    collision check performed last month must remain reproducible.
    """

    __tablename__ = "trajectories"
    id_prefix = "trj"

    wellbore_id: Mapped[str] = mapped_column(ForeignKey("wellbores.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="plan", nullable=False, index=True)
    version: Mapped[int] = mapped_column(default=1, nullable=False)
    datum: Mapped[str] = mapped_column(String(24), default="rkb", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    engine_run_id: Mapped[str | None] = mapped_column(String(64))
    source_document_id: Mapped[str | None] = mapped_column(String(64))
    station_count: Mapped[int] = mapped_column(default=0, nullable=False)
    total_md_si: Mapped[float | None] = mapped_column(Float)
    total_tvd_si: Mapped[float | None] = mapped_column(Float)
    max_inclination_deg: Mapped[float | None] = mapped_column(Float)
    target_formation_id: Mapped[str | None] = mapped_column(String(64))
    validation: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    notes: Mapped[str | None] = mapped_column(TextType)

    wellbore: Mapped[Wellbore] = relationship(back_populates="trajectories")
    stations: Mapped[list[TrajectoryStation]] = relationship(
        back_populates="trajectory", cascade="all, delete-orphan", order_by="TrajectoryStation.md_si"
    )


class TrajectoryStation(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Survey station.

    Computed columns (``tvd_si``, ``ns_si``, ``ew_si``, ``dls``) store the *as-computed*
    values produced by the trajectory engine, together with the engine version and the
    method, so a recomputation difference is visible instead of silent.
    """

    __tablename__ = "trajectory_stations"
    id_prefix = "sta"

    trajectory_id: Mapped[str] = mapped_column(
        ForeignKey("trajectories.id", ondelete="CASCADE"), nullable=False, index=True
    )
    wellbore_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    station_index: Mapped[int] = mapped_column(default=0, nullable=False)
    md_si: Mapped[float] = mapped_column(Float, nullable=False)
    inclination_deg: Mapped[float] = mapped_column(Float, nullable=False)
    azimuth_deg: Mapped[float] = mapped_column(Float, nullable=False)
    tvd_si: Mapped[float | None] = mapped_column(Float)
    ns_si: Mapped[float | None] = mapped_column(Float)
    ew_si: Mapped[float | None] = mapped_column(Float)
    tvdss_si: Mapped[float | None] = mapped_column(Float)
    dls_deg_per_30m: Mapped[float | None] = mapped_column(Float)
    build_rate_deg_per_30m: Mapped[float | None] = mapped_column(Float)
    turn_rate_deg_per_30m: Mapped[float | None] = mapped_column(Float)
    closure_distance_si: Mapped[float | None] = mapped_column(Float)
    closure_azimuth_deg: Mapped[float | None] = mapped_column(Float)
    vertical_section_si: Mapped[float | None] = mapped_column(Float)
    toolface_deg: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(40), default="plan", nullable=False)
    measured_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    magnetic_reference: Mapped[str | None] = mapped_column(String(40))
    grid_correction_deg: Mapped[float | None] = mapped_column(Float)
    quality_flags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    method: Mapped[str | None] = mapped_column(String(60), comment="e.g. minimum_curvature")
    engine_version: Mapped[str | None] = mapped_column(String(40))
    evidence_ref: Mapped[str | None] = mapped_column(String(64))
    raw_source: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)

    trajectory: Mapped[Trajectory] = relationship(back_populates="stations")
