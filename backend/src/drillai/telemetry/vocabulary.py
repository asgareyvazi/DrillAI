"""One vocabulary per fact, shared by the model, the service, the API and the client.

Every tuple in this module is a *closed* set: a value that is not in it is refused at the boundary
rather than stored and discovered later. The rule this package exists to enforce is that a spelling
decides meaning — ``kick_well_control`` and ``well_control`` are the same cause and must aggregate
into one bucket, ``good`` and ``ok`` are not both quality states, and a channel's unit is either
convertible to its dimension's canonical unit or the ingestion is refused.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- channels

#: The engineering quantity a channel measures. A unit belongs to exactly one dimension, which is what
#: makes "is this conversion legal?" a lookup rather than a judgement call.
CHANNEL_DIMENSIONS: tuple[str, ...] = (
    "force",
    "length",
    "velocity",
    "rotary_speed",
    "flow_rate",
    "pressure",
    "torque",
    "density",
    "mud_weight",
    "angle",
    "temperature",
    "dimensionless",
    "time",
    "ratio",
)

#: The canonical channel keys the platform knows how to place on a screen, in the spelling stored in
#: ``time_series.channel_key``. A key that is not here can still be ingested and queried — a channel is
#: data, not an enum — but it is not offered as a cockpit KPI, because the cockpit's strip is a
#: deliberate list and not "every channel somebody happened to write".
KPI_CHANNEL_KEYS: tuple[str, ...] = (
    "wob",
    "rpm",
    "flow_rate",
    "spp",
    "torque",
    "hookload",
    "rop",
    "ecd",
    "mse",
    "mud_weight",
    "depth_md",
    "block_position",
    "pit_volume",
    "gas_total",
)

#: Human labels for the KPI strip. The label is presentation; the key is the fact.
KPI_CHANNEL_LABELS: dict[str, str] = {
    "wob": "Weight on bit",
    "rpm": "Rotary speed",
    "flow_rate": "Flow rate",
    "spp": "Standpipe pressure",
    "torque": "Surface torque",
    "hookload": "Hookload",
    "rop": "Rate of penetration",
    "ecd": "Equivalent circulating density",
    "mse": "Mechanical specific energy",
    "mud_weight": "Mud weight",
    "depth_md": "Measured depth",
    "block_position": "Block position",
    "pit_volume": "Pit volume",
    "gas_total": "Total gas",
}

#: Where a channel's values come from. ``source`` is recorded on the channel and every point carries
#: the ``source_ref`` it arrived with, so a measurement can be traced to the acquisition system rather
#: than merely to "the platform".
SOURCE_KINDS: tuple[str, ...] = (
    "manual",
    "synthetic",
    "witsml",
    "etp",
    "opcua",
    "modbus",
    "csv_import",
    "ddr_extraction",
    "engine",
    "integration",
    "system",
)

# --------------------------------------------------------------------------- quality

#: Measurement quality, following the WITSML/OSDU convention the model's docstring already claimed but
#: never enforced. The distinction that matters operationally: ``suspect`` and ``bad`` are *recorded*
#: values that should not be trusted, ``missing`` is the absence of a value, and ``estimated`` is one
#: the platform computed rather than received.
QUALITY_STATES: tuple[str, ...] = (
    "good",
    "suspect",
    "bad",
    "missing",
    "estimated",
    "late",
    "duplicate",
    "out_of_order",
)

#: Quality states that are their own statement about *arrival* rather than about the measurement. A
#: late point is not a bad measurement: the value may be perfectly good and the fact worth recording is
#: that it arrived after the window it belongs to.
ARRIVAL_QUALITY_STATES: frozenset[str] = frozenset({"late", "duplicate", "out_of_order"})

#: The quality states a KPI may be rendered from without a warning. Everything else is displayed with
#: its quality, because a number a rig hand should distrust must look like one.
TRUSTWORTHY_QUALITY: frozenset[str] = frozenset({"good", "estimated"})

#: Freshness of a value relative to "now". ``missing`` and ``unknown`` are different claims: the first
#: means no point exists, the second means the platform cannot say how old the value is (no timestamp,
#: or a clock it does not trust).
FRESHNESS_STATES: tuple[str, ...] = ("fresh", "stale", "missing", "unknown")

# --------------------------------------------------------------------------- point identity

#: What makes two recorded points the same measurement.
#:
#: ``source_point_id`` is the acquisition system's own identifier and is authoritative when present.
#: When a source does not provide one — which is the normal case for a hand-entered reading or a CSV
#: without a sequence column — the platform derives a deterministic fingerprint from the fields that
#: identify the observation: the series, the instant, the value and the source reference. Two identical
#: replays of such a point collapse; a re-send with a *different* value at the same instant does not,
#: and is a conflict rather than a silent overwrite (see :mod:`drillai.telemetry.identity`).
POINT_IDENTITY_SOURCES: tuple[str, ...] = ("source_point_id", "fingerprint")

#: How a repeated point is treated when it arrives again with a different value.
REPLAY_POLICIES: tuple[str, ...] = ("reject", "revise")

# --------------------------------------------------------------------------- alerts

ALERT_SEVERITIES: tuple[str, ...] = ("low", "medium", "high", "critical")

#: The alert life cycle. ``cleared`` means the condition stopped being true (the rule's clear
#: condition was met); ``cancelled`` means a person decided the alert was not worth acting on. Keeping
#: them apart is what lets an operator ask "how many alerts actually resolved themselves?".
ALERT_STATUSES: tuple[str, ...] = ("raised", "acknowledged", "cleared", "cancelled")

ALERT_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "raised": frozenset({"acknowledged", "cleared", "cancelled"}),
    "acknowledged": frozenset({"cleared", "cancelled"}),
    "cleared": frozenset(),
    "cancelled": frozenset(),
}

ALERT_TERMINAL_STATUSES: frozenset[str] = frozenset({"cleared", "cancelled"})

#: What a rule watches. A rule is *data* — a comparison, a threshold and a duration — never a piece of
#: Python. There is no expression evaluation anywhere in this package, by design: a rule that arrives
#: from an integration or a workflow definition must not be able to execute code.
RULE_OPERATORS: tuple[str, ...] = ("gt", "gte", "lt", "lte", "eq")

#: Rule scope: whether the condition must hold for a sustained period, and how long an alert stays
#: quiet after it fires. Both are seconds; ``None`` means "not configured".
DEFAULT_SUSTAIN_SECONDS: float = 0.0
DEFAULT_CLEAR_SUSTAIN_SECONDS: float = 0.0
DEFAULT_COOLDOWN_SECONDS: float = 0.0

#: The largest batch a single ingestion call accepts. Beyond this the caller must split its own
#: batches: an unbounded request is an unbounded transaction, and a partial failure inside it would
#: leave the caller with no way to tell which half landed.
MAX_BATCH_POINTS: int = 5_000

#: The largest number of channels one latest-values query will resolve. The query is bounded on
#: purpose: a well with a thousand channels should be paged, not swept.
MAX_LATEST_CHANNELS: int = 200

# --------------------------------------------------------------------------- live feed

#: Domain event types published to the outbox. The names are the contract the WebSocket feed and any
#: future consumer read; a new fact gets a new name rather than overloading an existing one.
DOMAIN_EVENT_TYPES: tuple[str, ...] = (
    "telemetry.channel_created",
    "telemetry.received",
    "telemetry.channel_updated",
    "operation.changed",
    "event.created",
    "alert.raised",
    "alert.acknowledged",
    "alert.cleared",
    "alert.cancelled",
    "well_state.changed",
)

#: Event types a slow client may have coalesced: a stream that is falling behind keeps the *last*
#: telemetry value per channel and reports how many it dropped, because an operator needs the current
#: reading, not every reading. Nothing in this set may be silently dropped — an alert, an operation
#: change or a state change is a fact the client must receive or be told it missed.
COALESCIBLE_EVENT_TYPES: frozenset[str] = frozenset(
    {"telemetry.received", "telemetry.channel_updated"}
)


def is_kpi_channel(key: str) -> bool:
    """Whether a channel key belongs on the cockpit's KPI strip."""

    return key in KPI_CHANNEL_KEYS


def alert_transitions(status: str) -> frozenset[str]:
    return ALERT_STATUS_TRANSITIONS.get(status, frozenset())
