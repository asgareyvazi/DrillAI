"""NPT intelligence.

Non-productive time is **derived from recorded events**, never estimated. The rule this module
enforces is that an hour of NPT exists because somebody recorded an event with a duration (or an
operation with ``npt_hours``), and every aggregated number keeps a path back to those rows.

Two independent sources exist in the model and they are reported separately rather than blended:

``events``      — what happened, with ``is_npt``, ``npt_code``/``npt_category`` and a duration
``operations``  — the operation row's own ``npt_hours`` (the DDR roll-up)

Blending them would double count. So :func:`summarise` publishes both and makes the *choice* of
the reporting total explicit (``basis``), because an engineering review must be able to reconcile
the platform's number with the rig's number.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.errors import ValidationFailed
from drillai.db.models import Event, NptCode, OffsetCandidate, Operation, Well

#: Which recorded source a reported total is built from.
NPT_BASES = ("events", "operations")

#: Categories the platform will present even when nothing was recorded, so that "no NPT in this
#: category" is visible instead of the category simply disappearing from the chart.
STANDARD_CATEGORIES = (
    "equipment_failure",
    "stuck_pipe",
    "lost_circulation",
    "well_control",
    "hole_problems",
    "waiting",
    "weather",
    "downhole_tools",
    "surface_equipment",
    "third_party",
    "unclassified",
)


@dataclass
class NptEvent:
    """One recorded NPT occurrence."""

    id: str
    source: str  # event | operation
    title: str
    category: str
    subcategory: str | None
    code: str | None
    cause: str | None
    hours: float
    started_at: dt.datetime | None
    ended_at: dt.datetime | None
    depth_md_si: float | None
    section_id: str | None
    operation_id: str | None
    operator_controllable: bool | None
    confidence: float | None
    classification_source: str
    evidence_ref: str | None
    document_id: str | None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "title": self.title,
            "category": self.category,
            "subcategory": self.subcategory,
            "code": self.code,
            "cause": self.cause,
            "hours": round(self.hours, 3),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "depth_md_si": self.depth_md_si,
            "section_id": self.section_id,
            "operation_id": self.operation_id,
            "operator_controllable": self.operator_controllable,
            "confidence": self.confidence,
            "classification_source": self.classification_source,
            "evidence_ref": self.evidence_ref,
            "document_id": self.document_id,
            "note": self.note,
        }


@dataclass
class ParetoRow:
    key: str
    label: str
    hours: float
    occurrences: int
    percent_of_total: float
    cumulative_percent: float
    operator_controllable: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "hours": round(self.hours, 3),
            "occurrences": self.occurrences,
            "percent_of_total": round(self.percent_of_total, 2),
            "cumulative_percent": round(self.cumulative_percent, 2),
            "operator_controllable": self.operator_controllable,
        }


@dataclass
class NptSummary:
    total_hours: float
    basis: str
    hours_events: float
    hours_operations: float
    event_count: int
    measured_hours_total: float
    percent_of_well_time: float | None
    by_hours: float
    cases: list[NptEvent] = field(default_factory=list)
    by_category: list[ParetoRow] = field(default_factory=list)
    by_code: list[ParetoRow] = field(default_factory=list)
    by_section: list[ParetoRow] = field(default_factory=list)
    by_operation: list[ParetoRow] = field(default_factory=list)
    by_month: list[dict[str, Any]] = field(default_factory=list)
    controllable_hours: float = 0.0
    uncontrollable_hours: float = 0.0
    unknown_controllability_hours: float = 0.0
    offset_comparison: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_hours": round(self.total_hours, 3),
            "basis": self.basis,
            "hours_from_events": round(self.hours_events, 3),
            "hours_from_operations": round(self.hours_operations, 3),
            "event_count": self.event_count,
            "measured_hours_total": round(self.measured_hours_total, 3),
            "percent_of_well_time": (
                round(self.percent_of_well_time, 2) if self.percent_of_well_time is not None else None
            ),
            "by_hours": round(self.by_hours, 3),
            "controllable_hours": round(self.controllable_hours, 3),
            "uncontrollable_hours": round(self.uncontrollable_hours, 3),
            # Not the same as uncontrollable: a case whose controllability was never established is
            # reported as unknown, and the three buckets always add up to the total.
            "unknown_controllability_hours": round(self.unknown_controllability_hours, 3),
            "cases": [case.to_dict() for case in self.cases],
            "by_category": [row.to_dict() for row in self.by_category],
            "by_code": [row.to_dict() for row in self.by_code],
            "by_section": [row.to_dict() for row in self.by_section],
            "by_operation": [row.to_dict() for row in self.by_operation],
            "by_month": self.by_month,
            "offset_comparison": self.offset_comparison,
            "notes": self.notes,
        }


def _pareto(rows: list[tuple[str, str, float, int, bool | None]]) -> list[ParetoRow]:
    """Classic Pareto: descending by hours, with cumulative percentage."""
    total = sum(row[2] for row in rows)
    ordered = sorted(rows, key=lambda row: (-row[2], row[1]))
    result: list[ParetoRow] = []
    cumulative = 0.0
    for key, label, hours, occurrences, controllable in ordered:
        share = (100.0 * hours / total) if total > 0 else 0.0
        cumulative += share
        result.append(
            ParetoRow(
                key=key,
                label=label,
                hours=hours,
                occurrences=occurrences,
                percent_of_total=share,
                cumulative_percent=cumulative,
                operator_controllable=controllable,
            )
        )
    return result


class NptService:
    """Read-model over recorded NPT."""

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        self.session = session
        self.org_id = org_id

    async def _code_map(self) -> dict[str, NptCode]:
        rows = (
            await self.session.execute(select(NptCode).where(NptCode.org_id == self.org_id))
        ).scalars().all()
        return {row.code: row for row in rows}

    async def events_for_well(self, well_id: str) -> list[NptEvent]:
        codes = await self._code_map()
        rows = list(
            (
                await self.session.execute(
                    select(Event)
                    .where(Event.org_id == self.org_id, Event.well_id == well_id, Event.is_npt.is_(True))
                    .order_by(Event.occurred_at)
                )
            )
            .scalars()
            .all()
        )
        cases: list[NptEvent] = []
        for row in rows:
            hours = float(row.npt_hours or row.duration_hours or 0.0)
            code = codes.get(row.npt_code) if row.npt_code else None
            classification = "recorded" if row.npt_code or row.npt_category else "unclassified"
            cases.append(
                NptEvent(
                    id=row.id,
                    source="event",
                    title=row.title,
                    category=(row.npt_category or (code.category if code else "unclassified")),
                    subcategory=(code.subcategory if code else None),
                    code=row.npt_code,
                    cause=row.root_cause or row.description,
                    hours=hours,
                    started_at=row.occurred_at,
                    ended_at=row.ended_at,
                    depth_md_si=row.depth_md_si,
                    section_id=row.section_id,
                    operation_id=row.operation_id,
                    operator_controllable=(code.is_operator_controllable if code else None),
                    confidence=None,
                    classification_source=classification,
                    evidence_ref=row.evidence_ref,
                    document_id=row.source_document_id,
                )
            )
        return cases

    async def operations_with_npt(self, well_id: str) -> list[NptEvent]:
        rows = list(
            (
                await self.session.execute(
                    select(Operation)
                    .where(
                        Operation.org_id == self.org_id,
                        Operation.well_id == well_id,
                        Operation.npt_hours.is_not(None),
                    )
                    .order_by(Operation.actual_start)
                )
            )
            .scalars()
            .all()
        )
        cases: list[NptEvent] = []
        for row in rows:
            hours = float(row.npt_hours or 0.0)
            if hours <= 0:
                continue
            cases.append(
                NptEvent(
                    id=row.id,
                    source="operation",
                    title=row.name,
                    category="unclassified",
                    subcategory=None,
                    code=row.code,
                    cause=row.remarks,
                    hours=hours,
                    started_at=row.actual_start or row.planned_start,
                    ended_at=row.actual_end or row.planned_end,
                    depth_md_si=row.depth_to_md_si or row.depth_from_md_si,
                    section_id=row.section_id,
                    operation_id=row.id,
                    operator_controllable=None,
                    confidence=None,
                    classification_source="operation_rollup",
                    evidence_ref=None,
                    document_id=row.source_document_id,
                    note=(
                        "NPT recorded on the operation row itself; if a matching event exists this "
                        "row is a roll-up of it, not an additional loss"
                    ),
                )
            )
        return cases

    async def _offset_comparison(self, well: Well, total_hours: float) -> dict[str, Any] | None:
        """Compare against offset wells already analysed for this well — never fabricated."""
        candidates = list(
            (
                await self.session.execute(
                    select(OffsetCandidate)
                    .where(OffsetCandidate.org_id == self.org_id, OffsetCandidate.subject_well_id == well.id)
                    .order_by(OffsetCandidate.rank)
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        if not candidates:
            return None
        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            comparison = candidate.comparison or {}
            npt_entry = comparison.get("npt") if isinstance(comparison, dict) else None
            rows.append(
                {
                    "candidate_well_id": candidate.candidate_well_id,
                    "candidate_name": candidate.candidate_name,
                    "rank": candidate.rank,
                    "overall_similarity": candidate.overall_similarity,
                    "similarity_basis": candidate.rationale,
                    "data_quality": candidate.data_quality,
                    "npt": npt_entry,
                    "comparable": npt_entry is not None,
                }
            )
        return {
            "subject_total_hours": round(total_hours, 3),
            "candidates": rows,
            "note": (
                "Offset NPT is only shown where the offset analysis actually recorded it; "
                "similar wells are not evidence of similar performance"
            ),
        }

    async def summarise(
        self, well_id: str, *, basis: str = "events", include_offsets: bool = True
    ) -> NptSummary:
        if basis not in NPT_BASES:
            raise ValidationFailed(
                f"unknown NPT basis {basis!r}",
                details={"unknown": [basis], "known": list(NPT_BASES)},
            )
        well = (
            await self.session.execute(
                select(Well).where(Well.id == well_id, Well.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        cases = await self.events_for_well(well_id)
        operation_cases = await self.operations_with_npt(well_id)
        hours_events = sum(case.hours for case in cases)
        hours_operations = sum(case.hours for case in operation_cases)

        productive = (
            await self.session.execute(
                select(Operation).where(
                    Operation.org_id == self.org_id,
                    Operation.well_id == well_id,
                    Operation.operation_class == "actual",
                    Operation.is_productive.is_(True),
                )
            )
        ).scalars().all()
        productive_hours = sum(float(row.actual_duration_hours or 0.0) for row in productive)
        selected = cases if basis == "events" else operation_cases
        total = hours_events if basis == "events" else hours_operations
        measured = productive_hours + total

        category_rows: dict[str, tuple[str, str, float, int, bool | None]] = {}
        code_rows: dict[str, tuple[str, str, float, int, bool | None]] = {}
        section_rows: dict[str, tuple[str, str, float, int, bool | None]] = {}
        operation_rows: dict[str, tuple[str, str, float, int, bool | None]] = {}
        month_rows: dict[str, float] = {}
        for case in selected:
            entry = category_rows.get(
                case.category,
                (case.category, case.category.replace("_", " "), 0.0, 0, case.operator_controllable),
            )
            category_rows[case.category] = (
                entry[0],
                entry[1],
                entry[2] + case.hours,
                entry[3] + 1,
                entry[4] if entry[4] is not None else case.operator_controllable,
            )
            code_key = case.code or "unclassified"
            code_entry = code_rows.get(code_key, (code_key, code_key, 0.0, 0, case.operator_controllable))
            code_rows[code_key] = (
                code_entry[0],
                code_entry[1],
                code_entry[2] + case.hours,
                code_entry[3] + 1,
                code_entry[4],
            )
            section_key = case.section_id or "not-sectioned"
            section_entry = section_rows.get(
                section_key, (section_key, section_key, 0.0, 0, None)
            )
            section_rows[section_key] = (
                section_entry[0],
                section_entry[1],
                section_entry[2] + case.hours,
                section_entry[3] + 1,
                None,
            )
            operation_key = case.operation_id or "not-operation-scoped"
            op_entry = operation_rows.get(
                operation_key, (operation_key, operation_key, 0.0, 0, None)
            )
            operation_rows[operation_key] = (
                op_entry[0],
                op_entry[1],
                op_entry[2] + case.hours,
                op_entry[3] + 1,
                None,
            )
            if case.started_at is not None:
                month = case.started_at.strftime("%Y-%m")
                month_rows[month] = month_rows.get(month, 0.0) + case.hours

        for category in STANDARD_CATEGORIES:
            category_rows.setdefault(category, (category, category.replace("_", " "), 0.0, 0, None))

        notes: list[str] = []
        if basis == "events" and hours_operations > hours_events + 0.01:
            notes.append(
                "operations carry more NPT hours than events: part of the recorded NPT is not "
                "broken down into events, so the by-category picture is incomplete"
            )
        if any(case.classification_source == "unclassified" for case in selected):
            unclassified = sum(case.hours for case in selected if case.classification_source == "unclassified")
            notes.append(
                f"{round(unclassified, 2)} h of NPT carries no NPT code; categories are therefore "
                "a lower bound"
            )
        notes.append(
            "NPT hours come from recorded events/operations only. Absent records mean absent hours, "
            "never an assumed zero."
        )

        return NptSummary(
            total_hours=total,
            basis=basis,
            hours_events=hours_events,
            hours_operations=hours_operations,
            event_count=len(selected),
            measured_hours_total=measured,
            percent_of_well_time=(100.0 * total / measured) if measured > 0 else None,
            by_hours=total,
            cases=sorted(selected, key=lambda case: -case.hours),
            by_category=_pareto(list(category_rows.values())),
            by_code=_pareto(list(code_rows.values())),
            by_section=_pareto(list(section_rows.values())),
            by_operation=_pareto(list(operation_rows.values())),
            by_month=[
                {"month": month, "hours": round(hours, 3)} for month, hours in sorted(month_rows.items())
            ],
            controllable_hours=sum(
                case.hours for case in selected if case.operator_controllable is True
            ),
            uncontrollable_hours=sum(
                case.hours for case in selected if case.operator_controllable is False
            ),
            unknown_controllability_hours=sum(
                case.hours for case in selected if case.operator_controllable is None
            ),
            offset_comparison=(
                await self._offset_comparison(well, total) if include_offsets and well is not None else None
            ),
            notes=notes,
        )

    async def well_ids_with_npt(self, *, limit: int = 200) -> list[str]:
        rows = (
            await self.session.execute(
                select(Event.well_id)
                .where(Event.org_id == self.org_id, Event.is_npt.is_(True))
                .distinct()
                .limit(limit)
            )
        ).scalars().all()
        return list(rows)
