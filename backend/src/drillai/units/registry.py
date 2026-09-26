"""Units of measure — a first-class, testable foundation.

Rationale
---------
Drilling data is dangerous when units are ambiguous: ``8.5`` may be inches or a bit
size, ``9.5`` ppg versus 1.14 sg, ``12`` may be klbf or kN. The platform therefore
never stores a bare number for a physical quantity. It stores:

* canonical SI values (``value_si``) for computation and comparison, and
* the unit-system preference for display, plus the original unit when the value came
  from a source document (provenance!).

This module provides the dimension/unit registry, conversion, dimension-checked
arithmetic and display presets. Symbol naming follows the Energistics Unit of Measure
dictionary (the industry custodian used by WITSML/OSDU) so that adapter mapping is
mechanical rather than interpretive.

Non-linear units (API gravity, °F/°C offsets) are supported explicitly, and *differences*
of affine units (e.g. a 40 °F temperature increase) convert without applying offsets.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from drillai.core.errors import UnitError


class Dimension(StrEnum):
    DIMENSIONLESS = "dimensionless"
    LENGTH = "length"
    AREA = "area"
    VOLUME = "volume"
    MASS = "mass"
    TIME = "time"
    TEMPERATURE = "temperature"
    PRESSURE = "pressure"
    FORCE = "force"
    TORQUE = "torque"
    LINEAR_DENSITY = "linear_density"  # mass per length (pipe/string weight)
    DENSITY = "density"
    VELOCITY = "velocity"
    ACCELERATION = "acceleration"
    ROTATIONAL_SPEED = "rotational_speed"
    ANGLE = "angle"
    ANGLE_PER_LENGTH = "angle_per_length"  # DLS, build rate, turn rate
    VOLUME_FLOW = "volume_flow"
    MASS_FLOW = "mass_flow"
    POWER = "power"
    ENERGY = "energy"
    SPECIFIC_ENERGY = "specific_energy"  # energy per volume (MSE): equivalent to pressure
    DYNAMIC_VISCOSITY = "dynamic_viscosity"
    KINEMATIC_VISCOSITY = "kinematic_viscosity"
    PERMEABILITY = "permeability"
    PRESSURE_GRADIENT = "pressure_gradient"
    TEMPERATURE_GRADIENT = "temperature_gradient"
    COST = "cost"
    CONDUCTIVITY = "conductivity"
    FREQUENCY = "frequency"
    POWER_PER_AREA = "power_per_area"
    YIELD_STRESS = "yield_stress"        # lbf/100ft^2 drilling-fluid yield point
    HEAT_CAPACITY = "heat_capacity"


class UnitSystem(StrEnum):
    SI = "si"
    FIELD_US = "field_us"            # API oilfield customary
    METRIC_ENGINEERING = "metric_engineering"  # metric with oilfield conventions (sg, barg, degC)


# --------------------------------------------------------------------------- unit record
_LINEAR = "linear"
_AFFINE = "affine"
_FUNCTION = "function"


class Unit(BaseModel):
    """A unit definition."""

    model_config = ConfigDict(frozen=True)
    symbol: str
    dimension: Dimension
    # value_in_canonical = value * factor (+ offset for affine units)
    factor: float = 1.0
    offset: float = 0.0
    kind: str = _LINEAR
    to_canonical_fn: Callable[[float], float] | None = None
    from_canonical_fn: Callable[[float], float] | None = None
    name: str = ""
    notes: str = ""
    aliases: tuple[str, ...] = ()

    def to_canonical(self, value: float) -> float:
        if self.to_canonical_fn is not None:
            return float(self.to_canonical_fn(value))
        return value * self.factor + self.offset

    def from_canonical(self, value: float) -> float:
        if self.from_canonical_fn is not None:
            return float(self.from_canonical_fn(value))
        return (value - self.offset) / self.factor

    @property
    def is_affine(self) -> bool:
        return self.kind in (_AFFINE, _FUNCTION)


# --------------------------------------------------------------------------- registry
_REGISTRY: dict[str, Unit] = {}


def register(unit: Unit) -> Unit:
    for key in (unit.symbol, *unit.aliases):
        if key in _REGISTRY and _REGISTRY[key].symbol != unit.symbol:
            raise UnitError(f"unit symbol already registered: {key}")
        _REGISTRY[key] = unit
    return unit


def get_unit(symbol: str) -> Unit:
    key = symbol.strip()
    try:
        return _REGISTRY[key]
    except KeyError as exc:
        raise UnitError(
            f"unknown unit {symbol!r}",
            details={"known_units_sample": sorted({u.symbol for u in _REGISTRY.values()})[:40]},
        ) from exc


def known_units() -> list[Unit]:
    """Return unique unit definitions sorted by dimension then symbol."""
    unique = {unit.symbol: unit for unit in _REGISTRY.values()}
    return sorted(unique.values(), key=lambda u: (u.dimension.value, u.symbol))


def catalogue(dimension: str | None = None) -> dict[str, Any]:
    """Units for the registry endpoint: definitions plus the canonical unit per dimension.

    ``to_canonical_fn``/``from_canonical_fn`` are functions, so they are reported by name (or as
    ``computed``) rather than serialised — a client needs to know a conversion is non-linear, not
    the Python callable.
    """
    items = [
        {
            "symbol": unit.symbol,
            "name": unit.name or unit.symbol,
            "dimension": unit.dimension.value if hasattr(unit.dimension, "value") else str(unit.dimension),
            "factor": unit.factor,
            "offset": unit.offset,
            "kind": unit.kind,
            "aliases": list(unit.aliases),
            "notes": unit.notes,
            "custom_conversion": unit.to_canonical_fn is not None or unit.from_canonical_fn is not None,
        }
        for unit in known_units()
        if dimension is None or str(unit.dimension) == dimension or str(getattr(unit.dimension, "value", unit.dimension)) == dimension
    ]
    return {
        "items": items,
        "total": len(items),
        "dimensions": [
            {"dimension": str(item), "canonical_unit": canonical_unit(item)} for item in Dimension
        ],
    }


def canonical_unit(dimension: Dimension) -> str:
    for unit in _REGISTRY.values():
        if unit.dimension is dimension and unit.kind == _LINEAR and unit.factor == 1.0 and unit.offset == 0.0:
            return unit.symbol
    raise UnitError(f"no canonical unit registered for {dimension}")


def convert(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a value between units, raising on dimension mismatch."""
    src, dst = get_unit(from_unit), get_unit(to_unit)
    if src.dimension is not dst.dimension:
        raise UnitError(
            f"cannot convert {from_unit!r} ({src.dimension.value}) to {to_unit!r} ({dst.dimension.value})",
            details={"from": from_unit, "to": to_unit},
        )
    if src.symbol == dst.symbol:
        return float(value)
    return dst.from_canonical(src.to_canonical(value))


def convert_delta(value: float, from_unit: str, to_unit: str) -> float:
    """Convert a *difference* of an affine unit (offsets do not apply)."""
    src, dst = get_unit(from_unit), get_unit(to_unit)
    if src.dimension is not dst.dimension:
        raise UnitError(f"cannot convert delta {from_unit!r} to {to_unit!r}")
    if src.is_affine:
        if src.factor == 0:
            raise UnitError(f"unit {src.symbol!r} has no usable scale for deltas")
        return value * src.factor / dst.factor
    return dst.from_canonical(src.to_canonical(value))


def _api_to_sg(api: float) -> float:
    if api <= -131.5:
        raise UnitError("API gravity must be greater than -131.5")
    return 141.5 / (api + 131.5)


def _sg_to_api(sg: float) -> float:
    if sg <= 0:
        raise UnitError("specific gravity must be positive")
    return 141.5 / sg - 131.5


# --- dimensionless ---------------------------------------------------------------
register(Unit(symbol="1", dimension=Dimension.DIMENSIONLESS, name="dimensionless"))
register(Unit(symbol="%", dimension=Dimension.DIMENSIONLESS, factor=0.01, name="percent", aliases=("pct",)))
register(Unit(symbol="ppm", dimension=Dimension.DIMENSIONLESS, factor=1e-6, name="parts per million"))
register(Unit(symbol="sg", dimension=Dimension.DIMENSIONLESS, name="specific gravity", aliases=("SG",)))
register(
    Unit(
        symbol="API",
        dimension=Dimension.DIMENSIONLESS,
        kind=_FUNCTION,
        to_canonical_fn=_api_to_sg,
        from_canonical_fn=_sg_to_api,
        name="API gravity",
    )
)

# --- length ----------------------------------------------------------------------
register(Unit(symbol="m", dimension=Dimension.LENGTH, name="metre"))
register(Unit(symbol="km", dimension=Dimension.LENGTH, factor=1000.0, name="kilometre"))
register(Unit(symbol="cm", dimension=Dimension.LENGTH, factor=0.01, name="centimetre"))
register(Unit(symbol="mm", dimension=Dimension.LENGTH, factor=0.001, name="millimetre"))
register(Unit(symbol="ft", dimension=Dimension.LENGTH, factor=0.3048, name="foot", aliases=("'",)))
register(Unit(symbol="in", dimension=Dimension.LENGTH, factor=0.0254, name="inch", aliases=('"', "inch")))
register(Unit(symbol="1/16in", dimension=Dimension.LENGTH, factor=0.0254 / 16, name="sixteenth of an inch"))

# --- area / volume ---------------------------------------------------------------
register(Unit(symbol="m2", dimension=Dimension.AREA, name="square metre", aliases=("m^2",)))
register(Unit(symbol="in2", dimension=Dimension.AREA, factor=0.00064516, name="square inch", aliases=("in^2",)))
register(Unit(symbol="mm2", dimension=Dimension.AREA, factor=1e-6, name="square millimetre", aliases=("mm^2",)))
register(Unit(symbol="m3", dimension=Dimension.VOLUME, name="cubic metre", aliases=("m^3",)))
register(Unit(symbol="L", dimension=Dimension.VOLUME, factor=0.001, name="litre", aliases=("l", "liter")))
register(Unit(symbol="bbl", dimension=Dimension.VOLUME, factor=0.158987294928, name="US oil barrel"))
register(Unit(symbol="gal", dimension=Dimension.VOLUME, factor=0.003785411784, name="US gallon"))
register(Unit(symbol="ft3", dimension=Dimension.VOLUME, factor=0.028316846592, name="cubic foot", aliases=("ft^3",)))
register(Unit(symbol="scf", dimension=Dimension.VOLUME, factor=0.028316846592, name="standard cubic foot @ 60F/14.696psia"))
register(Unit(symbol="MMscf", dimension=Dimension.VOLUME, factor=28316.846592, name="million standard cubic feet"))

# --- mass ------------------------------------------------------------------------
register(Unit(symbol="kg", dimension=Dimension.MASS, name="kilogram"))
register(Unit(symbol="t", dimension=Dimension.MASS, factor=1000.0, name="metric tonne", aliases=("tonne",)))
register(Unit(symbol="lbm", dimension=Dimension.MASS, factor=0.45359237, name="pound mass", aliases=("lb", "lbs")))
register(Unit(symbol="klbm", dimension=Dimension.MASS, factor=453.59237, name="thousand pounds mass", aliases=("klbs",)))
register(Unit(symbol="sack", dimension=Dimension.MASS, factor=42.63768278, name="cement sack (94 lbm)"))
register(
    Unit(
        symbol="ppb",
        dimension=Dimension.MASS,
        factor=0.45359237 / 0.158987294928,
        name="pounds per barrel (as mass concentration)",
    )
)

# --- time ------------------------------------------------------------------------
register(Unit(symbol="s", dimension=Dimension.TIME, name="second"))
register(Unit(symbol="min", dimension=Dimension.TIME, factor=60.0, name="minute"))
register(Unit(symbol="h", dimension=Dimension.TIME, factor=3600.0, name="hour", aliases=("hr",)))
register(Unit(symbol="a", dimension=Dimension.TIME, factor=31557600.0, name="year (Julian)", aliases=("yr", "y")))
register(Unit(symbol="d", dimension=Dimension.TIME, factor=86400.0, name="day"))

# --- temperature ------------------------------------------------------------------
register(Unit(symbol="K", dimension=Dimension.TEMPERATURE, name="kelvin"))
register(Unit(symbol="degC", dimension=Dimension.TEMPERATURE, factor=1.0, offset=273.15, kind=_AFFINE, name="degree Celsius", aliases=("C", "celsius")))
register(Unit(symbol="degF", dimension=Dimension.TEMPERATURE, factor=5.0 / 9.0, offset=459.67 * 5.0 / 9.0, kind=_AFFINE, name="degree Fahrenheit", aliases=("F", "fahrenheit")))
register(Unit(symbol="degR", dimension=Dimension.TEMPERATURE, factor=5.0 / 9.0, name="degree Rankine", aliases=("R",)))

# --- pressure / stress --------------------------------------------------------------
register(Unit(symbol="Pa", dimension=Dimension.PRESSURE, name="pascal"))
register(Unit(symbol="kPa", dimension=Dimension.PRESSURE, factor=1000.0, name="kilopascal"))
register(Unit(symbol="MPa", dimension=Dimension.PRESSURE, factor=1e6, name="megapascal"))
register(Unit(symbol="GPa", dimension=Dimension.PRESSURE, factor=1e9, name="gigapascal"))
register(Unit(symbol="bar", dimension=Dimension.PRESSURE, factor=1e5, name="bar"))
register(Unit(symbol="barg", dimension=Dimension.PRESSURE, factor=1e5, name="bar gauge (reference = atmospheric)"))
register(Unit(symbol="psi", dimension=Dimension.PRESSURE, factor=6894.757293168361, name="pound force per square inch"))
register(Unit(symbol="ksi", dimension=Dimension.PRESSURE, factor=6894757.293168361, name="thousand psi"))
register(Unit(symbol="psia", dimension=Dimension.PRESSURE, factor=6894.757293168361, name="psi absolute"))
register(Unit(symbol="psig", dimension=Dimension.PRESSURE, factor=6894.757293168361, name="psi gauge (reference = atmospheric)"))
register(Unit(symbol="atm", dimension=Dimension.PRESSURE, factor=101325.0, name="standard atmosphere"))

# --- force / torque ---------------------------------------------------------------
register(Unit(symbol="N", dimension=Dimension.FORCE, name="newton"))
register(Unit(symbol="kN", dimension=Dimension.FORCE, factor=1000.0, name="kilonewton"))
register(Unit(symbol="daN", dimension=Dimension.FORCE, factor=10.0, name="dekanewton", aliases=("decaN",)))
register(Unit(symbol="lbf", dimension=Dimension.FORCE, factor=4.4482216152605, name="pound force", aliases=("lb_f",)))
register(Unit(symbol="klbf", dimension=Dimension.FORCE, factor=4448.2216152605, name="thousand pound force", aliases=("klb", "kips")))
register(Unit(symbol="kgf", dimension=Dimension.FORCE, factor=9.80665, name="kilogram force", aliases=("kgf/cm2 basis",)))
register(Unit(symbol="tonf", dimension=Dimension.FORCE, factor=9964.01641818352, name="US short ton force", aliases=("ton-force",)))
register(Unit(symbol="N.m", dimension=Dimension.TORQUE, name="newton metre", aliases=("Nm", "N*m")))
register(Unit(symbol="kN.m", dimension=Dimension.TORQUE, factor=1000.0, name="kilonewton metre", aliases=("kNm",)))
register(Unit(symbol="lbf.ft", dimension=Dimension.TORQUE, factor=1.3558179483314004, name="pound force foot", aliases=("ft.lbf", "ft-lbf", "ftlbf")))
register(Unit(symbol="lbf.in", dimension=Dimension.TORQUE, factor=0.11298482902761666, name="pound force inch", aliases=("in.lbf",)))
register(Unit(symbol="klbf.ft", dimension=Dimension.TORQUE, factor=1355.8179483314004, name="thousand pound force foot", aliases=("klb.ft",)))

# --- density / linear density -------------------------------------------------------
register(Unit(symbol="kg/m3", dimension=Dimension.DENSITY, name="kilogram per cubic metre", aliases=("kg/m^3",)))
register(Unit(symbol="g/cm3", dimension=Dimension.DENSITY, factor=1000.0, name="gram per cubic centimetre", aliases=("g/cc", "g/cm^3")))
register(Unit(symbol="ppg", dimension=Dimension.DENSITY, factor=119.82642731689663, name="pound per US gallon"))
register(Unit(symbol="pcf", dimension=Dimension.DENSITY, factor=16.018463373960142, name="pound per cubic foot", aliases=("lbm/ft3",)))
register(Unit(symbol="kg/m", dimension=Dimension.LINEAR_DENSITY, name="kilogram per metre"))
register(Unit(symbol="lbm/ft", dimension=Dimension.LINEAR_DENSITY, factor=1.4881639435695537, name="pound mass per foot", aliases=("lb/ft",)))

# --- velocity / acceleration --------------------------------------------------------
register(Unit(symbol="m/s", dimension=Dimension.VELOCITY, name="metre per second"))
register(Unit(symbol="ft/s", dimension=Dimension.VELOCITY, factor=0.3048, name="foot per second", aliases=("fps",)))
register(Unit(symbol="m/h", dimension=Dimension.VELOCITY, factor=1 / 3600, name="metre per hour"))
register(Unit(symbol="ft/h", dimension=Dimension.VELOCITY, factor=0.3048 / 3600, name="foot per hour", aliases=("ft/hr",)))
register(Unit(symbol="m/min", dimension=Dimension.VELOCITY, factor=1 / 60, name="metre per minute"))
register(Unit(symbol="ft/min", dimension=Dimension.VELOCITY, factor=0.3048 / 60, name="foot per minute", aliases=("ft/min",)))
register(Unit(symbol="m/s2", dimension=Dimension.ACCELERATION, name="metre per second squared", aliases=("m/s^2",)))
register(Unit(symbol="ft/s2", dimension=Dimension.ACCELERATION, factor=0.3048, name="foot per second squared", aliases=("ft/s^2",)))

# --- rotational / angle -------------------------------------------------------------
register(Unit(symbol="rad", dimension=Dimension.ANGLE, name="radian"))
register(Unit(symbol="deg", dimension=Dimension.ANGLE, factor=math.pi / 180.0, name="degree", aliases=("°",)))
register(Unit(symbol="rad/s", dimension=Dimension.ROTATIONAL_SPEED, name="radian per second"))
register(Unit(symbol="rpm", dimension=Dimension.ROTATIONAL_SPEED, factor=2 * math.pi / 60.0, name="revolution per minute"))
register(Unit(symbol="rev/min", dimension=Dimension.ROTATIONAL_SPEED, factor=2 * math.pi / 60.0, name="revolution per minute"))
register(Unit(symbol="rev/s", dimension=Dimension.ROTATIONAL_SPEED, factor=2 * math.pi, name="revolution per second", aliases=("rps",)))
register(Unit(symbol="deg/30m", dimension=Dimension.ANGLE_PER_LENGTH, factor=(math.pi / 180.0) / 30.0, name="degree per 30 metre (dogleg severity)"))
register(Unit(symbol="deg/100ft", dimension=Dimension.ANGLE_PER_LENGTH, factor=(math.pi / 180.0) / 30.48, name="degree per 100 foot (dogleg severity)"))
register(Unit(symbol="rad/m", dimension=Dimension.ANGLE_PER_LENGTH, name="radian per metre"))

# --- flow --------------------------------------------------------------------------
register(Unit(symbol="m3/s", dimension=Dimension.VOLUME_FLOW, name="cubic metre per second", aliases=("m^3/s",)))
register(Unit(symbol="L/min", dimension=Dimension.VOLUME_FLOW, factor=1e-3 / 60, name="litre per minute", aliases=("lpm",)))
register(Unit(symbol="gpm", dimension=Dimension.VOLUME_FLOW, factor=0.003785411784 / 60, name="US gallon per minute", aliases=("gal/min",)))
register(Unit(symbol="bpm", dimension=Dimension.VOLUME_FLOW, factor=0.158987294928 / 60, name="US barrel per minute", aliases=("bbl/min",)))
register(Unit(symbol="kg/s", dimension=Dimension.MASS_FLOW, name="kilogram per second"))
register(Unit(symbol="lbm/h", dimension=Dimension.MASS_FLOW, factor=0.45359237 / 3600, name="pound mass per hour"))

# --- power / energy -------------------------------------------------------------------
register(Unit(symbol="W", dimension=Dimension.POWER, name="watt"))
register(Unit(symbol="kW", dimension=Dimension.POWER, factor=1000.0, name="kilowatt"))
register(Unit(symbol="hp", dimension=Dimension.POWER, factor=745.6998715822702, name="mechanical horsepower"))
register(Unit(symbol="J", dimension=Dimension.ENERGY, name="joule"))
register(Unit(symbol="kJ", dimension=Dimension.ENERGY, factor=1000.0, name="kilojoule"))
register(Unit(symbol="btu", dimension=Dimension.ENERGY, factor=1055.05585262, name="British thermal unit"))
register(Unit(symbol="J/m3", dimension=Dimension.SPECIFIC_ENERGY, name="joule per cubic metre (MSE)", aliases=("J/m^3",)))
register(Unit(symbol="kPa-mse", dimension=Dimension.SPECIFIC_ENERGY, factor=1000.0, name="kilopascal (as MSE)", aliases=("kPa(MSE)",)))

# --- fluid / rock ---------------------------------------------------------------------
register(Unit(symbol="Pa.s", dimension=Dimension.DYNAMIC_VISCOSITY, name="pascal second", aliases=("Pa*s",)))
register(Unit(symbol="cP", dimension=Dimension.DYNAMIC_VISCOSITY, factor=0.001, name="centipoise", aliases=("cp",)))
register(Unit(symbol="m2/s", dimension=Dimension.KINEMATIC_VISCOSITY, name="square metre per second", aliases=("m^2/s",)))
register(Unit(symbol="cSt", dimension=Dimension.KINEMATIC_VISCOSITY, factor=1e-6, name="centistokes"))
register(Unit(symbol="m2", dimension=Dimension.PERMEABILITY, name="square metre (permeability)", aliases=("m2-perm",)))
register(Unit(symbol="mD", dimension=Dimension.PERMEABILITY, factor=9.869233e-16, name="millidarcy"))
register(Unit(symbol="D", dimension=Dimension.PERMEABILITY, factor=9.869233e-13, name="darcy"))
register(Unit(symbol="Pa/m", dimension=Dimension.PRESSURE_GRADIENT, name="pascal per metre"))
register(Unit(symbol="psi/ft", dimension=Dimension.PRESSURE_GRADIENT, factor=6894.757293168361 / 0.3048, name="psi per foot"))
register(Unit(symbol="kPa/m", dimension=Dimension.PRESSURE_GRADIENT, factor=1000.0, name="kilopascal per metre"))
register(Unit(symbol="ppg-emw", dimension=Dimension.PRESSURE_GRADIENT, factor=119.82642731689663 * 9.80665, name="ppg equivalent mud weight gradient", aliases=("ppg/m",)))
register(Unit(symbol="K/m", dimension=Dimension.TEMPERATURE_GRADIENT, name="kelvin per metre"))
register(Unit(symbol="degC/100m", dimension=Dimension.TEMPERATURE_GRADIENT, factor=1 / 100, name="degree Celsius per 100 metre"))
register(Unit(symbol="degF/100ft", dimension=Dimension.TEMPERATURE_GRADIENT, factor=(5.0 / 9.0) / 30.48, name="degree Fahrenheit per 100 foot"))
register(Unit(symbol="lbf/100ft2", dimension=Dimension.YIELD_STRESS, factor=0.478802589802, name="pound force per 100 square feet (yield point)", aliases=("lbf/100sqft",)))
register(Unit(symbol="Pa.yp", dimension=Dimension.YIELD_STRESS, factor=1.0, name="pascal (as yield stress)"))
register(Unit(symbol="1/s", dimension=Dimension.FREQUENCY, name="per second (frequency)", aliases=("Hz", "1/second")))
register(Unit(symbol="1/min", dimension=Dimension.FREQUENCY, factor=1 / 60.0, name="per minute", aliases=("spm", "1/minute", "rpm-freq")))

register(Unit(symbol="W/m2", dimension=Dimension.POWER_PER_AREA, name="watt per square metre", aliases=("W/m^2",)))
register(Unit(symbol="hp/in2", dimension=Dimension.POWER_PER_AREA, factor=745.6998715822702 / (0.0254**2), name="hydraulic horsepower per square inch", aliases=("HSI", "hsi")))
register(Unit(symbol="kW/m2", dimension=Dimension.POWER_PER_AREA, factor=1000.0, name="kilowatt per square metre"))

register(Unit(symbol="W/m.K", dimension=Dimension.CONDUCTIVITY, name="watt per metre kelvin", aliases=("W/(m.K)",)))
register(Unit(symbol="J/kg.K", dimension=Dimension.HEAT_CAPACITY, name="joule per kilogram kelvin", aliases=("J/(kg.K)",)))
register(Unit(symbol="USD", dimension=Dimension.COST, name="US dollar"))
register(Unit(symbol="USD/ft", dimension=Dimension.COST, name="US dollar (per foot basis)", aliases=("USD-per-ft",)))


# --------------------------------------------------------------------------- quantities
class Quantity(BaseModel):
    """A physical value with an explicit unit."""

    model_config = ConfigDict(frozen=True)
    value: float
    unit: str = Field(description="unit symbol registered in the platform UOM registry")

    @field_validator("unit")
    @classmethod
    def _unit_exists(cls, value: str) -> str:
        get_unit(value)  # raises UnitError
        return value

    @property
    def dimension(self) -> Dimension:
        return get_unit(self.unit).dimension

    def to(self, unit: str) -> Quantity:
        return Quantity(value=convert(self.value, self.unit, unit), unit=unit)

    def to_si(self) -> Quantity:
        return self.to(canonical_unit(self.dimension))

    def si_value(self) -> float:
        return get_unit(self.unit).to_canonical(self.value)

    def __add__(self, other: Quantity) -> Quantity:
        _require_same_dimension(self, other)
        return Quantity(value=self.value + other.to(self.unit).value, unit=self.unit)

    def __sub__(self, other: Quantity) -> Quantity:
        _require_same_dimension(self, other)
        return Quantity(value=self.value - other.to(self.unit).value, unit=self.unit)

    def __mul__(self, scalar: float) -> Quantity:
        return Quantity(value=self.value * float(scalar), unit=self.unit)

    __rmul__ = __mul__

    def __truediv__(self, scalar: float) -> Quantity:
        return Quantity(value=self.value / float(scalar), unit=self.unit)

    def __neg__(self) -> Quantity:
        return Quantity(value=-self.value, unit=self.unit)

    def __lt__(self, other: Quantity) -> bool:  # type: ignore[override]
        _require_same_dimension(self, other)
        return self.value < other.to(self.unit).value

    def __le__(self, other: Quantity) -> bool:  # type: ignore[override]
        _require_same_dimension(self, other)
        return self.value <= other.to(self.unit).value

    def is_close(self, other: Quantity, rel_tol: float = 1e-9, abs_tol: float = 0.0) -> bool:
        _require_same_dimension(self, other)
        return math.isclose(self.value, other.to(self.unit).value, rel_tol=rel_tol, abs_tol=abs_tol)

    def format(self, decimals: int | None = None, system: UnitSystem | None = None) -> str:
        """Human display, optionally converting to a unit-system preference."""
        target = self.unit
        if system is not None:
            target = preferred_unit(self.dimension, system)
        converted = self.to(target)
        if decimals is None:
            decimals = _auto_decimals(converted.value)
        return f"{converted.value:,.{decimals}f} {target}"


def _require_same_dimension(a: Quantity, b: Quantity) -> None:
    if a.dimension is not b.dimension:
        raise UnitError(
            f"dimension mismatch: {a.unit} ({a.dimension.value}) vs {b.unit} ({b.dimension.value})"
        )


def _auto_decimals(value: float) -> int:
    magnitude = abs(value)
    if magnitude == 0:
        return 2
    if magnitude >= 1000:
        return 0
    if magnitude >= 100:
        return 1
    if magnitude >= 1:
        return 2
    return 3


def q(value: float, unit: str) -> Quantity:
    """Terse constructor used across engines: ``q(120, "rpm")``."""
    return Quantity(value=value, unit=unit)


# --------------------------------------------------------------------------- display presets
_PREFERRED: dict[UnitSystem, dict[Dimension, str]] = {
    UnitSystem.SI: {
        Dimension.LENGTH: "m",
        Dimension.VOLUME: "m3",
        Dimension.DENSITY: "kg/m3",
        Dimension.PRESSURE: "kPa",
        Dimension.FORCE: "kN",
        Dimension.TORQUE: "kN.m",
        Dimension.VELOCITY: "m/h",
        Dimension.VOLUME_FLOW: "L/min",
        Dimension.TEMPERATURE: "degC",
        Dimension.LINEAR_DENSITY: "kg/m",
        Dimension.ANGLE_PER_LENGTH: "deg/30m",
        Dimension.PERMEABILITY: "mD",
        Dimension.PRESSURE_GRADIENT: "kPa/m",
        Dimension.MASS: "t",
        Dimension.SPECIFIC_ENERGY: "kPa-mse",
    },
    UnitSystem.METRIC_ENGINEERING: {
        Dimension.LENGTH: "m",
        Dimension.VOLUME: "m3",
        Dimension.DENSITY: "g/cm3",
        Dimension.PRESSURE: "bar",
        Dimension.FORCE: "daN",
        Dimension.TORQUE: "kN.m",
        Dimension.VELOCITY: "m/h",
        Dimension.VOLUME_FLOW: "L/min",
        Dimension.TEMPERATURE: "degC",
        Dimension.LINEAR_DENSITY: "kg/m",
        Dimension.ANGLE_PER_LENGTH: "deg/30m",
        Dimension.PERMEABILITY: "mD",
        Dimension.PRESSURE_GRADIENT: "kPa/m",
        Dimension.MASS: "t",
        Dimension.SPECIFIC_ENERGY: "kPa-mse",
    },
    UnitSystem.FIELD_US: {
        Dimension.LENGTH: "ft",
        Dimension.VOLUME: "bbl",
        Dimension.DENSITY: "ppg",
        Dimension.PRESSURE: "psi",
        Dimension.FORCE: "klbf",
        Dimension.TORQUE: "lbf.ft",
        Dimension.VELOCITY: "ft/h",
        Dimension.VOLUME_FLOW: "gpm",
        Dimension.TEMPERATURE: "degF",
        Dimension.LINEAR_DENSITY: "lbm/ft",
        Dimension.ANGLE_PER_LENGTH: "deg/100ft",
        Dimension.PERMEABILITY: "mD",
        Dimension.PRESSURE_GRADIENT: "psi/ft",
        Dimension.MASS: "lbm",
        Dimension.SPECIFIC_ENERGY: "psi",
        Dimension.AREA: "in2",
    },
}


def preferred_unit(dimension: Dimension, system: UnitSystem) -> str:
    preset = _PREFERRED.get(system, {})
    if dimension in preset:
        return preset[dimension]
    return canonical_unit(dimension)


def display(value_si: float, dimension: Dimension, system: UnitSystem, decimals: int | None = None) -> str:
    """Format a canonical (SI) value for display in the given unit system."""
    unit = preferred_unit(dimension, system)
    converted = get_unit(unit).from_canonical(value_si)
    if decimals is None:
        decimals = _auto_decimals(converted)
    return f"{converted:,.{decimals}f} {unit}"


def to_display_value(value_si: float, dimension: Dimension, system: UnitSystem) -> tuple[float, str]:
    unit = preferred_unit(dimension, system)
    return get_unit(unit).from_canonical(value_si), unit


def from_unit(value: float, unit: str) -> float:
    """Convert an input value to canonical SI."""
    return get_unit(unit).to_canonical(value)


def quantity_field(
    default: float,
    unit: str,
    *,
    description: str = "",
    ge: float | None = None,
    le: float | None = None,
    gt: float | None = None,
    lt: float | None = None,
    examples: list[float] | None = None,
) -> Any:
    """Create a float field carrying its unit in the JSON schema.

    Engine input/output contracts use this so that units travel with the schema — the
    API, the workflow node inspector and the docs all render units from one source.
    """
    get_unit(unit)
    extra: dict[str, Any] = {"unit": unit, "dimension": get_unit(unit).dimension.value}
    if examples:
        extra["examples"] = examples
    return Field(
        default=default, description=description or None, ge=ge, le=le, gt=gt, lt=lt, json_schema_extra=extra
    )


def dimensional_consistency(quantities: list[Quantity], expected: Dimension) -> None:
    """Assert that all provided quantities share an expected dimension."""
    for item in quantities:
        if item.dimension is not expected:
            raise UnitError(f"expected {expected.value}, got {item.dimension.value} for {item.unit}")
