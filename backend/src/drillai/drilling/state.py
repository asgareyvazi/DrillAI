"""Well state: the questions every drilling engineer asks first.

*Where are we? What is the current operation? What comes next? How are we performing?
What is the NPT? What is missing?*

Design rules that this module follows:

1. **Recorded data only.** Every value is read from ``well_sections``, ``operations``,
   ``events``, ``time_series_points``, ``extracted_records`` or the digital twin. Nothing is
   interpolated, averaged or guessed. A missing value is reported as missing.
2. **Traceable.** Each returned value carries ``source`` (which table/field) and, where one
   exists, an ``evidence_ref`` so the UI can answer "where did this number come from?".
3. **Explicit operation classification.** "Next operation" distinguishes *planned* (a planned
   operation row exists), *inferred* (derived from the planned sequence) and *unknown* (no plan
   on file). Inference is never presented as fact.
4. **No engineering arithmetic.** Performance ratios that need engineering models (MSE, ECD,
   hydraulics) come from engines; this module only assembles measured series and recorded values.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.errors import NotFound
from drillai.db.models import (
    Document,
    Event,
    ExtractedRecord,
    Operation,
    Recommendation,
    Risk,
    TwinAspect,
    Well,
    Wellbore,
    WellSection,
)
from drillai.documents.lifecycle import VALIDATED_STATES
from drillai.telemetry.service import TelemetryService

#: How a "next operation" conclusion was reached. Kept as an explicit enum-like tuple so the
#: UI and the advisor can render the difference instead of flattening plan and guess together.
NEXT_OPERATION_BASIS = ("planned", "inferred", "unknown")

#: KPI channels, in the canonical spelling used by ``time_series.channel_key``. The value is a
#: human label only; the platform never translates a channel into a different engineering
#: quantity than the one the channel declares.
KPI_CHANNELS: dict[str, str] = {
    "rop": "Rate of penetration",
    "wob": "Weight on bit",
    "rpm": "Rotary speed",
    "flow_rate": "Flow rate",
    "spp": "Standpipe pressure",
    "torque": "Surface torque",
    "hookload": "Hookload",
    "ecd": "Equivalent circulating density",
    "mse": "Mechanical specific energy",
    "mud_weight": "Mud weight",
    "pore_pressure_gradient": "Pore pressure gradient",
}


@dataclass(frozen=True)
class TelemetrySnapshot:
    """What one bounded telemetry read says about a well: the measured values, and the channels that are
    declared but silent.

    The second list is the reason this type exists. "This channel exists and has never reported" is a
    different fact from "this well has no such channel", and an operator debugging a feed needs to know
    which one they are looking at. Collapsing the two into an empty list is how a broken acquisition path
    looks exactly like a rig that was never instrumented.
    """

    measured: list[Measured]
    silent_channels: list[str]
    as_of: dt.datetime | None


@dataclass(frozen=True)
class Measured:
    """A single measured value with its provenance.

    ``value`` is canonical SI (the platform stores SI); ``unit`` is the canonical unit for the
    dimension. ``source`` names the exact origin so no number is ever anonymous.
    """

    key: str
    label: str
    value: float | None
    unit: str | None
    source: str
    source_id: str | None = None
    observed_at: dt.datetime | None = None
    quality: str = "unverified"
    evidence_ref: str | None = None
    note: str | None = None
    #: Seconds since the value was measured, and the verdict drawn from it. A number on a dashboard
    #: without these is a number a reader has to *assume* is current, which is how an operator ends up
    #: acting on a reading from yesterday. ``freshness`` is one of ``fresh``/``stale``/``missing``/
    #: ``unknown`` and is decided by the telemetry service, from configuration the browser is told as
    #: well — see ``TelemetryService.freshness``.
    age_seconds: float | None = None
    freshness: str | None = None
    received_at: dt.datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "source_id": self.source_id,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "age_seconds": self.age_seconds,
            "freshness": self.freshness,
            "quality": self.quality,
            "evidence_ref": self.evidence_ref,
            "note": self.note,
        }


@dataclass
class MissingData:
    """Something the platform *knows* it does not have. Reported, never filled in silently."""

    key: str
    description: str
    why_it_matters: str
    how_to_supply: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "description": self.description,
            "why_it_matters": self.why_it_matters,
            "how_to_supply": self.how_to_supply,
        }


@dataclass
class OperationsWindow:
    """Current / previous / next operation, with the basis of both claims.

    ``current_basis`` is not decoration. "Current" is resolved from the recorded actual chain, so the two
    honest answers are "recorded" (one event is unambiguously the newest) and "ambiguous" (two or more
    started operations share the newest start timestamp, and *which one is current* is a question the data
    does not answer). Reporting the second as if it were the first is how a monitor ends up confidently
    showing the wrong operation and how a daily report attributes hours to the wrong activity.
    """

    current: Operation | None = None
    previous: Operation | None = None
    next_operation: Operation | None = None
    next_basis: str = "unknown"
    next_note: str | None = None
    recent: list[Operation] = field(default_factory=list)
    #: Why ``current`` is what it is — and the ids that competed for it when the answer is ambiguous.
    current_note: str | None = None
    current_candidates: list[str] = field(default_factory=list)

    @property
    def current_basis(self) -> str:
        if self.current is None:
            return "unknown"
        return "ambiguous" if len(self.current_candidates) > 1 else "recorded"


def measured_md(section: WellSection) -> float | None:
    """The *measured* depth of a section.

    Only recorded measurements count. ``planned_bottom_md_si`` is deliberately **not** a fallback:
    presenting a planned depth as the current depth is exactly the kind of quiet substitution this
    platform exists to prevent.
    """
    for candidate in (section.current_md_si, section.actual_bottom_md_si):
        if candidate is not None:
            return float(candidate)
    return None


def planned_md(section: WellSection) -> float | None:
    return float(section.planned_bottom_md_si) if section.planned_bottom_md_si is not None else None


def _shallowest(sections: list[WellSection]) -> WellSection | None:
    """The section drilling is in: the active one, else the deepest section started."""
    if not sections:
        return None
    for section in sections:
        if section.status == "drilling":
            return section
    started = [s for s in sections if s.actual_top_md_si is not None]
    if started:
        return max(started, key=lambda s: s.actual_top_md_si or 0.0)
    return max(sections, key=lambda s: planned_md(s) or 0.0)


class WellStateService:
    """Assembles the drilling state of one well from recorded data."""

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        self.session = session
        self.org_id = org_id

    # ------------------------------------------------------------------ loaders

    async def _well(self, well_id: str) -> Well:
        row = (
            await self.session.execute(
                select(Well).where(Well.id == well_id, Well.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound("well not found", details={"well_id": well_id})
        return row

    async def _wellbores(self, well_id: str) -> list[Wellbore]:
        return list(
            (
                await self.session.execute(
                    select(Wellbore)
                    .where(Wellbore.well_id == well_id, Wellbore.org_id == self.org_id)
                    .order_by(Wellbore.sequence)
                )
            )
            .scalars()
            .all()
        )

    async def _sections(self, wellbore_ids: list[str]) -> list[WellSection]:
        if not wellbore_ids:
            return []
        return list(
            (
                await self.session.execute(
                    select(WellSection)
                    .where(WellSection.wellbore_id.in_(wellbore_ids), WellSection.org_id == self.org_id)
                    .order_by(WellSection.sequence)
                )
            )
            .scalars()
            .all()
        )

    async def _operations(self, well_id: str) -> list[Operation]:
        return list(
            (
                await self.session.execute(
                    select(Operation)
                    .where(Operation.org_id == self.org_id, Operation.well_id == well_id)
                    .order_by(Operation.sequence, Operation.actual_start, Operation.planned_start)
                )
            )
            .scalars()
            .all()
        )

    async def _events(self, well_id: str) -> list[Event]:
        return list(
            (
                await self.session.execute(
                    select(Event)
                    .where(Event.org_id == self.org_id, Event.well_id == well_id)
                    .order_by(Event.occurred_at)
                )
            )
            .scalars()
            .all()
        )

    async def operation_window_for(self, well_id: str, *, limit: int = 25) -> OperationsWindow:
        """The operation window from a *bounded* read, for callers that are not already holding history.

        The state page loads every operation of the well because it renders the whole timeline. A live
        monitor and a context section do not need that: they need the newest rows around "now", so this
        reads at most ``limit`` actual operations (newest first) plus the planned rows that could name a
        successor, and resolves the window with the same :meth:`operations_window` the state page uses.
        One rule for "what is current", two reads that suit their callers.
        """

        actual = list(
            (
                await self.session.execute(
                    select(Operation)
                    .where(
                        Operation.org_id == self.org_id,
                        Operation.well_id == well_id,
                        Operation.operation_class == "actual",
                    )
                    .order_by(Operation.actual_start.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        planned = list(
            (
                await self.session.execute(
                    select(Operation)
                    .where(
                        Operation.org_id == self.org_id,
                        Operation.well_id == well_id,
                        Operation.operation_class == "planned",
                    )
                    .order_by(Operation.sequence)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return self.operations_window(planned + actual)

    async def _twin_aspects(self, well_id: str) -> list[TwinAspect]:
        return list(
            (
                await self.session.execute(
                    select(TwinAspect)
                    .where(TwinAspect.org_id == self.org_id, TwinAspect.well_id == well_id)
                    .where(TwinAspect.is_current.is_(True))
                )
            )
            .scalars()
            .all()
        )

    # ------------------------------------------------------------------ operations

    def operations_window(self, operations: list[Operation]) -> OperationsWindow:
        """Resolve current / previous / next from the *recorded* operation chain.

        The chain is explicit (``predecessor_operation_id``, ``sequence``), not inferred from
        timestamps, so "what comes next" is a query rather than a guess.
        """
        window = OperationsWindow()
        actual = [op for op in operations if op.operation_class == "actual"]
        planned = [op for op in operations if op.operation_class == "planned"]

        started = [op for op in actual if op.actual_start is not None]
        window.recent = sorted(started, key=lambda op: op.actual_start or dt.datetime.min.replace(tzinfo=dt.UTC))[-12:]
        if started:
            window.current = max(started, key=lambda op: op.actual_start or dt.datetime.min.replace(tzinfo=dt.UTC))
            # A tie for the newest start is not resolved by guessing: the competing ids are carried on the
            # window so a caller can say "one of these two" instead of naming one of them.
            window.current_candidates = [
                op.id for op in started if op.actual_start == window.current.actual_start
            ]
            if len(window.current_candidates) > 1:
                window.current_note = (
                    f"{len(window.current_candidates)} operations share the newest start "
                    f"({window.current.actual_start.isoformat() if window.current.actual_start else 'unknown'}); "
                    "which one is current is not determined by the recorded data"
                )
            elif window.current.predecessor_operation_id:
                window.current_note = "newest started operation, reached from its recorded predecessor"
            else:
                window.current_note = "newest started operation on the well"
            if window.current.predecessor_operation_id:
                window.previous = next(
                    (op for op in operations if op.id == window.current.predecessor_operation_id), None
                )
            if window.previous is None and len(window.recent) > 1:
                window.previous = window.recent[-2]

        # 1. Explicitly planned: successor recorded on the current operation.
        if window.current is not None:
            successor = next(
                (op for op in operations if op.predecessor_operation_id == window.current.id), None
            )
            if successor is not None:
                window.next_operation = successor
                window.next_basis = "planned"
                window.next_note = "successor recorded on the current operation"
                return window
            following = next(
                (op for op in operations if op.sequence > window.current.sequence
                 and op.operation_class == window.current.operation_class),
                None,
            )
            if following is not None:
                window.next_operation = following
                window.next_basis = "planned"
                window.next_note = "next row in the recorded operation sequence"
                return window

        # 2. Inferred: a *planned* row exists for a later sequence.
        if planned:
            candidates = [op for op in planned if op.status in {"planned", "ready"}]
            pool = candidates or planned
            nxt = min(pool, key=lambda op: op.sequence)
            window.next_operation = nxt
            window.next_basis = "inferred"
            window.next_note = (
                "taken from the planned operation rows; no successor was recorded on the current "
                "operation, so this is a plan, not a confirmation"
            )
            return window

        window.next_basis = "unknown"
        window.next_note = "no operation plan is on file for this well"
        return window

    # ------------------------------------------------------------------ progress

    def progress(
        self, well: Well, wellbores: list[Wellbore], sections: list[WellSection]
    ) -> dict[str, Any]:
        """Depth progress, strictly separating measured, planned and reported values."""
        active = next((wb for wb in wellbores if wb.is_active), wellbores[-1] if wellbores else None)
        section = _shallowest(sections) if sections else None
        current_md = measured_md(section) if section else None
        planned_bottom = max((planned_md(s) or 0.0) for s in sections) if sections else None
        planned_td = None
        if active is not None:
            planned_td = active.planned_td_md_si
        if planned_td is None:
            planned_td = well.total_depth_planned_si
        target = current_md if current_md is not None else None
        percent = None
        if target is not None and planned_td:
            percent = round(min(max(target / float(planned_td), 0.0), 1.5) * 100, 2)
        measured_sections = [s for s in sections if measured_md(s) is not None]
        actual_bottom = max((measured_md(s) or 0.0) for s in measured_sections) if measured_sections else None
        depth_variance = None
        if actual_bottom is not None and section is not None and planned_md(section) is not None:
            depth_variance = round(actual_bottom - float(planned_md(section)), 3)
        return {
            "wellbore_id": active.id if active else None,
            "wellbore_name": active.name if active else None,
            "current_md_si": current_md,
            "current_md_source": (
                "well_sections.current_md_si" if section and section.current_md_si is not None
                else ("well_sections.actual_bottom_md_si" if current_md is not None else None)
            ),
            "planned_td_md_si": planned_td,
            "planned_bottom_md_si": planned_bottom,
            "actual_bottom_md_si": actual_bottom,
            "depth_variance_si": depth_variance,
            "percent_planned_depth": percent,
            "percent_basis": (
                "current measured MD / planned TD" if percent is not None else None
            ),
            "current_section": (
                {
                    "id": section.id,
                    "name": section.name,
                    "kind": section.kind,
                    "status": section.status,
                    "hole_diameter_si": section.hole_diameter_si,
                    "hole_diameter_nominal": section.hole_diameter_nominal,
                    "planned_top_md_si": section.planned_top_md_si,
                    "planned_bottom_md_si": section.planned_bottom_md_si,
                    "actual_top_md_si": section.actual_top_md_si,
                    "current_md_si": section.current_md_si,
                    "measured_md_si": measured_md(section),
                }
                if section
                else None
            ),
            "section_count": len(sections),
            "sections_started": len([s for s in sections if s.actual_top_md_si is not None]),
            "source": "well_sections + wellbores (measured fields only)",
            "note": (
                "planned depths are reported separately and are never used as the current depth"
            ),
        }

    # ------------------------------------------------------------------ measured values

    async def measured_values(
        self, well_id: str, *, limit_per_channel: int = 1, now: dt.datetime | None = None
    ) -> list[Measured]:
        """The measured KPI values only — see :meth:`telemetry_snapshot` for the full picture."""

        snapshot = await self.telemetry_snapshot(well_id, now=now)
        return snapshot.measured

    async def telemetry_snapshot(self, well_id: str, *, now: dt.datetime | None = None) -> TelemetrySnapshot:
        """Latest recorded value per KPI channel: one bounded read over telemetry, then documents.

        The telemetry half is delegated to :class:`~drillai.telemetry.service.TelemetryService`, which
        resolves the newest reading per channel with a single window statement over a bounded channel
        set. The previous implementation selected every channel of the well and then ran a query per
        channel with ``ORDER BY ts DESC LIMIT 1``: correct, unbounded, and — for the state page that is
        opened first on every well — the slowest thing the cockpit did.

        A channel with no measurement is simply absent from the telemetry half, and a channel whose
        newest measurement is days old comes back marked ``stale`` with its age. Neither is silently
        turned into a current value.
        """

        service = TelemetryService(self.session, self.org_id)
        readings = await service.latest(well_id=well_id, limit=len(KPI_CHANNELS) or 1, now=now)
        measured: dict[str, Measured] = {}
        silent: list[str] = []
        as_of: dt.datetime | None = None
        for reading in readings:
            if reading.channel_key not in KPI_CHANNELS:
                continue
            if reading.value is None:
                silent.append(reading.channel_key)
                continue
            if reading.observed_at is not None and (as_of is None or reading.observed_at > as_of):
                as_of = reading.observed_at
            measured[reading.channel_key] = Measured(
                key=reading.channel_key,
                label=KPI_CHANNELS[reading.channel_key],
                value=float(reading.value),
                unit=reading.unit,
                source=f"time_series:{reading.channel_key}",
                source_id=reading.channel_id,
                observed_at=reading.observed_at,
                received_at=reading.received_at,
                age_seconds=reading.age_seconds,
                freshness=reading.freshness,
                quality=reading.quality or "unverified",
                note=(
                    "latest recorded point on the channel"
                    if reading.freshness == "fresh"
                    else f"latest recorded point on the channel ({reading.freshness})"
                ),
            )

        # Fall back to structured parameters extracted from the most recent document (usually the
        # latest DDR). Anything found here is *reported as extracted*, with its provenance.
        latest = (
            await self.session.execute(
                select(ExtractedRecord, Document.id)
                .join(Document, Document.id == ExtractedRecord.document_id)
                .where(
                    ExtractedRecord.org_id == self.org_id,
                    ExtractedRecord.well_id == well_id,
                    ExtractedRecord.record_type == "parameter_set",
                )
                .order_by(ExtractedRecord.created_at.desc())
                .limit(1)
            )
        ).first()
        if latest is not None:
            record, document_id = latest
            payload = record.payload or {}
            unit_context = record.unit_context or {}
            for key, label in KPI_CHANNELS.items():
                if key in measured:
                    continue
                candidate = payload.get(key)
                if not isinstance(candidate, (int, float)):
                    continue
                measured[key] = Measured(
                    key=key,
                    label=label,
                    value=float(candidate),
                    unit=unit_context.get(key),
                    source="extracted_record:parameter_set",
                    source_id=record.id,
                    observed_at=record.observed_at or record.created_at,
                    quality="validated" if record.validation_state in VALIDATED_STATES else "extracted",
                    evidence_ref=record.id,
                    # A number read out of a document has an extraction provenance, not a measurement
                    # timeline: it is honest about *when it was read*, not about when it was true. Calling
                    # it fresh would put a value from an unknown shift beside a live sensor reading, so it
                    # is ``unknown`` — a fourth state, distinct from both ``fresh`` and ``stale``.
                    freshness="unknown",
                    note=f"from a document record, so its age is unknown; extracted from document {document_id} "
                    f"(method {record.method}, confidence {record.confidence})",
                )
        return TelemetrySnapshot(
            measured=sorted(measured.values(), key=lambda item: item.key),
            silent_channels=sorted(silent),
            as_of=as_of,
        )

    # ------------------------------------------------------------------ risks / recommendations

    async def open_risks(self, well_id: str, *, limit: int = 20) -> list[Risk]:
        stmt: Select = (
            select(Risk)
            .where(Risk.org_id == self.org_id, Risk.well_id == well_id)
            .where(Risk.status.in_(("open", "mitigating", "monitoring")))
            .order_by(Risk.severity.desc(), Risk.created_at.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def recommendations(self, well_id: str, *, limit: int = 10) -> list[Recommendation]:
        return list(
            (
                await self.session.execute(
                    select(Recommendation)
                    .where(Recommendation.org_id == self.org_id, Recommendation.well_id == well_id)
                    .order_by(Recommendation.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def document_counts(self, well_id: str) -> dict[str, int]:
        total = (
            await self.session.execute(
                select(func.count())
                .select_from(Document)
                .where(Document.org_id == self.org_id, Document.well_id == well_id)
            )
        ).scalar_one()
        ocr = (
            await self.session.execute(
                select(func.count())
                .select_from(Document)
                .where(
                    Document.org_id == self.org_id,
                    Document.well_id == well_id,
                    Document.ocr_required.is_(True),
                )
            )
        ).scalar_one()
        return {"documents": int(total), "awaiting_ocr": int(ocr)}

    # ------------------------------------------------------------------ assembly

    def missing_data(
        self,
        *,
        well: Well,
        wellbores: list[Wellbore],
        sections: list[WellSection],
        window: OperationsWindow,
        measured: list[Measured],
        documents: dict[str, int],
        silent_channels: list[str] | None = None,
    ) -> list[MissingData]:
        """Everything the platform wanted and did not get. Never silently substituted."""
        missing: list[MissingData] = []
        have_channels = {item.key for item in measured}
        if not wellbores:
            missing.append(
                MissingData(
                    key="wellbore",
                    description="No wellbore is defined for this well",
                    why_it_matters="Depth, section and operation context are all wellbore-scoped",
                    how_to_supply="POST /api/v1/wells/{well_id}/wellbores",
                )
            )
        elif not sections:
            missing.append(
                MissingData(
                    key="sections",
                    description="No hole sections are defined for this wellbore",
                    why_it_matters="Hydraulics, casing design and progress all need hole geometry",
                    how_to_supply="POST /api/v1/wellbores/{wellbore_id}/sections",
                )
            )
        if window.current is None:
            missing.append(
                MissingData(
                    key="current_operation",
                    description="No started operation is recorded",
                    why_it_matters="'Where are we?' and the timeline are both built from operations",
                    how_to_supply="Ingest a DDR, or POST operations through the workflow runtime",
                )
            )
        if documents["documents"] == 0:
            missing.append(
                MissingData(
                    key="documents",
                    description="No documents have been ingested for this well",
                    why_it_matters="Every engineering statement must be traceable to evidence",
                    how_to_supply="POST /api/v1/documents (multipart upload)",
                )
            )
        # A channel that is declared but has never reported is a *different* gap from a channel that does
        # not exist, and it is the more urgent one: the acquisition path is broken somewhere between the
        # sensor and the platform.
        for key in silent_channels or []:
            missing.append(
                MissingData(
                    key=f"kpi.{key}.never_reported",
                    description=f"The {KPI_CHANNELS.get(key, key)} channel is registered but has never reported",
                    why_it_matters=(
                        "A registered channel with no measurements means the feed is broken rather than "
                        "absent: the rig is instrumented and the platform is not receiving"
                    ),
                    how_to_supply=(
                        "Check the connector/adapter for this channel, then append the readings with "
                        "POST /api/v1/timeseries/{series_id}/points"
                    ),
                )
            )
        for key in ("rop", "wob", "rpm", "flow_rate"):
            if key not in have_channels and key not in set(silent_channels or []):
                missing.append(
                    MissingData(
                        key=f"kpi.{key}",
                        description=f"No recorded {KPI_CHANNELS[key].lower()} for this well",
                        why_it_matters="Drilling performance and drilling-parameter optimisation need it",
                        how_to_supply=(
                            "Bring the channel in through a connector (WITSML/ETP) or upload a DDR "
                            "whose table contains the parameter"
                        ),
                    )
                )
        if not any(s.pore_pressure_gradient_si for s in sections):
            missing.append(
                MissingData(
                    key="pore_pressure",
                    description="No pore pressure gradient is recorded on any section",
                    why_it_matters="Mud weight windows, kick tolerance and casing design depend on it",
                    how_to_supply="Set it on the section, or ingest the pore pressure study",
                )
            )
        return missing

    async def state(self, well_id: str) -> dict[str, Any]:
        """The complete drilling state of a well, assembled from recorded data."""
        well = await self._well(well_id)
        wellbores = await self._wellbores(well_id)
        sections = await self._sections([wb.id for wb in wellbores])
        operations = await self._operations(well_id)
        events = await self._events(well_id)
        aspects = await self._twin_aspects(well_id)
        snapshot = await self.telemetry_snapshot(well_id)
        measured = snapshot.measured
        window = self.operations_window(operations)
        documents = await self.document_counts(well_id)
        risks = await self.open_risks(well_id)

        # When this answer was assembled, and how old the newest measurement in it is. A state payload
        # without these is a snapshot a client has to *assume* is current; with them, a screen can say
        # "as of 09:14" and the difference between a live well and a well whose feed stopped is visible
        # in the data rather than in the reader's head.
        generated_at = dt.datetime.now(tz=dt.UTC)
        telemetry_as_of = snapshot.as_of
        state_freshness, state_age = TelemetryService(self.session, self.org_id).freshness(
            telemetry_as_of, now=generated_at
        )

        npt_events = [event for event in events if event.is_npt]
        npt_hours = sum(float(event.npt_hours or event.duration_hours or 0.0) for event in npt_events)
        productive_hours = sum(
            float(op.actual_duration_hours or 0.0) for op in operations if op.operation_class == "actual"
        )
        npt_from_operations = sum(float(op.npt_hours or 0.0) for op in operations)
        total_hours = productive_hours + npt_hours
        return {
            "well": {
                "id": well.id,
                "name": well.name,
                "uwi": well.uwi,
                "status": well.status,
                "well_type": well.well_type,
                "operator": well.operator,
                "project_id": well.project_id,
                "rig_id": well.rig_id,
                "spud_date": well.spud_date.isoformat() if well.spud_date else None,
                "twin_state": well.twin_state,
                "target_formations": list(well.target_formations or []),
            },
            "progress": self.progress(well, wellbores, sections),
            "operation": {
                "current": _operation_summary(window.current),
                "previous": _operation_summary(window.previous),
                "next": _operation_summary(window.next_operation),
                "next_basis": window.next_basis,
                "next_note": window.next_note,
                "current_basis": window.current_basis,
                "current_note": window.current_note,
                "current_candidates": list(window.current_candidates),
                "recent": [_operation_summary(op) for op in window.recent],
            },
            "measured": [item.to_dict() for item in measured],
            # The state's own freshness contract. `generated_at` is when this answer was assembled;
            # `as_of` is the newest measurement behind it — the difference between the two is how the
            # platform says "this is what was recorded, and here is how old it is" instead of implying
            # that an answer served now describes the well as it is now. Each value in `measured`
            # carries its own freshness too, so one stale channel cannot hide behind a fresh one.
            "telemetry": {
                "generated_at": generated_at.isoformat(),
                "as_of": telemetry_as_of.isoformat() if telemetry_as_of else None,
                "age_seconds": state_age,
                "freshness": state_freshness,
                "note": (
                    "as_of is the newest measurement behind this answer; a value whose channel has "
                    "stopped reporting appears with freshness=stale and its age rather than as current"
                ),
            },
            "npt": {
                "total_hours": round(npt_hours, 2),
                "hours_from_operations": round(npt_from_operations, 2),
                "event_count": len(npt_events),
                "percent_of_well_time": (
                    round(100.0 * npt_hours / total_hours, 2) if total_hours > 0 else None
                ),
                "measured_hours_total": round(total_hours, 2),
                "note": (
                    "NPT is the sum of recorded NPT events; operations without events contribute "
                    "nothing, so an under-reported well stays visibly under-reported"
                ),
            },
            "twin": {
                "aspects": sorted({aspect.aspect for aspect in aspects}),
                "aspect_count": len(aspects),
            },
            "risks": [
                {
                    "id": risk.id,
                    "title": risk.title,
                    "category": risk.category,
                    "severity": risk.severity,
                    "probability": risk.probability,
                    "impact": risk.impact,
                    "status": risk.status,
                    "mitigation": risk.mitigation,
                    "evidence_ref": risk.evidence_ref,
                }
                for risk in risks
            ],
            "documents": documents,
            "missing": [
                item.to_dict()
                for item in self.missing_data(
                    well=well,
                    wellbores=wellbores,
                    sections=sections,
                    window=window,
                    measured=measured,
                    documents=documents,
                    silent_channels=snapshot.silent_channels,
                )
            ],
            "counts": {
                "wellbores": len(wellbores),
                "sections": len(sections),
                "operations": len(operations),
                "events": len(events),
                "npt_events": len(npt_events),
            },
        }


def _operation_summary(operation: Operation | None) -> dict[str, Any] | None:
    if operation is None:
        return None
    return {
        "id": operation.id,
        "name": operation.name,
        "code": operation.code,
        "kind": operation.kind,
        "phase": operation.phase,
        "status": operation.status,
        "operation_class": operation.operation_class,
        "sequence": operation.sequence,
        "planned_start": operation.planned_start.isoformat() if operation.planned_start else None,
        "planned_end": operation.planned_end.isoformat() if operation.planned_end else None,
        "actual_start": operation.actual_start.isoformat() if operation.actual_start else None,
        "actual_end": operation.actual_end.isoformat() if operation.actual_end else None,
        "planned_duration_hours": operation.planned_duration_hours,
        "actual_duration_hours": operation.actual_duration_hours,
        "duration_variance_hours": (
            round(float(operation.actual_duration_hours) - float(operation.planned_duration_hours), 2)
            if operation.actual_duration_hours is not None and operation.planned_duration_hours is not None
            else None
        ),
        "npt_hours": operation.npt_hours,
        "depth_from_md_si": operation.depth_from_md_si,
        "depth_to_md_si": operation.depth_to_md_si,
        "is_productive": operation.is_productive,
        "data_quality": operation.data_quality,
        "source": operation.source,
        "source_document_id": operation.source_document_id,
        "remarks": operation.remarks,
    }
