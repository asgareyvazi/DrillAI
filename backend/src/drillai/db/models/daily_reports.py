"""Governed daily drilling report identity, immutable revision history and typed observations.

A report revision is a domain record, not an untyped JSON export. Engineering measurements and
activity spans are separate rows with canonical values, source units, temporal/depth basis and source
references. ``JsonType`` is intentionally absent from this module.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    CheckConstraint,
    Date,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from drillai.db.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    IntType,
    OrgScopedMixin,
    TimestampMixin,
    UtcDateTime,
)
from drillai.db.models.operations import CAUSE_BASES, NPT_CATEGORIES, OPERATION_KINDS

REPORT_SHIFT_CODES = ("day", "night", "24h", "custom")
REPORT_SOURCE_KINDS = ("manual", "import", "connector")
REPORT_REVISION_STATUSES = ("draft", "submitted", "approved", "rejected", "withdrawn")
REPORT_ACTIVITY_KINDS = ("operation", "npt", "event", "safety", "milestone", "other")
REPORT_TIME_BASES = ("observed", "reported", "calculated", "planned", "inferred", "unknown")
REPORT_DEPTH_REFERENCES = ("md", "tvd", "tvdss", "unknown")
REPORT_RECONCILIATION_STATES = ("manual", "proposed", "accepted", "corrected", "rejected", "unresolved")

# Each entry is (dimension, canonical unit, required depth reference). Values are deliberately limited
# to quantities supported by the telemetry unit converter and named in the DDR governance decision.
# An unsupported or ambiguously labelled source value remains source evidence, not a measurement row.
DAILY_REPORT_PARAMETER_SPECS: dict[str, tuple[str, str, str | None]] = {
    "depth_md": ("length", "m", "md"),
    "depth_tvd": ("length", "m", "tvd"),
    "depth_tvdss": ("length", "m", "tvdss"),
    "bit_depth_md": ("length", "m", "md"),
    "hole_depth_md": ("length", "m", "md"),
    "daily_progress_md": ("length", "m", "md"),
    "rop": ("velocity", "m/s", None),
    "wob": ("force", "N", None),
    "hookload": ("force", "N", None),
    "rotary_speed": ("rotary_speed", "rpm", None),
    "flow_rate": ("flow_rate", "m3/s", None),
    "standpipe_pressure": ("pressure", "Pa", None),
    "torque": ("torque", "N.m", None),
    "mud_weight": ("mud_weight", "kg/m3", None),
    "mud_weight_in": ("mud_weight", "kg/m3", None),
    "mud_weight_out": ("mud_weight", "kg/m3", None),
    "mud_temperature": ("temperature", "degC", None),
    "funnel_viscosity": ("time", "s", None),
}


class DailyDrillingReport(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Stable identity for one report date/shift or operator report number on a wellbore."""

    __tablename__ = "daily_drilling_reports"
    id_prefix = "ddr"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "wellbore_id", "report_date", "identity_key", name="uq_daily_drilling_report_identity"
        ),
        UniqueConstraint("id", "org_id", name="uq_daily_drilling_report_id_org"),
        CheckConstraint("length(trim(identity_key)) > 0", name="daily_report_identity_key_nonempty"),
        CheckConstraint("length(trim(report_timezone)) > 0", name="daily_report_timezone_nonempty"),
        CheckConstraint("shift_code IN ('day', 'night', '24h', 'custom')", name="daily_report_shift_valid"),
        CheckConstraint(
            "(shift_code = 'custom' AND shift_label IS NOT NULL AND length(trim(shift_label)) > 0) OR "
            "(shift_code <> 'custom' AND shift_label IS NULL)",
            name="daily_report_shift_label_consistent",
        ),
        CheckConstraint(
            "report_number IS NULL OR length(trim(report_number)) > 0",
            name="daily_report_number_nonempty",
        ),
        CheckConstraint("latest_revision_number >= 1", name="daily_report_latest_revision_positive"),
        CheckConstraint(
            "latest_approved_revision_number IS NULL OR latest_approved_revision_number >= 1",
            name="daily_report_approved_revision_positive",
        ),
        Index("ix_daily_drilling_reports_well_date", "org_id", "well_id", "report_date"),
        Index("ix_daily_drilling_reports_wellbore_date", "org_id", "wellbore_id", "report_date"),
    )

    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False, index=True)
    well_id: Mapped[str] = mapped_column(ForeignKey("wells.id", ondelete="RESTRICT"), nullable=False, index=True)
    wellbore_id: Mapped[str] = mapped_column(ForeignKey("wellbores.id", ondelete="RESTRICT"), nullable=False, index=True)
    report_date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    report_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    shift_code: Mapped[str] = mapped_column(String(24), nullable=False)
    shift_label: Mapped[str | None] = mapped_column(String(80))
    report_number: Mapped[str | None] = mapped_column(String(120), index=True)
    #: Normalized report number, or ``shift:<code>`` when no report number was supplied.
    identity_key: Mapped[str] = mapped_column(String(180), nullable=False)
    latest_revision_number: Mapped[int] = mapped_column(IntType, default=1, server_default="1", nullable=False)
    latest_approved_revision_number: Mapped[int | None] = mapped_column(IntType)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)


class DailyDrillingReportRevision(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A numbered, reviewable snapshot of the typed report content."""

    __tablename__ = "daily_drilling_report_revisions"
    id_prefix = "drr"
    __table_args__ = (
        ForeignKeyConstraint(
            ["report_id", "org_id"],
            ["daily_drilling_reports.id", "daily_drilling_reports.org_id"],
            ondelete="RESTRICT",
            name="fk_daily_report_revision_report_tenant",
        ),
        UniqueConstraint("report_id", "revision_number", name="uq_daily_report_revision_number"),
        UniqueConstraint("id", "org_id", name="uq_daily_report_revision_id_org"),
        CheckConstraint("revision_number >= 1", name="daily_report_revision_number_positive"),
        CheckConstraint("edit_version >= 1", name="daily_report_edit_version_positive"),
        CheckConstraint(
            "period_start_at IS NULL OR period_end_at IS NULL OR period_end_at >= period_start_at",
            name="daily_report_period_ordered",
        ),
        CheckConstraint(
            "status IN ('draft', 'submitted', 'approved', 'rejected', 'withdrawn')",
            name="daily_report_revision_status_valid",
        ),
        CheckConstraint(
            "source_kind IN ('manual', 'import', 'connector')",
            name="daily_report_revision_source_valid",
        ),
        CheckConstraint(
            "source_page_number IS NULL OR source_page_number >= 1",
            name="daily_report_source_page_positive",
        ),
        CheckConstraint(
            "supersedes_revision_id IS NULL OR supersedes_revision_id <> id",
            name="daily_report_revision_not_self_superseding",
        ),
        Index("ix_daily_report_revisions_report_status", "org_id", "report_id", "status"),
    )

    report_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    revision_number: Mapped[int] = mapped_column(IntType, nullable=False)
    edit_version: Mapped[int] = mapped_column(IntType, default=1, server_default="1", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="draft", server_default="draft", nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), index=True
    )
    source_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("extracted_records.id", ondelete="RESTRICT"), index=True
    )
    source_region_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_regions.id", ondelete="RESTRICT"), index=True
    )
    source_page_number: Mapped[int | None] = mapped_column(IntType)
    period_start_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    period_end_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    summary: Mapped[str | None] = mapped_column(Text)
    operations_summary: Mapped[str | None] = mapped_column(Text)
    safety_notes: Mapped[str | None] = mapped_column(Text)
    next_period_plan: Mapped[str | None] = mapped_column(Text)
    environmental_notes: Mapped[str | None] = mapped_column(Text)
    supersedes_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("daily_drilling_report_revisions.id", ondelete="RESTRICT"), index=True
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_by: Mapped[str | None] = mapped_column(String(64))
    submitted_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)


class DailyReportActivity(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """A typed activity/event span and its source, time, depth and reconciliation basis."""

    __tablename__ = "daily_report_activities"
    id_prefix = "dra"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "org_id"],
            ["daily_drilling_report_revisions.id", "daily_drilling_report_revisions.org_id"],
            ondelete="CASCADE",
            name="fk_daily_report_activity_revision_tenant",
        ),
        UniqueConstraint("revision_id", "sequence", name="uq_daily_report_activity_sequence"),
        CheckConstraint("sequence >= 0", name="daily_report_activity_sequence_nonnegative"),
        CheckConstraint(
            "activity_kind IN ('operation', 'npt', 'event', 'safety', 'milestone', 'other')",
            name="daily_report_activity_kind_valid",
        ),
        CheckConstraint(
            "(start_at IS NULL AND start_time_basis = 'unknown') OR "
            "(start_at IS NOT NULL AND start_time_basis <> 'unknown')",
            name="daily_report_activity_start_basis_consistent",
        ),
        CheckConstraint(
            "(end_at IS NULL AND end_time_basis = 'unknown') OR "
            "(end_at IS NOT NULL AND end_time_basis <> 'unknown')",
            name="daily_report_activity_end_basis_consistent",
        ),
        CheckConstraint(
            "start_at IS NULL OR end_at IS NULL OR end_at >= start_at",
            name="daily_report_activity_time_ordered",
        ),
        CheckConstraint(
            "operation_kind IS NULL OR operation_kind IN ("
            + ", ".join(f"'{value}'" for value in OPERATION_KINDS)
            + ")",
            name="daily_report_activity_operation_kind_valid",
        ),
        CheckConstraint(
            "npt_category IS NULL OR npt_category IN ("
            + ", ".join(f"'{value}'" for value in NPT_CATEGORIES)
            + ")",
            name="daily_report_activity_npt_category_valid",
        ),
        CheckConstraint(
            "cause_basis IN (" + ", ".join(f"'{value}'" for value in CAUSE_BASES) + ")",
            name="daily_report_activity_cause_basis_valid",
        ),
        CheckConstraint(
            "(duration_seconds IS NULL AND duration_basis = 'unknown' AND "
            "duration_source_value IS NULL AND duration_source_unit IS NULL) OR "
            "(duration_seconds IS NOT NULL AND duration_seconds >= 0 AND duration_basis <> 'unknown' "
            "AND duration_source_value IS NOT NULL AND duration_source_unit IS NOT NULL "
            "AND length(trim(duration_source_unit)) > 0)",
            name="daily_report_activity_duration_consistent",
        ),
        CheckConstraint(
            "(depth_from_si IS NULL AND depth_from_source_value IS NULL AND depth_from_source_unit IS NULL) OR "
            "(depth_from_si IS NOT NULL AND depth_from_si >= 0 AND depth_from_source_value IS NOT NULL "
            "AND depth_from_source_unit IS NOT NULL AND length(trim(depth_from_source_unit)) > 0)",
            name="daily_report_activity_depth_from_source_consistent",
        ),
        CheckConstraint(
            "(depth_to_si IS NULL AND depth_to_source_value IS NULL AND depth_to_source_unit IS NULL) OR "
            "(depth_to_si IS NOT NULL AND depth_to_si >= 0 AND depth_to_source_value IS NOT NULL "
            "AND depth_to_source_unit IS NOT NULL AND length(trim(depth_to_source_unit)) > 0)",
            name="daily_report_activity_depth_to_source_consistent",
        ),
        CheckConstraint(
            "depth_from_si IS NULL OR depth_to_si IS NULL OR depth_to_si >= depth_from_si",
            name="daily_report_activity_depth_ordered",
        ),
        CheckConstraint(
            "((depth_from_si IS NULL AND depth_to_si IS NULL) AND depth_basis = 'unknown') OR "
            "((depth_from_si IS NOT NULL OR depth_to_si IS NOT NULL) AND depth_basis <> 'unknown')",
            name="daily_report_activity_depth_basis_consistent",
        ),
        CheckConstraint(
            "depth_reference IN ('md', 'tvd', 'tvdss', 'unknown')",
            name="daily_report_activity_depth_reference_valid",
        ),
        CheckConstraint(
            "depth_basis IN ('observed', 'reported', 'calculated', 'planned', 'inferred', 'unknown')",
            name="daily_report_activity_depth_basis_valid",
        ),
        CheckConstraint(
            "duration_basis IN ('observed', 'reported', 'calculated', 'planned', 'inferred', 'unknown')",
            name="daily_report_activity_duration_basis_valid",
        ),
        CheckConstraint(
            "resolution_state IN ('manual', 'proposed', 'accepted', 'corrected', 'rejected', 'unresolved')",
            name="daily_report_activity_resolution_valid",
        ),
        CheckConstraint(
            "source_page_number IS NULL OR source_page_number >= 1",
            name="daily_report_activity_source_page_positive",
        ),
        Index("ix_daily_report_activities_revision_kind", "org_id", "revision_id", "activity_kind"),
    )

    revision_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(IntType, nullable=False)
    activity_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    operation_kind: Mapped[str | None] = mapped_column(String(40))
    npt_category: Mapped[str | None] = mapped_column(String(60), index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    start_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    start_time_basis: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    end_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    end_time_basis: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    duration_source_value: Mapped[float | None] = mapped_column(Float)
    duration_source_unit: Mapped[str | None] = mapped_column(String(24))
    duration_basis: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    depth_from_si: Mapped[float | None] = mapped_column(Float)
    depth_from_source_value: Mapped[float | None] = mapped_column(Float)
    depth_from_source_unit: Mapped[str | None] = mapped_column(String(24))
    depth_to_si: Mapped[float | None] = mapped_column(Float)
    depth_to_source_value: Mapped[float | None] = mapped_column(Float)
    depth_to_source_unit: Mapped[str | None] = mapped_column(String(24))
    depth_reference: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    depth_basis: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    cause_basis: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    resolution_state: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), index=True
    )
    source_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("extracted_records.id", ondelete="RESTRICT"), index=True
    )
    source_region_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_regions.id", ondelete="RESTRICT"), index=True
    )
    source_page_number: Mapped[int | None] = mapped_column(IntType)
    operation_id: Mapped[str | None] = mapped_column(
        ForeignKey("operations.id", ondelete="RESTRICT"), index=True
    )
    event_id: Mapped[str | None] = mapped_column(ForeignKey("events.id", ondelete="RESTRICT"), index=True)


class DailyReportMeasurement(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """One controlled numeric DDR measurement with exact input and canonical SI provenance."""

    __tablename__ = "daily_report_measurements"
    id_prefix = "drm"
    __table_args__ = (
        ForeignKeyConstraint(
            ["revision_id", "org_id"],
            ["daily_drilling_report_revisions.id", "daily_drilling_report_revisions.org_id"],
            ondelete="CASCADE",
            name="fk_daily_report_measurement_revision_tenant",
        ),
        UniqueConstraint("revision_id", "parameter_key", "ordinal", name="uq_daily_report_measurement_identity"),
        CheckConstraint("ordinal >= 0", name="daily_report_measurement_ordinal_nonnegative"),
        CheckConstraint(
            "parameter_key IN (" + ", ".join(f"'{key}'" for key in DAILY_REPORT_PARAMETER_SPECS) + ")",
            name="daily_report_measurement_parameter_valid",
        ),
        CheckConstraint(
            " OR ".join(
                f"(parameter_key = '{key}' AND dimension = '{dimension}' AND canonical_unit = '{unit}')"
                for key, (dimension, unit, _reference) in DAILY_REPORT_PARAMETER_SPECS.items()
            ),
            name="daily_report_measurement_parameter_contract",
        ),
        CheckConstraint(
            "length(trim(source_unit)) > 0 AND length(trim(accepted_input_unit)) > 0",
            name="daily_report_measurement_units_nonempty",
        ),
        CheckConstraint(
            "value_basis IN ('observed', 'reported', 'calculated', 'planned', 'inferred', 'unknown')",
            name="daily_report_measurement_value_basis_valid",
        ),
        CheckConstraint(
            "time_basis IN ('observed', 'reported', 'calculated', 'planned', 'inferred', 'unknown')",
            name="daily_report_measurement_time_basis_valid",
        ),
        CheckConstraint(
            "(observed_at IS NULL AND time_basis = 'unknown') OR "
            "(observed_at IS NOT NULL AND time_basis <> 'unknown')",
            name="daily_report_measurement_time_basis_consistent",
        ),
        CheckConstraint(
            "depth_reference IN ('md', 'tvd', 'tvdss', 'unknown')",
            name="daily_report_measurement_depth_reference_valid",
        ),
        CheckConstraint(
            "depth_basis IN ('observed', 'reported', 'calculated', 'planned', 'inferred', 'unknown')",
            name="daily_report_measurement_depth_basis_valid",
        ),
        CheckConstraint(
            "(depth_si IS NULL AND depth_basis = 'unknown' AND depth_source_value IS NULL AND depth_source_unit IS NULL) OR "
            "(depth_si IS NOT NULL AND depth_si >= 0 AND depth_basis <> 'unknown' "
            "AND depth_source_value IS NOT NULL AND depth_source_unit IS NOT NULL "
            "AND length(trim(depth_source_unit)) > 0)",
            name="daily_report_measurement_depth_basis_consistent",
        ),
        CheckConstraint(
            "resolution_state IN ('manual', 'proposed', 'accepted', 'corrected', 'rejected', 'unresolved')",
            name="daily_report_measurement_resolution_valid",
        ),
        CheckConstraint(
            "source_page_number IS NULL OR source_page_number >= 1",
            name="daily_report_measurement_source_page_positive",
        ),
        Index("ix_daily_report_measurements_revision_key", "org_id", "revision_id", "parameter_key"),
    )

    revision_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    parameter_key: Mapped[str] = mapped_column(String(48), nullable=False)
    ordinal: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_unit: Mapped[str] = mapped_column(String(24), nullable=False)
    value_si: Mapped[float] = mapped_column(Float, nullable=False)
    source_value: Mapped[float] = mapped_column(Float, nullable=False)
    source_unit: Mapped[str] = mapped_column(String(24), nullable=False)
    accepted_input_value: Mapped[float] = mapped_column(Float, nullable=False)
    accepted_input_unit: Mapped[str] = mapped_column(String(24), nullable=False)
    value_basis: Mapped[str] = mapped_column(String(24), default="reported", nullable=False)
    observed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    time_basis: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    depth_si: Mapped[float | None] = mapped_column(Float)
    depth_source_value: Mapped[float | None] = mapped_column(Float)
    depth_source_unit: Mapped[str | None] = mapped_column(String(24))
    depth_reference: Mapped[str] = mapped_column(String(16), default="unknown", nullable=False)
    depth_basis: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    resolution_state: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    source_document_id: Mapped[str | None] = mapped_column(
        ForeignKey("documents.id", ondelete="RESTRICT"), index=True
    )
    source_record_id: Mapped[str | None] = mapped_column(
        ForeignKey("extracted_records.id", ondelete="RESTRICT"), index=True
    )
    source_region_id: Mapped[str | None] = mapped_column(
        ForeignKey("document_regions.id", ondelete="RESTRICT"), index=True
    )
    source_page_number: Mapped[int | None] = mapped_column(IntType)


__all__ = [
    "DAILY_REPORT_PARAMETER_SPECS",
    "REPORT_ACTIVITY_KINDS",
    "REPORT_DEPTH_REFERENCES",
    "REPORT_RECONCILIATION_STATES",
    "REPORT_REVISION_STATUSES",
    "REPORT_SHIFT_CODES",
    "REPORT_SOURCE_KINDS",
    "REPORT_TIME_BASES",
    "DailyDrillingReport",
    "DailyDrillingReportRevision",
    "DailyReportActivity",
    "DailyReportMeasurement",
]
