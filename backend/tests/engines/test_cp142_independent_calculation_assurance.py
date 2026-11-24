"""Independent closed-form references for three distinct calculation families.

These deterministic vectors are mathematical software checks, not field measurements, vendor
comparisons, or qualified engineering acceptance. Reference expressions are derived separately
from the implementation under test and use only synthetic inputs.
"""

from __future__ import annotations

import math

import pytest


def _direction(inclination_deg: float, azimuth_deg: float) -> tuple[float, float, float]:
    inclination = math.radians(inclination_deg)
    azimuth = math.radians(azimuth_deg)
    return (
        math.cos(inclination),
        math.sin(inclination) * math.cos(azimuth),
        math.sin(inclination) * math.sin(azimuth),
    )


def test_trajectory_displacement_matches_independent_great_circle_integral(engines):
    """Integrate the spherical unit tangent in closed form for one synthetic 3-D survey interval."""
    start = (18.0, 27.0)
    end = (63.0, 119.0)
    length_m = 240.0
    first, last = _direction(*start), _direction(*end)
    dogleg = math.acos(sum(a * b for a, b in zip(first, last, strict=True)))
    # Integral_0^1 slerp(first,last,t) dt = tan(dogleg/2)/dogleg * (first+last).
    # This reference is expressed as the integral of the continuous tangent rather than the
    # engine's station ratio-factor calculation.
    scale = length_m * math.tan(dogleg / 2.0) / dogleg
    expected = tuple(scale * (a + b) for a, b in zip(first, last, strict=True))
    stations = [
        {
            "md_si": 0.0,
            "inclination_deg": start[0],
            "azimuth_deg": start[1],
            "azimuth_reference": "grid",
            "azimuth_reference_id": "CP142-SYNTHETIC-GRID",
        },
        {
            "md_si": length_m,
            "inclination_deg": end[0],
            "azimuth_deg": end[1],
            "azimuth_reference": "grid",
            "azimuth_reference_id": "CP142-SYNTHETIC-GRID",
        },
    ]

    _, result = engines.execute("trajectory.minimum_curvature", {"stations": stations})

    terminal = result.outputs.stations[-1]
    assert (terminal.tvd_si, terminal.ns_si, terminal.ew_si) == pytest.approx(expected, rel=1e-10, abs=1e-10)


def test_newtonian_pipe_pressure_loss_matches_hagen_poiseuille(engines):
    """The zero-yield-stress limit is checked against an independently evaluated pipe-flow law."""
    diameter_m = 0.1086
    length_m = 1200.0
    flow_m3_s = 0.012
    viscosity_pa_s = 0.035
    _, result = engines.execute(
        "hydraulics.laminar",
        {
            "elements": [
                {"kind": "hole", "from_depth_si": 0.0, "to_depth_si": length_m, "od_si": 0.2159},
                {
                    "kind": "drillpipe",
                    "from_depth_si": 0.0,
                    "to_depth_si": length_m,
                    "od_si": 0.127,
                    "id_si": diameter_m,
                    "linear_mass_si": 29.05,
                },
            ],
            "flow_rate_si": flow_m3_s,
            "mud_weight_si": 1200.0,
            "plastic_viscosity_si": viscosity_pa_s,
            "yield_point_si": 0.0,
            "bit_depth_si": length_m,
            "bit_tvd_si": length_m,
            "depth_reference": {"md_origin": "synthetic-rig-floor", "tvd_origin": "synthetic-rig-floor", "vertical_datum": "synthetic-datum", "coordinate_reference": "synthetic-grid", "survey_reference": "synthetic-survey", "source_reference": "synthetic-example", "depth_positive": "down"},
            "bit_diameter_si": 0.2159,
            "tfa_si": None,
            "nozzle_count": None,
        },
    )

    expected_pa = 128.0 * viscosity_pa_s * length_m * flow_m3_s / (math.pi * diameter_m**4)
    assert result.outputs.string_pressure_loss_si == pytest.approx(expected_pa, rel=1e-9)


def test_mse_superposition_matches_independent_teale_definition(engines):
    """Axial work and rotary work are calculated separately before their energy-density sum."""
    wob_n = 42_000.0
    torque_nm = 2_650.0
    rpm = 96.0
    rop_m_h = 18.0
    bit_diameter_m = 0.2032
    bit_area_m2 = math.pi * bit_diameter_m**2 / 4.0
    revolutions_per_second = rpm / 60.0
    penetration_m_per_second = rop_m_h / 3600.0
    axial_pa = wob_n / bit_area_m2
    rotary_pa = (2.0 * math.pi * revolutions_per_second * torque_nm) / (
        bit_area_m2 * penetration_m_per_second
    )

    _, result = engines.execute(
        "drilling.mse",
        {
            "wob_si": wob_n,
            "torque_si": torque_nm,
            "rpm_si": rpm,
            "rop_si": rop_m_h,
            "bit_diameter_si": bit_diameter_m,
        },
    )

    assert result.outputs.mse_axial_component_si == pytest.approx(axial_pa, rel=1e-12)
    assert result.outputs.mse_torsional_component_si == pytest.approx(rotary_pa, rel=1e-12)
    assert result.outputs.mse_si == pytest.approx(axial_pa + rotary_pa, rel=1e-12)
    assert result.outputs.dysfunction_indicator == "unknown"
    assert any("no confined compressive strength" in note for note in result.outputs.notes)


def test_power_law_pressure_gradient_matches_independent_radial_flow_integral(engines):
    """Numerically integrate the pipe shear profile rather than reusing the closed-form solver."""
    diameter_m, length_m, flow_m3_s, density = 0.10, 850.0, 0.0005, 1200.0
    consistency = 0.8
    for flow_index in (0.55, 1.25):
        _, result = engines.execute("hydraulics.power_law_pipe", {
            "diameter_si": diameter_m, "length_si": length_m, "flow_rate_si": flow_m3_s,
            "density_si": density, "consistency_si": consistency, "flow_index": flow_index,
            "measured_shear_min_per_s": 0.0, "measured_shear_max_per_s": 10000.0,
        })
        gradient = result.outputs.pressure_gradient_si
        radius, subdivisions = diameter_m / 2.0, 20000
        dr = radius / subdivisions
        # By integrating concentric rings and exchanging the order of integration, the flow
        # follows Q = pi integral_0^R r^2 * shear_rate(r) dr, where local shear stress is G*r/2.
        # The reference is a midpoint numerical integral, not the engine's closed expression.
        independently_integrated_q = math.pi * sum(
            (r := (i + 0.5) * dr) ** 2 * (gradient * r / (2.0 * consistency)) ** (1.0 / flow_index)
            for i in range(subdivisions)
        ) * dr
        assert independently_integrated_q == pytest.approx(flow_m3_s, rel=1e-8)
        assert result.outputs.pressure_loss_si == pytest.approx(gradient * length_m, rel=1e-12)


def test_kill_sheet_static_balance_and_pressure_endpoints_independent(engines):
    """Synthetic hydrostatic reference only; not an approved well-control procedure."""
    depth_m, mud_density, sidpp_pa, scr_pa = 2400.0, 1200.0, 1_200_000.0, 2_500_000.0
    _, result = engines.execute("well_control.kill_sheet", {
        "tvd_si": depth_m, "current_mud_weight_si": mud_density, "sidpp_si": sidpp_pa,
        "slow_circulating_pressure_si": scr_pa, "strokes_to_bit": 1200.0,
        "annulus_strokes": 600.0, "method": "wait_and_weight",
    })
    formation_pa = mud_density * 9.80665 * depth_m + sidpp_pa
    independently_balanced_density = formation_pa / (9.80665 * depth_m)
    assert result.outputs.formation_pressure_si == pytest.approx(formation_pa, rel=1e-12)
    assert result.outputs.kill_mud_weight_si == pytest.approx(independently_balanced_density, rel=1e-12)
    assert result.outputs.initial_circulating_pressure_si == pytest.approx(scr_pa + sidpp_pa, rel=1e-12)
    assert result.outputs.final_circulating_pressure_si == pytest.approx(
        scr_pa * independently_balanced_density / mud_density, rel=1e-12
    )
    assert result.outputs.schedule[0].pressure_si == pytest.approx(scr_pa + sidpp_pa)
    assert result.outputs.schedule[-1].pressure_si == pytest.approx(result.outputs.final_circulating_pressure_si)
    assert result.outputs.input_provenance_verified is False
    assert result.outputs.volumetric_kick_tolerance_si is None
