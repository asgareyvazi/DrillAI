"""Offset-well intelligence: similarity scoring, weighted statistics and hazard aggregation.

Offset analysis is the backbone of well planning, and it fails in two ways when it is done
naively: averaging over wells that are not comparable, and hiding why a well was included.
This engine therefore returns, for every candidate:

* a per-factor similarity score with its **contribution** to the total (so the ranking is
  explainable and auditable), 
* a data-coverage figure, with candidates below the minimum coverage **rejected and listed
  with the reason** instead of being silently dropped,
* optional per-well hazards/lessons, aggregated into a frequency profile that names the
  contributing wells (evidence).

All arithmetic is deterministic. This is the structured contract that downstream statistics,
planning and ML models consume — the platform never asks a language model to rank wells.
"""

from __future__ import annotations

import datetime as dt
import statistics

from pydantic import BaseModel, ConfigDict, Field

from drillai.engines.contract import ConstraintViolation, EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field

FACTORS = ("depth", "hole_size", "mud_weight", "inclination", "formation", "recency")


class WellAttributes(BaseModel):
    """The comparable attributes of one well (reference or candidate)."""

    model_config = ConfigDict(extra="forbid")
    well_id: str
    well_name: str | None = None
    tvd_si: float | None = quantity_field(None, "m", gt=0.0)
    md_si: float | None = quantity_field(None, "m", gt=0.0)
    hole_size_si: float | None = quantity_field(None, "m", gt=0.0)
    mud_weight_si: float | None = quantity_field(None, "kg/m3", gt=0.0)
    max_inclination_deg: float | None = quantity_field(None, "deg", ge=0.0, le=180.0)
    formations: list[str] = Field(default_factory=list)
    spud_date: str | None = None
    completion_type: str | None = None
    exclude: bool = Field(default=False, description="explicitly excluded from the analysis")
    exclude_reason: str | None = None
    data_source: str | None = Field(default=None, description="where the attributes came from (report, database, DDR)")
    evidence_refs: list[str] = Field(default_factory=list)


class OffsetLesson(BaseModel):
    model_config = ConfigDict(extra="forbid")
    well_id: str
    category: str = Field(description="lost_circulation|stuck_pipe|kick|wellbore_instability|equipment|hse|other")
    description: str
    severity: str = Field(default="medium", description="low|medium|high")
    depth_si: float | None = quantity_field(None, "m", ge=0.0)
    evidence_ref: str | None = None


class OffsetTolerances(BaseModel):
    """Half-width of the similarity window per factor: the difference at which a score hits zero."""

    model_config = ConfigDict(extra="forbid")
    depth_si: float = quantity_field(500.0, "m", gt=0.0)
    hole_size_si: float = quantity_field(0.0508, "m", gt=0.0, description="2 inch window")
    mud_weight_si: float = quantity_field(150.0, "kg/m3", gt=0.0)
    inclination_deg: float = quantity_field(20.0, "deg", gt=0.0)
    recency_years: float = quantity_field(10.0, "a", gt=0.0, description="age at which the recency score reaches zero")


class OffsetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: WellAttributes
    candidates: list[WellAttributes] = Field(min_length=1)
    lessons: list[OffsetLesson] = Field(default_factory=list)
    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "depth": 0.30,
            "hole_size": 0.20,
            "mud_weight": 0.15,
            "inclination": 0.15,
            "formation": 0.10,
            "recency": 0.10,
        },
        description="relative importance per factor; normalised internally, omitted factors are ignored",
    )
    tolerances: OffsetTolerances = Field(default_factory=OffsetTolerances)
    minimum_coverage: float = quantity_field(
        0.6, "1", ge=0.0, le=1.0, description="fraction of the weighted factors that must be available"
    )
    as_of: str | None = Field(default=None, description="ISO date used for recency; defaults to today")
    statistics_metrics: list[str] = Field(
        default_factory=lambda: ["tvd_si", "md_si", "mud_weight_si", "max_inclination_deg"]
    )


class FactorScore(BaseModel):
    model_config = ConfigDict(extra="forbid")
    factor: str
    score: float | None
    weight: float
    contribution: float | None
    reference_value: float | str | None = None
    candidate_value: float | str | None = None
    detail: str | None = None


class OffsetCandidateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    well_id: str
    well_name: str | None = None
    rank: int | None = None
    score: float | None = None
    coverage: float
    included: bool
    exclusion_reason: str | None = None
    factors: list[FactorScore]
    data_source: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


class OffsetStatistics(BaseModel):
    model_config = ConfigDict(extra="forbid")
    metric: str
    count: int
    weighted_mean: float
    median: float
    minimum: float
    maximum: float
    p10: float
    p90: float
    unit: str


class HazardProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    occurrences: int
    severity: dict[str, int]
    wells: list[str]
    example: str | None = None


class OffsetOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ranked: list[OffsetCandidateResult]
    rejected: list[OffsetCandidateResult]
    statistics: list[OffsetStatistics]
    hazards: list[HazardProfile]
    lessons_considered: int
    normalized_weights: dict[str, float]
    notes: list[str]


def _age_years(value: str | None, as_of: dt.datetime) -> float | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return (as_of - parsed).days / 365.25


class _FactorCollector:
    """Accumulates factor scores for one candidate.

    Factor weights come from the normalized input weights; a factor is only scored when the
    candidate actually carries the underlying value, so coverage stays honest.
    """

    def __init__(self, normalized_weights: dict[str, float]) -> None:
        self.normalized_weights = normalized_weights
        self.factors: list[FactorScore] = []
        self.available_weight = 0.0
        self.weighted_score = 0.0

    def add(
        self,
        factor: str,
        score: float | None,
        reference_value: float | str | None,
        candidate_value: float | str | None,
        detail: str | None = None,
    ) -> None:
        weight = self.normalized_weights.get(factor, 0.0)
        if weight <= 0:
            return
        contribution = None if score is None else weight * score
        if score is not None:
            self.available_weight += weight
            self.weighted_score += contribution or 0.0
        self.factors.append(
            FactorScore(
                factor=factor,
                score=score,
                weight=weight,
                contribution=contribution,
                reference_value=reference_value,
                candidate_value=candidate_value,
                detail=detail,
            )
        )


def _window_score(difference: float, tolerance: float) -> float:
    """1.0 for an exact match, decreasing linearly to 0 at the tolerance."""
    if tolerance <= 0:
        return 1.0 if difference == 0 else 0.0
    return max(0.0, 1.0 - difference / tolerance)


def _formation_similarity(reference: list[str], candidate: list[str]) -> tuple[float | None, str]:
    """Jaccard overlap of formation names, reported with the original spelling."""
    reference_lookup = {name.strip().lower(): name.strip() for name in reference if name.strip()}
    candidate_set = {name.strip().lower() for name in candidate if name.strip()}
    if not reference_lookup or not candidate_set:
        return None, "formation lists missing"
    reference_set = set(reference_lookup)
    union = reference_set | candidate_set
    if not union:
        return None, "formation lists empty"
    jaccard = len(reference_set & candidate_set) / len(union)
    missing = sorted(reference_lookup[name] for name in reference_set - candidate_set)
    detail = f"Jaccard {jaccard:.2f}"
    if missing:
        detail += f"; reference formations absent in candidate: {', '.join(missing)}"
    return jaccard, detail


def compute_offsets(inputs: OffsetInput) -> EngineResult:
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []

    tolerance = inputs.tolerances
    as_of = dt.datetime.now(dt.UTC)
    if inputs.as_of:
        as_of = dt.datetime.fromisoformat(inputs.as_of.replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=dt.UTC)

    weights = {factor: weight for factor, weight in inputs.weights.items() if factor in FACTORS and weight > 0}
    unknown = sorted(set(inputs.weights) - set(FACTORS))
    if unknown:
        warnings.append(f"unknown weight factors ignored: {', '.join(unknown)}")
    if not weights:
        raise ValueError("no usable similarity weights were provided")
    total_weight = sum(weights.values())
    normalized = {factor: weight / total_weight for factor, weight in sorted(weights.items())}

    reference_age = _age_years(inputs.reference.spud_date, as_of)
    results: list[OffsetCandidateResult] = []

    for candidate in inputs.candidates:
        collector = _FactorCollector(normalized)

        collector.add(
            "depth",
            _window_score(abs((candidate.tvd_si or 0.0) - (inputs.reference.tvd_si or 0.0)), tolerance.depth_si)
            if candidate.tvd_si and inputs.reference.tvd_si
            else None,
            inputs.reference.tvd_si,
            candidate.tvd_si,
        )
        collector.add(
            "hole_size",
            _window_score(abs((candidate.hole_size_si or 0.0) - (inputs.reference.hole_size_si or 0.0)), tolerance.hole_size_si)
            if candidate.hole_size_si and inputs.reference.hole_size_si
            else None,
            inputs.reference.hole_size_si,
            candidate.hole_size_si,
            "hole size mismatch changes hydraulics, bit selection and casing programme",
        )
        collector.add(
            "mud_weight",
            _window_score(abs((candidate.mud_weight_si or 0.0) - (inputs.reference.mud_weight_si or 0.0)), tolerance.mud_weight_si)
            if candidate.mud_weight_si and inputs.reference.mud_weight_si
            else None,
            inputs.reference.mud_weight_si,
            candidate.mud_weight_si,
        )
        collector.add(
            "inclination",
            _window_score(
                abs((candidate.max_inclination_deg or 0.0) - (inputs.reference.max_inclination_deg or 0.0)),
                tolerance.inclination_deg,
            )
            if candidate.max_inclination_deg is not None and inputs.reference.max_inclination_deg is not None
            else None,
            inputs.reference.max_inclination_deg,
            candidate.max_inclination_deg,
        )
        formation_score, formation_detail = _formation_similarity(
            inputs.reference.formations, candidate.formations
        )
        collector.add("formation", formation_score, None, None, formation_detail)
        candidate_age = _age_years(candidate.spud_date, as_of)
        if candidate_age is not None and reference_age is not None:
            difference = abs(candidate_age - reference_age)
            collector.add(
                "recency",
                _window_score(difference, tolerance.recency_years),
                inputs.reference.spud_date,
                candidate.spud_date,
                f"{difference:.1f} year(s) apart",
            )
        else:
            collector.add("recency", None, inputs.reference.spud_date, candidate.spud_date, "spud date missing")

        factors = collector.factors
        coverage = collector.available_weight  # normalized weights sum to 1
        score = collector.weighted_score / collector.available_weight if collector.available_weight > 0 else None
        reason = None
        included = True
        if candidate.exclude:
            included = False
            reason = candidate.exclude_reason or "excluded by the analyst"
        elif coverage < inputs.minimum_coverage:
            included = False
            reason = (
                f"data coverage {coverage:.0%} is below the {inputs.minimum_coverage:.0%} minimum "
                f"(missing: {', '.join(f.factor for f in factors if f.score is None)})"
            )

        results.append(
            OffsetCandidateResult(
                well_id=candidate.well_id,
                well_name=candidate.well_name,
                score=score,
                coverage=coverage,
                included=included,
                exclusion_reason=reason,
                factors=factors,
                data_source=candidate.data_source,
                evidence_refs=candidate.evidence_refs,
            )
        )

    ranked = sorted(
        (result for result in results if result.included),
        key=lambda result: (-(result.score or 0.0), result.well_id),
    )
    for position, result in enumerate(ranked, start=1):
        result.rank = position
    rejected = [result for result in results if not result.included]

    # Weighted statistics over the ranked offsets: similarity is the weight, so a closer well
    # contributes more than a marginal one — and the weighting is visible in normalized_weights.
    included_ids = {result.well_id for result in ranked}
    candidates_by_id = {candidate.well_id: candidate for candidate in inputs.candidates}
    statistics_out: list[OffsetStatistics] = []
    for metric in inputs.statistics_metrics:
        values: list[tuple[float, float]] = []
        for result in ranked:
            candidate = candidates_by_id[result.well_id]
            value = getattr(candidate, metric, None)
            if value is None:
                continue
            values.append((float(value), result.score or 0.0))
        if not values:
            continue
        numbers = sorted(value for value, _ in values)
        weight_sum = sum(weight for _, weight in values) or float(len(values))
        weighted_mean = sum(value * weight for value, weight in values) / weight_sum
        statistics_out.append(
            OffsetStatistics(
                metric=metric,
                count=len(numbers),
                weighted_mean=weighted_mean,
                median=statistics.median(numbers),
                minimum=numbers[0],
                maximum=numbers[-1],
                p10=_percentile(numbers, 0.10),
                p90=_percentile(numbers, 0.90),
                unit=_metric_unit(metric),
            )
        )

    hazards = _hazard_profile(inputs.lessons, included_ids)
    notes = [
        "similarity is a weighted, linear-window comparison over declared factors — no learned model, no LLM",
        "candidates below the minimum data coverage are rejected and reported with the reason",
        "statistics are weighted by similarity score, not simple averages",
        "hazard profile counts only lessons from the ranked offsets and names the contributing wells",
    ]
    if rejected:
        warnings.append(f"{len(rejected)} candidate(s) were rejected: review the reasons before planning")
    if not hazards and inputs.lessons:
        warnings.append("lessons were supplied but none belong to the ranked offsets")
    if any(result.coverage < 0.8 for result in ranked):
        violations.append(
            ConstraintViolation(
                name="offset_data_coverage",
                message="some ranked offsets have less than 80% factor coverage: their scores are less certain",
                severity="warning",
                limit=0.8,
                actual=min(result.coverage for result in ranked),
                unit="fraction",
                source="offset similarity",
            )
        )

    outputs = OffsetOutput(
        ranked=ranked,
        rejected=rejected,
        statistics=statistics_out,
        hazards=hazards,
        lessons_considered=len(inputs.lessons),
        normalized_weights=normalized,
        notes=notes,
    )
    return EngineResult(outputs=outputs, warnings=warnings, violations=violations, assumptions_applied=notes[:3])


def _percentile(sorted_values: list[float], fraction: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def _metric_unit(metric: str) -> str:
    return {
        "tvd_si": "m",
        "md_si": "m",
        "mud_weight_si": "kg/m3",
        "max_inclination_deg": "deg",
    }.get(metric, "")


def _hazard_profile(lessons: list[OffsetLesson], included_wells: set[str]) -> list[HazardProfile]:
    grouped: dict[str, list[OffsetLesson]] = {}
    for lesson in lessons:
        if lesson.well_id not in included_wells:
            continue
        grouped.setdefault(lesson.category, []).append(lesson)
    profiles = []
    for category, items in grouped.items():
        severity_counts: dict[str, int] = {}
        for item in items:
            severity_counts[item.severity] = severity_counts.get(item.severity, 0) + 1
        profiles.append(
            HazardProfile(
                category=category,
                occurrences=len(items),
                severity=dict(sorted(severity_counts.items())),
                wells=sorted({item.well_id for item in items}),
                example=next((item.description for item in items if item.severity == "high"), items[0].description),
            )
        )
    return sorted(profiles, key=lambda profile: (-profile.occurrences, profile.category))


OFFSET_SPEC = EngineSpec(
    key="offsets.similarity",
    name="Offset well similarity and statistics",
    version="1.1.0",
    domain_pack="Offset Intelligence Pack",
    category="offsets",
    summary=(
        "Explainable offset-well ranking: weighted per-factor similarity with contributions, data-coverage "
        "rejection, similarity-weighted statistics and a hazard profile drawn from offset lessons."
    ),
    inputs_model=OffsetInput,
    outputs_model=OffsetOutput,
    consumes=("well.identity", "trajectory.summary", "formation.markers", "mud.properties", "lessons.learned", "drilling.bit_record"),
    produces=("offset.candidates", "offset.statistics", "offset.lessons"),
    parameters={
        "factors": {"value": list(FACTORS), "description": "similarity factors available to weight"},
        "similarity": {"value": "linear_window", "description": "score = max(0, 1 − |Δ|/tolerance)"},
        "statistics_weighting": {"value": "similarity_score", "description": "weights used for the statistics"},
    },
    assumptions=(
        "well attributes are comparable and consistently reported (same datum and definitions)",
        "the analyst choose the weights, tolerances and the minimum coverage",
        "formation names refer to the same stratigraphic nomenclature across wells",
    ),
    limitations=(
        "no learned similarity (geological or trajectory embedding) — factors are declared and inspectable",
        "no automatic outlier removal beyond the explicit exclusions and coverage rule",
        "hazard aggregation is descriptive; it does not compute probabilities",
    ),
    references=(
        "SPE ATCE 2020 — Smart Custom Well Design Based On Automated Offset Well Analysis",
        "IADC/SPE offset-analysis workflows (candidate selection, hazard compilation by depth)",
    ),
    tags=("offsets", "planning", "similarity", "statistics"),
    action_level="L1",
)

register_engine(FunctionEngine(OFFSET_SPEC, compute_offsets), aliases=("offsets", "offset_analysis"))
