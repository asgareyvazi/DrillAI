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
import itertools
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from drillai.engines.contract import (
    ConstraintViolation,
    EngineResult,
    EngineSpec,
    FunctionEngine,
)
from drillai.engines.registry import register_engine
from drillai.units.registry import quantity_field

METHOD = "minimum_curvature"
ENGINE_VERSION = "1.3.0"
_DEG = math.pi / 180.0
_ANTIPODAL_DOGLEG_TOLERANCE_RAD = 1e-8
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
    azimuth_deg: float = quantity_field(0.0, "deg", description="raw input azimuth in the explicitly declared reference")
    azimuth_reference: Literal["grid", "true", "magnetic", "unknown"] = Field(
        default="unknown",
        description="reference of azimuth_deg; unknown is rejected for trajectory calculation",
    )
    azimuth_reference_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        description="operator-declared frame identity (for example the applicable project grid/EPSG reference)",
    )
    source: str = Field(default="plan", description="plan|mwd|gyro|tie_in|interpolated|manual")
    measured_at: dt.datetime | None = None
    grid_correction_deg: float | None = quantity_field(
        None,
        "deg",
        description=(
            "signed additive correction from this station's declared azimuth_reference into grid; "
            "used only when target_azimuth_reference is explicitly grid"
        ),
    )
    quality_flags: list[str] = Field(default_factory=list)
    toolface_deg: float | None = quantity_field(None, "deg", description="toolface orientation, where recorded")

    @field_validator("azimuth_deg", "grid_correction_deg")
    @classmethod
    def _require_finite_angles(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("azimuth and azimuth corrections must be finite")
        return value

    @field_validator("azimuth_deg")
    @classmethod
    def _normalise_azimuth(cls, value: float) -> float:
        if value < 0 or value >= 360:
            raise ValueError("azimuth must be within [0, 360)")
        return value

    @field_validator("grid_correction_deg")
    @classmethod
    def _bound_grid_correction(cls, value: float | None) -> float | None:
        if value is not None and not -360.0 <= value <= 360.0:
            raise ValueError("grid_correction_deg must be between -360 and 360 degrees")
        return value

    @field_validator("azimuth_reference_id")
    @classmethod
    def _normalize_reference_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or len(value) > 120 or not value.isprintable():
            raise ValueError("azimuth_reference_id must be non-empty printable text no longer than 120 characters")
        return value

    @model_validator(mode="after")
    def _require_reference_identity(self) -> SurveyStationInput:
        if self.azimuth_reference != "unknown" and self.azimuth_reference_id is None:
            raise ValueError("a declared azimuth_reference requires azimuth_reference_id")
        return self

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_magnetic_reference(cls, value: object) -> object:
        """Accept the former field name, but never infer a reference from its absence."""
        if not isinstance(value, dict) or "magnetic_reference" not in value:
            return value
        data = dict(value)
        legacy_reference = data.pop("magnetic_reference")
        if legacy_reference is None:
            return data
        if "azimuth_reference" in data and data["azimuth_reference"] != legacy_reference:
            raise ValueError("magnetic_reference and azimuth_reference declarations conflict")
        data["azimuth_reference"] = legacy_reference
        return data


def _resolve_azimuth_frame(
    stations: list[SurveyStationInput],
    target_reference: Literal["grid", "true", "magnetic"] | None,
    target_reference_id: str | None,
    *,
    allow_legacy_grid_correction: bool | None,
) -> tuple[Literal["grid", "true", "magnetic"], str, list[tuple[float, float | None]]]:
    """Validate declared frame identities and return azimuths in one explicit output frame."""
    if not stations:
        raise ValueError("at least one station is required to resolve the survey azimuth reference")
    unknown = [index for index, station in enumerate(stations) if station.azimuth_reference == "unknown"]
    if unknown:
        raise ValueError(
            f"survey station(s) {unknown} have unknown azimuth_reference; declare grid, true, or magnetic explicitly"
        )
    declared = {(station.azimuth_reference, station.azimuth_reference_id) for station in stations}
    if target_reference is None:
        if target_reference_id is not None:
            raise ValueError("target_azimuth_reference_id requires target_azimuth_reference")
        if len(declared) != 1:
            raise ValueError(
                "survey stations use incompatible azimuth reference identities; declare target_azimuth_reference "
                "and target_azimuth_reference_id plus an explicit supported correction for every converted station"
            )
        target_reference, target_reference_id = next(iter(declared))
    if target_reference_id is None:
        raise ValueError("target_azimuth_reference_id is required with an explicit target_azimuth_reference")

    corrected: list[tuple[float, float | None]] = []
    for index, station in enumerate(stations):
        source_frame = (station.azimuth_reference, station.azimuth_reference_id)
        target_frame = (target_reference, target_reference_id)
        correction = station.grid_correction_deg
        if source_frame == target_frame:
            if correction is not None:
                raise ValueError(
                    f"station {index} declares a grid correction although its azimuth is already in "
                    f"target frame {target_frame!r}"
                )
            corrected.append((station.azimuth_deg, None))
            continue
        if target_reference != "grid":
            raise ValueError(
                f"station {index} cannot be converted from {source_frame!r} to {target_frame!r}; "
                "only an explicitly declared correction into a named grid frame is supported"
            )
        if allow_legacy_grid_correction is False:
            raise ValueError("apply_grid_correction=False conflicts with converting a station into grid reference")
        if correction is None:
            raise ValueError(
                f"station {index} requires an explicit signed grid_correction_deg from {source_frame!r} "
                f"into target grid frame {target_reference_id!r}"
            )
        corrected.append(((station.azimuth_deg + correction) % 360.0, correction))
    return target_reference, target_reference_id, corrected


class TieInPoint(BaseModel):
    """Known position of the first station (e.g. existing wellbore, slot coordinates)."""

    model_config = ConfigDict(extra="forbid")
    tvd_si: float = quantity_field(
        0.0, "m", description="tie-in TVD expressed in the parent TrajectoryEngineInput.datum"
    )
    ns_si: float = quantity_field(0.0, "m")
    ew_si: float = quantity_field(0.0, "m")
    azimuth_reference: Literal["grid", "true", "magnetic", "unknown"] = "unknown"
    azimuth_reference_id: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("azimuth_reference_id")
    @classmethod
    def _normalize_reference_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or len(value) > 120 or not value.isprintable():
            raise ValueError("tie-in azimuth_reference_id must be non-empty printable text up to 120 characters")
        return value

    @model_validator(mode="after")
    def _validate_lateral_reference(self) -> TieInPoint:
        if (
            (abs(self.ns_si) > 0 or abs(self.ew_si) > 0)
            and (self.azimuth_reference == "unknown" or self.azimuth_reference_id is None)
        ):
            raise ValueError("a non-zero tie-in requires an explicit azimuth_reference and azimuth_reference_id")
        if self.azimuth_reference != "unknown" and self.azimuth_reference_id is None:
            raise ValueError("a declared tie-in azimuth_reference requires azimuth_reference_id")
        return self


class TrajectoryEngineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stations: list[SurveyStationInput] = Field(min_length=2)
    datum: Literal["rkb", "msl", "gl", "cf", "derrick_floor", "unknown"] = Field(
        default="unknown",
        description="explicit vertical reference datum (rkb, msl, gl, cf, or derrick_floor); unknown is preserved and cannot be reconciled",
    )
    kb_elevation_si: float | None = quantity_field(
        None, "m", description="KB elevation above MSL (for TVDSS); positive upwards"
    )
    vertical_section_azimuth_deg: float | None = quantity_field(
        None,
        "deg",
        description="azimuth used for vertical-section projection in its separately declared reference frame",
    )
    vertical_section_azimuth_reference: Literal["grid", "true", "magnetic", "unknown"] = "unknown"
    vertical_section_azimuth_reference_id: str | None = Field(default=None, min_length=1, max_length=120)
    tie_in: TieInPoint | None = None
    target_azimuth_reference: Literal["grid", "true", "magnetic"] | None = Field(
        default=None,
        description=(
            "common output frame; when omitted every station must explicitly declare the same known reference. "
            "Reference changes require a signed per-station grid_correction_deg into grid."
        ),
    )
    target_azimuth_reference_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=120,
        description="operator-declared identity of the common output reference frame",
    )
    max_dls_limit: float | None = quantity_field(None, "deg/30m", description="planned DLS limit for validation")
    max_inc_limit: float | None = quantity_field(None, "deg", description="planned inclination limit")
    apply_grid_correction: bool | None = Field(
        default=None,
        deprecated=True,
        description="legacy explicit request for grid output; prefer target_azimuth_reference='grid'",
    )

    @field_validator("vertical_section_azimuth_deg")
    @classmethod
    def _validate_vertical_section_azimuth(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or not 0.0 <= value < 360.0):
            raise ValueError("vertical_section_azimuth_deg must be finite and within [0, 360)")
        return value

    @field_validator("vertical_section_azimuth_reference_id")
    @classmethod
    def _normalize_vertical_section_reference_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or len(value) > 120 or not value.isprintable():
            raise ValueError(
                "vertical_section_azimuth_reference_id must be non-empty printable text no longer than 120 characters"
            )
        return value

    @field_validator("target_azimuth_reference_id")
    @classmethod
    def _normalize_target_reference_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or len(value) > 120 or not value.isprintable():
            raise ValueError(
                "target_azimuth_reference_id must be non-empty printable text no longer than 120 characters"
            )
        return value

    @model_validator(mode="after")
    def _migrate_legacy_correction_request(self) -> TrajectoryEngineInput:
        if self.apply_grid_correction is True:
            if self.target_azimuth_reference not in (None, "grid"):
                raise ValueError("apply_grid_correction=True conflicts with target_azimuth_reference")
            self.target_azimuth_reference = "grid"
        target_reference, target_reference_id, _ = _resolve_azimuth_frame(
            self.stations,
            self.target_azimuth_reference,
            self.target_azimuth_reference_id,
            allow_legacy_grid_correction=self.apply_grid_correction,
        )
        if self.tie_in and self.tie_in.azimuth_reference != "unknown":
            tie_in_frame = (self.tie_in.azimuth_reference, self.tie_in.azimuth_reference_id)
            target_frame = (target_reference, target_reference_id)
            if tie_in_frame != target_frame:
                raise ValueError(f"tie-in frame {tie_in_frame!r} does not match target trajectory frame {target_frame!r}")

        vertical_section_frame = (
            self.vertical_section_azimuth_reference,
            self.vertical_section_azimuth_reference_id,
        )
        if self.vertical_section_azimuth_deg is None:
            if vertical_section_frame != ("unknown", None):
                raise ValueError("vertical-section azimuth reference metadata requires vertical_section_azimuth_deg")
        else:
            if (
                self.vertical_section_azimuth_reference == "unknown"
                or self.vertical_section_azimuth_reference_id is None
            ):
                raise ValueError(
                    "vertical_section_azimuth_deg requires an explicit reference type and exact reference identity"
                )
            if vertical_section_frame != (target_reference, target_reference_id):
                raise ValueError(
                    f"vertical-section azimuth frame {vertical_section_frame!r} does not match target trajectory "
                    f"frame {(target_reference, target_reference_id)!r}"
                )
        return self


class SurveyStationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    md_si: float
    inclination_deg: float
    azimuth_deg: float = Field(description="azimuth used in the common output reference")
    input_azimuth_deg: float = Field(description="raw station azimuth before any explicitly declared correction")
    input_azimuth_reference: Literal["grid", "true", "magnetic", "unknown"]
    input_azimuth_reference_id: str | None
    azimuth_correction_applied_deg: float | None = Field(
        default=None,
        description="signed correction added to input_azimuth_deg, when an explicit reference conversion was declared",
    )
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
    datum: Literal["rkb", "msl", "gl", "cf", "derrick_floor", "unknown"]
    azimuth_reference: Literal["grid", "true", "magnetic"]
    azimuth_reference_id: str
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
    stations = inputs.stations
    measured_depths = [station.md_si for station in stations]
    if len(set(measured_depths)) != len(measured_depths):
        raise ValueError("duplicate measured depths in survey")
    if any(current < previous for previous, current in itertools.pairwise(measured_depths)):
        raise ValueError("measured depth must be strictly increasing in input order")
    target_reference, target_reference_id, corrected_azimuths = _resolve_azimuth_frame(
        stations,
        inputs.target_azimuth_reference,
        inputs.target_azimuth_reference_id,
        allow_legacy_grid_correction=inputs.apply_grid_correction,
    )
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []
    if inputs.datum == "unknown":
        warnings.append("TVDSS was not calculated because the vertical datum is unknown")
    elif inputs.datum == "rkb" and inputs.kb_elevation_si is None:
        warnings.append("TVDSS was not calculated because an RKB-referenced TVD requires KB elevation above MSL")
    elif inputs.datum not in {"rkb", "msl"}:
        warnings.append(
            f"TVDSS was not calculated because no MSL conversion is defined for the {inputs.datum} datum"
        )
    if target_reference == "magnetic":
        warnings.append(
            "trajectory coordinates are referenced to magnetic north; they have not been converted to grid or true north"
        )

    results: list[SurveyStationResult] = []
    tie_in = inputs.tie_in or TieInPoint()
    prev = stations[0]
    prev_inc = prev.inclination_deg * _DEG
    first_azimuth, first_correction = corrected_azimuths[0]

    tvd = tie_in.tvd_si
    ns = tie_in.ns_si
    ew = tie_in.ew_si
    doglegs: list[float] = []

    first = SurveyStationResult(
        index=0,
        md_si=prev.md_si,
        inclination_deg=prev.inclination_deg,
        azimuth_deg=first_azimuth,
        input_azimuth_deg=prev.azimuth_deg,
        input_azimuth_reference=prev.azimuth_reference,
        input_azimuth_reference_id=prev.azimuth_reference_id,
        azimuth_correction_applied_deg=first_correction,
        tvd_si=tvd,
        ns_si=ns,
        ew_si=ew,
        tvdss_si=_tvdss(tvd, inputs.datum, inputs.kb_elevation_si),
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
        azimuth_deg, correction_applied = corrected_azimuths[index]
        prev_azimuth_deg = corrected_azimuths[index - 1][0]
        azimuth = azimuth_deg * _DEG
        prev_azimuth_corrected = prev_azimuth_deg * _DEG

        dogleg_angle = _dogleg_angle(prev_inc, prev_azimuth_corrected, inclination, azimuth)
        if math.pi - dogleg_angle <= _ANTIPODAL_DOGLEG_TOLERANCE_RAD:
            raise ValueError(
                f"minimum curvature is undefined for antipodal survey directions at station {index}; "
                "an intermediate station is required to define the arc"
            )
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
        turn_rate = _signed_angle_deg(azimuth_deg - prev_azimuth_deg) * _30M_IN_M / delta_md
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
                azimuth_deg=azimuth_deg,
                input_azimuth_deg=station.azimuth_deg,
                input_azimuth_reference=station.azimuth_reference,
                input_azimuth_reference_id=station.azimuth_reference_id,
                azimuth_correction_applied_deg=correction_applied,
                tvd_si=tvd,
                ns_si=ns,
                ew_si=ew,
                tvdss_si=_tvdss(tvd, inputs.datum, inputs.kb_elevation_si),
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
        azimuth_reference=target_reference,
        azimuth_reference_id=target_reference_id,
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
            f"coordinates and closure azimuth are reported in declared {target_reference} frame {target_reference_id!r}",
            "unknown or mixed station references are rejected; signed corrections are applied only when explicitly declared",
            "no toolface or bending-moment correction applied",
        ],
    )


# --------------------------------------------------------------------------- helpers
def _dogleg_angle(i1: float, a1: float, i2: float, a2: float) -> float:
    cos_dl = math.cos(i2 - i1) - math.sin(i1) * math.sin(i2) * (1.0 - math.cos(a2 - a1))
    return math.acos(max(-1.0, min(1.0, cos_dl)))


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


def _tvdss(tvd: float, datum: str, kb_elevation_si: float | None) -> float | None:
    """Return TVDSS only when the input datum has an explicit, compatible MSL conversion."""
    if datum == "msl":
        return tvd
    if datum == "rkb" and kb_elevation_si is not None:
        # TVD is positive downward from RKB; KB elevation is positive upward from MSL.
        return tvd - kb_elevation_si
    return None


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
    """Normalise an angle difference to [-180, 180)."""
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
        "datum": {
            "value": "unknown",
            "description": "explicit vertical reference datum (rkb, msl, gl, cf, or derrick_floor); unknown cannot be reconciled with another survey",
        },
        "target_azimuth_reference": {
            "value": None,
            "description": "explicit common output reference; otherwise all station declarations must agree",
        },
        "target_azimuth_reference_id": {
            "value": None,
            "description": "identity of the output grid/projection or other reference frame",
        },
        "vertical_section_azimuth_reference": {
            "value": "required with vertical_section_azimuth_deg",
            "description": "must exactly match the target trajectory reference type",
        },
        "vertical_section_azimuth_reference_id": {
            "value": "required with vertical_section_azimuth_deg",
            "description": "must exactly match the target trajectory reference identity",
        },
        "max_reasonable_dls_deg_per_30m": {"value": MAX_REASONABLE_DLS, "unit": "deg/30m"},
        "max_station_gap_m": {"value": MAX_STATION_GAP_M, "unit": "m"},
    },
    assumptions=(
        "minimum curvature between stations",
        "constant inclination/azimuth rate within an interval",
        "station and vertical-section azimuth references must be explicit and compatible with the common output reference",
        "no azimuth-reference conversion or vertical-datum compatibility is inferred",
        "an explicit signed correction is required for conversion into a named grid frame",
    ),
    limitations=(
        "does not model bending moments, toolface history or BHA sag",
        "tight-radius and high-dogleg rotary-steerable behaviour is only as accurate as station spacing",
        "does not derive magnetic declination/grid convergence; caller-supplied corrections are not independently verified",
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
