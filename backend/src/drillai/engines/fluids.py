"""Fluids and hydraulics engines: wellbore capacity/volumes and circulation hydraulics.

Two engines share one geometry model:

* ``wellbore.capacity`` — string/annulus volumes, pump strokes, bottoms-up and circulation
  time, which the rig floor uses on every connection;
* ``hydraulics.laminar`` — laminar Bingham-plastic pressure losses (exact implicit solution,
  not a field-unit approximation), bit/orifice pressure drop, jet impact force, bit
  hydraulic horsepower, ECD profile, annular velocities, cuttings settling and a
  transport-ratio indicator, with pore-pressure/fracture-gradient limit checks.

Physics notes (why this is written in SI rather than in oilfield formulas)
-------------------------------------------------------------------------
Pressure loss is solved from first principles per interval:

* **Pipe (Bingham plastic, laminar):** with wall shear stress ``τ_w = D·(dP/dL)/4`` and
  ``x = τ_y/τ_w`` the exact flow equation is

      Q = (π D⁴ ΔP) / (128 μ_p L) · [1 − (4/3)x + (1/3)x⁴]        for x ≤ 1, else Q = 0

  which reduces to Poiseuille for ``τ_y = 0``. Because the equation is implicit in ΔP, the
  engine solves it by bisection — no hand-waved constant.

* **Annulus (Bingham plastic, laminar, slot approximation):** with gap ``h``, mean
  circumference ``w`` and ``ξ = 2τ_y·L/(h·ΔP)``,

      Q = (w h³ ΔP)/(12 μ_p L) · [1 − (3/2)ξ + (1/2)ξ³]

* **Bit/nozzles:** orifice equation ``ΔP = ρQ² / (2 (C_d A)²)`` with the coefficient declared
  as an assumption, jet velocity ``V = Q/A`` and impact force ``F = ρQV``.

Turbulence is *not* silently ignored: the Reynolds number is computed per interval and the
result is flagged (and downgraded to a warning constraint) whenever the laminar assumption
is violated. That is what a defensible hydraulics result must do.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from drillai.engines.contract import ConstraintViolation, EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field

WATER_DENSITY_SI = 1000.0
GRAVITY = 9.80665
DEFAULT_DISCHARGE_COEFFICIENT = 0.95
CRITICAL_REYNOLDS = 2100.0
CUTTING_DENSITY_SI = 2600.0  # kg/m3, sandstone/shale average
DEFAULT_CUTTING_DIAMETER_SI = 0.006  # 6 mm


class TubularElement(BaseModel):
    """A tubular or hole segment, defined by top/bottom measured depth and diameters."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal[
        "hole", "open_hole", "casing", "liner", "riser", "drillpipe", "hwdp", "drillcollar", "tubing", "bha"
    ]
    name: str | None = None
    from_depth_si: float = quantity_field(0.0, "m", ge=0.0)
    to_depth_si: float = quantity_field(0.0, "m", ge=0.0)
    od_si: float = quantity_field(..., "m", gt=0.0, description="outer diameter")
    id_si: float | None = quantity_field(None, "m", gt=0.0, description="inner diameter (hole has no id)")
    linear_mass_si: float | None = quantity_field(None, "kg/m", description="nominal linear mass (used by T&D)")

    @model_validator(mode="after")
    def _check_depths(self) -> TubularElement:
        if self.to_depth_si <= self.from_depth_si:
            raise ValueError(f"{self.kind} element must have to_depth > from_depth ({self.name or ''})")
        if self.id_si is not None and self.id_si >= self.od_si:
            raise ValueError(f"{self.kind} element has id >= od ({self.name or ''})")
        return self

    @property
    def length_si(self) -> float:
        return self.to_depth_si - self.from_depth_si


class PumpSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output_per_stroke_si: float = quantity_field(0.0, "m3", ge=0.0, description="theoretical volume per stroke")
    strokes_per_minute: float | None = quantity_field(None, "1/min", gt=0.0)
    max_pressure_si: float | None = quantity_field(None, "Pa")
    count: int = Field(default=1, ge=1, le=6, description="number of active pumps")


class CapacityEngineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    elements: list[TubularElement] = Field(min_length=1)
    current_depth_si: float = quantity_field(0.0, "m", ge=0.0, description="bit depth (MD) for bottoms-up/lag")
    pump: PumpSpec | None = None
    flow_rate_si: float | None = quantity_field(None, "m3/s", gt=0.0)


class IntervalVolumes(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: str
    from_depth_si: float
    to_depth_si: float
    length_si: float
    string_capacity_si: float = 0.0
    annulus_capacity_si: float = 0.0
    string_volume_si: float = 0.0
    annulus_volume_si: float = 0.0
    annular_velocity_si: float | None = None


class CapacityEngineOutput(BaseModel):
    """Volumes are as-designed over the whole modelled geometry; bottoms-up is measured from
    ``current_depth_si`` (i.e. it answers "how long until the current bottom is clean")."""
    model_config = ConfigDict(extra="forbid")
    string_capacity_si: float
    annulus_capacity_si: float
    string_volume_si: float
    annulus_volume_si: float
    total_volume_si: float
    string_strokes: float | None = None
    annulus_strokes: float | None = None
    total_strokes: float | None = None
    bottoms_up_strokes: float | None = None
    bottoms_up_volume_si: float | None = None
    bottoms_up_time_si: float | None = None
    circulation_time_si: float | None = None
    intervals: list[IntervalVolumes]
    assumptions: list[str]


class HydraulicsEngineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    elements: list[TubularElement] = Field(min_length=1)
    flow_rate_si: float = quantity_field(..., "m3/s", gt=0.0)
    mud_weight_si: float = quantity_field(..., "kg/m3", gt=0.0)
    plastic_viscosity_si: float = quantity_field(0.0, "Pa.s", ge=0.0, description="PV (Fann 600 − 300 reading)")
    yield_point_si: float = quantity_field(0.0, "Pa.yp", ge=0.0, description="YP (Fann 300 − PV)")
    bit_depth_si: float = quantity_field(..., "m", gt=0.0)
    bit_tvd_si: float | None = quantity_field(None, "m", gt=0.0, description="true vertical depth of the bit (ECD)")
    bit_diameter_si: float | None = quantity_field(None, "m")
    tfa_si: float | None = quantity_field(None, "m2", gt=0.0, description="total flow area")
    nozzle_count: int | None = Field(default=None, ge=1, le=24)
    discharge_coefficient: float = quantity_field(DEFAULT_DISCHARGE_COEFFICIENT, "1", gt=0.0, le=1.0)
    rop_si: float | None = quantity_field(None, "m/h", gt=0.0)
    cutting_diameter_si: float | None = quantity_field(None, "m", gt=0.0)
    cutting_density_si: float | None = quantity_field(None, "kg/m3", gt=0.0)
    ecd_limit_si: float | None = quantity_field(None, "kg/m3")
    fracture_gradient_si: float | None = quantity_field(None, "Pa/m")
    pore_pressure_gradient_si: float | None = quantity_field(None, "Pa/m")


class HydraulicsStage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    kind: str
    from_depth_si: float
    to_depth_si: float
    length_si: float
    hydraulic_diameter_si: float
    flow_area_si: float
    velocity_si: float
    pressure_loss_si: float
    pressure_loss_gradient_si: float
    reynolds_number: float
    regime: str
    annular_velocity_si: float | None = None


class HydraulicsEngineOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    flow_rate_si: float
    flow_rate_lpm: float
    mud_weight_si: float
    bit_diameter_si: float
    bit_tvd_si: float
    tfa_si: float
    nozzle_equivalent_diameter_si: float | None = None
    bit_pressure_drop_si: float
    string_pressure_loss_si: float
    annulus_pressure_loss_si: float
    total_pressure_loss_si: float
    standpipe_pressure_expected_si: float
    bit_hydraulic_horsepower_si: float
    hydraulic_horsepower_per_area_si: float
    jet_velocity_si: float
    jet_impact_force_si: float
    ecd_bottom_si: float
    ecd_margin_si: float | None = None
    ecd_profile: list[dict]
    annular_velocity_max_si: float
    annular_velocity_min_si: float
    slip_velocity_si: float | None = None
    cutting_transport_ratio: float | None = None
    cuttings_concentration_volume_fraction: float | None = None
    turbulent_intervals: list[str]
    stages: list[HydraulicsStage] = []
    rheology_model: str = "bingham_plastic_laminar_exact"
    assumptions: list[str]


# --------------------------------------------------------------------------- geometry
_INNER_KINDS = ("drillpipe", "hwdp", "drillcollar", "tubing", "bha")
_OUTER_KINDS = ("hole", "open_hole", "casing", "liner", "riser")


def _boundaries(elements: list[TubularElement]) -> list[float]:
    points = {0.0}
    for element in elements:
        points.add(round(element.from_depth_si, 9))
        points.add(round(element.to_depth_si, 9))
    return sorted(points)


def _intervals(elements: list[TubularElement]) -> list[tuple[float, float]]:
    points = _boundaries(elements)
    return [(points[i], points[i + 1]) for i in range(len(points) - 1) if points[i + 1] > points[i]]


def _outer_id(element: TubularElement) -> float | None:
    """Inner diameter of a conduit: the bore of a hole is its diameter; casing uses its ID."""
    if element.kind in ("hole", "open_hole"):
        return element.od_si
    return element.id_si


def _volumes(elements: list[TubularElement], flow_rate_si: float | None) -> tuple[list[IntervalVolumes], float, float]:
    intervals: list[IntervalVolumes] = []
    string_volume = 0.0
    annulus_volume = 0.0
    for top, bottom in _intervals(elements):
        length = bottom - top
        inners = [e for e in elements if e.kind in _INNER_KINDS and e.from_depth_si < bottom and e.to_depth_si > top]
        outers = [e for e in elements if e.kind in _OUTER_KINDS and e.from_depth_si < bottom and e.to_depth_si > top]
        inner = max(inners, key=lambda e: e.od_si) if inners else None
        outer = min(outers, key=lambda e: _outer_id(e) or 0.0) if outers else None

        string_capacity = math.pi / 4.0 * (inner.id_si**2 if inner and inner.id_si else 0.0)
        annulus_capacity = 0.0
        if outer is not None and _outer_id(outer):
            outer_bore = _outer_id(outer) or 0.0
            hole_area = math.pi / 4.0 * outer_bore**2
            pipe_area = math.pi / 4.0 * (inner.od_si**2 if inner else 0.0)
            annulus_capacity = max(0.0, hole_area - pipe_area)

        string_volume += string_capacity * length
        annulus_volume += annulus_capacity * length
        velocity = (flow_rate_si / annulus_capacity) if (flow_rate_si and annulus_capacity > 0) else None

        intervals.append(
            IntervalVolumes(
                name=(inner.name if inner and inner.name else (outer.name if outer and outer.name else "interval")),
                kind=f"{outer.kind if outer else 'open'}/{inner.kind if inner else 'open'}",
                from_depth_si=top,
                to_depth_si=bottom,
                length_si=length,
                string_capacity_si=string_capacity,
                annulus_capacity_si=annulus_capacity,
                string_volume_si=string_capacity * length,
                annulus_volume_si=annulus_capacity * length,
                annular_velocity_si=velocity,
            )
        )
    return intervals, string_volume, annulus_volume


def _span(elements: list[TubularElement]) -> float:
    return max(e.to_depth_si for e in elements) - min(e.from_depth_si for e in elements)


def _clip(elements: list[TubularElement], depth_si: float) -> list[TubularElement]:
    clipped = [
        element.model_copy(
            update={
                "from_depth_si": min(element.from_depth_si, depth_si),
                "to_depth_si": min(element.to_depth_si, depth_si),
            }
        )
        for element in elements
        if element.from_depth_si < depth_si
    ]
    return [element for element in clipped if element.to_depth_si > element.from_depth_si]


# --------------------------------------------------------------------------- engine 1
def compute_capacity(inputs: CapacityEngineInput) -> EngineResult:
    intervals, string_volume, annulus_volume = _volumes(inputs.elements, inputs.flow_rate_si)
    warnings: list[str] = []
    span = _span(inputs.elements)

    string_strokes = annulus_strokes = bottoms_up_strokes = None
    bottoms_up_volume = bottoms_up_time = circulation_time = None
    output_per_stroke = inputs.pump.output_per_stroke_si if inputs.pump else 0.0
    if output_per_stroke and output_per_stroke > 0:
        string_strokes = string_volume / output_per_stroke
        annulus_strokes = annulus_volume / output_per_stroke
        if inputs.current_depth_si > 0:
            _, bottoms_up_volume = _annulus_volume_below(inputs.elements, inputs.current_depth_si)
            bottoms_up_strokes = bottoms_up_volume / output_per_stroke
    else:
        warnings.append("no pump output provided: strokes and bottoms-up counts are unavailable")

    if inputs.pump and inputs.pump.strokes_per_minute:
        stroke_rate = inputs.pump.strokes_per_minute * inputs.pump.count
        if bottoms_up_strokes is not None:
            bottoms_up_time = bottoms_up_strokes / stroke_rate * 60.0
        if string_strokes is not None:
            circulation_time = (string_strokes + (annulus_strokes or 0.0)) / stroke_rate * 60.0
    if inputs.flow_rate_si:
        circulation_time = (string_volume + annulus_volume) / inputs.flow_rate_si

    outputs = CapacityEngineOutput(
        string_capacity_si=(string_volume / span) if span else 0.0,
        annulus_capacity_si=(annulus_volume / span) if span else 0.0,
        string_volume_si=string_volume,
        annulus_volume_si=annulus_volume,
        total_volume_si=string_volume + annulus_volume,
        string_strokes=string_strokes,
        annulus_strokes=annulus_strokes,
        total_strokes=((string_strokes or 0.0) + (annulus_strokes or 0.0)) if string_strokes is not None else None,
        bottoms_up_strokes=bottoms_up_strokes,
        bottoms_up_volume_si=bottoms_up_volume,
        bottoms_up_time_si=bottoms_up_time,
        circulation_time_si=circulation_time,
        intervals=intervals,
        assumptions=[
            "volumes from nominal diameters (no tool-joint upset allowance)",
            "annulus = bore² − largest inner tubular OD² per interval",
            "hole enlargement/washout not included",
            "string/annulus volumes cover the full modelled geometry; bottoms-up is clipped to the current bit depth",
        ],
    )
    return EngineResult(outputs=outputs, warnings=warnings, assumptions_applied=outputs.assumptions)


def _annulus_volume_below(elements: list[TubularElement], depth_si: float) -> tuple[list[IntervalVolumes], float]:
    clipped = _clip(elements, depth_si)
    if not clipped:
        return [], 0.0
    intervals, _, annulus = _volumes(clipped, None)
    return intervals, annulus


# --------------------------------------------------------------------------- engine 2
def _bingham_pipe_flow_rate(dpdl: float, diameter: float, mu_p: float, tau_y: float) -> float:
    """Exact laminar Bingham-plastic pipe flow rate for a given pressure gradient."""
    if dpdl <= 0:
        return 0.0
    tau_w = diameter * dpdl / 4.0
    if tau_w <= tau_y:
        return 0.0
    x = tau_y / tau_w
    newtonian = math.pi * diameter**4 * dpdl / (128.0 * mu_p)
    return newtonian * (1.0 - (4.0 / 3.0) * x + (1.0 / 3.0) * x**4)


def _bingham_pipe_dpdl(flow_rate: float, diameter: float, mu_p: float, tau_y: float) -> float:
    """Solve the implicit Bingham-plastic pipe equation for dP/dL by bisection."""
    if flow_rate <= 0:
        return 0.0
    if mu_p <= 0:
        mu_p = 1e-4
    low, high = 1e-6, 1e7
    for _ in range(200):
        mid = 0.5 * (low + high)
        if _bingham_pipe_flow_rate(mid, diameter, mu_p, tau_y) < flow_rate:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


def _bingham_slot_flow_rate(dpdl: float, gap: float, width: float, mu_p: float, tau_y: float) -> float:
    """Exact laminar Bingham-plastic slot (annulus) flow rate for a pressure gradient."""
    if dpdl <= 0:
        return 0.0
    shear_ratio = 2.0 * tau_y / (gap * dpdl)
    if shear_ratio >= 1.0:
        return 0.0
    newtonian = width * gap**3 * dpdl / (12.0 * mu_p)
    return newtonian * (1.0 - 1.5 * shear_ratio + 0.5 * shear_ratio**3)


def _bingham_slot_dpdl(flow_rate: float, gap: float, width: float, mu_p: float, tau_y: float) -> float:
    if flow_rate <= 0:
        return 0.0
    if mu_p <= 0:
        mu_p = 1e-4
    low, high = 1e-6, 1e7
    for _ in range(200):
        mid = 0.5 * (low + high)
        if _bingham_slot_flow_rate(mid, gap, width, mu_p, tau_y) < flow_rate:
            low = mid
        else:
            high = mid
    return 0.5 * (low + high)


def _reynolds(velocity: float, hydraulic_diameter: float, density: float, mu_p: float) -> float:
    if mu_p <= 0:
        return float("inf")
    return density * velocity * hydraulic_diameter / mu_p


def compute_hydraulics(inputs: HydraulicsEngineInput) -> EngineResult:
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []
    turbulent_intervals: list[str] = []

    flow = inputs.flow_rate_si
    mu_p = max(inputs.plastic_viscosity_si, 1e-6)
    tau_y = inputs.yield_point_si

    bit_diameter = inputs.bit_diameter_si or _deepest_hole_diameter(inputs.elements, inputs.bit_depth_si)
    if bit_diameter is None:
        raise ValueError("bit diameter not supplied and no hole element is present in the geometry")
    bit_tvd = inputs.bit_tvd_si or inputs.bit_depth_si
    if inputs.bit_tvd_si is None:
        warnings.append("bit TVD not supplied: ECD is referenced to MD and will be optimistic in inclined hole")

    stages: list[HydraulicsStage] = []
    string_loss = annulus_loss = 0.0
    annular_velocities: list[float] = []
    ecd_profile: list[dict] = []

    clipped = _clip(inputs.elements, inputs.bit_depth_si)
    for top, bottom in _intervals(clipped):
        length = bottom - top
        if length <= 0:
            continue
        inners = [e for e in inputs.elements if e.kind in _INNER_KINDS and e.from_depth_si < bottom and e.to_depth_si > top]
        outers = [e for e in inputs.elements if e.kind in _OUTER_KINDS and e.from_depth_si < bottom and e.to_depth_si > top]
        inner = max(inners, key=lambda e: e.od_si) if inners else None
        outer = min(outers, key=lambda e: _outer_id(e) or 1e9) if outers else None

        if inner and inner.id_si:
            bore = inner.id_si
            area = math.pi / 4.0 * bore**2
            velocity = flow / area
            gradient = _bingham_pipe_dpdl(flow, bore, mu_p, tau_y)
            reynolds = _reynolds(velocity, bore, inputs.mud_weight_si, mu_p)
            regime = "laminar" if reynolds < CRITICAL_REYNOLDS else "turbulent_assumption_violated"
            if regime != "laminar":
                turbulent_intervals.append(f"string/{inner.kind}@{top:.0f}m")
                warnings.append(
                    f"string interval at {top:.0f} m has Re={reynolds:.0f}: laminar assumption is violated"
                )
            loss = gradient * length
            string_loss += loss
            stages.append(
                HydraulicsStage(
                    name=inner.name or "string",
                    kind=f"string/{inner.kind}",
                    from_depth_si=top,
                    to_depth_si=bottom,
                    length_si=length,
                    hydraulic_diameter_si=bore,
                    flow_area_si=area,
                    velocity_si=velocity,
                    pressure_loss_si=loss,
                    pressure_loss_gradient_si=gradient,
                    reynolds_number=reynolds,
                    regime=regime,
                )
            )

        if outer is not None:
            bore = _outer_id(outer) or 0.0
            pipe_od = inner.od_si if inner else 0.0
            if bore > pipe_od > 0:
                gap = (bore - pipe_od) / 2.0
                width = math.pi * (bore + pipe_od) / 2.0
                area = math.pi / 4.0 * (bore**2 - pipe_od**2)
                velocity = flow / area
                gradient = _bingham_slot_dpdl(flow, gap, width, mu_p, tau_y)
                hydraulic_diameter = bore - pipe_od
                reynolds = _reynolds(velocity, hydraulic_diameter, inputs.mud_weight_si, mu_p)
                regime = "laminar" if reynolds < CRITICAL_REYNOLDS else "turbulent_assumption_violated"
                if regime != "laminar":
                    turbulent_intervals.append(f"annulus/{outer.kind}@{top:.0f}m")
                    warnings.append(
                        f"annulus interval at {top:.0f} m has Re={reynolds:.0f}: laminar assumption is violated"
                    )
                loss = gradient * length
                annulus_loss += loss
                annular_velocities.append(velocity)
                stages.append(
                    HydraulicsStage(
                        name=outer.name or f"annulus-{outer.kind}",
                        kind=f"annulus/{outer.kind}",
                        from_depth_si=top,
                        to_depth_si=bottom,
                        length_si=length,
                        hydraulic_diameter_si=hydraulic_diameter,
                        flow_area_si=area,
                        velocity_si=velocity,
                        pressure_loss_si=loss,
                        pressure_loss_gradient_si=gradient,
                        reynolds_number=reynolds,
                        regime=regime,
                        annular_velocity_si=velocity,
                    )
                )
                ecd_profile.append(
                    {
                        "md_si": round(bottom, 3),
                        "annulus_pressure_loss_si": round(loss, 3),
                        "annular_velocity_si": round(velocity, 6),
                    }
                )

    tfa = inputs.tfa_si or 0.0
    discharge = inputs.discharge_coefficient
    if tfa > 0:
        bit_pressure_drop = inputs.mud_weight_si * flow**2 / (2.0 * (discharge * tfa) ** 2)
        jet_velocity = flow / tfa
        jet_impact_force = inputs.mud_weight_si * flow * jet_velocity
        bit_hhp = flow * bit_pressure_drop
        # Hydraulic specific impact (HSI): bit hydraulic power per unit bit cross-section,
        # i.e. the SI expression of hp/in2 of hole area.
        bit_area = math.pi / 4.0 * bit_diameter**2
        hhp_per_area = bit_hhp / bit_area if bit_area > 0 else 0.0
        equivalent_nozzle_diameter = (
            math.sqrt(4.0 * tfa / (math.pi * inputs.nozzle_count)) if inputs.nozzle_count else None
        )
    else:
        bit_pressure_drop = jet_velocity = jet_impact_force = bit_hhp = hhp_per_area = 0.0
        equivalent_nozzle_diameter = None
        warnings.append("no TFA supplied: bit pressure drop, jet velocity, impact force and bit HHP not computed")

    total_loss = string_loss + annulus_loss + bit_pressure_drop
    ecd_si = inputs.mud_weight_si + (annulus_loss / (GRAVITY * bit_tvd)) if bit_tvd else inputs.mud_weight_si

    transport_ratio = cuttings_concentration = slip_velocity = None
    if inputs.rop_si and annular_velocities:
        minimum_velocity = min(annular_velocities)
        cutting_diameter = inputs.cutting_diameter_si or DEFAULT_CUTTING_DIAMETER_SI
        cutting_density = inputs.cutting_density_si or CUTTING_DENSITY_SI
        slip_velocity = _settling_velocity(cutting_diameter, cutting_density, inputs.mud_weight_si, mu_p)
        transport_ratio = max(0.0, min(1.0, (minimum_velocity - slip_velocity) / minimum_velocity))
        cuttings_concentration = _cuttings_concentration(
            hole_diameter_si=bit_diameter,
            pipe_od_si=(max((e.od_si for e in inputs.elements if e.kind in _INNER_KINDS), default=0.0)),
            rop_m_per_h=inputs.rop_si,
            annular_velocity_si=minimum_velocity,
        )
        if transport_ratio < 0.5:
            violations.append(
                ConstraintViolation(
                    name="hole_cleaning_transport_ratio",
                    message=(
                        f"cuttings transport ratio {transport_ratio:.2f} is low "
                        f"(annular velocity {minimum_velocity:.2f} m/s vs slip velocity {slip_velocity:.2f} m/s)"
                    ),
                    severity="warning",
                    limit=0.5,
                    actual=transport_ratio,
                    source="settling-velocity balance",
                )
            )

    margin = None
    if inputs.ecd_limit_si is not None:
        margin = inputs.ecd_limit_si - ecd_si
        if ecd_si > inputs.ecd_limit_si:
            violations.append(
                ConstraintViolation(
                    name="ecd_limit",
                    message=f"ECD {ecd_si:.1f} kg/m3 exceeds the limit {inputs.ecd_limit_si:.1f} kg/m3",
                    severity="error",
                    limit=inputs.ecd_limit_si,
                    actual=ecd_si,
                    unit="kg/m3",
                )
            )
    if inputs.fracture_gradient_si is not None:
        limit_mw = inputs.fracture_gradient_si / GRAVITY
        if ecd_si > limit_mw:
            violations.append(
                ConstraintViolation(
                    name="fracture_gradient",
                    message=f"ECD {ecd_si:.1f} kg/m3 exceeds fracture-gradient equivalent {limit_mw:.1f} kg/m3",
                    severity="error",
                    limit=limit_mw,
                    actual=ecd_si,
                    unit="kg/m3",
                )
            )
        elif margin is None:
            margin = limit_mw - ecd_si
    if inputs.pore_pressure_gradient_si is not None:
        min_mw = inputs.pore_pressure_gradient_si / GRAVITY
        if ecd_si < min_mw:
            violations.append(
                ConstraintViolation(
                    name="pore_pressure",
                    message=f"ECD {ecd_si:.1f} kg/m3 is below pore-pressure equivalent {min_mw:.1f} kg/m3",
                    severity="error",
                    limit=min_mw,
                    actual=ecd_si,
                    unit="kg/m3",
                )
            )

    if turbulent_intervals:
        violations.append(
            ConstraintViolation(
                name="laminar_assumption",
                message=f"{len(turbulent_intervals)} interval(s) exceed Re {CRITICAL_REYNOLDS:.0f}: laminar model is optimistic",
                severity="warning",
                source="Reynolds check",
            )
        )

    outputs = HydraulicsEngineOutput(
        flow_rate_si=flow,
        flow_rate_lpm=flow * 60000.0,
        mud_weight_si=inputs.mud_weight_si,
        bit_diameter_si=bit_diameter,
        bit_tvd_si=bit_tvd,
        tfa_si=tfa,
        nozzle_equivalent_diameter_si=equivalent_nozzle_diameter,
        bit_pressure_drop_si=bit_pressure_drop,
        string_pressure_loss_si=string_loss,
        annulus_pressure_loss_si=annulus_loss,
        total_pressure_loss_si=total_loss,
        standpipe_pressure_expected_si=total_loss,
        bit_hydraulic_horsepower_si=bit_hhp,
        hydraulic_horsepower_per_area_si=hhp_per_area,
        jet_velocity_si=jet_velocity,
        jet_impact_force_si=jet_impact_force,
        ecd_bottom_si=ecd_si,
        ecd_margin_si=margin,
        ecd_profile=ecd_profile,
        annular_velocity_max_si=max(annular_velocities) if annular_velocities else 0.0,
        annular_velocity_min_si=min(annular_velocities) if annular_velocities else 0.0,
        slip_velocity_si=slip_velocity,
        cutting_transport_ratio=transport_ratio,
        cuttings_concentration_volume_fraction=cuttings_concentration,
        turbulent_intervals=turbulent_intervals,
        stages=stages,
        assumptions=[
            "laminar Bingham-plastic flow solved exactly (implicit equation, bisection)",
            "annulus represented by the slot approximation with mean circumference",
            "bit modelled as an orifice with a declared discharge coefficient",
            "no surface line losses, no tool-joint upsets, no eccentricity",
            "ECD referenced to the supplied bit TVD (MD fallback is flagged)",
            "cuttings settling uses a drag-balance settling velocity for the nominal cutting size",
        ],
    )
    return EngineResult(outputs=outputs, warnings=warnings, violations=violations, assumptions_applied=outputs.assumptions)


def _deepest_hole_diameter(elements: list[TubularElement], depth_si: float) -> float | None:
    holes = [e for e in elements if e.kind in ("hole", "open_hole") and e.from_depth_si < depth_si <= e.to_depth_si]
    if not holes:
        holes = [e for e in elements if e.kind in ("hole", "open_hole")]
    if not holes:
        return None
    return max(holes, key=lambda e: e.to_depth_si).od_si


def _settling_velocity(
    particle_diameter: float, particle_density: float, fluid_density: float, mu_p: float
) -> float:
    """Drag-balance settling velocity (Stokes for Re<1, intermediate otherwise)."""
    if fluid_density <= 0:
        return 0.0
    density_difference = max(particle_density - fluid_density, 0.0)
    if density_difference <= 0:
        return 0.0
    # Iterate on the drag coefficient: C_d = 24/Re for Re<1, else Schiller-Naumann approximation.
    velocity = particle_diameter**2 * density_difference * GRAVITY / (18.0 * mu_p)
    for _ in range(50):
        reynolds = fluid_density * velocity * particle_diameter / mu_p
        if reynolds <= 0:
            break
        if reynolds < 1.0:
            drag = 24.0 / reynolds
        else:
            drag = (24.0 / reynolds) * (1.0 + 0.15 * reynolds**0.687)
        new_velocity = math.sqrt(
            max(4.0 * particle_diameter * density_difference * GRAVITY / (3.0 * drag * fluid_density), 0.0)
        )
        if abs(new_velocity - velocity) < 1e-6:
            velocity = new_velocity
            break
        velocity = 0.5 * velocity + 0.5 * new_velocity
    return velocity


def _cuttings_concentration(
    *, hole_diameter_si: float, pipe_od_si: float, rop_m_per_h: float, annular_velocity_si: float
) -> float:
    if annular_velocity_si <= 0 or pipe_od_si <= 0:
        return 0.0
    annulus_area = math.pi / 4.0 * (hole_diameter_si**2 - pipe_od_si**2)
    if annulus_area <= 0:
        return 0.0
    cuttings_rate = (rop_m_per_h / 3600.0) * math.pi / 4.0 * hole_diameter_si**2
    return max(0.0, min(1.0, cuttings_rate / (annular_velocity_si * annulus_area)))


CAPACITY_SPEC = EngineSpec(
    key="wellbore.capacity",
    name="Wellbore capacity, strokes and bottoms-up",
    version="1.1.0",
    domain_pack="Hydraulics Pack",
    category="hydraulics",
    summary="String and annulus volumes, pump strokes, bottoms-up volume/strokes/time and circulation time.",
    inputs_model=CapacityEngineInput,
    outputs_model=CapacityEngineOutput,
    consumes=("wellbore.geometry", "drillstring.assembly", "pump.parameters"),
    produces=("wellbore.volumes", "hydraulics.profile"),
    parameters={"annulus_model": {"value": "concentric_largest_od", "description": "annulus simplification"}},
    assumptions=("nominal diameters, no tool-joint allowance", "largest inner tubular OD per interval"),
    limitations=("no eccentricity or washout", "does not resolve multiple nested strings beyond the largest OD"),
    references=("standard volumetric capacity relations", "API RP 13D (context)"),
    tags=("hydraulics", "volumes", "operations"),
    validation_status="verified_against_reference",
    action_level="L1",
)

HYDRAULICS_SPEC = EngineSpec(
    key="hydraulics.laminar",
    name="Circulation hydraulics (laminar Bingham plastic)",
    version="1.2.0",
    domain_pack="Hydraulics Pack",
    category="hydraulics",
    summary=(
        "Exact laminar Bingham-plastic pressure losses, orifice-based bit pressure drop, jet impact force, "
        "bit HHP, ECD, annular velocities, cuttings settling/transport with pore-pressure and fracture checks."
    ),
    inputs_model=HydraulicsEngineInput,
    outputs_model=HydraulicsEngineOutput,
    consumes=("wellbore.geometry", "drillstring.assembly", "mud.properties", "bit.spec", "pump.parameters"),
    produces=("hydraulics.profile",),
    parameters={
        "rheology": {"value": "bingham_plastic", "description": "PV/YP rheology model"},
        "discharge_coefficient": {"value": DEFAULT_DISCHARGE_COEFFICIENT, "description": "nozzle orifice coefficient"},
        "critical_reynolds": {"value": CRITICAL_REYNOLDS, "description": "laminar/turbulent boundary used for warnings"},
    },
    assumptions=(
        "laminar Bingham-plastic rheology (PV/YP)",
        "slot approximation for the annulus",
        "clean concentric geometry, no surface line losses",
        "cuttings are spherical with the declared density and diameter",
    ),
    limitations=(
        "turbulent flow is detected (Reynolds) but not modelled: results are optimistic in that regime",
        "Herschel-Bulkley / power-law fluids are not supported yet",
        "no temperature/pressure-dependent density, no eccentricity, no surge/swab",
        "no motor/BHA pressure drop unless supplied as an element",
    ),
    references=(
        "Bourgoyne et al., Applied Drilling Engineering (laminar Bingham-plastic relations)",
        "API RP 13D — Rheology and Hydraulics of Oil-Well Drilling Fluids (framework)",
    ),
    tags=("hydraulics", "ecd", "hole-cleaning", "nozzles"),
    action_level="L1",
)

register_engine(FunctionEngine(CAPACITY_SPEC, compute_capacity), aliases=("capacity", "volumes"))
register_engine(FunctionEngine(HYDRAULICS_SPEC, compute_hydraulics), aliases=("hydraulics", "circulation"))
