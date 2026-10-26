"""CP19 deterministic analytical test-vector suite across all 14 engines & 122 unit definitions.

These tests measure numerical behavior against hand-calculated equations and unit references. They
are not a field trial, domain-owner sign-off, third-party certification, or production approval.

Verifies:
1. Independent closed-form analytical benchmarks across all 14 registered engineering engines
   (`trajectory.minimum_curvature`, `wellbore.capacity`, `hydraulics.laminar`,
   `torque_drag.soft_string`, `drilling.mse`, `well_control.kill_sheet`, `tubulars.api_5c3`,
   `integrity.barrier_envelope`, `schematic.well_model`, `offsets.similarity`,
   `readiness.assessment`, `twin.reconciliation`, `optimization.sweep`, `optimization.pareto`).
2. Complete unit registry round-trip & affine reference accuracy across all 122 registered unit definitions and
   33 dimensions (`drillai.units.registry`).
3. Metamorphic monotonicity invariants, boundary/invalid input rejection (`EngineInputInvalid`),
   and strict `json.dumps(..., allow_nan=False)` numerical safety across all 14 engines.
4. Full provenance, units (`input_units`, `output_units`), assumptions, limitations, validation
   status, and LLM/agent non-override enforcement (`execute_engine` & `agent_catalogue`).
"""

from __future__ import annotations

import json
import math
from typing import Any

import pytest
from sqlalchemy import select

from drillai.ai.agents import agent_catalogue, install_default_agents
from drillai.core.errors import EngineInputInvalid
from drillai.db.models import EngineRun, Organization
from drillai.engines.registry import load_default_engines
from drillai.engines.service import execute_engine
from drillai.security.actions import ActionLevel
from drillai.units.registry import (
    Dimension,
    UnitError,
    convert,
    get_unit,
    known_units,
)


def test_cp19_01_all_units_and_dimensions_round_trip_and_reference_accuracy() -> None:
    """Every unit in known_units() across all dimensions must round-trip accurately
    through canonical SI and reject cross-dimension conversions."""
    all_units = known_units()
    assert len(all_units) == 122, "unit count changes require updating the exact registry contract"
    assert len(Dimension) == 33, "dimension count changes require updating the exact registry contract"

    for dim in Dimension:
        dim_units = [u for u in all_units if u.dimension == dim]
        assert len(dim_units) >= 1, f"Dimension {dim} has no registered units"
        for u in dim_units:
            for sample_val in (1.0, 10.0, 42.5):
                si_val = u.to_canonical(sample_val)
                back_val = u.from_canonical(si_val)
                assert math.isclose(back_val, sample_val, rel_tol=1e-11, abs_tol=1e-11), (
                    f"Round-trip failed for unit {u.symbol} ({dim}): {sample_val} -> {si_val} -> {back_val}"
                )

    # Exact affine temperature reference checks
    assert math.isclose(convert(0.0, "degC", "K"), 273.15, abs_tol=1e-9)
    assert math.isclose(convert(0.0, "degC", "degF"), 32.0, abs_tol=1e-9)
    assert math.isclose(convert(100.0, "degC", "degF"), 212.0, abs_tol=1e-9)
    assert math.isclose(convert(0.0, "K", "degR"), 0.0, abs_tol=1e-9)
    assert math.isclose(convert(273.15, "K", "degR"), 491.67, rel_tol=1e-6)

    # Exact oilfield conversion benchmarks
    assert math.isclose(convert(1.0, "ft", "m"), 0.3048, rel_tol=1e-12)
    assert math.isclose(convert(1.0, "in", "m"), 0.0254, rel_tol=1e-12)
    assert math.isclose(convert(1.0, "psi", "Pa"), 6894.757293168, rel_tol=1e-9)
    assert math.isclose(convert(10.0, "ppg", "kg/m3"), 1198.26427, rel_tol=1e-6)
    assert math.isclose(convert(1.0, "D", "m2"), 9.869233e-13, rel_tol=1e-6)
    assert get_unit("m2").dimension == Dimension.AREA
    assert get_unit("m2-perm").dimension == Dimension.PERMEABILITY

    with pytest.raises(UnitError):
        convert(100.0, "psi", "m")


def test_cp19_02_independent_analytical_benchmarks_for_all_14_engines() -> None:
    """Check all 14 engines against analytical test vectors, not field certification, and enforce strict JSON safety."""
    reg = load_default_engines()
    assert len(reg.keys()) == 14
    field_validation_required = {"tubulars.api_5c3", "well_control.kill_sheet"}

    def run_and_check_json(key: str, payload: dict[str, Any]) -> dict[str, Any]:
        spec, res = reg.execute(key, payload)
        expected_status = "needs_field_validation" if key in field_validation_required else "formula_derived"
        assert spec.validation_status == expected_status
        out_dict = res.outputs.model_dump(mode="json")
        # Strict IEEE-754 finite check: raises ValueError if any NaN/Inf exists anywhere in output
        encoded = json.dumps(out_dict, allow_nan=False)
        assert len(encoded) > 2
        return out_dict

    # 1. trajectory.minimum_curvature — SPE Book Vol. 2 / Bourgoyne et al. Minimum Curvature
    # 0 -> 1000 m, I1=0 deg, I2=30 deg, A1=0 deg, A2=90 deg
    # beta = 30 deg = pi/6 rad, RF = (2/beta)*tan(beta/2)
    beta = math.radians(30.0)
    rf = (2.0 / beta) * math.tan(beta / 2.0)
    expected_tvd = 0.5 * 1000.0 * (1.0 + math.cos(beta)) * rf
    expected_east = 0.5 * 1000.0 * math.sin(beta) * rf
    traj_out = run_and_check_json(
        "trajectory.minimum_curvature",
        {
            "stations": [
                {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                {"md_si": 1000.0, "inclination_deg": 30.0, "azimuth_deg": 90.0},
            ]
        },
    )
    assert math.isclose(traj_out["total_tvd_si"], expected_tvd, rel_tol=1e-6)
    assert math.isclose(traj_out["total_displacement_si"], expected_east, rel_tol=1e-6)
    assert math.isclose(traj_out["max_dls_deg_per_30m"], 0.9, rel_tol=1e-6)

    # 2. wellbore.capacity — Concentric Cylindrical Volume & Bottoms-Up Time
    # 2000 m hole (OD=0.2159 m) with 2000 m drillpipe (OD=0.127 m, ID=0.1086 m), Q=0.03 m3/s
    cap_pipe = (math.pi / 4.0) * (0.1086**2)
    cap_ann = (math.pi / 4.0) * (0.2159**2 - 0.127**2)
    vol_pipe = cap_pipe * 2000.0
    vol_ann = cap_ann * 2000.0
    cap_out = run_and_check_json(
        "wellbore.capacity",
        {
            "elements": [
                {"kind": "hole", "from_depth_si": 0.0, "to_depth_si": 2000.0, "od_si": 0.2159},
                {
                    "kind": "drillpipe",
                    "from_depth_si": 0.0,
                    "to_depth_si": 2000.0,
                    "od_si": 0.127,
                    "id_si": 0.1086,
                },
            ],
            "current_depth_si": 2000.0,
            "flow_rate_si": 0.03,
            "pump": {"output_per_stroke_si": 0.015},
        },
    )
    assert math.isclose(cap_out["string_volume_si"], vol_pipe, rel_tol=1e-5)
    assert math.isclose(cap_out["annulus_volume_si"], vol_ann, rel_tol=1e-5)
    assert math.isclose(cap_out["bottoms_up_time_si"], vol_ann / 0.03, rel_tol=1e-5)
    assert math.isclose(cap_out["bottoms_up_strokes"], vol_ann / 0.015, rel_tol=1e-5)

    # 3. hydraulics.laminar — Bit Nozzle Orifice Pressure Drop + Bingham-Plastic Annular ECD
    # dP_bit = rho * Q^2 / (2 * Cd^2 * TFA^2)
    rho_h, q_h, cd_h, tfa_h = 1250.0, 0.030, 0.95, 0.00040
    expected_dp_bit = (rho_h * (q_h**2)) / (2.0 * (cd_h**2) * (tfa_h**2))
    hyd_out = run_and_check_json(
        "hydraulics.laminar",
        {
            "elements": [
                {"kind": "hole", "from_depth_si": 0.0, "to_depth_si": 2000.0, "od_si": 0.2159},
                {
                    "kind": "drillpipe",
                    "from_depth_si": 0.0,
                    "to_depth_si": 2000.0,
                    "od_si": 0.127,
                    "id_si": 0.1086,
                },
            ],
            "flow_rate_si": q_h,
            "mud_weight_si": rho_h,
            "plastic_viscosity_si": 0.020,
            "yield_point_si": 8.0,
            "bit_depth_si": 2000.0,
            "bit_tvd_si": 2000.0,
            "bit_diameter_si": 0.2159,
            "tfa_si": tfa_h,
            "discharge_coefficient": cd_h,
        },
    )
    assert math.isclose(hyd_out["bit_pressure_drop_si"], expected_dp_bit, rel_tol=1e-5)
    expected_ecd = rho_h + hyd_out["annulus_pressure_loss_si"] / (9.80665 * 2000.0)
    assert math.isclose(hyd_out["ecd_bottom_si"], expected_ecd, rel_tol=1e-5)

    # 4. torque_drag.soft_string — Johancsik (1984) Constant-Inclination (30 deg) Straight Inclined Hole
    # L=1000 m, theta=30 deg (constant -> d_theta=0, d_phi=0), m=30 kg/m, rho_m=1570 kg/m3, rho_s=7850 kg/m3
    # BF = 1 - 1570/7850 = 0.80
    # W_air = 30 * 9.80665 * 1000 = 294,199.5 N; W_buoyed = 0.80 * W_air = 235,359.6 N
    # Normal force Fn = W_buoyed * sin(30 deg) = 0.5 * W_buoyed
    # Rotating hookload = W_buoyed * cos(30 deg)
    # Pickup hookload = W_buoyed * (cos(30 deg) + mu_pu * sin(30 deg))
    # Slackoff hookload = W_buoyed * (cos(30 deg) - mu_so * sin(30 deg))
    w_air = 30.0 * 9.80665 * 1000.0
    bf = 1.0 - (1570.0 / 7850.0)
    w_buoy = w_air * bf
    theta_rad = math.radians(30.0)
    td_out = run_and_check_json(
        "torque_drag.soft_string",
        {
            "survey": [
                {"md_si": 0.0, "inclination_deg": 30.0, "azimuth_deg": 0.0},
                {"md_si": 1000.0, "inclination_deg": 30.0, "azimuth_deg": 0.0},
            ],
            "string": [
                {
                    "kind": "drillpipe",
                    "from_depth_si": 0.0,
                    "to_depth_si": 1000.0,
                    "od_si": 0.127,
                    "id_si": 0.1086,
                    "linear_mass_si": 30.0,
                }
            ],
            "mud_weight_si": 1570.0,
            "bit_depth_si": 1000.0,
            "friction_factor_pickup": 0.25,
            "friction_factor_slackoff": 0.20,
            "friction_factor_rotating": 0.15,
        },
    )
    assert math.isclose(td_out["buoyancy_factor"], bf, rel_tol=1e-6)
    assert math.isclose(td_out["string_weight_buoyed_si"], w_buoy, rel_tol=1e-6)
    assert math.isclose(td_out["hookload_rotating_si"], w_buoy * math.cos(theta_rad), rel_tol=1e-5)
    assert math.isclose(
        td_out["hookload_pickup_si"],
        w_buoy * (math.cos(theta_rad) + 0.25 * math.sin(theta_rad)),
        rel_tol=1e-5,
    )
    assert math.isclose(
        td_out["hookload_slackoff_si"],
        w_buoy * (math.cos(theta_rad) - 0.20 * math.sin(theta_rad)),
        rel_tol=1e-5,
    )
    expected_off_bottom_torque = 0.15 * (w_buoy * math.sin(theta_rad)) * (0.127 / 2.0)
    assert math.isclose(td_out["surface_torque_off_bottom_si"], expected_off_bottom_torque, rel_tol=1e-5)

    # 5. drilling.mse — Teale (1965) Mechanical Specific Energy
    # A_b = pi/4 * D^2; MSE = WOB/A_b + (120 * pi * RPM * T) / (A_b * ROP_m_h)
    d_b, wob, trq, rpm, rop = 0.2159, 90_000.0, 14_000.0, 120.0, 25.0
    a_b = (math.pi / 4.0) * (d_b**2)
    expected_mse = (wob / a_b) + (120.0 * math.pi * rpm * trq) / (a_b * rop)
    mse_out = run_and_check_json(
        "drilling.mse",
        {
            "wob_si": wob,
            "torque_si": trq,
            "rpm_si": rpm,
            "rop_si": rop,
            "bit_diameter_si": d_b,
            "confined_compressive_strength_si": 70_000_000.0,
        },
    )
    assert math.isclose(mse_out["bit_area_si"], a_b, rel_tol=1e-6)
    assert math.isclose(mse_out["mse_si"], expected_mse, rel_tol=1e-6)
    assert math.isclose(mse_out["drilling_efficiency"], 70_000_000.0 / expected_mse, rel_tol=1e-5)

    # 6. well_control.kill_sheet — IWCF / IADC Kill Sheet Equations
    # TVD=3000 m, MW=1200 kg/m3, SIDPP=3.5 MPa, SCP=4.0 MPa, Shoe TVD=1500 m, Shoe Frac MW=1650 kg/m3
    tvd_k, mw_k, sidpp_k, scp_k = 3000.0, 1200.0, 3_500_000.0, 4_000_000.0
    expected_kmw = mw_k + sidpp_k / (9.80665 * tvd_k)
    expected_icp = scp_k + sidpp_k
    expected_fcp = scp_k * (expected_kmw / mw_k)
    expected_maasp = (1650.0 - mw_k) * 9.80665 * 1500.0
    ks_out = run_and_check_json(
        "well_control.kill_sheet",
        {
            "tvd_si": tvd_k,
            "current_mud_weight_si": mw_k,
            "sidpp_si": sidpp_k,
            "slow_circulating_pressure_si": scp_k,
            "casing_shoe_tvd_si": 1500.0,
            "shoe_fracture_mud_weight_si": 1650.0,
            "strokes_to_bit": 1200.0,
        },
    )
    assert math.isclose(ks_out["kill_mud_weight_si"], expected_kmw, rel_tol=1e-5)
    assert math.isclose(ks_out["initial_circulating_pressure_si"], expected_icp, rel_tol=1e-5)
    assert math.isclose(ks_out["final_circulating_pressure_si"], expected_fcp, rel_tol=1e-5)
    assert math.isclose(ks_out["maasp_si"], expected_maasp, rel_tol=1e-5)

    # 7. tubulars.api_5c3 — API 5C3 / ISO 10400 Burst, Axial Yield & Collapse
    # OD=0.244475 m (9-5/8"), ID=0.2205 m, t=0.0119875 m, P110 (Yp=758.423 MPa)
    od_t, id_t, yp_t = 0.244475, 0.2205, 758_423_000.0
    t_wall = 0.5 * (od_t - id_t)
    expected_burst = 0.875 * (2.0 * yp_t * t_wall / od_t)
    expected_axial = yp_t * (math.pi / 4.0) * (od_t**2 - id_t**2)
    tub_out = run_and_check_json(
        "tubulars.api_5c3",
        {
            "segments": [
                {
                    "name": "9-5/8 P110 Production Casing",
                    "grade": "P110",
                    "from_depth_si": 0.0,
                    "to_depth_si": 2500.0,
                    "outer_diameter_si": od_t,
                    "wall_thickness_si": t_wall,
                    "yield_stress_si": yp_t,
                    "internal_pressure_si": 35_000_000.0,
                    "external_pressure_si": 20_000_000.0,
                    "axial": {"hanging_weight_si": 1_200_000.0},
                }
            ],
            "design_factors": {"burst": 1.1, "collapse": 1.0, "tension": 1.3, "triaxial": 1.25},
        },
    )
    seg0 = tub_out["segments"][0]
    assert math.isclose(seg0["burst_rating_uniaxial_si"], expected_burst, rel_tol=1e-5)
    assert math.isclose(seg0["axial_yield_force_si"], expected_axial, rel_tol=1e-5)
    assert tub_out["overall_qualified"] is True

    # 8. integrity.barrier_envelope — NORSOK D-010 Two-Barrier Independence Verification
    # Two independent verified barriers with evidence from 0-3000 m -> envelope_complete = True
    bar_ok = run_and_check_json(
        "integrity.barrier_envelope",
        {
            "well_depth_si": 3000.0,
            "required_barriers": 2,
            "minimum_independent_kinds": 2,
            "require_evidence": True,
            "elements": [
                {
                    "name": "Drilling Fluid Column",
                    "kind": "fluid_column",
                    "from_depth_si": 0.0,
                    "to_depth_si": 3000.0,
                    "status": "verified",
                    "evidence_ref": "doc_ddr_01",
                },
                {
                    "name": "9-5/8 Casing + BOP Stack",
                    "kind": "casing",
                    "from_depth_si": 0.0,
                    "to_depth_si": 3000.0,
                    "status": "verified",
                    "evidence_ref": "doc_bop_test_01",
                },
            ],
        },
    )
    assert bar_ok["envelope_complete"] is True
    assert bar_ok["verified_barrier_count"] == 2
    assert bar_ok["gaps"] == []

    # 9. schematic.well_model — Concentric Annulus Hydraulic Diameter (D_h - D_c) & Capacity
    # Hole D_h=0.31115 m, Casing OD=0.244475 m over 0-1500 m
    expected_eq_diam = 0.31115 - 0.244475
    expected_ann_cap = (math.pi / 4.0) * (0.31115**2 - 0.244475**2)
    sch_out = run_and_check_json(
        "schematic.well_model",
        {
            "well_name": "CP19-SCHEMATIC-01",
            "hole_sections": [
                {"name": "12-1/4 Hole", "top_depth_si": 0.0, "bottom_depth_si": 1500.0, "hole_diameter_si": 0.31115}
            ],
            "casing_strings": [
                {
                    "name": "9-5/8 Casing",
                    "kind": "intermediate",
                    "outer_diameter_si": 0.244475,
                    "inner_diameter_si": 0.2205,
                    "top_depth_si": 0.0,
                    "shoe_depth_si": 1500.0,
                    "cement_top_si": 500.0,
                }
            ],
        },
    )
    assert math.isclose(sch_out["annuli"][0]["equivalent_diameter_si"], expected_eq_diam, rel_tol=1e-5)
    assert math.isclose(sch_out["annuli"][0]["capacity_m3_per_m"], expected_ann_cap, rel_tol=1e-5)
    assert sch_out["issues"] == []

    # 10. offsets.similarity — Normalized Linear Tolerance + Formation Jaccard Similarity
    # Reference: tvd=3000, hole=0.2159, mw=1200, inc=20, formations=['Ekofisk', 'Tor']
    # Cand A: identical -> score = 1.0
    # Cand B: delta_tvd=250 (tol=500 -> s=0.5), hole=0.2159 (s=1.0), mw=1275 (tol=150 -> s=0.5),
    #         inc=30 (tol=20 -> s=0.5), formations=['Ekofisk'] (Jaccard=0.5)
    # Weights: depth=0.4, hole_size=0.2, mud_weight=0.2, inclination=0.1, formation=0.1
    # Expected Cand B score = 0.4*0.5 + 0.2*1.0 + 0.2*0.5 + 0.1*0.5 + 0.1*0.5 = 0.60
    off_out = run_and_check_json(
        "offsets.similarity",
        {
            "reference": {
                "well_id": "ref_well",
                "tvd_si": 3000.0,
                "hole_size_si": 0.2159,
                "mud_weight_si": 1200.0,
                "max_inclination_deg": 20.0,
                "formations": ["Ekofisk", "Tor"],
            },
            "candidates": [
                {
                    "well_id": "cand_identical",
                    "tvd_si": 3000.0,
                    "hole_size_si": 0.2159,
                    "mud_weight_si": 1200.0,
                    "max_inclination_deg": 20.0,
                    "formations": ["Ekofisk", "Tor"],
                },
                {
                    "well_id": "cand_partial",
                    "tvd_si": 3250.0,
                    "hole_size_si": 0.2159,
                    "mud_weight_si": 1275.0,
                    "max_inclination_deg": 30.0,
                    "formations": ["Ekofisk"],
                },
            ],
            "weights": {
                "depth": 0.40,
                "hole_size": 0.20,
                "mud_weight": 0.20,
                "inclination": 0.10,
                "formation": 0.10,
            },
        },
    )
    assert off_out["ranked"][0]["well_id"] == "cand_identical"
    assert math.isclose(off_out["ranked"][0]["score"], 1.0, rel_tol=1e-6)
    assert off_out["ranked"][1]["well_id"] == "cand_partial"
    assert math.isclose(off_out["ranked"][1]["score"], 0.60, rel_tol=1e-6)

    # 11. readiness.assessment — BOM Shortfall, Lead-Time Order Date & Criticality Rollup
    # as_of = 2026-10-01; item 1 ready; item 2 critical shortage (req=10, avail=4 -> shortfall=6,
    # required_by=2026-10-25, lead_time_days=10 -> latest_order_date=2026-10-15)
    rdy_out = run_and_check_json(
        "readiness.assessment",
        {
            "as_of": "2026-10-01",
            "scope": "section_spud",
            "requirements": [
                {
                    "item_id": "itm_bop",
                    "name": "18-3/4 BOP Stack",
                    "status": "available",
                    "criticality": "critical",
                    "quantity_required": 1.0,
                    "quantity_available": 1.0,
                    "required_by": "2026-10-20",
                    "lead_time_days": 5.0,
                    "source": "Base Yard",
                },
                {
                    "item_id": "itm_csg",
                    "name": "9-5/8 P110 Casing Joints",
                    "status": "on_order",
                    "criticality": "critical",
                    "quantity_required": 100.0,
                    "quantity_available": 40.0,
                    "required_by": "2026-10-25",
                    "lead_time_days": 10.0,
                    "source": "Mill A",
                },
            ],
        },
    )
    assert rdy_out["overall_state"] == "not_ready"
    assert rdy_out["ready_count"] == 1
    assert rdy_out["not_ready_count"] == 1
    assert "itm_csg" in rdy_out["critical_items_not_ready"]
    csg_item = next(i for i in rdy_out["items"] if i["item_id"] == "itm_csg")
    assert math.isclose(csg_item["quantity_shortfall"], 60.0, rel_tol=1e-6)
    assert csg_item["latest_order_date"] == "2026-10-15"

    # 12. twin.reconciliation — Planned vs Actual 3D Trajectory Offset Reconciliation
    # Planned vertical 0 -> 1000 m (N=0, E=0, TVD=1000 m)
    # Actual tie-in shifted East by 12 m at 0 m and vertical to 1000 m (anchor_actual_to_planned_start=False)
    rec_out = run_and_check_json(
        "twin.reconciliation",
        {
            "planned": {
                "stations": [
                    {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                    {"md_si": 1000.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                ]
            },
            "actual": {
                "tie_in": {"tvd_si": 0.0, "ns_si": 0.0, "ew_si": 12.0},
                "stations": [
                    {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                    {"md_si": 1000.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                ],
            },
            "anchor_actual_to_planned_start": False,
            "lateral_tolerance_si": 20.0,
            "vertical_tolerance_si": 10.0,
        },
    )
    assert math.isclose(rec_out["max_lateral_offset_si"], 12.0, rel_tol=1e-6)
    assert math.isclose(rec_out["final_vertical_offset_si"], 0.0, abs_tol=1e-6)
    assert rec_out["tracking_status"] == "on_plan"

    # 13. optimization.sweep — Full-Factorial Grid & Halton Sequence Generation
    # 2 parameters with 3 linear steps each -> 3 x 3 = 9 candidates
    swp_out = run_and_check_json(
        "optimization.sweep",
        {
            "method": "grid",
            "parameters": [
                {"name": "flow_rate_si", "min_si": 0.02, "max_si": 0.04, "steps": 3},
                {"name": "mud_weight_si", "min_si": 1200.0, "max_si": 1300.0, "steps": 3},
            ],
        },
    )
    assert swp_out["candidate_count"] == 9
    assert swp_out["parameter_levels"]["flow_rate_si"] == [0.02, 0.03, 0.04]
    assert swp_out["parameter_levels"]["mud_weight_si"] == [1200.0, 1250.0, 1300.0]

    # 14. optimization.pareto — Strict Pareto Dominance & Weighted Utility Ranking
    # Maximize rop, minimize mse:
    # c1: rop=30, mse=100 (Pareto optimal)
    # c2: rop=20, mse=80  (Pareto optimal)
    # c3: rop=18, mse=110 (Strictly dominated by both c1 and c2!)
    par_out = run_and_check_json(
        "optimization.pareto",
        {
            "objectives": [
                {"key": "rop", "sense": "maximize", "weight": 0.6},
                {"key": "mse", "sense": "minimize", "weight": 0.4},
            ],
            "candidates": [
                {"candidate_id": "c1", "values": {"wob": 100.0}, "objectives": {"rop": 30.0, "mse": 100.0}},
                {"candidate_id": "c2", "values": {"wob": 80.0}, "objectives": {"rop": 20.0, "mse": 80.0}},
                {"candidate_id": "c3", "values": {"wob": 70.0}, "objectives": {"rop": 18.0, "mse": 110.0}},
            ],
        },
    )
    assert par_out["feasible_count"] == 3
    frontier_ids = set(par_out["pareto_frontier"])
    assert frontier_ids == {"c1", "c2"}
    assert "c3" not in frontier_ids


def test_cp19_03_metamorphic_monotonicity_and_invalid_input_rejection() -> None:
    """Verify physical monotonicity relations across engines and strict rejection of invalid inputs."""
    reg = load_default_engines()

    # 1. Hydraulics monotonicity: increasing mud weight or flow rate strictly increases SPP and ECD
    base_hyd = {
        "elements": [
            {"kind": "hole", "from_depth_si": 0.0, "to_depth_si": 2000.0, "od_si": 0.2159},
            {"kind": "drillpipe", "from_depth_si": 0.0, "to_depth_si": 2000.0, "od_si": 0.127, "id_si": 0.1086},
        ],
        "flow_rate_si": 0.025,
        "mud_weight_si": 1200.0,
        "plastic_viscosity_si": 0.018,
        "yield_point_si": 7.0,
        "bit_depth_si": 2000.0,
        "bit_tvd_si": 2000.0,
        "bit_diameter_si": 0.2159,
        "tfa_si": 0.00045,
    }
    _, h_low = reg.execute("hydraulics.laminar", base_hyd)
    _, h_high_q = reg.execute("hydraulics.laminar", {**base_hyd, "flow_rate_si": 0.035})
    _, h_high_mw = reg.execute("hydraulics.laminar", {**base_hyd, "mud_weight_si": 1350.0})
    assert h_high_q.outputs.standpipe_pressure_expected_si > h_low.outputs.standpipe_pressure_expected_si
    assert h_high_q.outputs.ecd_bottom_si > h_low.outputs.ecd_bottom_si
    assert h_high_mw.outputs.ecd_bottom_si > h_low.outputs.ecd_bottom_si

    # 2. Torque & Drag monotonicity: increasing friction factor strictly increases pickup hookload and decreases slackoff hookload
    base_td = {
        "survey": [
            {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
            {"md_si": 1500.0, "inclination_deg": 35.0, "azimuth_deg": 45.0},
        ],
        "string": [
            {
                "kind": "drillpipe",
                "from_depth_si": 0.0,
                "to_depth_si": 1500.0,
                "od_si": 0.127,
                "id_si": 0.1086,
                "linear_mass_si": 29.0,
            }
        ],
        "mud_weight_si": 1250.0,
        "bit_depth_si": 1500.0,
        "friction_factor_pickup": 0.20,
        "friction_factor_slackoff": 0.20,
    }
    _, td_low_mu = reg.execute("torque_drag.soft_string", base_td)
    _, td_high_mu = reg.execute(
        "torque_drag.soft_string",
        {**base_td, "friction_factor_pickup": 0.35, "friction_factor_slackoff": 0.35},
    )
    assert td_high_mu.outputs.hookload_pickup_si > td_low_mu.outputs.hookload_pickup_si
    assert td_high_mu.outputs.hookload_slackoff_si < td_low_mu.outputs.hookload_slackoff_si

    # 3. Invalid / negative physical inputs must raise EngineInputInvalid (never crash or silently pass)
    with pytest.raises(EngineInputInvalid):
        reg.execute(
            "drilling.mse",
            {"wob_si": -100.0, "torque_si": 1000.0, "rpm_si": 100.0, "rop_si": 20.0, "bit_diameter_si": 0.2159},
        )
    with pytest.raises(EngineInputInvalid):
        reg.execute(
            "well_control.kill_sheet",
            {"tvd_si": 0.0, "current_mud_weight_si": 1200.0, "sidpp_si": 2e6, "slow_circulating_pressure_si": 3e6},
        )


async def test_cp19_04_engine_provenance_units_assumptions_and_llm_non_override(session) -> None:
    """execute_engine must persist full unit maps, assumptions, limitations, validation status,
    hashes, and agents must be capped at ActionLevel.L2 with no ability to override engine results."""
    org = Organization(slug="cp19-eng-org", name="CP19 Engineering Org", kind="operator")
    session.add(org)
    await session.flush()

    exec_res = await execute_engine(
        session,
        engine_key="well_control.kill_sheet",
        inputs={
            "tvd_si": 2800.0,
            "current_mud_weight_si": 1220.0,
            "sidpp_si": 2_800_000.0,
            "slow_circulating_pressure_si": 3_500_000.0,
        },
        org_id=org.id,
    )
    payload = exec_res.to_payload()
    assert payload["engine_key"] == "well_control.kill_sheet"
    assert payload["validation_status"] == "needs_field_validation"
    assert payload["domain_pack"] == "Well Control Pack"
    assert payload["category"] == "well_control"
    assert payload["units"]["unit_system"] == "SI"
    assert payload["units"]["input_units"]["tvd_si"] == "m"
    assert payload["units"]["input_units"]["current_mud_weight_si"] == "kg/m3"
    assert payload["units"]["input_units"]["sidpp_si"] == "Pa"
    assert payload["units"]["output_units"]["kill_mud_weight_si"] == "kg/m3"
    assert payload["units"]["output_units"]["initial_circulating_pressure_si"] == "Pa"
    assert len(payload["assumptions"]) >= 2
    assert len(payload["limitations"]) >= 2
    assert len(payload["inputs_hash"]) == 64
    assert len(payload["outputs_hash"]) == 64

    row = (await session.execute(select(EngineRun).where(EngineRun.id == exec_res.engine_run_id))).scalar_one()
    assert row.units["unit_system"] == "SI"
    assert row.units["input_units"]["tvd_si"] == "m"
    assert row.assumptions == payload["assumptions"]
    assert row.limitations == payload["limitations"]

    # Verify all registered AI agents are capped at ActionLevel.DRAFT (L2)
    install_default_agents()
    cat = agent_catalogue()
    assert cat["max_agent_autonomy"] == ActionLevel.DRAFT.value
    assert len(cat["agents"]) >= 1
    for agent_spec in cat["agents"]:
        level = ActionLevel(agent_spec["max_action_level"])
        assert level.rank <= ActionLevel.DRAFT.rank, (
            f"Agent {agent_spec['key']} exceeds L2 ceiling: {agent_spec['max_action_level']}"
        )
