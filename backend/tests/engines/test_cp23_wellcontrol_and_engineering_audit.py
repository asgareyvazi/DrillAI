"""CP23 Well-Control & Engineering Correctness Audit test suite.

Validates:
1. CP23-A: Explicit `wait_and_weight` (single-circulation Engineer's Method) vs `drillers_method`
   (two-circulation Driller's Method) kill-sheet stroke/pressure schedules.
2. CP23-B: Separation of static shoe `mud_weight_margin_si` (`kg/m3`) from preliminary single-bubble
   static `volumetric_kick_tolerance_si` (`m3`).
3. CP23-C: Kill-rate pressure (`KRP`) annular-friction assumption disclosure, input provenance
   verification (`recorded_scr_test` vs `estimated` / `manual_override`), `hydraulics.laminar`
   `regime_validity` boundary (`Re > 2100`), and `tubulars.api_5c3` pipe-body scope boundary.
"""

from __future__ import annotations

import pytest

from drillai.engines.registry import EngineRegistry

GRAVITY = 9.80665


def test_cp23_wait_and_weight_vs_drillers_method_schedule(engines: EngineRegistry) -> None:
    """Wait-and-Weight is 1 circulation (ICP->FCP to bit, FCP in annulus); Driller's Method is 2 circulations."""
    base_inputs = {
        "tvd_si": 3000.0,
        "current_mud_weight_si": 1200.0,
        "sidpp_si": 3.5e6,
        "sicp_si": 4.2e6,
        "slow_circulating_pressure_si": 4.5e6,
        "strokes_to_bit": 1800.0,
        "annulus_strokes": 4200.0,
        "pump_output_per_stroke_si": 0.018,
        "kill_rate_si": 0.015,
        "casing_shoe_tvd_si": 1800.0,
        "shoe_fracture_mud_weight_si": 1750.0,
    }

    # 1. Wait-and-Weight (Engineer's Method)
    _, ww_res = engines.execute("well_control.kill_sheet", {**base_inputs, "method": "wait_and_weight"})
    ww = ww_res.outputs
    expected_kmw = 1200.0 + 3.5e6 / (GRAVITY * 3000.0)
    expected_icp = 4.5e6 + 3.5e6
    expected_fcp = 4.5e6 * (expected_kmw / 1200.0)

    assert ww.method == "wait_and_weight"
    assert ww.circulation_cycles == 1
    assert ww.kill_mud_weight_si == pytest.approx(expected_kmw, rel=1e-12)
    assert ww.initial_circulating_pressure_si == pytest.approx(expected_icp, rel=1e-12)
    assert ww.final_circulating_pressure_si == pytest.approx(expected_fcp, rel=1e-12)
    assert ww.total_kill_strokes == pytest.approx(1800.0 + 4200.0)
    assert ww.schedule[0].phase == "initial"
    assert ww.schedule[0].strokes == 0.0
    assert ww.schedule[0].pressure_si == pytest.approx(expected_icp)
    assert ww.schedule[-2].phase == "final"
    assert ww.schedule[-2].strokes == pytest.approx(1800.0)
    assert ww.schedule[-2].pressure_si == pytest.approx(expected_fcp)
    assert ww.schedule[-1].phase == "displace_annulus_at_fcp"
    assert ww.schedule[-1].strokes == pytest.approx(6000.0)
    assert ww.schedule[-1].pressure_si == pytest.approx(expected_fcp)
    assert any("Wait-and-Weight" in note for note in ww.notes)

    # 2. Driller's Method (Two-Circulation)
    _, dm_res = engines.execute("well_control.kill_sheet", {**base_inputs, "method": "drillers_method"})
    dm = dm_res.outputs
    assert dm.method == "drillers_method"
    assert dm.circulation_cycles == 2
    assert dm.total_kill_strokes == pytest.approx((1800.0 + 4200.0) * 2)

    # Circulation 1 rows hold constant ICP with original mud weight (1200 kg/m3)
    c1_rows = [r for r in dm.schedule if r.phase.startswith("circulation_1_")]
    assert len(c1_rows) == 3
    assert [r.strokes for r in c1_rows] == [0.0, 1800.0, 6000.0]
    for row in c1_rows:
        assert row.pressure_si == pytest.approx(expected_icp, rel=1e-12)
        assert row.mud_weight_si == pytest.approx(1200.0, rel=1e-12)

    # Circulation 2 rows step from ICP down to FCP over 6000 -> 7800 strokes, then hold FCP to 12000 strokes
    c2_rows = [r for r in dm.schedule if r.phase.startswith("circulation_2_")]
    assert c2_rows[-2].phase == "circulation_2_kill_mud_at_bit"
    assert c2_rows[-2].strokes == pytest.approx(7800.0)
    assert c2_rows[-2].pressure_si == pytest.approx(expected_fcp, rel=1e-12)
    assert c2_rows[-2].mud_weight_si == pytest.approx(expected_kmw, rel=1e-12)
    assert c2_rows[-1].phase == "circulation_2_displace_annulus_at_fcp"
    assert c2_rows[-1].strokes == pytest.approx(12000.0)
    assert c2_rows[-1].pressure_si == pytest.approx(expected_fcp, rel=1e-12)
    assert any("Driller's method (two-circulation)" in note for note in dm.notes)


def test_cp23_mud_weight_margin_vs_volumetric_kick_tolerance(engines: EngineRegistry) -> None:
    """Separates static shoe mud-weight margin (kg/m3) from single-bubble volumetric kick tolerance (m3)."""
    inputs_without_influx = {
        "tvd_si": 3000.0,
        "current_mud_weight_si": 1250.0,
        "sidpp_si": 2.0e6,
        "sicp_si": 2.8e6,
        "slow_circulating_pressure_si": 4.0e6,
        "strokes_to_bit": 1500.0,
        "casing_shoe_tvd_si": 2000.0,
        "shoe_fracture_mud_weight_si": 1650.0,
    }
    _, res_no_influx = engines.execute("well_control.kill_sheet", inputs_without_influx)
    out_no_influx = res_no_influx.outputs
    assert out_no_influx.mud_weight_margin_si == pytest.approx(1650.0 - 1250.0)
    assert out_no_influx.kick_tolerance_mud_weight_si == pytest.approx(400.0)
    assert out_no_influx.volumetric_kick_tolerance_si is None
    assert (
        out_no_influx.volumetric_kick_tolerance_basis
        == "not_computed_requires_influx_density_and_annulus_capacity"
    )

    # Now supply gas influx density (250 kg/m3) and annular capacity at shoe (0.025 m3/m)
    inputs_with_influx = {
        **inputs_without_influx,
        "influx_density_si": 250.0,
        "annulus_capacity_at_shoe_si": 0.025,
    }
    _, res_influx = engines.execute("well_control.kill_sheet", inputs_with_influx)
    out_influx = res_influx.outputs

    # Independent hand calculation:
    # Formation pressure = 1250 * g * 3000 + 2.0e6
    # Base pressure at shoe (2000 m) with 1000 m of mud below shoe = 1250 * g * 2000 + 2.0e6
    # Fracture pressure at shoe = 1650 * g * 2000
    # Allowable extra pressure at shoe from gas column = (1650 - 1250) * g * 2000 - 2.0e6
    p_frac_shoe = 1650.0 * GRAVITY * 2000.0
    p_shoe_base = 1250.0 * GRAVITY * 2000.0 + 2.0e6
    expected_height = (p_frac_shoe - p_shoe_base) / ((1250.0 - 250.0) * GRAVITY)
    expected_vol = expected_height * 0.025

    assert out_influx.volumetric_kick_tolerance_basis == "preliminary_single_bubble_unmigrated_at_shoe"
    assert out_influx.max_allowable_influx_height_at_shoe_si == pytest.approx(expected_height, rel=1e-12)
    assert out_influx.volumetric_kick_tolerance_si == pytest.approx(expected_vol, rel=1e-12)


def test_cp23_kill_sheet_input_provenance_and_friction_assumptions(engines: EngineRegistry) -> None:
    """Verifies KRP friction assumption disclosure and input provenance warnings."""
    base = {
        "tvd_si": 2500.0,
        "current_mud_weight_si": 1200.0,
        "sidpp_si": 2.5e6,
        "slow_circulating_pressure_si": 4.2e6,
        "strokes_to_bit": 1600.0,
        "casing_shoe_tvd_si": 1500.0,
        "shoe_fracture_mud_weight_si": 1700.0,
    }

    _, verified_res = engines.execute(
        "well_control.kill_sheet",
        {
            **base,
            "input_provenance": {
                "slow_circulating_pressure_si": "recorded_scr_test",
                "shoe_fracture_mud_weight_si": "lot_fit_test",
            },
        },
    )
    assert verified_res.outputs.input_provenance_verified is True
    assert verified_res.outputs.friction_assumption == "drillstring_dominated_krp_annular_friction_neglected"
    assert not any("provenance" in w for w in verified_res.warnings)

    _, estimated_res = engines.execute(
        "well_control.kill_sheet",
        {
            **base,
            "input_provenance": {
                "slow_circulating_pressure_si": "estimated",
                "shoe_fracture_mud_weight_si": "manual_override",
            },
        },
    )
    assert estimated_res.outputs.input_provenance_verified is False
    assert any("estimated or manual_override provenance" in w for w in estimated_res.warnings)


def test_cp23_hydraulics_turbulent_regime_validity_and_tubulars_scope(engines: EngineRegistry) -> None:
    """Verifies hydraulics.laminar regime_validity and tubulars.api_5c3 pipe-body scope flags."""
    elements = [
        {"kind": "casing", "from_depth_si": 0.0, "to_depth_si": 2000.0, "od_si": 0.2445, "id_si": 0.2205},
        {"kind": "drillpipe", "from_depth_si": 0.0, "to_depth_si": 2000.0, "od_si": 0.127, "id_si": 0.1086},
    ]
    # 1. Low flow / high viscosity -> strictly laminar
    _, lam_res = engines.execute(
        "hydraulics.laminar",
        {
            "elements": elements,
            "flow_rate_si": 0.004,
            "mud_weight_si": 1250.0,
            "plastic_viscosity_si": 0.045,
            "yield_point_si": 18.0,
            "tfa_si": 3.4e-4,
            "bit_diameter_si": 0.2159,
            "bit_depth_si": 2000.0,
            "bit_tvd_si": 2000.0,
        },
    )
    assert lam_res.outputs.turbulent_intervals == []
    assert lam_res.outputs.regime_validity == "laminar_valid"

    # 2. High flow / low viscosity -> exceeds critical Reynolds (Re > 2100)
    _, turb_res = engines.execute(
        "hydraulics.laminar",
        {
            "elements": elements,
            "flow_rate_si": 0.045,
            "mud_weight_si": 1250.0,
            "plastic_viscosity_si": 0.012,
            "yield_point_si": 4.0,
            "tfa_si": 3.4e-4,
            "bit_diameter_si": 0.2159,
            "bit_depth_si": 2000.0,
            "bit_tvd_si": 2000.0,
        },
    )
    assert len(turb_res.outputs.turbulent_intervals) >= 1
    assert turb_res.outputs.regime_validity == "non_certified_turbulent_exceedance"
    assert any(v.name == "laminar_assumption" for v in turb_res.violations)

    # 3. Tubulars API 5C3 scope classification
    _, tub_res = engines.execute(
        "tubulars.api_5c3",
        {
            "segments": [
                {
                    "name": "9-5/8 N-80",
                    "from_depth_si": 0.0,
                    "to_depth_si": 1500.0,
                    "outer_diameter_si": 0.244475,
                    "wall_thickness_si": 0.0119888,
                    "yield_stress_si": 551.58e6,
                    "internal_pressure_si": 25.0e6,
                    "external_pressure_si": 18.0e6,
                }
            ]
        },
    )
    assert tub_res.outputs.scope_classification == "pipe_body_only_connections_not_rated"
    assert tub_res.outputs.connection_verification_required is True


def test_cp23_output_unit_inference_consistency() -> None:
    """Verifies that output unit metadata maps kill-sheet outputs to their true SI units."""
    from drillai.engines.service import _extract_model_units
    from drillai.engines.wellcontrol import KillSheetOutput

    units = _extract_model_units(KillSheetOutput)
    assert units["kill_mud_weight_si"] == "kg/m3"
    assert units["mud_weight_margin_si"] == "kg/m3"
    assert units["kick_tolerance_mud_weight_si"] == "kg/m3"
    assert units["volumetric_kick_tolerance_si"] == "m3"
    assert units["max_allowable_influx_height_at_shoe_si"] == "m"
    assert units["shoe_margin_si"] == "Pa"
    assert units["maasp_si"] == "Pa"
    assert units["initial_circulating_pressure_si"] == "Pa"
    assert units["final_circulating_pressure_si"] == "Pa"
    assert units["time_to_kill_si"] == "s"

