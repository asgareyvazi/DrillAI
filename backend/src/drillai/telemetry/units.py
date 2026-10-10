"""One conversion engine, and no arithmetic anywhere else.

The rule this module enforces: **a number is meaningless without its unit, and the platform stores one
canonical unit per dimension**. Ingestion converts *once*, here, and refuses anything it cannot convert
explicitly — an unsupported unit, a unit from another dimension, or a value with no unit at all. There
is no fallback, no "assume the canonical unit", and no rounding that changes a quantity.

The registry is a table, not code, so extending it is a data change with a test rather than a new
branch somewhere:

    (unit symbol, dimension, factor to canonical, offset to canonical)

``canonical = value * factor + offset``. That form is enough for every conversion the drilling domain
needs (pressure, force, flow, density, angle, temperature) and small enough to be read in one screen.

Why this exists at all: the same standpipe pressure arrives as psi from one rig's WITSML feed, bar from
another and kPa from a hand-entered daily report. Storing them side by side without conversion makes
"the maximum SPP this week" a question about three different quantities, and no chart can repair it
afterwards. Converting at the boundary also keeps the source unit — :class:`Conversion` returns both, so
provenance is never lost to normalisation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from drillai.core.errors import ValidationFailed
from drillai.telemetry.vocabulary import CHANNEL_DIMENSIONS

#: factor/offset to the canonical unit of the dimension. Canonical units follow SI; the two exceptions
#: are deliberate and documented: rotary speed is rpm (a revolution is an angle, and 1/min is how the
#: rig counts it) and mud weight is kg/m³ (the SI density unit, kept as its own dimension because
#: converting ppg to kg/m³ and back through ``density`` would lose the "this is the mud in the hole"
#: meaning the KPI strip depends on).
_CONVERSIONS: tuple[tuple[str, str, float, float], ...] = (
    # --- length (canonical: m) ---------------------------------------------------------
    ("m", "length", 1.0, 0.0),
    ("ft", "length", 0.3048, 0.0),
    ("in", "length", 0.0254, 0.0),
    ("cm", "length", 0.01, 0.0),
    ("mm", "length", 0.001, 0.0),
    ("km", "length", 1000.0, 0.0),
    # --- force (canonical: N) ----------------------------------------------------------
    ("N", "force", 1.0, 0.0),
    ("kN", "force", 1000.0, 0.0),
    ("klbf", "force", 4448.2216152605, 0.0),
    ("lbf", "force", 4.4482216152605, 0.0),
    ("kgf", "force", 9.80665, 0.0),
    ("tonf", "force", 9806.65, 0.0),
    # --- velocity (canonical: m/s) -----------------------------------------------------
    ("m/s", "velocity", 1.0, 0.0),
    ("m/min", "velocity", 1.0 / 60.0, 0.0),
    ("m/h", "velocity", 1.0 / 3600.0, 0.0),
    ("ft/min", "velocity", 0.3048 / 60.0, 0.0),
    ("ft/h", "velocity", 0.3048 / 3600.0, 0.0),
    ("ft/s", "velocity", 0.3048, 0.0),
    # --- rotary speed (canonical: rpm) -------------------------------------------------
    ("rpm", "rotary_speed", 1.0, 0.0),
    ("rev/min", "rotary_speed", 1.0, 0.0),
    ("1/min", "rotary_speed", 1.0, 0.0),
    ("rad/s", "rotary_speed", 60.0 / (2.0 * math.pi), 0.0),
    ("r/s", "rotary_speed", 60.0, 0.0),
    # --- flow rate (canonical: m3/s) ---------------------------------------------------
    ("m3/s", "flow_rate", 1.0, 0.0),
    ("m3/min", "flow_rate", 1.0 / 60.0, 0.0),
    ("m3/h", "flow_rate", 1.0 / 3600.0, 0.0),
    ("L/min", "flow_rate", 1.0 / 60_000.0, 0.0),
    ("L/s", "flow_rate", 1.0 / 1000.0, 0.0),
    ("gpm", "flow_rate", 0.003785411784 / 60.0, 0.0),
    ("gpm_us", "flow_rate", 0.003785411784 / 60.0, 0.0),
    ("gpm_uk", "flow_rate", 0.00454609 / 60.0, 0.0),
    ("bbl/min", "flow_rate", 0.158987294928 / 60.0, 0.0),
    # --- pressure (canonical: Pa) ------------------------------------------------------
    ("Pa", "pressure", 1.0, 0.0),
    ("kPa", "pressure", 1000.0, 0.0),
    ("MPa", "pressure", 1e6, 0.0),
    ("bar", "pressure", 100_000.0, 0.0),
    ("mbar", "pressure", 100.0, 0.0),
    ("psi", "pressure", 6894.757293168, 0.0),
    ("ksi", "pressure", 6_894_757.293168, 0.0),
    ("psia", "pressure", 6894.757293168, 0.0),
    ("psig", "pressure", 6894.757293168, 0.0),
    ("atm", "pressure", 101_325.0, 0.0),
    # --- torque (canonical: N.m) -------------------------------------------------------
    ("N.m", "torque", 1.0, 0.0),
    ("kN.m", "torque", 1000.0, 0.0),
    ("ft.lbf", "torque", 1.3558179483314004, 0.0),
    ("klbf.ft", "torque", 1355.8179483314004, 0.0),
    ("lbf.ft", "torque", 1.3558179483314004, 0.0),
    # --- density (canonical: kg/m3) ----------------------------------------------------
    ("kg/m3", "density", 1.0, 0.0),
    ("g/cm3", "density", 1000.0, 0.0),
    ("sg", "density", 1000.0, 0.0),
    ("ppg", "density", 119.82642731689663, 0.0),
    ("lb/ft3", "density", 16.01846337396014, 0.0),
    # --- mud weight (canonical: kg/m3) -------------------------------------------------
    ("kg/m3", "mud_weight", 1.0, 0.0),
    ("ppg", "mud_weight", 119.82642731689663, 0.0),
    ("sg", "mud_weight", 1000.0, 0.0),
    ("g/cm3", "mud_weight", 1000.0, 0.0),
    ("lb/ft3", "mud_weight", 16.01846337396014, 0.0),
    # --- angle (canonical: deg) --------------------------------------------------------
    ("deg", "angle", 1.0, 0.0),
    ("rad", "angle", 180.0 / math.pi, 0.0),
    ("deg/30m", "angle", 1.0, 0.0),
    ("deg/100ft", "angle", 3.280839895013123, 0.0),
    # --- temperature (canonical: degC) -------------------------------------------------
    ("degC", "temperature", 1.0, 0.0),
    ("degF", "temperature", 5.0 / 9.0, -32.0 * 5.0 / 9.0),
    ("K", "temperature", 1.0, -273.15),
    ("degR", "temperature", 5.0 / 9.0, -273.15),
    # --- ratio / dimensionless (canonical: the ratio itself) ----------------------------
    ("ratio", "ratio", 1.0, 0.0),
    ("%", "ratio", 0.01, 0.0),
    ("pct", "ratio", 0.01, 0.0),
    ("fraction", "ratio", 1.0, 0.0),
    # --- dimensionless count (canonical: the count) ------------------------------------
    ("count", "dimensionless", 1.0, 0.0),
    ("bbl", "dimensionless", 1.0, 0.0),
    ("m3", "dimensionless", 1.0, 0.0),
    ("L", "dimensionless", 1.0, 0.0),
    ("kg", "dimensionless", 1.0, 0.0),
    ("t", "dimensionless", 1.0, 0.0),
    # --- time (canonical: s) -----------------------------------------------------------
    ("s", "time", 1.0, 0.0),
    ("min", "time", 60.0, 0.0),
    ("h", "time", 3600.0, 0.0),
)

#: The unit a dimension is stored in. A channel declares its dimension at creation and its values are
#: always in this unit afterwards.
CANONICAL_UNIT: dict[str, str] = {
    "force": "N",
    "length": "m",
    "velocity": "m/s",
    "rotary_speed": "rpm",
    "flow_rate": "m3/s",
    "pressure": "Pa",
    "torque": "N.m",
    "density": "kg/m3",
    "mud_weight": "kg/m3",
    "angle": "deg",
    "temperature": "degC",
    "dimensionless": "count",
    "time": "s",
    "ratio": "ratio",
}

#: ``(unit, dimension) -> (factor, offset)``. Built once; the loop below asserts the table's own
#: coherence (no duplicate unit within a dimension, every dimension known, every dimension canonical).
_TABLE: dict[tuple[str, str], tuple[float, float]] = {}
for _unit, _dimension, _factor, _offset in _CONVERSIONS:
    _key = (_unit, _dimension)
    if _key in _TABLE:  # pragma: no cover - a typo in the table above
        raise RuntimeError(f"duplicate conversion for {_unit!r} in {_dimension!r}")
    if _dimension not in CHANNEL_DIMENSIONS:  # pragma: no cover - a typo in the table above
        raise RuntimeError(f"unknown dimension {_dimension!r} for unit {_unit!r}")
    if _dimension not in CANONICAL_UNIT:  # pragma: no cover - a typo in the table above
        raise RuntimeError(f"dimension {_dimension!r} has no canonical unit")
    _TABLE[_key] = (_factor, _offset)


def known_units(dimension: str | None = None) -> tuple[str, ...]:
    """Every unit the platform can convert, or every unit in one dimension."""

    return tuple(
        sorted({unit for (unit, dim) in _TABLE if dimension is None or dim == dimension})
    )


def unit_dimensions(unit: str) -> tuple[str, ...]:
    """The dimensions a unit symbol belongs to. Most symbols mean one thing; ``kg/m3`` is density and
    mud weight, which are the same quantity asked two different questions."""

    return tuple(sorted({dim for (u, dim) in _TABLE if u == unit}))


def dimension_of(unit: str, *, dimension: str | None = None) -> str | None:
    """The dimension a unit belongs to, or ``None`` when the platform cannot convert it.

    ``dimension`` disambiguates a symbol that belongs to more than one (``kg/m3``): the caller that
    knows it is reading a mud weight says so, and gets mud-weight semantics worth of conversions.
    """

    if dimension is not None:
        return dimension if (unit, dimension) in _TABLE else None
    found = unit_dimensions(unit)
    return found[0] if len(found) == 1 else None


@dataclass(frozen=True)
class Conversion:
    """The result of a normalisation: the canonical value *and* what it was."""

    value: float | None
    unit: str
    source_value: float | None
    source_unit: str | None
    dimension: str
    converted: bool


def convert(value: float | None, source_unit: str | None, dimension: str) -> Conversion:
    """Convert ``value`` from ``source_unit`` into the canonical unit of ``dimension``.

    Refusals, all explicit (``ValidationFailed`` with the field named):

    * an unknown dimension — the platform cannot store what it cannot name;
    * an unknown source unit — silently treating ``psi`` as ``Pa`` turns 3 500 into 3 500 pascals;
    * a unit from another dimension — ``ft`` is not a pressure, and a caller that thinks it is has a
      bug that a conversion factor would hide forever;
    * a non-finite value — ``inf``/``nan`` are not measurements.

    A ``None`` value stays ``None``: the *absence* of a measurement is recorded as ``missing`` quality,
      and is not the same as a zero, so it is preserved rather than defaulted.
    """

    if dimension not in CHANNEL_DIMENSIONS:
        raise ValidationFailed(
            "the channel's dimension is not one the platform can convert",
            details={"field": "dimension", "value": dimension, "allowed": list(CHANNEL_DIMENSIONS)},
        )
    canonical = CANONICAL_UNIT[dimension]
    if value is None:
        return Conversion(None, canonical, None, source_unit, dimension, converted=False)
    if source_unit is None:
        raise ValidationFailed(
            "a value arrived with no unit; it cannot be stored as a canonical measurement",
            details={"field": "unit", "value": None, "expected": canonical},
        )
    entry = _TABLE.get((source_unit, dimension))
    if entry is None:
        dimensions = unit_dimensions(source_unit)
        if dimensions:
            raise ValidationFailed(
                f"unit {source_unit!r} measures {', '.join(dimensions)}, not {dimension}",
                details={
                    "field": "unit",
                    "value": source_unit,
                    "expected_dimension": dimension,
                    "unit_dimensions": list(dimensions),
                },
            )
        raise ValidationFailed(
            f"unit {source_unit!r} is not known; the platform refuses to guess a conversion",
            details={"field": "unit", "value": source_unit, "allowed": list(known_units(dimension))},
        )
    factor, offset = entry
    if not math.isfinite(value):
        raise ValidationFailed(
            "the value is not a finite number",
            details={"field": "value", "value": value},
        )
    converted_value = value * factor + offset
    return Conversion(
        value=converted_value,
        unit=canonical,
        source_value=value,
        source_unit=source_unit,
        dimension=dimension,
        converted=(source_unit != canonical),
    )


def to_canonical_unit(value: float, unit: str, dimension: str) -> float:
    """The converted value alone, for callers that already recorded the provenance."""

    conversion = convert(value, unit, dimension)
    assert conversion.value is not None  # a float in, a float out; kept narrow for the type checker
    return conversion.value


def from_canonical_unit(value: float, unit: str, dimension: str) -> float:
    """Convert a stored canonical value out to ``unit`` — used by exporters and by tests that must
    state a rig-floor number in the unit the rig floor uses."""

    if dimension not in CHANNEL_DIMENSIONS:
        raise ValidationFailed("unknown dimension", details={"field": "dimension", "value": dimension})
    entry = _TABLE.get((unit, dimension))
    if entry is None:
        raise ValidationFailed(
            f"unit {unit!r} is not known for dimension {dimension!r}",
            details={"field": "unit", "value": unit, "allowed": list(known_units(dimension))},
        )
    factor, offset = entry
    return (value - offset) / factor
