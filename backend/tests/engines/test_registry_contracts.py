"""Registry, contract and metadata guarantees.

These tests protect the property that makes the platform extensible: any engine, from any
domain pack, is discoverable, self-describing (schemas + units + limitations) and executable
through one uniform contract.
"""

from __future__ import annotations

import pytest

from drillai.core.errors import EngineInputInvalid, EngineNotFound
from drillai.engines.contract import PORT_TYPES, EngineResult, EngineSpec
from drillai.engines.mechanics import MSE_SPEC
from drillai.engines.registry import EngineRegistry


def test_aliases_resolve_to_the_same_engine(engines):
    assert engines.get("trajectory") is engines.get("trajectory.minimum_curvature")
    assert engines.get("min_curvature") is engines.get("trajectory.minimum_curvature")
    assert engines.get("capacity") is engines.get("wellbore.capacity")
    assert engines.get("circulation") is engines.get("hydraulics.laminar")
    assert engines.get("tnd") is engines.get("torque_drag.soft_string")
    assert engines.get("mse") is engines.get("drilling.mse")
    assert engines.get("api_5c3") is engines.get("tubulars.api_5c3")


def test_unknown_engine_raises_engine_not_found(engines):
    with pytest.raises(EngineNotFound) as excinfo:
        engines.get("does.not.exist")
    assert "does.not.exist" in str(excinfo.value)


def test_registry_rejects_duplicate_keys(empty_registry: EngineRegistry):
    empty_registry.register_function(MSE_SPEC, lambda inputs: EngineResult(outputs=inputs))
    with pytest.raises(ValueError, match="already registered"):
        empty_registry.register_function(MSE_SPEC, lambda inputs: EngineResult(outputs=inputs))


def test_lookup_helpers_partition_the_registry(engines):
    assert engines.by_category("mechanics"), "mechanics category should contain the MSE/T&D engines"
    assert "trajectory.minimum_curvature" in {engine.spec.key for engine in engines.by_domain_pack("Directional Pack")}
    assert engines.by_produced_port("trajectory.stations")
    assert engines.by_category("no-such-category") == []


def test_every_engine_is_self_describing(engines):
    descriptions = engines.describe_all()
    assert len(descriptions) == len(engines)
    for description in descriptions:
        assert description["summary"], description["key"]
        assert description["input_schema"]["properties"], description["key"]
        assert description["output_schema"]["properties"], description["key"]
        assert description["assumptions"], f"{description['key']} must declare its assumptions"
        assert description["limitations"], f"{description['key']} must declare its limitations"
        assert description["action_level"] in {"L0", "L1", "L2", "L3", "L4", "L5"}


def test_every_engine_publishes_explicit_numeric_output_unit_status(engines):
    """All 14 output schemas enumerate every numeric leaf as declared or explicitly unknown."""
    for engine in engines:
        description = engine.spec.describe()
        contract = description["unit_contract"]
        assert contract["contract_version"] == "drillai.engine-units.v1"
        assert contract["unknown_unit_policy"]

        schema = engine.spec.outputs_model.model_json_schema()
        definitions = schema.get("$defs", {})
        schema_paths: set[str] = set()
        for name, definition in schema.get("properties", {}).items():
            schema_paths.update(path for path, _unit in _numeric_leaves(definition, definitions, name))
        output_fields = contract["output_fields"]
        assert schema_paths <= set(output_fields), (
            f"{engine.spec.key} is missing numeric schema paths from its output unit contract: "
            f"{sorted(schema_paths - set(output_fields))}"
        )
        for path, descriptor in output_fields.items():
            assert descriptor["status"] in {"declared", "unknown"}, (engine.spec.key, path)
            if descriptor["status"] == "unknown":
                assert descriptor["value_unit"] is None, (engine.spec.key, path)
                assert descriptor["unknown_reason"], (engine.spec.key, path)
                assert not any(
                    conversion["supported"]
                    for conversion in descriptor["display_units"].values()
                ), (engine.spec.key, path)
            else:
                assert descriptor["value_unit"], (engine.spec.key, path)
                assert descriptor["canonical_unit"], (engine.spec.key, path)


def _numeric_leaves(schema: dict, defs: dict, path: str = "", inherited_unit: str | None = None):
    """Yield (json-path, unit) for every numeric leaf of a JSON schema.

    Pydantic flattens ``json_schema_extra`` onto the field, so an optional ``float | None``
    declaration carries its unit on the parent schema rather than inside the ``anyOf``
    branch; the unit is therefore inherited while descending.
    """
    unit = schema.get("unit") or inherited_unit
    for keyword in ("anyOf", "oneOf", "allOf"):
        for branch in schema.get(keyword, []):
            yield from _numeric_leaves(branch, defs, path, unit)
    reference = schema.get("$ref")
    if reference:
        name = reference.rsplit("/", 1)[-1]
        yield from _numeric_leaves(defs.get(name, {}), defs, path, unit)
    if "properties" in schema:
        for name, definition in schema["properties"].items():
            yield from _numeric_leaves(definition, defs, f"{path}.{name}" if path else name, unit)
    elif "items" in schema:
        yield from _numeric_leaves(schema["items"], defs, f"{path}[]", unit)
    elif schema.get("type") in {"number", "integer"}:
        yield path, unit


def test_every_numeric_input_declares_its_unit(engines):
    """Units must travel with the schema: the API, workflow inspector and UI read one source.

    This walks nested models (arrays, sub-objects and $refs), so a new field deep inside a
    geometry model cannot silently enter the platform unit-less.
    """
    missing: list[str] = []
    checked = 0
    for engine in engines:
        schema = engine.spec.inputs_model.model_json_schema()
        defs = schema.get("$defs", {})
        for name, definition in schema.get("properties", {}).items():
            for path, unit in _numeric_leaves(definition, defs, name):
                checked += 1
                if not unit:
                    missing.append(f"{engine.spec.key}: {path}")
    assert checked > 60, f"unit coverage regressed (only {checked} numeric fields seen)"
    dynamic_sibling_units = {
        "optimization.sweep: parameters[].min_si",
        "optimization.sweep: parameters[].max_si",
        "optimization.sweep: parameters[].fixed",
    }
    assert set(missing) == dynamic_sibling_units, (
        "only the sweep bounds/fixed value may use their explicit sibling `unit`; all other numeric "
        "inputs must declare their unit in the schema:\n" + "\n".join(missing)
    )
    sweep_schema = next(
        engine.spec.inputs_model.model_json_schema()
        for engine in engines
        if engine.spec.key == "optimization.sweep"
    )
    parameter_schema = sweep_schema["$defs"]["SweepParameter"]["properties"]
    assert "display unit" in parameter_schema["unit"]["description"]
    for field in ("min_si", "max_si", "fixed"):
        assert "canonical" in parameter_schema[field]["description"]
        assert "sibling unit" in parameter_schema[field]["description"]


def test_consumes_and_produces_use_known_ports(engines):
    for engine in engines:
        for port in (*engine.spec.consumes, *engine.spec.produces):
            assert port in PORT_TYPES, f"{engine.spec.key} references unknown port {port}"


def test_port_types_are_unique_and_described():
    assert len(PORT_TYPES) == len(set(PORT_TYPES))
    for port in PORT_TYPES:
        assert port, "empty port name"


def test_execute_wraps_schema_failures_in_a_domain_error(engines):
    with pytest.raises(EngineInputInvalid) as excinfo:
        engines.execute("trajectory.minimum_curvature", {"stations": []})
    assert excinfo.value.details["engine"] == "trajectory.minimum_curvature"
    assert excinfo.value.details["errors"], "field-level errors must survive for the API to return"
    assert excinfo.value.details["errors"][0]["loc"] == ("stations",)


def test_execute_returns_spec_and_result(engines):
    spec, result = engines.execute("drilling.mse", {
        "wob_si": 50_000.0,
        "torque_si": 3_000.0,
        "rpm_si": 120.0,
        "rop_si": 20.0,
        "bit_diameter_si": 0.2159,
    })
    assert isinstance(spec, EngineSpec)
    assert isinstance(result, EngineResult)
    assert result.is_feasible


def test_registration_is_the_only_extension_point(empty_registry: EngineRegistry):
    """A new engine becomes available with no change to the registry implementation."""
    calls: list[str] = []

    def custom(inputs):
        calls.append("run")
        return EngineResult(outputs=inputs)

    spec = MSE_SPEC.model_copy(update={"key": "custom.engine", "name": "Custom", "version": "0.1.0"})
    engine = register_engine_custom(empty_registry, spec, custom)
    assert empty_registry.get("custom.engine") is engine
    empty_registry.execute("custom.engine", {"wob_si": 1.0, "torque_si": 1.0, "rpm_si": 1.0, "rop_si": 1.0, "bit_diameter_si": 1.0})
    assert calls == ["run"]


def register_engine_custom(registry: EngineRegistry, spec: EngineSpec, fn):
    from drillai.engines.contract import FunctionEngine

    return registry.register(FunctionEngine(spec, fn))


def test_registration_rejects_namespace_collisions_atomically(empty_registry: EngineRegistry):
    """Neither aliases nor later canonical keys may redirect a registered calculation."""
    fn = lambda inputs: EngineResult(outputs=inputs)  # noqa: E731
    first = empty_registry.register_function(MSE_SPEC, fn, aliases=("mse-short",))
    second = MSE_SPEC.model_copy(update={"key": "other.engine"})

    for aliases in (("safe", "mse-short"), ("drilling.mse",), ("other.engine",), ("repeat", "repeat")):
        with pytest.raises(ValueError, match="alias already registered"):
            empty_registry.register_function(second, fn, aliases=iter(aliases))
        assert empty_registry.keys() == ["drilling.mse"]
        assert empty_registry.get("mse-short") is first
        assert not empty_registry.has("safe")
        assert not empty_registry.has("other.engine")

    canonical_collision = MSE_SPEC.model_copy(update={"key": "mse-short"})
    with pytest.raises(ValueError, match="already registered"):
        empty_registry.register_function(canonical_collision, fn)
    assert empty_registry.keys() == ["drilling.mse"]
    assert empty_registry.get("mse-short") is first
    added = empty_registry.register_function(second, fn, aliases=("other-short",))
    assert empty_registry.get("other-short") is added
    assert empty_registry.get("drilling.mse") is first


def test_registry_rejects_nonfinite_engine_inputs_before_and_after_validation(engines):
    """Neither nested floats, strings coerced to floats nor constructed models are safe inputs."""
    from drillai.engines.mechanics import MseEngineInput

    base = {"wob_si": 42000.0, "torque_si": 2650.0, "rpm_si": 96.0, "rop_si": 18.0, "bit_diameter_si": 0.2032}
    for value in (float("inf"), float("-inf"), float("nan"), "Infinity", "NaN"):
        with pytest.raises(EngineInputInvalid) as exc:
            engines.execute("drilling.mse", {**base, "wob_si": value})
        # The error must itself be strict-JSON serializable, without invalid raw values.
        import json

        json.dumps(exc.value.to_payload(), allow_nan=False)
    constructed = MseEngineInput.model_construct(**{**base, "wob_si": float("inf")})
    with pytest.raises(EngineInputInvalid, match="non-finite"):
        engines.execute("drilling.mse", constructed)
    nested = {
        "stations": [
            {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
            {"md_si": float("inf"), "inclination_deg": 1.0, "azimuth_deg": 0.0},
        ]
    }
    with pytest.raises(EngineInputInvalid, match="non-finite"):
        engines.execute("trajectory.minimum_curvature", nested)

    from drillai.core.errors import EngineConvergenceError

    # This real engine overflows in ordinary floating-point multiplication despite finite inputs.
    with pytest.raises(EngineConvergenceError, match="non-finite"):
        engines.execute("drilling.mse", {**base, "wob_si": 1e308})


def test_registry_rejects_nonfinite_output_even_from_finite_inputs(empty_registry: EngineRegistry):
    from pydantic import BaseModel

    from drillai.core.errors import EngineConvergenceError

    class NestedOutput(BaseModel):
        nested: dict[str, list[float]]

    def bad_result(inputs):
        return EngineResult(outputs=NestedOutput(nested={"value": [float("nan")]}))

    empty_registry.register_function(MSE_SPEC, bad_result)
    with pytest.raises(EngineConvergenceError, match="non-finite"):
        empty_registry.execute(
            "drilling.mse",
            {"wob_si": 42000.0, "torque_si": 2650.0, "rpm_si": 96.0, "rop_si": 18.0, "bit_diameter_si": 0.2032},
        )


def test_generated_roadmap_detects_registry_version_or_source_drift():
    """The canonical table is generated, and edited declarations cannot pass the guard."""
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location("engine_roadmap_guard", root / "scripts/verify_engine_roadmap.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.source_sha256(b"one\ntwo\n") == module.source_sha256(b"one\r\ntwo\r\n")
    assert module.source_sha256(b"one\ntwo\n") != module.source_sha256(b"one\nthree\n")
    current = module.ROADMAP.read_text(encoding="utf-8")
    assert module.replace_table(current) == current
    mutated = current.replace("| `drilling.mse` | 1.1.1 |", "| `drilling.mse` | 0.0.0 |")
    assert mutated != current
    assert module.replace_table(mutated) == current
    import pytest
    with pytest.raises(ValueError):
        module.replace_table(current.replace(module.END, ""))
