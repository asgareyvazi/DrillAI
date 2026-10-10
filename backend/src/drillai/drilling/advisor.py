"""The Operations Advisor.

*"Where are we? What is the current operation? What comes next? How are we performing? What is
NPT? What are the risks? What supports this? What is missing?"*

The advisor's answer is a **typed contract, not prose**. Every response separates:

===============  ===================================================================
``facts``        read directly from recorded rows (operations, events, sections, twin)
``calculations`` produced by deterministic engineering engines, with engine key + version
``evidence``     documents, pages, excerpts and extracted records behind the answer
``inference``    reasoning that goes beyond the records, and the basis it rests on
``recommendation`` a proposed action, which requires explicit engineering justification
``unknown``      what the platform does not know, and how it could be obtained
===============  ===================================================================

Two rules are enforced structurally:

1. **The LLM cannot introduce a number.** Facts and calculations are assembled from the database
   and the engine registry *before* any model is called. When a language model is used it may only
   narrate the sections it is given, and its output is attached to ``inference.narrative`` with the
   model, provider and the fact ids it was shown — it can never mutate ``facts`` or
   ``calculations``.
2. **Missing data is an answer.** ``unknown`` is always present, even when everything else is
   complete, because an advisor that never says "I don't know" is not usable in a drilling review.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.ai.providers import ChatMessage, CompletionRequest
from drillai.ai.router import LlmRouter, TaskProfile
from drillai.core.errors import ValidationFailed
from drillai.db.models import Document, EngineRun, EvidenceLink
from drillai.drilling.npt import NptService
from drillai.drilling.state import WellStateService
from drillai.drilling.timeline import TimelineService

#: Questions the advisor is architecturally able to answer. Each key maps to the sections it
#: populates, so a client can render only what is relevant and the advisor can refuse a question
#: it has no data path for.
ADVISOR_QUESTIONS: dict[str, str] = {
    "where_are_we": "Current depth, section drilled and progress against plan",
    "current_operation": "The operation in progress and how long it has been running",
    "previous_operation": "The last completed operation and its duration variance",
    "next_operation": "The next operation, stating whether it is planned or inferred",
    "performance": "Recorded drilling parameters and their provenance",
    "npt": "Non-productive time with its Pareto, categories and attribution",
    "risks": "Open risks recorded on the well",
    "documents": "Documents on file and what was extracted from them",
    "evidence": "Evidence supporting the current state",
    "offsets": "Offset wells already analysed for this well",
    "engineering_checks": "Which engines are applicable to the current state and their last runs",
    "missing": "What the platform does not know about this well",
}


@dataclass
class Fact:
    """A statement read from recorded data. ``source`` is a table/field reference, never a guess."""

    key: str
    statement: str
    value: Any = None
    unit: str | None = None
    source: str = ""
    source_id: str | None = None
    evidence_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "statement": self.statement,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "source_id": self.source_id,
            "evidence_ref": self.evidence_ref,
        }


@dataclass
class Calculation:
    """A number that came from an engine, carrying the version that produced it."""

    engine_key: str
    engine_version: str
    statement: str
    outputs: dict[str, Any]
    is_feasible: bool | None = None
    warnings: list[str] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    engine_run_id: str | None = None
    ran_at: dt.datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine_key": self.engine_key,
            "engine_version": self.engine_version,
            "statement": self.statement,
            "outputs": self.outputs,
            "is_feasible": self.is_feasible,
            "warnings": self.warnings,
            "violations": self.violations,
            "engine_run_id": self.engine_run_id,
            "ran_at": self.ran_at.isoformat() if self.ran_at else None,
        }


@dataclass
class EvidenceItem:
    id: str
    kind: str
    document_id: str | None
    document_title: str | None
    page_number: int | None
    excerpt: str | None
    method: str | None
    confidence: float | None
    subject_kind: str | None = None
    subject_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "document_id": self.document_id,
            "document_title": self.document_title,
            "page_number": self.page_number,
            "excerpt": self.excerpt,
            "method": self.method,
            "confidence": self.confidence,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
        }


@dataclass
class AdvisorAnswer:
    question: str
    well_id: str
    generated_at: dt.datetime
    facts: list[Fact] = field(default_factory=list)
    calculations: list[Calculation] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    inference: list[dict[str, Any]] = field(default_factory=list)
    narrative: dict[str, Any] | None = None
    recommendation: list[dict[str, Any]] = field(default_factory=list)
    unknown: list[dict[str, Any]] = field(default_factory=list)
    context_used: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "well_id": self.well_id,
            "generated_at": self.generated_at.isoformat(),
            "facts": [item.to_dict() for item in self.facts],
            "calculations": [item.to_dict() for item in self.calculations],
            "evidence": [item.to_dict() for item in self.evidence],
            "inference": self.inference,
            "narrative": self.narrative,
            "recommendation": self.recommendation,
            "unknown": self.unknown,
            "context_used": self.context_used,
            "limitations": self.limitations,
            "sections": {
                "facts": len(self.facts),
                "calculations": len(self.calculations),
                "evidence": len(self.evidence),
                "inference": len(self.inference),
                "recommendation": len(self.recommendation),
                "unknown": len(self.unknown),
            },
        }


class OperationsAdvisor:
    """Assembles an evidence-backed answer about one well.

    The advisor is deliberately *not* an agent loop: it is a composed read over the same services
    the UI uses. An agent (see :mod:`drillai.ai.agents`) can orchestrate it and add narration, but
    the contract below is what the platform guarantees, with or without an LLM.
    """

    def __init__(
        self,
        session: AsyncSession,
        org_id: str,
        *,
        llm_router: LlmRouter | None = None,
        actor: str | None = None,
    ) -> None:
        self.session = session
        self.org_id = org_id
        self.llm_router = llm_router
        self.actor = actor
        self.state_service = WellStateService(session, org_id)
        self.npt_service = NptService(session, org_id)
        self.timeline_service = TimelineService(session, org_id)

    # ------------------------------------------------------------------ evidence

    async def _evidence_for(self, subject_ids: list[str], *, limit: int = 25) -> list[EvidenceItem]:
        if not subject_ids:
            return []
        rows = (
            await self.session.execute(
                select(EvidenceLink)
                .where(EvidenceLink.org_id == self.org_id, EvidenceLink.subject_id.in_(subject_ids))
                .order_by(EvidenceLink.confidence.desc().nullslast(), EvidenceLink.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        items: list[EvidenceItem] = []
        for row in rows:
            title = None
            if row.document_id:
                document = await self.session.get(Document, row.document_id)
                title = document.title if document is not None else None
            items.append(
                EvidenceItem(
                    id=row.id,
                    kind=row.evidence_kind,
                    document_id=row.document_id,
                    document_title=title,
                    page_number=row.page_number,
                    excerpt=row.excerpt,
                    method=row.method,
                    confidence=row.confidence,
                    subject_kind=row.subject_kind,
                    subject_id=row.subject_id,
                )
            )
        return items

    # ------------------------------------------------------------------ engine context

    async def _engine_runs(self, well_id: str, *, limit: int = 20) -> list[EngineRun]:
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

    # ------------------------------------------------------------------ narrative

    async def _narrate(self, answer: AdvisorAnswer) -> dict[str, Any] | None:
        """Optional narration over *already computed* sections.

        The prompt contains only the sections. The model is told, in the prompt itself, that it may
        not introduce numbers, and the response is stored as narrative — never merged into facts.
        """
        if self.llm_router is None or not answer.facts:
            return None
        payload = {
            "facts": [item.to_dict() for item in answer.facts],
            "calculations": [item.to_dict() for item in answer.calculations],
            "unknown": answer.unknown,
        }
        request = CompletionRequest(
            messages=[
                ChatMessage(
                    role="system",
                    content=(
                        "You are a drilling engineering assistant. Summarise the supplied FACTS and "
                        "CALCULATIONS for a drilling supervisor. Use only the values given. Do not "
                        "compute, estimate or invent any number. If something is not in the FACTS, "
                        "say it is unknown. Keep it under 120 words."
                    ),
                ),
                ChatMessage(role="user", content=f"Question: {answer.question}\n\nData: {payload}"),
            ],
            metadata={"purpose": "operations_advisor", "well_id": answer.well_id},
        )
        try:
            response, decision = await self.llm_router.complete(
                request,
                session=self.session,
                org_id=self.org_id,
                profile=TaskProfile.SUMMARIZE,
                subject_kind="well",
                subject_id=answer.well_id,
            )
        except Exception as exc:
            return {
                "status": "unavailable",
                "reason": f"{type(exc).__name__}: {exc}",
                "note": "the structured answer above is unaffected: narration is optional",
            }
        return {
            "status": "generated",
            "text": response.text,
            "provider": response.provider,
            "model": response.model,
            "usage": response.usage.model_dump(),
            "routing": {"reason": decision.rationale, "provider": decision.provider.name},
            "guardrail": "narration only; it cannot modify facts or calculations",
            "prompt_confined_to": ["facts", "calculations", "unknown"],
        }

    # ------------------------------------------------------------------ assembly

    async def ask(self, well_id: str, question: str = "where_are_we") -> AdvisorAnswer:
        if question not in ADVISOR_QUESTIONS:
            raise ValidationFailed(
                f"unknown advisor question {question!r}; known: {sorted(ADVISOR_QUESTIONS)}",
                details={"unknown": [question], "known": sorted(ADVISOR_QUESTIONS)},
            )
        now = dt.datetime.now(tz=dt.UTC)
        answer = AdvisorAnswer(question=question, well_id=well_id, generated_at=now)
        state = await self.state_service.state(well_id)
        well = state["well"]
        progress = state["progress"]
        operation = state["operation"]

        answer.context_used = {
            "well": well["name"],
            "well_id": well["id"],
            "missing_keys": [item["key"] for item in state["missing"]],
        }

        # ---- FACTS: straight from recorded rows -------------------------------------------
        answer.facts.append(
            Fact(
                key="well.identity",
                statement=f"Well {well['name']} ({well['id']}), status {well['status']}",
                value=well["name"],
                source="wells",
                source_id=well["id"],
            )
        )
        current = operation["current"]
        if current is not None:
            answer.facts.append(
                Fact(
                    key="operation.current",
                    statement=f"Current operation: {current['name']} ({current['kind']})",
                    value=current["name"],
                    source="operations",
                    source_id=current["id"],
                )
            )
            if current["actual_start"]:
                answer.facts.append(
                    Fact(
                        key="operation.current.started",
                        statement=f"Started {current['actual_start']}",
                        value=current["actual_start"],
                        source="operations.actual_start",
                        source_id=current["id"],
                    )
                )
            if current["actual_duration_hours"] is not None:
                answer.facts.append(
                    Fact(
                        key="operation.current.duration",
                        statement=f"Duration {current['actual_duration_hours']} h"
                        + (
                            f" against a plan of {current['planned_duration_hours']} h "
                            f"(variance {current['duration_variance_hours']} h)"
                            if current["planned_duration_hours"] is not None
                            else " (no planned duration on file)"
                        ),
                        value=current["actual_duration_hours"],
                        unit="h",
                        source="operations.actual_duration_hours",
                        source_id=current["id"],
                    )
                )
        previous = operation["previous"]
        if previous is not None:
            answer.facts.append(
                Fact(
                    key="operation.previous",
                    statement=f"Previous operation: {previous['name']} ({previous['kind']})",
                    value=previous["name"],
                    source="operations",
                    source_id=previous["id"],
                )
            )
        if current is None:
            answer.inference.append(
                {
                    "key": "operation.current",
                    "statement": "No started operation is recorded, so 'where are we' cannot be answered from data",
                    "basis": "operations table has no row with actual_start",
                    "confidence": "n/a",
                }
            )
        if progress["current_md_si"] is not None:
            answer.facts.append(
                Fact(
                    key="progress.depth",
                    statement=f"Current measured depth {progress['current_md_si']:.1f} m "
                    f"(source {progress['current_md_source']})",
                    value=progress["current_md_si"],
                    unit="m",
                    source=progress["current_md_source"] or "well_sections",
                    source_id=progress["current_section"]["id"] if progress["current_section"] else None,
                )
            )
        if progress["current_section"] is not None:
            section = progress["current_section"]
            answer.facts.append(
                Fact(
                    key="progress.section",
                    statement=f"Section {section['name']} ({section['kind']}), status {section['status']}",
                    value=section["name"],
                    source="well_sections",
                    source_id=section["id"],
                )
            )
        for measured in state["measured"]:
            answer.facts.append(
                Fact(
                    key=f"measured.{measured['key']}",
                    statement=(
                        f"{measured['label']} = {measured['value']} {measured['unit'] or ''}".strip()
                        + (f" (quality {measured['quality']})" if measured["quality"] else "")
                    ),
                    value=measured["value"],
                    unit=measured["unit"],
                    source=measured["source"],
                    source_id=measured["source_id"],
                    evidence_ref=measured["evidence_ref"],
                )
            )

        # ---- NPT ---------------------------------------------------------------------------
        npt = await self.npt_service.summarise(well_id, include_offsets=True)
        if npt.total_hours > 0 or npt.event_count > 0:
            answer.facts.append(
                Fact(
                    key="npt.total",
                    statement=(
                        f"NPT {round(npt.total_hours, 2)} h over {npt.event_count} recorded cases"
                        + (
                            f" ({round(npt.percent_of_well_time, 2)}% of recorded well time)"
                            if npt.percent_of_well_time is not None
                            else ""
                        )
                    ),
                    value=npt.total_hours,
                    unit="h",
                    source=f"events + operations ({npt.basis})",
                )
            )
            for row in npt.by_category[:5]:
                if row.hours <= 0:
                    continue
                answer.facts.append(
                    Fact(
                        key=f"npt.category.{row.key}",
                        statement=(
                            f"NPT category {row.label}: {row.hours} h ({row.percent_of_total}%), "
                            f"{row.occurrences} case(s)"
                        ),
                        value=row.hours,
                        unit="h",
                        source="events.npt_category",
                    )
                )
        for note in npt.notes:
            answer.unknown.append({"key": "npt.caveat", "statement": note, "kind": "caveat"})

        # ---- RISKS --------------------------------------------------------------------------
        for risk in state["risks"][:5]:
            answer.facts.append(
                Fact(
                    key=f"risk.{risk['id']}",
                    statement=f"Risk ({risk['severity']}, {risk['status']}): {risk['title']}",
                    value=risk["title"],
                    source="risks",
                    source_id=risk["id"],
                    evidence_ref=risk["evidence_ref"],
                )
            )

        # ---- CALCULATIONS --------------------------------------------------------------------
        for run in await self._engine_runs(well_id, limit=10):
            if run.status != "succeeded":
                continue
            answer.calculations.append(
                Calculation(
                    engine_key=run.engine_key,
                    engine_version=run.engine_version,
                    statement=(
                        f"{run.engine_key} v{run.engine_version} ran with "
                        f"{'a feasible result' if run.is_feasible else 'constraint violations'}"
                    ),
                    outputs=run.outputs or {},
                    is_feasible=run.is_feasible,
                    warnings=list(run.warnings or []),
                    violations=list(run.constraint_violations or []),
                    engine_run_id=run.id,
                    ran_at=run.finished_at or run.started_at,
                )
            )

        # ---- EVIDENCE ------------------------------------------------------------------------
        subject_ids = [well["id"]]
        subject_ids += [item.source_id for item in answer.facts if item.source_id]
        context = await self._context_documents(well_id)
        answer.evidence.extend(context)
        answer.evidence.extend(await self._evidence_for([sid for sid in subject_ids if sid]))

        # ---- INFERENCE -----------------------------------------------------------------------
        if operation["next_basis"] == "inferred":
            answer.inference.append(
                {
                    "key": "operation.next",
                    "statement": (
                        f"Next operation is probably {operation['next']['name']}"
                        if operation["next"]
                        else "No next operation could be identified"
                    ),
                    "basis": operation["next_note"],
                    "confidence": "plan, not confirmation",
                    "basis_kind": "inferred",
                }
            )
        elif operation["next_basis"] == "planned" and operation["next"]:
            answer.facts.append(
                Fact(
                    key="operation.next",
                    statement=f"Next operation (planned): {operation['next']['name']}",
                    value=operation["next"]["name"],
                    source="operations",
                    source_id=operation["next"]["id"],
                )
            )
        if npt.total_hours > 0 and npt.by_category:
            worst = npt.by_category[0]
            if worst.hours > 0:
                answer.inference.append(
                    {
                        "key": "npt.dominant",
                        "statement": f"The dominant recorded NPT category is {worst.label}",
                        "basis": (
                            f"{worst.hours} h of {round(npt.total_hours, 2)} h NPT "
                            f"({worst.percent_of_total}%)"
                        ),
                        "confidence": "high (arithmetic over recorded events)",
                        "basis_kind": "derived_from_evidence",
                    }
                )

        # ---- UNKNOWN: always populated --------------------------------------------------------
        for item in state["missing"]:
            answer.unknown.append(
                {
                    "key": item["key"],
                    "statement": item["description"],
                    "kind": "missing_data",
                    "why_it_matters": item["why_it_matters"],
                    "how_to_supply": item["how_to_supply"],
                }
            )
        last_engine = max((run.finished_at or run.started_at for run in await self._engine_runs(well_id, limit=1)), default=None)
        if last_engine is None:
            answer.unknown.append(
                {
                    "key": "calculations.none",
                    "statement": "No engineering engine has been run against this well",
                    "kind": "missing_data",
                    "why_it_matters": (
                        "Performance, hydraulics and optimisation statements cannot be made without "
                        "deterministic calculations"
                    ),
                    "how_to_supply": "POST /api/v1/registry/engines/{engine_key}/run",
                }
            )

        # ---- RECOMMENDATION ----------------------------------------------------------------
        recommendations = await self.state_service.recommendations(well_id, limit=5)
        for row in recommendations:
            answer.recommendation.append(
                {
                    "id": row.id,
                    "title": row.title,
                    "statement": row.statement,
                    "status": row.status,
                    "action_level": row.action_level,
                    "confidence": row.confidence,
                    "confidence_basis": row.confidence_basis,
                    "assumptions": row.assumptions,
                    "constraints_applied": row.constraints_applied,
                    "why_not": row.why_not,
                    "engine_run_ids": row.engine_run_ids,
                    "requires_approval": row.action_level in {"L3", "L4", "L5"},
                }
            )
        if not recommendations:
            answer.unknown.append(
                {
                    "key": "recommendation.none",
                    "statement": "No recommendation has been produced for this well",
                    "kind": "missing_data",
                    "why_it_matters": "An advisory answer without a proposed action is incomplete",
                    "how_to_supply": (
                        "run the 'Daily Drilling Intelligence' workflow, or the optimisation engines "
                        "and emit a recommendation"
                    ),
                }
            )

        answer.limitations.append(
            "The advisor reads recorded state. It does not read real-time rig data, and it does not "
            "infer a measurement that was never recorded."
        )
        answer.narrative = await self._narrate(answer)
        return answer

    async def _context_documents(self, well_id: str, *, limit: int = 10) -> list[EvidenceItem]:
        """Documents on file for the well — the coarsest level of evidence, always available."""
        rows = (
            await self.session.execute(
                select(Document)
                .where(Document.org_id == self.org_id, Document.well_id == well_id)
                .order_by(Document.created_at.desc())
                .limit(limit)
            )
        ).scalars().all()
        items: list[EvidenceItem] = []
        for row in rows:
            summary = row.extraction_summary or {}
            items.append(
                EvidenceItem(
                    id=row.id,
                    kind="document",
                    document_id=row.id,
                    document_title=row.title or row.document_number or row.id,
                    page_number=None,
                    excerpt=(
                        f"{row.doc_type} · status {row.status} · "
                        f"{summary.get('records', 0)} extracted records · {row.page_count or 0} page(s)"
                    ),
                    method=summary.get("parser"),
                    confidence=None,
                    subject_kind="well",
                    subject_id=well_id,
                )
            )
        return items


__all__ = [
    "ADVISOR_QUESTIONS",
    "AdvisorAnswer",
    "Calculation",
    "EvidenceItem",
    "Fact",
    "OperationsAdvisor",
]
