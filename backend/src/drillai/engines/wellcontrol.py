"""Well control: kill sheet (`wait_and_weight` and `drillers_method`), MAASP, shoe mud-weight margin,
and preliminary single-bubble static volumetric kick tolerance.

Supports two explicit constant-bottomhole-pressure kill methods as taught in IADC WellSharp / IWCF
and API RP 59:

* ``method="wait_and_weight"`` (**Engineer's Method**, default):
  Single circulation with weighted kill mud (`KMW`). Drillpipe pressure steps linearly from
  ``ICP = KRP + SIDPP`` down to ``FCP = KRP × (KMW / OMW)`` over ``0 -> strokes_to_bit`` while
  kill mud fills the drillstring, then holds constant at ``FCP`` while kill mud displaces the
  annulus (``annulus_strokes``).
* ``method="drillers_method"`` (**Driller's Method**, two-circulation):
  - **Circulation 1**: Circulates the influx out of the well using original mud (`OMW`) while
    holding drillpipe pressure constant at ``ICP = KRP + SIDPP`` from ``0`` to
    ``strokes_to_bit + annulus_strokes``.
  - **Circulation 2**: Displaces the well with weighted kill mud (`KMW`), stepping drillpipe
    pressure linearly from ``ICP`` down to ``FCP = KRP × (KMW / OMW)`` over ``strokes_to_bit``
    and holding ``FCP`` up the annulus.

Terminology & Engineering Boundaries (CP23)
-------------------------------------------
* ``mud_weight_margin_si`` (``kg/m3``) is the static mud-weight margin at the casing shoe
  (``shoe_fracture_mud_weight_si - current_mud_weight_si``). ``kick_tolerance_mud_weight_si`` is
  retained as a deprecated alias for backward compatibility and is **not** a volumetric kick
  tolerance.
* ``volumetric_kick_tolerance_si`` (``m3``) is computed only when ``casing_shoe_tvd_si``,
  ``shoe_fracture_mud_weight_si``, ``influx_density_si``, and ``annulus_capacity_at_shoe_si`` are
  supplied, using a preliminary single-bubble unmigrated hydrostatic balance at the casing shoe
  (``volumetric_kick_tolerance_basis="preliminary_single_bubble_unmigrated_at_shoe"``). It does
  not model multi-phase gas expansion, Z-factor compressibility, or mud solubility.
* ``FCP = KRP × (KMW / OMW)`` assumes ``slow_circulating_pressure_si`` (`KRP`) is dominated by
  drillstring and bit nozzle friction with negligible annular friction during kill circulation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from drillai.engines.contract import ConstraintViolation, EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field

GRAVITY = 9.80665
KPA_PER_PSI = 6.894757293168361


class KillSheetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: Literal["wait_and_weight", "drillers_method"] = "wait_and_weight"
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
    annulus_strokes: float | None = quantity_field(
        None, "1", ge=0.0, description="strokes from bit to surface (annular displacement)"
    )
    casing_shoe_tvd_si: float | None = quantity_field(None, "m", gt=0.0)
    shoe_fracture_mud_weight_si: float | None = quantity_field(
        None, "kg/m3", gt=0.0, description="fracture mud weight at the shoe (from LOT/FIT)"
    )
    shoe_formation_integrity_si: float | None = quantity_field(None, "kg/m3")
    influx_type: str | None = None
    influx_density_si: float | None = quantity_field(
        None, "kg/m3", gt=0.0, description="estimated kick fluid density (e.g. 250 kg/m3 for gas)"
    )
    annulus_capacity_at_shoe_si: float | None = quantity_field(
        None, "m2", gt=0.0, description="annular cross-sectional capacity opposite casing shoe (m2 = m3/m) for volumetric kick tolerance"
    )
    input_provenance: dict[str, str] | None = None
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
    method: Literal["wait_and_weight", "drillers_method"] = "wait_and_weight"
    circulation_cycles: int = 1
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
    annulus_strokes: float | None = None
    total_kill_strokes: float | None = None
    schedule: list[KillSheetRow]
    formation_pressure_equivalent_si: float
    formation_pressure_si: float
    mud_weight_margin_si: float | None = None
    kick_tolerance_mud_weight_si: float | None = None
    volumetric_kick_tolerance_si: float | None = None
    max_allowable_influx_height_at_shoe_si: float | None = None
    volumetric_kick_tolerance_basis: str = "not_computed_requires_influx_density_and_annulus_capacity"
    friction_assumption: str = "drillstring_dominated_krp_annular_friction_neglected"
    input_provenance_verified: bool = False
    time_to_kill_si: float | None = None
    notes: list[str]


def compute_kill_sheet(inputs: KillSheetInput) -> EngineResult:
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []

    if inputs.sicp_si is not None and inputs.sicp_si > inputs.sidpp_si and inputs.sicp_si - inputs.sidpp_si > 5e5:
        warnings.append(
            "SICP exceeds SIDPP significantly: possible annulus obstruction or trapped pressure — verify before pumping"
        )

    # Check input provenance when provided or flag unverified manual/estimated inputs
    prov = inputs.input_provenance or {}
    unverified_sources = {"estimated", "manual_override", "assumed", "unverified"}
    has_unverified_prov = any(str(v).lower() in unverified_sources for v in prov.values())
    input_provenance_verified = bool(
        prov
        and not has_unverified_prov
        and "slow_circulating_pressure_si" in prov
    )
    if has_unverified_prov:
        warnings.append(
            "safety-critical kill-sheet inputs include estimated or manual_override provenance — verify against signed SCR and LOT/FIT records before execution"
        )

    # Kill mud weight: KMW = MW + SIDPP / (g * TVD)
    kill_mw = inputs.current_mud_weight_si + inputs.sidpp_si / (GRAVITY * inputs.tvd_si)
    formation_pressure = inputs.current_mud_weight_si * GRAVITY * inputs.tvd_si + inputs.sidpp_si
    kill_rate_pressure = inputs.slow_circulating_pressure_si

    icp = kill_rate_pressure + inputs.sidpp_si
    fcp = kill_rate_pressure * kill_mw / inputs.current_mud_weight_si if inputs.current_mud_weight_si else 0.0

    strokes_to_bit = inputs.strokes_to_bit or inputs.strokes_surface_to_bit
    ann_strokes = inputs.annulus_strokes
    total_kill_strokes: float | None = None
    schedule: list[KillSheetRow] = []

    if strokes_to_bit and strokes_to_bit > 0:
        if inputs.method == "wait_and_weight":
            schedule.append(
                KillSheetRow(
                    phase="initial",
                    strokes=0.0,
                    pressure_si=icp,
                    pressure_kpa=icp / 1000.0,
                    mud_weight_si=inputs.current_mud_weight_si,
                )
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
                    phase="final",
                    strokes=strokes_to_bit,
                    pressure_si=fcp,
                    pressure_kpa=fcp / 1000.0,
                    mud_weight_si=kill_mw,
                )
            )
            if ann_strokes and ann_strokes > 0:
                total_kill_strokes = strokes_to_bit + ann_strokes
                schedule.append(
                    KillSheetRow(
                        phase="displace_annulus_at_fcp",
                        strokes=round(total_kill_strokes, 1),
                        pressure_si=fcp,
                        pressure_kpa=fcp / 1000.0,
                        mud_weight_si=kill_mw,
                    )
                )
            else:
                total_kill_strokes = strokes_to_bit
        else:
            # True two-circulation Driller's Method:
            # Circulation 1: circulate out the influx with original mud holding constant drillpipe pressure = ICP
            if not ann_strokes or ann_strokes <= 0:
                warnings.append(
                    "annulus_strokes not provided for drillers_method: Circulation 1 annular displacement duration is not fully bounded"
                )
            c1_end = strokes_to_bit + (ann_strokes or 0.0)
            schedule.append(
                KillSheetRow(
                    phase="circulation_1_initial",
                    strokes=0.0,
                    pressure_si=icp,
                    pressure_kpa=icp / 1000.0,
                    mud_weight_si=inputs.current_mud_weight_si,
                )
            )
            schedule.append(
                KillSheetRow(
                    phase="circulation_1_hold_icp_to_bit",
                    strokes=round(strokes_to_bit, 1),
                    pressure_si=icp,
                    pressure_kpa=icp / 1000.0,
                    mud_weight_si=inputs.current_mud_weight_si,
                )
            )
            if ann_strokes and ann_strokes > 0:
                schedule.append(
                    KillSheetRow(
                        phase="circulation_1_complete_influx_out",
                        strokes=round(c1_end, 1),
                        pressure_si=icp,
                        pressure_kpa=icp / 1000.0,
                        mud_weight_si=inputs.current_mud_weight_si,
                    )
                )
            # Circulation 2: displace drillstring with kill mud stepping linearly ICP -> FCP over strokes_to_bit
            for index in range(1, inputs.schedule_points + 1):
                fraction = index / (inputs.schedule_points + 1)
                strokes = c1_end + strokes_to_bit * fraction
                pressure = icp - (icp - fcp) * fraction
                mud_weight = inputs.current_mud_weight_si + (kill_mw - inputs.current_mud_weight_si) * fraction
                schedule.append(
                    KillSheetRow(
                        phase="circulation_2_pump_kill_mud",
                        strokes=round(strokes, 1),
                        pressure_si=pressure,
                        pressure_kpa=pressure / 1000.0,
                        mud_weight_si=mud_weight,
                    )
                )
            c2_bit = c1_end + strokes_to_bit
            schedule.append(
                KillSheetRow(
                    phase="circulation_2_kill_mud_at_bit",
                    strokes=round(c2_bit, 1),
                    pressure_si=fcp,
                    pressure_kpa=fcp / 1000.0,
                    mud_weight_si=kill_mw,
                )
            )
            if ann_strokes and ann_strokes > 0:
                total_kill_strokes = c2_bit + ann_strokes
                schedule.append(
                    KillSheetRow(
                        phase="circulation_2_displace_annulus_at_fcp",
                        strokes=round(total_kill_strokes, 1),
                        pressure_si=fcp,
                        pressure_kpa=fcp / 1000.0,
                        mud_weight_si=kill_mw,
                    )
                )
            else:
                total_kill_strokes = c2_bit
    else:
        warnings.append("strokes to bit not provided: pressure schedule cannot be tabulated")

    maasp = None
    max_allowable_mw = None
    shoe_pressure = None
    shoe_margin = None
    if inputs.casing_shoe_tvd_si and inputs.shoe_fracture_mud_weight_si:
        max_allowable_mw = inputs.shoe_fracture_mud_weight_si
        maasp = max(
            0.0,
            (inputs.shoe_fracture_mud_weight_si - inputs.current_mud_weight_si)
            * GRAVITY
            * inputs.casing_shoe_tvd_si,
        )
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

    mud_weight_margin = None
    if max_allowable_mw is not None:
        # Static shoe mud-weight margin (kg/m3): fracture_MW - current_MW.
        # Kept distinct from volumetric kick tolerance (m3).
        mud_weight_margin = max_allowable_mw - inputs.current_mud_weight_si

    volumetric_kick_tolerance = None
    max_influx_height = None
    volumetric_basis = "not_computed_requires_influx_density_and_annulus_capacity"
    if (
        inputs.casing_shoe_tvd_si
        and inputs.shoe_fracture_mud_weight_si
        and inputs.influx_density_si is not None
        and inputs.annulus_capacity_at_shoe_si is not None
    ):
        delta_rho = inputs.current_mud_weight_si - inputs.influx_density_si
        if delta_rho > 0:
            p_frac_shoe = inputs.shoe_fracture_mud_weight_si * GRAVITY * inputs.casing_shoe_tvd_si
            # Pressure at shoe from bottomhole formation pressure minus mud column below shoe
            p_shoe_base = formation_pressure - inputs.current_mud_weight_si * GRAVITY * max(
                0.0, inputs.tvd_si - inputs.casing_shoe_tvd_si
            )
            delta_p_allowable = max(0.0, p_frac_shoe - p_shoe_base)
            max_influx_height = delta_p_allowable / (delta_rho * GRAVITY)
            volumetric_kick_tolerance = max_influx_height * inputs.annulus_capacity_at_shoe_si
            volumetric_basis = "preliminary_single_bubble_unmigrated_at_shoe"

    time_to_kill = None
    if inputs.kill_rate_si and inputs.kill_rate_si > 0 and total_kill_strokes and inputs.pump_output_per_stroke_si:
        volume = total_kill_strokes * inputs.pump_output_per_stroke_si
        time_to_kill = volume / inputs.kill_rate_si

    if inputs.method == "wait_and_weight":
        method_note = (
            "Wait-and-Weight (Engineer's) method pressure schedule: ICP = KRP + SIDPP stepping linearly "
            "to FCP = KRP × (KMW / OMW) over strokes_to_bit"
        )
    else:
        method_note = (
            "Driller's method (two-circulation) pressure schedule: Circulation 1 holds constant ICP = KRP + SIDPP "
            "with original mud; Circulation 2 steps ICP -> FCP = KRP × (KMW / OMW) over strokes_to_bit with kill mud"
        )

    notes = [
        method_note,
        "kill mud weight = OMW + SIDPP/(g × TVD)",
        "MAASP = (fracture MW − OMW) × g × shoe TVD",
        "FCP = KRP × (KMW / OMW) assumes KRP is drillstring/bit friction dominated with negligible annular friction",
        "mud_weight_margin_si is static shoe mud-weight margin (kg/m3), distinct from volumetric_kick_tolerance_si (m3)",
    ]
    if inputs.sicp_si is None:
        notes.append("SICP not supplied: annular pressure behaviour is not evaluated")

    outputs = KillSheetOutput(
        method=inputs.method,
        circulation_cycles=1 if inputs.method == "wait_and_weight" else 2,
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
        annulus_strokes=ann_strokes,
        total_kill_strokes=total_kill_strokes,
        schedule=schedule,
        formation_pressure_equivalent_si=formation_pressure / (GRAVITY * inputs.tvd_si),
        formation_pressure_si=formation_pressure,
        mud_weight_margin_si=mud_weight_margin,
        kick_tolerance_mud_weight_si=mud_weight_margin,
        volumetric_kick_tolerance_si=volumetric_kick_tolerance,
        max_allowable_influx_height_at_shoe_si=max_influx_height,
        volumetric_kick_tolerance_basis=volumetric_basis,
        friction_assumption="drillstring_dominated_krp_annular_friction_neglected",
        input_provenance_verified=input_provenance_verified,
        time_to_kill_si=time_to_kill,
        notes=notes,
    )
    return EngineResult(outputs=outputs, warnings=warnings, violations=violations, assumptions_applied=notes[:5])


KILL_SHEET_SPEC = EngineSpec(
    key="well_control.kill_sheet",
    name="Well control kill sheet (Wait-and-Weight & Driller's Method)",
    version="1.2.0",
    domain_pack="Well Control Pack",
    category="well_control",
    summary=(
        "Kill mud weight, ICP/FCP, Wait-and-Weight (single-circulation) or Driller's-method (two-circulation) "
        "pressure schedule, MAASP, static shoe mud-weight margin, and preliminary single-bubble volumetric kick tolerance."
    ),
    inputs_model=KillSheetInput,
    outputs_model=KillSheetOutput,
    consumes=("wellbore.geometry", "wellbore.section_state", "pump.parameters", "mud.properties", "trajectory.summary"),
    produces=("well_control.kill_sheet", "well_control.state"),
    parameters={
        "default_method": {"value": "wait_and_weight"},
        "supported_methods": {"value": ("wait_and_weight", "drillers_method")},
    },
    assumptions=(
        "single influx, steady-state shut-in pressures",
        "kill rate pressure (KRP) measured at the slow kill rate and dominated by drillstring/bit friction",
        "no annular friction while shut in and negligible annular friction during kill circulation",
        "volumetric_kick_tolerance_si (when computed) uses a preliminary single-bubble unmigrated balance at the casing shoe",
    ),
    limitations=(
        "does not model multi-phase gas expansion, real-gas Z-factor compressibility, or oil-based mud gas solubility",
        "mud_weight_margin_si (and deprecated alias kick_tolerance_mud_weight_si) is a static shoe mud-weight margin (kg/m3), not a multi-phase volumetric kick tolerance",
        "not a substitute for a certified well control procedure or IWCF/IADC WellSharp training",
    ),
    references=(
        "IADC WellSharp / IWCF Well Control Manual (Wait-and-Weight & Driller's Method)",
        "API RP 59 — Well Control Operations",
    ),
    tags=("well-control", "kill-sheet", "maasp", "safety"),
    action_level="L2",
    validation_status="verified_against_reference",
)

register_engine(FunctionEngine(KILL_SHEET_SPEC, compute_kill_sheet), aliases=("kill_sheet", "well_control"))
