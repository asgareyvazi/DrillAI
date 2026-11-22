"""Independent checks of the limited pipe model, not field-fluid validation."""

from __future__ import annotations

import math

import pytest

from drillai.core.errors import EngineInputInvalid


def payload(**changes):
    return {
        "diameter_si": 0.1, "length_si": 100.0, "flow_rate_si": 0.0002,
        "density_si": 1200.0, "consistency_si": 0.5, "flow_index": 0.7,
        "measured_shear_min_per_s": 0.1, "measured_shear_max_per_s": 1000.0,
        **changes,
    }


def test_pipe_power_law_matches_independent_velocity_profile_integration(engines):
    """Integrate the independently constructed velocity profile and recover the prescribed Q."""
    inputs = payload()
    spec, result = engines.execute("hydraulics.power_law_pipe", inputs)
    assert spec.version == "1.0.0"
    gradient = result.outputs.pressure_gradient_si
    radius = inputs["diameter_si"] / 2
    n, k = inputs["flow_index"], inputs["consistency_si"]
    # From radial shear-stress balance: velocity at r is integral of
    # (gradient*s/(2*K))**(1/n) from r to R. Midpoint quadrature is
    # deliberately independent of the production wall-rate relation.
    points = 10000
    dr = radius / points
    velocity = 0.0
    q = 0.0
    for i in reversed(range(points)):
        r = (i + 0.5) * dr
        local_shear = (gradient * r / (2 * k)) ** (1 / n)
        midpoint_velocity = velocity + 0.5 * local_shear * dr
        q += 2 * math.pi * r * midpoint_velocity * dr
        velocity += local_shear * dr
    assert q == pytest.approx(inputs["flow_rate_si"], rel=1e-7)
    assert result.outputs.pressure_loss_si == pytest.approx(gradient * inputs["length_si"])
    assert any("extrapolated" in warning for warning in result.warnings)
    assert any("not proof" in warning for warning in result.warnings)


def test_newtonian_limit_and_rate_length_scaling(engines):
    args = payload(flow_index=1.0, consistency_si=0.02, density_si=1000.0, flow_rate_si=0.00001)
    _, result = engines.execute("hydraulics.power_law_pipe", args)
    expected = 128 * 0.02 * args["length_si"] * args["flow_rate_si"] / (math.pi * args["diameter_si"] ** 4)
    assert result.outputs.pressure_loss_si == pytest.approx(expected, rel=1e-12)
    reynolds = 1000.0 * result.outputs.velocity_si * args["diameter_si"] / 0.02
    assert result.outputs.generalized_reynolds == pytest.approx(reynolds, rel=1e-12)
    _, longer = engines.execute("hydraulics.power_law_pipe", {**args, "length_si": 200.0})
    assert longer.outputs.pressure_loss_si == pytest.approx(2 * expected)

    # For n=1, Re = rho*4Q/(pi*D*mu). Test either side of the screen;
    # a screen below 2100 still does not prove actual laminar flow.
    q_at_2100 = 2100 * math.pi * args["diameter_si"] * 0.02 / (4 * 1000.0)
    _, below = engines.execute("hydraulics.power_law_pipe", {**args, "flow_rate_si": q_at_2100 * .999})
    assert below.outputs.generalized_reynolds == pytest.approx(2097.9)
    with pytest.raises(EngineInputInvalid, match="laminar screen"):
        engines.execute("hydraulics.power_law_pipe", {**args, "flow_rate_si": q_at_2100 * 1.001})
    contract = engines.get("hydraulics.power_law_pipe").spec.describe()["unit_contract"]
    assert contract["input_fields"]["consistency_si"]["status"] == "unknown"
    assert not contract["input_fields"]["consistency_si"]["display_units"]["field_us"]["supported"]


def test_power_law_rejects_inapplicable_invalid_and_nonfinite_inputs(engines):
    for changes in (
        {"measured_shear_min_per_s": 5.0, "measured_shear_max_per_s": 6.0},
        {"measured_shear_min_per_s": 1.0, "measured_shear_max_per_s": 1.0},
        {"flow_rate_si": 0.1},  # Re above laminar screen
        {"flow_index": 0.0}, {"consistency_si": 0.0}, {"diameter_si": -1.0},
        {"flow_index": float("nan")}, {"flow_rate_si": float("inf")},
        {"density_si": True}, {"unsupported_annulus": True},
    ):
        with pytest.raises(EngineInputInvalid):
            engines.execute("hydraulics.power_law_pipe", payload(**changes))
