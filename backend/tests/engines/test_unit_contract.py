"""Authoritative unit-contract coverage for engine schemas and runtime values.

These tests validate software metadata and display conversions only. They are not field validation,
engineering acceptance, or authorization to operate.
"""

from __future__ import annotations

from typing import Any

from drillai.engines.unit_contract import (
    UNIT_CONTRACT_VERSION,
    build_engine_unit_contract,
    declared_unit_fields,
    schema_unit_contract,
    stored_unit_contract,
    unit_descriptor_from_declared_unit,
)
from drillai.units.registry import Dimension, UnitSystem, convert, get_unit, preferred_unit


def _descriptor(contract: dict[str, Any], path: str, section: str = "output_fields") -> dict[str, Any]:
    return contract[section][path]


def test_added_units_keep_pressure_mse_and_force_per_length_dimensions_distinct() -> None:
    assert get_unit("psi").dimension is Dimension.PRESSURE
    assert get_unit("psi-mse").dimension is Dimension.SPECIFIC_ENERGY
    assert get_unit("J/m3").dimension is Dimension.SPECIFIC_ENERGY
    assert preferred_unit(Dimension.SPECIFIC_ENERGY, UnitSystem.FIELD_US) == "psi-mse"
    assert get_unit("N/m").dimension is Dimension.FORCE_PER_LENGTH
    assert get_unit("klbf/ft").dimension is Dimension.FORCE_PER_LENGTH
    assert convert(1.0, "psi-mse", "J/m3") == convert(1.0, "psi", "Pa")


def test_all_fourteen_engines_have_schema_contracts_and_explicit_unknown_policy(engines) -> None:
    assert len(engines.keys()) == 14
    for engine in engines:
        spec = engine.spec
        contract = schema_unit_contract(spec.key, spec.inputs_model, spec.outputs_model)
        assert contract["contract_version"] == UNIT_CONTRACT_VERSION
        assert contract["unknown_unit_policy"]
        assert contract["input_fields"]
        assert contract["output_fields"]
        for side in ("input_fields", "output_fields"):
            for path, descriptor in contract[side].items():
                assert descriptor["status"] in {"declared", "unknown"}, (spec.key, path)
                if descriptor["status"] == "unknown":
                    assert descriptor["value_unit"] is None, (spec.key, path)
                    assert descriptor["unknown_reason"], (spec.key, path)
                    assert not any(item["supported"] for item in descriptor["display_units"].values())
                else:
                    assert descriptor["value_unit"], (spec.key, path)
                    assert descriptor["canonical_unit"], (spec.key, path)


def test_sweep_units_follow_explicit_parameter_declaration_and_canonical_storage(engines) -> None:
    spec = engines.get("optimization.sweep").spec
    inputs = {
        "parameters": [
            {
                "name": "flow_rate_si",
                "unit": "L/min",
                "min_si": 0.03,
                "max_si": 0.06,
                "steps": 2,
            }
        ],
        "method": "grid",
        "samples": 2,
    }
    outputs = {
        "method": "grid",
        "candidates": [
            {"candidate_id": "c0", "values": {"flow_rate_si": 0.03}, "index": 0}
        ],
        "candidate_count": 1,
        "parameter_levels": {"flow_rate_si": [0.03, 0.06]},
        "truncated": False,
        "notes": [],
    }
    contract = build_engine_unit_contract(
        spec.key, spec.inputs_model, spec.outputs_model, inputs, outputs
    )
    for path in ("parameters[0].min_si", "parameters[0].max_si"):
        descriptor = _descriptor(contract, path, "input_fields")
        assert descriptor["status"] == "declared"
        assert descriptor["value_unit"] == "m3/s"
        assert descriptor["requested_display_unit"] == "L/min"
        assert descriptor["unit_system_basis"] == "canonical_si_from_declared_dimension"
    candidate = _descriptor(contract, "candidates[0].values.flow_rate_si")
    level = _descriptor(contract, "parameter_levels.flow_rate_si[0]")
    assert candidate["value_unit"] == level["value_unit"] == "m3/s"
    assert candidate["display_units"]["field_us"]["unit_symbol"] == "gpm"

    inputs["parameters"][0]["unit"] = "unregistered-flow-unit"
    unknown = build_engine_unit_contract(
        spec.key, spec.inputs_model, spec.outputs_model, inputs, outputs
    )
    assert _descriptor(unknown, "candidates[0].values.flow_rate_si")["status"] == "unknown"
    assert _descriptor(unknown, "candidates[0].values.flow_rate_si")["value_unit"] is None


def test_pareto_dynamic_outputs_use_objective_and_parameter_units_only_when_declared(engines) -> None:
    spec = engines.get("optimization.pareto").spec
    inputs = {
        "candidates": [
            {
                "candidate_id": "c0",
                "values": {"flow_rate_si": 0.0000630901964},
                "objectives": {"ecd_margin_si": 15.0},
                "uncertainty": {},
                "violations": [],
                "feasible": True,
            }
        ],
        "objectives": [{"key": "ecd_margin_si", "sense": "maximize", "unit": "kg/m3"}],
        "parameter_units": {"flow_rate_si": "m3/s"},
    }
    outputs = {
        "feasible_count": 1,
        "infeasible_count": 0,
        "pareto_frontier": ["c0"],
        "ranking": [
            {
                "candidate_id": "c0",
                "rank": 1,
                "score": 1.0,
                "objective_scores": {"ecd_margin_si": 1.0},
                "on_frontier": True,
                "dominated_by": [],
                "robust_score": None,
            }
        ],
        "trade_offs": [
            {
                "objective": "ecd_margin_si",
                "sense": "maximize",
                "best_candidate_id": "c0",
                "best_value": 15.0,
                "worst_value": 12.0,
                "spread": 3.0,
            }
        ],
        "sensitivities": [
            {
                "parameter": "flow_rate_si",
                "objective": "ecd_margin_si",
                "spearman_rho": 0.5,
                "samples": 3,
                "interpretation": "synthetic test vector",
            }
        ],
        "feasible_ranges": {"flow_rate_si": {"min": 0.000063, "max": 0.000126, "count": 3}},
        "explanation": [],
        "notes": [],
    }
    contract = build_engine_unit_contract(
        spec.key, spec.inputs_model, spec.outputs_model, inputs, outputs
    )
    assert _descriptor(contract, "candidates[0].values.flow_rate_si", "input_fields")["value_unit"] == "m3/s"
    assert _descriptor(contract, "candidates[0].objectives.ecd_margin_si", "input_fields")["value_unit"] == "kg/m3"
    assert _descriptor(contract, "trade_offs[0].best_value")["value_unit"] == "kg/m3"
    assert _descriptor(contract, "feasible_ranges.flow_rate_si.min")["value_unit"] == "m3/s"
    assert _descriptor(contract, "ranking[0].objective_scores.ecd_margin_si")["dimension"] == "dimensionless"
    assert _descriptor(contract, "sensitivities[0].samples")["semantic"] == "count"

    inputs["parameter_units"] = {}
    inputs["objectives"][0].pop("unit")
    unknown = build_engine_unit_contract(
        spec.key, spec.inputs_model, spec.outputs_model, inputs, outputs
    )
    assert _descriptor(unknown, "candidates[0].values.flow_rate_si", "input_fields")["status"] == "unknown"
    assert _descriptor(unknown, "trade_offs[0].best_value")["status"] == "unknown"


def test_constraint_violations_use_their_explicit_sibling_unit_and_historical_units_stay_unknown(engines) -> None:
    spec = engines.get("tubulars.api_5c3").spec
    inputs: dict[str, Any] = {}
    output: dict[str, Any] = {}
    violation = {"name": "burst", "message": "test", "limit": 10.0, "actual": 12.0, "unit": "Pa"}
    current = build_engine_unit_contract(
        spec.key, spec.inputs_model, spec.outputs_model, inputs, output, [violation]
    )
    assert _descriptor(current, "violations[0].limit", "violation_fields")["value_unit"] == "Pa"
    assert _descriptor(current, "violations[0].actual", "violation_fields")["value_unit"] == "Pa"

    violation["unit"] = "unknown-unit-token"
    unknown = build_engine_unit_contract(
        spec.key, spec.inputs_model, spec.outputs_model, inputs, output, [violation]
    )
    assert _descriptor(unknown, "violations[0].limit", "violation_fields")["status"] == "unknown"

    legacy = stored_unit_contract(
        None,
        {"depth_si": 100.0},
        {"pressure_si": 1_000.0},
        [violation],
    )
    assert legacy["unit_system"] == "unknown"
    assert legacy["input_fields"]["depth_si"]["status"] == "unknown"
    assert legacy["output_fields"]["pressure_si"]["status"] == "unknown"
    assert legacy["violation_fields"]["violations[0].limit"]["status"] == "unknown"


def test_dynamic_recommendation_parameter_units_are_explicit_or_unknown() -> None:
    persisted = unit_descriptor_from_declared_unit("kg/m3", source="recommendation_parameter")
    fields = declared_unit_fields(
        {
            "result": [
                {"name": "mud_weight", "value": 1_200.0, "unit_contract": persisted},
                {"name": "unlabelled", "value": 4.0},
                {"name": "unregistered", "value": 5.0, "unit": "not-a-registry-unit"},
            ]
        },
        source="recommendation_parameter",
    )

    assert fields["result[0].value"]["status"] == "declared"
    assert fields["result[0].value"]["value_unit"] == "kg/m3"
    assert fields["result[1].value"]["status"] == "unknown"
    assert fields["result[2].value"]["status"] == "unknown"
    assert not any("unit_contract" in path for path in fields)
