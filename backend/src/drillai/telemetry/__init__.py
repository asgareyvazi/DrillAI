"""Real-time telemetry: channels, measurements, quality, freshness, rules and alerts.

The package is deliberately split by *question*, not by layer:

:mod:`drillai.telemetry.vocabulary`
    one closed vocabulary per fact — channels, dimensions, quality, freshness, alert states, domain
    event types — shared by the model, the service, the API and the client.
:mod:`drillai.telemetry.units`
    the single conversion engine. Ingestion converts once; nothing else does arithmetic on a unit.
:mod:`drillai.telemetry.identity`
    what makes a channel a channel and a point the same point it was before, as functions the database
    constraint and the service both call.
:mod:`drillai.telemetry.service`
    channels, batch ingestion, windows and latest values — bounded in SQL, honest about what it
    accepted and what it refused.
:mod:`drillai.telemetry.rules`
    the deterministic rule evaluator: comparisons and durations only, no expression evaluation.
:mod:`drillai.telemetry.alerts`
    the alert life cycle with the evidence a reader needs to answer "why was this raised?".
:mod:`drillai.telemetry.outbox`
    the domain-event log written in the same transaction as the mutation that produced it.
:mod:`drillai.telemetry.adapters`
    the boundary an external acquisition system plugs into — generic, WITSML-shaped, ETP-shaped.
"""

from __future__ import annotations

from drillai.telemetry.identity import channel_identity, point_identity, scope_token
from drillai.telemetry.units import CANONICAL_UNIT, convert
from drillai.telemetry.vocabulary import (
    ALERT_SEVERITIES,
    ALERT_STATUSES,
    KPI_CHANNEL_KEYS,
    KPI_CHANNEL_LABELS,
    QUALITY_STATES,
)

__all__ = [
    "ALERT_SEVERITIES",
    "ALERT_STATUSES",
    "CANONICAL_UNIT",
    "KPI_CHANNEL_KEYS",
    "KPI_CHANNEL_LABELS",
    "QUALITY_STATES",
    "channel_identity",
    "convert",
    "point_identity",
    "scope_token",
]
