"""Engineering dependency graph: what goes stale when an input changes.

Engines already declare the context ports they consume and produce
(:data:`drillai.engines.contract.PORT_TYPES`). This module turns those declarations into a graph
and answers the practical question a drilling engineer asks after changing one number:

    *"I changed the mud weight — which results are now stale?"*

    mud_properties
      → hydraulics.laminar (hydraulics.profile)
      → twin.reconciliation?  no — it does not consume mud
      → optimization.sweep (through the problem definition)

Nothing here is hard-coded per engine: the graph is derived from the registry, so a new engine
participates automatically, which is the whole point of declaring ports.

Only engines already **run against this well** are reported as stale; the rest are listed as
"would be affected", because a calculation that was never run cannot be stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.errors import ValidationFailed
from drillai.db.models import EngineRun
from drillai.engines.contract import PORT_TYPES
from drillai.engines.registry import EngineRegistry, load_default_engines


def _port_names(value: Any) -> list[str]:
    """Engine specs expose ports as names or as ``PortSpec`` objects depending on the declaration
    path; normalise both so the graph never has to care."""
    names: list[str] = []
    for port in value or []:
        name = port if isinstance(port, str) else getattr(port, "name", None)
        if name:
            names.append(name)
    return names


@dataclass
class EngineNode:
    key: str
    version: str
    category: str
    consumes: list[str]
    produces: list[str]
    action_level: str = "L1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "version": self.version,
            "category": self.category,
            "consumes": self.consumes,
            "produces": self.produces,
            "action_level": self.action_level,
        }


@dataclass
class StaleImpact:
    port: str
    affected_engines: list[str] = field(default_factory=list)
    stale_produced_ports: list[str] = field(default_factory=list)
    ran_before: list[str] = field(default_factory=list)
    never_run: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "port": self.port,
            "affected_engines": self.affected_engines,
            "stale_produced_ports": self.stale_produced_ports,
            "ran_before": self.ran_before,
            "never_run": self.never_run,
            "recalculation_needed": self.ran_before,
        }


class DependencyGraphService:
    """Port-level dependency graph over the engine registry."""

    def __init__(
        self,
        session: AsyncSession | None,
        org_id: str,
        *,
        registry: EngineRegistry | None = None,
    ) -> None:
        self.session = session
        self.org_id = org_id
        self.registry = registry or load_default_engines()

    # ------------------------------------------------------------------ graph

    def nodes(self) -> list[EngineNode]:
        result: list[EngineNode] = []
        for key in sorted(self.registry.keys()):
            spec = self.registry.get(key).spec
            result.append(
                EngineNode(
                    key=spec.key,
                    version=getattr(spec, "version", "unknown"),
                    category=getattr(spec, "category", ""),
                    consumes=_port_names(getattr(spec, "consumes", None)),
                    produces=_port_names(getattr(spec, "produces", None)),
                    action_level=str(getattr(spec, "action_level", "L1")),
                )
            )
        return result

    def graph(self) -> dict[str, Any]:
        nodes = self.nodes()
        producers: dict[str, list[str]] = {}
        consumers: dict[str, list[str]] = {}
        for node in nodes:
            for port in node.produces:
                producers.setdefault(port, []).append(node.key)
            for port in node.consumes:
                consumers.setdefault(port, []).append(node.key)
        edges: list[dict[str, str]] = []
        for port, producer_keys in producers.items():
            for producer_key in producer_keys:
                for consumer_key in consumers.get(port, []):
                    if consumer_key == producer_key:
                        continue
                    edges.append({"from": producer_key, "to": consumer_key, "port": port})
        unused_ports = sorted(set(PORT_TYPES) - set(producers) - set(consumers))
        return {
            "nodes": [node.to_dict() for node in nodes],
            "edges": edges,
            "ports": {
                "known": len(PORT_TYPES),
                "produced": sorted(producers),
                "consumed": sorted(consumers),
                "declared_but_unused": unused_ports,
            },
            "producers": {port: sorted(keys) for port, keys in sorted(producers.items())},
            "source": "engine registry port declarations (no hard-coded wiring)",
        }

    def downstream(self, port: str, *, _seen: set[str] | None = None) -> list[str]:
        """Engines that consume ``port``, transitively through their own produced ports."""
        seen = _seen if _seen is not None else set()
        direct: list[str] = []
        for node in self.nodes():
            if port in node.consumes and node.key not in seen:
                seen.add(node.key)
                direct.append(node.key)
        result = list(direct)
        for key in direct:
            spec = self.registry.get(key).spec
            for produced in _port_names(getattr(spec, "produces", None)):
                result.extend(self.downstream(produced, _seen=seen))
        # preserve order, drop duplicates
        ordered: list[str] = []
        for key in result:
            if key not in ordered:
                ordered.append(key)
        return ordered

    # ------------------------------------------------------------------ impact

    async def impact(self, well_id: str, changed_ports: list[str]) -> dict[str, Any]:
        """Which engines go stale when the given ports change, for one well."""
        if self.session is None:
            raise ValidationFailed(
                "impact analysis needs a database session; use graph() for the static topology"
            )
        unknown_ports = [port for port in changed_ports if port not in PORT_TYPES]
        ran = (
            await self.session.execute(
                select(EngineRun.engine_key, EngineRun.id, EngineRun.finished_at)
                .where(EngineRun.org_id == self.org_id, EngineRun.well_id == well_id)
                .order_by(EngineRun.created_at.desc())
            )
        ).all()
        ran_keys = {row[0] for row in ran}
        impacts: list[StaleImpact] = []
        stale_ports: set[str] = set()
        for port in changed_ports:
            if port in unknown_ports:
                continue
            affected = self.downstream(port)
            produced: list[str] = []
            for key in affected:
                spec = self.registry.get(key).spec
                for produced_port in _port_names(getattr(spec, "produces", None)):
                    if produced_port not in produced:
                        produced.append(produced_port)
            stale_ports.update(produced)
            impacts.append(
                StaleImpact(
                    port=port,
                    affected_engines=affected,
                    stale_produced_ports=produced,
                    ran_before=sorted(key for key in affected if key in ran_keys),
                    never_run=sorted(key for key in affected if key not in ran_keys),
                )
            )
        recommendations_stale = bool({"recommendation.state", "recommendation.evidence"} & stale_ports)
        return {
            "well_id": well_id,
            "changed_ports": changed_ports,
            "unknown_ports": unknown_ports,
            "impacts": [item.to_dict() for item in impacts],
            "stale_ports": sorted(stale_ports),
            "recalculation_candidates": sorted(
                {key for item in impacts for key in item.ran_before}
            ),
            "engines_never_run": sorted({key for item in impacts for key in item.never_run}),
            "downstream_of_stale": sorted(
                {
                    key
                    for port in stale_ports
                    for key in self.downstream(port)
                }
            ),
            "recommendations_stale": recommendations_stale,
            "note": (
                "only engines that have already run against this well are reported as stale; the "
                "others are listed as affected-but-not-yet-run"
            ),
            "unknown_port_note": (
                f"{len(unknown_ports)} port(s) are not declared in the engine contract"
                if unknown_ports
                else None
            ),
        }
