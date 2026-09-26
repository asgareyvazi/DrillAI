"""AI layer records: model calls, agent runs, tool calls, prompts, knowledge sources,
conversations and human feedback.

Two principles are encoded structurally:

1. **The LLM is a replaceable component.** ``LlmCall`` records provider, model, routing
   reason, tokens, latency and cost — never a hard-coded provider identity elsewhere.
2. **No unobservable AI.** Every model call, tool call and agent step is a row that can be
   joined with the OTel-aligned span table, the workflow run and the recommendation it
   contributed to. Prompt/response content is captured **only** when configured
   (``llm_capture_content``), because DDRs contain commercially sensitive data.

Tool and agent *definitions* live in the registry (``registry.py``); these tables record
*executions*.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import Boolean, Float, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from drillai.db.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    IntType,
    JsonType,
    Money,
    OrgScopedMixin,
    TextType,
    TimestampMixin,
    UtcDateTime,
)

LLM_OPERATIONS = ("chat", "completion", "embedding", "vision", "speech", "rerank", "tool_call")
AGENT_RUN_STATUSES = ("queued", "running", "succeeded", "failed", "paused", "waiting_approval", "cancelled")
TOOL_CALL_STATUSES = ("requested", "approved", "running", "succeeded", "failed", "denied", "timeout", "skipped")
MESSAGE_ROLES = ("system", "user", "assistant", "tool")
KNOWLEDGE_SOURCE_KINDS = (
    "document_library",
    "well_files",
    "standards",
    "lessons",
    "procedures",
    "structured_records",
    "time_series",
    "external_api",
    "offset_wells",
)


class LlmCall(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """One model invocation, with routing rationale and resource usage."""

    __tablename__ = "llm_calls"
    id_prefix = "llm"

    provider: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    response_model: Mapped[str | None] = mapped_column(String(120))
    operation: Mapped[str] = mapped_column(String(24), default="chat", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="ok", nullable=False, index=True)
    purpose: Mapped[str | None] = mapped_column(String(120), index=True)
    prompt_key: Mapped[str | None] = mapped_column(String(120), index=True)
    prompt_version: Mapped[int | None] = mapped_column(IntType)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(64))
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    routing_reason: Mapped[str | None] = mapped_column(TextType)
    policy: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    temperature: Mapped[float | None] = mapped_column(Float)
    max_tokens: Mapped[int | None] = mapped_column(IntType)
    input_tokens: Mapped[int | None] = mapped_column(IntType)
    output_tokens: Mapped[int | None] = mapped_column(IntType)
    cached_tokens: Mapped[int | None] = mapped_column(IntType)
    cost_usd: Mapped[Decimal | None] = mapped_column(Money)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    finish_reason: Mapped[str | None] = mapped_column(String(60))
    content_captured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    request_payload: Mapped[dict | None] = mapped_column(JsonType)
    response_payload: Mapped[dict | None] = mapped_column(JsonType)
    system_chars: Mapped[int | None] = mapped_column(IntType)
    input_chars: Mapped[int | None] = mapped_column(IntType)
    output_chars: Mapped[int | None] = mapped_column(IntType)
    error: Mapped[str | None] = mapped_column(TextType)
    retry_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    span_id: Mapped[str | None] = mapped_column(String(32))
    evidence_refs: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class AgentRun(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """One execution of an agent definition (registry ``kind='agent'``)."""

    __tablename__ = "agent_runs"
    id_prefix = "agn"

    agent_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    agent_version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False, index=True)
    goal: Mapped[str | None] = mapped_column(TextType)
    trigger: Mapped[str] = mapped_column(String(32), default="api", nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    inputs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    outputs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    context_refs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    steps: Mapped[list] = mapped_column(
        JsonType, default=list, nullable=False, comment="tool/engine/retrieval steps with results (no hidden CoT)"
    )
    llm_call_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    tool_call_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    engine_run_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    recommendation_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    parent_agent_run_id: Mapped[str | None] = mapped_column(String(64))
    action_level: Mapped[str] = mapped_column(String(8), default="L1", nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    total_tokens: Mapped[int | None] = mapped_column(IntType)
    cost_usd: Mapped[Decimal | None] = mapped_column(Money)
    step_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(TextType)
    stop_reason: Mapped[str | None] = mapped_column(String(80))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    initiated_by: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class ToolCall(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """One tool invocation with side-effect declaration and governance outcome."""

    __tablename__ = "tool_calls"
    id_prefix = "tcl"

    tool_key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    tool_version: Mapped[int | None] = mapped_column(IntType)
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False, index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    inputs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    outputs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    side_effects: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    action_level: Mapped[str] = mapped_column(String(8), default="L0", nullable=False, index=True)
    approval_id: Mapped[str | None] = mapped_column(String(64), index=True)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    permission_decision: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    attempt: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(120), index=True)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    error: Mapped[str | None] = mapped_column(TextType)
    error_code: Mapped[str | None] = mapped_column(String(80))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    span_id: Mapped[str | None] = mapped_column(String(32))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class PromptTemplate(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Versioned prompt. Built-in prompts are seeded defaults that users can fork."""

    __tablename__ = "prompt_templates"
    id_prefix = "prm"

    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    purpose: Mapped[str | None] = mapped_column(String(200))
    domain_pack: Mapped[str | None] = mapped_column(String(64), index=True)
    template: Mapped[str] = mapped_column(TextType, nullable=False)
    variables: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    model_hints: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    output_schema: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    is_system_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(TextType)
    evaluation_score: Mapped[float | None] = mapped_column(Float)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class KnowledgeSource(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A named, scoped knowledge collection used by retrieval and agents."""

    __tablename__ = "knowledge_sources"
    id_prefix = "kns"

    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    kind: Mapped[str] = mapped_column(String(60), default="document_library", nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(TextType)
    scope: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    config: Mapped[dict] = mapped_column(
        JsonType, default=dict, nullable=False, comment="retrieval modes, filters, weights, top_k"
    )
    classification: Mapped[str] = mapped_column(String(32), default="internal", nullable=False)
    retention_days: Mapped[int | None] = mapped_column(IntType)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_system_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    document_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    last_indexed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    created_by: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Conversation(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A chat/agent thread, bound to an engineering context (well, project, operation)."""

    __tablename__ = "conversations"
    id_prefix = "cnv"

    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str | None] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(24), default="chat", nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64))
    agent_key: Mapped[str | None] = mapped_column(String(120))
    context: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    pinned_entity: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    last_message_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    message_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Message(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    __tablename__ = "messages"
    id_prefix = "msg"

    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    content: Mapped[str] = mapped_column(TextType, nullable=False)
    content_parts: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="complete", nullable=False)
    llm_call_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64))
    citations: Mapped[list] = mapped_column(
        JsonType, default=list, nullable=False, comment="evidence references shown to the user"
    )
    tool_calls: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    recommendations: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    token_count: Mapped[int | None] = mapped_column(IntType)
    feedback_score: Mapped[int | None] = mapped_column(IntType)
    provenance: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class MemoryNote(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Durable scoped memory (well/project/user/global) with provenance.

    Deliberately explicit and inspectable: an agent's memory is a queryable table, not an
    opaque vector store, so it can be reviewed, corrected and expired.
    """

    __tablename__ = "memory_notes"
    id_prefix = "mem"

    scope: Mapped[str] = mapped_column(String(24), default="well", nullable=False, index=True)
    scope_id: Mapped[str | None] = mapped_column(String(64), index=True)
    key: Mapped[str | None] = mapped_column(String(160), index=True)
    content: Mapped[str] = mapped_column(TextType, nullable=False)
    summary: Mapped[str | None] = mapped_column(String(300))
    source_kind: Mapped[str | None] = mapped_column(String(40))
    source_id: Mapped[str | None] = mapped_column(String(64))
    evidence_refs: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    importance: Mapped[float | None] = mapped_column(Float)
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    created_by: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Feedback(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Human feedback on any AI/engineering output — the raw material of evaluations."""

    __tablename__ = "feedback"
    id_prefix = "fbk"

    target_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rating: Mapped[int | None] = mapped_column(IntType)
    verdict: Mapped[str | None] = mapped_column(String(24), comment="helpful|unhelpful|incorrect|incomplete")
    comment: Mapped[str | None] = mapped_column(TextType)
    corrected_value: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))
    used_in_eval: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
