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
from datetime import datetime
from itertools import pairwise
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from drillai.core.errors import EngineConvergenceError, EngineInputInvalid
from drillai.engines.contract import ConstraintViolation, EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field

WATER_DENSITY_SI = 1000.0
GRAVITY = 9.80665
CRITICAL_REYNOLDS = 2100.0


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
    count: int = Field(default=1, ge=1, le=6, description="number of active pumps", json_schema_extra={"unit": "1"})


class OpenBoreDeclaration(BaseModel):
    """A caller-declared unoccupied interval, not a system-verified field observation."""

    model_config = ConfigDict(extra="forbid")
    from_depth_si: float = quantity_field(..., "m", ge=0.0)
    to_depth_si: float = quantity_field(..., "m", gt=0.0)
    declared_by: str = Field(min_length=1, max_length=200, description="Caller-supplied identity; not independently authenticated")
    declared_at: datetime = Field(description="Timezone-aware time of human declaration")
    basis: str = Field(min_length=1, max_length=1000, description="Human-declared supporting source or review basis")

    @model_validator(mode="after")
    def _check_declaration(self) -> OpenBoreDeclaration:
        if self.to_depth_si <= self.from_depth_si:
            raise ValueError("open bore declaration must have positive length")
        if not self.declared_by.strip() or not self.basis.strip():
            raise ValueError("open bore declaration requires identity and basis")
        if self.declared_at.tzinfo is None or self.declared_at.utcoffset() is None:
            raise ValueError("open bore declaration timestamp requires a timezone")
        return self


class CapacityEngineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    elements: list[TubularElement] = Field(min_length=1)
    current_depth_si: float = quantity_field(0.0, "m", ge=0.0, description="bit depth (MD) for bottoms-up/lag")
    pump: PumpSpec | None = None
    flow_rate_si: float | None = quantity_field(None, "m3/s", gt=0.0)
    open_bore_declarations: list[OpenBoreDeclaration] = Field(
        default_factory=list,
        description="Explicit per-interval human declarations, not independently verified geometry.",
    )


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


class DepthReference(BaseModel):
    """Caller-declared basis; values must already be normalized, no transformation is inferred."""

    model_config = ConfigDict(extra="forbid")
    md_origin: str = Field(min_length=1, max_length=200)
    tvd_origin: str = Field(min_length=1, max_length=200)
    vertical_datum: str = Field(min_length=1, max_length=200)
    coordinate_reference: str = Field(min_length=1, max_length=200)
    survey_reference: str = Field(min_length=1, max_length=200)
    source_reference: str = Field(min_length=1, max_length=500)
    depth_positive: Literal["down"]

    @model_validator(mode="after")
    def _check_reference(self) -> DepthReference:
        for value in (self.md_origin, self.tvd_origin, self.vertical_datum, self.coordinate_reference,
                      self.survey_reference, self.source_reference):
            if not value.strip():
                raise ValueError("depth reference fields must be nonblank")
        if self.md_origin != self.tvd_origin:
            raise ValueError("MD and TVD origins must match; no datum conversion is performed")
        return self


class HydraulicsEngineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    elements: list[TubularElement] = Field(min_length=1)
    flow_rate_si: float = quantity_field(..., "m3/s", gt=0.0)
    mud_weight_si: float = quantity_field(..., "kg/m3", gt=0.0)
    plastic_viscosity_si: float = quantity_field(..., "Pa.s", gt=0.0, description="PV (Fann 600 − 300 reading)")
    yield_point_si: float = quantity_field(0.0, "Pa.yp", ge=0.0, description="YP (Fann 300 − PV)")
    bit_depth_si: float = quantity_field(..., "m", gt=0.0)
    bit_tvd_si: float = quantity_field(..., "m", gt=0.0, description="true vertical depth from the declared origin (ECD); must not exceed measured depth")
    depth_reference: DepthReference = Field(description="Caller-declared common MD/TVD origin, datum, frame, survey and source; no conversion is inferred")
    bit_diameter_si: float | None = quantity_field(None, "m", gt=0.0)
    tfa_si: float | None = quantity_field(None, "m2", gt=0.0, description="total flow area")
    nozzle_count: int | None = Field(default=None, ge=1, le=24, description="total number of bit nozzles", json_schema_extra={"unit": "1"})
    discharge_coefficient: float | None = quantity_field(None, "1", gt=0.0, le=1.0, description="explicit nozzle orifice coefficient, required with TFA")
    additional_downhole_pressure_loss_si: float | None = quantity_field(None, "Pa", ge=0.0, description="caller-declared aggregate pressure loss for unmodeled BHA, motor and tool joints at this flow; zero must be explicit")
    additional_loss_valid_flow_min_si: float | None = quantity_field(None, "m3/s", gt=0.0, description="optional lower flow bound for declared constant additional downhole loss")
    additional_loss_valid_flow_max_si: float | None = quantity_field(None, "m3/s", gt=0.0, description="optional upper flow bound for declared constant additional downhole loss")
    surface_pressure_loss_si: float | None = quantity_field(None, "Pa", ge=0.0, description="caller-declared surface circuit pressure loss or conservative bound; not independently verified")
    surface_loss_valid_flow_min_si: float | None = quantity_field(None, "m3/s", gt=0.0, description="optional lower flow bound for a caller-declared constant surface loss")
    surface_loss_valid_flow_max_si: float | None = quantity_field(None, "m3/s", gt=0.0, description="optional upper flow bound for a caller-declared constant surface loss")
    rop_si: float | None = quantity_field(None, "m/h", gt=0.0)
    cutting_diameter_si: float | None = quantity_field(None, "m", gt=0.0)
    cutting_density_si: float | None = quantity_field(None, "kg/m3", gt=0.0)
    ecd_limit_si: float | None = quantity_field(None, "kg/m3")
    fracture_gradient_si: float | None = quantity_field(None, "Pa/m")
    pore_pressure_gradient_si: float | None = quantity_field(None, "Pa/m")

    @model_validator(mode="after")
    def _require_nozzle_coefficient(self) -> HydraulicsEngineInput:
        if self.tfa_si is not None and self.discharge_coefficient is None:
            raise ValueError("TFA requires an explicit discharge coefficient")
        low, high = self.surface_loss_valid_flow_min_si, self.surface_loss_valid_flow_max_si
        if (low is None) != (high is None) or (low is not None and self.surface_pressure_loss_si is None):
            raise ValueError("surface loss flow bounds require both bounds and a surface loss")
        if low is not None and high is not None and (low > high or not low <= self.flow_rate_si <= high):
            raise ValueError("flow rate must lie within the declared surface loss flow range")
        low, high = self.additional_loss_valid_flow_min_si, self.additional_loss_valid_flow_max_si
        if (low is None) != (high is None) or (low is not None and self.additional_downhole_pressure_loss_si is None):
            raise ValueError("additional downhole loss flow bounds require both bounds and a loss")
        if low is not None and high is not None and (low > high or not low <= self.flow_rate_si <= high):
            raise ValueError("flow rate must lie within the declared additional downhole loss flow range")
        return self


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
    tfa_si: float | None
    nozzle_equivalent_diameter_si: float | None = None
    bit_pressure_drop_si: float | None
    string_pressure_loss_si: float
    annulus_pressure_loss_si: float
    total_pressure_loss_si: float | None
    standpipe_pressure_expected_si: float | None
    bit_hydraulic_horsepower_si: float | None
    hydraulic_horsepower_per_area_si: float | None
    jet_velocity_si: float | None
    jet_impact_force_si: float | None
    ecd_bottom_si: float
    ecd_margin_si: float | None = None
    ecd_profile: list[dict]
    annular_velocity_max_si: float
    annular_velocity_min_si: float
    slip_velocity_si: float | None = None
    cutting_transport_ratio: float | None = None
    cuttings_concentration_volume_fraction: float | None = None
    turbulent_intervals: list[str]
    regime_validity: str = "laminar_valid"
    stages: list[HydraulicsStage] = []
    rheology_model: str = "bingham_plastic_laminar_exact"
    assumptions: list[str]


# --------------------------------------------------------------------------- geometry
_INNER_KINDS = ("drillpipe", "hwdp", "drillcollar", "tubing", "bha")
_OUTER_KINDS = ("hole", "open_hole", "casing", "liner", "riser")


def _boundaries(elements: list[TubularElement]) -> list[float]:
    points = {0.0}
    for element in elements:
        points.add(element.from_depth_si)
        points.add(element.to_depth_si)
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
    _validate_capacity_geometry(inputs)
    intervals, string_volume, annulus_volume = _volumes(inputs.elements, inputs.flow_rate_si)
    warnings: list[str] = []
    span = _span(inputs.elements)

    string_strokes = annulus_strokes = bottoms_up_strokes = None
    bottoms_up_volume = _annulus_volume_below(inputs.elements, inputs.current_depth_si)[1] if inputs.current_depth_si > 0 else None
    bottoms_up_time = circulation_time = None
    if inputs.pump and inputs.pump.strokes_per_minute and inputs.flow_rate_si:
        implied_flow = inputs.pump.output_per_stroke_si * inputs.pump.strokes_per_minute * inputs.pump.count / 60.0
        if not math.isclose(implied_flow, inputs.flow_rate_si, rel_tol=1e-6, abs_tol=0.0):
            raise EngineInputInvalid("pump stroke rate and supplied flow rate disagree", details={"engine": "wellbore.capacity", "pump_flow_rate_si": implied_flow, "flow_rate_si": inputs.flow_rate_si})
    output_per_stroke = inputs.pump.output_per_stroke_si if inputs.pump else 0.0
    if output_per_stroke and output_per_stroke > 0:
        string_strokes = string_volume / output_per_stroke
        annulus_strokes = annulus_volume / output_per_stroke
        if inputs.current_depth_si > 0:
            assert bottoms_up_volume is not None
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
        if bottoms_up_volume is None and inputs.current_depth_si > 0:
            _, bottoms_up_volume = _annulus_volume_below(inputs.elements, inputs.current_depth_si)
        if bottoms_up_time is None and bottoms_up_volume is not None:
            bottoms_up_time = bottoms_up_volume / inputs.flow_rate_si
        circulation_time = (string_volume + annulus_volume) / inputs.flow_rate_si

    geometry_assumption = (
        "unoccupied intervals are covered by caller-supplied declarations; identity and basis are not independently verified"
        if inputs.open_bore_declarations else "a declared inner string covers the full model"
    )
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
            geometry_assumption,
            "volumes from nominal diameters (no tool-joint upset allowance)",
            "annulus = bore² − largest inner tubular OD² per interval",
            "hole enlargement/washout not included",
            "string/annulus volumes cover the full modelled geometry; bottoms-up is clipped to the current bit depth",
        ],
    )
    return EngineResult(outputs=outputs, warnings=warnings, assumptions_applied=outputs.assumptions)


def _validate_capacity_geometry(inputs: CapacityEngineInput, *, engine: str = "wellbore.capacity") -> None:
    """Require an unambiguous bore and string basis over the whole model, not just to the bit."""
    depth = max(max(e.to_depth_si for e in inputs.elements), inputs.current_depth_si)
    declarations = inputs.open_bore_declarations
    for declaration in declarations:
        if declaration.to_depth_si > depth:
            raise EngineInputInvalid("open bore declaration extends beyond model geometry", details={"engine": engine})
    boundaries = sorted({0.0, depth, *(p for e in inputs.elements for p in (e.from_depth_si, e.to_depth_si)),
                         *(p for d in declarations for p in (d.from_depth_si, d.to_depth_si))})
    for top, bottom in pairwise(boundaries):
        outers = [e for e in inputs.elements if e.kind in _OUTER_KINDS and e.from_depth_si < bottom and e.to_depth_si > top]
        inners = [e for e in inputs.elements if e.kind in _INNER_KINDS and e.from_depth_si < bottom and e.to_depth_si > top]
        details = {"engine": engine, "from_depth_si": top, "to_depth_si": bottom}
        if not outers or any(_outer_id(e) is None for e in outers):
            raise EngineInputInvalid("a known bore is required across the full model and current depth", details=details)
        if len(inners) > 1:
            raise EngineInputInvalid("overlapping inner tubulars are not supported", details=details)
        declared = [d for d in declarations if d.from_depth_si < bottom and d.to_depth_si > top]
        if len(declared) > 1 or (declared and inners):
            raise EngineInputInvalid("open bore declaration conflicts with another declaration or tubular", details=details)
        if not inners and not declared:
            raise EngineInputInvalid("missing inner geometry is unknown; declare the specific unoccupied interval", details=details)
        if inners and inners[0].id_si is None:
            raise EngineInputInvalid("a declared inner tubular requires an internal diameter", details=details)
        # The smallest known bore is the active annulus. Duplicate holes are ambiguous; nested
        # tubular conduits must fit into the next surrounding bore rather than silently overlap.
        if sum(e.kind in ("hole", "open_hole") for e in outers) > 1:
            raise EngineInputInvalid("overlapping hole segments are ambiguous", details=details)
        outer_by_bore = sorted(outers, key=lambda e: _outer_id(e) or 0.0)
        for inner_outer, outer_outer in pairwise(outer_by_bore):
            outer_bore = _outer_id(outer_outer)
            if outer_bore is None or inner_outer.od_si > outer_bore:
                raise EngineInputInvalid("nested outer tubulars do not fit inside their enclosing bore", details=details)
        bore = _outer_id(outer_by_bore[0])
        if bore is None or (inners and inners[0].od_si >= bore):
            raise EngineInputInvalid("inner tubular must fit inside the bore", details=details)


def _annulus_volume_below(elements: list[TubularElement], depth_si: float) -> tuple[list[IntervalVolumes], float]:
    clipped = _clip(elements, depth_si)
    # _intervals stops at the deepest supplied segment; include the requested bit depth so a
    # missing bottom segment cannot silently become a shorter bottoms-up calculation.
    boundaries = sorted({0.0, depth_si, *(p for e in clipped for p in (e.from_depth_si, e.to_depth_si))})
    for top, bottom in pairwise(boundaries):
        outers = [e for e in clipped if e.kind in _OUTER_KINDS and e.from_depth_si < bottom and e.to_depth_si > top]
        inners = [e for e in clipped if e.kind in _INNER_KINDS and e.from_depth_si < bottom and e.to_depth_si > top]
        if not outers or any(_outer_id(e) is None for e in outers):
            raise EngineInputInvalid("bottoms-up requires a known bore over the entire surface-to-bit interval", details={"engine": "wellbore.capacity", "from_depth_si": top, "to_depth_si": bottom})
        bore = min(_outer_id(e) for e in outers)  # type: ignore[type-var]
        if inners and max(e.od_si for e in inners) >= bore:
            raise EngineInputInvalid("inner tubular must fit inside the bore", details={"engine": "wellbore.capacity", "from_depth_si": top, "to_depth_si": bottom})
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
    return newtonian * (1.0 - x)**2 * (x*x + 2*x + 3) / 3.0


def _solve_gradient(flow_rate: float, flow_at_gradient, stage: str) -> float:
    """Bracket on both sides before bisection; fail closed if floating point cannot solve it."""
    def evaluate(gradient: float) -> float:
        try:
            value = flow_at_gradient(gradient)
        except (OverflowError, ZeroDivisionError) as exc:
            raise EngineConvergenceError("hydraulics gradient is numerically unrepresentable", details={"engine": "hydraulics.laminar", "stage": stage}) from exc
        if not math.isfinite(value) or value < 0:
            raise EngineConvergenceError("hydraulics gradient is numerically unrepresentable", details={"engine": "hydraulics.laminar", "stage": stage})
        return value

    low, high = 0.0, 1.0
    if evaluate(high) < flow_rate:
        for _ in range(1075):
            low = high
            high *= 2.0
            if not math.isfinite(high):
                break
            if evaluate(high) >= flow_rate:
                break
        else:
            high = float("inf")
        if not math.isfinite(high) or evaluate(high) < flow_rate:
            raise EngineConvergenceError("hydraulics gradient could not be bracketed", details={"engine": "hydraulics.laminar", "stage": stage})
    else:
        for _ in range(1075):
            candidate = high / 2.0
            if candidate == 0.0 or evaluate(candidate) < flow_rate:
                low = candidate
                break
            high = candidate
        else:
            raise EngineConvergenceError("hydraulics gradient could not be bracketed", details={"engine": "hydraulics.laminar", "stage": stage})

    for _ in range(100):
        mid = low + (high - low) / 2.0
        if mid in (low, high):
            break
        if evaluate(mid) < flow_rate:
            low = mid
        else:
            high = mid
    result = low + (high - low) / 2.0
    actual = evaluate(result)
    if result <= 0 or abs(actual / flow_rate - 1.0) > 1e-9:
        raise EngineConvergenceError("hydraulics gradient did not converge", details={"engine": "hydraulics.laminar", "stage": stage})
    return result


def _bingham_pipe_dpdl(flow_rate: float, diameter: float, mu_p: float, tau_y: float) -> float:
    return _solve_gradient(flow_rate, lambda gradient: _bingham_pipe_flow_rate(gradient, diameter, mu_p, tau_y), "pipe")


def _bingham_slot_flow_rate(dpdl: float, gap: float, width: float, mu_p: float, tau_y: float) -> float:
    """Exact laminar Bingham-plastic slot (annulus) flow rate for a pressure gradient."""
    if dpdl <= 0:
        return 0.0
    shear_ratio = 2.0 * tau_y / (gap * dpdl)
    if shear_ratio >= 1.0:
        return 0.0
    newtonian = width * gap**3 * dpdl / (12.0 * mu_p)
    return newtonian * (1.0 - shear_ratio)**2 * (shear_ratio + 2.0) / 2.0


def _bingham_slot_dpdl(flow_rate: float, gap: float, width: float, mu_p: float, tau_y: float) -> float:
    return _solve_gradient(flow_rate, lambda gradient: _bingham_slot_flow_rate(gradient, gap, width, mu_p, tau_y), "annulus")


def _reynolds(velocity: float, hydraulic_diameter: float, density: float, mu_p: float) -> float:
    if mu_p <= 0:
        return float("inf")
    return density * velocity * hydraulic_diameter / mu_p


def compute_hydraulics(inputs: HydraulicsEngineInput) -> EngineResult:
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []
    turbulent_intervals: list[str] = []

    flow = inputs.flow_rate_si
    mu_p = inputs.plastic_viscosity_si
    tau_y = inputs.yield_point_si

    bit_diameter = inputs.bit_diameter_si or _deepest_hole_diameter(inputs.elements, inputs.bit_depth_si)
    if bit_diameter is None:
        raise EngineInputInvalid("bit diameter is missing and no hole segment covers the bit depth", details={"engine": "hydraulics.laminar"})
    if inputs.bit_diameter_si is None:
        warnings.append("bit diameter assumed equal to the nominal hole diameter at bit depth; actual bit diameter is unverified")
    bit_tvd = inputs.bit_tvd_si
    if bit_tvd > inputs.bit_depth_si:
        raise EngineInputInvalid("bit TVD cannot exceed bit measured depth", details={"engine": "hydraulics.laminar"})
    clipped = _clip(inputs.elements, inputs.bit_depth_si)
    if not clipped:
        raise EngineInputInvalid("circulation path is missing", details={"engine": "hydraulics.laminar"})
    _validate_capacity_geometry(
        CapacityEngineInput(elements=clipped, current_depth_si=inputs.bit_depth_si),
        engine="hydraulics.laminar",
    )

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

    tfa = inputs.tfa_si
    discharge = inputs.discharge_coefficient
    if tfa is not None:
        assert discharge is not None  # enforced by input validation
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
        bit_pressure_drop = jet_velocity = jet_impact_force = bit_hhp = hhp_per_area = None
        equivalent_nozzle_diameter = None
        warnings.append("no TFA supplied: bit pressure drop, jet velocity, impact force and bit HHP not computed")

    # The downhole total includes modeled pipe/annular/nozzle losses and a caller-declared
    # aggregate for unmodeled downhole components. Missing values are not treated as zero.
    # Standpipe additionally needs a declared surface loss; declarations are not verified.
    extra_loss = inputs.additional_downhole_pressure_loss_si
    total_loss = string_loss + annulus_loss + bit_pressure_drop + extra_loss if bit_pressure_drop is not None and extra_loss is not None else None
    if extra_loss is None:
        warnings.append("additional downhole pressure loss not supplied: total loss and standpipe pressure are unavailable")
    else:
        warnings.append("additional downhole pressure loss and any declared flow range are caller assertions, not independently verified")
    surface_loss = inputs.surface_pressure_loss_si
    standpipe = total_loss + surface_loss if total_loss is not None and surface_loss is not None else None
    if surface_loss is None:
        warnings.append("surface circuit pressure loss not supplied: standpipe pressure is unavailable")
    else:
        warnings.append("surface circuit loss is caller-declared and is not independently verified; any flow-range bound is a caller assertion")
    warnings.append("bit ECD uses modeled annular loss only; additional declared downhole loss is not apportioned to the annulus")
    ecd_si = inputs.mud_weight_si + (annulus_loss / (GRAVITY * bit_tvd)) if bit_tvd else inputs.mud_weight_si

    transport_ratio = cuttings_concentration = slip_velocity = None
    if inputs.rop_si and (inputs.cutting_diameter_si is None or inputs.cutting_density_si is None):
        warnings.append("cuttings transport unavailable: cutting diameter and density must both be declared")
    if inputs.rop_si and annular_velocities and inputs.cutting_diameter_si is not None and inputs.cutting_density_si is not None:
        minimum_velocity = min(annular_velocities)
        cutting_diameter = inputs.cutting_diameter_si
        cutting_density = inputs.cutting_density_si
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
        standpipe_pressure_expected_si=standpipe,
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
        regime_validity="non_certified_turbulent_exceedance" if turbulent_intervals else "laminar_valid",
        stages=stages,
        assumptions=[
            "laminar Bingham-plastic flow solved exactly (implicit equation, bisection)",
            "annulus represented by the slot approximation with mean circumference",
            "bit modelled as an orifice with a declared discharge coefficient",
            "downhole total requires declared additional downhole losses and excludes surface losses; standpipe requires a declared surface loss",
            "no tool-joint upsets, no eccentricity",
            "ECD uses modeled annular loss only; additional downhole loss is not apportioned to the annulus",
            "ECD referenced to the supplied bit TVD and caller-declared common origin; depth reference is not independently verified",
            "cuttings settling uses a drag-balance settling velocity for the nominal cutting size",
        ],
    )
    return EngineResult(outputs=outputs, warnings=warnings, violations=violations, assumptions_applied=outputs.assumptions)


def _deepest_hole_diameter(elements: list[TubularElement], depth_si: float) -> float | None:
    holes = [e for e in elements if e.kind in ("hole", "open_hole") and e.from_depth_si < depth_si <= e.to_depth_si]
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
    version="1.4.0",
    domain_pack="Hydraulics Pack",
    category="hydraulics",
    summary="String and annulus volumes, pump strokes, bottoms-up volume/strokes/time and circulation time.",
    inputs_model=CapacityEngineInput,
    outputs_model=CapacityEngineOutput,
    consumes=("wellbore.geometry", "drillstring.assembly", "pump.parameters"),
    produces=("wellbore.volumes", "hydraulics.profile"),
    parameters={"annulus_model": {"value": "concentric_largest_od", "description": "annulus simplification"}},
    assumptions=("nominal diameters, no tool-joint allowance", "largest inner tubular OD per interval"),
    limitations=("no eccentricity or washout", "does not resolve multiple nested strings beyond the largest OD", "unoccupied intervals require an explicit user declaration, not independent verification"),
    references=("standard volumetric capacity relations", "API RP 13D (context)"),
    tags=("hydraulics", "volumes", "operations"),
    validation_status="formula_derived",
    action_level="L1",
)

HYDRAULICS_SPEC = EngineSpec(
    key="hydraulics.laminar",
    name="Circulation hydraulics (laminar Bingham plastic)",
    version="1.6.0",
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
        "critical_reynolds": {"value": CRITICAL_REYNOLDS, "description": "laminar/turbulent boundary used for warnings"},
    },
    assumptions=(
        "laminar Bingham-plastic rheology (PV/YP)",
        "slot approximation for the annulus",
        "clean concentric geometry; surface pressure loss is caller-declared at the specified flow rate",
        "cuttings are spherical with the declared density and diameter",
    ),
    limitations=(
        "turbulent flow is detected (Reynolds) but not modelled: results are optimistic in that regime",
        "Herschel-Bulkley / power-law fluids are not supported yet",
        "no temperature/pressure-dependent density, no eccentricity, no surge/swab",
        "motor/BHA and tool joint pressure losses and surface losses must be explicitly declared; eccentricity is not modeled",
        "total/standpipe unavailable without nozzle and additional downhole-loss input; standpipe also requires surface-loss input; declarations are not verified",
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
