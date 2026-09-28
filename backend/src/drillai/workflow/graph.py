"""Workflow definition model and validation.

A workflow is a directed graph of typed nodes. The model is deliberately small and strict:
everything the runtime needs to be deterministic (ordering, looping, error handling, human
gates, retries) is *data* in the graph, not implicit behaviour in code.

Validation returns structured issues instead of raising on the first problem, because the
editor shows all of them at once. Publish requires zero errors (warnings are allowed but are
surfaced in the UI and stored on the version).

The graph hash is the identity of a published version: a run records both the version id and
the hash, so a run can always be reproduced even if a version row is later renamed.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict, deque
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from drillai.core.errors import WorkflowDefinitionInvalid
from drillai.core.serialization import content_hash

__all__ = [
    "GraphEdge",
    "GraphIssue",
    "GraphNode",
    "ValidationReport",
    "WorkflowGraph",
    "topological_order",
]

NodeErrorPolicy = Literal["fail", "continue", "retry", "branch"]


class GraphNode(BaseModel):
    """One step. ``type`` must exist in the node registry; ``config`` is validated by it."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    type: str = Field(description="node type key, e.g. engine.run")
    name: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="mapping of node input name -> expression/constant; expressions may reference "
        "other node outputs with ${node_id.field}",
    )
    on_error: NodeErrorPolicy = "fail"
    retry: dict[str, Any] = Field(default_factory=dict, description="{max_attempts, backoff_seconds}")
    timeout_seconds: float | None = Field(default=None, gt=0)
    is_breakpoint: bool = False
    notes: str | None = None
    position: dict[str, float] | None = Field(default=None, description="editor coordinates")
    action_level: str | None = Field(default=None, description="override; defaults to the node type's level")


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    source: str
    target: str
    source_handle: str | None = None
    target_handle: str | None = None
    condition: str | None = Field(default=None, description="optional expression; taken when truthy")
    label: str | None = None
    is_loop_back: bool = False


class GraphIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    severity: Literal["error", "warning"] = "error"
    node_id: str | None = None
    edge_id: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[GraphIssue] = Field(default_factory=list)
    node_count: int = 0
    edge_count: int = 0
    entry_nodes: list[str] = Field(default_factory=list)
    exit_nodes: list[str] = Field(default_factory=list)
    node_types: dict[str, int] = Field(default_factory=dict)
    families: dict[str, int] = Field(default_factory=dict)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_valid(self) -> bool:
        """Derived, never stored: the graph is valid exactly when it has no error-severity issue.

        It is a computed field rather than a property so that every place a report is serialised --
        the validate endpoint and the version rows alike -- publishes the same shape, and a client
        never has to re-derive validity from the issue list.
        """
        return not self.errors

    @property
    def errors(self) -> list[GraphIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[GraphIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]


class WorkflowGraph(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    variables: dict[str, Any] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    version: int = 1
    description: str | None = None

    # ------------------------------------------------------------------ shape
    def node_map(self) -> dict[str, GraphNode]:
        return {node.id: node for node in self.nodes}

    def outgoing(self, node_id: str) -> list[GraphEdge]:
        return [edge for edge in self.edges if edge.source == node_id]

    def incoming(self, node_id: str) -> list[GraphEdge]:
        return [edge for edge in self.edges if edge.target == node_id]

    def entry_nodes(self) -> list[str]:
        targets = {edge.target for edge in self.edges if not edge.is_loop_back}
        return [node.id for node in self.nodes if node.id not in targets]

    def exit_nodes(self) -> list[str]:
        sources = {edge.source for edge in self.edges if not edge.is_loop_back}
        return [node.id for node in self.nodes if node.id not in sources]

    def hash(self) -> str:
        return content_hash(self.model_dump(mode="json", exclude_none=True))

    def clone_with(self, **overrides: Any) -> WorkflowGraph:
        data = self.model_dump()
        data.update(overrides)
        return WorkflowGraph.model_validate(data)


def topological_order(graph: WorkflowGraph) -> list[str]:
    """Kahn ordering over non-loop edges; raises when the graph has a cycle."""
    indegree: dict[str, int] = {node.id: 0 for node in graph.nodes}
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge.is_loop_back:
            continue
        if edge.source not in indegree or edge.target not in indegree:
            continue
        indegree[edge.target] += 1
        adjacency[edge.source].append(edge.target)
    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    order: list[str] = []
    while queue:
        current = queue.popleft()
        order.append(current)
        for successor in sorted(adjacency[current]):
            indegree[successor] -= 1
            if indegree[successor] == 0:
                queue.append(successor)
    if len(order) != len(graph.nodes):
        raise WorkflowDefinitionInvalid(
            "workflow graph contains a cycle",
            details={"processed": order, "nodes": [node.id for node in graph.nodes]},
        )
    return order


def validate_graph(
    graph: WorkflowGraph,
    *,
    node_types: dict[str, Any],
    strict: bool = True,
) -> ValidationReport:
    """Validate structure, node types, config and wiring.

    ``node_types`` maps a node type key to a spec object exposing ``family``,
    ``config_model``, ``required_permissions`` and ``inputs``/``outputs``.
    """
    issues: list[GraphIssue] = []
    ids = [node.id for node in graph.nodes]
    duplicates = {node_id for node_id in ids if ids.count(node_id) > 1}
    for node_id in sorted(duplicates):
        issues.append(GraphIssue(code="duplicate_node_id", message=f"node id {node_id!r} is used more than once", node_id=node_id))

    if not graph.nodes:
        issues.append(GraphIssue(code="empty_graph", message="a workflow needs at least one node"))

    known = set(ids)
    families: dict[str, int] = defaultdict(int)
    type_counts: dict[str, int] = defaultdict(int)
    for node in graph.nodes:
        spec = node_types.get(node.type)
        if spec is None:
            issues.append(
                GraphIssue(
                    code="unknown_node_type",
                    message=f"node type {node.type!r} is not registered",
                    node_id=node.id,
                    details={"known_types": sorted(node_types)},
                )
            )
            continue
        type_counts[node.type] += 1
        families[getattr(spec, "family", "unknown")] += 1
        config_model = getattr(spec, "config_model", None)
        if config_model is not None:
            try:
                config_model.model_validate(node.config)
            except Exception as exc:
                issues.append(
                    GraphIssue(
                        code="invalid_node_config",
                        message=f"{node.id}: {exc}",
                        node_id=node.id,
                        details={"node_type": node.type},
                    )
                )
        required = getattr(node, "required_inputs", None)
        if required:
            missing = [name for name in required if name not in node.inputs and name not in node.config]
            if missing:
                issues.append(
                    GraphIssue(
                        code="missing_node_inputs",
                        message=f"{node.id}: missing inputs {', '.join(missing)}",
                        node_id=node.id,
                    )
                )
        # References may appear in a node's inputs *or* inside its config (a report body, a
        # condition): both are scanned, otherwise a typo in a config reference only fails at run
        # time, after earlier nodes have already executed.
        for source_name, values in (("inputs", node.inputs), ("config", node.config)):
            for expression in _expressions(values):
                referenced = _referenced_node(expression)
                if referenced and referenced not in known:
                    issues.append(
                        GraphIssue(
                            code="dangling_reference",
                            message=f"{node.id} references unknown node {referenced!r} in {source_name} {expression!r}",
                            node_id=node.id,
                        )
                    )
        if node.action_level is not None and node.action_level not in {"L0", "L1", "L2", "L3", "L4", "L5"}:
            issues.append(
                GraphIssue(
                    code="invalid_action_level",
                    message=f"{node.id}: action_level {node.action_level!r} is not L0–L5",
                    node_id=node.id,
                )
            )
        if node.retry and not node.retry.get("max_attempts"):
            issues.append(
                GraphIssue(
                    code="invalid_retry",
                    message=f"{node.id}: retry requires max_attempts",
                    node_id=node.id,
                    severity="warning",
                )
            )

    for edge in graph.edges:
        if edge.source not in known:
            issues.append(
                GraphIssue(code="unknown_edge_source", message=f"edge source {edge.source!r} is not a node", edge_id=edge.id)
            )
        if edge.target not in known:
            issues.append(
                GraphIssue(code="unknown_edge_target", message=f"edge target {edge.target!r} is not a node", edge_id=edge.id)
            )
        if edge.source == edge.target and not edge.is_loop_back:
            issues.append(
                GraphIssue(
                    code="self_loop",
                    message=f"edge {edge.source} → {edge.target} is a self-loop; mark it as a loop-back edge",
                    edge_id=edge.id,
                )
            )

    entries = graph.entry_nodes()
    if graph.nodes and not entries:
        issues.append(GraphIssue(code="no_entry_node", message="every node has an incoming edge: the graph is a cycle"))

    reachable: set[str] = set()
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        adjacency[edge.source].append(edge.target)
    queue = deque(entries)
    while queue:
        current = queue.popleft()
        if current in reachable:
            continue
        reachable.add(current)
        for successor in adjacency.get(current, []):
            queue.append(successor)
    unreachable = sorted(set(ids) - reachable)
    for node_id in unreachable:
        issues.append(
            GraphIssue(
                code="unreachable_node",
                message=f"{node_id} is not reachable from any entry node",
                node_id=node_id,
                severity="error" if strict else "warning",
            )
        )

    cycle_free = True
    try:
        topological_order(graph)
    except WorkflowDefinitionInvalid:
        cycle_free = False
        issues.append(
            GraphIssue(
                code="cycle_detected",
                message="the graph contains a cycle; loops must be expressed with loop-back edges",
            )
        )

    if not any((getattr(node_types.get(node.type), "terminal", False)) for node in graph.nodes if node.type in node_types):
        issues.append(
            GraphIssue(
                code="no_terminal_node",
                message="the workflow has no output/terminal node: nothing will be produced",
                severity="warning",
            )
        )
    if not cycle_free and graph.settings.get("allow_cycles"):
        issues[-1].severity = "warning"

    return ValidationReport(
        issues=issues,
        node_count=len(graph.nodes),
        edge_count=len(graph.edges),
        entry_nodes=entries,
        exit_nodes=graph.exit_nodes(),
        node_types=dict(type_counts),
        families=dict(families),
    )


def _expressions(values: dict[str, Any]) -> list[str]:
    found: list[str] = []
    for value in values.values():
        if isinstance(value, str) and "${" in value:
            found.append(value)
        elif isinstance(value, dict):
            found.extend(_expressions(value))
        elif isinstance(value, list):
            found.extend(_expressions(item) for item in value if isinstance(item, str) and "${" in item)
    return found


def _referenced_node(expression: str) -> str | None:
    """Extract the node id from ``${node_id.field.path}``; ignores ``${vars.x}``."""
    start = expression.find("${")
    end = expression.find("}", start)
    if start < 0 or end < 0:
        return None
    path = expression[start + 2 : end]
    head = path.split(".", 1)[0]
    return None if head in {"vars", "run", "scope", "inputs"} else head


def graph_summary(graph: WorkflowGraph, report: ValidationReport | None = None) -> dict[str, Any]:
    """Compact, editor-friendly summary (used by the API and by chat→graph responses)."""
    return {
        "nodes": len(graph.nodes),
        "edges": len(graph.edges),
        "variables": sorted(graph.variables),
        "entry_nodes": report.entry_nodes if report else graph.entry_nodes(),
        "exit_nodes": report.exit_nodes if report else graph.exit_nodes(),
        "families": dict(report.families) if report else {},
        "node_types": dict(report.node_types) if report else {},
        "hash": graph.hash(),
        "generated_at": dt.datetime.now(tz=dt.UTC).isoformat(),
    }
