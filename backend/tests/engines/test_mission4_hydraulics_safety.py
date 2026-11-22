"""Independent checks for unavailable pressure terms; no field validation is implied."""

import math

import pytest

from drillai.core.errors import EngineInputInvalid


def inputs(**overrides):
    payload = {
        "elements": [
            {"kind": "hole", "from_depth_si": 0, "to_depth_si": 1000, "od_si": .216},
            {"kind": "drillpipe", "from_depth_si": 0, "to_depth_si": 1000, "od_si": .1, "id_si": .08},
        ],
        "flow_rate_si": .001, "mud_weight_si": 1200, "plastic_viscosity_si": .03,
        "yield_point_si": 0, "bit_depth_si": 1000, "bit_tvd_si": 900,
        "bit_diameter_si": .216, "additional_downhole_pressure_loss_si": 0,
        "depth_reference": {"md_origin": "floor", "tvd_origin": "floor", "vertical_datum": "test",
                            "coordinate_reference": "test-grid", "survey_reference": "synthetic",
                            "source_reference": "synthetic", "depth_positive": "down"},
    }
    payload.update(overrides)
    return payload


def test_missing_nozzle_is_unknown_not_zero_and_downhole_losses_remain_available(engines):
    _, result = engines.execute("hydraulics.laminar", inputs())
    out = result.outputs
    assert out.bit_pressure_drop_si is None
    assert out.jet_velocity_si is None
    assert out.jet_impact_force_si is None
    assert out.bit_hydraulic_horsepower_si is None
    assert out.total_pressure_loss_si is None
    assert out.standpipe_pressure_expected_si is None
    assert out.string_pressure_loss_si > 0 and out.annulus_pressure_loss_si > 0
    assert any("TFA" in message for message in result.warnings)


def test_surface_pressure_is_unavailable_without_explicit_surface_loss(engines):
    _, result = engines.execute("hydraulics.laminar", inputs(tfa_si=.0003, discharge_coefficient=.9))
    out = result.outputs
    expected_bit = 1200 * .001**2 / (2 * (.9 * .0003)**2)
    assert out.bit_pressure_drop_si == pytest.approx(expected_bit)
    assert out.total_pressure_loss_si == pytest.approx(out.string_pressure_loss_si + out.annulus_pressure_loss_si + expected_bit)
    assert out.standpipe_pressure_expected_si is None
    assert any("surface" in message.lower() for message in result.warnings)


def test_declared_surface_loss_is_included_once(engines):
    _, result = engines.execute("hydraulics.laminar", inputs(tfa_si=.0003, discharge_coefficient=.9, surface_pressure_loss_si=12345, additional_downhole_pressure_loss_si=777))
    out = result.outputs
    pipe = 128 * .03 * .001 * 1000 / (math.pi * .08**4)
    gap = (.216 - .1) / 2
    area = math.pi / 4 * (.216**2 - .1**2)
    annulus = 12 * .03 * (.001 / area) * 1000 / gap**2
    bit = 1200 * .001**2 / (2 * (.9 * .0003)**2)
    assert out.standpipe_pressure_expected_si == pytest.approx(pipe + annulus + bit + 777 + 12345, rel=1e-9)
    assert out.total_pressure_loss_si == pytest.approx(pipe + annulus + bit + 777, rel=1e-9)


def test_nozzle_requires_explicit_discharge_coefficient(engines):
    with pytest.raises(EngineInputInvalid):
        engines.execute("hydraulics.laminar", inputs(tfa_si=.0003))


def test_surface_loss_range_requires_paired_bounds_and_in_range_flow(engines):
    base = inputs(tfa_si=.0003, discharge_coefficient=.9, surface_pressure_loss_si=100)
    for override in (
        {"surface_loss_valid_flow_min_si": .0005},
        {"surface_loss_valid_flow_min_si": .002, "surface_loss_valid_flow_max_si": .003},
        {"surface_loss_valid_flow_min_si": .002, "surface_loss_valid_flow_max_si": .001},
        {"surface_pressure_loss_si": None, "surface_loss_valid_flow_min_si": .0005, "surface_loss_valid_flow_max_si": .002},
    ):
        with pytest.raises(EngineInputInvalid):
            engines.execute("hydraulics.laminar", {**base, **override})
    _, result = engines.execute("hydraulics.laminar", {**base, "surface_loss_valid_flow_min_si": .0005, "surface_loss_valid_flow_max_si": .002})
    assert result.outputs.standpipe_pressure_expected_si is not None


@pytest.mark.asyncio
async def test_optimisation_cannot_reuse_point_surface_loss_at_another_flow(engines):
    from drillai.drilling.optimisation import DrillingOptimisationService

    service = DrillingOptimisationService(None, "synthetic", registry=engines)
    result = await service._evaluate(
        well_id="synthetic", section=None, candidate_values={"flow_rate_si": .0015},
        hydraulics_base=inputs(tfa_si=.0003, discharge_coefficient=.9, surface_pressure_loss_si=100),
        torque_base={}, limits={"spp_si": 1e9}, objectives=[{"key": "annular_velocity_m_s"}], persist_runs=False,
    )
    assert result.hydraulics["standpipe_pressure_expected_si"] is None
    assert result.feasible is False
    assert any(item.name == "standpipe_pressure_within_pump_limit" and not item.satisfied for item in result.constraints)
    assert any("does not cover" in warning for warning in result.warnings)


@pytest.mark.asyncio
async def test_optimisation_uses_explicit_range_but_does_not_extrapolate(engines):
    from drillai.drilling.optimisation import DrillingOptimisationService

    service = DrillingOptimisationService(None, "synthetic", registry=engines)
    base = inputs(tfa_si=.0003, discharge_coefficient=.9, surface_pressure_loss_si=100,
                  surface_loss_valid_flow_min_si=.0005, surface_loss_valid_flow_max_si=.002,
                  additional_loss_valid_flow_min_si=.0005, additional_loss_valid_flow_max_si=.002)
    async def evaluate(flow):
        return await service._evaluate(
            well_id="synthetic", section=None, candidate_values={"flow_rate_si": flow}, hydraulics_base=base,
            torque_base={}, limits={"spp_si": 1e9}, objectives=[{"key": "annular_velocity_m_s"}], persist_runs=False,
        )
    inside = await evaluate(.0015)
    outside = await evaluate(.003)
    assert inside.hydraulics["standpipe_pressure_expected_si"] is not None
    assert any(item.name == "standpipe_pressure_within_pump_limit" and item.satisfied for item in inside.constraints)
    assert outside.hydraulics["standpipe_pressure_expected_si"] is None
    assert outside.feasible is False


@pytest.mark.asyncio
async def test_mud_weight_sweep_changes_the_actual_hydraulics(engines):
    from drillai.drilling.optimisation import DrillingOptimisationService

    service = DrillingOptimisationService(None, "synthetic", registry=engines)
    async def evaluate(weight):
        return await service._evaluate(
            well_id="synthetic", section=None, candidate_values={"mud_weight_si": weight},
            hydraulics_base=inputs(), torque_base={}, limits={},
            objectives=[{"key": "annular_velocity_m_s"}], persist_runs=False,
        )
    light, heavy = await evaluate(1200), await evaluate(1400)
    assert heavy.hydraulics["ecd_bottom_si"] - light.hydraulics["ecd_bottom_si"] == pytest.approx(200.0)


@pytest.mark.asyncio
async def test_optimisation_does_not_certify_turbulent_laminar_model(engines):
    from drillai.drilling.optimisation import DrillingOptimisationService

    service = DrillingOptimisationService(None, "synthetic", registry=engines)
    result = await service._evaluate(
        well_id="synthetic", section=None, candidate_values={"flow_rate_si": .06},
        hydraulics_base=inputs(), torque_base={}, limits={},
        objectives=[{"key": "annular_velocity_m_s"}], persist_runs=False,
    )
    assert result.feasible is False
    assert any(item.name == "hydraulics_model_applicability" and not item.satisfied for item in result.constraints)


@pytest.mark.asyncio
async def test_surface_loss_flow_range_does_not_authorize_density_extrapolation(engines):
    from drillai.drilling.optimisation import DrillingOptimisationService

    service = DrillingOptimisationService(None, "synthetic", registry=engines)
    result = await service._evaluate(
        well_id="synthetic", section=None, candidate_values={"mud_weight_si": 1400},
        hydraulics_base=inputs(tfa_si=.0003, discharge_coefficient=.9, surface_pressure_loss_si=100,
                               surface_loss_valid_flow_min_si=.0005, surface_loss_valid_flow_max_si=.002,
                  additional_loss_valid_flow_min_si=.0005, additional_loss_valid_flow_max_si=.002),
        torque_base={}, limits={"spp_si": 1e9}, objectives=[{"key": "annular_velocity_m_s"}], persist_runs=False,
    )
    assert result.hydraulics["standpipe_pressure_expected_si"] is None
    assert not result.feasible
    assert any("does not cover the candidate mud density" in warning for warning in result.warnings)


def test_unknown_unmodeled_downhole_loss_prevents_total_pressure(engines):
    _, result = engines.execute("hydraulics.laminar", inputs(tfa_si=.0003, discharge_coefficient=.9,
        surface_pressure_loss_si=100, additional_downhole_pressure_loss_si=None))
    assert result.outputs.bit_pressure_drop_si is not None
    assert result.outputs.total_pressure_loss_si is None
    assert result.outputs.standpipe_pressure_expected_si is None
    assert any("additional downhole" in warning for warning in result.warnings)


def test_cuttings_are_unavailable_when_size_or_density_is_unknown(engines):
    for override in ({}, {"cutting_diameter_si": .006}, {"cutting_density_si": 2600}):
        _, result = engines.execute("hydraulics.laminar", inputs(rop_si=10, **override))
        assert result.outputs.slip_velocity_si is None
        assert result.outputs.cutting_transport_ratio is None
        assert result.outputs.cuttings_concentration_volume_fraction is None
        assert any("cutting diameter and density" in warning for warning in result.warnings)


def test_missing_bit_diameter_never_extrapolates_from_unrelated_hole(engines):
    sections = [
        {"kind": "hole", "from_depth_si": 0, "to_depth_si": 500, "od_si": .3},
        {"kind": "casing", "from_depth_si": 500, "to_depth_si": 1000, "od_si": .24, "id_si": .22},
        {"kind": "drillpipe", "from_depth_si": 0, "to_depth_si": 1000, "od_si": .1, "id_si": .08},
    ]
    with pytest.raises(EngineInputInvalid):
        engines.execute("hydraulics.laminar", inputs(elements=sections, bit_diameter_si=None))
    _, inferred = engines.execute("hydraulics.laminar", inputs(bit_diameter_si=None))
    assert inferred.outputs.bit_diameter_si == pytest.approx(.216)
    assert any("bit diameter assumed" in warning for warning in inferred.warnings)
    with pytest.raises(EngineInputInvalid):
        engines.execute("hydraulics.laminar", inputs(bit_diameter_si=-.1))
