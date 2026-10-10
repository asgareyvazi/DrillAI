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

from sqlalchemy import and_, case, extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.errors import NotFound, ValidationFailed
from drillai.db.models import (
    NPT_CATEGORY_ALIASES,
    Event,
    NptCode,
    OffsetCandidate,
    Operation,
    Well,
    canonical_npt_category,
)
from drillai.db.models.operations import NPT_CATEGORIES

#: Which recorded source a reported total is built from.
NPT_BASES = ("events", "operations")

#: The one category that is a denial rather than a loss, imported by name so the rule reads here too.
NOT_NPT_CATEGORY = "not_npt"


def canonical_category_sql(column: Any) -> Any:
    """The canonical category of a stored value, translated *in SQL*.

    Rows written before the vocabulary was unified — and rows a connector will write tomorrow — may carry
    a raw spelling (``kick_well_control``). This expression translates them inside the GROUP BY, so a
    legacy kick is counted with the canonical ``well_control`` instead of forming a bucket of one that no
    chart legend knows about. It is generated from :data:`NPT_CATEGORY_ALIASES`, so mapping an alias once
    reaches the aggregation: there is no second table of translations to keep in step, and nothing is
    loaded into Python to be renamed there.
    """

    normalized = func.lower(func.trim(column))
    aliases = [
        (normalized == raw, canonical) for raw, canonical in NPT_CATEGORY_ALIASES.items()
    ]
    return case(*aliases, else_=normalized)

#: The chart's categories: the canonical NPT vocabulary, minus ``not_npt``. Derived rather than
#: re-declared — this tuple used to be its own list, and it disagreed with the vocabulary the event
#: service validates against in five of its eleven values (``hole_problems`` vs ``hole_problem``,
#: ``well_control`` vs ``kick_well_control``, ``unclassified`` vs ``unknown``, ``downhole_tools`` vs
#: ``tool_failure``, ``surface_equipment`` vs ``rig_equipment``). A report whose buckets are spelled
#: differently from the rows cannot show them, so the buckets *are* the rows' spelling.
STANDARD_CATEGORIES = tuple(
    category for category in NPT_CATEGORIES if category != NOT_NPT_CATEGORY
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
    #: Whether ``cases`` is a bounded prefix of everything that matched, rather than all of it.
    cases_truncated: bool = False
    case_limit: int = 500

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
            "cases_truncated": self.cases_truncated,
            "case_limit": self.case_limit,
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

    def _event_filters(
        self,
        well_id: str,
        *,
        section_id: str | None = None,
        operation_id: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> list[Any]:
        """The scope of an NPT question, written once so the cases and the aggregates agree."""
        clauses: list[Any] = [
            Event.org_id == self.org_id,
            Event.well_id == well_id,
            Event.is_npt.is_(True),
        ]
        if section_id is not None:
            clauses.append(Event.section_id == section_id)
        if operation_id is not None:
            clauses.append(Event.operation_id == operation_id)
        if since is not None:
            clauses.append(Event.occurred_at >= since)
        if until is not None:
            clauses.append(Event.occurred_at <= until)
        return clauses

    async def events_for_well(
        self,
        well_id: str,
        *,
        section_id: str | None = None,
        operation_id: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        limit: int | None = None,
    ) -> list[NptEvent]:
        codes = await self._code_map()
        stmt = (
            select(Event)
            .where(
                *self._event_filters(
                    well_id,
                    section_id=section_id,
                    operation_id=operation_id,
                    since=since,
                    until=until,
                )
            )
            .order_by(Event.occurred_at)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = list((await self.session.execute(stmt)).scalars().all())
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
                    category=(
                        canonical_npt_category(row.npt_category)
                        or (code.category if code else "unclassified")
                    ),
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

    def _operation_filters(
        self,
        well_id: str,
        *,
        section_id: str | None = None,
        operation_id: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> list[Any]:
        started = func.coalesce(Operation.actual_start, Operation.planned_start)
        clauses: list[Any] = [
            Operation.org_id == self.org_id,
            Operation.well_id == well_id,
            # A zero-hour roll-up is "no loss booked here", not an NPT occurrence: keeping it would
            # inflate the occurrence count and put an empty row in the evidence list.
            Operation.npt_hours > 0,
        ]
        if section_id is not None:
            clauses.append(Operation.section_id == section_id)
        if operation_id is not None:
            clauses.append(Operation.id == operation_id)
        if since is not None:
            clauses.append(started >= since)
        if until is not None:
            clauses.append(started <= until)
        return clauses

    async def operations_with_npt(
        self,
        well_id: str,
        *,
        section_id: str | None = None,
        operation_id: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        limit: int | None = None,
    ) -> list[NptEvent]:
        stmt = (
            select(Operation)
            .where(
                *self._operation_filters(
                    well_id,
                    section_id=section_id,
                    operation_id=operation_id,
                    since=since,
                    until=until,
                )
            )
            .order_by(Operation.actual_start)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = list((await self.session.execute(stmt)).scalars().all())
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

    async def _event_aggregates(self, clauses: list[Any]) -> dict[str, Any]:
        """Every grouped number for the events basis, computed by the database.

        The previous version read every NPT row on the well into Python and built the buckets there:
        the answer was right, but the work grew with the well's history while the request stayed the
        same size. These are five grouped queries with a bounded result — one row per category, code,
        section, operation and month — and the sums come back already summed.
        """
        hours = func.coalesce(Event.npt_hours, Event.duration_hours, 0.0)
        controllable = NptCode.is_operator_controllable
        code_join = and_(NptCode.org_id == Event.org_id, NptCode.code == Event.npt_code)
        category = func.coalesce(canonical_category_sql(Event.npt_category), NptCode.category, "unclassified")

        async def grouped(key_expr: Any, label_expr: Any, *extra_group: Any) -> list[tuple]:
            stmt = (
                select(
                    key_expr,
                    label_expr,
                    func.sum(hours),
                    func.count(),
                    func.max(controllable),
                )
                .select_from(Event)
                .outerjoin(NptCode, code_join)
                .where(*clauses)
                .group_by(key_expr, label_expr, *extra_group)
                .order_by(func.sum(hours).desc())
            )
            rows = (await self.session.execute(stmt)).all()
            return [(str(row[0]), str(row[1]), float(row[2] or 0.0), int(row[3]), row[4]) for row in rows]

        # An event row carrying `is_npt` is an NPT occurrence whatever its hours: the flag is the
        # recorder's claim, and an event that reports no loss is still part of the record. (An
        # operation roll-up is the other way round — its column *is* the quantity, so a zero there
        # means "nothing booked" and is not counted.) The totals exclude nothing either way: a
        # zero-hour row adds zero.
        totals = (
            await self.session.execute(
                select(
                    func.sum(hours),
                    func.count(),
                    func.sum(case((controllable.is_(True), hours), else_=0.0)),
                    func.sum(case((controllable.is_(False), hours), else_=0.0)),
                    func.sum(
                        case(
                            (
                                and_(
                                    controllable.is_not(True),
                                    controllable.is_not(False),
                                ),
                                hours,
                            ),
                            else_=0.0,
                        )
                    ),
                )
                .select_from(Event)
                .outerjoin(NptCode, code_join)
                .where(*clauses)
            )
        ).one()

        months = (
            await self.session.execute(
                select(
                    extract("year", Event.occurred_at),
                    extract("month", Event.occurred_at),
                    func.sum(hours),
                )
                .where(*clauses, Event.occurred_at.is_not(None))
                .group_by(extract("year", Event.occurred_at), extract("month", Event.occurred_at))
                .order_by(extract("year", Event.occurred_at), extract("month", Event.occurred_at))
            )
        ).all()
        return {
            "hours": float(totals[0] or 0.0),
            "count": int(totals[1] or 0),
            "controllable": float(totals[2] or 0.0),
            "uncontrollable": float(totals[3] or 0.0),
            "unknown": float(totals[4] or 0.0),
            "by_category": await grouped(category, category),
            "by_code": await grouped(
                func.coalesce(Event.npt_code, "unclassified"), func.coalesce(Event.npt_code, "unclassified")
            ),
            "by_section": await grouped(
                func.coalesce(Event.section_id, "not-sectioned"),
                func.coalesce(Event.section_id, "not-sectioned"),
            ),
            "by_operation": await grouped(
                func.coalesce(Event.operation_id, "not-operation-scoped"),
                func.coalesce(Event.operation_id, "not-operation-scoped"),
            ),
            "by_month": [
                {"month": f"{int(year):04d}-{int(month):02d}", "hours": round(float(total or 0.0), 3)}
                for year, month, total in months
            ],
        }

    async def _operation_aggregates(self, clauses: list[Any]) -> dict[str, Any]:
        """The same groups for the operations basis, where the category is the row's own claim."""
        hours = Operation.npt_hours
        started = func.coalesce(Operation.actual_start, Operation.planned_start)

        async def grouped(key_expr: Any, label_expr: Any) -> list[tuple]:
            stmt = (
                select(key_expr, label_expr, func.sum(hours), func.count())
                .where(*clauses)
                .group_by(key_expr, label_expr)
                .order_by(func.sum(hours).desc())
            )
            rows = (await self.session.execute(stmt)).all()
            return [
                (str(row[0]), str(row[1] or row[0]), float(row[2] or 0.0), int(row[3]), None) for row in rows
            ]

        totals = (
            await self.session.execute(select(func.sum(hours), func.count()).where(*clauses))
        ).one()
        months = (
            await self.session.execute(
                select(extract("year", started), extract("month", started), func.sum(hours))
                .where(*clauses)
                .group_by(extract("year", started), extract("month", started))
                .order_by(extract("year", started), extract("month", started))
            )
        ).all()
        hours_total = float(totals[0] or 0.0)
        by_category = await grouped(
            func.coalesce(Operation.code, "unclassified"), func.coalesce(Operation.code, "unclassified")
        )
        return {
            "hours": hours_total,
            "count": int(totals[1] or 0),
            # An operation row's own roll-up carries no controllability judgement, so every hour is
            # reported as unknown rather than being split into buckets nobody established.
            "controllable": 0.0,
            "uncontrollable": 0.0,
            "unknown": hours_total,
            # On this basis the row *is* the operation, so its code is the closest thing to a
            # category the platform has — and where there is none the bucket says so.
            "by_category": by_category,
            "by_code": by_category,
            "by_section": await grouped(
                func.coalesce(Operation.section_id, "not-sectioned"),
                func.coalesce(Operation.section_id, "not-sectioned"),
            ),
            "by_operation": await grouped(Operation.id, Operation.name),
            "by_month": [
                {"month": f"{int(year):04d}-{int(month):02d}", "hours": round(float(value or 0.0), 3)}
                for year, month, value in months
                if year is not None and month is not None
            ],
        }

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
        self,
        well_id: str,
        *,
        basis: str = "events",
        include_offsets: bool = True,
        section_id: str | None = None,
        operation_id: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        case_limit: int = 500,
    ) -> NptSummary:
        """NPT for one well, optionally scoped to a section or an operation.

        The totals and every breakdown are computed by the database over the same scope; ``cases`` is
        the evidence list behind them and is bounded, with ``cases_truncated`` saying when it was cut
        so a client never reads a short list as a complete one.
        """
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
        if well is None:
            # Not "a well with no NPT": a well this caller cannot see. Returning a zero summary made
            # a foreign well indistinguishable from a well that had a clean month.
            raise NotFound("well not found", details={"well_id": well_id})
        event_clauses = self._event_filters(
            well_id, section_id=section_id, operation_id=operation_id, since=since, until=until
        )
        operation_clauses = self._operation_filters(
            well_id, section_id=section_id, operation_id=operation_id, since=since, until=until
        )
        event_totals = await self._event_aggregates(event_clauses)
        operation_totals = await self._operation_aggregates(operation_clauses)
        hours_events = event_totals["hours"]
        hours_operations = operation_totals["hours"]

        productive_hours = float(
            (
                await self.session.execute(
                    select(func.sum(Operation.actual_duration_hours)).where(
                        Operation.org_id == self.org_id,
                        Operation.well_id == well_id,
                        Operation.operation_class == "actual",
                        Operation.is_productive.is_(True),
                    )
                )
            ).scalar_one_or_none()
            or 0.0
        )

        cases = await self.events_for_well(
            well_id,
            section_id=section_id,
            operation_id=operation_id,
            since=since,
            until=until,
            limit=case_limit,
        )
        operation_cases = await self.operations_with_npt(
            well_id,
            section_id=section_id,
            operation_id=operation_id,
            since=since,
            until=until,
            limit=case_limit,
        )
        selected = cases if basis == "events" else operation_cases
        totals = event_totals if basis == "events" else operation_totals
        total = hours_events if basis == "events" else hours_operations
        measured = productive_hours + total
        # The occurrence count comes from the aggregates, not from the page of cases: a bounded list
        # must not shrink a number that describes the whole scope.
        occurrences = int(totals["count"])
        truncated = occurrences > len(selected)

        category_rows = [tuple(row) for row in totals["by_category"]]
        for category in STANDARD_CATEGORIES:
            if not any(row[0] == category for row in category_rows):
                category_rows.append((category, category.replace("_", " "), 0.0, 0, None))

        notes: list[str] = []
        if any(scoped is not None for scoped in (section_id, operation_id, since, until)):
            notes.append(
                "the summary is scoped: totals and breakdowns cover only the selected "
                "section/operation and time window, not the whole well"
            )
        if truncated:
            notes.append(
                f"the case list is bounded to {case_limit} rows; totals and breakdowns are computed "
                "over every matching row"
            )
        if basis == "events" and hours_operations > hours_events + 0.01:
            notes.append(
                "operations carry more NPT hours than events: part of the recorded NPT is not "
                "broken down into events, so the by-category picture is incomplete"
            )
        if any(case.classification_source == "unclassified" for case in selected) or (
            basis == "events" and not selected and hours_events > 0
        ):
            notes.append(
                "part of the recorded NPT carries no NPT code; categories are therefore a lower bound"
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
            event_count=occurrences,
            measured_hours_total=measured,
            percent_of_well_time=(100.0 * total / measured) if measured > 0 else None,
            by_hours=total,
            cases=sorted(selected, key=lambda case: -case.hours),
            by_category=_pareto(list(category_rows)),
            by_code=_pareto([tuple(row) for row in totals["by_code"]]),
            by_section=_pareto([tuple(row) for row in totals["by_section"]]),
            by_operation=_pareto([tuple(row) for row in totals["by_operation"]]),
            by_month=list(totals["by_month"]),
            controllable_hours=totals["controllable"],
            uncontrollable_hours=totals["uncontrollable"],
            unknown_controllability_hours=totals["unknown"],
            offset_comparison=(
                await self._offset_comparison(well, total) if include_offsets and well is not None else None
            ),
            notes=notes,
            cases_truncated=truncated,
            case_limit=case_limit,
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
