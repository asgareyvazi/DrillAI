"""Engine registry and discovery.

Engines self-register through a decorator, so adding a new calculation is an additive
change in its own domain-pack module — no core file has to change. The registry is the
single lookup used by the API (``/engines``), workflow nodes, agent tools and optimizers.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from pydantic import BaseModel, ValidationError

from drillai.core.errors import EngineInputInvalid, EngineNotFound
from drillai.engines.contract import Engine, EngineResult, EngineSpec, FunctionEngine


class EngineRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, Engine] = {}
        self._aliases: dict[str, str] = {}

    # ------------------------------------------------------------------ registration
    def register(self, engine: Engine, *, aliases: Iterable[str] = ()) -> Engine:
        key = engine.spec.key
        if key in self._engines:
            raise ValueError(f"engine already registered: {key}")
        self._engines[key] = engine
        for alias in aliases:
            self._aliases[alias] = key
        return engine

    def register_function(
        self,
        spec: EngineSpec,
        fn,
        *,
        aliases: Iterable[str] = (),
    ) -> Engine:
        return self.register(FunctionEngine(spec, fn), aliases=aliases)

    # ------------------------------------------------------------------ lookup
    def get(self, key: str) -> Engine:
        resolved = self._aliases.get(key, key)
        try:
            return self._engines[resolved]
        except KeyError as exc:
            raise EngineNotFound(
                f"engine {key!r} is not registered", details={"available": sorted(self._engines)[:20]}
            ) from exc

    def has(self, key: str) -> bool:
        return self._aliases.get(key, key) in self._engines

    def keys(self) -> list[str]:
        return sorted(self._engines)

    def all(self) -> list[Engine]:
        return [self._engines[key] for key in self.keys()]

    def by_category(self, category: str) -> list[Engine]:
        return [engine for engine in self.all() if engine.spec.category == category]

    def by_domain_pack(self, domain_pack: str) -> list[Engine]:
        return [engine for engine in self.all() if engine.spec.domain_pack == domain_pack]

    def by_produced_port(self, port: str) -> list[Engine]:
        return [engine for engine in self.all() if port in engine.spec.produces]

    def describe_all(self) -> list[dict[str, Any]]:
        return [engine.spec.describe() for engine in self.all()]

    def summary(self) -> list[dict[str, Any]]:
        return [
            {
                "key": engine.spec.key,
                "name": engine.spec.name,
                "version": engine.spec.version,
                "category": engine.spec.category,
                "domain_pack": engine.spec.domain_pack,
                "summary": engine.spec.summary,
                "consumes": list(engine.spec.consumes),
                "produces": list(engine.spec.produces),
                "action_level": engine.spec.action_level,
                "validation_status": engine.spec.validation_status,
                "tags": list(engine.spec.tags),
            }
            for engine in self.all()
        ]

    def __iter__(self) -> Iterator[Engine]:
        return iter(self.all())

    def __len__(self) -> int:
        return len(self._engines)

    # ------------------------------------------------------------------ execution
    def execute(self, key: str, inputs: BaseModel | dict[str, Any]) -> tuple[EngineSpec, EngineResult]:
        engine = self.get(key)
        if isinstance(inputs, BaseModel):
            payload = inputs
        else:
            try:
                payload = engine.spec.inputs_model.model_validate(inputs)
            except ValidationError as exc:
                raise EngineInputInvalid(
                    f"invalid inputs for engine {key!r}",
                    details={"engine": key, "errors": exc.errors(include_url=False)},
                ) from exc
        result = engine.run(payload)
        return engine.spec, result


#: Process-wide registry. Domain-pack modules register into it at import time.
ENGINE_REGISTRY = EngineRegistry()


def register_engine(engine: Engine, *, aliases: Iterable[str] = ()) -> Engine:
    return ENGINE_REGISTRY.register(engine, aliases=aliases)


#: Domain-pack modules that register the shipped engines. Adding an engine is a new module
#: plus one line here (or a plugin entry point) — no core file changes.
ENGINE_MODULES = (
    "trajectory",
    "fluids",
    "mechanics",
    "wellcontrol",
    "tubulars",
    "integrity",
    "schematic",
    "offsets",
    "readiness",
    "twin",
    "optimization",
)


def load_default_engines() -> EngineRegistry:
    """Import the shipped domain-pack modules so their engines register (idempotent).

    Import failures are *not* swallowed: a broken engine module must fail loudly at startup
    rather than silently disappear from the registry.
    """
    import importlib

    for module_name in ENGINE_MODULES:
        importlib.import_module(f"drillai.engines.{module_name}")
    return ENGINE_REGISTRY
