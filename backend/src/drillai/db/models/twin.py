"""Digital Well Twin: aspects, snapshots and the change record.

The twin is **not** a table row and **not** a picture. It is a set of *aspects* of a
well's state, each of which can exist in several **states**:

``planned | actual | current | historical | predicted | recommended``

Distinguishing those states is what makes the twin answerable:

* "current depth" (``current``), "planned TD" (``planned``), "predicted pore pressure at
  the bit if we keep the current MW" (``predicted``), "recommended WOB window"
  (``recommended``) — the same aspect, different truth claims, different evidence.

Each aspect row carries its computation provenance (engine run, inputs hash, source
refs) and validity window, and is content-hashed so that a change is detectable, which
is what drives impact analysis and the timeline.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, Float, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from drillai.db.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    IntType,
    JsonType,
    OrgScopedMixin,
    TextType,
    TimestampMixin,
    UtcDateTime,
)

ASPECTS = (
    "identity",            # names, uwi, type, status, datums
    "geometry",            # wellbore path, hole/casing geometry
    "trajectory",          # planned/actual survey derived state
    "formations",          # markers and prognosed tops
    "drilling_state",      # current depth, parameters, bit on bottom
    "bha",                 # what is in hole
    "bit",
    "mud",                 # fluid properties and volumes
    "hydraulics",          # ECD, pressure profile, hole cleaning indices
    "casing",              # design, loads, actual tally
    "cement",
    "barriers",            # well barrier elements and verification
    "integrity",
    "equipment_in_hole",
    "completion",
    "intervention",
    "production",
    "operations",          # current/next/last operation
    "schedule",            # progress vs plan (days, depth, cost)
    "risk",
    "readiness",
    "services",
    "materials",
    "documents",
    "data_quality",
    "performance",         # ROP, MSE, NPT statistics
    "offset_analogue",     # which offsets are considered analogues and why
    "recommendation_state",
)

STATE_KINDS = ("planned", "actual", "current", "historical", "predicted", "recommended", "baseline", "scenario")


class TwinAspect(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """One aspect of the well state in one state-kind."""

    __tablename__ = "twin_aspects"
    id_prefix = "twn"
    __table_args__ = (
        Index("ix_twin_aspects_well_aspect_state", "well_id", "aspect", "state_kind", "is_current"),
    )

    well_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    aspect: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    state_kind: Mapped[str] = mapped_column(String(24), default="current", nullable=False, index=True)
    schema_key: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    summary: Mapped[str | None] = mapped_column(TextType)
    confidence: Mapped[float | None] = mapped_column(Float)
    data_quality: Mapped[str] = mapped_column(String(24), default="unverified", nullable=False)
    computed_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    computed_by: Mapped[str] = mapped_column(String(40), default="platform", nullable=False)
    engine_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_refs: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    assumptions: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    valid_from: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    valid_to: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    supersedes_id: Mapped[str | None] = mapped_column(String(64))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class TwinSnapshot(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """A point-in-time capture of all aspects (baseline for scenarios and comparisons)."""

    __tablename__ = "twin_snapshots"
    id_prefix = "snp"

    well_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(120), default="current", nullable=False)
    kind: Mapped[str] = mapped_column(String(24), default="current", nullable=False, index=True)
    as_of: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    aspect_ids: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    aspect_hashes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    summary: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    scenario_id: Mapped[str | None] = mapped_column(String(64), index=True)
    parent_snapshot_id: Mapped[str | None] = mapped_column(String(64), index=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    twin_state: Mapped[str] = mapped_column(String(32), default="planned", nullable=False)
    completeness: Mapped[float | None] = mapped_column(Float, comment="share of expected aspects present")
    notes: Mapped[str | None] = mapped_column(TextType)


class ChangeRecord(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Append-only change history for every governed entity.

    Answers: what changed, when, by whom, why, from what, to what, and which downstream
    components were flagged as affected. It is **not** full event sourcing of the domain
    (see ADR-0005): entities keep their current state, and this table records the
    transitions that matter for audit and impact analysis.
    """

    __tablename__ = "change_records"
    id_prefix = "chg"

    subject_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    change_type: Mapped[str] = mapped_column(String(32), default="updated", nullable=False, index=True)
    version: Mapped[int | None] = mapped_column(IntType)
    field_paths: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    before: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    after: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    reason: Mapped[str | None] = mapped_column(TextType)
    actor_kind: Mapped[str] = mapped_column(String(24), default="user", nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str | None] = mapped_column(String(40))
    source_ref: Mapped[str | None] = mapped_column(String(120))
    impact: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    impact_computed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    occurred_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64))
    approval_id: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
