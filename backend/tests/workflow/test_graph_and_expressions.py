"""Workflow graph validation, the expression language, and versioning."""

from __future__ import annotations

import pytest

from drillai.core.errors import Conflict, WorkflowDefinitionInvalid
from drillai.security.actions import ActionLevel, Principal
from drillai.workflow.expressions import (
    ExpressionError,
    UnresolvedReference,
    evaluate_condition,
    resolve_references,
)
from drillai.workflow.graph import GraphEdge, GraphNode, WorkflowGraph, topological_order, validate_graph
from drillai.workflow.nodes import node_spec, registered_node_types
from drillai.workflow.service import WorkflowService


def graph(*nodes: tuple[str, str, dict | None], edges: list[tuple[str, str, str | None, bool]] | None = None, **kwargs) -> WorkflowGraph:
    return WorkflowGraph(
        nodes=[GraphNode(id=node_id, type=node_type, config=config or {}) for node_id, node_type, config in nodes],
        edges=[
            GraphEdge(id=f"e{index}", source=source, target=target, condition=condition, is_loop_back=loop)
            for index, (source, target, condition, loop) in enumerate(edges or [], start=1)
        ],
        **kwargs,
    )


# --------------------------------------------------------------------------- expressions


class _Context:
    def __init__(self, outputs=None, variables=None, inputs=None) -> None:
        self.node_outputs = outputs or {}
        self.variables = variables or {}
        self.inputs = inputs or {}
        self.run_id = "run_1"
        self.node_id = "node_1"
        self.loop_index = 0


def test_references_resolve_through_outputs_and_variables():
    context = _Context(outputs={"engine": {"result": {"max_dls_deg_per_30m": 3.0}, "is_feasible": False}}, variables={"x": 2})
    assert evaluate_condition("${engine.is_feasible} == false", context) is True
    assert evaluate_condition("${engine.result.max_dls_deg_per_30m} > 2.5", context) is True
    assert evaluate_condition("${vars.x} == 2 and not ${engine.is_feasible}", context) is True
    assert resolve_references("DLS was ${engine.result.max_dls_deg_per_30m} deg/30m", context) == "DLS was 3.0 deg/30m"
    # A whole-string reference keeps the referenced type.
    assert resolve_references("${engine.is_feasible}", context) is False
    # A missing reference is fatal by default: silently rendering an empty value is how a report
    # ends up saying "TVD at TD:  m" and nobody notices.
    with pytest.raises(UnresolvedReference) as caught:
        resolve_references("${engine.result.nope} m", context)
    assert "max_dls_deg_per_30m" in str(caught.value)  # the message names the level that failed and what it holds
    assert resolve_references("${engine.result.nope} m", context, strict=False) == " m"


def test_condition_language_rejects_python_and_code_execution():
    context = _Context(variables={"x": 1})
    for hostile in (
        "__import__('os').system('echo pwned')",
        "${vars.x}.__class__.__mro__",
        "open('/etc/passwd').read()",
        "1; 2",
        "lambda: 1",
    ):
        with pytest.raises(ExpressionError):
            evaluate_condition(hostile, context)


def test_condition_errors_are_explicit():
    context = _Context(outputs={"a": {"value": "text"}})
    with pytest.raises(ExpressionError):
        evaluate_condition("", context)
    with pytest.raises(ExpressionError):
        evaluate_condition("${a.value} > 5", context)  # cannot compare text with a number
    assert evaluate_condition('${a.value} == "text"', context) is True
    assert evaluate_condition('"drilling" in ${vars.phase}', _Context(variables={"phase": "drilling ahead"})) is True


# --------------------------------------------------------------------------- validation


def test_valid_graph_reports_shape_and_families():
    report = validate_graph(
        graph(
            ("ctx", "data.load_context", None),
            ("engine", "engineering.run_engine", {"engine_key": "trajectory.minimum_curvature"}),
            ("rep", "output.report", {"title": "Report"}),
            edges=[("ctx", "engine", None, False), ("engine", "rep", None, False)],
        ),
        node_types=registered_node_types(),
    )
    assert report.is_valid, [issue.message for issue in report.errors]
    assert report.entry_nodes == ["ctx"]
    assert report.exit_nodes == ["rep"]
    assert report.node_count == 3 and report.edge_count == 2
    assert "data" in report.families


def test_validation_collects_every_problem_at_once():
    report = validate_graph(
        graph(
            ("a", "data.load_context", None),
            ("a", "output.report", {"title": "x"}),  # duplicate id
            ("b", "not.a.real.node", None),  # unknown type
            ("c", "engineering.run_engine", {"engine_key": "nope"}),  # unknown engine handled by the registry at run time
            ("d", "logic.branch", {"condition": "${missing.field} == 1"}),  # dangling reference
            ("x", "logic.set_variables", {}),  # disconnected cycle: unreachable
            ("y", "logic.set_variables", {}),
            edges=[("a", "b", None, False), ("x", "y", None, False), ("y", "x", None, False)],
        ),
        node_types=registered_node_types(),
    )
    codes = {issue.code for issue in report.issues}
    assert {"duplicate_node_id", "unknown_node_type", "dangling_reference", "unreachable_node"} <= codes
    assert not report.is_valid


def test_invalid_node_config_is_reported_with_the_field():
    report = validate_graph(
        graph(("bad", "logic.branch", {})),  # condition is required
        node_types=registered_node_types(),
    )
    assert any(issue.code == "invalid_node_config" and "condition" in issue.message for issue in report.issues)


def test_cycles_are_detected_and_loop_back_edges_are_honoured():
    cyclic = graph(
        ("a", "logic.set_variables", {}),
        ("b", "logic.set_variables", {}),
        edges=[("a", "b", None, False), ("b", "a", None, False)],
    )
    report = validate_graph(cyclic, node_types=registered_node_types())
    assert any(issue.code == "cycle_detected" for issue in report.issues)
    with pytest.raises(WorkflowDefinitionInvalid):
        topological_order(cyclic)

    looped = graph(
        ("a", "logic.set_variables", {}),
        ("b", "logic.branch", {"condition": "true"}),
        edges=[("a", "b", None, False), ("b", "a", None, True)],
    )
    report = validate_graph(looped, node_types=registered_node_types())
    assert not any(issue.code == "cycle_detected" for issue in report.issues)
    assert topological_order(looped) == ["a", "b"]


def test_graph_hash_is_content_addressed():
    first = graph(("a", "data.load_context", {}), ("b", "output.report", {"title": "Report"}), edges=[("a", "b", None, False)])
    second = graph(("a", "data.load_context", {}), ("b", "output.report", {"title": "Report"}), edges=[("a", "b", None, False)])
    third = graph(("a", "data.load_context", {}), ("b", "output.report", {"title": "U"}), edges=[("a", "b", None, False)])
    assert first.hash() == second.hash()
    assert first.hash() != third.hash()


# --------------------------------------------------------------------------- node catalogue


def test_every_node_family_is_represented_and_documented():
    specs = registered_node_types()
    families = {spec.family for spec in specs.values()}
    assert families == {"data", "rag", "engineering", "analytics", "logic", "human", "ai", "output", "integration"}
    for spec in specs.values():
        assert spec.description and spec.name
        assert spec.config_model is not None
        describe = spec.describe()
        assert "config_schema" in describe
    # every node that can act on the outside world must not be L0-L3
    assert node_spec("integration.http_fetch").action_level.rank >= ActionLevel.EXECUTE_WITH_APPROVAL.rank
    assert node_spec("output.notify").action_level.rank >= ActionLevel.EXECUTE_WITH_APPROVAL.rank
    # engineering work is deterministic and non-destructive
    assert node_spec("engineering.run_engine").family == "engineering"


def test_registering_an_unknown_family_is_refused():
    from drillai.core.errors import ValidationFailed
    from drillai.workflow.nodes import NodeSpec, register_node_type

    with pytest.raises(ValidationFailed):
        register_node_type(NodeSpec(key="weird", name="Weird", family="nonsense", description="nope"))


# --------------------------------------------------------------------------- versioning


@pytest.fixture
def principal() -> Principal:
    return Principal(
        id="usr_author",
        kind="user",
        permissions=frozenset({"workflow.*"}),
        max_action_level=ActionLevel.DRAFT,
    )


async def test_versioning_is_immutable_and_hash_deduplicated(session, principal):
    service = WorkflowService(session, org_id="org_test", principal=principal)
    definition = graph(("a", "data.load_context", {}), ("b", "output.report", {"title": "Report"}), edges=[("a", "b", None, False)])
    workflow = await service.create(key="daily-review", name="Daily review", graph=definition)
    first = await service.get_version(workflow.id)
    assert first.version == 1

    # Saving an unchanged graph does not mint a new version: version numbers must mean something.
    again = await service.save_version(workflow.id, definition)
    assert again.id == first.id

    edited = definition.clone_with(description="adds a branch note")
    second = await service.save_version(workflow.id, edited, notes="clarify")
    assert second.version == 2
    assert second.graph_hash != first.graph_hash
    assert (await service.get_version(workflow.id, 1)).graph_hash == first.graph_hash

    published = await service.publish(workflow.id, version=1)
    assert published.published_at is not None
    await session.flush()
    refreshed = await session.get(type(workflow), workflow.id)
    assert refreshed.status == "published"
    assert (await service.get_version(workflow.id)).id == published.id


async def test_duplicate_keys_are_rejected_and_forking_is_traceable(session, principal):
    service = WorkflowService(session, org_id="org_test", principal=principal)
    definition = graph(("a", "data.load_context", {}), ("b", "output.report", {"title": "Report"}), edges=[("a", "b", None, False)])
    source = await service.create(key="template-flow", name="Template flow", graph=definition, is_template=True)
    with pytest.raises(Conflict):
        await service.create(key="template-flow", name="Duplicate")

    forked = await service.fork(source.id, key="template-flow-copy", name="My flow")
    assert forked.forked_from_id == source.id
    assert forked.current_version == 1
    assert (await service.get_graph(forked.id)).hash() == definition.hash()
    # the fork is independent: editing it does not touch the source
    await service.save_version(forked.id, definition.clone_with(description="mine"))
    assert (await service.get_graph(source.id)).description is None
    assert (await service.get_graph(forked.id)).description == "mine"


async def test_publishing_an_invalid_graph_is_refused(session, principal):
    service = WorkflowService(session, org_id="org_test", principal=principal)
    broken = graph(("a", "data.load_context", {}), ("b", "not.registered", {}), edges=[("a", "b", None, False)])
    workflow = await service.create(key="broken-flow", name="Broken", graph=broken)
    with pytest.raises(WorkflowDefinitionInvalid):
        await service.publish(workflow.id)
    report = await service.validate(broken)
    assert not report.is_valid
