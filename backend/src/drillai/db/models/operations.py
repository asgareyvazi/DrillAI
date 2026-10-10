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

from sqlalchemy import Boolean, Float, ForeignKey, Index, String, UniqueConstraint
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

#: The *one* NPT category vocabulary. It is the list the event service validates against, the list the
#: NPT report groups by and the list the classifier's codes are written in — deliberately the same list,
#: because the platform had three.
#:
#: The three used to disagree: this tuple accepted ``kick_well_control``, ``hole_problem``,
#: ``rig_equipment``, ``tool_failure``, ``unknown`` and ``not_npt``; ``drilling.npt`` presented
#: ``well_control``, ``hole_problems``, ``surface_equipment``, ``downhole_tools`` and ``unclassified``
#: as its chart buckets; and the content classifier emitted codes whose categories were in neither. A
#: kick recorded by the classifier therefore produced a chart bucket nobody could match back to the
#: event, and an event written with the report's spelling was refused by the validator. Exactly one
#: spelling per meaning survives here, and everything else arrives through
#: :data:`NPT_CATEGORY_ALIASES`.
#:
#: ``not_npt`` is part of the vocabulary because an event must be able to say "this was *not* a loss of
#: time" explicitly rather than by omitting a category — a blank category and a denial are different
#: statements, and the NPT reports count only what was classified.
NPT_CATEGORIES = (
    "equipment_failure",
    "surface_equipment",
    "downhole_tools",
    "hole_problems",
    "lost_circulation",
    "stuck_pipe",
    "well_control",
    "third_party",
    "weather",
    "waiting",
    "unclassified",
    "not_npt",
)

#: Raw spellings this platform has written or accepted, mapped to the canonical category above. The
#: mapping exists so that data which already exists — and connectors that speak an older dialect — are
#: *translated*, visibly and once, instead of being stored under a second spelling the reports cannot
#: group. ``kick_well_control → well_control`` is the case that started this: a kick is well control,
#: and two names for it split the very number the report exists to produce.
NPT_CATEGORY_ALIASES = {
    "kick_well_control": "well_control",
    "kick": "well_control",
    "well_control_loss": "well_control",
    "hole_problem": "hole_problems",
    "wellbore_problem": "hole_problems",
    "wellbore_problems": "hole_problems",
    "loss_circulation": "lost_circulation",
    "lost_returns": "lost_circulation",
    "tool_failure": "downhole_tools",
    "downhole_tool_failure": "downhole_tools",
    "rig_equipment": "equipment_failure",
    "equipment_failures": "equipment_failure",
    "surface_equipment_failure": "surface_equipment",
    "logistics": "waiting",
    "waiting_on_weather": "weather",
    "third_party_time": "third_party",
    "unknown": "unclassified",
    "other": "unclassified",
}


def canonical_npt_category(value: str | None) -> str | None:
    """Translate a category spelling to the canonical one.

    Canonical values pass through unchanged; a known alias is translated; anything else is returned
    *unchanged* so the caller can refuse it with the value in the message. Silently folding an unknown
    spelling into "unclassified" would lose the only evidence that a connector is speaking a dialect
    the platform does not know.
    """

    if value is None:
        return None
    text = value.strip().lower()
    if text in NPT_CATEGORIES:
        return text
    return NPT_CATEGORY_ALIASES.get(text, text)


def is_npt_category(value: str | None) -> bool:
    """True when the value is canonical — a translation may be needed first, but nothing is guessed."""

    return value is not None and value in NPT_CATEGORIES


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
    """A named channel on a well: one quantity, one unit, one source.

    Values are always stored in the canonical unit for the channel's dimension (``unit`` records it,
    and ``src_unit`` keeps the unit the source sent, for provenance) — the classic WITSML mismatch
    (ft vs m, gpm vs L/min) is resolved once, here, at ingestion.

    **Identity.** ``scope_token`` is the canonical identity of the *place* the channel measures:
    ``well``, ``well``/``wellbore`` or ``well``/``wellbore``/``operation``, produced by
    :func:`drillai.telemetry.identity.scope_token`. The unique constraint is over
    ``(org_id, scope_token, channel_key, dimension)`` — so two wells may each have their own ``wob``,
    a well-level ``depth_md`` and a wellbore-level ``depth_md`` may coexist, and the same key in two
    dimensions is refused rather than silently reinterpreted. The well/wellbore/operation columns are
    kept alongside it because every query filters on them and reading a scope out of a token in SQL
    would be a string operation the database cannot index.
    """

    __tablename__ = "time_series"
    id_prefix = "tms"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "scope_token", "channel_key", "dimension", name="uq_time_series_identity"
        ),
        Index("ix_time_series_well_channel", "org_id", "well_id", "channel_key"),
        Index("ix_time_series_bore_channel", "org_id", "wellbore_id", "channel_key"),
    )

    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    #: Canonical scope identity — see the class docstring. Populated on every write by the service and
    #: backfilled by the migration; ``NULL`` only for rows written before this revision.
    scope_token: Mapped[str] = mapped_column(String(200), index=True)
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
    """A single measurement.

    Four instants exist in a telemetry system and only two of them are stored here, which is the
    honest pair: ``ts`` is when the measurement was taken (the source time, and the axis of every
    query), and ``received_at`` is when the platform accepted it. The difference between them is what
    makes a *late* point recognisable — a reading from yesterday's connection, handed over this
    morning, is late rather than wrong — and ``is_late``/``is_out_of_order`` record that judgement
    instead of leaving a reader to infer it from a comparison every time.

    **Identity.** Exactly one of ``source_point_id`` (the acquisition system's own identifier, when it
    sends one) or ``fingerprint`` (a documented hash of series/instant/value/source, when it does not)
    is set, and ``dedup_key`` is whichever it is, so the unique constraint
    ``(series_id, dedup_key)`` enforces replay collapse in the database rather than in the service's
    memory. A repeated point with a *different* value does not collapse: it is a distinct row and the
    service reports the conflict (see the ingestion service).

    Values are in the series' canonical unit. ``src_value``/``src_unit`` keep what arrived.
    """

    __tablename__ = "time_series_points"
    id_prefix = "tsp"
    __table_args__ = (
        UniqueConstraint("series_id", "dedup_key", name="uq_time_series_point_identity"),
        Index("ix_time_series_points_series_ts", "series_id", "ts"),
        Index("ix_time_series_points_source_identity", "source_ref", "source_point_id"),
    )

    series_id: Mapped[str] = mapped_column(
        ForeignKey("time_series.id", ondelete="CASCADE"), nullable=False, index=True
    )
    ts: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    received_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    value: Mapped[float | None] = mapped_column(Float)
    quality: Mapped[str] = mapped_column(String(16), default="good", nullable=False, index=True)
    sequence: Mapped[int | None] = mapped_column(IntType, comment="source sequence for gap detection")
    depth_md_si: Mapped[float | None] = mapped_column(Float)
    source_point_id: Mapped[str | None] = mapped_column(String(160))
    source_ref: Mapped[str | None] = mapped_column(String(200))
    fingerprint: Mapped[str | None] = mapped_column(String(64))
    dedup_key: Mapped[str] = mapped_column(String(160))
    is_late: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_out_of_order: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    src_value: Mapped[float | None] = mapped_column(Float)
    src_unit: Mapped[str | None] = mapped_column(String(40))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class AlertRule(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A deterministic condition on one channel.

    This is *data*, never code: an operator (or a workflow) writes a comparison, a threshold and two
    durations, and :mod:`drillai.telemetry.rules` evaluates them. There is no expression string, no
    ``eval`` and no callable anywhere in the path — a rule that could execute code would turn alert
    configuration into remote code execution, and a rule that an LLM writes would be an unfalsifiable
    claim about a measured quantity.
    """

    __tablename__ = "alert_rules"
    id_prefix = "arl"
    __table_args__ = (
        UniqueConstraint("org_id", "rule_key", name="uq_alert_rules_key"),
        Index("ix_alert_rules_channel", "org_id", "channel_key"),
    )

    rule_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(TextType)
    #: What to watch. The rule resolves a channel by key within its scope at evaluation time rather
    #: than pinning a series id, so replacing a sensor does not silently disarm a rule.
    channel_key: Mapped[str] = mapped_column(String(160), nullable=False)
    scope_token: Mapped[str | None] = mapped_column(String(200), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    #: Raise condition. ``unit`` is the unit the thresholds are written in; when it is set and differs
    #: from the channel's canonical unit, the evaluation converts it through the central unit engine
    #: (refusing a unit from another dimension) and the alert records the threshold it actually compared.
    unit: Mapped[str | None] = mapped_column(String(24))
    operator: Mapped[str] = mapped_column(String(8), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    #: Clear condition. Defaults to the raise condition's opposite when not configured; may be set to a
    #: *looser* value than the raise threshold, which is what hysteresis means.
    clear_operator: Mapped[str | None] = mapped_column(String(8))
    clear_threshold: Mapped[float | None] = mapped_column(Float)
    #: The condition must hold continuously for this long before the alert is raised.
    sustain_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    clear_sustain_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    #: After clearing, the rule stays quiet for this long. Prevents raise/clear/raise chatter.
    cooldown_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="medium", nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    message: Mapped[str | None] = mapped_column(TextType)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class OutboxSequence(Base, TimestampMixin, OrgScopedMixin):
    """The per-organization counter behind ``outbox_events.sequence``.

    A stream's sequence has to be gap-free and monotonic or a consumer cannot tell "I missed something"
    from "there was nothing to miss" — and the WebSocket feed's resume-cursor contract is exactly that
    statement. Deriving the next number with ``MAX(sequence) + 1`` inside each writer's transaction looks
    equivalent and is not: two concurrent writers read the same maximum, one loses the unique constraint,
    and the loser's *whole* transaction (the measurement it was storing, the alert it was raising) is the
    thing that fails.

    A single counter row per organization, advanced with ``UPDATE ... RETURNING``, serializes the
    allocation on the row lock instead. Both dialects the platform supports lock the row for the update,
    so the second writer waits rather than losing — and nothing is ever retried by catching an exception.
    """

    __tablename__ = "outbox_sequences"

    org_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_sequence: Mapped[int] = mapped_column(IntType, default=0, nullable=False)


class OutboxWellSequence(Base, TimestampMixin, OrgScopedMixin):
    """The per-well counter behind ``outbox_events.well_sequence``.

    ``OutboxSequence`` gives an organisation one total order across every well it operates. A well's
    live stream (`GET /wells/{well_id}/live/stream`) needs a contiguous sequence of its own: if Well B
    emits 1,500 events between two events on Well A, Well A's own backlog is one event, not 1,500.
    Keeping one counter row per ``(org_id, well_id)`` makes each well stream gap-free on its own axis.
    """

    __tablename__ = "outbox_well_sequences"

    org_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    well_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_sequence: Mapped[int] = mapped_column(IntType, default=0, nullable=False)


class OutboxEvent(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A domain event committed in the same transaction as the mutation that produced it.

    The failure this exists to prevent is the asymmetric one: a row is written and the notification is
    lost, or a notification is emitted and the transaction rolls back. Writing the event beside the
    mutation makes "did this happen?" and "was it announced?" one question the database answers
    atomically; publication then reads this table and moves a cursor over it.

    ``sequence`` is per organization and strictly increasing; ``well_sequence`` is per ``(org_id, well_id)``
    and strictly increasing for well-scoped events, so a well stream consumer can detect real gaps on
    its well without mistaking activity on another well for a dropped range. ``published_at`` records
    when a dispatcher last handled the row — a row with ``published_at IS NULL`` is a fact the platform
    has not announced yet, which is exactly what a reconnect needs to find.
    """

    __tablename__ = "outbox_events"
    id_prefix = "obx"
    __table_args__ = (
        UniqueConstraint("org_id", "sequence", name="uq_outbox_events_sequence"),
        UniqueConstraint("org_id", "well_id", "well_sequence", name="uq_outbox_events_well_sequence"),
        Index("ix_outbox_events_well_sequence", "org_id", "well_id", "sequence"),
        Index("ix_outbox_events_well_stream", "org_id", "well_id", "well_sequence"),
        Index("ix_outbox_events_unpublished", "published_at", "sequence"),
    )

    event_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(IntType, nullable=False)
    well_sequence: Mapped[int | None] = mapped_column(IntType)
    subject_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(64))
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    #: The event's own schema version. A consumer that meets a version it does not know must be able to
    #: say so rather than parse a payload whose meaning changed under it.
    schema_version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    actor: Mapped[str | None] = mapped_column(String(64))


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
    #: The lineage a reader needs to answer "why was this raised?": which series and which point, in
    #: which section, under which rule, with what the condition looked like when it cleared.
    series_id: Mapped[str | None] = mapped_column(String(64), index=True)
    source_point_id: Mapped[str | None] = mapped_column(String(64))
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    rule_id: Mapped[str | None] = mapped_column(String(64), index=True)
    sustained_seconds: Mapped[float | None] = mapped_column(Float)
    clear_observed_value: Mapped[float | None] = mapped_column(Float)
    observed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    cancelled_reason: Mapped[str | None] = mapped_column(TextType)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
