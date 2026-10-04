"""Operations, events, NPT classification, time-series and alerts.

The operation chain is the backbone of the digital well twin's temporal state:

``Operation`` (planned and actual) → ``Event`` (what happened, with NPT attribution)
→ ``TimeSeries`` (what the sensors recorded) → ``Alert`` (what needs attention).

Explicit ``predecessor_operation_id`` / ``sequence`` make "what happened before / what
comes next" a first-class query rather than an inference from timestamps.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import Boolean, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from drillai.db.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    IntType,
    JsonType,
    Money,
    OrgScopedMixin,
    TextType,
    TimestampMixin,
    UtcDateTime,
)

OPERATION_KINDS = (
    "spud",
    "drilling",
    "slide_drilling",
    "rotary_drilling",
    "reaming",
    "back_reaming",
    "connection",
    "trip_in",
    "trip_out",
    "wiper_trip",
    "casing",
    "cementing",
    "curing_woc",
    "nipple_up",
    "pressure_test",
    "mud_conditioning",
    "circulating",
    "logging",
    "coring",
    "fishing",
    "milling",
    "jarring",
    "blow_out_preventer_test",
    "well_control",
    "completion",
    "perforating",
    "stimulation",
    "well_testing",
    "well_cleanup",
    "suspension",
    "abandonment",
    "p_and_a",
    "rig_up",
    "rig_down",
    "move",
    "maintenance",
    "waiting",
    "other",
)
OPERATION_PHASES = ("planning", "preparation", "executing", "completed", "suspended", "cancelled")
#: ``status`` is the work item's own condition, one level finer than the phase: a planned operation
#: can be ``planned`` or ``ready``, an executed one ``in_progress`` or ``completed``.
OPERATION_STATUSES = ("planned", "ready", "in_progress", "completed", "suspended", "cancelled")
OPERATION_CLASSES = ("plan", "actual", "forecast")

#: What a recorded row came from. A connector's reading and an operator's entry are different
#: evidence, and ``source`` (free-form) is kept for the specific system that supplied it.
SOURCE_KINDS = ("manual", "ddr_promotion", "import", "connector", "integration", "system")

#: Who established an event's cause: the report said so, a person concluded it afterwards, or nobody
#: has. An inferred cause must never render like a recorded one.
CAUSE_BASES = ("recorded", "inferred", "unknown")

#: Whether a classification came off the document or was derived by the platform.
CLASSIFICATION_SOURCES = ("recorded", "derived", "unclassified")

#: The life cycle of an event. An ``open`` event is work; a ``closed`` one is history. ``cancelled``
#: exists because an event raised in error has to be retired without deleting the row that proves it
#: was raised.
EVENT_STATUSES = ("open", "acknowledged", "investigating", "closed", "cancelled")
EVENT_SEVERITIES = ("low", "medium", "high", "critical")

EVENT_KINDS = (
    "incident",
    "near_miss",
    "hazard",
    "observation",
    "equipment_failure",
    "tool_failure",
    "kick",
    "influx",
    "lost_circulation",
    "stuck_pipe",
    "twist_off",
    "washout",
    "dysfunction",
    "vibration",
    "hole_instability",
    "tight_hole",
    "poor_hole_cleaning",
    "differential_sticking",
    "wellhead_issue",
    "weather_downtime",
    "third_party_downtime",
    "rig_repair",
    "waiting_on_orders",
    "change",
    "milestone",
    "alert",
    "performance",
    "data_quality",
)

NPT_CATEGORIES = (
    "equipment_failure",
    "tool_failure",
    "wellbore_problem",
    "lost_circulation",
    "stuck_pipe",
    "kick_well_control",
    "hole_problem",
    "rig_equipment",
    "third_party",
    "weather",
    "logistics",
    "waiting",
    "unknown",
    "not_npt",
)


class Operation(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A planned or executed operation on a wellbore section."""

    __tablename__ = "operations"
    id_prefix = "opr"

    well_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    wellbore_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    parent_operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    predecessor_operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_class: Mapped[str] = mapped_column(String(16), default="actual", nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    code: Mapped[str | None] = mapped_column(String(60), index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), default="drilling", nullable=False, index=True)
    phase: Mapped[str] = mapped_column(String(24), default="completed", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="completed", nullable=False)
    planned_start: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    planned_end: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    actual_start: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    actual_end: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    planned_duration_hours: Mapped[float | None] = mapped_column(Float)
    actual_duration_hours: Mapped[float | None] = mapped_column(Float)
    depth_from_md_si: Mapped[float | None] = mapped_column(Float)
    depth_to_md_si: Mapped[float | None] = mapped_column(Float)
    hole_diameter_si: Mapped[float | None] = mapped_column(Float)
    is_productive: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    npt_hours: Mapped[float | None] = mapped_column(Float)
    invisible_lost_time_hours: Mapped[float | None] = mapped_column(Float)
    cost_usd: Mapped[Decimal | None] = mapped_column(Money)
    program_artifact_version_id: Mapped[str | None] = mapped_column(String(64), index=True)
    bha_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    bit_run_id: Mapped[str | None] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    source_document_id: Mapped[str | None] = mapped_column(String(64), index=True)
    data_quality: Mapped[str] = mapped_column(String(24), default="unverified", nullable=False)
    remarks: Mapped[str | None] = mapped_column(TextType)
    #: Which extracted record this row was promoted from, and the fingerprint that identifies the
    #: promotion. Both are columns because re-processing a DDR asks "has this already been promoted?"
    #: once per record, and answering it by loading every operation of the well and scanning JSON is
    #: the difference between an index lookup and a table scan that grows with the well's history.
    source_record_id: Mapped[str | None] = mapped_column(String(64), index=True)
    promotion_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    #: Whether the value came from a document, a person, or a connector — the reader-facing form of
    #: ``source``, kept separate so an import can be told from an entry without parsing a string.
    source_kind: Mapped[str] = mapped_column(
        String(24), default="manual", nullable=False, index=True
    )
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    is_demo_fixture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class NptCode(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """NPT code catalog (IADC/operator style, hierarchical).

    Seeded with a standard catalogue; organizations can extend it, which is why the
    code table is data and not an enum.
    """

    __tablename__ = "npt_codes"
    id_prefix = "npt"
    __table_args__ = (UniqueConstraint("org_id", "code"),)

    code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    category: Mapped[str] = mapped_column(String(60), default="unknown", nullable=False, index=True)
    subcategory: Mapped[str | None] = mapped_column(String(80))
    description: Mapped[str | None] = mapped_column(TextType)
    is_operator_controllable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    parent_code: Mapped[str | None] = mapped_column(String(32), index=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class Event(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """What happened: incidents, downtime, hazards, milestones, performance notes."""

    __tablename__ = "events"
    id_prefix = "evn"

    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="observation", nullable=False, index=True)
    category: Mapped[str | None] = mapped_column(String(60), index=True)
    npt_code: Mapped[str | None] = mapped_column(String(32), index=True)
    npt_category: Mapped[str | None] = mapped_column(String(60), index=True)
    is_npt: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    description: Mapped[str | None] = mapped_column(TextType)
    occurred_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    duration_hours: Mapped[float | None] = mapped_column(Float)
    depth_md_si: Mapped[float | None] = mapped_column(Float)
    depth_tvd_si: Mapped[float | None] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(String(24), default="low", nullable=False, index=True)
    npt_hours: Mapped[float | None] = mapped_column(Float)
    cost_usd: Mapped[Decimal | None] = mapped_column(Money)
    root_cause: Mapped[str | None] = mapped_column(TextType)
    immediate_action: Mapped[str | None] = mapped_column(TextType)
    corrective_action: Mapped[str | None] = mapped_column(TextType)
    status: Mapped[str] = mapped_column(String(24), default="closed", nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    source_document_id: Mapped[str | None] = mapped_column(String(64), index=True)
    evidence_ref: Mapped[str | None] = mapped_column(String(64), index=True)
    #: The record this event was promoted from, and the promotion fingerprint — see ``Operation``.
    source_record_id: Mapped[str | None] = mapped_column(String(64), index=True)
    promotion_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    source_kind: Mapped[str] = mapped_column(
        String(24), default="manual", nullable=False, index=True
    )
    #: Who established the cause, and how sure they were that it is established. An LLM-written root
    #: cause and an operator-confirmed one are different claims; without this they render identically.
    cause_basis: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    #: Reported versus derived classification, so a category the platform inferred is never presented
    #: as one a person recorded.
    classification_source: Mapped[str] = mapped_column(String(24), default="recorded", nullable=False)
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    is_demo_fixture: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class TimeSeries(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A named real-time or historical channel.

    Values are always stored in canonical SI (``unit`` records the canonical unit) with
    the originating unit kept in ``src_unit`` for provenance — the classic WITSML
    mismatch (ft vs m, gpm vs L/min) is resolved once, at ingestion.
    """

    __tablename__ = "time_series"
    id_prefix = "tms"
    __table_args__ = (UniqueConstraint("org_id", "channel_key"),)

    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    asset_ref: Mapped[str | None] = mapped_column(String(120), comment="logical asset/channel owner")
    channel_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    dimension: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    unit: Mapped[str] = mapped_column(String(40), nullable=False, comment="canonical SI unit symbol")
    src_unit: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str | None] = mapped_column(TextType)
    is_realtime: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(40), default="manual", nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(200))
    sampling_hint_seconds: Mapped[float | None] = mapped_column(Float)
    first_ts: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    last_ts: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    point_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class TimeSeriesPoint(Base, IdMixin, CreatedAtMixin):
    """A single measurement. Quality flags follow the WITSML/OSDU convention."""

    __tablename__ = "time_series_points"
    id_prefix = "tsp"

    series_id: Mapped[str] = mapped_column(
        ForeignKey("time_series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ts: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    value: Mapped[float | None] = mapped_column(Float)
    quality: Mapped[str] = mapped_column(String(16), default="good", nullable=False)
    sequence: Mapped[int | None] = mapped_column(IntType, comment="source sequence for gap detection")
    depth_md_si: Mapped[float | None] = mapped_column(Float)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Alert(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Operational alert raised by a rule, engine, workflow or integration."""

    __tablename__ = "alerts"
    id_prefix = "alt"

    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(60), default="engineering", nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(24), default="medium", nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(TextType)
    status: Mapped[str] = mapped_column(String(24), default="open", nullable=False, index=True)
    action_level: Mapped[str] = mapped_column(String(8), default="L3", nullable=False)
    raised_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    raised_by: Mapped[str | None] = mapped_column(String(64))
    rule_ref: Mapped[str | None] = mapped_column(String(120))
    engine_run_id: Mapped[str | None] = mapped_column(String(64))
    workflow_run_id: Mapped[str | None] = mapped_column(String(64))
    subject_kind: Mapped[str | None] = mapped_column(String(40))
    subject_id: Mapped[str | None] = mapped_column(String(64))
    observed_value: Mapped[float | None] = mapped_column(Float)
    threshold_value: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))
    acknowledged_by: Mapped[str | None] = mapped_column(String(64))
    acknowledged_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    cleared_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    evidence_ref: Mapped[str | None] = mapped_column(String(64))
    notified_channels: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
