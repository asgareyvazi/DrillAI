"""Torque & drag and drilling mechanics, checked against closed-form hand calculations."""

from __future__ import annotations

import math

import pytest

STEEL_E = 210e9
GRAVITY = 9.80665
DP_MASS = 29.05  # kg/m, 5 in 19.5 lb/ft drill pipe
DP_OD, DP_ID = 0.127, 0.1086
MUD_WEIGHT = 1200.0
DEPTH = 2000.0


def _string(depth: float = DEPTH) -> list[dict]:
    return [
        {
            "kind": "drillpipe",
            "from_depth_si": 0.0,
            "to_depth_si": depth,
            "od_si": DP_OD,
            "id_si": DP_ID,
            "linear_mass_si": DP_MASS,
        }
    ]


def _vertical_survey(depth: float = DEPTH) -> list[dict]:
    return [
        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": depth, "inclination_deg": 0.0, "azimuth_deg": 0.0},
    ]


# --------------------------------------------------------------------------- torque & drag
def test_vertical_string_hookload_equals_buoyed_weight(engines):
    _, result = engines.execute(
        "torque_drag.soft_string",
        {"survey": _vertical_survey(), "string": _string(), "mud_weight_si": MUD_WEIGHT, "bit_depth_si": DEPTH},
    )
    output = result.outputs
    buoyancy = 1.0 - MUD_WEIGHT / 7850.0
    weight_air = DP_MASS * GRAVITY * DEPTH
    assert output.string_weight_air_si == pytest.approx(weight_air, rel=1e-12)
    assert output.buoyancy_factor == pytest.approx(buoyancy, rel=1e-12)
    assert output.string_weight_buoyed_si == pytest.approx(weight_air * buoyancy, rel=1e-12)
    # a vertical string has no normal force, so there is no friction: all three tensions agree
    assert output.hookload_pickup_si == pytest.approx(weight_air * buoyancy, rel=1e-9)
    assert output.hookload_slackoff_si == pytest.approx(output.hookload_pickup_si, rel=1e-9)
    assert output.hookload_rotating_si == pytest.approx(output.hookload_pickup_si, rel=1e-9)
    assert output.drag_up_si == pytest.approx(0.0, abs=1e-6)
    assert output.surface_torque_off_bottom_si == pytest.approx(0.0, abs=1e-6)


def test_axial_stretch_matches_integral_of_tension_over_ea(engines):
    _, result = engines.execute(
        "torque_drag.soft_string",
        {"survey": _vertical_survey(), "string": _string(), "mud_weight_si": MUD_WEIGHT, "bit_depth_si": DEPTH},
    )
    output = result.outputs
    buoyed_weight = output.string_weight_buoyed_si
    wall_area = math.pi / 4 * (DP_OD**2 - DP_ID**2)
    # tension decreases linearly from the buoyed weight at surface to zero at the bit
    expected = (buoyed_weight / 2.0) * DEPTH / (STEEL_E * wall_area)
    assert output.stretch_si == pytest.approx(expected, rel=1e-6)


def test_deviated_well_creates_drag_in_the_expected_order(engines):
    survey = [
        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 30.0},
        {"md_si": 1000.0, "inclination_deg": 0.0, "azimuth_deg": 30.0},
        {"md_si": 2000.0, "inclination_deg": 60.0, "azimuth_deg": 30.0},
    ]
    _, result = engines.execute(
        "torque_drag.soft_string",
        {
            "survey": survey,
            "string": _string(),
            "mud_weight_si": MUD_WEIGHT,
            "bit_depth_si": 2000.0,
            "friction_factor_pickup": 0.25,
            "friction_factor_slackoff": 0.25,
        },
    )
    output = result.outputs
    assert output.hookload_pickup_si > output.hookload_rotating_si > output.hookload_slackoff_si
    assert output.drag_up_si > 0
    assert output.drag_down_si > 0
    assert output.side_force_max_si > 0
    assert output.side_force_max_md_si == pytest.approx(2000.0)
    # the rotating (no-drag) tension integrates the vertical component of the weight, so it is
    # lower than the full buoyed weight as soon as the well leaves vertical
    assert output.hookload_rotating_si < output.string_weight_buoyed_si


def test_zero_friction_removes_drag_but_not_weight(engines):
    survey = [
        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": 2000.0, "inclination_deg": 60.0, "azimuth_deg": 0.0},
    ]
    _, result = engines.execute(
        "torque_drag.soft_string",
        {
            "survey": survey,
            "string": _string(),
            "mud_weight_si": MUD_WEIGHT,
            "bit_depth_si": DEPTH,
            "friction_factor_pickup": 0.0,
            "friction_factor_slackoff": 0.0,
            "friction_factor_rotating": 0.0,
        },
    )
    output = result.outputs
    assert output.drag_up_si == pytest.approx(0.0, abs=1e-6)
    assert output.surface_torque_off_bottom_si == pytest.approx(0.0, abs=1e-9)
    assert output.hookload_rotating_si < output.string_weight_buoyed_si  # cosine projection of weight


def test_rotating_torque_is_produced_in_a_build_section(engines):
    survey = [
        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": 1000.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": 2000.0, "inclination_deg": 45.0, "azimuth_deg": 0.0},
    ]
    _, result = engines.execute(
        "torque_drag.soft_string",
        {"survey": survey, "string": _string(), "mud_weight_si": MUD_WEIGHT, "bit_depth_si": DEPTH},
    )
    assert result.outputs.surface_torque_off_bottom_si > 0


def test_bit_torque_is_estimated_and_flagged_when_not_measured(engines):
    survey = [
        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": 2000.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
    ]
    _, result = engines.execute(
        "torque_drag.soft_string",
        {
            "survey": survey,
            "string": _string(),
            "mud_weight_si": MUD_WEIGHT,
            "bit_depth_si": DEPTH,
            "wob_si": 100_000.0,
            "bit_diameter_si": 0.2159,
        },
    )
    output = result.outputs
    expected = 0.30 * 100_000.0 * 0.2159 / 2.0
    assert output.bit_torque_si == pytest.approx(expected, rel=1e-12)
    assert output.surface_torque_on_bottom_si == pytest.approx(output.surface_torque_off_bottom_si + expected)
    assert any("bit torque estimated" in warning for warning in output.warnings)
    assert any("estimated" in assumption or "coefficients" in assumption for assumption in result.assumptions_applied)


def test_rig_hookload_limit_produces_an_error_violation(engines):
    _, result = engines.execute(
        "torque_drag.soft_string",
        {
            "survey": _vertical_survey(),
            "string": _string(),
            "mud_weight_si": MUD_WEIGHT,
            "bit_depth_si": DEPTH,
            "rig_hookload_limit_si": 100_000.0,
        },
    )
    assert any(violation.name == "rig_hookload_limit" for violation in result.violations)
    assert result.is_feasible is False


def test_string_that_stops_short_of_the_bit_is_integrated_over_its_own_extent(engines):
    """A sparse survey must not hide the weight of a string that ends above the last station."""
    _, result = engines.execute(
        "torque_drag.soft_string",
        {"survey": _vertical_survey(), "string": _string(1000.0), "mud_weight_si": MUD_WEIGHT, "bit_depth_si": 2000.0},
    )
    output = result.outputs
    expected = DP_MASS * GRAVITY * 1000.0 * (1 - MUD_WEIGHT / 7850.0)
    assert output.hookload_rotating_si == pytest.approx(expected, rel=1e-9)
    assert output.stations[-1].md_si == pytest.approx(1000.0)
    assert any("does not reach the bit" in warning for warning in output.warnings)


def test_transform_of_a_partial_string_matches_the_full_string_at_the_same_depth(engines):
    """Tension at 1000 m must be identical whether the string ends there or continues."""
    _, partial = engines.execute(
        "torque_drag.soft_string",
        {"survey": _vertical_survey(), "string": _string(1000.0), "mud_weight_si": MUD_WEIGHT, "bit_depth_si": 2000.0},
    )
    survey = [
        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": 1000.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": 2000.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
    ]
    _, full = engines.execute(
        "torque_drag.soft_string",
        {"survey": survey, "string": _string(), "mud_weight_si": MUD_WEIGHT, "bit_depth_si": 2000.0},
    )
    partial_node = next(station for station in partial.outputs.stations if station.md_si == pytest.approx(1000.0))
    full_node = next(station for station in full.outputs.stations if station.md_si == pytest.approx(1000.0))
    assert partial_node.tension_rotating_si == pytest.approx(full_node.tension_rotating_si, rel=1e-12)


# --------------------------------------------------------------------------- MSE
def _mse_input(**overrides) -> dict:
    payload = {
        "wob_si": 50_000.0,
        "torque_si": 3_000.0,
        "rpm_si": 120.0,
        "rop_si": 20.0,
        "bit_diameter_si": 0.2159,
    }
    payload.update(overrides)
    return payload


def test_mse_matches_the_definition(engines):
    _, result = engines.execute("drilling.mse", _mse_input())
    output = result.outputs
    area = math.pi / 4 * 0.2159**2
    rop_per_second = 20.0 / 3600.0
    expected_axial = 50_000.0 / area
    expected_torsional = (2.0 * math.pi * 120.0 / 60.0 * 3_000.0) / (area * rop_per_second)
    assert output.bit_area_si == pytest.approx(area, rel=1e-12)
    assert output.mse_axial_component_si == pytest.approx(expected_axial, rel=1e-12)
    assert output.mse_torsional_component_si == pytest.approx(expected_torsional, rel=1e-12)
    assert output.mse_si == pytest.approx(expected_axial + expected_torsional, rel=1e-12)
    assert output.mse_psi == pytest.approx(output.mse_si / 6894.757293168361, rel=1e-12)
    assert output.wob_per_diameter_si == pytest.approx(50_000.0 / 0.2159)


def test_drilling_efficiency_uses_confined_compressive_strength(engines):
    _, result = engines.execute("drilling.mse", _mse_input(confined_compressive_strength_si=150e6))
    output = result.outputs
    assert output.drilling_efficiency == pytest.approx(min(1.0, 150e6 / output.mse_si), rel=1e-12)
    assert output.drilling_efficiency < 1.0


def test_reference_mse_drives_the_dysfunction_indicator(engines):
    _, efficient = engines.execute("drilling.mse", _mse_input(reference_mse_si=180e6))
    _, dysfunctional = engines.execute("drilling.mse", _mse_input(reference_mse_si=100e6))
    assert efficient.outputs.dysfunction_indicator == "efficient"
    assert efficient.outputs.dysfunction_ratio == pytest.approx(efficient.outputs.mse_si / 180e6)
    assert dysfunctional.outputs.dysfunction_indicator == "dysfunction_likely"
    assert any(violation.name == "mse_dysfunction" for violation in dysfunctional.violations)
    # a warning-severity violation must not mark the run infeasible
    assert dysfunctional.is_feasible is True


def test_low_efficiency_is_flagged_without_a_reference(engines):
    _, result = engines.execute("drilling.mse", _mse_input(confined_compressive_strength_si=40e6))
    assert result.outputs.drilling_efficiency is not None and result.outputs.drilling_efficiency < 1.0
    assert result.outputs.dysfunction_indicator == "dysfunction_likely"
    assert any(violation.name == "drilling_efficiency" for violation in result.violations)


def test_mse_requires_positive_parameters(engines):
    from drillai.core.errors import EngineInputInvalid

    with pytest.raises(EngineInputInvalid):
        engines.execute("drilling.mse", _mse_input(rop_si=0.0))
    with pytest.raises(EngineInputInvalid):
        engines.execute("drilling.mse", _mse_input(rpm_si=-1.0))
