"""Unit system tests — conversions are verified against published reference values."""

from __future__ import annotations

import math

import pytest

from drillai.core.errors import UnitError
from drillai.units.registry import (
    Dimension,
    UnitSystem,
    canonical_unit,
    convert,
    convert_delta,
    display,
    get_unit,
    known_units,
    preferred_unit,
    q,
    quantity_field,
    to_display_value,
)

APPROX = 1e-9


def rel(actual: float, expected: float) -> bool:
    return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)


class TestReferenceConversions:
    def test_length(self):
        assert rel(convert(1.0, "ft", "m"), 0.3048)
        assert rel(convert(1.0, "in", "m"), 0.0254)
        assert rel(convert(1.0, "m", "ft"), 1 / 0.3048)

    def test_volume(self):
        assert rel(convert(1.0, "bbl", "gal"), 42.0)
        assert rel(convert(1.0, "bbl", "m3"), 0.158987294928)
        assert rel(convert(1.0, "gal", "L"), 3.785411784)

    def test_pressure(self):
        assert rel(convert(1.0, "psi", "Pa"), 6894.757293168361)
        assert rel(convert(1.0, "kPa", "psi"), 1000 / 6894.757293168361)
        assert rel(convert(1.0, "bar", "kPa"), 100.0)

    def test_force_and_torque(self):
        assert rel(convert(1.0, "lbf", "N"), 4.4482216152605)
        assert rel(convert(1.0, "klbf", "N"), 4448.2216152605)
        assert rel(convert(1.0, "lbf.ft", "N.m"), 1.3558179483314004)

    def test_density_oilfield(self):
        # 1 ppg = 119.8264 kg/m3 (API/SPE reference)
        assert rel(convert(1.0, "ppg", "kg/m3"), 119.82642731689663)
        assert math.isclose(convert(9.0, "ppg", "kg/m3"), 1078.4378458520697, rel_tol=1e-9)
        assert rel(convert(1.0, "g/cm3", "sg") if False else convert(1.0, "g/cm3", "kg/m3"), 1000.0)

    def test_flow(self):
        assert rel(convert(1.0, "bpm", "gpm"), 42.0)
        assert math.isclose(convert(500.0, "gpm", "m3/s"), 500 * 0.003785411784 / 60, rel_tol=1e-12)

    def test_rotational_speed(self):
        assert rel(convert(1.0, "rpm", "rad/s"), 2 * math.pi / 60)
        assert math.isclose(convert(120.0, "rpm", "rev/min"), 120.0, rel_tol=1e-12)

    def test_dogleg_severity(self):
        # 3 deg/100ft = 3 deg per 30.48 m = 2.9528 deg/30m
        assert math.isclose(convert(3.0, "deg/100ft", "deg/30m"), 3 * 30 / 30.48, rel_tol=1e-9)
        # 1.5 deg/30m = 1.524 deg/100ft
        assert math.isclose(convert(1.5, "deg/30m", "deg/100ft"), 1.524, rel_tol=1e-9)

    def test_permeability(self):
        assert rel(convert(1.0, "mD", "m2"), 9.869233e-16)
        assert math.isclose(convert(1000.0, "mD", "D"), 1.0, rel_tol=1e-9)

    def test_yield_point(self):
        # 1 lbf/100ft2 = 0.4788 Pa
        assert math.isclose(convert(10.0, "lbf/100ft2", "Pa.yp"), 4.78802589802, rel_tol=1e-9)

    def test_round_trip_identity(self):
        for value in (0.0, 1.0, -3.5, 12345.6789):
            for src, dst in (("m", "ft"), ("psi", "kPa"), ("bbl", "m3"), ("klbf", "kN")):
                assert math.isclose(convert(convert(value, src, dst), dst, src), value, rel_tol=1e-9)


class TestAffineUnits:
    def test_celsius_fahrenheit(self):
        assert rel(convert(0.0, "degC", "degF"), 32.0)
        assert rel(convert(100.0, "degC", "degF"), 212.0)
        assert rel(convert(32.0, "degF", "degC"), 0.0)
        assert rel(convert(-40.0, "degC", "degF"), -40.0)

    def test_kelvin(self):
        assert rel(convert(273.15, "K", "degC"), 0.0)

    def test_delta_conversion_has_no_offset(self):
        # a 40 degF temperature increase == 22.22 degC
        assert math.isclose(convert_delta(40.0, "degF", "degC"), 40 * 5 / 9, rel_tol=1e-12)
        assert math.isclose(convert_delta(100.0, "degF", "degF"), 100.0, rel_tol=1e-12)


class TestNonLinearUnits:
    def test_api_gravity_to_sg(self):
        # API 35 -> SG 0.8498 (reference value from API 2540 tables)
        assert math.isclose(convert(35.0, "API", "sg"), 141.5 / 166.5, rel_tol=1e-12)
        assert math.isclose(convert(10.0, "API", "sg"), 1.0, rel_tol=1e-12)

    def test_sg_to_api_round_trip(self):
        for api in (10.0, 22.5, 35.0, 45.0):
            assert math.isclose(convert(convert(api, "API", "sg"), "sg", "API"), api, rel_tol=1e-9)

    def test_invalid_api_values_rejected(self):
        with pytest.raises(UnitError):
            convert(-200.0, "API", "sg")


class TestDimensionSafety:
    def test_dimension_mismatch_raises(self):
        with pytest.raises(UnitError) as exc:
            convert(1.0, "psi", "ft")
        assert exc.value.code == "domain.unit_error"
        assert "dimension" in str(exc.value).lower() or "cannot convert" in str(exc.value)

    def test_unknown_unit_raises(self):
        with pytest.raises(UnitError):
            get_unit("furlong")

    def test_quantity_arithmetic_requires_same_dimension(self):
        with pytest.raises(UnitError):
            q(10.0, "ppg") + q(1.0, "ft")

    def test_quantity_add_converts_operand(self):
        total = q(1.0, "bbl") + q(10.0, "gal")
        assert math.isclose(total.value, 1 + 10 / 42, rel_tol=1e-12)

    def test_quantity_scalar_ops_and_comparison(self):
        assert (q(500.0, "gpm") * 2).value == 1000.0
        assert q(1.0, "ft") > q(11.0, "in")
        assert q(120.0, "rpm").is_close(q(2.0, "rev/s"), rel_tol=1e-12)
        assert (-q(1.0, "kPa")).value == -1.0


class TestRegistryAndDisplay:
    def test_canonical_units(self):
        assert canonical_unit(Dimension.LENGTH) == "m"
        assert canonical_unit(Dimension.PRESSURE) == "Pa"
        assert canonical_unit(Dimension.ROTATIONAL_SPEED) == "rad/s"

    def test_registry_has_no_duplicate_symbols(self):
        symbols = [unit.symbol for unit in known_units()]
        assert len(symbols) == len(set(symbols))
        assert len(symbols) > 60

    def test_preferred_units_per_system(self):
        assert preferred_unit(Dimension.DENSITY, UnitSystem.FIELD_US) == "ppg"
        assert preferred_unit(Dimension.FORCE, UnitSystem.FIELD_US) == "klbf"
        assert preferred_unit(Dimension.FORCE, UnitSystem.SI) == "kN"
        assert preferred_unit(Dimension.DENSITY, UnitSystem.METRIC_ENGINEERING) == "g/cm3"

    def test_display_round_trip(self):
        # 9 ppg = 1078.44 kg/m3 -> displayed as 9.00 ppg in field units
        value_si = q(9.0, "ppg").si_value()
        assert display(value_si, Dimension.DENSITY, UnitSystem.FIELD_US, decimals=2) == "9.00 ppg"
        assert display(1.0, Dimension.LENGTH, UnitSystem.FIELD_US, decimals=3) == "3.281 ft"
        value, unit = to_display_value(6894757.293168361, Dimension.PRESSURE, UnitSystem.FIELD_US)
        assert unit == "psi" and math.isclose(value, 1000.0, rel_tol=1e-9)

    def test_quantity_format_and_si(self):
        assert q(9.0, "ppg").to("kg/m3").si_value() == pytest.approx(1078.4378, rel=1e-6)
        assert q(2.0, "ft").format(decimals=3) == "2.000 ft"

    def test_quantity_field_exposes_unit_metadata(self):
        field = quantity_field(0.0, "klbf", description="weight on bit", ge=0.0)
        assert field.json_schema_extra["unit"] == "klbf"
        assert field.json_schema_extra["dimension"] == "force"

    def test_quantity_field_rejects_unregistered_unit(self):
        with pytest.raises(UnitError):
            quantity_field(0.0, "stones")
