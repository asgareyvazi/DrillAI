"""Workflow node registry.

Nine families, each with a distinct job — this is what keeps a workflow graph readable and keeps
LLM usage honest:

``data``        read platform data (context, wells, catalogues)
``rag``         retrieval over the corpus (lexical/semantic/metadata with engineering filters)
``engineering`` run deterministic engineering engines
``analytics``   statistics, aggregates, offset comparison
``logic``       branching, variable assignment, mapping
``human``       approval / review gates (the run suspends and is persisted)
``ai``          LLM calls behind the provider abstraction, checked by guardrails
``output``      produce artefacts: recommendations, reports, schematics, notifications
``integration`` reach outside the platform (messaging, WITSML/ETP) — always L4+

A node type declares its config schema, its inputs, whether it is terminal, and its action level.
Executors receive a :class:`NodeContext` and return a :class:`NodeOutcome`; they never touch the
workflow tables themselves — the runtime owns persistence, tracing, retries and approval
suspension. That split is what makes the runtime replaceable and the nodes testable.

Configuration values and inputs may contain ``${node_id.field.path}`` references, resolved by the
runtime against previous node outputs and run variables before the executor is called.

The canonical form is ``${node_id.<key path inside that node's outcome outputs>}`` — so a value the
engine node reports under its ``outputs`` key is written ``${trajectory.outputs.max_dls_deg_per_30m}``,
while a flat payload such as the analytics node's is ``${stats.mean}``. ``vars.<name>`` and
``inputs.<name>`` address run variables and workflow inputs; nothing else is addressable.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import UTC
from drillai.core.errors import NodeExecutionFailed, ValidationFailed
from drillai.core.logging import get_logger
from drillai.security.actions import ActionLevel, Principal

logger = get_logger(__name__)

__all__ = [
    "ApprovalSpec",
    "NodeContext",
    "NodeFamily",
    "NodeOutcome",
    "NodeSpec",
    "install_default_nodes",
    "node_spec",
    "node_types_for_ui",
    "register_node_type",
    "registered_node_types",
]


class NodeFamily(str):
    """String constants for the nine node families (kept as plain strings for JSON friendliness)."""

    DATA = "data"
    RAG = "rag"
    ENGINEERING = "engineering"
    ANALYTICS = "analytics"
    LOGIC = "logic"
    HUMAN = "human"
    AI = "ai"
    OUTPUT = "output"
    INTEGRATION = "integration"

    ALL: ClassVar[tuple[str, ...]] = (DATA, RAG, ENGINEERING, ANALYTICS, LOGIC, HUMAN, AI, OUTPUT, INTEGRATION)


class EmptyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- context & outcome


@dataclass
class ApprovalSpec:
    """A request for a human decision; the runtime persists it and suspends the run."""

    title: str
    description: str
    kind: str = "workflow_node"
    required_role: str | None = None
    action_level: ActionLevel = ActionLevel.PROPOSE
    proposed_action: dict[str, Any] = field(default_factory=dict)
    risk_notes: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    expires_in_hours: float | None = 72.0


@dataclass
class NodeContext:
    """Everything a node executor may use. Passed explicitly — no import-time globals."""

    session: AsyncSession
    org_id: str
    principal: Principal
    run_id: str
    node_id: str
    node_name: str
    inputs: dict[str, Any]
    variables: dict[str, Any]
    node_outputs: dict[str, dict[str, Any]]
    project_id: str | None = None
    well_id: str | None = None
    wellbore_id: str | None = None
    section_id: str | None = None
    operation_id: str | None = None
    loop_index: int = 0
    approval_payload: dict[str, Any] | None = None
    services: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False

    def variable(self, name: str, default: Any = None) -> Any:
        return self.variables.get(name, default)

    def output_of(self, node_id: str, path: str | None = None) -> Any:
        payload = self.node_outputs.get(node_id, {})
        if not path:
            return payload
        current: Any = payload
        for part in path.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return None
        return current


@dataclass
class NodeOutcome:
    outputs: dict[str, Any] = field(default_factory=dict)
    branch: str | None = None
    variables: dict[str, Any] = field(default_factory=dict)
    approval: ApprovalSpec | None = None
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stop: bool = False
    engine_run_id: str | None = None
    llm_call_id: str | None = None
    tool_call_id: str | None = None


NodeExecutor = Callable[[NodeContext, BaseModel], Awaitable[NodeOutcome]]


@dataclass(frozen=True)
class NodeSpec:
    key: str
    name: str
    family: str
    description: str
    executor: NodeExecutor | None = None
    config_model: type[BaseModel] = EmptyConfig
    action_level: ActionLevel = ActionLevel.OBSERVE
    terminal: bool = False
    required_inputs: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    version: str = "1.0.0"

    def describe(self, *, include_schema: bool = True) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "family": self.family,
            "description": self.description,
            "version": self.version,
            "action_level": self.action_level.value,
            "terminal": self.terminal,
            "required_inputs": list(self.required_inputs),
            "produces": list(self.produces),
            "tags": list(self.tags),
            "config_schema": self.config_model.model_json_schema() if include_schema else None,
        }


_NODES: dict[str, NodeSpec] = {}


def register_node_type(spec: NodeSpec, *, replace: bool = False) -> NodeSpec:
    if spec.key in _NODES and not replace:
        raise ValidationFailed(f"node type {spec.key!r} is already registered")
    if spec.family not in NodeFamily.ALL:
        raise ValidationFailed(f"unknown node family {spec.family!r}", details={"families": list(NodeFamily.ALL)})
    _NODES[spec.key] = spec
    return spec


def node_spec(key: str) -> NodeSpec:
    spec = _NODES.get(key)
    if spec is None:
        raise ValidationFailed(f"node type {key!r} is not registered", details={"known_types": sorted(_NODES)})
    return spec


def registered_node_types() -> dict[str, NodeSpec]:
    return dict(_NODES)


def node_types_for_ui() -> list[dict[str, Any]]:
    """Catalogue for the workflow editor palette, grouped by family."""
    return [spec.describe() for _, spec in sorted(_NODES.items(), key=lambda item: (item[1].family, item[0]))]


# --------------------------------------------------------------------------- data / rag nodes


class LoadContextConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str = Field(default="workflow", description="context purpose: prompt|agent|workflow|dashboard|rag")
    sections: list[str] | None = Field(default=None, description="None = the purpose's default sections")
    token_budget: int | None = Field(default=None, ge=64)
    include_evidence: bool = True
    max_items_per_section: int = Field(default=50, ge=1, le=500)
    render_prompt: bool = Field(default=True, description="also render the prompt payload for AI nodes")


async def _load_context(context: NodeContext, config: BaseModel) -> NodeOutcome:
    from drillai.context.builder import default_context_builder
    from drillai.context.model import ContextPurpose, ContextRequest, ContextScope

    cfg = LoadContextConfig.model_validate(config)
    scope = ContextScope(
        org_id=context.org_id,
        project_id=context.project_id,
        well_id=context.well_id,
        wellbore_id=context.wellbore_id,
        section_id=context.section_id,
        operation_id=context.operation_id,
    )
    request = ContextRequest(
        scope=scope,
        purpose=ContextPurpose(cfg.purpose),
        sections=cfg.sections,
        include_evidence=cfg.include_evidence,
        max_items_per_section=cfg.max_items_per_section,
        token_budget=cfg.token_budget,
        permissions=context.principal.permissions,
    )
    bundle = await default_context_builder().build(context.session, request)
    rendered = default_context_builder().render_prompt_payload(bundle) if cfg.render_prompt else None
    sections = [
        {"key": section.key, "items": len(section.items), "empty_reason": section.empty_reason}
        for section in bundle.sections
    ]
    return NodeOutcome(
        outputs={
            "bundle_id": bundle.bundle_id,
            "sections": sections,
            "item_count": bundle.item_count(),
            "token_estimate": bundle.token_estimate,
            "omitted_sections": bundle.omitted_sections,
            "prompt_payload": rendered,
            "summary": bundle.model_dump(mode="json", exclude={"sections"}),
        },
        variables={"context_bundle_id": bundle.bundle_id, "context_prompt": rendered},
        notes=[f"context built for purpose {cfg.purpose}"],
    )


class RagSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2)
    mode: str = Field(default="hybrid", pattern="^(metadata|lexical|semantic|hybrid)$")
    limit: int = Field(default=10, ge=1, le=50)
    doc_type: str | None = None
    chunk_kind: str | None = None
    depth_from_si: float | None = None
    depth_to_si: float | None = None
    include_neighbours: int = Field(default=0, ge=0, le=2)


async def _rag_search(context: NodeContext, config: BaseModel) -> NodeOutcome:
    from drillai.rag.retriever import RetrievalQuery, expand_neighbours, retrieve

    cfg = RagSearchConfig.model_validate(config)
    query = RetrievalQuery(
        text=cfg.query,
        mode=cfg.mode,
        limit=cfg.limit,
        well_id=context.well_id,
        wellbore_id=context.wellbore_id,
        section_id=context.section_id,
        operation_id=context.operation_id,
        doc_type=cfg.doc_type,
        chunk_kind=cfg.chunk_kind,
        depth_from_si=cfg.depth_from_si,
        depth_to_si=cfg.depth_to_si,
    )
    result = await retrieve(context.session, context.org_id, query)
    hits = result.hits
    if cfg.include_neighbours:
        hits = await expand_neighbours(context.session, hits, radius=cfg.include_neighbours)
    return NodeOutcome(
        outputs={
            "hits": [hit.model_dump() for hit in hits],
            "hit_count": len(hits),
            "mode_used": result.mode_used,
            "notes": result.notes,
            "filters": {key: str(value) for key, value in result.filters_applied.items()},
        },
        variables={"last_retrieval": {"query": cfg.query, "hits": len(hits), "mode": result.mode_used}},
        notes=result.notes,
    )


# --------------------------------------------------------------------------- engineering node


class RunEngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    engine_key: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    persist: bool = Field(default=True, description="write an EngineRun row (always true in production)")


async def _run_engine(context: NodeContext, config: BaseModel) -> NodeOutcome:
    from drillai.engines.registry import load_default_engines
    from drillai.engines.service import execute_engine

    cfg = RunEngineConfig.model_validate(config)
    merged_inputs = {**cfg.inputs, **context.inputs}

    # One auditable execution path is shared with the API and the agent tool layer, so a number
    # produced here is recorded exactly like one produced interactively (same hashes, same
    # provenance columns). ``persist=False`` is only used by dry runs and graph previews.
    if cfg.persist and not context.dry_run:
        execution = await execute_engine(
            context.session,
            engine_key=cfg.engine_key,
            inputs=merged_inputs,
            org_id=context.org_id,
            subject_kind="well" if context.well_id else "none",
            subject_id=context.well_id or context.run_id,
            project_id=context.project_id,
            well_id=context.well_id,
            wellbore_id=context.wellbore_id,
            section_id=context.section_id,
            operation_id=context.operation_id,
            input_source_kind="workflow",
            input_source_id=context.run_id,
            triggered_by="workflow",
            workflow_run_id=context.run_id,
        )
        payload = execution.to_payload()
        engine_run_id = execution.engine_run_id
        payload["limits"] = execution.limitations
        payload.pop("limitations", None)
        return NodeOutcome(
            outputs=payload,
            engine_run_id=engine_run_id,
            notes=[f"engine {execution.engine_key} v{execution.engine_version} executed"],
        )

    spec, result = load_default_engines().execute(cfg.engine_key, merged_inputs)
    return NodeOutcome(
        outputs={
            "engine_key": spec.key,
            "engine_version": spec.version,
            "result": result.outputs.model_dump(mode="json"),
            "is_feasible": result.is_feasible,
            "warnings": result.warnings,
            "violations": [violation.model_dump(mode="json") for violation in result.violations],
            "assumptions": result.assumptions_applied,
            "limits": list(spec.limitations),
            "engine_run_id": None,
            "inputs_hash": "",
            "outputs_hash": "",
        },
        notes=[f"engine {spec.key} v{spec.version} executed (not persisted: dry run)"],
    )


# --------------------------------------------------------------------------- analytics node


class SeriesStatsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: list[float] = Field(default_factory=list)
    label: str = "series"
    unit: str | None = None
    thresholds: dict[str, float] = Field(default_factory=dict, description="name -> value; flags count above/below")


async def _series_stats(context: NodeContext, config: BaseModel) -> NodeOutcome:
    cfg = SeriesStatsConfig.model_validate(config)
    values = [float(value) for value in cfg.values]
    if not values:
        return NodeOutcome(outputs={"count": 0, "label": cfg.label}, notes=["empty series"])
    ordered = sorted(values)
    stats: dict[str, Any] = {
        "count": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "min": ordered[0],
        "max": ordered[-1],
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
        "p10": _percentile(ordered, 0.10),
        "p90": _percentile(ordered, 0.90),
        "label": cfg.label,
        "unit": cfg.unit,
    }
    flags: dict[str, int] = {}
    for name, threshold in cfg.thresholds.items():
        flags[name] = sum(1 for value in values if value > threshold)
    stats["exceedance"] = flags
    return NodeOutcome(outputs=stats, notes=[f"{len(values)} values summarised"])


def _percentile(ordered: list[float], fraction: float) -> float:
    if not ordered:
        return 0.0
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


# --------------------------------------------------------------------------- logic nodes


class BranchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition: str = Field(description="expression, e.g. ${engine.outputs.is_feasible} == false")
    true_branch: str = "true"
    false_branch: str = "false"


async def _branch(context: NodeContext, config: BaseModel) -> NodeOutcome:
    from drillai.workflow.expressions import evaluate_condition

    cfg = BranchConfig.model_validate(config)
    taken = evaluate_condition(cfg.condition, context)
    return NodeOutcome(
        outputs={"condition": cfg.condition, "result": taken, "branch": cfg.true_branch if taken else cfg.false_branch},
        branch=cfg.true_branch if taken else cfg.false_branch,
    )


class SetVariablesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    variables: dict[str, Any] = Field(default_factory=dict)


async def _set_variables(context: NodeContext, config: BaseModel) -> NodeOutcome:
    cfg = SetVariablesConfig.model_validate(config)
    return NodeOutcome(outputs={"set": sorted(cfg.variables)}, variables=dict(cfg.variables))


# --------------------------------------------------------------------------- human node


class ApprovalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3)
    description: str = ""
    kind: str = "workflow_node"
    required_role: str | None = None
    risk_notes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    expires_in_hours: float | None = Field(default=72.0, gt=0)
    action_level: str = "L3"


async def _approval(context: NodeContext, config: BaseModel) -> NodeOutcome:
    cfg = ApprovalConfig.model_validate(config)
    # A decision recorded on this node already (resume) is passed in as approval_payload.
    if context.approval_payload:
        decision = context.approval_payload.get("status")
        # The decision is *data*, not a branch name. `logic.branch` picks between labelled edges; a
        # human gate must not, or the plain edge that follows it in every sane graph would be
        # evaluated as "not the matching branch" and dropped — the report after the approval would
        # silently never run. Downstream routing reads the decision from this node's outputs, either
        # through an edge condition (`${approve.decision} == 'approved'`) or not at all.
        return NodeOutcome(
            outputs={"decision": decision, "decided_by": context.approval_payload.get("decided_by")},
            stop=decision == "rejected",
        )
    return NodeOutcome(
        approval=ApprovalSpec(
            title=cfg.title,
            description=cfg.description,
            kind=cfg.kind,
            required_role=cfg.required_role,
            action_level=ActionLevel(cfg.action_level),
            risk_notes=cfg.risk_notes,
            evidence_refs=cfg.evidence_refs,
            expires_in_hours=cfg.expires_in_hours,
        ),
        outputs={"requested": True},
    )


# --------------------------------------------------------------------------- ai node


class AiCompleteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system_prompt: str = "You are a well engineering assistant. Use only the supplied context and state clearly when it is insufficient."
    user_prompt: str
    purpose: str = Field(default="generate", description="classify|extract|summarize|generate|plan|chat|code")
    provider: str | None = None
    model: str | None = None
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1)
    schema_key: str | None = Field(default=None, description="key into the workflow's output schemas")
    output_schema: dict[str, Any] | None = Field(default=None, description="inline JSON schema for structured output")
    context_variable: str = Field(default="context_prompt", description="run variable holding the grounded context")
    capture_content: bool = False


async def _ai_complete(context: NodeContext, config: BaseModel) -> NodeOutcome:
    from drillai.ai.guardrails import evaluate_output
    from drillai.ai.providers import ChatMessage, CompletionRequest
    from drillai.ai.router import LlmRouter, RoutingPolicy, TaskProfile
    from drillai.core.errors import ModelNotConfigured

    cfg = AiCompleteConfig.model_validate(config)
    policy = RoutingPolicy(
        prefer_provider=cfg.provider,
        prefer_model=cfg.model,
        require_structured_output=bool(cfg.output_schema),
    )
    router = context.services.get("llm_router") or LlmRouter(policy=policy)
    grounded_context = context.variable(cfg.context_variable) or ""
    messages = [
        ChatMessage(role="system", content=cfg.system_prompt),
        ChatMessage(role="user", content=cfg.user_prompt),
    ]
    if grounded_context:
        messages.insert(
            1,
            ChatMessage(
                role="system",
                content="GROUNDED CONTEXT (the only admissible source of engineering values):\n" + str(grounded_context),
            ),
        )
    request = CompletionRequest(
        messages=messages,
        model=cfg.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        response_schema=cfg.output_schema,
        metadata={"capture_content": cfg.capture_content},
    )
    try:
        response, decision = await router.complete(
            request,
            session=context.session,
            org_id=context.org_id,
            profile=TaskProfile(cfg.purpose),
            subject_kind="workflow_run",
            subject_id=context.run_id,
            workflow_run_id=context.run_id,
        )
    except ModelNotConfigured as exc:
        # No model configured is a *deployment* condition: fail the node with a clear message
        # rather than silently substituting fabricated text.
        raise NodeExecutionFailed(f"no LLM provider available for node {context.node_id}: {exc}") from exc

    checked, report = evaluate_output(
        text=response.text,
        structured=response.structured,
        context={"prompt": grounded_context},
        claims=None,
        citations=None,
        schema_model=None,
    )
    if report.blocked:
        raise NodeExecutionFailed(
            f"AI output for node {context.node_id} failed guardrails",
            details={"findings": [finding.model_dump() for finding in report.findings]},
        )
    return NodeOutcome(
        outputs={
            "text": response.text,
            "structured": checked,
            "model": response.model,
            "provider": response.provider,
            "usage": response.usage.model_dump(),
            "guardrails": report.to_dict(),
            "routing": decision.rationale,
        },
        notes=[f"model {response.provider}:{response.model}"],
    )


# --------------------------------------------------------------------------- output nodes


class RecommendationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3)
    statement: str
    domain: str = "drilling"
    kind: str = "operational"
    rationale: str = ""
    why_not: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    constraints_applied: list[str] = Field(default_factory=list)
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    sensitivities: dict[str, Any] = Field(default_factory=dict)
    uncertainty: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_basis: str | None = None
    data_quality: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    depth_from_md_si: float | None = None
    depth_to_md_si: float | None = None
    action_level: str = "L1"


async def _recommendation(context: NodeContext, config: BaseModel) -> NodeOutcome:
    from drillai.db.models import Recommendation

    cfg = RecommendationConfig.model_validate(config)
    engine_run_ids = [
        value
        for value in [context.node_outputs.get(node_id, {}).get("engine_run_id") for node_id in context.node_outputs]
        if value
    ]
    evidence_refs: list[str] = []
    for payload in context.node_outputs.values():
        for hit in payload.get("hits", []) if isinstance(payload.get("hits"), list) else []:
            if isinstance(hit, dict) and hit.get("citations"):
                evidence_refs.extend(str(cite) for cite in hit["citations"])
    row = Recommendation(
        org_id=context.org_id,
        project_id=context.project_id,
        well_id=context.well_id,
        wellbore_id=context.wellbore_id,
        section_id=context.section_id,
        operation_id=context.operation_id,
        domain=cfg.domain,
        kind=cfg.kind,
        title=cfg.title,
        statement=cfg.statement,
        parameters=cfg.parameters,
        rationale=cfg.rationale,
        why_not=cfg.why_not,
        assumptions=cfg.assumptions,
        constraints_applied=cfg.constraints_applied,
        alternatives=cfg.alternatives,
        sensitivities=cfg.sensitivities,
        uncertainty=cfg.uncertainty,
        confidence=cfg.confidence,
        confidence_basis=cfg.confidence_basis,
        data_quality=cfg.data_quality,
        status="draft",
        action_level=cfg.action_level,
        engine_run_ids=engine_run_ids,
        workflow_run_id=context.run_id,
        depth_from_md_si=cfg.depth_from_md_si,
        depth_to_md_si=cfg.depth_to_md_si,
        created_by=context.principal.id,
        created_by_kind=context.principal.kind,
    )
    context.session.add(row)
    await context.session.flush()
    return NodeOutcome(
        outputs={
            "recommendation_id": row.id,
            "title": cfg.title,
            "status": row.status,
            "engine_run_ids": engine_run_ids,
            "evidence_refs": sorted(set(evidence_refs)),
        },
        variables={"recommendation_id": row.id},
        artifacts=[{"kind": "recommendation", "name": cfg.title, "recommendation_id": row.id}],
        notes=["recommendation stored as draft; a human decides whether to adopt it"],
    )


class ReportConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3)
    sections: dict[str, str] = Field(
        default_factory=dict, description="heading -> markdown body (expressions allowed)"
    )
    include_context_summary: bool = True


async def _report(context: NodeContext, config: BaseModel) -> NodeOutcome:
    cfg = ReportConfig.model_validate(config)
    lines = [f"# {cfg.title}", "", f"_Run {context.run_id}, node {context.node_id}, generated {dt.datetime.now(tz=UTC).isoformat()}_", ""]
    if cfg.include_context_summary and context.variable("context_bundle_id"):
        lines += [f"Context bundle: `{context.variable('context_bundle_id')}`", ""]
    for heading, body in cfg.sections.items():
        lines += [f"## {heading}", "", str(body), ""]
    content = "\n".join(lines)
    return NodeOutcome(
        outputs={"markdown": content, "byte_size": len(content.encode()), "sections": sorted(cfg.sections)},
        artifacts=[{"kind": "report", "name": cfg.title, "content_type": "text/markdown", "payload": {"markdown": content}}],
    )


class NotifyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: str = Field(description="telegram|whatsapp|email|webhook|in_app")
    recipient: str
    subject: str | None = None
    body: str
    connector_key: str | None = None


async def _notify(context: NodeContext, config: BaseModel) -> NodeOutcome:
    from drillai.db.models import OutboundMessage

    cfg = NotifyConfig.model_validate(config)
    if context.dry_run:
        return NodeOutcome(outputs={"queued": False, "dry_run": True, "channel": cfg.channel}, notes=["dry run: nothing queued"])
    row = OutboundMessage(
        org_id=context.org_id,
        channel=cfg.channel,
        recipient=cfg.recipient,
        subject=cfg.subject,
        body=cfg.body,
        status="queued",
        trigger_kind="workflow",
        workflow_run_id=context.run_id,
        well_id=context.well_id,
        idempotency_key=f"{context.run_id}:{context.node_id}",
    )
    context.session.add(row)
    await context.session.flush()
    return NodeOutcome(
        outputs={"message_id": row.id, "channel": cfg.channel, "status": row.status},
        notes=["message queued through the connector outbox (delivery is an integration concern)"],
    )


# --------------------------------------------------------------------------- drilling nodes
#
# These nodes are what turn the platform into a drilling product rather than a generic workflow
# engine: they operate on the *drilling* domain objects through the same services the API uses, so
# a workflow can never do something the API cannot (and stays inside the same authorization and
# audit rules).


class ProcessDdrConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: str | None = Field(
        default=None,
        description=(
            "explicit document; when omitted the latest ddr document for the well in scope is used"
        ),
    )
    dry_run: bool = Field(
        default=False, description="compute the promotion plan without writing any rows"
    )


async def _process_ddr(context: NodeContext, config: BaseModel) -> NodeOutcome:
    """Promote the latest DDR into structured operations, events and twin state."""
    from drillai.db.models import Document
    from drillai.drilling.ddr import DdrProcessor

    cfg = ProcessDdrConfig.model_validate(config)
    document_id = cfg.document_id
    resolved_from = "explicit"
    if document_id is None:
        if not context.well_id:
            raise ValidationFailed(
                "process_ddr needs either an explicit document_id or a well in scope",
                details={"node_id": context.node_id},
            )
        row = (
            await context.session.execute(
                select(Document)
                .where(
                    Document.org_id == context.org_id,
                    Document.well_id == context.well_id,
                    Document.doc_type.in_(("ddr", "drilling_document")),
                )
                .order_by(Document.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if row is None:
            return NodeOutcome(
                outputs={"processed": False, "reason": "no DDR document is on file for this well"},
                notes=["nothing to promote: the well has no DDR document"],
            )
        document_id = row.id
        resolved_from = "latest_ddr_for_well"
    processor = DdrProcessor(context.session, context.org_id, actor_id=context.principal.id)
    report = await processor.process(document_id, dry_run=cfg.dry_run or context.dry_run)
    payload = report.to_dict()
    return NodeOutcome(
        outputs={
            "document_id": document_id,
            "resolved_from": resolved_from,
            "processed": True,
            "dry_run": payload["dry_run"],
            "operations_created": payload["operations_created"],
            "events_created": payload["events_created"],
            "npt_hours_classified": payload["npt_hours_classified"],
            "records_needing_review": payload["records_needing_review"],
            "not_promoted": payload["not_promoted"],
            "warnings": payload["warnings"],
        },
        notes=list(payload["warnings"])[:5],
    )


class WellStateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_missing: bool = True


async def _well_state(context: NodeContext, config: BaseModel) -> NodeOutcome:
    """Assemble the drilling state of the well in scope."""
    from drillai.drilling.state import WellStateService

    cfg = WellStateConfig.model_validate(config)
    if not context.well_id:
        raise ValidationFailed("well_state requires a well in scope", details={"node_id": context.node_id})
    state = await WellStateService(context.session, context.org_id).state(context.well_id)
    return NodeOutcome(
        outputs={
            "well": state["well"],
            "progress": state["progress"],
            "operation": state["operation"],
            "measured": state["measured"],
            "npt": state["npt"],
            "risks": state["risks"],
            "documents": state["documents"],
            "counts": state["counts"],
            "missing": state["missing"] if cfg.include_missing else [],
            "missing_count": len(state["missing"]),
        },
        notes=(
            [f"{len(state['missing'])} piece(s) of data are missing for this well"]
            if state["missing"]
            else []
        ),
    )


class NptSummaryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    basis: str = Field(default="events", pattern="^(events|operations)$")
    include_offsets: bool = True


async def _npt_summary(context: NodeContext, config: BaseModel) -> NodeOutcome:
    """NPT total, Pareto and attribution for the well in scope."""
    from drillai.drilling.npt import NptService

    cfg = NptSummaryConfig.model_validate(config)
    if not context.well_id:
        raise ValidationFailed("npt_summary requires a well in scope", details={"node_id": context.node_id})
    summary = await NptService(context.session, context.org_id).summarise(
        context.well_id, basis=cfg.basis, include_offsets=cfg.include_offsets
    )
    payload = summary.to_dict()
    return NodeOutcome(
        outputs={
            "total_hours": payload["total_hours"],
            "percent_of_well_time": payload["percent_of_well_time"],
            "event_count": payload["event_count"],
            "by_category": payload["by_category"][:8],
            "top_case": payload["cases"][0] if payload["cases"] else None,
            "controllable_hours": payload["controllable_hours"],
            "notes": payload["notes"],
        },
        notes=payload["notes"][:3],
    )


class OptimiseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parameters: list[dict[str, Any]] = Field(min_length=1)
    hydraulics_inputs: dict[str, Any]
    torque_drag_inputs: dict[str, Any] | None = None
    limits: dict[str, float] = Field(default_factory=dict)
    objectives: list[dict[str, Any]] = Field(default_factory=list)
    samples: int = Field(default=24, ge=2, le=500)
    title: str | None = None


async def _optimise(context: NodeContext, config: BaseModel) -> NodeOutcome:
    """Run the drilling parameter optimisation, persist it, and emit the recommendation id."""
    from drillai.drilling.optimisation import DrillingOptimisationService

    cfg = OptimiseConfig.model_validate(config)
    if not context.well_id:
        raise ValidationFailed("optimise requires a well in scope", details={"node_id": context.node_id})
    service = DrillingOptimisationService(context.session, context.org_id, actor_id=context.principal.id)
    result = await service.optimise(
        well_id=context.well_id,
        parameters=cfg.parameters,
        hydraulics_inputs=cfg.hydraulics_inputs,
        torque_drag_inputs=cfg.torque_drag_inputs,
        limits=cfg.limits,
        objectives=cfg.objectives,
        section_id=context.section_id,
        samples=cfg.samples,
        title=cfg.title,
    )
    explanation = result["explanation"]
    return NodeOutcome(
        outputs={
            "optimization_run_id": result["optimization_run_id"],
            "recommendation_id": result["recommendation_id"],
            "candidates_evaluated": explanation["candidates_evaluated"],
            "feasible_count": explanation["feasible_count"],
            "pareto_count": explanation["pareto_count"],
            "recommended": explanation["recommended"],
            "why_not": explanation["why_not"],
            "not_evaluated": result["not_evaluated"],
        },
        engine_run_id=(explanation.get("engine_run_ids") or [None])[0],
        notes=[
            f"{explanation['feasible_count']} of {explanation['candidates_evaluated']} candidates "
            "are feasible",
            "performance objectives (ROP, MSE) are not predicted: no model exists",
        ],
    )


class AdvisorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = "where_are_we"
    use_llm: bool = Field(
        default=False,
        description=(
            "when true the configured LLM narrates the already-computed facts; it never computes "
            "or alters a value"
        ),
    )


async def _advisor_answer(context: NodeContext, config: BaseModel) -> NodeOutcome:
    """Produce the six-section operations-advisor answer for the well in scope."""
    from drillai.drilling.advisor import ADVISOR_QUESTIONS, OperationsAdvisor

    cfg = AdvisorConfig.model_validate(config)
    if cfg.question not in ADVISOR_QUESTIONS:
        raise ValidationFailed(
            "unknown advisor question",
            details={"question": cfg.question, "known": sorted(ADVISOR_QUESTIONS)},
        )
    if not context.well_id:
        raise ValidationFailed("advisor requires a well in scope", details={"node_id": context.node_id})
    router = context.services.get("llm_router") if cfg.use_llm else None
    advisor = OperationsAdvisor(context.session, context.org_id, llm_router=router)
    answer = await advisor.ask(context.well_id, cfg.question)
    payload = answer.to_dict()
    return NodeOutcome(
        outputs={
            "question": payload["question"],
            "facts": payload["facts"],
            "calculations": payload["calculations"],
            "evidence": payload["evidence"],
            "inference": payload["inference"],
            "recommendation": payload["recommendation"],
            "unknown": payload["unknown"],
            "narrative": payload["narrative"],
            "sections": payload["sections"],
        },
        llm_call_id=None,
        notes=[
            "facts and calculations are assembled from recorded data and engine runs; the "
            "narrative cannot modify them"
        ],
    )


# --------------------------------------------------------------------------- integration node


class HttpFetchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(pattern="^https?://")
    method: str = Field(default="GET", pattern="^(GET|POST)$")
    headers: dict[str, str] = Field(default_factory=dict)
    body: dict[str, Any] | None = None
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)


async def _http_fetch(context: NodeContext, config: BaseModel) -> NodeOutcome:
    """Outbound HTTP is an L4 action: it runs only with an approval recorded on the run."""
    import httpx

    cfg = HttpFetchConfig.model_validate(config)
    if context.dry_run:
        return NodeOutcome(outputs={"dry_run": True, "url": cfg.url})
    async with httpx.AsyncClient(timeout=cfg.timeout_seconds) as client:
        response = await client.request(cfg.method, cfg.url, headers=cfg.headers, json=cfg.body)
    return NodeOutcome(
        outputs={
            "status_code": response.status_code,
            "content_type": response.headers.get("content-type"),
            "body_preview": response.text[:2000],
            "ok": response.is_success,
        },
        notes=[f"HTTP {cfg.method} {cfg.url} -> {response.status_code}"],
    )


# --------------------------------------------------------------------------- registration


def install_default_nodes() -> None:
    specs = (
        NodeSpec(
            key="data.load_context",
            name="Load engineering context",
            family=NodeFamily.DATA,
            description="Build the context bundle (well, sections, twin, documents, engine results, risks…).",
            executor=_load_context,
            config_model=LoadContextConfig,
            action_level=ActionLevel.OBSERVE,
            produces=("context.bundle",),
            tags=("context",),
        ),
        NodeSpec(
            key="rag.search",
            name="Search documents",
            family=NodeFamily.RAG,
            description="Retrieve passages with engineering filters (section, depth window, document type).",
            executor=_rag_search,
            config_model=RagSearchConfig,
            action_level=ActionLevel.OBSERVE,
            produces=("document.hits",),
            tags=("retrieval",),
        ),
        NodeSpec(
            key="engineering.run_engine",
            name="Run engineering engine",
            family=NodeFamily.ENGINEERING,
            description="Execute a deterministic engine and persist the run with inputs, outputs and violations.",
            executor=_run_engine,
            config_model=RunEngineConfig,
            action_level=ActionLevel.DRAFT,
            produces=("engine.result",),
            tags=("engineering", "deterministic"),
        ),
        NodeSpec(
            key="analytics.series_stats",
            name="Series statistics",
            family=NodeFamily.ANALYTICS,
            description="Mean/median/percentiles/standard deviation over a series with optional exceedance counts.",
            executor=_series_stats,
            config_model=SeriesStatsConfig,
            action_level=ActionLevel.OBSERVE,
            produces=("analytics.summary",),
        ),
        NodeSpec(
            key="logic.branch",
            name="Branch",
            family=NodeFamily.LOGIC,
            description="Evaluate a condition and take the matching outgoing edge.",
            executor=_branch,
            config_model=BranchConfig,
            action_level=ActionLevel.OBSERVE,
            produces=("logic.decision",),
        ),
        NodeSpec(
            key="logic.set_variables",
            name="Set variables",
            family=NodeFamily.LOGIC,
            description="Assign run variables from expressions or constants.",
            executor=_set_variables,
            config_model=SetVariablesConfig,
            action_level=ActionLevel.OBSERVE,
            produces=("logic.variables",),
        ),
        NodeSpec(
            key="human.approval",
            name="Human approval",
            family=NodeFamily.HUMAN,
            description="Suspend the run and request a decision from a named role; the decision resumes or stops the run.",
            executor=_approval,
            config_model=ApprovalConfig,
            action_level=ActionLevel.PROPOSE,
            produces=("approval.request",),
            tags=("human-in-the-loop",),
        ),
        NodeSpec(
            key="ai.complete",
            name="AI completion",
            family=NodeFamily.AI,
            description="Call the configured LLM with grounded context; output is schema-validated and guardrail-checked.",
            executor=_ai_complete,
            config_model=AiCompleteConfig,
            action_level=ActionLevel.DRAFT,
            produces=("ai.output",),
            tags=("llm",),
        ),
        NodeSpec(
            key="output.recommendation",
            name="Emit recommendation",
            family=NodeFamily.OUTPUT,
            description="Persist a recommendation with rationale, why-not, assumptions, alternatives and provenance.",
            executor=_recommendation,
            config_model=RecommendationConfig,
            action_level=ActionLevel.ADVISE,
            produces=("recommendation",),
            tags=("output",),
        ),
        NodeSpec(
            key="output.report",
            name="Build report",
            family=NodeFamily.OUTPUT,
            description="Render a markdown report from run variables and node outputs.",
            executor=_report,
            config_model=ReportConfig,
            action_level=ActionLevel.OBSERVE,
            produces=("report.document",),
            terminal=True,
        ),
        NodeSpec(
            key="output.notify",
            name="Queue notification",
            family=NodeFamily.OUTPUT,
            description="Queue a message on a channel (Telegram, WhatsApp, email, webhook) through the outbox.",
            executor=_notify,
            config_model=NotifyConfig,
            action_level=ActionLevel.EXECUTE_WITH_APPROVAL,
            produces=("message.queued",),
            terminal=True,
        ),
        NodeSpec(
            key="drilling.process_ddr",
            name="Process daily drilling report",
            family=NodeFamily.DATA,
            description=(
                "Promote the latest DDR into structured operations, NPT events, survey stations and "
                "twin state, with provenance and an explicit list of what was not promoted."
            ),
            executor=_process_ddr,
            config_model=ProcessDdrConfig,
            action_level=ActionLevel.DRAFT,
            required_inputs=("document.raw",),
            produces=("operation.state", "npt.statistics", "twin.state"),
            tags=("drilling", "ddr", "ingestion"),
        ),
        NodeSpec(
            key="drilling.well_state",
            name="Well drilling state",
            family=NodeFamily.ANALYTICS,
            description="Where are we: progress, current/previous/next operation, measured KPIs, risks, gaps.",
            executor=_well_state,
            config_model=WellStateConfig,
            action_level=ActionLevel.OBSERVE,
            required_inputs=("operation.state",),
            produces=("operation.state", "schedule.state"),
            tags=("drilling", "kpi"),
        ),
        NodeSpec(
            key="drilling.npt_summary",
            name="NPT summary and Pareto",
            family=NodeFamily.ANALYTICS,
            description="Non-productive time from recorded events, with Pareto by category and attribution.",
            executor=_npt_summary,
            config_model=NptSummaryConfig,
            action_level=ActionLevel.OBSERVE,
            required_inputs=("npt.statistics",),
            produces=("npt.statistics",),
            tags=("drilling", "npt"),
        ),
        NodeSpec(
            key="drilling.optimise",
            name="Drilling parameter optimisation",
            family=NodeFamily.ENGINEERING,
            description=(
                "Generate candidate parameters, evaluate them with the hydraulics and torque&drag "
                "engines, rank them on computed objectives and persist an explained recommendation."
            ),
            executor=_optimise,
            config_model=OptimiseConfig,
            action_level=ActionLevel.DRAFT,
            required_inputs=("drilling.parameters", "mud.properties", "wellbore.geometry"),
            produces=("optimization.candidates", "optimization.frontier", "recommendation.state"),
            tags=("drilling", "optimization"),
        ),
        NodeSpec(
            key="ai.advisor_answer",
            name="Operations advisor answer",
            family=NodeFamily.AI,
            description=(
                "Six-section answer contract: facts, calculations, evidence, inference, "
                "recommendation, unknown. Optional LLM narration cannot alter the first two."
            ),
            executor=_advisor_answer,
            config_model=AdvisorConfig,
            action_level=ActionLevel.OBSERVE,
            required_inputs=("operation.state", "document.evidence"),
            produces=("recommendation.evidence",),
            tags=("drilling", "advisor"),
        ),
        NodeSpec(
            key="integration.http_fetch",
            name="HTTP request",
            family=NodeFamily.INTEGRATION,
            description="Outbound HTTP call to an external service. Requires approval: this leaves the platform.",
            executor=_http_fetch,
            config_model=HttpFetchConfig,
            action_level=ActionLevel.EXECUTE_WITH_APPROVAL,
            produces=("integration.response",),
            tags=("integration",),
        ),
    )
    for spec in specs:
        register_node_type(spec, replace=True)


install_default_nodes()
