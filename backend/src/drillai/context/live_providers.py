"""Context sections for the live well: what is happening now, why we were told, and how old it is.

The existing sections answer "what is on file about this well". These five answer "what is the well doing
right now, what has the platform been told, and can any of it be trusted": ``operational_state``,
``live_telemetry``, ``alerts``, ``recent_events`` and ``freshness``.

Three rules are enforced here rather than trusted to a consumer:

* **every item carries its provenance and its age** — ``source_kind``, ``source_ids``, ``observed_at``,
  ``received_at``, ``data_quality`` and ``freshness``. A language model that reads a section cannot quote
  a number without also having the timestamp that says whether it is still true.
* **every read is tenant-scoped and bounded** — the well is looked up inside the caller's organisation
  (a foreign well id yields an empty section saying so, not another organisation's data), and the
  telemetry read is the same bounded single-statement path the API uses.
* **no section invents anything** — a channel that has never reported is *missing*; a series with no
  trustworthy point is *unknown*; a well with no operations says "no started operation is recorded"
  rather than showing the plan as if it were happening.

Trends are computed from stored measurements only. A trend is a description of the points that already
exist ("rising over the last four readings"), never a prediction, and the method and window that produced
it travel with it.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.context.builder import SectionProvider, SectionResult, _empty, _trim
from drillai.context.model import ContextItem, ContextRequest, ContextSection
from drillai.db.models import Well
from drillai.drilling.state import WellStateService, _operation_summary
from drillai.telemetry.alerts import AlertService
from drillai.telemetry.outbox import events_since
from drillai.telemetry.service import TelemetryService

__all__ = [
    "AlertsProvider",
    "FreshnessProvider",
    "LiveTelemetryProvider",
    "OperationalStateProvider",
    "RecentEventsProvider",
    "install_live_providers",
    "live_providers",
]

#: How many stored points per channel a trend is computed from. Small on purpose: a trend on a live feed
#: is a statement about the last few readings, and a bigger window would make "rising" mean "rose at some
#: point this shift".
TREND_WINDOW_POINTS = 6


async def _owned_well(session: AsyncSession, request: ContextRequest, section: ContextSection) -> Well | None:
    """The well in scope, or ``None`` with the section carrying the reason.

    The lookup is ``(id, org_id)``. A well that belongs to another organisation is reported the same way
    an unknown well is — "not in this organisation" — so the section cannot be used to probe for the
    existence of other tenants' wells.
    """

    well_id = request.scope.well_id
    if not well_id:
        _empty(section, "no well in scope")
        return None
    well = (
        await session.execute(
            select(Well).where(Well.id == well_id, Well.org_id == request.scope.org_id)
        )
    ).scalar_one_or_none()
    if well is None:
        _empty(section, f"well {well_id} is not in this organisation")
        return None
    return well


class OperationalStateProvider(SectionProvider):
    """Where the well is: the current operation, how long it has run, and the basis of that claim."""

    key = "operational_state"
    title = "Operational state"
    required_permission = "well.read"
    priority = 15

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        well = await _owned_well(session, request, section)
        if well is None:
            return section, []
        notes: list[str] = []
        if request.scope.wellbore_id:
            notes.append("wellbore in scope: the operation window is the well's, not the wellbore's")
        window = await WellStateService(session, request.scope.org_id).operation_window_for(well.id)
        current = window.current
        state: dict[str, Any] = {
            "well_id": well.id,
            "well_name": well.name,
            "status": well.status,
            "current_basis": window.current_basis,
            "current_note": window.current_note,
            "current_candidates": list(window.current_candidates),
        }
        if current is None:
            state["reason"] = "no started operation is recorded for this well"
            notes.append(
                "no started operation is recorded; the platform will not present a planned operation as current"
            )
        else:
            started = current.actual_start
            duration = (
                (dt.datetime.now(tz=dt.UTC) - started).total_seconds() if started is not None else None
            )
            state["current_operation"] = {
                **_operation_summary(current),
                "basis": window.current_basis,
                "basis_note": window.current_note,
                "elapsed_seconds": duration,
                "elapsed_source": "actual_start to now" if duration is not None else None,
            }
            if window.current_basis == "ambiguous":
                notes.append(
                    "two or more operations share the newest start; the current operation is reported as "
                    "ambiguous rather than chosen"
                )
        if window.previous is not None:
            state["previous_operation"] = _operation_summary(window.previous)
        if window.next_operation is not None:
            state["next_operation"] = {
                **_operation_summary(window.next_operation),
                "basis": window.next_basis,
                "note": window.next_note,
                "is_planned": window.next_operation.operation_class != "actual",
            }
        section.items.append(
            ContextItem(
                kind="operational_state",
                id=well.id,
                label=well.name,
                data=state,
                source_kind="database",
                source_ids=[well.id] + ([current.id] if current is not None else []),
                data_quality=current.data_quality if current is not None else None,
                observed_at=current.actual_start if current is not None else None,
                notes=notes,
            )
        )
        return section, [well.id]


class LiveTelemetryProvider(SectionProvider):
    """The newest measurement on each channel of the well, with age, quality and freshness."""

    key = "live_telemetry"
    title = "Live telemetry"
    required_permission = "telemetry.read"
    priority = 30

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        well = await _owned_well(session, request, section)
        if well is None:
            return section, []
        service = TelemetryService(session, request.scope.org_id)
        readings = await service.latest(
            well_id=well.id,
            wellbore_id=request.scope.wellbore_id,
            operation_id=request.scope.operation_id,
            limit=max(request.max_items_per_section, 1) * 4,
        )
        if not readings:
            return _empty(section, "no telemetry channels are registered for this well"), [well.id]
        trends = await service.trends(
            well_id=well.id,
            channel_ids=[reading.channel_id for reading in readings],
            samples=TREND_WINDOW_POINTS,
        )
        for reading in readings:
            trend = trends.get(reading.channel_id)
            section.items.append(
                ContextItem(
                    kind="telemetry_reading",
                    id=reading.channel_id,
                    label=reading.label,
                    data={
                        "channel_id": reading.channel_id,
                        "channel_key": reading.channel_key,
                        "dimension": reading.dimension,
                        "value": reading.value,
                        "unit": reading.unit,
                        # The trend is part of the reading, not a separate claim: a trend without the value
                        # it was computed from is a sentence with nothing to check it against.
                        "trend": trend.to_dict() if trend is not None else None,
                    },
                    source_kind="database",
                    source_ids=[reading.channel_id],
                    units={"value": reading.unit},
                    data_quality=reading.quality,
                    observed_at=reading.observed_at,
                    received_at=reading.received_at,
                    freshness=reading.freshness,
                    notes=(
                        [f"measured {reading.age_seconds:.0f}s ago"]
                        if reading.age_seconds is not None
                        else ["the platform cannot compute this reading's age"]
                    ),
                )
            )
            if reading.freshness != "fresh":
                section.notes.append(
                    f"{reading.channel_key} is {reading.freshness}"
                    + (f" ({reading.age_seconds:.0f}s old)" if reading.age_seconds is not None else "")
                )
        section.source_ids = [well.id]
        return _trim(section, request.max_items_per_section), [well.id]


class AlertsProvider(SectionProvider):
    """Everything raised and not yet closed — the reason an operator is being asked to look."""

    key = "alerts"
    title = "Open alerts"
    required_permission = "alert.read"
    priority = 35

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        well = await _owned_well(session, request, section)
        if well is None:
            return section, []
        alerts = await AlertService(session, request.scope.org_id).open_alerts(
            well_id=well.id, limit=max(request.max_items_per_section, 1) * 2
        )
        if not alerts:
            return _empty(section, "no alert is open on this well"), [well.id]
        for alert in alerts:
            section.items.append(
                ContextItem(
                    kind="alert",
                    id=alert.id,
                    label=f"{alert.severity} {alert.status}: {alert.title or alert.rule_key or alert.id}",
                    data={
                        "alert_id": alert.id,
                        "severity": alert.severity,
                        "status": alert.status,
                        "rule_id": alert.rule_id,
                        "rule_key": alert.rule_key,
                        "series_id": alert.series_id,
                        "channel_key": (alert.attributes or {}).get("channel_key"),
                        "observed_value": alert.last_observed_value,
                        "observed_unit": (alert.attributes or {}).get("unit"),
                        "raised_at": alert.raised_at.isoformat() if alert.raised_at else None,
                        "acknowledged_at": alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
                        "sustained_seconds": alert.sustained_seconds,
                        "raise_reason": (alert.attributes or {}).get("raise_reason"),
                        "evidence": f"/api/v1/alerts/{alert.id}/evidence",
                    },
                    source_kind="database",
                    source_ids=[alert.id] + ([alert.rule_id] if alert.rule_id else []),
                    observed_at=alert.observed_at,
                    data_quality="verified" if alert.status != "raised" else "unacknowledged",
                    notes=[
                        "read-only: acknowledging or clearing an alert is a governed action with an actor, "
                        "a reason and an audit entry — a context provider never performs one"
                    ],
                )
            )
        section.source_ids = [well.id]
        return _trim(section, request.max_items_per_section), [well.id]


class RecentEventsProvider(SectionProvider):
    """The platform's own event stream for this well: what changed, in order, with its sequence."""

    key = "recent_events"
    title = "Recent events"
    required_permission = "telemetry.read"
    priority = 40

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        well = await _owned_well(session, request, section)
        if well is None:
            return section, []
        rows = await events_since(
            session,
            request.scope.org_id,
            well_id=well.id,
            limit=min(max(request.max_items_per_section, 1) * 2, 200),
        )
        if not rows:
            return _empty(section, "no event has been published for this well"), [well.id]
        # Newest first: the feed's order is a cursor, but a reader wants the most recent change at the top.
        for row in sorted(rows, key=lambda item: item.sequence, reverse=True):
            section.items.append(
                ContextItem(
                    kind="event",
                    id=row.id,
                    label=row.event_type,
                    data={
                        "event_id": row.id,
                        "type": row.event_type,
                        "sequence": row.sequence,
                        "subject_kind": row.subject_kind,
                        "subject_id": row.subject_id,
                        "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
                        "published_at": row.published_at.isoformat() if row.published_at else None,
                        "schema_version": row.schema_version,
                        "trace_id": row.trace_id,
                        "payload": dict(row.payload or {}),
                    },
                    source_kind="event_stream",
                    source_ids=[row.id] + ([row.well_id] if row.well_id else []),
                    observed_at=row.occurred_at,
                    received_at=row.published_at,
                    notes=[f"stream sequence {row.sequence}"],
                )
            )
        section.source_ids = [well.id]
        return _trim(section, request.max_items_per_section), [well.id]


class FreshnessProvider(SectionProvider):
    """How much of the well is actually reporting, counted rather than described."""

    key = "freshness"
    title = "Data freshness"
    required_permission = "telemetry.read"
    priority = 45

    async def build(self, session: AsyncSession, request: ContextRequest) -> SectionResult:
        section = ContextSection(key=self.key, title=self.title)
        well = await _owned_well(session, request, section)
        if well is None:
            return section, []
        service = TelemetryService(session, request.scope.org_id)
        readings = await service.latest(well_id=well.id, limit=None)
        counts = {"fresh": 0, "stale": 0, "missing": 0, "unknown": 0}
        silent: list[str] = []
        newest: dt.datetime | None = None
        for reading in readings:
            counts[reading.freshness] = counts.get(reading.freshness, 0) + 1
            if reading.value is None:
                silent.append(reading.channel_key)
            if reading.observed_at is not None and (newest is None or reading.observed_at > newest):
                newest = reading.observed_at
        generated_at = dt.datetime.now(tz=dt.UTC)
        state, as_of_age = service.freshness(newest, now=generated_at)
        section.items.append(
            ContextItem(
                kind="freshness_summary",
                id=well.id,
                label=f"Telemetry freshness: {state}",
                data={
                    "well_id": well.id,
                    "generated_at": generated_at.isoformat(),
                    "telemetry_as_of": newest.isoformat() if newest else None,
                    "age_seconds": as_of_age,
                    "state": state,
                    "channels": counts,
                    "channels_total": len(readings),
                    "channels_never_reported": sorted(silent),
                    "thresholds": {
                        "fresh_seconds": service.fresh_seconds,
                        "stale_seconds": service.stale_seconds,
                        "late_seconds": service.late_seconds,
                    },
                },
                source_kind="database",
                source_ids=[well.id],
                observed_at=newest,
                freshness=state,
                notes=[
                    "the same thresholds the API and the browser use; a client never re-derives freshness",
                    "channels_never_reported are registered channels with no measurement at all — a broken "
                    "acquisition path, not an absent one",
                ],
            )
        )
        return section, [well.id]


def live_providers() -> tuple[SectionProvider, ...]:
    return (
        OperationalStateProvider(),
        LiveTelemetryProvider(),
        AlertsProvider(),
        RecentEventsProvider(),
        FreshnessProvider(),
    )


def install_live_providers() -> None:
    """Register the live sections. Called once at import of :mod:`drillai.context`."""

    from drillai.context.builder import register_section_provider

    for provider in live_providers():
        register_section_provider(provider, replace=True)
