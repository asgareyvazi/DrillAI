"""Report data contracts.

A report is a *structured payload*, not a document. Rendering (Word/PDF/HTML) is a presentation
concern that can change; the contract below is what every consumer — API, workflow output node,
email digest, management dashboard — shares.

Every report separates five section kinds, and the separation is enforced by the type:

``facts``          read from recorded rows, each with its table/field source
``calculations``   engine outputs with engine key and version
``evidence``       documents/pages/excerpts with extraction method and confidence
``recommendations`` proposed actions with assumptions, constraints and approval state
``assumptions``    what the report is resting on, and what it could not verify

The EOWR (end-of-well report) has its own builder: it composes well history, section reviews, KPI
and lessons rather than inventing a template.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.errors import ValidationFailed
from drillai.db.models import EngineRun, Lesson, Operation
from drillai.drilling.npt import NptService
from drillai.drilling.state import WellStateService
from drillai.drilling.timeline import TimelineService

#: Report kinds the platform can assemble today. Each is a different composition of the same
#: building blocks; adding a kind means writing a builder, not a template.
REPORT_KINDS: dict[str, str] = {
    "daily_drilling": "The last reporting period: operations, parameters, NPT and evidence",
    "operations_summary": "Operations with planned/actual durations and variance",
    "npt": "NPT total, Pareto and attribution",
    "drilling_performance": "Recorded drilling parameters and engine-derived performance",
    "offset_comparison": "Offset wells analysed for this well with similarity evidence",
    "management_summary": "Headline state, risks and recommendations for a management audience",
    "engineering_calculation": "Engine runs with inputs, outputs, assumptions and violations",
    "recommendation": "Recommendations with why-not, assumptions and approval state",
    "end_of_well": "Well history, sections, KPI, NPT and lessons learned (EOWR foundation)",
}


@dataclass
class ReportSection:
    key: str
    title: str
    kind: str  # facts | calculations | evidence | recommendations | assumptions
    items: list[dict[str, Any]] = field(default_factory=list)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "kind": self.kind,
            "items": self.items,
            "note": self.note,
            "count": len(self.items),
        }


@dataclass
class Report:
    kind: str
    title: str
    well_id: str
    generated_at: dt.datetime
    period_from: dt.datetime | None = None
    period_to: dt.datetime | None = None
    sections: list[ReportSection] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def section(self, key: str) -> ReportSection | None:
        return next((section for section in self.sections if section.key == key), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "well_id": self.well_id,
            "generated_at": self.generated_at.isoformat(),
            "period_from": self.period_from.isoformat() if self.period_from else None,
            "period_to": self.period_to.isoformat() if self.period_to else None,
            "metadata": self.metadata,
            "sections": [section.to_dict() for section in self.sections],
            "section_kinds": sorted({section.kind for section in self.sections}),
            "is_renderable": True,
            "rendering_note": (
                "this payload is the report contract; document rendering (Word/PDF) is a separate "
                "presentation layer and is not implemented"
            ),
        }


class ReportingService:
    """Assembles report payloads from the same services the UI and the advisor read."""

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        self.session = session
        self.org_id = org_id
        self.state = WellStateService(session, org_id)
        self.npt = NptService(session, org_id)
        self.timeline = TimelineService(session, org_id)

    async def _operations(self, well_id: str, *, limit: int = 100) -> list[Operation]:
        return list(
            (
                await self.session.execute(
                    select(Operation)
                    .where(Operation.org_id == self.org_id, Operation.well_id == well_id)
                    .order_by(Operation.sequence)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def _engine_runs(self, well_id: str, *, limit: int = 30) -> list[EngineRun]:
        return list(
            (
                await self.session.execute(
                    select(EngineRun)
                    .where(EngineRun.org_id == self.org_id, EngineRun.well_id == well_id)
                    .order_by(EngineRun.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def _lessons(self, well_id: str) -> list[Lesson]:
        return list(
            (
                await self.session.execute(
                    select(Lesson).where(Lesson.org_id == self.org_id, Lesson.well_id == well_id)
                )
            )
            .scalars()
            .all()
        )

    def _state_sections(self, state: dict[str, Any], *, title: str) -> ReportSection:
        items: list[dict[str, Any]] = []
        for fact in state["measured"]:
            items.append(
                {
                    "key": fact["key"],
                    "label": fact["label"],
                    "value": fact["value"],
                    "unit": fact["unit"],
                    "source": fact["source"],
                    "quality": fact["quality"],
                    "observed_at": fact["observed_at"],
                    "evidence_ref": fact["evidence_ref"],
                }
            )
        progress = state["progress"]
        items.append(
            {
                "key": "progress.current_md",
                "label": "Current measured depth",
                "value": progress["current_md_si"],
                "unit": "m",
                "source": progress["current_md_source"],
                "quality": "measured" if progress["current_md_si"] is not None else "missing",
            }
        )
        return ReportSection(key="facts", title=title, kind="facts", items=items)

    async def build(
        self,
        kind: str,
        well_id: str,
        *,
        period_from: dt.datetime | None = None,
        period_to: dt.datetime | None = None,
    ) -> Report:
        if kind not in REPORT_KINDS:
            raise ValidationFailed(
                f"unknown report kind {kind!r}",
                details={"unknown": [kind], "known": sorted(REPORT_KINDS)},
            )
        now = dt.datetime.now(tz=dt.UTC)
        state = await self.state.state(well_id)
        report = Report(
            kind=kind,
            title=f"{REPORT_KINDS[kind]} — {state['well']['name']}",
            well_id=well_id,
            generated_at=now,
            period_from=period_from,
            period_to=period_to,
        )
        report.metadata = {
            "well": state["well"],
            "progress": state["progress"],
            "counts": state["counts"],
            "generated_by": "drillai.drilling.reporting",
        }

        timeline = await self.timeline.build(
            well_id, since=period_from, until=period_to, limit=400
        )
        if kind in {"daily_drilling", "management_summary", "operations_summary", "end_of_well"}:
            report.sections.append(
                ReportSection(
                    key="timeline",
                    title="Chronology",
                    kind="facts",
                    items=[entry.to_dict() for entry in timeline],
                    note=(
                        "one merged view of operations, events, documents, engine runs, "
                        "recommendations, twin changes and approvals"
                    ),
                )
            )
        if kind in {"daily_drilling", "drilling_performance", "management_summary", "end_of_well"}:
            report.sections.append(
                self._state_sections(state, title="Recorded drilling state")
            )
        if kind in {"daily_drilling", "operations_summary", "end_of_well"}:
            operations = await self._operations(well_id)
            report.sections.append(
                ReportSection(
                    key="operations",
                    title="Operations with plan variance",
                    kind="facts",
                    items=[
                        {
                            "id": row.id,
                            "sequence": row.sequence,
                            "name": row.name,
                            "kind": row.kind,
                            "class": row.operation_class,
                            "planned_duration_hours": row.planned_duration_hours,
                            "actual_duration_hours": row.actual_duration_hours,
                            "variance_hours": (
                                round(
                                    float(row.actual_duration_hours) - float(row.planned_duration_hours), 2
                                )
                                if row.actual_duration_hours is not None
                                and row.planned_duration_hours is not None
                                else None
                            ),
                            "npt_hours": row.npt_hours,
                            "is_productive": row.is_productive,
                            "source_document_id": row.source_document_id,
                            "data_quality": row.data_quality,
                        }
                        for row in operations
                    ],
                )
            )
        if kind in {"npt", "daily_drilling", "management_summary", "end_of_well"}:
            summary = await self.npt.summarise(well_id)
            report.sections.append(
                ReportSection(
                    key="npt",
                    title="Non-productive time",
                    kind="facts",
                    items=[summary.to_dict()],
                    note="derived from recorded NPT events and operation roll-ups only",
                )
            )
            report.sections.append(
                ReportSection(
                    key="assumptions",
                    title="Assumptions and caveats",
                    kind="assumptions",
                    items=[{"key": f"npt.{index}", "statement": note} for index, note in enumerate(summary.notes)],
                )
            )
        if kind in {"engineering_calculation", "drilling_performance", "management_summary", "end_of_well"}:
            runs = await self._engine_runs(well_id)
            report.sections.append(
                ReportSection(
                    key="calculations",
                    title="Engineering calculations",
                    kind="calculations",
                    items=[
                        {
                            "engine_key": run.engine_key,
                            "engine_version": run.engine_version,
                            "status": run.status,
                            "is_feasible": run.is_feasible,
                            "inputs": run.inputs,
                            "outputs": run.outputs,
                            "assumptions": run.assumptions,
                            "limitations": run.limitations,
                            "warnings": run.warnings,
                            "constraint_violations": run.constraint_violations,
                            "inputs_hash": run.inputs_hash,
                            "outputs_hash": run.outputs_hash,
                            "trace_id": run.trace_id,
                            "ran_at": (run.finished_at or run.started_at).isoformat()
                            if (run.finished_at or run.started_at)
                            else None,
                        }
                        for run in runs
                    ],
                    note="a historical result is reproducible from inputs_hash plus engine version",
                )
            )
        if kind in {"offset_comparison", "management_summary", "end_of_well"}:
            summary = await self.npt.summarise(well_id, include_offsets=True)
            report.sections.append(
                ReportSection(
                    key="offsets",
                    title="Offset comparison",
                    kind="evidence",
                    items=[summary.offset_comparison] if summary.offset_comparison else [],
                    note=(
                        "empty means no offset analysis has been run for this well — absence of "
                        "offsets is stated, not substituted"
                    ),
                )
            )
        if kind in {"recommendation", "management_summary", "daily_drilling", "end_of_well"}:
            items: list[dict[str, Any]] = []
            for row in await self.state.recommendations(well_id, limit=20):
                items.append(
                    {
                        "id": row.id,
                        "title": row.title,
                        "statement": row.statement,
                        "parameters": row.parameters,
                        "why_not": row.why_not,
                        "alternatives": row.alternatives,
                        "assumptions": row.assumptions,
                        "constraints_applied": row.constraints_applied,
                        "sensitivities": row.sensitivities,
                        "uncertainty": row.uncertainty,
                        "confidence": row.confidence,
                        "confidence_basis": row.confidence_basis,
                        "status": row.status,
                        "action_level": row.action_level,
                        "engine_run_ids": row.engine_run_ids,
                        "offset_well_ids": row.offset_well_ids,
                        "created_by_kind": row.created_by_kind,
                        "reviewed_by": row.reviewed_by,
                    }
                )
            report.sections.append(
                ReportSection(
                    key="recommendations",
                    title="Recommendations and decisions",
                    kind="recommendations",
                    items=items,
                    note="each recommendation carries its own why-not and approval state",
                )
            )
        if kind == "end_of_well":
            lessons = await self._lessons(well_id)
            report.sections.append(
                ReportSection(
                    key="lessons",
                    title="Lessons learned",
                    kind="facts",
                    items=[
                        {
                            "id": row.id,
                            "title": row.title,
                            "description": row.description,
                            "category": row.category,
                            "recommendation": row.recommendation,
                            "recurrence_count": row.recurrence_count,
                            "evidence_ref": row.evidence_ref,
                        }
                        for row in lessons
                    ],
                    note="lessons recorded against this well; nothing is synthesised for the report",
                )
            )
            report.sections.append(
                ReportSection(
                    key="open_questions",
                    title="What the record does not answer",
                    kind="assumptions",
                    items=state["missing"],
                )
            )
        if not any(section.kind == "evidence" for section in report.sections):
            evidence_items = [
                entry.to_dict() for entry in timeline if entry.kind in {"document", "engine_run"}
            ][:50]
            report.sections.append(
                ReportSection(
                    key="evidence",
                    title="Evidence trail",
                    kind="evidence",
                    items=evidence_items,
                )
            )
        return report
