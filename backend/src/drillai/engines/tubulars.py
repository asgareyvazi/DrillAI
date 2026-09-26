"""Tubular design: API 5C3 / TR 5C3 pipe body ratings and triaxial safety factors.

Pipe body performance is computed from the API formulation rather than from a canned
dimensional table, so any OD/wall/grades combination the user types is rateable and the
data source stays auditable:

* internal yield (burst): Barlow with the 0.875 wall-thickness tolerance factor,
* collapse: the four API domains (yield, plastic, transition, elastic) with the
  TR 5C3 coefficient polynomials and the D/t domain boundaries,
* axial yield: cross-sectional area x yield strength,
* biaxial derating of burst and collapse under axial load (API TR 5C3 combined loading),
* triaxial (von Mises) equivalent stress check at the inner wall for the design point,
* design factors are explicit inputs with documented defaults and every check is reported.

Only pipe-body ratings are produced. Connection ratings, wear and corrosion allowances must
come from the manufacturer's catalogue and are deliberately not invented here.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field

from drillai.engines.contract import ConstraintViolation, EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field

PSI = 6894.757293168361
GRAVITY = 9.80665

# API TR 5C3 coefficient polynomials (yield strength in psi).
def _coefficients(yield_stress_psi: float) -> dict[str, float]:
    """API TR 5C3 coefficient polynomials (A, B, C) and the transition constants (F, G).

    A, B and C are the published third/first-order polynomials in the yield strength.
    F and G are recovered from their definitions rather than tabulated: the transition line
    ``P = Yp (F/(D/t) - G)`` is tangent to the elastic collapse curve at the transition/elastic
    boundary ``d1 = (2 + B/A) / (3 B/A)`` (which fixes F through the slope match) and passes
    through ``D/t = A/B``, where the average plastic collapse pressure is zero (which fixes
    ``G = F B / A``). Deriving them keeps the engine valid for yield strengths between the
    tabulated grades, where interpolation of F/G tables is undefined.
    """
    fy = yield_stress_psi
    a = 2.8762 + 0.10679e-5 * fy + 0.21301e-10 * fy**2 - 0.53132e-16 * fy**3
    b = 0.026233 + 0.50609e-6 * fy
    c = -465.93 + 0.030867 * fy - 0.10483e-7 * fy**2 + 0.36989e-13 * fy**3
    beta = b / a
    d1 = (2.0 + beta) / (3.0 * beta)
    f = (46.95e6 / fy) * (3.0 * d1 - 1.0) / (d1 - 1.0) ** 3
    g = f * beta
    return {"A": a, "B": b, "C": c, "F": f, "G": g}


def _domain_boundaries(yield_stress_psi: float) -> tuple[float, float, float]:
    coefficients = _coefficients(yield_stress_psi)
    a, b, c, f, g = (coefficients[key] for key in ("A", "B", "C", "F", "G"))
    fy = yield_stress_psi
    b_plus = b + c / fy
    yield_plastic = ((a - 2.0) ** 2 + 8.0 * b_plus) ** 0.5 + (a - 2.0)
    yield_plastic = yield_plastic / (2.0 * b_plus)
    plastic_transition = fy * (a - f) / (c + fy * (b - g))
    transition_elastic = (2.0 + b / a) / (3.0 * b / a)
    return yield_plastic, plastic_transition, transition_elastic


def bare_collapse_pressure(yield_stress_si: float, d_over_t: float) -> tuple[float, str]:
    """API collapse resistance (Pa) and the governing domain name."""
    fy = yield_stress_si / PSI  # coefficients are defined in psi
    coefficients = _coefficients(fy)
    a, b, c, f, g = (coefficients[key] for key in ("A", "B", "C", "F", "G"))
    yield_plastic, plastic_transition, transition_elastic = _domain_boundaries(fy)

    if d_over_t <= yield_plastic:
        pressure_psi = 2.0 * fy * ((d_over_t - 1.0) / d_over_t**2)
        domain = "yield"
    elif d_over_t <= plastic_transition:
        pressure_psi = fy * (a / d_over_t - b) - c
        domain = "plastic"
    elif d_over_t <= transition_elastic:
        pressure_psi = fy * (f / d_over_t - g)
        domain = "transition"
    else:
        pressure_psi = 46.95e6 / (d_over_t * (d_over_t - 1.0) ** 2)
        domain = "elastic"
    return max(0.0, pressure_psi * PSI), domain


def burst_rating(yield_stress_si: float, outer_diameter_si: float, wall_thickness_si: float) -> float:
    """Barlow internal yield pressure with the API 0.875 wall tolerance factor."""
    return 0.875 * 2.0 * yield_stress_si * wall_thickness_si / outer_diameter_si


def axial_yield_force(yield_stress_si: float, outer_diameter_si: float, wall_thickness_si: float) -> float:
    inner_diameter = outer_diameter_si - 2.0 * wall_thickness_si
    return math.pi / 4.0 * (outer_diameter_si**2 - inner_diameter**2) * yield_stress_si


def yield_stress_with_axial(yield_stress_si: float, axial_stress_si: float) -> float:
    """API TR 5C3 axial-stress equivalent yield strength for collapse/burst ratings.

    For axial tension the TR 5C3 combined-loading expression applies:

        f_yax = [sqrt(1 - 0.75 (sigma_a/f_y)^2) - 0.5 (sigma_a/f_y)] f_y

    For axial compression TR 5C3 2015 does **not** reduce the collapse resistance (the
    governing term is ``p_i + sigma_a``), so the unmodified yield strength is returned and
    compression cases are covered by the triaxial check, which carries the real axial stress.
    """
    if axial_stress_si <= 0:
        return yield_stress_si
    ratio = axial_stress_si / yield_stress_si
    factor = math.sqrt(max(0.0, 1.0 - 0.75 * ratio**2)) - 0.5 * ratio
    return max(0.0, factor * yield_stress_si)


class DesignFactors(BaseModel):
    """Explicit design factors. Defaults are conventional land/moderate wells and are editable."""

    model_config = ConfigDict(extra="forbid")
    burst: float = quantity_field(1.10, "1", gt=0.0)
    collapse: float = quantity_field(1.00, "1", gt=0.0)
    tension: float = quantity_field(1.60, "1", gt=0.0)
    triaxial: float = quantity_field(1.25, "1", gt=0.0)


class AxialLoad(BaseModel):
    """Axial load at a pipe section. Sign convention: **tension positive**.

    ``hanging_weight_si`` is the buoyed weight of everything below the section (positive in the
    normal hanging case); a negative value models compression, e.g. set-down weight or the
    part of a string below its neutral point.
    """

    model_config = ConfigDict(extra="forbid")
    hanging_weight_si: float = quantity_field(
        0.0, "N", description="buoyed weight below this point; negative in compression"
    )
    overpull_si: float = quantity_field(0.0, "N", ge=0.0)
    pressure_area_force_si: float = quantity_field(0.0, "N", description="piston/ballooning force from pressure acting on a change of area")


class TubularSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    from_depth_si: float = quantity_field(0.0, "m", ge=0.0)
    to_depth_si: float = quantity_field(..., "m", gt=0.0)
    outer_diameter_si: float = quantity_field(..., "m", gt=0.0)
    wall_thickness_si: float = quantity_field(..., "m", gt=0.0)
    yield_stress_si: float = quantity_field(..., "Pa", gt=0.0)
    grade: str | None = None
    linear_mass_si: float | None = quantity_field(None, "kg/m", gt=0.0)
    internal_pressure_si: float = quantity_field(0.0, "Pa", ge=0.0)
    external_pressure_si: float = quantity_field(0.0, "Pa", ge=0.0)
    axial: AxialLoad | None = None
    wear_allowance: float = quantity_field(
        0.0, "1", ge=0.0, le=0.5, description="fraction of wall thickness removed by wear"
    )

    @property
    def inner_diameter_si(self) -> float:
        return self.outer_diameter_si - 2.0 * self.wall_thickness_si * (1.0 - self.wear_allowance)


class SegmentResult(BaseModel):
    """Load check for one tubular section.

    ``burst_rating_si`` and ``collapse_rating_si`` are the ratings **as applied**, i.e. after
    the biaxial (axial-stress) derating; the uniaxial ratings are reported next to them and
    ``axial_derating_factor`` records the correction, so every safety factor is reproducible
    by hand from the numbers in this record.
    """

    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    from_depth_si: float
    to_depth_si: float
    d_over_t: float
    collapse_domain: str
    axial_stress_si: float
    equivalent_yield_stress_si: float
    axial_derating_factor: float
    burst_rating_si: float
    burst_rating_uniaxial_si: float
    collapse_rating_si: float
    collapse_rating_uniaxial_si: float
    axial_yield_force_si: float
    axial_load_si: float
    burst_safety_factor: float | None = None
    collapse_safety_factor: float | None = None
    tension_safety_factor: float | None = None
    triaxial_safety_factor: float | None = None
    triaxial_equivalent_stress_si: float | None = None
    net_collapse_load_si: float
    net_burst_load_si: float
    governs: str
    qualified: bool
    violations: list[ConstraintViolation] = Field(default_factory=list)


class TubularDesignInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: list[TubularSegment] = Field(min_length=1)
    design_factors: DesignFactors = Field(default_factory=DesignFactors)
    mud_weight_si: float | None = quantity_field(None, "kg/m3", gt=0.0, description="used to compute hydrostatic external load when not given per segment")


class TubularDesignOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segments: list[SegmentResult]
    design_factors: DesignFactors
    overall_qualified: bool
    governing_segment: str | None = None
    governed_by: str | None = None
    minimum_safety_factor: float | None = None
    violations: list[ConstraintViolation] = Field(default_factory=list)
    notes: list[str]


def compute_tubular_design(inputs: TubularDesignInput) -> EngineResult:
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []
    factors = inputs.design_factors
    results: list[SegmentResult] = []
    segment_violations: list[ConstraintViolation] = []

    for segment in sorted(inputs.segments, key=lambda item: item.from_depth_si):
        if segment.to_depth_si <= segment.from_depth_si:
            raise ValueError(f"{segment.name or segment.from_depth_si}: to_depth must exceed from_depth")
        wall = segment.wall_thickness_si * (1.0 - segment.wear_allowance)
        if wall <= 0 or segment.inner_diameter_si <= 0:
            raise ValueError(f"{segment.name or segment.from_depth_si}: wall thickness or inner diameter is non-positive")

        d_over_t = segment.outer_diameter_si / wall
        burst = burst_rating(segment.yield_stress_si, segment.outer_diameter_si, wall)
        collapse, domain = bare_collapse_pressure(segment.yield_stress_si, d_over_t)
        axial_yield = axial_yield_force(segment.yield_stress_si, segment.outer_diameter_si, wall)

        axial = segment.axial or AxialLoad()
        axial_load = axial.hanging_weight_si + axial.overpull_si + axial.pressure_area_force_si
        area = math.pi / 4.0 * (segment.outer_diameter_si**2 - segment.inner_diameter_si**2)
        axial_stress = axial_load / area if area > 0 else 0.0

        # Biaxial derating: axial tension lowers the equivalent yield strength used for both
        # the burst and the collapse rating (API TR 5C3 combined loading).
        effective_yield = yield_stress_with_axial(segment.yield_stress_si, axial_stress)
        if effective_yield < segment.yield_stress_si:
            burst_derated = burst_rating(effective_yield, segment.outer_diameter_si, wall)
            collapse_derated = bare_collapse_pressure(effective_yield, d_over_t)[0]
        else:
            burst_derated = burst
            collapse_derated = collapse

        external = segment.external_pressure_si
        if inputs.mud_weight_si and not external:
            external = inputs.mud_weight_si * GRAVITY * segment.to_depth_si
        net_collapse = max(0.0, external - segment.internal_pressure_si)
        net_burst = max(0.0, segment.internal_pressure_si - external)

        burst_sf = burst_derated / net_burst if net_burst > 0 else None
        collapse_sf = collapse_derated / net_collapse if net_collapse > 0 else None
        tension_sf = axial_yield / axial_load if axial_load > 0 else None
        triaxial_sf = None
        equivalent_stress = None
        if segment.internal_pressure_si or external:
            triaxial_sf, equivalent_stress = _triaxial(segment, wall, axial_stress, segment.yield_stress_si)

        checks: list[tuple[str, float | None, float]] = [
            ("burst", burst_sf, factors.burst),
            ("collapse", collapse_sf, factors.collapse),
            ("tension", tension_sf, factors.tension),
            ("triaxial", triaxial_sf, factors.triaxial),
        ]
        segment_issues: list[ConstraintViolation] = []
        ratios = []
        for name, safety, required in checks:
            if safety is None:
                continue
            ratios.append((safety / required, safety, required, name))
            if safety < required:
                message = (
                    f"{segment.name or 'segment'}: {name} safety factor {safety:.2f} is below the required "
                    f"{required:.2f} (depth {segment.from_depth_si:.0f}-{segment.to_depth_si:.0f} m)"
                )
                issue = ConstraintViolation(
                    name=f"{name}_safety_factor",
                    message=message,
                    severity="error",
                    limit=required,
                    actual=safety,
                    unit="1",
                    source=f"design factor {name}",
                )
                segment_issues.append(issue)
                segment_violations.append(issue)

        worst = min(ratios, key=lambda item: item[0]) if ratios else None
        results.append(
            SegmentResult(
                name=segment.name,
                from_depth_si=segment.from_depth_si,
                to_depth_si=segment.to_depth_si,
                d_over_t=d_over_t,
                collapse_domain=domain,
                axial_stress_si=axial_stress,
                equivalent_yield_stress_si=effective_yield,
                axial_derating_factor=(effective_yield / segment.yield_stress_si),
                burst_rating_si=burst_derated,
                burst_rating_uniaxial_si=burst,
                collapse_rating_si=collapse_derated,
                collapse_rating_uniaxial_si=collapse,
                axial_yield_force_si=axial_yield,
                axial_load_si=axial_load,
                burst_safety_factor=burst_sf,
                collapse_safety_factor=collapse_sf,
                tension_safety_factor=tension_sf,
                triaxial_safety_factor=triaxial_sf,
                triaxial_equivalent_stress_si=equivalent_stress,
                net_collapse_load_si=net_collapse,
                net_burst_load_si=net_burst,
                governs=worst[3] if worst else "none",
                qualified=not segment_issues,
                violations=segment_issues,
            )
        )

    violations.extend(segment_violations)
    ratios = []
    for result in results:
        for name, safety, required in (
            ("burst", result.burst_safety_factor, factors.burst),
            ("collapse", result.collapse_safety_factor, factors.collapse),
            ("tension", result.tension_safety_factor, factors.tension),
            ("triaxial", result.triaxial_safety_factor, factors.triaxial),
        ):
            if safety is not None:
                ratios.append((safety / required, result.name or f"{result.from_depth_si:.0f}-{result.to_depth_si:.0f} m", name, safety))
    governing = min(ratios, key=lambda item: item[0]) if ratios else None

    if governing and governing[1].split(" ")[0] == "":
        warnings.append("governing segment has no name: set segment names for readable reports")

    notes = [
        "pipe body ratings only — connection ratings come from the manufacturer catalogue",
        "burst uses the API Barlow equation with the 0.875 wall-tolerance factor",
        "collapse uses API TR 5C3 coefficients and D/t domain boundaries",
        "axial load derates burst and collapse through the equivalent yield strength",
        "triaxial check is a von Mises stress check at the inner wall using the Lamé solution",
    ]
    if inputs.mud_weight_si:
        notes.append("external pressure taken as mud hydrostatic at segment bottom where not supplied per segment")

    outputs = TubularDesignOutput(
        segments=results,
        design_factors=factors,
        overall_qualified=not violations,
        governing_segment=governing[1] if governing else None,
        governed_by=governing[2] if governing else None,
        minimum_safety_factor=governing[3] if governing else None,
        violations=violations,
        notes=notes,
    )
    return EngineResult(outputs=outputs, warnings=warnings, violations=violations, assumptions_applied=notes[:4])


def inner_wall_stresses(
    outer_diameter_si: float,
    wall_thickness_si: float,
    internal_pressure_si: float,
    external_pressure_si: float,
    axial_stress_si: float,
) -> tuple[float, float, float]:
    """Lamé thick-wall stresses at the bore: (radial, tangential, axial) in Pa.

    Sign convention: tension positive, so the radial stress at the bore is ``-p_i`` and the
    hoop stress is compressive (negative) under net external pressure.
    """
    outer_radius = outer_diameter_si / 2.0
    inner_radius = outer_radius - wall_thickness_si
    if inner_radius <= 0:
        raise ValueError("wall thickness leaves no bore")
    a2, b2 = inner_radius**2, outer_radius**2
    radial = (internal_pressure_si * a2 - external_pressure_si * b2) / (b2 - a2) - (
        (internal_pressure_si - external_pressure_si) * a2 * b2 / (b2 - a2)
    ) / a2
    tangential = (internal_pressure_si * a2 - external_pressure_si * b2) / (b2 - a2) + (
        (internal_pressure_si - external_pressure_si) * a2 * b2 / (b2 - a2)
    ) / a2
    return radial, tangential, axial_stress_si


def _triaxial(
    segment: TubularSegment,
    wall: float,
    axial_stress: float,
    yield_stress: float,
) -> tuple[float, float]:
    """von Mises equivalent stress at the inner wall vs the yield strength."""
    radial, tangential, axial = inner_wall_stresses(
        segment.outer_diameter_si,
        wall,
        segment.internal_pressure_si,
        segment.external_pressure_si,
        axial_stress,
    )
    equivalent = math.sqrt(
        0.5 * ((tangential - radial) ** 2 + (radial - axial) ** 2 + (axial - tangential) ** 2)
    )
    safety = yield_stress / equivalent if equivalent > 1e-9 else float("inf")
    return safety, equivalent


TUBULAR_SPEC = EngineSpec(
    key="tubulars.api_5c3",
    name="Tubular design (pipe body, API 5C3)",
    version="1.1.0",
    domain_pack="Casing Design Pack",
    category="tubulars",
    summary=(
        "API TR 5C3 pipe body burst, collapse, axial and triaxial ratings with explicit design factors, "
        "biaxial derating and wear allowance."
    ),
    inputs_model=TubularDesignInput,
    outputs_model=TubularDesignOutput,
    consumes=("wellbore.geometry", "formation.pressures", "mud.properties"),
    produces=("tubular.design",),
    parameters={
        "burst_formula": {"value": "barlow_0.875"},
        "collapse_domains": {"value": ("yield", "plastic", "transition", "elastic")},
    },
    assumptions=(
        "nominal wall thickness (manufacturer tolerances other than the API 0.875 burst factor are not applied)",
        "yield strength is the specified minimum for the grade",
        "pressure loads are static, no thermal or bending stresses beyond the axial term",
    ),
    limitations=(
        "pipe body only: no connection, coupling or thread rating",
        "no buckling or bending stress from doglegs",
        "bending, thermal and dynamic loads must be added by a dedicated design workflow",
    ),
    references=(
        "API TR 5C3 (2018) — Calculating Performance Properties of Pipe Used as Casing or Tubing",
        "API Bull 5C3 / equivalent TR equations for collapse domains",
    ),
    tags=("casing", "tubing", "burst", "collapse", "design"),
    action_level="L1",
    validation_status="verified_against_reference",
)

register_engine(FunctionEngine(TUBULAR_SPEC, compute_tubular_design), aliases=("tubulars", "casing_design", "api_5c3"))
