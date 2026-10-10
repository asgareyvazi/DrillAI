"""Operational readiness: are the equipment, materials and services ready for the operation?

Planning fails on logistics far more often than on mathematics. This engine turns the
equipment/BOM data into an explicit readiness assessment:

* per-requirement status with the reason (shortage, lead time, certification, no source),
* the **latest order date** that still meets the required-by date,
* a roll-up where a single unready critical item makes the whole scope not ready,
* the specific items that need a decision, so a plan is never "ready" by accident.

Dates are ISO strings; the assessment is deterministic and uses the supplied ``as_of`` date
rather than the wall clock, so results are reproducible.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field

from drillai.engines.contract import ConstraintViolation, EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field

READY_STATES = {"available", "allocated", "delivered"}


class ReadinessRequirement(BaseModel):
    """One line of the engineering BOM / service requirement."""

    model_config = ConfigDict(extra="forbid")
    item_id: str
    name: str
    category: str = Field(default="equipment", description="equipment|material|service|consumable|personnel")
    quantity_required: float = quantity_field(1.0, "1", gt=0.0)
    quantity_available: float = quantity_field(0.0, "1", ge=0.0)
    unit: str = Field(default="unit", description="display unit of the quantity (ea, m, t, set)")
    status: str = Field(
        default="unavailable",
        description="available|allocated|delivered|on_order|unavailable|substitute",
    )
    criticality: str = Field(default="standard", description="critical|standard|optional")
    required_by: str | None = Field(default=None, description="ISO date the item is needed on location")
    lead_time_days: float = quantity_field(0.0, "d", ge=0.0)
    certification_valid_until: str | None = None
    source: str | None = Field(default=None, description="vendor, yard or internal source")
    substitute_for: str | None = Field(default=None, description="if this is a substitute, the item it replaces")
    notes: str | None = None


class ReadinessInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirements: list[ReadinessRequirement] = Field(min_length=1)
    as_of: str | None = Field(default=None, description="ISO date used for all date arithmetic; defaults to today")
    scope: str = Field(default="operation", description="what the readiness assessment covers (spud, section, job)")


class RequirementAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: str
    name: str
    category: str
    criticality: str
    state: str = Field(description="ready|at_risk|not_ready|not_required")
    quantity_shortfall: float
    latest_order_date: str | None = None
    reasons: list[str]
    actions: list[str]


class ReadinessOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str
    as_of: str
    overall_state: str
    ready_count: int
    at_risk_count: int
    not_ready_count: int
    critical_items_not_ready: list[str]
    items: list[RequirementAssessment]
    next_required_date: str | None = None
    longest_lead_time_days: float
    notes: list[str]


def _parse(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def compute_readiness(inputs: ReadinessInput) -> EngineResult:
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []
    as_of = _parse(inputs.as_of) or dt.date.today()

    assessments: list[RequirementAssessment] = []
    for requirement in inputs.requirements:
        shortfall = max(0.0, requirement.quantity_required - requirement.quantity_available)
        reasons: list[str] = []
        actions: list[str] = []
        state = "ready"

        if requirement.status not in READY_STATES:
            if requirement.status == "on_order":
                reasons.append("item is on order but not yet on location")
                state = "at_risk"
            elif requirement.status == "substitute":
                reasons.append(
                    f"substitute in use{'' if not requirement.substitute_for else f' for {requirement.substitute_for}'}"
                )
                state = "at_risk"
                actions.append("verify the substitute is approved for the application")
            else:
                reasons.append(f"status {requirement.status!r}")
                state = "not_ready"

        if shortfall > 0:
            reasons.append(
                f"short by {shortfall:.2f} {requirement.unit} of {requirement.quantity_required:.2f} required"
            )
            state = "not_ready"
            actions.append(f"source {shortfall:.2f} {requirement.unit}")

        required_by = _parse(requirement.required_by)
        latest_order_date = None
        if required_by is not None:
            latest_order_date = required_by - dt.timedelta(days=requirement.lead_time_days)
            if latest_order_date < as_of and state != "ready":
                reasons.append(
                    f"lead time of {requirement.lead_time_days:.0f} d means the item had to be ordered by "
                    f"{latest_order_date.isoformat()}"
                )
                state = "not_ready"
                actions.append("escalate to expedite or find an alternative source")
        elif requirement.lead_time_days > 0 and state != "ready":
            warnings.append(f"{requirement.item_id}: no required-by date, so the lead time cannot be scheduled")
            if state == "ready":
                state = "at_risk"

        certification = _parse(requirement.certification_valid_until)
        if certification is not None and certification < as_of:
            reasons.append(f"certification expired on {certification.isoformat()}")
            state = "not_ready"
            actions.append("re-certify or replace the item")

        if not reasons:
            reasons.append("available, quantified and certified")

        assessments.append(
            RequirementAssessment(
                item_id=requirement.item_id,
                name=requirement.name,
                category=requirement.category,
                criticality=requirement.criticality,
                state=state,
                quantity_shortfall=shortfall,
                latest_order_date=latest_order_date.isoformat() if latest_order_date else None,
                reasons=reasons,
                actions=actions,
            )
        )

    critical_not_ready = [
        assessment.item_id
        for assessment in assessments
        if assessment.criticality == "critical" and assessment.state == "not_ready"
    ]
    at_risk = [assessment for assessment in assessments if assessment.state == "at_risk"]
    overall = "ready"
    if critical_not_ready:
        overall = "not_ready"
    elif at_risk:
        overall = "at_risk"

    for assessment in assessments:
        if assessment.criticality == "critical" and assessment.state == "not_ready":
            violations.append(
                ConstraintViolation(
                    name="critical_item_not_ready",
                    message=f"critical item {assessment.item_id} ({assessment.name}) is not ready: {'; '.join(assessment.reasons)}",
                    severity="error",
                    source="readiness roll-up",
                )
            )
        elif assessment.state == "at_risk":
            violations.append(
                ConstraintViolation(
                    name="item_at_risk",
                    message=f"{assessment.item_id} ({assessment.name}) is at risk: {'; '.join(assessment.reasons)}",
                    severity="warning",
                    source="readiness roll-up",
                )
            )

    required_dates = [date for date in (_parse(item.required_by) for item in inputs.requirements) if date]
    notes = [
        "a single critical item that is not ready makes the whole scope not ready",
        "latest order date = required-by date − lead time, evaluated against the supplied as_of date",
        "at-risk items never block the roll-up but are always reported",
    ]
    outputs = ReadinessOutput(
        scope=inputs.scope,
        as_of=as_of.isoformat(),
        overall_state=overall,
        ready_count=sum(1 for assessment in assessments if assessment.state == "ready"),
        at_risk_count=len(at_risk),
        not_ready_count=sum(1 for assessment in assessments if assessment.state == "not_ready"),
        critical_items_not_ready=critical_not_ready,
        items=assessments,
        next_required_date=min(required_dates).isoformat() if required_dates else None,
        longest_lead_time_days=max((item.lead_time_days for item in inputs.requirements), default=0.0),
        notes=notes,
    )
    return EngineResult(outputs=outputs, warnings=warnings, violations=violations, assumptions_applied=notes)


READINESS_SPEC = EngineSpec(
    key="readiness.assessment",
    name="Operational readiness assessment",
    version="1.1.0",
    domain_pack="Readiness Pack",
    category="readiness",
    summary=(
        "Roll-up of equipment, material and service readiness: shortages, lead-time feasibility, "
        "certification validity and the critical items that block the operation."
    ),
    inputs_model=ReadinessInput,
    outputs_model=ReadinessOutput,
    consumes=("equipment.inventory", "services.availability", "schedule.plan", "well.identity"),
    produces=("readiness.state",),
    parameters={
        "date_basis": {"value": "as_of_input", "description": "dates are evaluated against the supplied as_of date"},
        "criticality_rule": {"value": "critical blocks scope"},
    },
    assumptions=(
        "quantity available reflects the inventory at the as-of date",
        "lead times are calendar days and do not model partial shipments",
        "certification validity covers the whole operation window",
    ),
    limitations=(
        "no probabilistic lead-time modelling",
        "no cost or vendor-priority optimisation",
        "personnel competency matrices are out of scope for this engine",
    ),
    references=("Well construction readiness practice (BOM availability, lead-time feasibility)",),
    tags=("readiness", "logistics", "bom", "planning"),
    action_level="L1",
)

register_engine(FunctionEngine(READINESS_SPEC, compute_readiness), aliases=("readiness", "operational_readiness"))
