"""Well control and tubular design.

These two engines carry the safety-critical numbers, so they are checked against
independent hand calculations and, for tubulars, against published API pipe-body ratings.
"""

from __future__ import annotations

import math

import pytest

from drillai.engines.tubulars import _coefficients, _domain_boundaries

PSI = 6894.757293168361
INCH = 0.0254
PPG = 119.82642731689663
GRAVITY = 9.80665


# --------------------------------------------------------------------------- well control
def _kill_sheet_input(**overrides) -> dict:
    payload = {
        "tvd_si": 2000.0,
        "current_mud_weight_si": 1200.0,
        "sidpp_si": 3.0e6,
        "sicp_si": 4.0e6,
        "slow_circulating_pressure_si": 5.0e6,
        "strokes_to_bit": 2000.0,
        "pump_output_per_stroke_si": 0.02,
        "kill_rate_si": 0.02,
        "casing_shoe_tvd_si": 1500.0,
        "shoe_fracture_mud_weight_si": 1800.0,
    }
    payload.update(overrides)
    return payload


def test_kill_mud_weight_from_sidpp(engines):
    _, result = engines.execute("well_control.kill_sheet", _kill_sheet_input())
    output = result.outputs
    expected = 1200.0 + 3.0e6 / (GRAVITY * 2000.0)
    assert output.kill_mud_weight_si == pytest.approx(expected, rel=1e-12)
    assert output.kill_mud_weight_ppg == pytest.approx(expected / PPG, rel=1e-12)
    assert output.kill_mud_weight_increase_si == pytest.approx(expected - 1200.0)
    assert output.formation_pressure_si == pytest.approx(1200.0 * GRAVITY * 2000.0 + 3.0e6)
    assert output.formation_pressure_equivalent_si == pytest.approx(output.formation_pressure_si / (GRAVITY * 2000.0))


def test_icp_fcp_and_the_pressure_schedule(engines):
    _, result = engines.execute("well_control.kill_sheet", _kill_sheet_input())
    output = result.outputs
    krp = 5.0e6
    assert output.initial_circulating_pressure_si == pytest.approx(krp + 3.0e6)
    assert output.final_circulating_pressure_si == pytest.approx(krp * output.kill_mud_weight_si / 1200.0)
    assert output.initial_circulating_pressure_kpa == pytest.approx(output.initial_circulating_pressure_si / 1000.0)

    schedule = output.schedule
    assert schedule[0].phase == "initial" and schedule[0].strokes == 0.0
    assert schedule[0].pressure_si == pytest.approx(output.initial_circulating_pressure_si)
    assert schedule[-1].phase == "final"
    assert schedule[-1].strokes == pytest.approx(2000.0)
    assert schedule[-1].pressure_si == pytest.approx(output.final_circulating_pressure_si)
    assert schedule[-1].mud_weight_si == pytest.approx(output.kill_mud_weight_si)
    # pressure falls monotonically and mud weight rises monotonically along the schedule
    pressures = [row.pressure_si for row in schedule]
    weights = [row.mud_weight_si for row in schedule]
    assert pressures == sorted(pressures, reverse=True)
    assert weights == sorted(weights)


def test_maasp_and_shoe_margin(engines):
    _, result = engines.execute("well_control.kill_sheet", _kill_sheet_input())
    output = result.outputs
    expected_maasp = (1800.0 - 1200.0) * GRAVITY * 1500.0
    assert output.maasp_si == pytest.approx(expected_maasp, rel=1e-12)
    assert output.max_allowable_mud_weight_si == pytest.approx(1800.0)
    expected_shoe_pressure = 1200.0 * GRAVITY * 1500.0 + 4.0e6
    assert output.shoe_pressure_at_kick_si == pytest.approx(expected_shoe_pressure)
    assert output.shoe_margin_si == pytest.approx(1800.0 * GRAVITY * 1500.0 - expected_shoe_pressure)


def test_shoe_fracture_exceeded_is_an_error(engines):
    _, result = engines.execute(
        "well_control.kill_sheet",
        _kill_sheet_input(sicp_si=15.0e6, shoe_fracture_mud_weight_si=1400.0),
    )
    assert any(violation.name == "shoe_fracture_exceeded" for violation in result.violations)
    assert result.is_feasible is False


def test_missing_shoe_data_is_announced(engines):
    _, result = engines.execute(
        "well_control.kill_sheet",
        {"tvd_si": 2000.0, "current_mud_weight_si": 1200.0, "sidpp_si": 3.0e6, "slow_circulating_pressure_si": 5.0e6},
    )
    output = result.outputs
    assert output.maasp_si is None
    assert output.schedule == []
    assert any("shoe fracture data" in warning for warning in result.warnings)
    assert any("strokes to bit" in warning for warning in result.warnings)


def test_kill_time_from_strokes_and_rate(engines):
    _, result = engines.execute("well_control.kill_sheet", _kill_sheet_input())
    output = result.outputs
    assert output.time_to_kill_si == pytest.approx(2000.0 * 0.02 / 0.02)


def test_sicp_much_above_sidpp_is_challenged(engines):
    _, result = engines.execute("well_control.kill_sheet", _kill_sheet_input(sicp_si=8.0e6))
    assert any("SICP" in warning for warning in result.warnings)


# --------------------------------------------------------------------------- tubulars
def _segment(outer_in: float, wall_in: float, yield_psi: float, **overrides) -> dict:
    payload = {
        "from_depth_si": 0.0,
        "to_depth_si": 1000.0,
        "outer_diameter_si": outer_in * INCH,
        "wall_thickness_si": wall_in * INCH,
        "yield_stress_si": yield_psi * PSI,
    }
    payload.update(overrides)
    return payload


#: Published API pipe-body ratings (psi): (OD in, wall in, yield psi, collapse, burst).
PUBLISHED_RATINGS = [
    ("9-5/8 47# N-80", 9.625, 0.472, 80_000, 4_750, 6_870),
    ("9-5/8 47# L-80", 9.625, 0.472, 80_000, 4_760, 6_870),
    ("9-5/8 47# P-110", 9.625, 0.472, 110_000, 5_300, 9_440),
    ("9-5/8 53.5# N-80", 9.625, 0.545, 80_000, 6_620, 7_930),
    ("9-5/8 58.4# N-80", 9.625, 0.595, 80_000, 7_890, 8_650),
    ("7 29# L-80", 7.0, 0.408, 80_000, 7_030, 8_160),
]


@pytest.mark.parametrize("label,outer_in,wall_in,yield_psi,collapse_psi,burst_psi", PUBLISHED_RATINGS)
def test_ratings_match_published_api_values(engines, label, outer_in, wall_in, yield_psi, collapse_psi, burst_psi):
    from drillai.engines.tubulars import bare_collapse_pressure, burst_rating

    collapse, domain = bare_collapse_pressure(yield_psi * PSI, outer_in / wall_in)
    burst = burst_rating(yield_psi * PSI, outer_in * INCH, wall_in * INCH)
    assert domain in {"yield", "plastic", "transition", "elastic"}
    assert collapse / PSI == pytest.approx(collapse_psi, rel=0.01), f"{label} collapse: {collapse / PSI:.0f} psi"
    assert burst / PSI == pytest.approx(burst_psi, rel=0.01), f"{label} burst: {burst / PSI:.0f} psi"


def test_api_coefficients_reproduce_the_published_grade_table():
    """A/B/C polynomials and the derived F/G must land on the classic API constants."""
    for yield_psi, expected in {
        55_000: {"A": 2.993, "B": 0.0540, "C": 1206, "F": 1.989, "G": 0.0360},
        80_000: {"A": 3.071, "B": 0.0667, "C": 1955, "F": 1.998, "G": 0.0434},
    }.items():
        coefficients = _coefficients(yield_psi)
        for key, value in expected.items():
            assert coefficients[key] == pytest.approx(value, rel=0.005), f"{yield_psi} psi {key}"


def test_collapse_domain_boundaries_match_published_values():
    for yield_psi, boundaries in {
        55_000: (14.81, 25.01, 37.21),
        80_000: (13.38, 22.47, 31.02),
    }.items():
        computed = _domain_boundaries(yield_psi)
        for actual, expected in zip(computed, boundaries, strict=True):
            assert actual == pytest.approx(expected, rel=0.01)


def test_axial_yield_force_matches_published_pipe_body_yield(engines):
    _, result = engines.execute(
        "tubulars.api_5c3",
        {"segments": [_segment(7.0, 0.408, 80_000, name="7in 29# L-80")]},
    )
    output = result.outputs
    # published pipe body yield strength: 676 kips
    assert output.segments[0].axial_yield_force_si / 4448.2216152605 == pytest.approx(676.0, rel=0.005)


def test_thick_wall_pipe_is_in_the_yield_domain(engines):
    _, result = engines.execute("tubulars.api_5c3", {"segments": [_segment(7.0, 0.540, 80_000)]})
    assert result.outputs.segments[0].collapse_domain == "yield"
    assert result.outputs.segments[0].d_over_t == pytest.approx(7.0 / 0.540)


def test_thin_wall_pipe_falls_into_the_elastic_domain(engines):
    from drillai.engines.tubulars import bare_collapse_pressure

    collapse, domain = bare_collapse_pressure(80_000 * PSI, 40.0)
    assert domain == "elastic"
    # elastic collapse must follow the closed-form API expression
    assert collapse == pytest.approx(46.95e6 / (40.0 * 39.0**2) * PSI, rel=1e-12)


def test_design_factor_violation_is_reported_with_the_governing_mode(engines):
    _, result = engines.execute(
        "tubulars.api_5c3",
        {
            "segments": [
                _segment(
                    9.625,
                    0.472,
                    80_000,
                    name="surface",
                    from_depth_si=0.0,
                    to_depth_si=1200.0,
                    internal_pressure_si=5.0e6,
                    external_pressure_si=45.0e6,  # far above the 4750 psi collapse rating
                )
            ]
        },
    )
    output = result.outputs
    assert output.overall_qualified is False
    assert output.governed_by == "collapse"
    assert any(violation.name == "collapse_safety_factor" for violation in output.violations)
    assert result.is_feasible is False


def test_net_loads_use_the_differential_pressure(engines):
    _, result = engines.execute(
        "tubulars.api_5c3",
        {
            "segments": [
                _segment(9.625, 0.472, 80_000, name="surface", internal_pressure_si=30.0e6, external_pressure_si=5.0e6)
            ]
        },
    )
    segment = result.outputs.segments[0]
    assert segment.net_burst_load_si == pytest.approx(25.0e6)
    assert segment.net_collapse_load_si == 0.0
    assert segment.collapse_safety_factor is None  # not applicable: no net collapse load
    assert segment.burst_safety_factor is not None


def test_wear_allowance_reduces_the_ratings(engines):
    _, untouched = engines.execute("tubulars.api_5c3", {"segments": [_segment(9.625, 0.472, 80_000)]})
    _, worn = engines.execute("tubulars.api_5c3", {"segments": [_segment(9.625, 0.472, 80_000, wear_allowance=0.2)]})
    assert worn.outputs.segments[0].burst_rating_si < untouched.outputs.segments[0].burst_rating_si
    assert worn.outputs.segments[0].collapse_rating_si < untouched.outputs.segments[0].collapse_rating_si
    assert worn.outputs.segments[0].axial_yield_force_si < untouched.outputs.segments[0].axial_yield_force_si


def test_axial_tension_derates_collapse_and_burst(engines):
    _, unloaded = engines.execute("tubulars.api_5c3", {"segments": [_segment(9.625, 0.472, 80_000)]})
    _, loaded = engines.execute(
        "tubulars.api_5c3",
        {"segments": [_segment(9.625, 0.472, 80_000, axial={"hanging_weight_si": 2.0e6})]},
    )
    assert loaded.outputs.segments[0].collapse_rating_si < unloaded.outputs.segments[0].collapse_rating_si
    assert loaded.outputs.segments[0].burst_rating_si < unloaded.outputs.segments[0].burst_rating_si
    assert loaded.outputs.segments[0].tension_safety_factor is not None


def test_axial_compression_does_not_derate_collapse(engines):
    """API TR 5C3 2015: collapse resistance is not reduced by axial compression."""
    _, unloaded = engines.execute("tubulars.api_5c3", {"segments": [_segment(9.625, 0.472, 80_000)]})
    _, compressed = engines.execute(
        "tubulars.api_5c3",
        {"segments": [_segment(9.625, 0.472, 80_000, axial={"hanging_weight_si": -1.0e6})]},
    )
    assert compressed.outputs.segments[0].collapse_rating_si == pytest.approx(
        unloaded.outputs.segments[0].collapse_rating_si
    )


def test_triaxial_check_uses_the_lamé_inner_wall_stress(engines):
    _, result = engines.execute(
        "tubulars.api_5c3",
        {"segments": [_segment(7.0, 0.408, 80_000, internal_pressure_si=56.26e6)]},  # 8160 psi
    )
    segment = result.outputs.segments[0]
    # Independent check of the Lamé/von Mises value at the bore of 7 in 29# L-80 at 8160 psi.
    outer_radius = 7.0 * INCH / 2
    inner_radius = outer_radius - 0.408 * INCH
    internal = 56.26e6
    hoop = internal * (inner_radius**2 + outer_radius**2) / (outer_radius**2 - inner_radius**2)
    radial = -internal
    expected = math.sqrt(0.5 * ((hoop - radial) ** 2 + (radial - 0) ** 2 + (0 - hoop) ** 2))
    assert segment.triaxial_equivalent_stress_si == pytest.approx(expected, rel=1e-6)
    assert segment.triaxial_safety_factor == pytest.approx(80_000 * PSI / segment.triaxial_equivalent_stress_si)
    # the Barlow rating is not the von Mises yield point: it sits below it, which is why the
    # triaxial check exists as a separate criterion
    assert segment.triaxial_equivalent_stress_si < 80_000 * PSI


def test_triaxial_safety_factor_falls_with_external_pressure(engines):
    _, low = engines.execute(
        "tubulars.api_5c3",
        {"segments": [_segment(9.625, 0.472, 80_000, internal_pressure_si=1.0e6, external_pressure_si=10.0e6)]},
    )
    _, high = engines.execute(
        "tubulars.api_5c3",
        {"segments": [_segment(9.625, 0.472, 80_000, internal_pressure_si=1.0e6, external_pressure_si=30.0e6)]},
    )
    assert high.outputs.segments[0].triaxial_safety_factor < low.outputs.segments[0].triaxial_safety_factor


def test_mud_weight_supplies_the_external_pressure_when_not_given(engines):
    _, result = engines.execute(
        "tubulars.api_5c3",
        {"segments": [_segment(9.625, 0.472, 80_000, to_depth_si=2000.0)], "mud_weight_si": 1200.0},
    )
    segment = result.outputs.segments[0]
    assert segment.net_collapse_load_si == pytest.approx(1200.0 * GRAVITY * 2000.0)


def test_governing_segment_and_minimum_safety_factor_are_reported(engines):
    _, result = engines.execute(
        "tubulars.api_5c3",
        {
            "segments": [
                _segment(9.625, 0.472, 80_000, name="surface", from_depth_si=0.0, to_depth_si=1000.0, internal_pressure_si=10.0e6),
                _segment(7.0, 0.408, 80_000, name="production", from_depth_si=1000.0, to_depth_si=3000.0, internal_pressure_si=45.0e6),
            ]
        },
    )
    output = result.outputs
    assert output.governing_segment == "production"
    assert output.governed_by in {"burst", "collapse", "tension", "triaxial"}
    assert output.minimum_safety_factor is not None
    assert output.overall_qualified is True


def test_design_factors_are_explicit_and_editable(engines):
    _, strict = engines.execute(
        "tubulars.api_5c3",
        {
            "segments": [_segment(9.625, 0.472, 80_000, name="surface", internal_pressure_si=30.0e6, external_pressure_si=5.0e6)],
            "design_factors": {"burst": 1.10, "collapse": 1.00, "tension": 1.60, "triaxial": 2.5},
        },
    )
    assert strict.outputs.design_factors.triaxial == 2.5
    assert any(violation.name == "triaxial_safety_factor" for violation in strict.outputs.violations)


def test_wall_thickness_that_leaves_no_bore_is_rejected(engines):
    from drillai.core.errors import EngineInputInvalid

    with pytest.raises(EngineInputInvalid):
        engines.execute("tubulars.api_5c3", {"segments": [_segment(7.0, -0.1, 80_000)]})
