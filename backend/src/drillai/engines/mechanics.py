"""Drilling mechanics engines: torque & drag (soft-string) and specific energy.

``torque_drag.soft_string``
    Johancsik soft-string model. Tension is accumulated station by station accounting for
    buoyed string weight and Coulomb friction against the wellbore, with the normal force
    from both inclination change and azimuth change. Outputs hookload for pick-up, slack-off
    and rotating, surface torque, side forces, drag and string stretch — the numbers a
    drilling engineer checks against the rig's weight indicator.

``drilling.mse``
    Mechanical specific energy with an optional confined-compressive-strength comparison,
    which turns raw parameter data into a drilling-efficiency and dysfunction indicator.

Both engines take survey stations and string elements in canonical SI. Aluminium, steel or
composite string properties come from ``youngs_modulus``/``density`` overrides so unusual
strings stay representable without special cases.
"""

from __future__ import annotations

import math
from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field

from drillai.engines.contract import ConstraintViolation, EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field

STEEL_DENSITY_SI = 7850.0
STEEL_YOUNGS_MODULUS_SI = 210e9
GRAVITY = 9.80665


class SurveyPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    md_si: float = quantity_field(..., "m", ge=0.0)
    inclination_deg: float = quantity_field(0.0, "deg", ge=0.0, le=180.0)
    azimuth_deg: float = quantity_field(0.0, "deg", ge=0.0, lt=360.0)
    tvd_si: float | None = quantity_field(None, "m", ge=0.0, description="TVD if already computed by the trajectory engine")


class StringElement(BaseModel):
    """A drillstring/tubing element with the diameters and mass needed by T&D."""

    model_config = ConfigDict(extra="forbid")
    kind: str = Field(default="drillpipe", description="drillpipe|hwdp|drillcollar|bha|tubing")
    name: str | None = None
    from_depth_si: float = quantity_field(0.0, "m", ge=0.0)
    to_depth_si: float = quantity_field(..., "m", gt=0.0)
    od_si: float = quantity_field(..., "m", gt=0.0)
    id_si: float | None = quantity_field(None, "m", gt=0.0)
    linear_mass_si: float = quantity_field(..., "kg/m", gt=0.0, description="nominal mass per length in air")

    @property
    def wall_area_si(self) -> float:
        if self.id_si:
            return math.pi / 4.0 * (self.od_si**2 - self.id_si**2)
        return 0.0

    @property
    def length_si(self) -> float:
        return self.to_depth_si - self.from_depth_si


class TorqueDragInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    survey: list[SurveyPoint] = Field(min_length=2)
    string: list[StringElement] = Field(min_length=1)
    mud_weight_si: float = quantity_field(1200.0, "kg/m3", gt=0.0)
    bit_depth_si: float = quantity_field(..., "m", gt=0.0)
    friction_factor_pickup: float = quantity_field(0.25, "1", ge=0.0, le=1.0)
    friction_factor_slackoff: float = quantity_field(0.20, "1", ge=0.0, le=1.0)
    friction_factor_rotating: float = quantity_field(
        0.15, "1", ge=0.0, le=1.0, description="circumferential friction while rotating"
    )
    wob_si: float | None = quantity_field(None, "N", ge=0.0, description="weight on bit for on-bottom torque")
    bit_torque_si: float | None = quantity_field(None, "N.m", ge=0.0)
    bit_torque_coefficient: float = quantity_field(
        0.30, "1", gt=0.0, le=2.0, description="bit torque = coefficient * WOB * bit radius when not supplied"
    )
    bit_diameter_si: float | None = quantity_field(None, "m", gt=0.0)
    pressure_area_si: float | None = quantity_field(
        None, "m2", gt=0.0, description="flow/piston area causing ballooning or bit thrust (optional)"
    )
    pump_pressure_si: float | None = quantity_field(None, "Pa")
    surface_pressure_limit_si: float | None = quantity_field(
        None, "Pa", description="standpipe pressure limit used for the pressure check"
    )
    rig_hookload_limit_si: float | None = quantity_field(None, "N")
    youngs_modulus_si: float = quantity_field(STEEL_YOUNGS_MODULUS_SI, "Pa", gt=0.0)
    density_si: float = quantity_field(STEEL_DENSITY_SI, "kg/m3", gt=0.0)


class StationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    md_si: float
    inclination_deg: float
    tvd_si: float | None = None
    tension_pickup_si: float
    tension_slackoff_si: float
    tension_rotating_si: float
    side_force_si: float
    normal_force_si: float
    dogleg_deg: float = 0.0


class TorqueDragOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    string_weight_air_si: float
    buoyancy_factor: float
    string_weight_buoyed_si: float
    hookload_pickup_si: float
    hookload_slackoff_si: float
    hookload_rotating_si: float
    drag_up_si: float
    drag_down_si: float
    surface_torque_off_bottom_si: float
    surface_torque_on_bottom_si: float
    bit_torque_si: float | None = None
    side_force_max_si: float
    side_force_max_md_si: float
    stretch_si: float | None = None
    tension_max_si: float
    neutral_point_md_si: float | None = None
    stations: list[StationResult]
    warnings: list[str] = Field(default_factory=list)


class MseEngineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wob_si: float = quantity_field(..., "N", gt=0.0)
    torque_si: float = quantity_field(..., "N.m", gt=0.0)
    rpm_si: float = quantity_field(..., "rpm", gt=0.0)
    rop_si: float = quantity_field(..., "m/h", gt=0.0)
    bit_diameter_si: float = quantity_field(..., "m", gt=0.0)
    flow_rate_si: float | None = quantity_field(None, "m3/s", gt=0.0)
    confined_compressive_strength_si: float | None = quantity_field(None, "Pa", gt=0.0)
    bit_hydraulic_horsepower_per_area_si: float | None = quantity_field(None, "W/m2", gt=0.0)
    reference_mse_si: float | None = quantity_field(
        None, "J/m3", gt=0.0, description="formation/reference MSE used for the dysfunction ratio"
    )
    depth_si: float | None = quantity_field(None, "m", ge=0.0)


class MseEngineOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bit_area_si: float
    mse_si: float
    mse_axial_component_si: float
    mse_torsional_component_si: float
    mse_psi: float
    drilling_efficiency: float | None = None
    dysfunction_ratio: float | None = None
    dysfunction_indicator: str
    rop_si: float
    wob_per_diameter_si: float
    estimated_confined_strength_si: float | None = None
    notes: list[str]


# --------------------------------------------------------------------------- T&D
def compute_torque_drag(inputs: TorqueDragInput) -> EngineResult:
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []

    buoyancy = max(0.0, 1.0 - inputs.mud_weight_si / inputs.density_si)
    stations = sorted(inputs.survey, key=lambda point: point.md_si)
    if stations[0].md_si > 0:
        stations.insert(
            0,
            SurveyPoint(md_si=0.0, inclination_deg=stations[0].inclination_deg, azimuth_deg=stations[0].azimuth_deg),
        )

    # Integrate over the union of survey stations and string boundaries so that a string
    # defined on its own interval is fully accounted for, even when the survey is sparse.
    deepest_element = max(element.to_depth_si for element in inputs.string)
    analysis_limit = min(stations[-1].md_si, deepest_element, inputs.bit_depth_si)
    if analysis_limit < inputs.bit_depth_si:
        warnings.append(
            f"analysis covers 0-{analysis_limit:.0f} m but the bit is at {inputs.bit_depth_si:.0f} m: "
            "the string or survey does not reach the bit"
        )
    nodes = {0.0, analysis_limit}
    nodes.update(station.md_si for station in stations if 0.0 < station.md_si < analysis_limit)
    for element in inputs.string:
        for depth in (element.from_depth_si, element.to_depth_si):
            if 0.0 <= depth <= analysis_limit:
                nodes.add(depth)
    depths = sorted(nodes)

    results: list[StationResult] = []
    tension_pickup = tension_slackoff = tension_rotating = 0.0
    side_forces: list[tuple[float, float]] = []
    torque_terms: list[float] = []
    stretch_terms: list[float] = []

    previous_inclination, previous_azimuth = _interpolate_angles(stations, 0.0)
    results.append(
        StationResult(
            md_si=0.0,
            inclination_deg=previous_inclination,
            tvd_si=stations[0].tvd_si,
            tension_pickup_si=0.0,
            tension_slackoff_si=0.0,
            tension_rotating_si=0.0,
            side_force_si=0.0,
            normal_force_si=0.0,
        )
    )

    for index in range(1, len(depths)):
        top, bottom = depths[index - 1], depths[index]
        delta_md = bottom - top
        if delta_md <= 0:
            continue
        element = _element_at(inputs.string, 0.5 * (top + bottom))
        if element is None:
            continue

        inclination, azimuth = _interpolate_angles(stations, bottom)
        weight = element.linear_mass_si * GRAVITY * buoyancy * delta_md
        mean_inclination_rad = math.radians(0.5 * (previous_inclination + inclination))
        delta_inclination = math.radians(inclination - previous_inclination)
        delta_azimuth = math.radians(_signed_delta_deg(azimuth - previous_azimuth))

        # Side force from the inclination change, the azimuth change and the weight component
        # normal to the hole (Johancsik soft string).
        inclination_term = 2.0 * tension_pickup * math.sin(abs(delta_inclination) / 2.0) + weight * math.sin(
            mean_inclination_rad
        )
        azimuth_term = (
            2.0 * tension_pickup * math.sin(abs(delta_azimuth) / 2.0) * math.sin(mean_inclination_rad)
        )
        normal_force = math.hypot(inclination_term, azimuth_term)

        tension_before = tension_rotating  # axial (no-drag) tension drives stretch
        tension_pickup += weight * math.cos(mean_inclination_rad) + inputs.friction_factor_pickup * normal_force
        tension_slackoff += weight * math.cos(mean_inclination_rad) - inputs.friction_factor_slackoff * normal_force
        tension_rotating += weight * math.cos(mean_inclination_rad)

        side_force_per_length = normal_force / delta_md
        side_forces.append((bottom, side_force_per_length))
        # Circumferential friction torque about the string axis.
        torque_terms.append(
            inputs.friction_factor_rotating * side_force_per_length * delta_md * (element.od_si / 2.0)
        )
        if element.wall_area_si > 0:
            mean_tension = 0.5 * (tension_before + tension_rotating)
            stretch_terms.append(mean_tension / (inputs.youngs_modulus_si * element.wall_area_si) * delta_md)

        if abs(delta_inclination) or abs(delta_azimuth):
            dogleg = math.degrees(
                math.acos(
                    max(
                        -1.0,
                        min(
                            1.0,
                            math.cos(delta_inclination)
                            - math.sin(math.radians(previous_inclination))
                            * math.sin(math.radians(inclination))
                            * (1.0 - math.cos(delta_azimuth)),
                        ),
                    )
                )
            )
        else:
            dogleg = 0.0

        results.append(
            StationResult(
                md_si=bottom,
                inclination_deg=inclination,
                tvd_si=_interpolate_tvd(stations, bottom),
                tension_pickup_si=tension_pickup,
                tension_slackoff_si=tension_slackoff,
                tension_rotating_si=tension_rotating,
                side_force_si=side_force_per_length,
                normal_force_si=normal_force,
                dogleg_deg=dogleg,
            )
        )
        previous_inclination, previous_azimuth = inclination, azimuth

    torque_off_bottom = sum(torque_terms)
    bit_torque = inputs.bit_torque_si
    if bit_torque is None and inputs.wob_si and inputs.bit_diameter_si:
        bit_torque = inputs.bit_torque_coefficient * inputs.wob_si * inputs.bit_diameter_si / 2.0
        warnings.append(
            "bit torque estimated from WOB with a declared coefficient; supply measured bit torque for a validated value"
        )
    torque_on_bottom = torque_off_bottom + (bit_torque or 0.0)

    string_weight_air = sum(element.linear_mass_si * GRAVITY * element.length_si for element in inputs.string)
    string_weight_buoyed = string_weight_air * buoyancy
    stretch = sum(stretch_terms) if all(element.wall_area_si > 0 for element in inputs.string) else None

    max_side = max(side_forces, key=lambda item: item[1]) if side_forces else (0.0, 0.0)
    max_tension = max((result.tension_pickup_si for result in results), default=0.0)
    neutral_point = _neutral_point(results)
    if neutral_point is not None:
        warnings.append(
            f"slack-off tension reaches zero at {neutral_point:.0f} m: below that depth the string is in "
            "compression and buckling is not modelled"
        )

    if inputs.rig_hookload_limit_si and tension_pickup > inputs.rig_hookload_limit_si:
        violations.append(
            ConstraintViolation(
                name="rig_hookload_limit",
                message=(
                    f"pick-up hookload {tension_pickup / 1000.0:.1f} kN exceeds rig limit "
                    f"{inputs.rig_hookload_limit_si / 1000.0:.1f} kN"
                ),
                severity="error",
                limit=inputs.rig_hookload_limit_si,
                actual=tension_pickup,
                unit="N",
            )
        )

    outputs = TorqueDragOutput(
        string_weight_air_si=string_weight_air,
        buoyancy_factor=buoyancy,
        string_weight_buoyed_si=string_weight_buoyed,
        hookload_pickup_si=tension_pickup,
        hookload_slackoff_si=tension_slackoff,
        hookload_rotating_si=tension_rotating,
        drag_up_si=tension_pickup - tension_rotating,
        drag_down_si=tension_rotating - tension_slackoff,
        surface_torque_off_bottom_si=torque_off_bottom,
        surface_torque_on_bottom_si=torque_on_bottom,
        bit_torque_si=bit_torque,
        side_force_max_si=max_side[1],
        side_force_max_md_si=max_side[0],
        stretch_si=stretch,
        tension_max_si=max_tension,
        neutral_point_md_si=neutral_point,
        stations=results,
        warnings=warnings,
    )
    return EngineResult(
        outputs=outputs,
        warnings=warnings,
        violations=violations,
        assumptions_applied=[
            "soft-string model (no bending stiffness)",
            "Coulomb friction with constant coefficients",
            "buoyancy applied as a simple density ratio (no piston/ballooning effects unless pressure area supplied)",
            "no buckling (sinusoidal/helical) modelling",
        ],
    )


def _element_at(string: list[StringElement], md_si: float) -> StringElement | None:
    """String element covering a measured depth (innermost/most specific first)."""
    for element in string:
        if element.from_depth_si <= md_si <= element.to_depth_si:
            return element
    return None


def _signed_delta_deg(delta: float) -> float:
    """Shortest signed angular difference in degrees, in (-180, 180]."""
    return (delta + 180.0) % 360.0 - 180.0


def _interpolate_angles(stations: list[SurveyPoint], md_si: float) -> tuple[float, float]:
    """Inclination and azimuth at any measured depth, linearly interpolated between stations."""
    if md_si <= stations[0].md_si:
        return stations[0].inclination_deg, stations[0].azimuth_deg
    for previous, current in pairwise(stations):
        if md_si <= current.md_si:
            span = current.md_si - previous.md_si
            if span <= 0:
                return current.inclination_deg, current.azimuth_deg
            fraction = (md_si - previous.md_si) / span
            azimuth = previous.azimuth_deg + fraction * _signed_delta_deg(current.azimuth_deg - previous.azimuth_deg)
            return (
                previous.inclination_deg + fraction * (current.inclination_deg - previous.inclination_deg),
                azimuth % 360.0,
            )
    return stations[-1].inclination_deg, stations[-1].azimuth_deg


def _interpolate_tvd(stations: list[SurveyPoint], md_si: float) -> float | None:
    known = [station for station in stations if station.tvd_si is not None]
    if not known:
        return None
    if md_si <= known[0].md_si:
        return known[0].tvd_si
    for previous, current in pairwise(known):
        if md_si <= current.md_si:
            span = current.md_si - previous.md_si
            if span <= 0:
                return current.tvd_si
            fraction = (md_si - previous.md_si) / span
            return previous.tvd_si + fraction * (current.tvd_si - previous.tvd_si)
    return known[-1].tvd_si


def _neutral_point(results: list[StationResult]) -> float | None:
    """Depth where the slack-off tension crosses zero (start of the compression zone)."""
    for previous, current in pairwise(results):
        if previous.tension_slackoff_si > 0 >= current.tension_slackoff_si:
            drop = previous.tension_slackoff_si - current.tension_slackoff_si
            if drop <= 0:
                return current.md_si
            fraction = previous.tension_slackoff_si / drop
            return previous.md_si + fraction * (current.md_si - previous.md_si)
    return None


# --------------------------------------------------------------------------- MSE
def compute_mse(inputs: MseEngineInput) -> EngineResult:
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []

    area = math.pi / 4.0 * inputs.bit_diameter_si**2
    rop_m_per_s = inputs.rop_si / 3600.0
    rpm_rev_per_s = inputs.rpm_si * 2.0 * math.pi / 60.0

    axial = inputs.wob_si / area
    torsional = (rpm_rev_per_s * inputs.torque_si) / (area * rop_m_per_s)
    mse = axial + torsional

    efficiency = None
    if inputs.confined_compressive_strength_si:
        efficiency = min(1.0, inputs.confined_compressive_strength_si / mse)

    dysfunction_ratio = None
    indicator = "unknown"
    if inputs.reference_mse_si:
        dysfunction_ratio = mse / inputs.reference_mse_si
        if dysfunction_ratio < 1.05:
            indicator = "efficient"
        elif dysfunction_ratio < 1.35:
            indicator = "marginal"
        else:
            indicator = "dysfunction_likely"
            violations.append(
                ConstraintViolation(
                    name="mse_dysfunction",
                    message=(
                        f"MSE {mse / 1e6:.0f} MPa is {dysfunction_ratio:.2f}x the reference "
                        f"{inputs.reference_mse_si / 1e6:.0f} MPa: dysfunction (bit balling, vibration or founder point)"
                    ),
                    severity="warning",
                    limit=inputs.reference_mse_si,
                    actual=mse,
                    unit="Pa",
                    source="MSE reference",
                )
            )
    elif inputs.confined_compressive_strength_si and efficiency is not None and efficiency < 0.35:
        indicator = "dysfunction_likely"
        violations.append(
            ConstraintViolation(
                name="drilling_efficiency",
                message=f"drilling efficiency {efficiency:.2f} is low for the provided rock strength",
                severity="warning",
                limit=0.35,
                actual=efficiency,
                source="CCS comparison",
            )
        )
    elif inputs.confined_compressive_strength_si:
        indicator = "efficient" if (efficiency or 0) >= 0.35 else "marginal"

    notes = [
        "MSE = WOB/A + (2*pi*N*T)/(A*ROP) in consistent SI units",
        "axial and torsional components are reported separately for diagnosis",
    ]
    if not inputs.confined_compressive_strength_si:
        notes.append("no confined compressive strength supplied: efficiency cannot be computed")

    outputs = MseEngineOutput(
        bit_area_si=area,
        mse_si=mse,
        mse_axial_component_si=axial,
        mse_torsional_component_si=torsional,
        mse_psi=mse / 6894.757293168361,
        drilling_efficiency=efficiency,
        dysfunction_ratio=dysfunction_ratio,
        dysfunction_indicator=indicator,
        rop_si=inputs.rop_si,
        wob_per_diameter_si=inputs.wob_si / inputs.bit_diameter_si,
        estimated_confined_strength_si=inputs.confined_compressive_strength_si,
        notes=notes,
    )
    return EngineResult(outputs=outputs, warnings=warnings, violations=violations, assumptions_applied=notes[:2])


TORQUE_DRAG_SPEC = EngineSpec(
    key="torque_drag.soft_string",
    name="Torque and drag (soft string)",
    version="1.1.0",
    domain_pack="T&D Pack",
    category="mechanics",
    summary=(
        "Johancsik soft-string torque & drag: pick-up/slack-off/rotating hookload, surface torque, "
        "side forces, drag, neutral point and string stretch with rig-limit checks."
    ),
    inputs_model=TorqueDragInput,
    outputs_model=TorqueDragOutput,
    consumes=("trajectory.stations", "drillstring.assembly", "mud.properties", "wellbore.geometry"),
    produces=("torque_drag.profile",),
    parameters={
        "model": {"value": "johancsik_soft_string"},
        "friction_factor_reference": {"value": "0.15-0.35", "description": "field range for water/oil based mud"},
    },
    assumptions=(
        "soft string: bending stiffness is neglected",
        "constant Coulomb friction coefficients over the whole string",
        "buoyancy via density ratio",
    ),
    limitations=(
        "no buckling (sinusoidal/helical) or lock-up analysis",
        "no temperature/curvature-dependent friction, no tool-joint local effects",
        "stretch is linear-elastic only",
    ),
    references=(
        "Johancsik, Friesen & Dawson, SPE 11380 (1984) — Torque and Drag in Directional Wells",
        "Aadnoy et al. — Petroleum Rock Mechanics (soft-string treatment)",
    ),
    tags=("torque", "drag", "hookload", "mechanics"),
    action_level="L1",
)

MSE_SPEC = EngineSpec(
    key="drilling.mse",
    name="Mechanical specific energy and drilling efficiency",
    version="1.1.0",
    domain_pack="Bit Pack",
    category="mechanics",
    summary=(
        "Mechanical specific energy with axial/torsional decomposition, drilling efficiency against rock "
        "strength and a dysfunction indicator from a reference MSE."
    ),
    inputs_model=MseEngineInput,
    outputs_model=MseEngineOutput,
    consumes=("drilling.parameters", "bit.spec", "formation.markers"),
    produces=("drilling.performance",),
    parameters={"dysfunction_thresholds": {"value": {"marginal": 1.05, "dysfunction": 1.35}}},
    assumptions=(
        "MSE from surface parameters (no downhole WOB/torque correction)",
        "constant ROP/WOB/torque within the interval",
    ),
    limitations=(
        "surface-measured values include friction losses and vibration effects",
        "no bit wear or dull-grading correction",
        "requires a reference MSE or rock strength for absolute judgements",
    ),
    references=("Teale (1965) — The Concept of Specific Energy in Rock Drilling", "Pessier & Fear (1992) — MSE drilling dysfunction"),
    tags=("rop", "mse", "performance", "dysfunction"),
    action_level="L1",
)

register_engine(FunctionEngine(TORQUE_DRAG_SPEC, compute_torque_drag), aliases=("torque_drag", "tnd"))
register_engine(FunctionEngine(MSE_SPEC, compute_mse), aliases=("mse", "specific_energy"))
