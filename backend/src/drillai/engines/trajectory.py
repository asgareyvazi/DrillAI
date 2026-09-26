"""Directional drilling: minimum-curvature trajectory engine.

Implements the industry-standard **minimum curvature** method (API RP 7G / SPE survey
practice) to derive TVD, north/east coordinates, dogleg severity, build/turn rates,
closure distance/azimuth and vertical section from measured depth, inclination and
azimuth.

Why this is an engine and not a spreadsheet: every offset comparison, torque & drag run,
anti-collision check, casing design and schematic depends on the trajectory, and those
downstream results must be reproducible from the survey that produced them — hence the
declared ports (``trajectory.stations`` → ``trajectory.summary``), the recorded method
name/version and the survey quality checks that surface as constraint violations instead
of being silently smoothed away.
"""

from __future__ import annotations

import datetime as dt
import math

from pydantic import BaseModel, ConfigDict, Field, field_validator

from drillai.engines.contract import (
    ConstraintViolation,
    EngineResult,
    EngineSpec,
    FunctionEngine,
)
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field

METHOD = "minimum_curvature"
ENGINE_VERSION = "1.1.0"
_DEG = math.pi / 180.0
_30M_IN_M = 30.0

# Survey-QC thresholds (configurable per project in a later iteration; defaults chosen to
# flag suspicious data, not to make engineering judgements).
MAX_REASONABLE_DLS = 15.0      # deg/30m
MAX_STATION_GAP_M = 100.0
MAX_INCLINATION_JUMP_DEG = 10.0


class SurveyStationInput(BaseModel):
    """One survey station in canonical SI (angles in degrees, as the industry does)."""

    model_config = ConfigDict(extra="forbid")
    md_si: float = quantity_field(0.0, "m", description="measured depth", ge=0.0)
    inclination_deg: float = quantity_field(0.0, "deg", description="inclination from vertical", ge=0.0, le=180.0)
    azimuth_deg: float = quantity_field(0.0, "deg", description="azimuth (grid or magnetic, declared)")
    source: str = Field(default="plan", description="plan|mwd|gyro|tie_in|interpolated|manual")
    measured_at: dt.datetime | None = None
    magnetic_reference: str | None = None
    grid_correction_deg: float | None = quantity_field(None, "deg", description="grid convergence applied to the azimuth")
    quality_flags: list[str] = Field(default_factory=list)
    toolface_deg: float | None = quantity_field(None, "deg", description="toolface orientation, where recorded")

    @field_validator("azimuth_deg")
    @classmethod
    def _normalise_azimuth(cls, value: float) -> float:
        if value < 0 or value >= 360:
            raise ValueError("azimuth must be within [0, 360)")
        return value


class TieInPoint(BaseModel):
    """Known position of the first station (e.g. existing wellbore, slot coordinates)."""

    model_config = ConfigDict(extra="forbid")
    tvd_si: float = quantity_field(0.0, "m")
    ns_si: float = quantity_field(0.0, "m")
    ew_si: float = quantity_field(0.0, "m")


class TrajectoryEngineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stations: list[SurveyStationInput] = Field(min_length=2)
    datum: str = Field(default="rkb", description="rkb|msl|gl")
    kb_elevation_si: float | None = quantity_field(
        None, "m", description="KB elevation above MSL (for TVDSS); positive upwards"
    )
    vertical_section_azimuth_deg: float | None = quantity_field(
        None, "deg", description="azimuth used for vertical-section projection (usually the plan azimuth)"
    )
    tie_in: TieInPoint | None = None
    max_dls_limit: float | None = quantity_field(None, "deg/30m", description="planned DLS limit for validation")
    max_inc_limit: float | None = quantity_field(None, "deg", description="planned inclination limit")
    apply_grid_correction: bool = True


class SurveyStationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    md_si: float
    inclination_deg: float
    azimuth_deg: float
    tvd_si: float
    ns_si: float
    ew_si: float
    tvdss_si: float | None = None
    dls_deg_per_30m: float = 0.0
    build_rate_deg_per_30m: float = 0.0
    turn_rate_deg_per_30m: float = 0.0
    closure_distance_si: float = 0.0
    closure_azimuth_deg: float = 0.0
    vertical_section_si: float | None = None
    delta_md_si: float = 0.0
    dogleg_angle_deg: float = 0.0
    source: str = "plan"
    quality_flags: list[str] = Field(default_factory=list)


class TrajectoryEngineOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = METHOD
    engine_version: str = ENGINE_VERSION
    datum: str
    stations: list[SurveyStationResult]
    total_md_si: float
    total_tvd_si: float
    total_displacement_si: float
    closure_azimuth_deg: float
    max_inclination_deg: float
    max_inclination_md_si: float
    max_dls_deg_per_30m: float
    max_dls_md_si: float
    average_dls_deg_per_30m: float
    survey_count: int
    sources: list[str]


def minimum_curvature(inputs: TrajectoryEngineInput) -> EngineResult:
    """Compute the trajectory using the minimum-curvature method."""
    stations = sorted(inputs.stations, key=lambda station: station.md_si)
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []

    if len({station.md_si for station in stations}) != len(stations):
        raise ValueError("duplicate measured depths in survey")

    results: list[SurveyStationResult] = []
    tie_in = inputs.tie_in or TieInPoint()
    prev = stations[0]
    prev_inc = prev.inclination_deg * _DEG

    tvd = tie_in.tvd_si
    ns = tie_in.ns_si
    ew = tie_in.ew_si
    doglegs: list[float] = []

    first = SurveyStationResult(
        index=0,
        md_si=prev.md_si,
        inclination_deg=prev.inclination_deg,
        azimuth_deg=prev.azimuth_deg,
        tvd_si=tvd,
        ns_si=ns,
        ew_si=ew,
        tvdss_si=_tvdss(tvd, inputs.kb_elevation_si),
        closure_distance_si=math.hypot(ns, ew),
        closure_azimuth_deg=_azimuth_deg(ns, ew),
        vertical_section_si=_vertical_section(ns, ew, inputs.vertical_section_azimuth_deg),
        source=prev.source,
        quality_flags=list(prev.quality_flags),
    )
    results.append(first)

    for index, station in enumerate(stations[1:], start=1):
        delta_md = station.md_si - prev.md_si
        if delta_md <= 0:
            raise ValueError(f"measured depth must increase strictly (station {index})")

        inclination = station.inclination_deg * _DEG
        azimuth = _corrected_azimuth_deg(station, inputs) * _DEG
        prev_azimuth_corrected = _corrected_azimuth_deg(prev, inputs) * _DEG

        dogleg_angle = _dogleg_angle(prev_inc, prev_azimuth_corrected, inclination, azimuth)
        ratio_factor = 1.0 if dogleg_angle < 1e-9 else (2.0 / dogleg_angle) * math.tan(dogleg_angle / 2.0)

        delta_tvd = (delta_md / 2.0) * (math.cos(prev_inc) + math.cos(inclination)) * ratio_factor
        delta_ns = (
            (delta_md / 2.0)
            * (math.sin(prev_inc) * math.cos(prev_azimuth_corrected) + math.sin(inclination) * math.cos(azimuth))
            * ratio_factor
        )
        delta_ew = (
            (delta_md / 2.0)
            * (math.sin(prev_inc) * math.sin(prev_azimuth_corrected) + math.sin(inclination) * math.sin(azimuth))
            * ratio_factor
        )

        tvd += delta_tvd
        ns += delta_ns
        ew += delta_ew

        dls = math.degrees(dogleg_angle) * _30M_IN_M / delta_md
        build_rate = (station.inclination_deg - prev.inclination_deg) * _30M_IN_M / delta_md
        turn_rate = _signed_angle_deg(station.azimuth_deg - prev.azimuth_deg) * _30M_IN_M / delta_md
        doglegs.append(dls)

        flags = list(station.quality_flags)
        if delta_md > MAX_STATION_GAP_M:
            flags.append("large_station_gap")
            warnings.append(f"station gap of {delta_md:.1f} m before MD {station.md_si:.1f} m reduces accuracy")
        if abs(station.inclination_deg - prev.inclination_deg) > MAX_INCLINATION_JUMP_DEG:
            flags.append("large_inclination_change")
        if dls > MAX_REASONABLE_DLS:
            flags.append("high_dls")
            warnings.append(
                f"dogleg severity {dls:.2f} deg/30m at MD {station.md_si:.1f} m exceeds {MAX_REASONABLE_DLS} deg/30m"
            )

        if inputs.max_dls_limit is not None and dls > inputs.max_dls_limit:
            violations.append(
                ConstraintViolation(
                    name="max_dls_limit",
                    message=(
                        f"DLS {dls:.2f} deg/30m at MD {station.md_si:.1f} m exceeds the plan limit "
                        f"{inputs.max_dls_limit:.2f} deg/30m"
                    ),
                    severity="error",
                    limit=inputs.max_dls_limit,
                    actual=dls,
                    unit="deg/30m",
                    source="plan limit",
                )
            )
        if inputs.max_inc_limit is not None and station.inclination_deg > inputs.max_inc_limit:
            violations.append(
                ConstraintViolation(
                    name="max_inclination_limit",
                    message=(
                        f"inclination {station.inclination_deg:.1f} deg at MD {station.md_si:.1f} m exceeds the plan "
                        f"limit {inputs.max_inc_limit:.1f} deg"
                    ),
                    severity="error",
                    limit=inputs.max_inc_limit,
                    actual=station.inclination_deg,
                    unit="deg",
                    source="plan limit",
                )
            )

        results.append(
            SurveyStationResult(
                index=index,
                md_si=station.md_si,
                inclination_deg=station.inclination_deg,
                azimuth_deg=station.azimuth_deg,
                tvd_si=tvd,
                ns_si=ns,
                ew_si=ew,
                tvdss_si=_tvdss(tvd, inputs.kb_elevation_si),
                dls_deg_per_30m=dls,
                build_rate_deg_per_30m=build_rate,
                turn_rate_deg_per_30m=turn_rate,
                closure_distance_si=math.hypot(ns, ew),
                closure_azimuth_deg=_azimuth_deg(ns, ew),
                vertical_section_si=_vertical_section(ns, ew, inputs.vertical_section_azimuth_deg),
                delta_md_si=delta_md,
                dogleg_angle_deg=math.degrees(dogleg_angle),
                source=station.source,
                quality_flags=flags,
            )
        )
        prev, prev_inc = station, inclination

    max_dls_index = max(range(len(doglegs)), key=lambda i: doglegs[i]) if doglegs else 0
    max_inc_station = max(results, key=lambda station_result: station_result.inclination_deg)
    last = results[-1]

    outputs = TrajectoryEngineOutput(
        datum=inputs.datum,
        stations=results,
        total_md_si=last.md_si,
        total_tvd_si=last.tvd_si,
        total_displacement_si=math.hypot(last.ns_si, last.ew_si),
        closure_azimuth_deg=last.closure_azimuth_deg,
        max_inclination_deg=max_inc_station.inclination_deg,
        max_inclination_md_si=max_inc_station.md_si,
        max_dls_deg_per_30m=max(doglegs) if doglegs else 0.0,
        max_dls_md_si=results[max_dls_index + 1].md_si if doglegs else results[0].md_si,
        average_dls_deg_per_30m=_average_dls(results),
        survey_count=len(results),
        sources=sorted({station.source for station in stations}),
    )

    return EngineResult(
        outputs=outputs,
        warnings=warnings,
        violations=violations,
        assumptions_applied=[
            "minimum curvature method between survey stations",
            "linear (constant) rate of change of inclination and azimuth between stations",
            "no toolface or bending-moment correction applied",
        ],
    )


# --------------------------------------------------------------------------- helpers
def _dogleg_angle(i1: float, a1: float, i2: float, a2: float) -> float:
    cos_dl = math.cos(i2 - i1) - math.sin(i1) * math.sin(i2) * (1.0 - math.cos(a2 - a1))
    return math.acos(max(-1.0, min(1.0, cos_dl)))


def _corrected_azimuth_deg(station: SurveyStationInput, inputs: TrajectoryEngineInput) -> float:
    """Apply grid convergence only when the survey is magnetic and a correction is declared."""
    azimuth = station.azimuth_deg
    if not inputs.apply_grid_correction:
        return azimuth
    if station.magnetic_reference in ("grid", "true"):
        return azimuth
    if station.grid_correction_deg is not None:
        return (azimuth + station.grid_correction_deg) % 360.0
    return azimuth


def _average_dls(results: list[SurveyStationResult]) -> float:
    """Length-weighted average dogleg severity: total severity per drilled length.

    A simple mean over intervals would let a single short, sharp interval outweigh a long
    smooth one, so each interval contributes its DLS weighted by its length.
    """
    total_length = sum(station.delta_md_si for station in results)
    if total_length <= 0:
        return 0.0
    weighted = sum(station.dls_deg_per_30m * station.delta_md_si for station in results)
    return weighted / total_length


def _tvdss(tvd: float, kb_elevation_si: float | None) -> float | None:
    if kb_elevation_si is None:
        return None
    return tvd - kb_elevation_si


def _azimuth_deg(ns: float, ew: float) -> float:
    if abs(ns) < 1e-12 and abs(ew) < 1e-12:
        return 0.0
    return math.degrees(math.atan2(ew, ns)) % 360.0


def _vertical_section(ns: float, ew: float, azimuth_deg: float | None) -> float | None:
    if azimuth_deg is None:
        return None
    azimuth = azimuth_deg * _DEG
    return ns * math.cos(azimuth) + ew * math.sin(azimuth)


def _signed_angle_deg(delta: float) -> float:
    """Normalise an angle difference to (-180, 180]."""
    return (delta + 180.0) % 360.0 - 180.0


TRAJECTORY_SPEC = EngineSpec(
    key="trajectory.minimum_curvature",
    name="Trajectory (minimum curvature)",
    version=ENGINE_VERSION,
    domain_pack="Directional Pack",
    category="directional",
    summary=(
        "Derives TVD, N/E, DLS, build/turn rates, closure and vertical section from survey "
        "stations using the minimum-curvature method, with survey quality checks."
    ),
    inputs_model=TrajectoryEngineInput,
    outputs_model=TrajectoryEngineOutput,
    consumes=("wellbore.geometry", "trajectory.stations"),
    produces=("trajectory.summary", "trajectory.stations"),
    parameters={
        "method": {"value": METHOD, "description": "survey calculation method"},
        "max_reasonable_dls_deg_per_30m": {"value": MAX_REASONABLE_DLS, "unit": "deg/30m"},
        "max_station_gap_m": {"value": MAX_STATION_GAP_M, "unit": "m"},
    },
    assumptions=(
        "minimum curvature between stations",
        "constant inclination/azimuth rate within an interval",
        "survey positions are as-corrected (magnetic declination/grid convergence handled at input)",
    ),
    limitations=(
        "does not model bending moments, toolface history or BHA sag",
        "tight-radius and high-dogleg rotary-steerable behaviour is only as accurate as station spacing",
        "does not perform anti-collision or error-model (ISCWSA) uncertainty analysis",
    ),
    references=(
        "API RP 7G — Drill Stem Design and Operating Limits (survey calculation practice)",
        "SPE 84246 / standard minimum-curvature formulation",
    ),
    tags=("directional", "survey", "trajectory"),
    action_level="L1",
)

register_engine(FunctionEngine(TRAJECTORY_SPEC, minimum_curvature), aliases=("trajectory", "min_curvature"))
