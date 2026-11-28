"""Structured schemas and deterministic rules for drilling programs and controlled procedures.

These are authoring contracts, not generated engineering answers. Every physical quantity carries
an explicit dimension, source unit and provenance state; normalization retains the entered value
and unit alongside the registry-derived canonical SI value. Unknown and conflicting observations are
valid draft data and remain visible, but cannot satisfy a required known design input at submission.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from drillai.core.errors import UnitError
from drillai.units.registry import Dimension, canonical_unit, convert, get_unit

SCHEMA_KEYS = {
    "program": ("drilling_program", 1),
    "procedure": ("controlled_procedure", 1),
}


class _ProductModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class QuantityObservation(_ProductModel):
    """One sourced value in a conflict set; no source value is selected automatically."""

    value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=40)
    source_kind: Literal["operator_entered", "source_document", "engine_run"]
    source_ref: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def source_is_explicit(self) -> QuantityObservation:
        if self.source_kind in {"source_document", "engine_run"} and not self.source_ref:
            raise ValueError("source_document and engine_run observations require a source_ref")
        if self.source_kind == "operator_entered" and self.source_ref is not None:
            raise ValueError("operator_entered observations do not accept a source_ref")
        try:
            get_unit(self.unit)
        except UnitError as exc:
            raise ValueError(str(exc)) from exc
        return self


class EngineeringQuantity(_ProductModel):
    """A known, unknown or conflicting physical quantity without inferred values or units."""

    state: Literal["known", "unknown", "conflicting"]
    dimension: Dimension
    value: float | None = Field(default=None, allow_inf_nan=False)
    unit: str | None = Field(default=None, min_length=1, max_length=40)
    source_kind: Literal["operator_entered", "source_document", "engine_run"] | None = None
    source_ref: str | None = Field(default=None, min_length=1, max_length=64)
    reason: str | None = Field(default=None, max_length=1000)
    observations: list[QuantityObservation] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def state_has_only_its_declared_evidence(self) -> EngineeringQuantity:
        if self.state == "known":
            if self.value is None or not self.unit or self.source_kind is None:
                raise ValueError("known quantities require value, unit and source_kind")
            if self.observations:
                raise ValueError("known quantities cannot also contain conflicting observations")
            if self.source_kind in {"source_document", "engine_run"} and not self.source_ref:
                raise ValueError("source_document and engine_run quantities require a source_ref")
            if self.source_kind == "operator_entered" and self.source_ref is not None:
                raise ValueError("operator_entered quantities do not accept a source_ref")
            try:
                registered = get_unit(self.unit)
                if registered.dimension is not self.dimension:
                    raise ValueError(
                        f"unit {self.unit!r} has dimension {registered.dimension.value!r}, "
                        f"not {self.dimension.value!r}"
                    )
                convert(float(self.value), registered.symbol, canonical_unit(self.dimension))
            except UnitError as exc:
                raise ValueError(str(exc)) from exc
        elif self.state == "unknown":
            if self.value is not None or self.unit is not None or self.source_kind is not None or self.source_ref is not None:
                raise ValueError("unknown quantities must not supply a value, unit or source")
            if self.observations:
                raise ValueError("unknown quantities cannot contain observations")
            if not self.reason:
                raise ValueError("unknown quantities require an explicit reason")
        else:
            if self.value is not None or self.unit is not None or self.source_kind is not None or self.source_ref is not None:
                raise ValueError("conflicting quantities must not select a value, unit or source")
            if len(self.observations) < 2:
                raise ValueError("conflicting quantities require at least two retained observations")
            if not self.reason:
                raise ValueError("conflicting quantities require an explicit reason")
            for observation in self.observations:
                try:
                    unit = get_unit(observation.unit)
                    if unit.dimension is not self.dimension:
                        raise ValueError(
                            f"conflicting observation unit {observation.unit!r} does not match "
                            f"{self.dimension.value!r}"
                        )
                    convert(observation.value, unit.symbol, canonical_unit(self.dimension))
                except UnitError as exc:
                    raise ValueError(str(exc)) from exc
        return self


class SourceReference(_ProductModel):
    kind: Literal["document", "engine_run"]
    id: str = Field(min_length=1, max_length=64)


class BasisStatement(_ProductModel):
    topic: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=1, max_length=2000)
    basis: Literal["reported", "assumption", "unknown", "conflicting"]
    reason: str | None = Field(default=None, max_length=1000)
    source_references: list[SourceReference] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unresolved_basis_has_reason(self) -> BasisStatement:
        if self.basis != "reported" and not self.reason:
            raise ValueError("assumption, unknown and conflicting basis statements require a reason")
        return self


class ProgramObjective(_ProductModel):
    sequence: int = Field(ge=1, le=100)
    statement: str = Field(min_length=1, max_length=2000)
    target_formation: str | None = Field(default=None, max_length=200)
    target_md: EngineeringQuantity | None = None
    source_references: list[SourceReference] = Field(default_factory=list, max_length=20)


class ProgramOperation(_ProductModel):
    sequence: int = Field(ge=1, le=200)
    activity: str = Field(min_length=1, max_length=300)
    purpose: str = Field(min_length=1, max_length=1200)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=30)
    planned_duration: EngineeringQuantity | None = None
    source_references: list[SourceReference] = Field(default_factory=list, max_length=20)


class ProgramParameter(_ProductModel):
    key: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=200)
    value: EngineeringQuantity
    applicability: str | None = Field(default=None, max_length=500)
    source_references: list[SourceReference] = Field(default_factory=list, max_length=20)


class ProgramSection(_ProductModel):
    sequence: int = Field(ge=1, le=100)
    name: str = Field(min_length=1, max_length=200)
    section_id: str | None = Field(default=None, min_length=1, max_length=64)
    hole_diameter: EngineeringQuantity
    planned_md_from: EngineeringQuantity
    planned_md_to: EngineeringQuantity
    mud_density_min: EngineeringQuantity | None = None
    mud_density_max: EngineeringQuantity | None = None
    operating_parameters: list[ProgramParameter] = Field(default_factory=list, max_length=100)
    planned_operations: list[ProgramOperation] = Field(default_factory=list, max_length=200)
    source_references: list[SourceReference] = Field(default_factory=list, max_length=30)


class ProgramRisk(_ProductModel):
    sequence: int = Field(ge=1, le=500)
    hazard: str = Field(min_length=1, max_length=300)
    initiating_causes: list[str] = Field(default_factory=list, max_length=30)
    consequences: list[str] = Field(default_factory=list, max_length=30)
    safeguards: list[str] = Field(default_factory=list, max_length=30)
    contingency: str | None = Field(default=None, max_length=2000)
    owner_role: str | None = Field(default=None, max_length=120)
    source_references: list[SourceReference] = Field(default_factory=list, max_length=30)


class DrillingProgramPayload(_ProductModel):
    """Versioned structured drilling-program content; an empty model is a draft, not an approved plan."""

    purpose: str = Field(default="", max_length=2000)
    objectives: list[ProgramObjective] = Field(default_factory=list, max_length=100)
    design_basis: list[BasisStatement] = Field(default_factory=list, max_length=200)
    sections: list[ProgramSection] = Field(default_factory=list, max_length=100)
    risks: list[ProgramRisk] = Field(default_factory=list, max_length=500)
    assumptions: list[BasisStatement] = Field(default_factory=list, max_length=200)


class ProcedurePrerequisite(_ProductModel):
    sequence: int = Field(ge=1, le=100)
    requirement: str = Field(min_length=1, max_length=1000)
    verification: str = Field(min_length=1, max_length=1000)
    source_references: list[SourceReference] = Field(default_factory=list, max_length=20)


class ProcedureStep(_ProductModel):
    sequence: int = Field(ge=1, le=200)
    title: str = Field(min_length=1, max_length=200)
    instruction: str = Field(min_length=1, max_length=3000)
    responsible_role: str = Field(min_length=1, max_length=120)
    preconditions: list[str] = Field(default_factory=list, max_length=30)
    hazards: list[str] = Field(default_factory=list, max_length=30)
    control_measures: list[str] = Field(default_factory=list, max_length=30)
    verification_criteria: list[str] = Field(min_length=1, max_length=30)
    response_if_not_met: str = Field(min_length=1, max_length=2000)
    hold_point: bool = False
    source_references: list[SourceReference] = Field(default_factory=list, max_length=20)


class StopWorkCondition(_ProductModel):
    sequence: int = Field(ge=1, le=100)
    trigger: str = Field(min_length=1, max_length=1000)
    immediate_response: str = Field(min_length=1, max_length=2000)
    escalation_role: str = Field(min_length=1, max_length=120)
    source_references: list[SourceReference] = Field(default_factory=list, max_length=20)


class ControlledProcedurePayload(_ProductModel):
    """Structured, document-only procedure content; it has no execution or rig-control surface."""

    purpose: str = Field(default="", max_length=2000)
    scope_statement: str = Field(default="", max_length=2000)
    prerequisites: list[ProcedurePrerequisite] = Field(default_factory=list, max_length=100)
    steps: list[ProcedureStep] = Field(default_factory=list, max_length=200)
    stop_work_conditions: list[StopWorkCondition] = Field(default_factory=list, max_length=100)
    source_references: list[SourceReference] = Field(default_factory=list, max_length=30)


@dataclass(frozen=True)
class PayloadValidationIssue:
    code: str
    severity: Literal["error", "warning", "info"]
    message: str
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "details": self.details,
        }


_PRODUCT_MODELS: dict[str, type[_ProductModel]] = {
    "program": DrillingProgramPayload,
    "procedure": ControlledProcedurePayload,
}


def parse_product_payload(kind: str, payload: Any) -> BaseModel:
    """Validate a user-authored payload without leaking raw submitted values in errors."""
    model = _PRODUCT_MODELS.get(kind)
    if model is None:
        raise ValueError(f"unsupported product kind {kind!r}")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        issues = [
            {
                "path": ".".join(str(part) for part in error.get("loc", ())),
                "code": error.get("type", "invalid"),
                "message": error.get("msg", "invalid value"),
            }
            for error in exc.errors(include_input=False)[:50]
        ]
        raise ProductPayloadError("product payload does not match its versioned schema", issues) from exc


class ProductPayloadError(ValueError):
    def __init__(self, message: str, issues: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.issues = issues


def normalize_payload(
    model: BaseModel,
    *,
    actor_id: str,
    authored_at: dt.datetime,
) -> dict[str, Any]:
    """Retain each entered value/unit and add registry-derived SI normalization and entry provenance."""
    raw = model.model_dump(mode="json", exclude_none=False)

    def normalize(node: Any) -> Any:
        if isinstance(node, list):
            return [normalize(item) for item in node]
        if not isinstance(node, dict):
            return node
        if "state" in node and "dimension" in node:
            dimension = Dimension(node["dimension"])
            if node["state"] == "known":
                unit = get_unit(str(node["unit"]))
                canonical = canonical_unit(dimension)
                normalized = dict(node)
                normalized["value_si"] = convert(float(node["value"]), str(node["unit"]), canonical)
                normalized["canonical_unit"] = canonical
                normalized["unit"] = unit.symbol
                normalized["entered_by"] = actor_id if node.get("source_kind") == "operator_entered" else None
                normalized["entered_at"] = authored_at.isoformat() if node.get("source_kind") == "operator_entered" else None
                return normalized
            if node["state"] == "conflicting":
                normalized = dict(node)
                observations = []
                canonical = canonical_unit(dimension)
                for observation in node.get("observations", []):
                    item = dict(observation)
                    registered = get_unit(str(item["unit"]))
                    item["unit"] = registered.symbol
                    item["value_si"] = convert(float(item["value"]), registered.symbol, canonical)
                    item["canonical_unit"] = canonical
                    item["entered_by"] = actor_id if item.get("source_kind") == "operator_entered" else None
                    item["entered_at"] = authored_at.isoformat() if item.get("source_kind") == "operator_entered" else None
                    observations.append(item)
                normalized["observations"] = observations
                return normalized
        return {key: normalize(value) for key, value in node.items()}

    return normalize(raw)


def _quantity(payload: dict[str, Any], *path: str) -> dict[str, Any] | None:
    value: Any = payload
    for part in path:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit():
            index = int(part)
            value = value[index] if index < len(value) else None
        else:
            return None
    return value if isinstance(value, dict) else None


def _quantity_issue(
    payload: dict[str, Any],
    path: tuple[str, ...],
    expected_dimension: Dimension,
    *,
    required: bool,
    positive: bool = False,
    nonnegative: bool = False,
) -> tuple[dict[str, Any] | None, list[PayloadValidationIssue]]:
    quantity = _quantity(payload, *path)
    dotted = ".".join(path)
    if quantity is None:
        if required:
            return None, [PayloadValidationIssue("required_quantity_missing", "error", "A required quantity is missing.", dotted)]
        return None, []
    issues: list[PayloadValidationIssue] = []
    if quantity.get("dimension") != expected_dimension.value:
        issues.append(
            PayloadValidationIssue(
                "quantity_dimension_mismatch",
                "error",
                f"This field requires the {expected_dimension.value} dimension; no conversion or reinterpretation was applied.",
                dotted,
                {"expected_dimension": expected_dimension.value, "declared_dimension": quantity.get("dimension")},
            )
        )
        return quantity, issues
    if quantity.get("state") != "known" or not isinstance(quantity.get("value_si"), (int, float)):
        if required:
            issues.append(
                PayloadValidationIssue(
                    "required_quantity_not_known",
                    "error",
                    "A required engineering input is unknown or conflicting; retain its state and resolve it before submission.",
                    dotted,
                    {"state": quantity.get("state")},
                )
            )
        else:
            issues.append(
                PayloadValidationIssue(
                    "optional_quantity_not_known",
                    "warning",
                    "An optional quantity is unknown or conflicting and remains unresolved.",
                    dotted,
                    {"state": quantity.get("state")},
                )
            )
        return quantity, issues
    value = float(quantity["value_si"])
    if not math.isfinite(value):
        issues.append(PayloadValidationIssue("quantity_not_finite", "error", "Quantity must be finite.", dotted))
    elif positive and value <= 0:
        issues.append(PayloadValidationIssue("quantity_not_positive", "error", "Quantity must be greater than zero.", dotted))
    elif nonnegative and value < 0:
        issues.append(PayloadValidationIssue("quantity_negative", "error", "Quantity must not be negative.", dotted))
    return quantity, issues


def _sequence_issues(items: list[dict[str, Any]], path: str) -> list[PayloadValidationIssue]:
    if not items:
        return []
    values = [item.get("sequence") for item in items]
    expected = list(range(1, len(items) + 1))
    if values != expected:
        return [
            PayloadValidationIssue(
                "sequence_not_contiguous",
                "error",
                "Sequence values must be unique and contiguous from one; no order was inferred.",
                path,
                {"sequences": values},
            )
        ]
    return []


def product_validation_findings(
    kind: str,
    payload: dict[str, Any],
    *,
    linked_program_state: str | None = None,
    engine_findings: list[dict[str, Any]] | None = None,
) -> list[PayloadValidationIssue]:
    """Run deterministic structure/range rules; never calculates a drilling design or approves it."""
    findings: list[PayloadValidationIssue] = []
    if kind == "program":
        objectives = payload.get("objectives") or []
        sections = payload.get("sections") or []
        if not payload.get("purpose", "").strip():
            findings.append(PayloadValidationIssue("program_purpose_missing", "warning", "Program purpose has not been recorded.", "purpose"))
        if not objectives:
            findings.append(PayloadValidationIssue("program_objectives_missing", "error", "At least one objective is required before submission.", "objectives"))
        findings.extend(_sequence_issues(objectives, "objectives"))
        for index, objective in enumerate(objectives):
            target = objective.get("target_md")
            if target is not None:
                _, issues = _quantity_issue(
                    payload,
                    ("objectives", str(index), "target_md"),
                    Dimension.LENGTH,
                    required=False,
                    nonnegative=True,
                )
                findings.extend(issues)
        if not sections:
            findings.append(PayloadValidationIssue("program_sections_missing", "error", "At least one structured well section is required before submission.", "sections"))
        findings.extend(_sequence_issues(sections, "sections"))
        section_ids = [item.get("section_id") for item in sections if item.get("section_id")]
        if len(section_ids) != len(set(section_ids)):
            findings.append(PayloadValidationIssue("section_identity_repeated", "error", "A well-section identity may appear only once in one program revision.", "sections"))
        findings.extend(_sequence_issues(payload.get("risks") or [], "risks"))
        previous_bottom: float | None = None
        for index, section in enumerate(sections):
            base = ("sections", str(index))
            field_prefix = ".".join(base)
            _, issues = _quantity_issue(payload, (*base, "hole_diameter"), Dimension.LENGTH, required=True, positive=True)
            findings.extend(issues)
            top, issues = _quantity_issue(payload, (*base, "planned_md_from"), Dimension.LENGTH, required=True, nonnegative=True)
            findings.extend(issues)
            bottom, issues = _quantity_issue(payload, (*base, "planned_md_to"), Dimension.LENGTH, required=True, positive=True)
            findings.extend(issues)
            if top and bottom and top.get("state") == bottom.get("state") == "known":
                top_si = top.get("value_si")
                bottom_si = bottom.get("value_si")
                if isinstance(top_si, (int, float)) and isinstance(bottom_si, (int, float)) and bottom_si <= top_si:
                    findings.append(PayloadValidationIssue("section_depth_order_invalid", "error", "Planned bottom measured depth must exceed planned top measured depth.", f"{field_prefix}.planned_md_to"))
                # Sections represent successive hole intervals. Permit coincident boundaries and
                # a 1e-6 m conversion-roundoff tolerance, not an inferred correction of overlaps.
                if (isinstance(top_si, (int, float)) and math.isfinite(top_si)
                        and previous_bottom is not None and top_si < previous_bottom - 1e-6):
                    findings.append(PayloadValidationIssue(
                        "section_depth_overlap", "error",
                        "This hole section starts above the preceding section's bottom; resolve the overlap before submission.",
                        f"{field_prefix}.planned_md_from",
                        {"previous_bottom_m": previous_bottom, "current_top_m": float(top_si), "comparison_tolerance_m": 1e-6},
                    ))
                if isinstance(bottom_si, (int, float)) and previous_bottom is not None and bottom_si < previous_bottom:
                    findings.append(PayloadValidationIssue("section_depth_sequence_decreases", "error", "Section bottom depths must not decrease with sequence.", f"{field_prefix}.planned_md_to"))
                if isinstance(bottom_si, (int, float)) and math.isfinite(bottom_si):
                    previous_bottom = float(bottom_si)
            has_min = section.get("mud_density_min") is not None
            has_max = section.get("mud_density_max") is not None
            if has_min != has_max:
                findings.append(PayloadValidationIssue("mud_density_window_incomplete", "error", "A mud-density range requires both an explicit minimum and maximum; neither is inferred.", field_prefix))
            if has_min and has_max:
                minimum, issues = _quantity_issue(payload, (*base, "mud_density_min"), Dimension.DENSITY, required=True, positive=True)
                findings.extend(issues)
                maximum, issues = _quantity_issue(payload, (*base, "mud_density_max"), Dimension.DENSITY, required=True, positive=True)
                findings.extend(issues)
                if minimum and maximum and minimum.get("state") == maximum.get("state") == "known":
                    low, high = minimum.get("value_si"), maximum.get("value_si")
                    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and low > high:
                        findings.append(PayloadValidationIssue("mud_density_window_reversed", "error", "Mud-density minimum exceeds its maximum.", f"{field_prefix}.mud_density_min"))
            parameters = section.get("operating_parameters") or []
            parameter_keys = [item.get("key") for item in parameters]
            if len(parameter_keys) != len(set(parameter_keys)):
                findings.append(PayloadValidationIssue("operating_parameter_key_repeated", "error", "Operating-parameter keys must be unique within a section.", f"{field_prefix}.operating_parameters"))
            operations = section.get("planned_operations") or []
            findings.extend(_sequence_issues(operations, f"{field_prefix}.planned_operations"))
            for parameter_index, parameter in enumerate(parameters):
                quantity = parameter.get("value") or {}
                if quantity.get("state") == "known":
                    try:
                        dimension = Dimension(quantity["dimension"])
                    except (KeyError, ValueError):
                        findings.append(PayloadValidationIssue("parameter_dimension_unknown", "error", "The operating parameter has no recognized dimension.", f"{field_prefix}.operating_parameters.{parameter_index}.value"))
                        continue
                    _, issues = _quantity_issue(
                        payload,
                        (*base, "operating_parameters", str(parameter_index), "value"),
                        dimension,
                        required=True,
                    )
                    findings.extend(issues)
                else:
                    findings.append(PayloadValidationIssue("parameter_value_unresolved", "warning", "An operating parameter remains explicitly unresolved.", f"{field_prefix}.operating_parameters.{parameter_index}.value", {"state": quantity.get("state")}))
            for operation_index, operation in enumerate(section.get("planned_operations") or []):
                duration = operation.get("planned_duration")
                if duration is not None:
                    _, issues = _quantity_issue(
                        payload,
                        (*base, "planned_operations", str(operation_index), "planned_duration"),
                        Dimension.TIME,
                        required=False,
                        nonnegative=True,
                    )
                    findings.extend(issues)
        for index, statement in enumerate(payload.get("design_basis") or []):
            if statement.get("basis") in {"assumption", "unknown", "conflicting"}:
                findings.append(PayloadValidationIssue("design_basis_not_verified", "warning", "Design basis is marked as an assumption, unknown or conflicting; it is not treated as verified evidence.", f"design_basis.{index}", {"basis": statement.get("basis")}))
            elif statement.get("basis") == "reported" and not statement.get("source_references"):
                findings.append(PayloadValidationIssue("reported_basis_without_source", "warning", "A reported design-basis statement has no source reference and is not treated as independently grounded.", f"design_basis.{index}"))
        for index, _statement in enumerate(payload.get("assumptions") or []):
            findings.append(PayloadValidationIssue("explicit_assumption", "warning", "An explicit assumption remains part of this draft and requires independent engineering review.", f"assumptions.{index}"))
    elif kind == "procedure":
        if not payload.get("purpose", "").strip():
            findings.append(PayloadValidationIssue("procedure_purpose_missing", "warning", "Procedure purpose has not been recorded.", "purpose"))
        if not payload.get("scope_statement", "").strip():
            findings.append(PayloadValidationIssue("procedure_scope_missing", "error", "A scope statement is required before submission.", "scope_statement"))
        prerequisites = payload.get("prerequisites") or []
        if not prerequisites:
            findings.append(PayloadValidationIssue("procedure_prerequisites_missing", "error", "At least one structured prerequisite and verification is required before submission.", "prerequisites"))
        findings.extend(_sequence_issues(prerequisites, "prerequisites"))
        steps = payload.get("steps") or []
        if not steps:
            findings.append(PayloadValidationIssue("procedure_steps_missing", "error", "At least one structured procedure step is required before submission.", "steps"))
        findings.extend(_sequence_issues(steps, "steps"))
        for index, step in enumerate(steps):
            if not step.get("verification_criteria"):
                findings.append(PayloadValidationIssue("step_verification_missing", "error", "Each controlled step requires an explicit verification criterion.", f"steps.{index}.verification_criteria"))
            if step.get("hazards") and not step.get("control_measures"):
                findings.append(PayloadValidationIssue("step_hazard_control_missing", "error", "A step that declares hazards must also declare control measures.", f"steps.{index}.control_measures"))
        conditions = payload.get("stop_work_conditions") or []
        if not conditions:
            findings.append(PayloadValidationIssue("stop_work_conditions_missing", "error", "At least one explicit stop-work condition and human escalation role is required before submission.", "stop_work_conditions"))
        findings.extend(_sequence_issues(conditions, "stop_work_conditions"))
        if linked_program_state is not None and linked_program_state != "approved":
            findings.append(PayloadValidationIssue("linked_program_revision_not_currently_approved", "error", "The exact referenced drilling-program revision is no longer approved; the reference was not rewritten.", "linked_program_version_id", {"referenced_state": linked_program_state}))
    else:
        findings.append(PayloadValidationIssue("unsupported_product_kind", "error", "No deterministic validation rules are registered for this product kind."))

    for finding in engine_findings or []:
        if finding.get("severity") in {"error", "warning"}:
            findings.append(
                PayloadValidationIssue(
                    str(finding.get("rule_key") or "linked_engine_finding"),
                    str(finding["severity"]),
                    str(finding.get("message") or "A linked registered-engine finding requires review."),
                    details=dict(finding.get("details") or {}),
                )
            )
    return findings


def validation_state(findings: list[PayloadValidationIssue]) -> str:
    if any(item.severity == "error" for item in findings):
        return "blocked"
    if any(item.severity == "warning" for item in findings):
        return "warnings"
    return "passed"


def schema_errors(exc: ProductPayloadError) -> list[dict[str, str]]:
    return exc.issues
