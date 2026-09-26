"""Well integrity: barrier-envelope verification.

Implements the two-barrier principle as a deterministic, evidence-aware check (NORSOK D-010
style, phrased in the platform's own model):

* every element carries a kind, a depth interval, a verification state and optional evidence,
* barrier elements are only counted when they are **verified**,
* two elements of the same independence group (e.g. the same casing string counted twice)
  are one barrier, not two,
* the engine walks the well and reports every depth interval where the number of independent
  verified barriers falls below the requirement, plus what is unverified and why.

The engine deliberately returns an advisory assessment only. Deciding to continue operations
with a degraded barrier envelope is an operational decision that belongs to a human with the
full picture — the platform must not automate it (action level L1).
"""

from __future__ import annotations

from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field

from drillai.engines.contract import ConstraintViolation, EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field

VERIFIED_STATES = {"verified"}
ACCEPTABLE_STATES = {"verified", "monitored"}


class BarrierElement(BaseModel):
    """One barrier element declared for the well, with its verification provenance."""

    model_config = ConfigDict(extra="forbid")
    name: str
    kind: str = Field(
        description=(
            "fluid_column|casing|cement|wellhead|bop|packer|plug|tree|annular_seal|formation|barrier_fluid|other"
        )
    )
    from_depth_si: float = quantity_field(0.0, "m", ge=0.0)
    to_depth_si: float = quantity_field(..., "m", gt=0.0)
    status: str = Field(
        default="verified",
        description="planned|installed|verified|monitored|impaired|failed|removed",
    )
    independence_group: str | None = Field(
        default=None,
        description="elements sharing a group cannot be counted as independent barriers (defaults to kind)",
    )
    verification_method: str | None = Field(default=None, description="pressure test|LOT/FIT|inspection|logging|visual|calculation")
    verification_date: str | None = None
    expiry_date: str | None = None
    evidence_ref: str | None = Field(default=None, description="reference to the evidence record (document, test report, log)")
    notes: str | None = None

    @property
    def barrier_group(self) -> str:
        return self.independence_group or self.kind


class IntegrityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    elements: list[BarrierElement] = Field(min_length=1)
    well_depth_si: float = quantity_field(..., "m", gt=0.0, description="deepest depth that must be protected")
    required_barriers: int = Field(default=2, ge=1, le=5, description="barriers required across the envelope")
    minimum_independent_kinds: int = Field(
        default=2,
        ge=1,
        le=5,
        description="minimum number of distinct barrier kinds that must make up the envelope",
    )
    phase: str = Field(default="drilling", description="drilling|completion|production|intervention|abandonment")
    require_evidence: bool = Field(default=True, description="count a verified element without evidence as unverified")


class EnvelopeInterval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_depth_si: float
    to_depth_si: float
    verified_barriers: int
    kinds: list[str]
    shortfall: int


class ElementAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: str
    barrier_group: str
    status: str
    counted: bool
    reason: str
    evidence_ref: str | None = None


class IntegrityOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: str
    required_barriers: int
    verified_barrier_count: int
    barrier_kinds: list[str]
    envelope_complete: bool
    intervals: list[EnvelopeInterval]
    gaps: list[EnvelopeInterval]
    unverified: list[ElementAssessment]
    failed: list[ElementAssessment]
    assessments: list[ElementAssessment]
    expired_verifications: list[str]
    notes: list[str]


def _parse_date(value: str | None):
    import datetime as dt

    if not value:
        return None
    for candidate in (value, value.replace("Z", "+00:00")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
        except ValueError:
            continue
    return None


def compute_integrity(inputs: IntegrityInput) -> EngineResult:
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []
    now = None
    assessments: list[ElementAssessment] = []

    for element in inputs.elements:
        if element.to_depth_si <= element.from_depth_si:
            raise ValueError(f"barrier element {element.name!r} has a non-positive depth interval")

        expired = False
        expiry = _parse_date(element.expiry_date)
        if expiry is not None:
            import datetime as dt

            now = now or dt.datetime.now(dt.UTC)
            expired = expiry < now

        has_evidence = bool(element.evidence_ref) or not inputs.require_evidence
        if element.status == "failed":
            reason = "element reported failed"
            counted = False
        elif element.status in VERIFIED_STATES and not has_evidence:
            reason = "verified but no evidence reference is recorded"
            counted = False
        elif element.status in ACCEPTABLE_STATES:
            reason = "counted as an independent barrier" if element.status == "verified" else "monitored (not independently verified)"
            counted = element.status == "verified"
        else:
            reason = f"status {element.status!r} is not an active verified barrier"
            counted = False
        if expired:
            counted = False
            reason = f"verification expired on {element.expiry_date}"

        assessments.append(
            ElementAssessment(
                name=element.name,
                kind=element.kind,
                barrier_group=element.barrier_group,
                status=element.status,
                counted=counted,
                reason=reason,
                evidence_ref=element.evidence_ref,
            )
        )
        if element.status == "failed":
            violations.append(
                ConstraintViolation(
                    name="barrier_failed",
                    message=f"barrier element {element.name!r} ({element.kind}) is failed",
                    severity="error",
                    source=element.kind,
                )
            )

    counted_elements = [
        element
        for element, assessment in zip(inputs.elements, assessments, strict=True)
        if assessment.counted
    ]

    # Envelope: walk the well from surface to the depth that must be protected and count
    # independent verified barrier groups over each sub-interval.
    boundaries = {0.0, inputs.well_depth_si}
    for element in counted_elements:
        boundaries.add(max(0.0, min(element.from_depth_si, inputs.well_depth_si)))
        boundaries.add(max(0.0, min(element.to_depth_si, inputs.well_depth_si)))
    depths = sorted(depth for depth in boundaries if 0.0 <= depth <= inputs.well_depth_si)

    intervals: list[EnvelopeInterval] = []
    for top, bottom in pairwise(depths):
        if bottom - top <= 0:
            continue
        midpoint = 0.5 * (top + bottom)
        covering = [
            element
            for element in counted_elements
            if element.from_depth_si <= midpoint <= element.to_depth_si
        ]
        groups: dict[str, list[BarrierElement]] = {}
        for element in covering:
            groups.setdefault(element.barrier_group, []).append(element)
        intervals.append(
            EnvelopeInterval(
                from_depth_si=top,
                to_depth_si=bottom,
                verified_barriers=len(groups),
                kinds=sorted({element.kind for element in covering}),
                shortfall=max(0, inputs.required_barriers - len(groups)),
            )
        )

    gaps = [interval for interval in intervals if interval.verified_barriers < inputs.required_barriers]
    kinds_overall = sorted({element.kind for element in counted_elements})
    envelope_complete = not gaps

    if gaps:
        worst = min(gaps, key=lambda interval: interval.verified_barriers)
        violations.append(
            ConstraintViolation(
                name="barrier_envelope_shortfall",
                message=(
                    f"{len(gaps)} interval(s) have fewer than {inputs.required_barriers} independent verified barriers "
                    f"(worst: {worst.from_depth_si:.0f}-{worst.to_depth_si:.0f} m with {worst.verified_barriers})"
                ),
                severity="error",
                limit=float(inputs.required_barriers),
                actual=float(worst.verified_barriers),
                unit="barriers",
                source="barrier envelope",
            )
        )
    if len(kinds_overall) < inputs.minimum_independent_kinds:
        violations.append(
            ConstraintViolation(
                name="barrier_diversity",
                message=(
                    f"envelope is made of {len(kinds_overall)} barrier kind(s) ({', '.join(kinds_overall) or 'none'}); "
                    f"{inputs.minimum_independent_kinds} independent kinds are required"
                ),
                severity="error",
                limit=float(inputs.minimum_independent_kinds),
                actual=float(len(kinds_overall)),
                unit="kinds",
                source="barrier envelope",
            )
        )

    unverified = [assessment for assessment in assessments if not assessment.counted and assessment.status != "failed"]
    failed = [assessment for assessment in assessments if assessment.status == "failed"]
    expired = [
        element.name
        for element in inputs.elements
        if element.expiry_date and _parse_date(element.expiry_date) and element.expiry_date
    ]
    if unverified:
        warnings.append(f"{len(unverified)} element(s) are not counted as verified barriers: review the reasons listed")

    notes = [
        "two-barrier principle: at least the required number of independent, verified barrier elements across the envelope",
        "elements sharing an independence group count once",
        "verification without an evidence reference is treated as unverified (unless require_evidence is off)",
        "assessment is advisory: barrier degradation decisions remain a human operational authority",
    ]
    outputs = IntegrityOutput(
        phase=inputs.phase,
        required_barriers=inputs.required_barriers,
        verified_barrier_count=len(counted_elements),
        barrier_kinds=kinds_overall,
        envelope_complete=envelope_complete,
        intervals=intervals,
        gaps=gaps,
        unverified=unverified,
        failed=failed,
        assessments=assessments,
        expired_verifications=sorted(set(expired)),
        notes=notes,
    )
    return EngineResult(outputs=outputs, warnings=warnings, violations=violations, assumptions_applied=notes[:3])


INTEGRITY_SPEC = EngineSpec(
    key="integrity.barrier_envelope",
    name="Well integrity barrier envelope",
    version="1.1.0",
    domain_pack="Integrity Pack",
    category="integrity",
    summary=(
        "Deterministic two-barrier verification: counts independent verified barrier elements per depth "
        "interval, reports shortfalls, unverified elements, failures and expired verifications."
    ),
    inputs_model=IntegrityInput,
    outputs_model=IntegrityOutput,
    consumes=("well.identity", "wellbore.geometry", "casing.strings", "cement.job", "well.integrity_barriers", "document.evidence"),
    produces=("barriers.state", "well.integrity_barriers"),
    parameters={
        "standard": {"value": "two_barrier (NORSOK D-010 aligned)", "description": "verification principle"},
        "count_unverified": {"value": False, "description": "unverified elements never count towards the envelope"},
    },
    assumptions=(
        "the declared barrier elements and their depth intervals are accurate",
        "independence is expressed through the independence group",
        "a verified element without an evidence reference is treated as unverified",
    ),
    limitations=(
        "does not size barriers against actual pressure loads (use the tubular/cement design engines)",
        "no annular pressure or sustained-casing-pressure diagnostics",
        "no risk-based anomaly trending",
    ),
    references=(
        "NORSOK D-010 — Well integrity in drilling and well operations",
        "API RP 96 — Deepwater well design and integrity (barrier concepts)",
    ),
    tags=("integrity", "barriers", "verification", "safety"),
    action_level="L1",
)

register_engine(FunctionEngine(INTEGRITY_SPEC, compute_integrity), aliases=("integrity", "barriers", "two_barrier"))
