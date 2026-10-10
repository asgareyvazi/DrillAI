"""Optimization: candidate generation, constraint filtering, Pareto ranking, sensitivity.

Two engines, because the two jobs are genuinely different:

``optimization.sweep``    generate a deterministic design: a full-factorial grid or a Halton
                          (low-discrepancy) sample over declared parameter ranges. No RNG, no
                          hidden seed — the same request produces the same candidates, which is
                          what makes an optimization run reviewable.
``optimization.pareto``   given evaluated candidates, apply the constraints, compute the Pareto
                          frontier, rank the feasible set against the objective weights, report
                          the trade-offs between objectives and the sensitivity of each objective
                          to each parameter.

Neither engine evaluates the physics: the caller runs the engineering engines. That separation is
deliberate — the optimizer must never invent an objective value, and the engines must never
optimize.

Explanations are data, not prose: the frontier, the dominating candidate, the trade-off table and
the rank correlations are all returned as structured fields so a recommendation can cite them.
"""

from __future__ import annotations

import math
from itertools import product

from pydantic import BaseModel, ConfigDict, Field

from drillai.engines.contract import ConstraintViolation, EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field

SWEEP_METHODS = {"grid", "halton"}
_PRIMES = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71)


# --------------------------------------------------------------------------- sweep


class SweepParameter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    min_si: float = quantity_field(0.0, "1", description="lower bound in canonical SI units")
    max_si: float = quantity_field(1.0, "1", description="upper bound in canonical SI units")
    unit: str = Field(default="", description="display unit for the parameter")
    steps: int = Field(default=5, ge=2, le=50, description="levels per parameter (grid method)")
    scale: str = Field(default="linear", pattern="^(linear|log)$")
    fixed: float | None = quantity_field(None, "1", description="pin the parameter to a single value")


class SweepInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parameters: list[SweepParameter] = Field(min_length=1, max_length=8)
    method: str = Field(default="grid", description="grid|halton")
    samples: int = Field(default=64, ge=2, le=2000, description="sample count for the Halton design")
    max_candidates: int = Field(default=500, ge=2, le=5000, description="hard cap on generated candidates")


class SweepCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str
    values: dict[str, float]
    index: int


class SweepOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str
    candidates: list[SweepCandidate]
    candidate_count: int
    parameter_levels: dict[str, list[float]]
    truncated: bool
    notes: list[str] = Field(default_factory=list)


def _levels(parameter: SweepParameter) -> list[float]:
    if parameter.fixed is not None:
        return [parameter.fixed]
    if parameter.steps == 1:
        return [parameter.min_si]
    if parameter.scale == "log":
        if parameter.min_si <= 0 or parameter.max_si <= 0:
            raise ValueError(f"parameter {parameter.name}: a log scale requires positive bounds")
        low, high = math.log(parameter.min_si), math.log(parameter.max_si)
        return [math.exp(low + (high - low) * index / (parameter.steps - 1)) for index in range(parameter.steps)]
    return [
        parameter.min_si + (parameter.max_si - parameter.min_si) * index / (parameter.steps - 1)
        for index in range(parameter.steps)
    ]


def _halton(index: int, base: int) -> float:
    result = 0.0
    fraction = 1.0
    current = index
    while current > 0:
        fraction /= base
        result += fraction * (current % base)
        current //= base
    return result


def compute_sweep(inputs: SweepInput) -> EngineResult:
    if inputs.method not in SWEEP_METHODS:
        return EngineResult(
            outputs=SweepOutput(method=inputs.method, candidates=[], candidate_count=0, parameter_levels={}, truncated=False),
            violations=[
                ConstraintViolation(
                    name="sweep_unknown_method",
                    message=f"method {inputs.method!r} is not one of {sorted(SWEEP_METHODS)}",
                    severity="error",
                    source="sweep design",
                )
            ],
        )
    names = [parameter.name for parameter in inputs.parameters]
    if len(set(names)) != len(names):
        return EngineResult(
            outputs=SweepOutput(method=inputs.method, candidates=[], candidate_count=0, parameter_levels={}, truncated=False),
            violations=[
                ConstraintViolation(
                    name="sweep_duplicate_parameter",
                    message=f"duplicate parameter names: {sorted({name for name in names if names.count(name) > 1})}",
                    severity="error",
                    source="sweep design",
                )
            ],
        )

    warnings: list[str] = []
    notes: list[str] = []
    truncated = False
    parameter_levels: dict[str, list[float]] = {}
    try:
        levels = [_levels(parameter) for parameter in inputs.parameters]
    except ValueError as exc:
        return EngineResult(
            outputs=SweepOutput(method=inputs.method, candidates=[], candidate_count=0, parameter_levels={}, truncated=False),
            violations=[
                ConstraintViolation(
                    name="sweep_invalid_range", message=str(exc), severity="error", source="sweep design"
                )
            ],
        )
    for parameter, values in zip(inputs.parameters, levels, strict=True):
        parameter_levels[parameter.name] = values

    if inputs.method == "grid":
        plan = [len(values) for values in levels]
        total = math.prod(plan)
        if total > inputs.max_candidates:
            truncated = True
            warnings.append(
                f"grid would produce {total} combinations, more than max_candidates={inputs.max_candidates}; "
                "the design was truncated. Use method='halton' for a large parameter space."
            )
        candidates: list[SweepCandidate] = []
        for index, combination in enumerate(product(*levels)):
            if index >= inputs.max_candidates:
                break
            candidates.append(
                SweepCandidate(
                    candidate_id=f"c{index:04d}",
                    values=dict(zip(names, combination, strict=True)),
                    index=index,
                )
            )
        notes.append(f"full factorial design over {len(names)} parameter(s): {total} nominal combinations")
    else:
        candidate_count = min(inputs.samples, inputs.max_candidates)
        if candidate_count < inputs.samples:
            truncated = True
            warnings.append(f"requested {inputs.samples} samples but max_candidates is {inputs.max_candidates}")
        candidates = []
        for index in range(candidate_count):
            values = {}
            for dimension, parameter in enumerate(inputs.parameters):
                base = _PRIMES[dimension % len(_PRIMES)]
                fraction = _halton(index + 1, base)
                low = levels[dimension][0]
                high = levels[dimension][-1]
                if parameter.scale == "log":
                    value = math.exp(math.log(low) + (math.log(high) - math.log(low)) * fraction)
                else:
                    value = low + (high - low) * fraction
                values[parameter.name] = value
            candidates.append(SweepCandidate(candidate_id=f"c{index:04d}", values=values, index=index))
        notes.append(
            f"Halton low-discrepancy design with {candidate_count} samples over {len(names)} parameter(s); "
            "deterministic (no random seed)"
        )

    return EngineResult(
        outputs=SweepOutput(
            method=inputs.method,
            candidates=candidates,
            candidate_count=len(candidates),
            parameter_levels=parameter_levels,
            truncated=truncated,
            notes=notes,
        ),
        warnings=warnings,
        assumptions_applied=[
            "parameter ranges are supplied by the caller; the engine does not widen them",
            "the design is deterministic: methods are grid and Halton only",
        ],
        limitations=(
            "no adaptive refinement or surrogate model",
            "no correlation structure between parameters (the design is orthogonal by construction)",
        ),
    )


SWEEP_SPEC = EngineSpec(
    key="optimization.sweep",
    name="Design of experiments sweep",
    version="1.1.0",
    domain_pack="Optimization Pack",
    category="optimization",
    summary="Generate a deterministic grid or Halton candidate design over declared parameter ranges.",
    inputs_model=SweepInput,
    outputs_model=SweepOutput,
    consumes=("optimization.problem",),
    produces=("optimization.candidates",),
    parameters={
        "methods": {"value": sorted(SWEEP_METHODS), "description": "grid (full factorial) and halton (low discrepancy)"},
        "max_candidates": {"value": 500, "description": "hard cap; exceeding it truncates and warns"},
    },
    assumptions=("parameters are independent", "ranges are physically admissible as supplied"),
    limitations=("no surrogate modelling", "no adaptive sampling"),
    references=("Halton low-discrepancy sequences (deterministic design of experiments)",),
    tags=("optimization", "doe", "candidates"),
    action_level="L1",
)

register_engine(FunctionEngine(SWEEP_SPEC, compute_sweep), aliases=("sweep", "doe"))


# --------------------------------------------------------------------------- pareto


class ObjectiveSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1)
    sense: str = Field(default="minimize", pattern="^(minimize|maximize)$")
    weight: float = quantity_field(1.0, "1", gt=0.0, description="relative importance of this objective in the weighted ranking")
    unit: str = Field(default="")
    required: bool = Field(default=True, description="when True, candidates missing this objective are infeasible")


class CandidateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str
    values: dict[str, float] = Field(default_factory=dict)
    objectives: dict[str, float] = Field(default_factory=dict)
    uncertainty: dict[str, float] = Field(default_factory=dict, description="± band per objective key")
    violations: list[str] = Field(default_factory=list, description="constraint descriptions already violated")
    feasible: bool | None = Field(default=None, description="explicit feasibility flag from the caller")


class ParetoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[CandidateSpec] = Field(min_length=1)
    objectives: list[ObjectiveSpec] = Field(min_length=1)
    robust: bool = Field(default=False, description="penalise ranking by the declared uncertainty bands")
    top_n: int = Field(default=10, ge=1, le=100)
    parameter_names: list[str] = Field(default_factory=list, description="restrict sensitivity analysis to these parameters")


class RankedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str
    rank: int
    score: float = Field(description="weighted objective score in [0, 1]; higher is better")
    objective_scores: dict[str, float]
    on_frontier: bool
    dominated_by: list[str] = Field(default_factory=list)
    robust_score: float | None = None


class TradeOff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str
    sense: str
    best_candidate_id: str
    best_value: float
    worst_value: float
    spread: float


class Sensitivity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parameter: str
    objective: str
    spearman_rho: float
    samples: int
    interpretation: str


class ParetoOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    feasible_count: int
    infeasible_count: int
    pareto_frontier: list[str]
    ranking: list[RankedCandidate]
    trade_offs: list[TradeOff]
    sensitivities: list[Sensitivity]
    feasible_ranges: dict[str, dict[str, float]]
    explanation: list[str]
    notes: list[str] = Field(default_factory=list)


def _feasible(candidate: CandidateSpec, objectives: list[ObjectiveSpec]) -> tuple[bool, str | None]:
    if candidate.feasible is False:
        return False, "marked infeasible by the caller"
    if candidate.violations:
        return False, "; ".join(candidate.violations)
    for objective in objectives:
        if objective.required and objective.key not in candidate.objectives:
            return False, f"objective {objective.key!r} is missing"
        value = candidate.objectives.get(objective.key)
        if value is not None and not math.isfinite(value):
            return False, f"objective {objective.key!r} is not a finite number"
    return True, None


def _dominates(left: CandidateSpec, right: CandidateSpec, objectives: list[ObjectiveSpec]) -> bool:
    strictly_better = False
    for objective in objectives:
        left_value = left.objectives.get(objective.key)
        right_value = right.objectives.get(objective.key)
        if left_value is None or right_value is None:
            continue
        if objective.sense == "minimize":
            if left_value > right_value:
                return False
            if left_value < right_value:
                strictly_better = True
        else:
            if left_value < right_value:
                return False
            if left_value > right_value:
                strictly_better = True
    return strictly_better


def _rank_map(values: list[float]) -> list[float]:
    """Average ranks (1 = smallest), ties share the mean rank."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        end = position
        while end + 1 < len(order) and values[order[end + 1]] == values[order[position]]:
            end += 1
        average = (position + end) / 2 + 1
        for index in range(position, end + 1):
            ranks[order[index]] = average
        position = end + 1
    return ranks


def _spearman(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3:
        return None
    left_ranks = _rank_map(left)
    right_ranks = _rank_map(right)
    mean_left = sum(left_ranks) / len(left_ranks)
    mean_right = sum(right_ranks) / len(right_ranks)
    numerator = sum((a - mean_left) * (b - mean_right) for a, b in zip(left_ranks, right_ranks, strict=True))
    denominator = math.sqrt(
        sum((a - mean_left) ** 2 for a in left_ranks) * sum((b - mean_right) ** 2 for b in right_ranks)
    )
    if denominator == 0:
        return None
    return numerator / denominator


def compute_pareto(inputs: ParetoInput) -> EngineResult:
    notes: list[str] = []
    warnings: list[str] = []
    infeasible: list[tuple[CandidateSpec, str]] = []
    feasible: list[CandidateSpec] = []
    for candidate in inputs.candidates:
        ok, reason = _feasible(candidate, inputs.objectives)
        if ok:
            feasible.append(candidate)
        else:
            infeasible.append((candidate, reason or "infeasible"))

    for _candidate, reason in infeasible:
        if "missing" in reason and "objective" in reason:
            warnings.append(f"candidate excluded: {reason}")
            break

    if not feasible:
        return EngineResult(
            outputs=ParetoOutput(
                feasible_count=0,
                infeasible_count=len(inputs.candidates),
                pareto_frontier=[],
                ranking=[],
                trade_offs=[],
                sensitivities=[],
                feasible_ranges={},
                explanation=["no candidate satisfies all constraints; nothing can be recommended"],
                notes=[reason for _candidate, reason in infeasible[:10]],
            ),
            warnings=warnings,
            violations=[
                ConstraintViolation(
                    name="no_feasible_candidate",
                    message=f"0 of {len(inputs.candidates)} candidates are feasible",
                    severity="error",
                    limit=float(len(inputs.candidates)),
                    actual=0.0,
                    unit="count",
                    source="pareto filter",
                )
            ],
        )

    # --- Pareto frontier -----------------------------------------------------
    frontier_ids: list[str] = []
    dominated_by: dict[str, list[str]] = {candidate.candidate_id: [] for candidate in feasible}
    for candidate in feasible:
        dominators = [
            other.candidate_id
            for other in feasible
            if other.candidate_id != candidate.candidate_id and _dominates(other, candidate, inputs.objectives)
        ]
        dominated_by[candidate.candidate_id] = dominators
        if not dominators:
            frontier_ids.append(candidate.candidate_id)

    # --- min-max normalisation and weighted score ----------------------------
    spans: dict[str, tuple[float, float]] = {}
    for objective in inputs.objectives:
        values = [candidate.objectives[objective.key] for candidate in feasible if objective.key in candidate.objectives]
        spans[objective.key] = (min(values), max(values))
    objective_scores: dict[str, dict[str, float]] = {}
    total_weight = sum(objective.weight for objective in inputs.objectives)
    for candidate in feasible:
        scores: dict[str, float] = {}
        for objective in inputs.objectives:
            low, high = spans[objective.key]
            value = candidate.objectives.get(objective.key)
            if value is None or high == low:
                # A degenerate objective carries no information; a neutral 0.5 is applied and
                # the fact is reported rather than silently ranking by the other objectives.
                scores[objective.key] = 0.5
                continue
            fraction = (value - low) / (high - low)
            scores[objective.key] = 1.0 - fraction if objective.sense == "minimize" else fraction
        objective_scores[candidate.candidate_id] = scores

    def weighted(candidate: CandidateSpec) -> float:
        return sum(objective_scores[candidate.candidate_id][objective.key] * objective.weight for objective in inputs.objectives) / total_weight

    def robust(candidate: CandidateSpec) -> float | None:
        if not inputs.robust:
            return None
        total = 0.0
        for objective in inputs.objectives:
            value = candidate.objectives.get(objective.key)
            if value is None:
                return None
            band = abs(candidate.uncertainty.get(objective.key, 0.0))
            low, high = spans[objective.key]
            if high == low:
                score = 0.5
            else:
                worst = value + band if objective.sense == "minimize" else value - band
                fraction = (worst - low) / (high - low)
                score = min(1.0, max(0.0, 1.0 - fraction if objective.sense == "minimize" else fraction))
            total += score * objective.weight
        return total / total_weight

    ordered = sorted(feasible, key=lambda candidate: (-(robust(candidate) if inputs.robust else weighted(candidate)), candidate.candidate_id))
    ranking = [
        RankedCandidate(
            candidate_id=candidate.candidate_id,
            rank=position,
            score=round(weighted(candidate), 6),
            objective_scores={key: round(value, 6) for key, value in objective_scores[candidate.candidate_id].items()},
            on_frontier=candidate.candidate_id in frontier_ids,
            dominated_by=dominated_by[candidate.candidate_id],
            robust_score=None if robust(candidate) is None else round(robust(candidate), 6),
        )
        for position, candidate in enumerate(ordered, start=1)
    ]

    # --- trade-offs ----------------------------------------------------------
    trade_offs: list[TradeOff] = []
    for objective in inputs.objectives:
        values = [
            (candidate.candidate_id, candidate.objectives[objective.key])
            for candidate in feasible
            if objective.key in candidate.objectives
        ]
        if not values:
            continue
        reverse = objective.sense == "maximize"
        values.sort(key=lambda item: item[1], reverse=reverse)
        trade_offs.append(
            TradeOff(
                objective=objective.key,
                sense=objective.sense,
                best_candidate_id=values[0][0],
                best_value=values[0][1],
                worst_value=values[-1][1],
                spread=abs(values[-1][1] - values[0][1]),
            )
        )

    # --- sensitivity ---------------------------------------------------------
    parameter_names = inputs.parameter_names or sorted({name for candidate in feasible for name in candidate.values})
    sensitivities: list[Sensitivity] = []
    if len(feasible) >= 3:
        for name in parameter_names:
            parameter_values = [candidate.values[name] for candidate in feasible if name in candidate.values]
            if len(parameter_values) < 3 or len(set(parameter_values)) < 2:
                continue
            for objective in inputs.objectives:
                pairs = [
                    (candidate.values[name], candidate.objectives[objective.key])
                    for candidate in feasible
                    if name in candidate.values and objective.key in candidate.objectives
                ]
                if len(pairs) < 3 or len({value for value, _ in pairs}) < 2:
                    continue
                rho = _spearman([value for value, _ in pairs], [value for _, value in pairs])
                if rho is None:
                    continue
                direction = "increases" if rho > 0 else "decreases"
                strength = "strong" if abs(rho) >= 0.6 else ("moderate" if abs(rho) >= 0.3 else "weak")
                sensitivities.append(
                    Sensitivity(
                        parameter=name,
                        objective=objective.key,
                        spearman_rho=round(rho, 4),
                        samples=len(pairs),
                        interpretation=(
                            f"{strength} rank correlation: as {name} rises, {objective.key} {direction} "
                            f"(ρ={rho:+.2f}, n={len(pairs)})"
                        ),
                    )
                )
        sensitivities.sort(key=lambda item: (-abs(item.spearman_rho), item.parameter, item.objective))
    else:
        notes.append("fewer than three feasible candidates: sensitivity analysis is not meaningful and was skipped")

    feasible_ranges: dict[str, dict[str, float]] = {}
    for name in parameter_names:
        values = [candidate.values[name] for candidate in feasible if name in candidate.values]
        if values:
            feasible_ranges[name] = {"min": min(values), "max": max(values), "count": len(values)}

    explanation = [
        f"{len(feasible)} of {len(inputs.candidates)} candidates are feasible",
        (
            "the Pareto frontier contains "
            + (", ".join(frontier_ids) if frontier_ids else "no candidate")
            + " (no candidate is better on every objective without being worse on another)"
        ),
        "best overall by weighted score: " + ranking[0].candidate_id,
    ]
    for trade_off in trade_offs:
        explanation.append(
            f"best {trade_off.objective} ({trade_off.sense}): {trade_off.best_candidate_id} "
            f"at {trade_off.best_value:.4g}{(' ' + trade_off.objective and '')}"
        )
    if sensitivities:
        strongest = sensitivities[0]
        explanation.append(f"most influential parameter/objective pair: {strongest.interpretation}")
    if any(
        value == 0.5 for scores in objective_scores.values() for value in scores.values()
    ):
        notes.append("at least one objective had no spread across feasible candidates; its score is neutral (0.5)")

    return EngineResult(
        outputs=ParetoOutput(
            feasible_count=len(feasible),
            infeasible_count=len(infeasible),
            pareto_frontier=sorted(frontier_ids),
            ranking=ranking[: inputs.top_n],
            trade_offs=trade_offs,
            sensitivities=sensitivities[:20],
            feasible_ranges=feasible_ranges,
            explanation=explanation,
            notes=notes,
        ),
        warnings=warnings,
        assumptions_applied=[
            "objective values were evaluated by the caller (this engine never computes physics)",
            "weights express analyst preference and are normalised by their sum",
            "min-max normalisation is applied across the feasible set only",
            "rank correlation is used for sensitivity: it detects monotone, not arbitrary, relations",
        ],
        limitations=(
            "no multi-objective evolutionary search (candidates come from the caller or a sweep)",
            "no constraint on uncertainty propagation beyond the optional robust ranking",
            "rank correlation does not establish causation nor detect interactions between parameters",
        ),
        references=("Pareto dominance and rank-correlation sensitivity analysis (standard practice)",),
    )


PARETO_SPEC = EngineSpec(
    key="optimization.pareto",
    name="Pareto frontier and sensitivity",
    version="1.1.0",
    domain_pack="Optimization Pack",
    category="optimization",
    summary=(
        "Filter evaluated candidates by constraints, compute the Pareto frontier and a weighted "
        "ranking, report objective trade-offs and parameter sensitivities."
    ),
    inputs_model=ParetoInput,
    outputs_model=ParetoOutput,
    consumes=("optimization.candidates", "engine.result"),
    produces=("optimization.frontier", "recommendation.evidence"),
    parameters={"rank_correlation": {"value": "spearman", "description": "sensitivity method"}},
    assumptions=(
        "candidate objective values are trusted as produced by the engines",
        "the caller declares direction and weight of each objective",
    ),
    limitations=(
        "no surrogate or gradient-based search",
        "no interaction terms between parameters",
    ),
    references=("Pareto dominance; Spearman rank correlation",),
    tags=("optimization", "pareto", "sensitivity", "trade-off"),
    action_level="L1",
)

register_engine(FunctionEngine(PARETO_SPEC, compute_pareto), aliases=("pareto", "optimize"))
