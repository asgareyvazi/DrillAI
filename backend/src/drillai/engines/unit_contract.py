"""Authoritative unit contracts for engine inputs, outputs, persistence, and display.

Numeric field names never determine units here. Units come from Pydantic quantity metadata, an
explicit per-engine output-field declaration, or a runtime unit declaration carried beside the
value (for example a constraint's ``unit`` or a sweep parameter's ``unit``). Anything else remains
explicitly unknown and is never converted.

The values and hashes written by :mod:`drillai.engines.service` are not changed by this module.
Display conversions are represented as affine coefficients in metadata and applied only by the
client when it renders a value.
"""

from __future__ import annotations

import re
import types
from dataclasses import dataclass
from typing import Annotated, Any, Union, get_args, get_origin

from pydantic import BaseModel

from drillai.units.registry import (
    Unit,
    UnitError,
    UnitSystem,
    canonical_unit,
    get_unit,
    preferred_unit,
)

UNIT_CONTRACT_VERSION = "drillai.engine-units.v1"
_UNKNOWN_REASON = "No authoritative unit is declared for this numeric value."


@dataclass(frozen=True)
class UnitSpec:
    """One explicit stored unit, optionally distinct from its registry key."""

    symbol: str
    registry_symbol: str | None = None
    semantic: str | None = None
    source: str = "engine_contract"
    requested_display_unit: str | None = None
    display_symbol: str | None = None


_COUNT = UnitSpec("1", semantic="count")
_RATIO = UnitSpec("1", semantic="ratio")


def _units(unit: str, *paths: str, semantic: str | None = None) -> dict[str, UnitSpec]:
    spec = UnitSpec(unit, semantic=semantic)
    return dict.fromkeys(paths, spec)


# Explicit output contracts for every registered engineering engine. Array paths use ``[]``;
# dictionary paths use ``*``. Dynamic unit-bearing result objects are resolved from their explicit
# sibling/input declaration at runtime, not from the field name.
_OUTPUT_FIELD_UNITS: dict[str, dict[str, UnitSpec]] = {
    "drilling.mse": {
        **_units("m2", "bit_area_si"),
        **_units("J/m3", "mse_si", "mse_axial_component_si", "mse_torsional_component_si"),
        "mse_psi": UnitSpec("psi", registry_symbol="psi-mse", display_symbol="psi"),
        **_units("1", "drilling_efficiency", "dysfunction_ratio", semantic="ratio"),
        **_units("m/h", "rop_si"),
        **_units("N/m", "wob_per_diameter_si"),
        **_units("Pa", "estimated_confined_strength_si"),
    },
    "hydraulics.laminar": {
        **_units("m3/s", "flow_rate_si"),
        **_units("L/min", "flow_rate_lpm"),
        **_units("kg/m3", "mud_weight_si", "ecd_bottom_si", "ecd_margin_si"),
        **_units("m", "bit_diameter_si", "bit_tvd_si", "nozzle_equivalent_diameter_si", "stages[].from_depth_si", "stages[].to_depth_si", "stages[].length_si", "stages[].hydraulic_diameter_si", "ecd_profile[].md_si"),
        **_units("m2", "tfa_si", "stages[].flow_area_si"),
        **_units("Pa", "bit_pressure_drop_si", "string_pressure_loss_si", "annulus_pressure_loss_si", "total_pressure_loss_si", "standpipe_pressure_expected_si", "stages[].pressure_loss_si", "ecd_profile[].annulus_pressure_loss_si"),
        **_units("W", "bit_hydraulic_horsepower_si"),
        **_units("W/m2", "hydraulic_horsepower_per_area_si"),
        **_units("m/s", "jet_velocity_si", "stages[].velocity_si", "stages[].annular_velocity_si", "slip_velocity_si", "annular_velocity_max_si", "annular_velocity_min_si", "ecd_profile[].annular_velocity_si"),
        **_units("N", "jet_impact_force_si"),
        **_units("Pa/m", "stages[].pressure_loss_gradient_si"),
        **_units("1", "stages[].reynolds_number", "cutting_transport_ratio", "cuttings_concentration_volume_fraction", semantic="ratio"),
    },
    "integrity.barrier_envelope": {
        **_units("1", "required_barriers", "verified_barrier_count", "intervals[].verified_barriers", "intervals[].shortfall", "gaps[].verified_barriers", "gaps[].shortfall", semantic="count"),
        **_units("m", "intervals[].from_depth_si", "intervals[].to_depth_si", "gaps[].from_depth_si", "gaps[].to_depth_si"),
    },
    "offsets.similarity": {
        **_units("1", "ranked[].rank", "rejected[].rank", "ranked[].score", "ranked[].coverage", "ranked[].factors[].score", "ranked[].factors[].weight", "ranked[].factors[].contribution", "rejected[].score", "rejected[].coverage", "rejected[].factors[].score", "rejected[].factors[].weight", "rejected[].factors[].contribution", "lessons_considered", "hazards[].occurrences", "hazards[].severity.*", "normalized_weights.*", semantic="count"),
        # Score/weight fields are ratios, but ranks and event totals are counts. Refine their
        # semantic labels while retaining the dimensionless unit.
        "ranked[].score": UnitSpec("1", semantic="score"),
        "ranked[].coverage": _RATIO,
        "ranked[].factors[].score": _RATIO,
        "ranked[].factors[].weight": _RATIO,
        "ranked[].factors[].contribution": _RATIO,
        "rejected[].score": UnitSpec("1", semantic="score"),
        "rejected[].coverage": _RATIO,
        "rejected[].factors[].score": _RATIO,
        "rejected[].factors[].weight": _RATIO,
        "rejected[].factors[].contribution": _RATIO,
        "normalized_weights.*": _RATIO,
        **_units("1", "ranked[].rank", "rejected[].rank", "lessons_considered", "hazards[].occurrences", "hazards[].severity.*", semantic="count"),
        **_units("1", "statistics[].count", semantic="count"),
    },
    "optimization.pareto": {
        **_units("1", "feasible_count", "infeasible_count", "ranking[].rank", "sensitivities[].samples", semantic="count"),
        **_units("1", "ranking[].score", "ranking[].objective_scores.*", "ranking[].robust_score", "sensitivities[].spearman_rho", semantic="ratio"),
        **_units("1", "feasible_ranges.*.count", semantic="count"),
        # Trade-off values inherit their physical unit from ObjectiveSpec.unit at runtime.
    },
    "optimization.sweep": {
        **_units("1", "candidate_count", "candidates[].index", semantic="count"),
        # Candidate values and parameter levels inherit their dimension from the matching
        # SweepParameter.unit declaration; a missing/invalid declaration remains unknown.
    },
    "readiness.assessment": {
        **_units("1", "ready_count", "at_risk_count", "not_ready_count", semantic="count"),
        **_units("d", "longest_lead_time_days"),
        # Item shortfall inherits the requirement's explicitly declared unit at runtime.
    },
    "schematic.well_model": {
        **_units("m", "total_depth_si", "wellhead_elevation_si", "ground_level_si", "annuli[].from_depth_si", "annuli[].to_depth_si", "annuli[].equivalent_diameter_si", "strings[].outer_diameter_si", "strings[].inner_diameter_si", "strings[].top_depth_si", "strings[].shoe_depth_si", "strings[].cement_top_si", "hole_sections[].top_depth_si", "hole_sections[].bottom_depth_si", "hole_sections[].hole_diameter_si", "completion[].top_depth_si", "completion[].bottom_depth_si", "completion[].outer_diameter_si", "completion[].inner_diameter_si", "formation_tops[].top_depth_si", "render_hints.wellhead_elevation_si", "render_hints.depth_scale.minimum", "render_hints.depth_scale.maximum"),
        **_units("m2", "annuli[].capacity_m3_per_m", "strings[].capacity_m3_per_m", "hole_sections[].capacity_m3_per_m"),
        **_units("kg/m", "strings[].weight_kg_per_m"),
        # ConstraintViolation.limit/actual inherit ConstraintViolation.unit at runtime.
    },
    "torque_drag.soft_string": {
        **_units("N", "string_weight_air_si", "string_weight_buoyed_si", "hookload_pickup_si", "hookload_slackoff_si", "hookload_rotating_si", "drag_up_si", "drag_down_si", "side_force_max_si", "tension_max_si", "stations[].tension_pickup_si", "stations[].tension_slackoff_si", "stations[].tension_rotating_si", "stations[].side_force_si", "stations[].normal_force_si"),
        **_units("1", "buoyancy_factor", semantic="ratio"),
        **_units("N.m", "surface_torque_off_bottom_si", "surface_torque_on_bottom_si", "bit_torque_si"),
        **_units("m", "side_force_max_md_si", "stretch_si", "neutral_point_md_si", "stations[].md_si", "stations[].tvd_si"),
        **_units("deg", "stations[].inclination_deg", "stations[].dogleg_deg"),
    },
    "trajectory.minimum_curvature": {
        **_units("1", "stations[].index", "survey_count", semantic="count"),
        **_units("m", "stations[].md_si", "stations[].tvd_si", "stations[].ns_si", "stations[].ew_si", "stations[].tvdss_si", "stations[].closure_distance_si", "stations[].vertical_section_si", "stations[].delta_md_si", "total_md_si", "total_tvd_si", "total_displacement_si", "max_inclination_md_si", "max_dls_md_si"),
        **_units("deg", "stations[].inclination_deg", "stations[].azimuth_deg", "stations[].input_azimuth_deg", "stations[].azimuth_correction_applied_deg", "stations[].closure_azimuth_deg", "stations[].dogleg_angle_deg", "closure_azimuth_deg", "max_inclination_deg"),
        **_units("deg/30m", "stations[].dls_deg_per_30m", "stations[].build_rate_deg_per_30m", "stations[].turn_rate_deg_per_30m", "max_dls_deg_per_30m", "average_dls_deg_per_30m"),
    },
    "tubulars.api_5c3": {
        **_units("m", "segments[].from_depth_si", "segments[].to_depth_si"),
        **_units("1", "segments[].d_over_t", "segments[].axial_derating_factor", "segments[].burst_safety_factor", "segments[].collapse_safety_factor", "segments[].tension_safety_factor", "segments[].triaxial_safety_factor", "design_factors.burst", "design_factors.collapse", "design_factors.tension", "design_factors.triaxial", "minimum_safety_factor", semantic="ratio"),
        **_units("Pa", "segments[].axial_stress_si", "segments[].equivalent_yield_stress_si", "segments[].burst_rating_si", "segments[].burst_rating_uniaxial_si", "segments[].collapse_rating_si", "segments[].collapse_rating_uniaxial_si", "segments[].triaxial_equivalent_stress_si", "segments[].net_collapse_load_si", "segments[].net_burst_load_si"),
        **_units("N", "segments[].axial_yield_force_si", "segments[].axial_load_si"),
        # Segment/root ConstraintViolation values inherit their own explicit unit at runtime.
    },
    "twin.reconciliation": {
        **_units("m", "stations[].md_si", "stations[].planned_tvd_si", "stations[].planned_ns_si", "stations[].planned_ew_si", "stations[].actual_tvd_si", "stations[].actual_ns_si", "stations[].actual_ew_si", "stations[].lateral_offset_si", "stations[].vertical_offset_si", "stations[].closure_offset_si", "max_lateral_offset_si", "max_lateral_offset_md_si", "max_vertical_offset_si", "max_vertical_offset_md_si", "mean_lateral_offset_si", "final_lateral_offset_si", "final_vertical_offset_si", "actual_total_md_si", "planned_total_md_si", "latest_actual_md_si", "tolerance_used_lateral_si", "tolerance_used_vertical_si"),
        **_units("1", "station_count", "compared_station_count", "stations_on_plan", "stations_on_watch", "stations_out_of_tolerance", "interpolated_stations", semantic="count"),
        **_units("1", "progress_fraction", semantic="ratio"),
    },
    "well_control.kill_sheet": {
        **_units("1", "circulation_cycles", "strokes_to_bit", "annulus_strokes", "total_kill_strokes", "schedule[].strokes", semantic="count"),
        **_units("kg/m3", "kill_mud_weight_si", "kill_mud_weight_increase_si", "max_allowable_mud_weight_si", "formation_pressure_equivalent_si", "mud_weight_margin_si", "kick_tolerance_mud_weight_si", "schedule[].mud_weight_si"),
        **_units("ppg", "kill_mud_weight_ppg"),
        **_units("Pa", "initial_circulating_pressure_si", "final_circulating_pressure_si", "maasp_si", "shoe_pressure_at_kick_si", "shoe_margin_si", "formation_pressure_si"),
        **_units("kPa", "initial_circulating_pressure_kpa", "final_circulating_pressure_kpa", "maasp_kpa", "schedule[].pressure_kpa"),
        **_units("m3/s", "kill_rate_si"),
        **_units("m3", "volumetric_kick_tolerance_si"),
        **_units("m", "max_allowable_influx_height_at_shoe_si"),
        **_units("s", "time_to_kill_si"),
        **_units("Pa", "schedule[].pressure_si"),
    },
    "wellbore.capacity": {
        **_units("m2", "string_capacity_si", "annulus_capacity_si", "intervals[].string_capacity_si", "intervals[].annulus_capacity_si"),
        **_units("m3", "string_volume_si", "annulus_volume_si", "total_volume_si", "bottoms_up_volume_si", "intervals[].string_volume_si", "intervals[].annulus_volume_si"),
        **_units("1", "string_strokes", "annulus_strokes", "total_strokes", "bottoms_up_strokes", semantic="count"),
        **_units("s", "bottoms_up_time_si", "circulation_time_si"),
        **_units("m", "intervals[].from_depth_si", "intervals[].to_depth_si", "intervals[].length_si"),
        **_units("m/s", "intervals[].annular_velocity_si"),
    },
}


_UNIT_LABELS = {
    "m2": "m²",
    "m3": "m³",
    "m2/s": "m²/s",
    "m3/s": "m³/s",
    "kg/m3": "kg/m³",
    "W/m2": "W/m²",
    "kW/m2": "kW/m²",
    "hp/in2": "hp/in²",
    "N.m": "N·m",
    "lbf.ft": "ft·lbf",
    "psi-mse": "psi",
}


def _unit_label(unit: Unit) -> str:
    return _UNIT_LABELS.get(unit.symbol, unit.symbol)


def _unknown_descriptor(
    *,
    source: str = "unknown",
    reason: str = _UNKNOWN_REASON,
    description: str | None = None,
    precision: int = 3,
) -> dict[str, Any]:
    display_units = {
        key: {
            "unit_symbol": None,
            "registry_symbol": None,
            "factor": None,
            "offset": None,
            "supported": False,
            "behavior": "unknown",
        }
        for key in ("si", "metric_engineering", "field_us")
    }
    return {
        "status": "unknown",
        "value_unit": None,
        "registry_unit": None,
        "dimension": None,
        "canonical_unit": None,
        "unit_system_basis": "unknown",
        "semantic": None,
        "source": source,
        "description": description,
        "description_status": "declared" if description else "not_declared",
        "unknown_reason": reason,
        "requested_display_unit": None,
        "precision": precision,
        "display_units": display_units,
    }


def _display_conversion(source_unit: Unit, target_unit: Unit) -> dict[str, Any]:
    if source_unit.dimension is not target_unit.dimension:
        return {
            "unit_symbol": _unit_label(target_unit),
            "registry_symbol": target_unit.symbol,
            "factor": None,
            "offset": None,
            "supported": False,
            "behavior": "dimension_mismatch",
        }
    if source_unit.to_canonical_fn is not None or target_unit.from_canonical_fn is not None:
        return {
            "unit_symbol": _unit_label(target_unit),
            "registry_symbol": target_unit.symbol,
            "factor": None,
            "offset": None,
            "supported": False,
            "behavior": "nonlinear",
        }
    factor = source_unit.factor / target_unit.factor
    offset = (source_unit.offset - target_unit.offset) / target_unit.factor
    return {
        "unit_symbol": _unit_label(target_unit),
        "registry_symbol": target_unit.symbol,
        "factor": factor,
        "offset": offset,
        "supported": True,
        "behavior": "affine" if source_unit.is_affine or target_unit.is_affine else "linear",
    }


def _declared_descriptor(
    spec: UnitSpec,
    *,
    source: str | None = None,
    description: str | None = None,
    precision: int = 3,
    basis: str = "explicit_unit",
) -> dict[str, Any]:
    try:
        source_unit = get_unit(spec.registry_symbol or spec.symbol)
        canonical = canonical_unit(source_unit.dimension)
        displays: dict[str, Any] = {}
        for key, unit_system in (
            ("si", UnitSystem.SI),
            ("metric_engineering", UnitSystem.METRIC_ENGINEERING),
            ("field_us", UnitSystem.FIELD_US),
        ):
            target = get_unit(preferred_unit(source_unit.dimension, unit_system))
            displays[key] = _display_conversion(source_unit, target)
    except UnitError as exc:
        return _unknown_descriptor(
            source=source or spec.source,
            reason=f"The declared unit is not registered or dimensionally usable: {exc}",
            description=description,
            precision=precision,
        )
    return {
        "status": "declared",
        "value_unit": spec.display_symbol or spec.symbol,
        "registry_unit": source_unit.symbol,
        "dimension": source_unit.dimension.value,
        "canonical_unit": canonical,
        "unit_system_basis": basis,
        "semantic": spec.semantic,
        "source": source or spec.source,
        "description": description,
        "description_status": "declared" if description else "not_declared",
        "unknown_reason": None,
        "requested_display_unit": spec.requested_display_unit,
        "precision": precision,
        "display_units": displays,
    }


def _field_unit_spec(field_info: Any) -> UnitSpec | None:
    extra = getattr(field_info, "json_schema_extra", None)
    if not isinstance(extra, dict):
        return None
    symbol = extra.get("unit")
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    return UnitSpec(symbol.strip(), source="schema")


def _field_description(field_info: Any) -> str | None:
    description = getattr(field_info, "description", None)
    return description.strip() if isinstance(description, str) and description.strip() else None


def _is_numeric_annotation(annotation: Any) -> bool:
    return annotation in (int, float)


def _iter_numeric_annotations(annotation: Any, path: str, field_info: Any, seen: set[type]) -> list[tuple[str, Any]]:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Annotated:
        return _iter_numeric_annotations(args[0], path, field_info, seen) if args else []
    if origin in (Union, types.UnionType):
        members = [arg for arg in args if arg is not type(None)]
        if any(_is_numeric_annotation(member) for member in members):
            return [(path, field_info)]
        results: list[tuple[str, Any]] = []
        for member in members:
            results.extend(_iter_numeric_annotations(member, path, field_info, seen))
        return results
    if origin in (list, tuple, set, frozenset):
        return _iter_numeric_annotations(args[0], f"{path}[]", field_info, seen) if args else []
    if origin is dict:
        return _iter_numeric_annotations(args[1], f"{path}.*", field_info, seen) if len(args) > 1 else []
    if _is_numeric_annotation(annotation):
        return [(path, field_info)]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel) and annotation not in seen:
        return _iter_model_numeric_fields(annotation, path, seen | {annotation})
    return []


def _iter_model_numeric_fields(
    model: type[BaseModel], prefix: str = "", seen: set[type] | None = None
) -> list[tuple[str, Any]]:
    visited = set(seen or ())
    visited.add(model)
    result: list[tuple[str, Any]] = []
    for name, field_info in model.model_fields.items():
        path = f"{prefix}.{name}" if prefix else name
        result.extend(_iter_numeric_annotations(field_info.annotation, path, field_info, visited))
    # A union such as ``float | str`` is numeric in the JSON response when the runtime value is a
    # number. Deduplicate those paths while preserving their declaration order.
    unique: dict[str, Any] = {}
    for path, info in result:
        unique.setdefault(path, info)
    return list(unique.items())


def _pattern_regex(pattern: str) -> re.Pattern[str]:
    escaped = re.escape(pattern)
    escaped = escaped.replace(r"\[\]", r"\[\d+\]")
    escaped = escaped.replace(r"\*", r"[^.\[\]]+")
    return re.compile(rf"^{escaped}$")


def _matches(pattern: str, path: str) -> bool:
    return bool(_pattern_regex(pattern).match(path))


def _best_pattern(mapping: dict[str, Any], path: str) -> str | None:
    matches = [pattern for pattern in mapping if pattern == path or _matches(pattern, path)]
    if not matches:
        return None
    # Prefer the most literal path; ties are deterministic and should not occur in the reviewed map.
    return max(matches, key=lambda pattern: (sum(char not in "[]*" for char in pattern), len(pattern)))


def _schema_unit_fields(
    engine_key: str,
    model: type[BaseModel],
    *,
    role: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    output_map = _OUTPUT_FIELD_UNITS.get(engine_key, {}) if role == "output" else {}
    for path, field_info in _iter_model_numeric_fields(model):
        output_pattern = _best_pattern(output_map, path)
        explicit = output_map[output_pattern] if output_pattern else None
        field_spec = _field_unit_spec(field_info)
        if explicit is not None:
            if field_spec is not None:
                declared_unit = get_unit(field_spec.symbol)
                contract_unit = get_unit(explicit.registry_symbol or explicit.symbol)
                if declared_unit.symbol != contract_unit.symbol:
                    raise ValueError(
                        f"{engine_key} output unit contract conflicts with schema at {path}: "
                        f"{declared_unit.symbol!r} vs {contract_unit.symbol!r}"
                    )
            spec = explicit
            source = "engine_contract"
        elif field_spec is not None:
            spec = field_spec
            source = "schema"
        else:
            result[path] = _unknown_descriptor(
                source="unknown",
                description=_field_description(field_info),
            )
            continue
        result[path] = _declared_descriptor(
            spec,
            source=source,
            description=_field_description(field_info),
        )
    # Dynamic ``dict`` outputs do not advertise value types in JSON Schema. Preserve their
    # explicitly declared path templates in the engine catalogue; actual values receive exact paths
    # in the persisted runtime contract.
    if role == "output":
        for pattern, spec in output_map.items():
            result.setdefault(
                pattern,
                _declared_descriptor(spec, source="engine_contract"),
            )
    return result


def schema_unit_contract(
    engine_key: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
) -> dict[str, Any]:
    """Static input/output numeric contracts included in the engine registry response."""
    return {
        "contract_version": UNIT_CONTRACT_VERSION,
        "unit_system": "SI",
        "unit_system_basis": "per_numeric_field; outputs are never relabelled from field names",
        "input_fields": _schema_unit_fields(engine_key, input_model, role="input"),
        "output_fields": _schema_unit_fields(engine_key, output_model, role="output"),
        "violation_fields": {},
        "unknown_unit_policy": "unknown values are shown without conversion; no unit is inferred",
    }


def _walk_numeric_values(value: Any, path: str = "") -> list[tuple[str, int | float, dict[str, Any] | None]]:
    found: list[tuple[str, int | float, dict[str, Any] | None]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            if isinstance(item, bool):
                continue
            if isinstance(item, (int, float)):
                found.append((child_path, item, value))
            else:
                found.extend(_walk_numeric_values(item, child_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            if isinstance(item, bool):
                continue
            if isinstance(item, (int, float)):
                found.append((child_path, item, None))
            else:
                found.extend(_walk_numeric_values(item, child_path))
    return found


def _runtime_unit_spec(symbol: Any, *, source: str, requested_display_unit: str | None = None) -> UnitSpec | None:
    if not isinstance(symbol, str) or not symbol.strip():
        return None
    normalized = symbol.strip()
    if normalized.lower() in {"count", "counts", "stroke", "strokes", "ea", "each"}:
        return UnitSpec("1", semantic="count", source=source)
    try:
        unit = get_unit(normalized)
    except UnitError:
        return None
    return UnitSpec(
        unit.symbol,
        semantic=None,
        source=source,
        requested_display_unit=requested_display_unit,
    )


def _declared_dimension_unit(display_unit: Any) -> UnitSpec | None:
    """Resolve canonical values from an explicit sibling display-unit declaration."""
    if not isinstance(display_unit, str) or not display_unit.strip():
        return None
    try:
        declared = get_unit(display_unit.strip())
        canonical = canonical_unit(declared.dimension)
    except UnitError:
        return None
    return UnitSpec(
        canonical,
        source="dynamic_result",
        requested_display_unit=declared.symbol,
    )


def unit_descriptor_from_declared_unit(
    unit: str | None,
    *,
    canonical_value: bool = False,
    source: str = "dynamic_result",
) -> dict[str, Any]:
    """Create a descriptor from an explicit unit string, or return an explicit unknown contract.

    ``canonical_value=True`` is used only when a schema says the numeric value is stored in
    canonical SI and the sibling unit declares its physical dimension (optimization sweep).
    Otherwise the number is already expressed in the declared unit.
    """
    spec = _declared_dimension_unit(unit) if canonical_value else _runtime_unit_spec(unit, source=source)
    if spec is None:
        return _unknown_descriptor(
            source=source,
            reason="The runtime unit declaration is missing or is not in the authoritative unit registry.",
        )
    return _declared_descriptor(
        spec,
        source=source,
        basis="canonical_si_from_declared_dimension" if canonical_value else "explicit_runtime_declaration",
    )


def _objective_units(inputs: dict[str, Any]) -> dict[str, str]:
    specs: dict[str, str] = {}
    objectives = inputs.get("objectives")
    if isinstance(objectives, list):
        for objective in objectives:
            if not isinstance(objective, dict):
                continue
            key, unit = objective.get("key"), objective.get("unit")
            if isinstance(key, str) and isinstance(unit, str) and unit.strip():
                specs[key] = unit.strip()
    return specs


def _parameter_units(inputs: dict[str, Any]) -> dict[str, str]:
    units = inputs.get("parameter_units")
    if not isinstance(units, dict):
        return {}
    return {
        str(name): unit.strip()
        for name, unit in units.items()
        if isinstance(name, str) and isinstance(unit, str) and unit.strip()
    }


def _sweep_units(inputs: dict[str, Any]) -> dict[str, str]:
    specs: dict[str, str] = {}
    parameters = inputs.get("parameters")
    if isinstance(parameters, list):
        for parameter in parameters:
            if not isinstance(parameter, dict):
                continue
            name, unit = parameter.get("name"), parameter.get("unit")
            if isinstance(name, str) and isinstance(unit, str) and unit.strip():
                specs[name] = unit.strip()
    return specs


def _dynamic_spec(
    engine_key: str,
    path: str,
    value: int | float,
    parent: dict[str, Any] | None,
    inputs: dict[str, Any],
) -> UnitSpec | None:
    normalized = re.sub(r"\[\d+\]", "[]", path)
    leaf = normalized.rsplit(".", 1)[-1]

    # Dynamic metric summaries and constraint values carry the unit on the same result object.
    if parent is not None and isinstance(parent.get("unit"), str) and leaf in {
        "limit",
        "actual",
        "weighted_mean",
        "median",
        "minimum",
        "maximum",
        "p10",
        "p90",
        "reference_value",
        "candidate_value",
    }:
        spec = _runtime_unit_spec(parent.get("unit"), source="dynamic_result")
        if spec is not None:
            return spec

    if engine_key == "readiness.assessment" and normalized.startswith("items[].quantity_shortfall"):
        item_id = parent.get("item_id") if parent else None
        requirements = inputs.get("requirements")
        if isinstance(requirements, list):
            for requirement in requirements:
                if isinstance(requirement, dict) and requirement.get("item_id") == item_id:
                    return _runtime_unit_spec(requirement.get("unit"), source="dynamic_result")

    if engine_key == "optimization.pareto" and normalized.startswith("trade_offs[]"):
        objective_key = parent.get("objective") if parent else None
        unit = _objective_units(inputs).get(objective_key) if isinstance(objective_key, str) else None
        return _runtime_unit_spec(unit, source="dynamic_result")

    if engine_key == "optimization.sweep":
        sweep_units = _sweep_units(inputs)
        # ``min_si``/``max_si``/``fixed`` and generated candidate values are canonical values;
        # the explicitly declared display unit supplies only their dimension.
        match = re.search(r"(?:values|parameter_levels)\.([^.[\]]+)", normalized)
        if match is None:
            # Input parameters themselves use parameters[].<field>, with the unit on that object.
            match = re.search(r"parameters\[\]\.([^.[\]]+)", normalized)
            if match and parent is not None:
                return _declared_dimension_unit(parent.get("unit"))
        elif match.group(1) in sweep_units:
            return _declared_dimension_unit(sweep_units[match.group(1)])

    if engine_key == "optimization.pareto" and normalized.startswith("feasible_ranges."):
        leaf = normalized.rsplit(".", 1)[-1]
        if leaf in {"min", "max"}:
            parameter = normalized[len("feasible_ranges.") : -(len(leaf) + 1)]
            return _declared_dimension_unit(_parameter_units(inputs).get(parameter))

    if engine_key == "optimization.pareto" and normalized.startswith("candidates[].values."):
        parameter = normalized.rsplit(".", 1)[-1]
        return _declared_dimension_unit(_parameter_units(inputs).get(parameter))

    # Pareto objective and uncertainty maps use ObjectiveSpec.unit; candidate decision values use
    # the explicit parameter_units declaration and are canonical SI.
    if engine_key == "optimization.pareto" and any(
        token in normalized for token in ("candidates[].objectives.", "candidates[].uncertainty.")
    ):
        objective_units = _objective_units(inputs)
        key = normalized.rsplit(".", 1)[-1]
        return _runtime_unit_spec(objective_units.get(key), source="dynamic_result")

    return None


def _runtime_fields(
    engine_key: str,
    values: dict[str, Any],
    *,
    role: str,
    schema_fields: dict[str, dict[str, Any]],
    inputs: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    explicit_output = _OUTPUT_FIELD_UNITS.get(engine_key, {}) if role == "output" else {}
    result: dict[str, dict[str, Any]] = {}
    for path, value, parent in _walk_numeric_values(values):
        pattern = _best_pattern(explicit_output, path)
        if pattern is not None:
            spec = explicit_output[pattern]
            result[path] = _declared_descriptor(spec, source="engine_contract")
            continue
        pattern = _best_pattern(schema_fields, path)
        if pattern is not None:
            descriptor = schema_fields[pattern]
            # Schema paths may be explicitly unknown; runtime unit declarations can refine only an
            # unknown descriptor and may never override a declared schema/engine contract.
            if descriptor.get("status") == "declared":
                result[path] = descriptor
                continue
        else:
            descriptor = None
        dynamic = _dynamic_spec(engine_key, path, value, parent, inputs)
        if dynamic is not None:
            basis = (
                "canonical_si_from_declared_dimension"
                if dynamic.requested_display_unit is not None
                else "explicit_runtime_declaration"
            )
            result[path] = _declared_descriptor(dynamic, source="dynamic_result", basis=basis)
        elif descriptor is not None:
            result[path] = descriptor
        else:
            result[path] = _unknown_descriptor()
    return result


def _legacy_unit_map(fields: dict[str, dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path, descriptor in fields.items():
        if "." not in path and "[" not in path and descriptor.get("status") == "declared":
            unit = descriptor.get("value_unit")
            if isinstance(unit, str):
                result[path] = unit
    return result


def build_engine_unit_contract(
    engine_key: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    input_values: dict[str, Any],
    output_values: dict[str, Any],
    violations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the immutable metadata payload for one live/persisted engine run."""
    schema = schema_unit_contract(engine_key, input_model, output_model)
    input_schema = schema["input_fields"]
    output_schema = schema["output_fields"]
    input_fields = _runtime_fields(
        engine_key,
        input_values,
        role="input",
        schema_fields=input_schema,
        inputs=input_values,
    )
    output_fields = _runtime_fields(
        engine_key,
        output_values,
        role="output",
        schema_fields=output_schema,
        inputs=input_values,
    )
    violation_fields = _runtime_fields(
        engine_key,
        {"violations": violations or []},
        role="output",
        schema_fields={},
        inputs=input_values,
    )
    return {
        "contract_version": UNIT_CONTRACT_VERSION,
        # Backward compatibility: legacy consumers use this to mean canonical SI input values.
        # Per-field output units below are the authoritative contract for display and reports.
        "unit_system": "SI",
        "unit_system_basis": "per_numeric_field; canonical stored values are never converted",
        "input_units": _legacy_unit_map(input_schema),
        "output_units": _legacy_unit_map(output_schema),
        "input_fields": input_fields,
        "output_fields": output_fields,
        "violation_fields": violation_fields,
        "input_schema_fields": input_schema,
        "output_schema_fields": output_schema,
        "unknown_unit_policy": "unknown values are shown without conversion; no unit is inferred",
    }


def _unknown_runtime_fields(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {path: _unknown_descriptor() for path, _value, _parent in _walk_numeric_values(values)}


def declared_unit_fields(
    values: Any,
    *,
    source: str = "dynamic_result",
) -> dict[str, dict[str, Any]]:
    """Build exact-path descriptors for dynamic JSON using only explicit sibling declarations.

    A persisted ``unit_contract`` descriptor takes precedence when it is structurally complete. A
    sibling ``unit`` is treated as the numeric value's own unit. Missing, malformed, or unregistered
    declarations remain explicitly unknown; neither the key name nor a legacy engine schema is used.
    """
    result: dict[str, dict[str, Any]] = {}
    for path, _value, parent in _walk_numeric_values(values):
        # Unit-contract metadata contains numeric factors/offsets but is not engineering payload.
        if (
            path == "unit_contract"
            or path.startswith("unit_contract.")
            or ".unit_contract." in path
            or path.endswith(".unit_contract")
        ):
            continue
        contract = parent.get("unit_contract") if parent is not None else None
        if (
            isinstance(contract, dict)
            and contract.get("status") in {"declared", "unknown"}
            and isinstance(contract.get("display_units"), dict)
            and isinstance(contract.get("value_unit"), (str, type(None)))
        ):
            result[path] = contract
            continue
        declared_unit = parent.get("unit") if parent is not None else None
        result[path] = unit_descriptor_from_declared_unit(
            declared_unit if isinstance(declared_unit, str) else None,
            source=source,
        )
    return result


def stored_unit_contract(
    units: Any,
    inputs: dict[str, Any] | None,
    outputs: dict[str, Any] | None,
    violations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a current contract or explicitly unknown metadata for a historical run.

    Historical rows are not relabelled using today's engine schema. That would manufacture
    provenance the original run did not persist.
    """
    if (
        isinstance(units, dict)
        and units.get("contract_version") == UNIT_CONTRACT_VERSION
        and isinstance(units.get("input_fields"), dict)
        and isinstance(units.get("output_fields"), dict)
    ):
        return units
    return {
        "contract_version": UNIT_CONTRACT_VERSION,
        "unit_system": "unknown",
        "unit_system_basis": "historical_run_without_authoritative_unit_contract",
        "input_units": {},
        "output_units": {},
        "input_fields": _unknown_runtime_fields(inputs or {}),
        "output_fields": _unknown_runtime_fields(outputs or {}),
        "violation_fields": _unknown_runtime_fields({"violations": violations or []}),
        "input_schema_fields": {},
        "output_schema_fields": {},
        "unknown_unit_policy": "historical values remain unconverted because their unit contract was not persisted",
    }
