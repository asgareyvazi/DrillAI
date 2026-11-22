"""Bounded, laminar power-law flow through a straight circular pipe only.

Rabinowitsch-Mooney relation for the wall shear rate and force balance: BYU, Chemical
Engineering Class 33, "More On Non-Newtonian Laminar Flow", equations 1 and 3–4,
https://uolab.groups.et.byu.net/files/pipevisco/hints/374_Class_33.pdf (no edition stated).
The generalized Reynolds number follows Metzner-Reed as presented by CheCalc,
https://www.checalc.com/fluid_flow_power_law.html (no edition stated). These describe a
mathematical pipe model, not a validation of a drilling-fluid sample or a whole well.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field, model_validator

from drillai.core.errors import EngineInputInvalid
from drillai.engines.contract import EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field


class PowerLawPipeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)

    diameter_si: float = quantity_field(..., "m", ge=1e-4, le=10.0, description="Straight circular pipe inside diameter")
    length_si: float = quantity_field(..., "m", gt=0.0, le=100000.0)
    flow_rate_si: float = quantity_field(..., "m3/s", ge=1e-9, le=100.0)
    density_si: float = quantity_field(..., "kg/m3", ge=1.0, le=10000.0)
    consistency_si: float = Field(
        ge=1e-8, le=1e8,
        description="Power-law consistency K in Pa·s^n; its physical dimension depends on flow_index. No automatic conversion.",
        json_schema_extra={"unit": "Pa.s^n"},
    )
    flow_index: float = quantity_field(..., "1", ge=0.2, le=1.5, description="Dimensionless power-law index n")
    measured_shear_min_per_s: float = quantity_field(..., "1/s", ge=0.0, le=1e8, description="Lower bound of caller-declared fit's shear-rate range")
    measured_shear_max_per_s: float = quantity_field(..., "1/s", gt=0.0, le=1e8, description="Upper bound of caller-declared fit's shear-rate range")

    @model_validator(mode="after")
    def validate_range(self) -> PowerLawPipeInput:
        if self.measured_shear_min_per_s >= self.measured_shear_max_per_s:
            raise ValueError("declared measured shear-rate bounds must be strictly increasing")
        return self


class PowerLawPipeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    velocity_si: float = quantity_field(..., "m/s")
    wall_shear_rate_per_s: float = quantity_field(..., "1/s", description="True wall shear rate")
    wall_shear_stress_si: float = quantity_field(..., "Pa")
    generalized_reynolds: float = quantity_field(..., "1")
    pressure_gradient_si: float = quantity_field(..., "Pa/m")
    pressure_loss_si: float = quantity_field(..., "Pa")


def compute_power_law_pipe(inputs: PowerLawPipeInput) -> EngineResult:
    """Compute only the distributed loss, rejecting extrapolated wall rates and high Re."""
    v = 4.0 * inputs.flow_rate_si / (math.pi * inputs.diameter_si**2)
    n = inputs.flow_index
    wall_rate = (8.0 * v / inputs.diameter_si) * ((3.0 * n + 1.0) / (4.0 * n))
    if not inputs.measured_shear_min_per_s <= wall_rate <= inputs.measured_shear_max_per_s:
        raise EngineInputInvalid("wall shear rate is outside the caller-declared rheology fit range")
    stress = inputs.consistency_si * wall_rate**n
    # Re_g = 8 rho V^2 / wall stress, algebraically Metzner-Reed's definition.
    reynolds = 8.0 * inputs.density_si * v**2 / stress
    if not math.isfinite(reynolds) or reynolds >= 2100.0:
        raise EngineInputInvalid("generalized Reynolds number is outside the supported laminar screen")
    gradient = 4.0 * stress / inputs.diameter_si
    loss = gradient * inputs.length_si
    if not all(math.isfinite(x) and x > 0 for x in (v, wall_rate, stress, reynolds, gradient, loss)):
        raise EngineInputInvalid("power-law pipe calculation is numerically outside the supported range")
    warnings = [
        "The declared rheology fit and fluid properties are not independently verified.",
        "The Reynolds screen is not proof of laminar flow or field applicability.",
    ]
    if inputs.measured_shear_min_per_s > 0:
        warnings.append(
            "The shear rate varies from zero at the axis to the wall rate; the nonzero measured minimum "
            "means the power-law fit is extrapolated near the axis."
        )
    return EngineResult(
        outputs=PowerLawPipeOutput(
            velocity_si=v, wall_shear_rate_per_s=wall_rate, wall_shear_stress_si=stress,
            generalized_reynolds=reynolds, pressure_gradient_si=gradient, pressure_loss_si=loss,
        ),
        warnings=warnings,
        assumptions_applied=[
            "steady, fully developed, incompressible, isothermal, laminar flow in one circular pipe",
            "time-independent power-law fluid with no wall slip, yield stress or end effects",
        ],
    )


POWER_LAW_SPEC = EngineSpec(
    key="hydraulics.power_law_pipe",
    name="Power-law laminar circular pipe loss",
    version="1.0.0",
    domain_pack="Hydraulics Pack",
    category="hydraulics",
    summary="Distributed laminar loss in one circular pipe with declared power-law fit range.",
    inputs_model=PowerLawPipeInput,
    outputs_model=PowerLawPipeOutput,
    consumes=("mud.properties",),
    produces=("engine.result",),
    assumptions=("steady fully developed laminar pipe flow", "time-independent power-law rheology"),
    limitations=(
        "single pipe only; no annulus, ECD, nozzle, fittings, surface network or total well pressure",
        "transition screen does not establish actual flow regime; no turbulent calculation",
        "rheology and range are caller declarations; no sample, temperature or field validation",
        "K has index-dependent units Pa·s^n and is not automatically unit-converted",
    ),
    references=(
        "BYU Chemical Engineering Class 33, More On Non-Newtonian Laminar Flow, equations 1 and 3–4, https://uolab.groups.et.byu.net/files/pipevisco/hints/374_Class_33.pdf (edition unstated)",
        "Metzner-Reed generalized Reynolds number as presented by CheCalc, https://www.checalc.com/fluid_flow_power_law.html (edition unstated)",
    ),
    tags=("hydraulics", "power-law", "pipe-only"),
    validation_status="formula_derived",
    action_level="L1",
)

register_engine(FunctionEngine(POWER_LAW_SPEC, compute_power_law_pipe))
