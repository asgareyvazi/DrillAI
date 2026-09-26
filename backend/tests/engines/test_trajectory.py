"""Trajectory engine: minimum-curvature correctness and survey QC.

The reference case is a vertical section followed by a 30 degree build at a constant azimuth,
which is simple enough to verify analytically: the circular-arc (ratio factor) solution is
exact and can be written down by hand.
"""

from __future__ import annotations

import math

import pytest

KB_ELEVATION_M = 30.0
BUILD_LENGTH_M = 300.0
BUILD_ANGLE_DEG = 30.0
HOLD_MD_M = 1000.0


def _build_and_hold_well() -> list[dict]:
    return [
        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": HOLD_MD_M, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": HOLD_MD_M + BUILD_LENGTH_M, "inclination_deg": BUILD_ANGLE_DEG, "azimuth_deg": 0.0},
    ]


def test_build_and_hold_matches_analytical_ratio_factor(engines):
    _, result = engines.execute(
        "trajectory.minimum_curvature",
        {
            "stations": _build_and_hold_well(),
            "kb_elevation_si": KB_ELEVATION_M,
            "vertical_section_azimuth_deg": 0.0,
        },
    )
    output = result.outputs
    angle_rad = math.radians(BUILD_ANGLE_DEG)
    ratio_factor = 2.0 / angle_rad * math.tan(angle_rad / 2.0)

    expected_tvd = HOLD_MD_M + (BUILD_LENGTH_M / 2.0) * (math.cos(0.0) + math.cos(angle_rad)) * ratio_factor
    expected_ns = (BUILD_LENGTH_M / 2.0) * (math.sin(0.0) + math.sin(angle_rad)) * ratio_factor

    assert output.total_tvd_si == pytest.approx(expected_tvd, rel=1e-9)
    assert output.stations[-1].ns_si == pytest.approx(expected_ns, rel=1e-9)
    assert output.stations[-1].ew_si == pytest.approx(0.0, abs=1e-9)
    assert output.total_displacement_si == pytest.approx(expected_ns, rel=1e-9)
    assert output.total_md_si == pytest.approx(HOLD_MD_M + BUILD_LENGTH_M)
    assert output.survey_count == 3


def test_true_vertical_depth_subsea_uses_kb_elevation(engines):
    _, result = engines.execute("trajectory.minimum_curvature", {"stations": _build_and_hold_well(), "kb_elevation_si": 30.0})
    assert result.outputs.stations[-1].tvdss_si == pytest.approx(result.outputs.total_tvd_si - 30.0)


def test_dls_is_constant_through_the_build(engines):
    _, result = engines.execute("trajectory.minimum_curvature", {"stations": _build_and_hold_well()})
    output = result.outputs
    expected_dls = BUILD_ANGLE_DEG / BUILD_LENGTH_M * 30.0  # deg per 30 m
    assert output.max_dls_deg_per_30m == pytest.approx(expected_dls, rel=1e-9)
    assert output.max_dls_md_si == pytest.approx(HOLD_MD_M + BUILD_LENGTH_M)
    # length-weighted: the 300 m build dominates the 1000 m vertical section
    expected_average = expected_dls * BUILD_LENGTH_M / (HOLD_MD_M + BUILD_LENGTH_M)
    assert output.average_dls_deg_per_30m == pytest.approx(expected_average, rel=1e-9)


def test_max_inclination_and_closure_azimuth_reported(engines):
    _, result = engines.execute("trajectory.minimum_curvature", {"stations": _build_and_hold_well()})
    output = result.outputs
    assert output.max_inclination_deg == pytest.approx(BUILD_ANGLE_DEG)
    assert output.max_inclination_md_si == pytest.approx(HOLD_MD_M + BUILD_LENGTH_M)
    assert output.closure_azimuth_deg == pytest.approx(0.0, abs=1e-9)


def test_limit_violations_are_errors_and_make_the_result_infeasible(engines):
    _, result = engines.execute(
        "trajectory.minimum_curvature",
        {"stations": _build_and_hold_well(), "max_dls_limit": 2.0, "max_inc_limit": 20.0},
    )
    names = {violation.name for violation in result.violations}
    assert {"max_dls_limit", "max_inclination_limit"} <= names
    assert all(violation.severity == "error" for violation in result.violations)
    assert result.is_feasible is False
    assert result.outputs.max_dls_deg_per_30m > 2.0


def test_within_limits_is_feasible(engines):
    _, result = engines.execute(
        "trajectory.minimum_curvature",
        {"stations": _build_and_hold_well(), "max_dls_limit": 3.5, "max_inc_limit": 45.0},
    )
    assert result.violations == []
    assert result.is_feasible


def test_duplicate_measured_depth_is_rejected(engines):
    stations = _build_and_hold_well()
    stations.append({"md_si": HOLD_MD_M, "inclination_deg": 1.0, "azimuth_deg": 0.0})
    with pytest.raises(ValueError, match="duplicate"):
        engines.execute("trajectory.minimum_curvature", {"stations": stations})


def test_single_station_is_rejected_by_the_contract(engines):
    from drillai.core.errors import EngineInputInvalid

    with pytest.raises(EngineInputInvalid) as excinfo:
        engines.execute("trajectory.minimum_curvature", {"stations": [{"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0}]})
    assert excinfo.value.details["errors"][0]["loc"] == ("stations",)


def test_long_survey_gap_is_warned_about(engines):
    _, result = engines.execute("trajectory.minimum_curvature", {"stations": _build_and_hold_well()})
    assert any("gap" in warning for warning in result.warnings)


def test_dogleg_and_build_rate_consistency(engines):
    """Dogleg angle, build rate and turn rate must agree with the station geometry."""
    _, result = engines.execute("trajectory.minimum_curvature", {"stations": _build_and_hold_well()})
    station = result.outputs.stations[-1]
    assert station.dogleg_angle_deg == pytest.approx(BUILD_ANGLE_DEG, rel=1e-9)
    assert station.build_rate_deg_per_30m == pytest.approx(BUILD_ANGLE_DEG / BUILD_LENGTH_M * 30.0, rel=1e-9)
    assert station.turn_rate_deg_per_30m == pytest.approx(0.0, abs=1e-9)


def test_tie_in_offsets_are_carried_through(engines):
    _, result = engines.execute(
        "trajectory.minimum_curvature",
        {
            "stations": _build_and_hold_well(),
            "tie_in": {"tvd_si": 100.0, "ns_si": 10.0, "ew_si": -5.0},
        },
    )
    output = result.outputs
    assert output.stations[0].tvd_si == pytest.approx(100.0)
    assert output.stations[0].ns_si == pytest.approx(10.0)
    assert output.stations[0].ew_si == pytest.approx(-5.0)
    assert output.total_displacement_si > 0
