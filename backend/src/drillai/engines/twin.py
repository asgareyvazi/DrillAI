"""Digital well twin: deviate-and-reconcile between what was planned and what happened.

The twin distinguishes *planned*, *actual*, *current* and *predicted* state. This engine answers
the question every drilling engineer asks first: where is the well relative to the plan, and does
the deviation matter?

Both surveys are normalised with the same minimum-curvature calculation used by the trajectory
engine (one implementation, no drift between plan and actual maths). The actual position is then
sampled at the *planned* stations, so deviation is reported in the plan's own frame — which is
what a directional driller acts on. Offsets are decomposed into lateral (horizontal) and vertical
components, and each station is classified against explicit tolerances rather than a smiley face.

Interpolation between actual survey stations is linear in MD and is declared as such: without it,
a plan station between two surveys would silently borrow the nearest survey and report a
deviation that is an artefact of sampling.
"""

from __future__ import annotations

import math
from itertools import pairwise

from pydantic import BaseModel, ConfigDict, Field

from drillai.engines.contract import ConstraintViolation, EngineResult, EngineSpec, FunctionEngine
from drillai.engines.registry import register_engine
from drillai.engines.trajectory import TrajectoryEngineInput, minimum_curvature
from drillai.units.registry import quantity_field

METHOD = "plan-vs-actual deviation (minimum curvature, linear MD interpolation)"
ENGINE_VERSION = "1.1.0"

STATUS_OK = "on_plan"
STATUS_WATCH = "watch"
STATUS_VIOLATION = "out_of_tolerance"


class TrajectoryComparisonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    planned: TrajectoryEngineInput = Field(description="planned trajectory (design survey)")
    actual: TrajectoryEngineInput = Field(description="surveyed trajectory (MWD/gyro)")
    lateral_tolerance_si: float = quantity_field(
        30.0, "m", gt=0.0, description="lateral offset considered acceptable for the well objective"
    )
    vertical_tolerance_si: float = quantity_field(
        15.0, "m", gt=0.0, description="vertical (TVD) offset considered acceptable"
    )
    watch_fraction: float = quantity_field(
        0.8, "1", gt=0.0, le=1.0,
        description="fraction of the tolerance above which a station is flagged as 'watch'",
    )
    anchor_actual_to_planned_start: bool = Field(
        default=True,
        description="align the actual survey's first station with the planned first station (tie-in / KB change)",
    )
    compare_to_md_si: float | None = quantity_field(
        None, "m", gt=0.0, description="only compare planned stations at or above this MD"
    )


class StationDeviation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    md_si: float
    planned_tvd_si: float
    planned_ns_si: float
    planned_ew_si: float
    actual_tvd_si: float
    actual_ns_si: float
    actual_ew_si: float
    lateral_offset_si: float
    vertical_offset_si: float
    closure_offset_si: float
    status: str
    interpolated: bool = Field(default=False, description="actual position interpolated between surveys")


class TrackReconciliationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    method: str = METHOD
    engine_version: str = ENGINE_VERSION
    stations: list[StationDeviation]
    station_count: int
    compared_station_count: int
    max_lateral_offset_si: float
    max_lateral_offset_md_si: float
    max_vertical_offset_si: float
    max_vertical_offset_md_si: float
    mean_lateral_offset_si: float
    final_lateral_offset_si: float
    final_vertical_offset_si: float
    tracking_status: str
    stations_on_plan: int
    stations_on_watch: int
    stations_out_of_tolerance: int
    interpolated_stations: int
    actual_total_md_si: float
    planned_total_md_si: float
    progress_fraction: float = Field(description="actual MD divided by planned total MD (capped at 1)")
    latest_actual_md_si: float
    tolerance_used_lateral_si: float
    tolerance_used_vertical_si: float
    notes: list[str] = Field(default_factory=list)


def _normalised(inputs: TrajectoryEngineInput) -> list[dict[str, float]]:
    """Run minimum curvature and return plain dicts (index, md, tvd, ns, ew)."""
    result = minimum_curvature(inputs)
    if result.violations:
        raise _TrajectoryInvalidError(result.violations)
    return [
        {
            "md_si": station.md_si,
            "tvd_si": station.tvd_si,
            "ns_si": station.ns_si,
            "ew_si": station.ew_si,
        }
        for station in result.outputs.stations
    ]


class _TrajectoryInvalidError(Exception):
    """Raised when a survey cannot be evaluated (surfaced as an engine violation)."""

    def __init__(self, violations: list[ConstraintViolation]) -> None:
        self.violations = violations
        super().__init__("trajectory input is invalid")


def _interpolate(stations: list[dict[str, float]], md: float) -> tuple[dict[str, float], bool] | None:
    """Linear interpolation of position at ``md``; returns None when MD is out of range."""
    if not stations or md < stations[0]["md_si"] or md > stations[-1]["md_si"]:
        return None
    for lower, upper in pairwise(stations):
        if lower["md_si"] <= md <= upper["md_si"]:
            span = upper["md_si"] - lower["md_si"]
            if span <= 0:
                return dict(lower), md != lower["md_si"]
            weight = (md - lower["md_si"]) / span
            return (
                {
                    key: lower[key] + (upper[key] - lower[key]) * weight for key in ("tvd_si", "ns_si", "ew_si")
                },
                md not in (lower["md_si"], upper["md_si"]),
            )
    return dict(stations[-1]), False


def compute_reconciliation(inputs: TrajectoryComparisonInput) -> EngineResult:
    warnings: list[str] = []
    violations: list[ConstraintViolation] = []
    notes: list[str] = []

    try:
        planned = _normalised(inputs.planned)
        actual = _normalised(inputs.actual)
    except _TrajectoryInvalidError as exc:
        for violation in exc.violations:
            violations.append(
                violation.model_copy(
                    update={
                        "name": f"trajectory.{violation.name}",
                        "message": f"survey not evaluable: {violation.message}",
                    }
                )
            )
        return EngineResult(
            outputs=_empty_output(inputs, reason="the supplied survey could not be evaluated"),
            warnings=warnings,
            violations=violations,
            assumptions_applied=[METHOD],
        )

    if inputs.anchor_actual_to_planned_start:
        shift_ns = planned[0]["ns_si"] - actual[0]["ns_si"]
        shift_ew = planned[0]["ew_si"] - actual[0]["ew_si"]
        shift_tvd = planned[0]["tvd_si"] - actual[0]["tvd_si"]
        if any(abs(value) > 1e-9 for value in (shift_ns, shift_ew, shift_tvd)):
            notes.append(
                f"actual survey anchored to the planned first station (ΔNS {shift_ns:+.2f} m, "
                f"ΔEW {shift_ew:+.2f} m, ΔTVD {shift_tvd:+.2f} m applied)"
            )
        for station in actual:
            station["ns_si"] += shift_ns
            station["ew_si"] += shift_ew
            station["tvd_si"] += shift_tvd

    planned_limit = inputs.compare_to_md_si or planned[-1]["md_si"]
    stations: list[StationDeviation] = []
    interpolated_count = 0
    uncovered: list[float] = []
    for plan_station in planned:
        md = plan_station["md_si"]
        if md > planned_limit + 1e-9:
            continue
        sampled = _interpolate(actual, md)
        if sampled is None:
            uncovered.append(md)
            continue
        position, interpolated = sampled
        interpolated_count += 1 if interpolated else 0
        lateral = math.hypot(position["ns_si"] - plan_station["ns_si"], position["ew_si"] - plan_station["ew_si"])
        vertical = position["tvd_si"] - plan_station["tvd_si"]
        lateral_ratio = lateral / inputs.lateral_tolerance_si
        vertical_ratio = abs(vertical) / inputs.vertical_tolerance_si
        ratio = max(lateral_ratio, vertical_ratio)
        if ratio > 1.0:
            status = STATUS_VIOLATION
        elif ratio >= inputs.watch_fraction:
            status = STATUS_WATCH
        else:
            status = STATUS_OK
        stations.append(
            StationDeviation(
                md_si=md,
                planned_tvd_si=plan_station["tvd_si"],
                planned_ns_si=plan_station["ns_si"],
                planned_ew_si=plan_station["ew_si"],
                actual_tvd_si=position["tvd_si"],
                actual_ns_si=position["ns_si"],
                actual_ew_si=position["ew_si"],
                lateral_offset_si=lateral,
                vertical_offset_si=vertical,
                closure_offset_si=math.hypot(position["ns_si"] - plan_station["ns_si"], position["ew_si"] - plan_station["ew_si"]),
                status=status,
                interpolated=interpolated,
            )
        )

    if uncovered:
        warnings.append(
            "the actual survey does not cover "
            f"{len(uncovered)} planned station(s) (deepest uncovered MD {max(uncovered):.0f} m); "
            "no deviation is reported there"
        )

    if not stations:
        warnings.append("no planned station could be compared: check MD ranges and the compare_to_md limit")
        return EngineResult(
            outputs=_empty_output(inputs, reason="no comparable stations"),
            warnings=warnings,
            violations=violations,
            assumptions_applied=[METHOD],
        )

    max_lateral = max(stations, key=lambda station: station.lateral_offset_si)
    max_vertical = max(stations, key=lambda station: abs(station.vertical_offset_si))
    out_of_tolerance = [station for station in stations if station.status == STATUS_VIOLATION]
    on_watch = [station for station in stations if station.status == STATUS_WATCH]

    if out_of_tolerance:
        violations.append(
            ConstraintViolation(
                name="deviation_exceeds_tolerance",
                message=(
                    f"{len(out_of_tolerance)} station(s) exceed the tolerance; "
                    f"worst lateral {max_lateral.lateral_offset_si:.1f} m at {max_lateral.md_si:.0f} m MD"
                ),
                severity="error",
                limit=max(inputs.lateral_tolerance_si, inputs.vertical_tolerance_si),
                actual=max(max_lateral.lateral_offset_si, abs(max_vertical.vertical_offset_si)),
                unit="m",
                source="plan vs actual deviation",
            )
        )
    if on_watch:
        warnings.append(f"{len(on_watch)} station(s) are within {inputs.watch_fraction:.0%} of the tolerance")

    if out_of_tolerance:
        tracking_status = STATUS_VIOLATION
    elif on_watch:
        tracking_status = STATUS_WATCH
    else:
        tracking_status = STATUS_OK

    planned_total = inputs.planned.stations[-1].md_si
    actual_total = inputs.actual.stations[-1].md_si
    notes.extend(
        [
            f"lateral tolerance {inputs.lateral_tolerance_si:.1f} m, vertical tolerance {inputs.vertical_tolerance_si:.1f} m",
            "actual position sampled at planned stations using linear interpolation in MD",
        ]
    )
    outputs = TrackReconciliationOutput(
        stations=stations,
        station_count=len(planned),
        compared_station_count=len(stations),
        max_lateral_offset_si=max_lateral.lateral_offset_si,
        max_lateral_offset_md_si=max_lateral.md_si,
        max_vertical_offset_si=max_vertical.vertical_offset_si,
        max_vertical_offset_md_si=max_vertical.md_si,
        mean_lateral_offset_si=sum(station.lateral_offset_si for station in stations) / len(stations),
        final_lateral_offset_si=stations[-1].lateral_offset_si,
        final_vertical_offset_si=stations[-1].vertical_offset_si,
        tracking_status=tracking_status,
        stations_on_plan=sum(1 for station in stations if station.status == STATUS_OK),
        stations_on_watch=len(on_watch),
        stations_out_of_tolerance=len(out_of_tolerance),
        interpolated_stations=interpolated_count,
        actual_total_md_si=actual_total,
        planned_total_md_si=planned_total,
        progress_fraction=min(1.0, actual_total / planned_total) if planned_total > 0 else 0.0,
        latest_actual_md_si=actual_total,
        tolerance_used_lateral_si=inputs.lateral_tolerance_si,
        tolerance_used_vertical_si=inputs.vertical_tolerance_si,
        notes=notes,
    )
    assumptions = [
        METHOD,
        "both surveys are evaluated with the minimum-curvature method",
        (
            "the actual survey was anchored to the planned first station before comparison, so a "
            "tie-in or slot difference is not reported as deviation"
            if inputs.anchor_actual_to_planned_start
            else "the actual survey was used exactly as supplied: a tie-in difference appears as deviation"
        ),
        "tolerances are supplied by the caller; the defaults are indicative, not a standard",
    ]
    return EngineResult(
        outputs=outputs,
        warnings=warnings,
        violations=violations,
        assumptions_applied=assumptions,
        limitations=[
            "interpolation is linear in MD; no spline or uncertainty model is applied",
            "magnetic/grid corrections are the caller's responsibility (see the trajectory engine)",
        ],
    )


def _empty_output(inputs: TrajectoryComparisonInput, *, reason: str) -> TrackReconciliationOutput:
    planned_total = inputs.planned.stations[-1].md_si if inputs.planned.stations else 0.0
    actual_total = inputs.actual.stations[-1].md_si if inputs.actual.stations else 0.0
    return TrackReconciliationOutput(
        stations=[],
        station_count=len(inputs.planned.stations),
        compared_station_count=0,
        max_lateral_offset_si=0.0,
        max_lateral_offset_md_si=0.0,
        max_vertical_offset_si=0.0,
        max_vertical_offset_md_si=0.0,
        mean_lateral_offset_si=0.0,
        final_lateral_offset_si=0.0,
        final_vertical_offset_si=0.0,
        tracking_status="unknown",
        stations_on_plan=0,
        stations_on_watch=0,
        stations_out_of_tolerance=0,
        interpolated_stations=0,
        actual_total_md_si=actual_total,
        planned_total_md_si=planned_total,
        progress_fraction=0.0,
        latest_actual_md_si=actual_total,
        tolerance_used_lateral_si=inputs.lateral_tolerance_si,
        tolerance_used_vertical_si=inputs.vertical_tolerance_si,
        notes=[reason],
    )


TwinReconciliationSpec = EngineSpec(
    key="twin.reconciliation",
    name="Plan vs actual deviation",
    version=ENGINE_VERSION,
    domain_pack="Well Twin Pack",
    category="twin",
    summary=(
        "Compare the surveyed well path with the planned path at the planned stations: lateral and "
        "vertical offsets, tolerance classification and tracking status."
    ),
    inputs_model=TrajectoryComparisonInput,
    outputs_model=TrackReconciliationOutput,
    consumes=("trajectory.summary", "wellbore.geometry", "well.identity"),
    produces=("twin.state", "deviation.summary"),
    parameters={
        "method": {"value": METHOD, "description": "normalisation and interpolation method"},
        "lateral_tolerance_si": {"value": 30.0, "unit": "m", "description": "default lateral tolerance (override per well objective)"},
        "vertical_tolerance_si": {"value": 15.0, "unit": "m", "description": "default vertical tolerance"},
        "watch_fraction": {"value": 0.8, "description": "fraction of tolerance that raises a watch flag"},
    },
    assumptions=(
        "both surveys are processed with minimum curvature using their own tie-in",
        "actual position between survey stations is interpolated linearly in MD",
        "the actual survey is anchored to the planned first station by default",
    ),
    limitations=(
        "no positional uncertainty (ISCWSA) model",
        "no automatic detection of a wrong tie-in or datum shift beyond the anchor statement",
        "predicted rather than surveyed positions must be passed as `actual` deliberately",
    ),
    references=(
        "Minimum-curvature survey computation (same implementation as trajectory.minimum_curvature)",
        "Directional drilling practice for plan-vs-actual tolerance control",
    ),
    tags=("twin", "trajectory", "deviation", "planning"),
    action_level="L1",
)

register_engine(
    FunctionEngine(TwinReconciliationSpec, compute_reconciliation),
    aliases=("twin", "well_twin", "deviation"),
)
