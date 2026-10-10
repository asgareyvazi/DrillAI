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
    elif schema.get("type") == "number":
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
    assert not missing, "numeric inputs without a declared unit:\n" + "\n".join(missing)


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
