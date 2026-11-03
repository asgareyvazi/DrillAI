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
        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0, "azimuth_reference": "grid", "azimuth_reference_id": "test-grid"},
        {"md_si": HOLD_MD_M, "inclination_deg": 0.0, "azimuth_deg": 0.0, "azimuth_reference": "grid", "azimuth_reference_id": "test-grid"},
        {"md_si": HOLD_MD_M + BUILD_LENGTH_M, "inclination_deg": BUILD_ANGLE_DEG, "azimuth_deg": 0.0, "azimuth_reference": "grid", "azimuth_reference_id": "test-grid"},
    ]


def test_build_and_hold_matches_analytical_ratio_factor(engines):
    _, result = engines.execute(
        "trajectory.minimum_curvature",
        {
            "stations": _build_and_hold_well(),
            "kb_elevation_si": KB_ELEVATION_M,
            "vertical_section_azimuth_deg": 0.0,
            "vertical_section_azimuth_reference": "grid",
            "vertical_section_azimuth_reference_id": "test-grid",
        },
    )
    output = result.outputs
    assert output.datum == "unknown"
    assert all(station.tvdss_si is None for station in output.stations)
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


def test_vertical_section_azimuth_requires_exact_declared_frame(engines):
    from drillai.core.errors import EngineInputInvalid

    stations = _build_and_hold_well()
    with pytest.raises(EngineInputInvalid) as missing_frame:
        engines.execute(
            "trajectory.minimum_curvature",
            {"stations": stations, "vertical_section_azimuth_deg": 45.0},
        )
    assert "requires an explicit reference type and exact reference identity" in str(
        missing_frame.value.details["errors"]
    )

    with pytest.raises(EngineInputInvalid) as mismatched_frame:
        engines.execute(
            "trajectory.minimum_curvature",
            {
                "stations": stations,
                "vertical_section_azimuth_deg": 45.0,
                "vertical_section_azimuth_reference": "grid",
                "vertical_section_azimuth_reference_id": "EPSG:32632",
            },
        )
    assert "does not match target trajectory frame" in str(mismatched_frame.value.details["errors"])


def test_multi_interval_constant_build_matches_independent_closed_form(engines):
    """A constant-curvature arc has a closed-form integral independent of the segment ratio-factor loop."""
    build_angle = math.radians(30.0)
    build_length = 300.0
    curvature_rad_per_m = build_angle / build_length
    stations = [
        {
            "md_si": md,
            "inclination_deg": inclination,
            "azimuth_deg": 90.0,
            "azimuth_reference": "grid",
            "azimuth_reference_id": "test-grid",
        }
        for md, inclination in ((0.0, 0.0), (100.0, 10.0), (200.0, 20.0), (300.0, 30.0), (500.0, 30.0))
    ]

    _, result = engines.execute("trajectory.minimum_curvature", {"stations": stations})

    # Integrate the continuous constant-curvature arc in inclination space, then add the 200 m tangent hold.
    expected_tvd = math.sin(build_angle) / curvature_rad_per_m + 200.0 * math.cos(build_angle)
    expected_ew = (1.0 - math.cos(build_angle)) / curvature_rad_per_m + 200.0 * math.sin(build_angle)
    assert result.outputs.total_tvd_si == pytest.approx(expected_tvd, rel=1e-10)
    assert result.outputs.stations[-1].ew_si == pytest.approx(expected_ew, rel=1e-10)
    assert result.outputs.stations[-1].ns_si == pytest.approx(0.0, abs=1e-9)
    assert result.outputs.max_dls_deg_per_30m == pytest.approx(3.0, rel=1e-10)
    assert result.outputs.azimuth_reference == "grid"
    assert result.outputs.azimuth_reference_id == "test-grid"


def test_reference_conversion_is_explicit_and_preserves_input_provenance(engines):
    magnetic_stations = [
        {
            "md_si": 0.0,
            "inclination_deg": 0.0,
            "azimuth_deg": 359.0,
            "azimuth_reference": "magnetic",
            "azimuth_reference_id": "geomagnetic-reference-2026",
            "grid_correction_deg": 3.0,
        },
        {
            "md_si": 300.0,
            "inclination_deg": 90.0,
            "azimuth_deg": 1.0,
            "azimuth_reference": "magnetic",
            "azimuth_reference_id": "geomagnetic-reference-2026",
            "grid_correction_deg": 3.0,
        },
    ]
    _, converted = engines.execute(
        "trajectory.minimum_curvature",
        {
            "stations": magnetic_stations,
            "target_azimuth_reference": "grid",
            "target_azimuth_reference_id": "EPSG:32631",
        },
    )
    _, as_grid = engines.execute(
        "trajectory.minimum_curvature",
        {
            "stations": [
                {
                    "md_si": 0.0,
                    "inclination_deg": 0.0,
                    "azimuth_deg": 2.0,
                    "azimuth_reference": "grid",
                    "azimuth_reference_id": "EPSG:32631",
                },
                {
                    "md_si": 300.0,
                    "inclination_deg": 90.0,
                    "azimuth_deg": 4.0,
                    "azimuth_reference": "grid",
                    "azimuth_reference_id": "EPSG:32631",
                },
            ],
        },
    )

    assert converted.outputs.azimuth_reference == "grid"
    assert converted.outputs.azimuth_reference_id == "EPSG:32631"
    assert converted.outputs.stations[0].input_azimuth_deg == 359.0
    assert converted.outputs.stations[0].input_azimuth_reference == "magnetic"
    assert converted.outputs.stations[0].input_azimuth_reference_id == "geomagnetic-reference-2026"
    assert converted.outputs.stations[0].azimuth_deg == pytest.approx(2.0)
    assert converted.outputs.stations[0].azimuth_correction_applied_deg == 3.0
    assert converted.outputs.stations[-1].azimuth_deg == pytest.approx(4.0)
    assert converted.outputs.stations[-1].ew_si == pytest.approx(as_grid.outputs.stations[-1].ew_si, abs=1e-10)
    assert converted.outputs.stations[-1].ns_si == pytest.approx(as_grid.outputs.stations[-1].ns_si, abs=1e-10)


def test_reference_changes_without_explicit_correction_or_common_identity_are_rejected(engines):
    from drillai.core.errors import EngineInputInvalid

    unknown = [
        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
        {"md_si": 300.0, "inclination_deg": 30.0, "azimuth_deg": 0.0},
    ]
    with pytest.raises(EngineInputInvalid) as unknown_error:
        engines.execute("trajectory.minimum_curvature", {"stations": unknown})
    assert "unknown azimuth_reference" in str(unknown_error.value.details["errors"])

    unknown_type_with_identity = [
        {
            "md_si": 0.0,
            "inclination_deg": 0.0,
            "azimuth_deg": 0.0,
            "azimuth_reference": "unknown",
            "azimuth_reference_id": "unclassified-frame-7",
        },
        {
            "md_si": 300.0,
            "inclination_deg": 30.0,
            "azimuth_deg": 0.0,
            "azimuth_reference": "unknown",
            "azimuth_reference_id": "unclassified-frame-7",
        },
    ]
    with pytest.raises(EngineInputInvalid) as unknown_identity_error:
        engines.execute("trajectory.minimum_curvature", {"stations": unknown_type_with_identity})
    assert "unknown azimuth_reference" in str(unknown_identity_error.value.details["errors"])

    incompatible = [
        {
            "md_si": 0.0,
            "inclination_deg": 0.0,
            "azimuth_deg": 0.0,
            "azimuth_reference": "grid",
            "azimuth_reference_id": "EPSG:32631",
        },
        {
            "md_si": 300.0,
            "inclination_deg": 30.0,
            "azimuth_deg": 0.0,
            "azimuth_reference": "grid",
            "azimuth_reference_id": "EPSG:32632",
        },
    ]
    with pytest.raises(EngineInputInvalid) as incompatible_error:
        engines.execute("trajectory.minimum_curvature", {"stations": incompatible})
    assert "incompatible azimuth reference identities" in str(incompatible_error.value.details["errors"])

    missing_correction = [
        {
            "md_si": 0.0,
            "inclination_deg": 0.0,
            "azimuth_deg": 0.0,
            "azimuth_reference": "magnetic",
            "azimuth_reference_id": "geomagnetic-reference-2026",
        },
        {
            "md_si": 300.0,
            "inclination_deg": 30.0,
            "azimuth_deg": 0.0,
            "azimuth_reference": "magnetic",
            "azimuth_reference_id": "geomagnetic-reference-2026",
        },
    ]
    with pytest.raises(EngineInputInvalid) as missing_error:
        engines.execute(
            "trajectory.minimum_curvature",
            {
                "stations": missing_correction,
                "target_azimuth_reference": "grid",
                "target_azimuth_reference_id": "EPSG:32631",
            },
        )
    assert "requires an explicit signed grid_correction_deg" in str(missing_error.value.details["errors"])

    with pytest.raises(EngineInputInvalid) as tie_in_frame_error:
        engines.execute(
            "trajectory.minimum_curvature",
            {
                "stations": [
                    {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0, "azimuth_reference": "grid", "azimuth_reference_id": "EPSG:32631"},
                    {"md_si": 300.0, "inclination_deg": 30.0, "azimuth_deg": 0.0, "azimuth_reference": "grid", "azimuth_reference_id": "EPSG:32631"},
                ],
                "tie_in": {
                    "ns_si": 10.0,
                    "azimuth_reference": "grid",
                    "azimuth_reference_id": "EPSG:32632",
                },
            },
        )
    assert "does not match target trajectory frame" in str(tie_in_frame_error.value.details["errors"])

    out_of_range_correction = [
        {
            "md_si": 0.0,
            "inclination_deg": 0.0,
            "azimuth_deg": 0.0,
            "azimuth_reference": "magnetic",
            "azimuth_reference_id": "geomagnetic-reference-2026",
            "grid_correction_deg": 361.0,
        },
        {
            "md_si": 300.0,
            "inclination_deg": 30.0,
            "azimuth_deg": 0.0,
            "azimuth_reference": "magnetic",
            "azimuth_reference_id": "geomagnetic-reference-2026",
            "grid_correction_deg": 361.0,
        },
    ]
    with pytest.raises(EngineInputInvalid) as correction_error:
        engines.execute(
            "trajectory.minimum_curvature",
            {
                "stations": out_of_range_correction,
                "target_azimuth_reference": "grid",
                "target_azimuth_reference_id": "EPSG:32631",
            },
        )
    assert "must be between -360 and 360" in str(correction_error.value.details["errors"])


def test_turn_rate_wraps_shortest_azimuth_direction_at_north(engines):
    _, result = engines.execute(
        "trajectory.minimum_curvature",
        {
            "stations": [
                {"md_si": 0.0, "inclination_deg": 90.0, "azimuth_deg": 359.0, "azimuth_reference": "grid", "azimuth_reference_id": "test-grid"},
                {"md_si": 300.0, "inclination_deg": 90.0, "azimuth_deg": 1.0, "azimuth_reference": "grid", "azimuth_reference_id": "test-grid"},
            ]
        },
    )
    assert result.outputs.stations[-1].turn_rate_deg_per_30m == pytest.approx(0.2)
    assert result.outputs.max_dls_deg_per_30m == pytest.approx(0.2)


def test_true_vertical_depth_subsea_uses_kb_elevation_only_for_rkb_input(engines):
    _, result = engines.execute(
        "trajectory.minimum_curvature",
        {"stations": _build_and_hold_well(), "datum": "rkb", "kb_elevation_si": 30.0},
    )
    assert result.outputs.stations[-1].tvdss_si == pytest.approx(result.outputs.total_tvd_si - 30.0)


def test_tvdss_is_not_inferred_for_unsupported_vertical_datums(engines):
    for datum in ("unknown", "gl", "cf", "derrick_floor"):
        _, result = engines.execute(
            "trajectory.minimum_curvature",
            {"stations": _build_and_hold_well(), "datum": datum, "kb_elevation_si": 30.0},
        )
        assert all(station.tvdss_si is None for station in result.outputs.stations)

    _, rkb_without_kb = engines.execute(
        "trajectory.minimum_curvature",
        {"stations": _build_and_hold_well(), "datum": "rkb"},
    )
    assert all(station.tvdss_si is None for station in rkb_without_kb.outputs.stations)


def test_msl_referenced_tvd_is_already_tvdss(engines):
    _, result = engines.execute(
        "trajectory.minimum_curvature",
        {"stations": _build_and_hold_well(), "datum": "msl", "kb_elevation_si": 30.0},
    )
    assert result.outputs.stations[-1].tvdss_si == pytest.approx(result.outputs.total_tvd_si)


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
    stations.append({"md_si": HOLD_MD_M, "inclination_deg": 1.0, "azimuth_deg": 0.0, "azimuth_reference": "grid", "azimuth_reference_id": "test-grid"})
    with pytest.raises(ValueError, match="duplicate"):
        engines.execute("trajectory.minimum_curvature", {"stations": stations})


def test_nonmonotonic_measured_depth_is_not_silently_reordered(engines):
    stations = _build_and_hold_well()
    stations[1], stations[2] = stations[2], stations[1]
    with pytest.raises(ValueError, match="strictly increasing in input order"):
        engines.execute("trajectory.minimum_curvature", {"stations": stations})


def test_single_station_is_rejected_by_the_contract(engines):
    from drillai.core.errors import EngineInputInvalid

    with pytest.raises(EngineInputInvalid) as excinfo:
        engines.execute("trajectory.minimum_curvature", {"stations": [{"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0, "azimuth_reference": "grid", "azimuth_reference_id": "test-grid"}]})
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
            "tie_in": {
                "tvd_si": 100.0,
                "ns_si": 10.0,
                "ew_si": -5.0,
                "azimuth_reference": "grid",
                "azimuth_reference_id": "test-grid",
            },
        },
    )
    output = result.outputs
    assert output.stations[0].tvd_si == pytest.approx(100.0)
    assert output.stations[0].ns_si == pytest.approx(10.0)
    assert output.stations[0].ew_si == pytest.approx(-5.0)
    assert output.total_displacement_si > 0
