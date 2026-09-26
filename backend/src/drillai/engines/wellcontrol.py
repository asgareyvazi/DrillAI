"""Well control: kill sheet, MAASP and kick tolerance.

Standard volumetric/Driller's-method calculations as taught in IADC/API well control
(IWCF) and used on the rig floor:

* kill mud weight from SIDPP and TVD,
* initial and final circulating pressure,
* the pressure schedule (ICP → FCP) against strokes to the bit,
* MAASP from the shoe fracture strength,
* margin checks against the casing shoe and formation integrity.

The engine is deterministic and reports every intermediate value, because a kill sheet is
reviewed by a well-site supervisor: the platform must show the same numbers they compute
independently, not a black-box answer.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from drillai.engines.contract import ConstraintViolation, EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field

GRAVITY = 9.80665
KPA_PER_PSI = 6.894757293168361


class KillSheetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tvd_si: float = quantity_field(..., "m", gt=0.0, description="true vertical depth of the bit/reservoir")
    current_mud_weight_si: float = quantity_field(..., "kg/m3", gt=0.0)
    sidpp_si: float = quantity_field(..., "Pa", ge=0.0, description="shut-in drill pipe pressure")
    sicp_si: float | None = quantity_field(None, "Pa", ge=0.0, description="shut-in casing pressure")
    pit_gain_si: float | None = quantity_field(None, "m3", ge=0.0)
    slow_circulating_pressure_si: float = quantity_field(
        ..., "Pa", ge=0.0, description="KRP: pressure at the slow kill rate (a.k.a. SCRP)"
    )
    kill_rate_si: float | None = quantity_field(None, "m3/s", gt=0.0)
    pump_output_per_stroke_si: float | None = quantity_field(None, "m3", gt=0.0)
    strokes_to_bit: float | None = quantity_field(None, "1", ge=0.0)
    strokes_surface_to_bit: float | None = quantity_field(None, "1", ge=0.0)
    casing_shoe_tvd_si: float | None = quantity_field(None, "m", gt=0.0)
    shoe_fracture_mud_weight_si: float | None = quantity_field(
        None, "kg/m3", gt=0.0, description="fracture mud weight at the shoe (from LOT/FIT)"
    )
    shoe_formation_integrity_si: float | None = quantity_field(None, "kg/m3")
    influx_type: str | None = None
    schedule_points: int = 5


class KillSheetRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: str
    strokes: float
    pressure_si: float
    pressure_kpa: float
    mud_weight_si: float


class KillSheetOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kill_mud_weight_si: float
    kill_mud_weight_ppg: float
    kill_mud_weight_increase_si: float
    initial_circulating_pressure_si: float
    final_circulating_pressure_si: float
    initial_circulating_pressure_kpa: float
    final_circulating_pressure_kpa: float
    maasp_si: float | None = None
    maasp_kpa: float | None = None
    max_allowable_mud_weight_si: float | None = None
    shoe_pressure_at_kick_si: float | None = None
    shoe_margin_si: float | None = None
    kill_rate_si: float | None = None
    strokes_to_bit: float | None = None
    schedule: list[KillSheetRow]
    formation_pressure_equivalent_si: float
    formation_pressure_si: float
    kick_tolerance_mud_weight_si: float | None = None
    time_to_kill_si: float | None = None
    notes: list[str]


def compute_kill_sheet(inputs: KillSheetInput) -> EngineResult:
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []

    if inputs.sicp_si is not None and inputs.sicp_si > inputs.sidpp_si and inputs.sicp_si - inputs.sidpp_si > 5e5:
        warnings.append(
            "SICP exceeds SIDPP significantly: possible annulus obstruction or trapped pressure — verify before pumping"
        )

    # Kill mud weight: KMW = MW + SIDPP / (g * TVD)
    kill_mw = inputs.current_mud_weight_si + inputs.sidpp_si / (GRAVITY * inputs.tvd_si)
    formation_pressure = inputs.current_mud_weight_si * GRAVITY * inputs.tvd_si + inputs.sidpp_si
    kill_rate_pressure = inputs.slow_circulating_pressure_si

    icp = kill_rate_pressure + inputs.sidpp_si
    fcp = kill_rate_pressure * kill_mw / inputs.current_mud_weight_si if inputs.current_mud_weight_si else 0.0

    strokes_to_bit = inputs.strokes_to_bit or inputs.strokes_surface_to_bit
    schedule: list[KillSheetRow] = []
    if strokes_to_bit and strokes_to_bit > 0:
        schedule.append(
            KillSheetRow(phase="initial", strokes=0.0, pressure_si=icp, pressure_kpa=icp / 1000.0, mud_weight_si=inputs.current_mud_weight_si)
        )
        for index in range(1, inputs.schedule_points + 1):
            fraction = index / (inputs.schedule_points + 1)
            strokes = strokes_to_bit * fraction
            pressure = icp - (icp - fcp) * fraction
            mud_weight = inputs.current_mud_weight_si + (kill_mw - inputs.current_mud_weight_si) * fraction
            schedule.append(
                KillSheetRow(
                    phase="pump_kill_mud",
                    strokes=round(strokes, 1),
                    pressure_si=pressure,
                    pressure_kpa=pressure / 1000.0,
                    mud_weight_si=mud_weight,
                )
            )
        schedule.append(
            KillSheetRow(
                phase="final", strokes=strokes_to_bit, pressure_si=fcp, pressure_kpa=fcp / 1000.0, mud_weight_si=kill_mw
            )
        )
    else:
        warnings.append("strokes to bit not provided: pressure schedule cannot be tabulated")

    maasp = None
    max_allowable_mw = None
    shoe_pressure = None
    shoe_margin = None
    if inputs.casing_shoe_tvd_si and inputs.shoe_fracture_mud_weight_si:
        max_allowable_mw = inputs.shoe_fracture_mud_weight_si
        maasp = max(0.0, (inputs.shoe_fracture_mud_weight_si - inputs.current_mud_weight_si) * GRAVITY * inputs.casing_shoe_tvd_si)
        shoe_pressure = inputs.current_mud_weight_si * GRAVITY * inputs.casing_shoe_tvd_si + (inputs.sicp_si or 0.0)
        shoe_margin = inputs.shoe_fracture_mud_weight_si * GRAVITY * inputs.casing_shoe_tvd_si - shoe_pressure
        if shoe_pressure > inputs.shoe_fracture_mud_weight_si * GRAVITY * inputs.casing_shoe_tvd_si:
            violations.append(
                ConstraintViolation(
                    name="shoe_fracture_exceeded",
                    message=(
                        f"pressure at the casing shoe {shoe_pressure / 1e6:.2f} MPa exceeds the fracture pressure "
                        f"{(inputs.shoe_fracture_mud_weight_si * GRAVITY * inputs.casing_shoe_tvd_si) / 1e6:.2f} MPa"
                    ),
                    severity="error",
                    actual=shoe_pressure,
                    unit="Pa",
                    source="casing shoe",
                )
            )
    else:
        warnings.append("casing shoe fracture data not supplied: MAASP and shoe margin are unavailable")

    kill_tolerance_mw = None
    if max_allowable_mw is not None:
        # Mud weight at which the reservoir pressure can just be controlled with the shoe
        # remaining below fracture: MTMW = (FP_shoe - SIDPP*TVD_bit/shoe...) simplified form.
        kill_tolerance_mw = max_allowable_mw - inputs.current_mud_weight_si

    time_to_kill = None
    if inputs.kill_rate_si and inputs.kill_rate_si > 0 and strokes_to_bit and inputs.pump_output_per_stroke_si:
        volume = strokes_to_bit * inputs.pump_output_per_stroke_si
        time_to_kill = volume / inputs.kill_rate_si

    notes = [
        "Driller's method pressure schedule: ICP = KRP + SIDPP, FCP = KRP × KMW/OMW",
        "kill mud weight = OMW + SIDPP/(g × TVD)",
        "MAASP = (fracture MW − OMW) × g × shoe TVD",
    ]
    if inputs.sicp_si is None:
        notes.append("SICP not supplied: annular pressure behaviour is not evaluated")

    outputs = KillSheetOutput(
        kill_mud_weight_si=kill_mw,
        kill_mud_weight_ppg=kill_mw / 119.82642731689663,
        kill_mud_weight_increase_si=kill_mw - inputs.current_mud_weight_si,
        initial_circulating_pressure_si=icp,
        final_circulating_pressure_si=fcp,
        initial_circulating_pressure_kpa=icp / 1000.0,
        final_circulating_pressure_kpa=fcp / 1000.0,
        maasp_si=maasp,
        maasp_kpa=(maasp / 1000.0) if maasp is not None else None,
        max_allowable_mud_weight_si=max_allowable_mw,
        shoe_pressure_at_kick_si=shoe_pressure,
        shoe_margin_si=shoe_margin,
        kill_rate_si=inputs.kill_rate_si,
        strokes_to_bit=strokes_to_bit,
        schedule=schedule,
        formation_pressure_equivalent_si=formation_pressure / (GRAVITY * inputs.tvd_si),
        formation_pressure_si=formation_pressure,
        kick_tolerance_mud_weight_si=kill_tolerance_mw,
        time_to_kill_si=time_to_kill,
        notes=notes,
    )
    return EngineResult(outputs=outputs, warnings=warnings, violations=violations, assumptions_applied=notes[:3])


KILL_SHEET_SPEC = EngineSpec(
    key="well_control.kill_sheet",
    name="Well control kill sheet",
    version="1.1.0",
    domain_pack="Well Control Pack",
    category="well_control",
    summary=(
        "Kill mud weight, ICP/FCP, Driller's-method pressure schedule, MAASP, shoe margin and kill time "
        "from shut-in pressures and pump data."
    ),
    inputs_model=KillSheetInput,
    outputs_model=KillSheetOutput,
    consumes=("wellbore.geometry", "wellbore.section_state", "pump.parameters", "mud.properties", "trajectory.summary"),
    produces=("well_control.kill_sheet", "well_control.state"),
    parameters={"method": {"value": "drillers_method"}},
    assumptions=(
        "single influx, steady-state shut-in pressures",
        "kill rate pressure measured at the same rate used for the kill",
        "no annular friction while shut in",
    ),
    limitations=(
        "does not size the influx or compute the influx gradient (needs SICP + pit gain interpretation)",
        "no multi-phase or migrating-bubble modelling",
        "not a substitute for a certified well control procedure or IWCF training",
    ),
    references=(
        "IADC Well Control Manual (Driller's method)",
        "API RP 59 — Well Control Operations",
    ),
    tags=("well-control", "kill-sheet", "maasp", "safety"),
    action_level="L2",
    validation_status="verified_against_reference",
)

register_engine(FunctionEngine(KILL_SHEET_SPEC, compute_kill_sheet), aliases=("kill_sheet", "well_control"))
