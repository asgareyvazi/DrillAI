"""Shared fixtures for engine tests.

The engine registry is process-wide and registration is intentionally idempotent at the
``load_default_engines`` level, so it is loaded once per test session.
"""

from __future__ import annotations

import pytest

from drillai.engines.registry import ENGINE_REGISTRY, load_default_engines

#: Engines that must exist for the platform's core drilling workflows to run.
REQUIRED_ENGINE_KEYS = {
    "trajectory.minimum_curvature",
    "wellbore.capacity",
    "hydraulics.laminar",
    "torque_drag.soft_string",
    "drilling.mse",
    "well_control.kill_sheet",
    "tubulars.api_5c3",
    "integrity.barrier_envelope",
    "schematic.well_model",
    "offsets.similarity",
    "readiness.assessment",
    "twin.reconciliation",
    "optimization.sweep",
    "optimization.pareto",
}


@pytest.fixture(scope="session")
def engines():
    registry = load_default_engines()
    missing = REQUIRED_ENGINE_KEYS - set(registry.keys())
    if missing:
        pytest.fail(f"missing required engines: {sorted(missing)}")
    assert set(registry.keys()) == REQUIRED_ENGINE_KEYS, (
        "the registry must expose exactly the shipped engines; update REQUIRED_ENGINE_KEYS when adding one"
    )
    yield registry
    assert registry is ENGINE_REGISTRY


@pytest.fixture
def empty_registry():
    from drillai.engines.registry import EngineRegistry

    return EngineRegistry()
