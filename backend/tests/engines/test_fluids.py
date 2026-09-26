"""Wellbore capacity and circulation hydraulics.

Correctness is checked against closed-form results rather than against the engine's own
output: Poiseuille flow for the Newtonian limit, the exact implicit Bingham-plastic
relations for the general case (reconstructed independently in the test), and the orifice
equation for the bit.
"""

from __future__ import annotations

import math

import pytest

GRAVITY = 9.80665
HOLE_OD = 0.2159  # 8.5 in
DP_OD = 0.127  # 5 in
DP_ID = 0.1086
DEPTH = 2000.0
MUD_WEIGHT = 1200.0
PV = 0.02  # 20 cP
YP = 4.78802589802  # 10 lbf/100ft2
FLOW = 0.02  # 0.02 m3/s = 1200 L/min
TFA = 0.00032258  # 6 x 12/32 in nozzles


def _geometry() -> list[dict]:
    return [
        {"kind": "hole", "from_depth_si": 0.0, "to_depth_si": DEPTH, "od_si": HOLE_OD},
        {"kind": "drillpipe", "from_depth_si": 0.0, "to_depth_si": DEPTH, "od_si": DP_OD, "id_si": DP_ID, "linear_mass_si": 29.05},
    ]


def _hydraulics_input(**overrides) -> dict:
    payload = {
        "elements": _geometry(),
        "flow_rate_si": FLOW,
        "mud_weight_si": MUD_WEIGHT,
        "plastic_viscosity_si": PV,
        "yield_point_si": YP,
        "bit_depth_si": DEPTH,
        "bit_tvd_si": DEPTH,
        "bit_diameter_si": HOLE_OD,
        "tfa_si": TFA,
        "nozzle_count": 6,
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- capacity
def test_capacity_volumes_match_cylindrical_geometry(engines):
    _, result = engines.execute("wellbore.capacity", {"elements": _geometry(), "current_depth_si": DEPTH})
    output = result.outputs
    assert output.string_volume_si == pytest.approx(math.pi / 4 * DP_ID**2 * DEPTH, rel=1e-12)
    assert output.annulus_volume_si == pytest.approx(math.pi / 4 * (HOLE_OD**2 - DP_OD**2) * DEPTH, rel=1e-12)
    assert output.total_volume_si == pytest.approx(output.string_volume_si + output.annulus_volume_si)
    assert output.string_capacity_si == pytest.approx(math.pi / 4 * DP_ID**2)


def test_capacity_strokes_and_bottoms_up(engines):
    pump_output, spm = 0.02, 60.0
    _, result = engines.execute(
        "wellbore.capacity",
        {"elements": _geometry(), "current_depth_si": DEPTH, "pump": {"output_per_stroke_si": pump_output, "strokes_per_minute": spm}},
    )
    output = result.outputs
    assert output.string_strokes == pytest.approx(output.string_volume_si / pump_output)
    assert output.annulus_strokes == pytest.approx(output.annulus_volume_si / pump_output)
    assert output.bottoms_up_strokes == pytest.approx(output.annulus_volume_si / pump_output)
    assert output.bottoms_up_volume_si == pytest.approx(output.annulus_volume_si)
    assert output.bottoms_up_time_si == pytest.approx(output.bottoms_up_strokes / spm * 60.0)
    assert output.circulation_time_si == pytest.approx(output.total_strokes / spm * 60.0)


def test_as_designed_volumes_are_independent_of_progress_but_bottoms_up_is_not(engines):
    """The volume basis is explicit: full geometry for volumes, current depth for bottoms-up."""
    _, result = engines.execute(
        "wellbore.capacity",
        {"elements": _geometry(), "current_depth_si": 1000.0, "pump": {"output_per_stroke_si": 0.02}},
    )
    output = result.outputs
    assert output.string_volume_si == pytest.approx(math.pi / 4 * DP_ID**2 * DEPTH, rel=1e-12)
    assert output.annulus_volume_si == pytest.approx(math.pi / 4 * (HOLE_OD**2 - DP_OD**2) * DEPTH, rel=1e-12)
    # bottoms-up only has to clear the annulus below the bit
    assert output.bottoms_up_volume_si == pytest.approx(math.pi / 4 * (HOLE_OD**2 - DP_OD**2) * 1000.0, rel=1e-9)
    assert output.bottoms_up_volume_si == pytest.approx(output.annulus_volume_si / 2.0, rel=1e-9)


def test_capacity_without_pump_reports_no_strokes(engines):
    _, result = engines.execute("wellbore.capacity", {"elements": _geometry(), "current_depth_si": DEPTH})
    assert result.outputs.bottoms_up_strokes is None
    assert any("pump" in warning for warning in result.warnings)


def test_element_geometry_is_validated(engines):
    from drillai.core.errors import EngineInputInvalid

    def messages(payload: dict) -> str:
        with pytest.raises(EngineInputInvalid) as excinfo:
            engines.execute("wellbore.capacity", payload)
        return " ".join(str(error.get("msg", "")) for error in excinfo.value.details["errors"])

    assert "to_depth" in messages(
        {"elements": [{"kind": "hole", "from_depth_si": 100.0, "to_depth_si": 50.0, "od_si": HOLE_OD}], "current_depth_si": 100.0}
    )
    assert "id" in messages(
        {"elements": [{"kind": "drillpipe", "from_depth_si": 0.0, "to_depth_si": 100.0, "od_si": 0.1, "id_si": 0.1}], "current_depth_si": 100.0}
    )


# --------------------------------------------------------------------------- hydraulics
def _bingham_pipe_flow_rate(dpdl: float, diameter: float, mu_p: float, tau_y: float) -> float:
    """Independent implementation of the exact Bingham-plastic pipe relation."""
    wall_stress = dpdl * diameter / 4.0
    newtonian = math.pi * diameter**4 * dpdl / (128.0 * mu_p)
    if wall_stress <= tau_y:
        return 0.0
    x = tau_y / wall_stress
    return newtonian * (1.0 - 4.0 / 3.0 * x + x**4 / 3.0)


def _bingham_slot_flow_rate(dpdl: float, gap: float, width: float, mu_p: float, tau_y: float) -> float:
    """Independent implementation of the exact Bingham-plastic slot relation."""
    shear_ratio = 2.0 * tau_y / (gap * dpdl)
    if shear_ratio >= 1.0:
        return 0.0
    return width * gap**3 * dpdl / (12.0 * mu_p) * (1.0 - 1.5 * shear_ratio + 0.5 * shear_ratio**3)


def test_newtonian_limit_matches_poiseuille(engines):
    """With YP = 0 the solved gradient must equal the Hagen-Poiseuille result."""
    _, result = engines.execute("hydraulics.laminar", _hydraulics_input(yield_point_si=0.0, tfa_si=None, nozzle_count=None))
    output = result.outputs
    velocity = FLOW / (math.pi / 4 * DP_ID**2)
    expected_gradient = 32.0 * PV * velocity / DP_ID**2
    assert output.string_pressure_loss_si / DEPTH == pytest.approx(expected_gradient, rel=1e-9)
    string_stages = [stage for stage in output.stages if stage.kind.startswith("string/")]
    assert len(string_stages) == 1
    assert string_stages[0].hydraulic_diameter_si == pytest.approx(DP_ID)
    assert string_stages[0].pressure_loss_si == pytest.approx(output.string_pressure_loss_si)
    # annular slot in the Newtonian limit: dP/dL = 12 mu V / h^2 with V from the annulus area
    gap = (HOLE_OD - DP_OD) / 2.0
    annulus_area = math.pi / 4 * (HOLE_OD**2 - DP_OD**2)
    annulus_velocity = FLOW / annulus_area
    assert output.annulus_pressure_loss_si / DEPTH == pytest.approx(12.0 * PV * annulus_velocity / gap**2, rel=1e-6)


def test_bingham_pipe_solution_round_trips(engines):
    """Feeding the solved gradient back into the exact relation must return the flow rate."""
    _, result = engines.execute("hydraulics.laminar", _hydraulics_input(tfa_si=None, nozzle_count=None))
    gradient = result.outputs.string_pressure_loss_si / DEPTH
    recovered = _bingham_pipe_flow_rate(gradient, DP_ID, PV, YP)
    assert recovered == pytest.approx(FLOW, rel=1e-6)


def test_bingham_annulus_solution_round_trips(engines):
    _, result = engines.execute("hydraulics.laminar", _hydraulics_input(tfa_si=None, nozzle_count=None))
    gradient = result.outputs.annulus_pressure_loss_si / DEPTH
    gap = (HOLE_OD - DP_OD) / 2.0
    width = math.pi * (HOLE_OD + DP_OD) / 2.0
    recovered = _bingham_slot_flow_rate(gradient, gap, width, PV, YP)
    assert recovered == pytest.approx(FLOW, rel=1e-6)


def test_yield_stress_increases_the_pressure_loss(engines):
    _, low = engines.execute("hydraulics.laminar", _hydraulics_input(yield_point_si=0.0, tfa_si=None, nozzle_count=None))
    _, high = engines.execute("hydraulics.laminar", _hydraulics_input(yield_point_si=20.0, tfa_si=None, nozzle_count=None))
    assert high.outputs.string_pressure_loss_si > low.outputs.string_pressure_loss_si
    assert high.outputs.annulus_pressure_loss_si > low.outputs.annulus_pressure_loss_si


def test_bit_pressure_drop_follows_the_orifice_equation(engines):
    discharge = 0.95
    _, result = engines.execute("hydraulics.laminar", _hydraulics_input(discharge_coefficient=discharge))
    output = result.outputs
    expected = MUD_WEIGHT * FLOW**2 / (2.0 * (discharge * TFA) ** 2)
    assert output.bit_pressure_drop_si == pytest.approx(expected, rel=1e-12)
    assert output.jet_velocity_si == pytest.approx(FLOW / TFA)
    assert output.jet_impact_force_si == pytest.approx(MUD_WEIGHT * FLOW * (FLOW / TFA))
    assert output.bit_hydraulic_horsepower_si == pytest.approx(FLOW * expected)


def test_total_loss_and_standpipe_pressure(engines):
    _, result = engines.execute("hydraulics.laminar", _hydraulics_input())
    output = result.outputs
    total = output.string_pressure_loss_si + output.annulus_pressure_loss_si + output.bit_pressure_drop_si
    assert output.total_pressure_loss_si == pytest.approx(total)
    assert output.standpipe_pressure_expected_si == pytest.approx(total)
    assert output.flow_rate_lpm == pytest.approx(FLOW * 60_000.0)


def test_ecd_definition_and_margin(engines):
    limit = 1300.0
    _, result = engines.execute("hydraulics.laminar", _hydraulics_input(ecd_limit_si=limit))
    output = result.outputs
    expected_ecd = MUD_WEIGHT + output.annulus_pressure_loss_si / (GRAVITY * DEPTH)
    assert output.ecd_bottom_si == pytest.approx(expected_ecd, rel=1e-12)
    assert output.ecd_margin_si == pytest.approx(limit - output.ecd_bottom_si)
    assert output.ecd_bottom_si > MUD_WEIGHT


def test_ecd_limit_and_fracture_gradient_violations(engines):
    _, result = engines.execute(
        "hydraulics.laminar",
        _hydraulics_input(ecd_limit_si=1201.0, fracture_gradient_si=1201.0 * GRAVITY, pore_pressure_gradient_si=1100.0 * GRAVITY),
    )
    names = {violation.name for violation in result.violations}
    assert {"ecd_limit", "fracture_gradient"} <= names
    assert result.is_feasible is False
    assert all(violation.severity == "error" for violation in result.violations if violation.name in {"ecd_limit", "fracture_gradient"})


def test_shallow_ecd_must_stay_above_pore_pressure(engines):
    _, result = engines.execute(
        "hydraulics.laminar",
        _hydraulics_input(pore_pressure_gradient_si=1300.0 * GRAVITY),
    )
    assert any(violation.name == "pore_pressure" for violation in result.violations)
    assert result.is_feasible is False


def test_turbulent_intervals_are_flagged_not_hidden(engines):
    """The laminar model must announce where it does not apply."""
    _, result = engines.execute("hydraulics.laminar", _hydraulics_input(flow_rate_si=0.06, tfa_si=None, nozzle_count=None))
    output = result.outputs
    assert output.turbulent_intervals
    assert any("laminar" in violation.name for violation in result.violations)
    assert any("Re=" in warning for warning in result.warnings)
    # a warning-severity violation must not make the result infeasible
    assert result.is_feasible is True


def test_missing_tfa_disables_bit_metrics_with_a_warning(engines):
    _, result = engines.execute("hydraulics.laminar", _hydraulics_input(tfa_si=None, nozzle_count=None))
    output = result.outputs
    assert output.bit_pressure_drop_si == 0.0
    assert output.jet_velocity_si == 0.0
    assert any("TFA" in warning for warning in result.warnings)


def test_bit_tvd_fallback_is_flagged(engines):
    _, result = engines.execute("hydraulics.laminar", _hydraulics_input(bit_tvd_si=None))
    assert any("bit TVD" in warning for warning in result.warnings)


def test_hydraulic_specific_impact_uses_bit_area(engines):
    _, result = engines.execute("hydraulics.laminar", _hydraulics_input())
    output = result.outputs
    bit_area = math.pi / 4 * HOLE_OD**2
    assert output.hydraulic_horsepower_per_area_si == pytest.approx(output.bit_hydraulic_horsepower_si / bit_area, rel=1e-12)


def test_cuttings_transport_indicators_are_reported(engines):
    _, result = engines.execute("hydraulics.laminar", _hydraulics_input(rop_si=20.0))
    output = result.outputs
    assert output.slip_velocity_si is not None and output.slip_velocity_si > 0
    assert 0.0 <= output.cutting_transport_ratio <= 1.0
    assert 0.0 <= output.cuttings_concentration_volume_fraction < 1.0


def test_low_annular_velocity_triggers_hole_cleaning_warning(engines):
    """Halving the hole size with the same flow must raise velocity; shrinking flow must warn."""
    _, result = engines.execute("hydraulics.laminar", _hydraulics_input(flow_rate_si=0.004, rop_si=30.0))
    assert any(violation.name == "hole_cleaning_transport_ratio" for violation in result.violations)
