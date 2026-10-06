"""Unit handling: one engine, explicit refusals, no silent conversion.

The failure this suite exists to prevent is arithmetic on a number whose unit nobody recorded. A
standpipe pressure of 3 500 read as pascals is 3 500 pascals; read as psi it is 24.1 megapascals. Both
are "a number in the database", and only one is a drilling measurement — so the platform converts once,
at the boundary, and refuses everything it cannot convert explicitly.
"""

from __future__ import annotations

import math

import pytest

from drillai.core.errors import ValidationFailed
from drillai.telemetry.units import (
    CANONICAL_UNIT,
    Conversion,
    convert,
    dimension_of,
    from_canonical_unit,
    known_units,
    to_canonical_unit,
    unit_dimensions,
)
from drillai.telemetry.vocabulary import CHANNEL_DIMENSIONS


@pytest.mark.parametrize(
    ("value", "unit", "dimension", "expected"),
    [
        (1.0, "ft", "length", 0.3048),
        (100.0, "ft/min", "velocity", 100.0 * 0.3048 / 60.0),
        (1.0, "gpm", "flow_rate", 0.003785411784 / 60.0),
        (1.0, "L/min", "flow_rate", 1.0 / 60_000.0),
        (9.2, "ppg", "mud_weight", 9.2 * 119.82642731689663),
        (9.2, "ppg", "density", 9.2 * 119.82642731689663),
        (3500.0, "psi", "pressure", 3500.0 * 6894.757293168),
        (100.0, "klbf", "force", 100.0 * 4448.2216152605),
        (120.0, "rpm", "rotary_speed", 120.0),
        (30.0, "deg", "angle", 30.0),
        (1.5, "deg/30m", "angle", 1.5),
        (60.0, "degF", "temperature", 15.555555555555557),
    ],
)
def test_conversions_the_domain_actually_uses(
    value: float, unit: str, dimension: str, expected: float
) -> None:
    conversion = convert(value, unit, dimension)
    assert conversion.value == pytest.approx(expected, rel=1e-9)
    assert conversion.unit == CANONICAL_UNIT[dimension]
    assert conversion.dimension == dimension
    assert conversion.source_value == value
    assert conversion.source_unit == unit


def test_the_source_unit_travels_with_the_canonical_value() -> None:
    """Provenance is not lost to normalisation: both numbers are returned, and the flag says whether
    anything changed."""

    converted = convert(100.0, "ft", "length")
    assert (converted.value, converted.source_value) == (30.48, 100.0)
    assert converted.converted is True

    unchanged = convert(30.48, "m", "length")
    assert unchanged.converted is False, "storing a value already in canonical units is not a conversion"


def test_conversion_round_trips_without_drifting() -> None:
    for unit, dimension in (("ft", "length"), ("psi", "pressure"), ("ppg", "density"), ("gpm", "flow_rate")):
        canonical = to_canonical_unit(1234.5, unit, dimension)
        assert from_canonical_unit(canonical, unit, dimension) == pytest.approx(1234.5, rel=1e-9)


def test_an_unknown_unit_is_refused_and_names_what_is_allowed() -> None:
    with pytest.raises(ValidationFailed) as caught:
        convert(1.0, "furlongs", "length")
    details = caught.value.details
    assert details["field"] == "unit"
    assert details["value"] == "furlongs"
    assert "ft" in details["allowed"] and "m" in details["allowed"]


def test_a_unit_from_another_dimension_is_refused_with_both_dimensions() -> None:
    """``ft`` is not a pressure. A conversion factor would have hidden a caller's bug forever."""

    with pytest.raises(ValidationFailed) as caught:
        convert(100.0, "ft", "pressure")
    assert caught.value.details["unit_dimensions"] == ["length"]
    assert caught.value.details["expected_dimension"] == "pressure"


def test_a_value_with_no_unit_is_refused_rather_than_assumed_canonical() -> None:
    with pytest.raises(ValidationFailed) as caught:
        convert(285.0, None, "rotary_speed")
    assert caught.value.details["field"] == "unit"
    assert caught.value.details["expected"] == "rpm"


def test_a_missing_value_stays_missing_and_is_not_a_zero() -> None:
    conversion = convert(None, "psi", "pressure")
    assert conversion.value is None
    assert conversion.source_value is None
    assert isinstance(conversion, Conversion)


def test_non_finite_values_are_refused() -> None:
    for value in (math.inf, -math.inf, math.nan):
        with pytest.raises(ValidationFailed):
            convert(value, "psi", "pressure")


def test_an_unknown_dimension_is_refused() -> None:
    with pytest.raises(ValidationFailed) as caught:
        convert(1.0, "m", "smell")
    assert caught.value.details["value"] == "smell"
    assert set(caught.value.details["allowed"]) == set(CHANNEL_DIMENSIONS)


def test_every_dimension_has_a_canonical_unit_and_at_least_one_conversion() -> None:
    """The table's own coherence, asserted rather than assumed: a dimension with no canonical unit
    could not be stored, and one with no conversions could not be filled."""

    for dimension in CHANNEL_DIMENSIONS:
        assert dimension in CANONICAL_UNIT, dimension
        assert known_units(dimension), dimension
        assert dimension_of(CANONICAL_UNIT[dimension], dimension=dimension) == dimension


def test_a_symbol_that_means_two_dimensions_can_be_disambiguated() -> None:
    """``kg/m3`` is a density and a mud weight — the same quantity asked two questions — so a caller
    that knows which one it is reading says so, and a caller that does not is told it is ambiguous."""

    assert set(unit_dimensions("kg/m3")) == {"density", "mud_weight"}
    assert dimension_of("kg/m3") is None
    assert dimension_of("kg/m3", dimension="mud_weight") == "mud_weight"
    assert dimension_of("kg/m3", dimension="pressure") is None
