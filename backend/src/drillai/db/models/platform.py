"""Platform-level tables: registry, security/audit, observability/evaluations and
integration/connector state.

Kept in one module because these tables are cross-cutting infrastructure rather than
domain entities.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, Float, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from drillai.db.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    IntType,
    JsonType,
    OrgScopedMixin,
    TextType,
    TimestampMixin,
    UtcDateTime,
)

# --------------------------------------------------------------------------- registry
REGISTRY_KINDS = (
    "agent",
    "skill",
    "tool",
    "engine",
    "optimizer",
    "node_type",
    "workflow_template",
    "prompt",
    "report_template",
    "dashboard",
    "retrieval_strategy",
    "model_provider",
    "data_connector",
    "domain_pack",
    "view",
)
REGISTRY_STATUSES = ("draft", "active", "deprecated", "archived")


class RegistryItem(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A registered capability. ``org_id`` empty means a platform default.

    "Everything default, everything editable": built-in agents/skills/tools/engines are
    code-defined defaults *and* materialised here, so the UI can list, inspect, compare
    and fork them; a user-created override shadows the default for its organization while
    the default remains intact and restorable.
    """

    __tablename__ = "registry_items"
    id_prefix = "rgi"
    __table_args__ = (UniqueConstraint("org_id", "kind", "key"),)

    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str | None] = mapped_column(TextType)
    domain: Mapped[str | None] = mapped_column(String(80), index=True)
    domain_pack: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False, index=True)
    current_version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    is_system_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_editable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    forked_from_id: Mapped[str | None] = mapped_column(String(64), index=True)
    owner: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    permissions: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    action_level: Mapped[str] = mapped_column(String(8), default="L0", nullable=False)
    schema_version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)

    versions: Mapped[list[RegistryItemVersion]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="RegistryItemVersion.version"
    )


class RegistryItemVersion(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    __tablename__ = "registry_item_versions"
    id_prefix = "rgv"
    __table_args__ = (UniqueConstraint("item_id", "version"),)

    item_id: Mapped[str] = mapped_column(
        ForeignKey("registry_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    definition: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    schema_key: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    changelog: Mapped[str | None] = mapped_column(TextType)
    created_by: Mapped[str | None] = mapped_column(String(64))
    published_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    published_by: Mapped[str | None] = mapped_column(String(64))

    item: Mapped[RegistryItem] = relationship(back_populates="versions")


# --------------------------------------------------------------------------- security
POLICY_EFFECTS = ("allow", "deny")
RESOURCE_KINDS = (
    "organization",
    "project",
    "field",
    "well",
    "wellbore",
    "document",
    "workflow",
    "engine",
    "agent",
    "tool",
    "skill",
    "model",
    "knowledge_source",
    "data_class",
    "action",
)


class PolicyBinding(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Explicit allow/deny binding for a subject (role/user/agent/tool) on a resource."""

    __tablename__ = "policy_bindings"
    id_prefix = "pol"

    subject_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    resource_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), index=True)
    permission: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    effect: Mapped[str] = mapped_column(String(16), default="allow", nullable=False)
    conditions: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    priority: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    description: Mapped[str | None] = mapped_column(TextType)


class AuditLog(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Every governed mutation and every agent/tool execution lands here."""

    __tablename__ = "audit_logs"
    id_prefix = "aud"

    actor_kind: Mapped[str] = mapped_column(String(24), default="user", nullable=False, index=True)
    actor_id: Mapped[str | None] = mapped_column(String(64), index=True)
    actor_display: Mapped[str | None] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    action_level: Mapped[str] = mapped_column(String(8), default="L0", nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    outcome: Mapped[str] = mapped_column(String(24), default="success", nullable=False, index=True)
    permission_decision: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    details: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    before: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    after: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(64), index=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64))
    agent_run_id: Mapped[str | None] = mapped_column(String(64))
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    request_id: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    occurred_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)


class SecretRef(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A *reference* to secret material — never the secret itself.

    The platform stores where a credential lives (environment variable, file path in a
    mounted secret volume, or an external vault path) so that credentials never enter the
    database, backups or logs. See SECURITY_MODEL.md.
    """

    __tablename__ = "secret_refs"
    id_prefix = "srf"
    __table_args__ = (UniqueConstraint("org_id", "key"),)

    key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    backend: Mapped[str] = mapped_column(String(40), default="env", nullable=False)
    locator: Mapped[str] = mapped_column(String(500), nullable=False)
    description: Mapped[str | None] = mapped_column(TextType)
    scope: Mapped[str | None] = mapped_column(String(80))
    last_rotated_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    created_by: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class IdempotencyKey(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Replay protection for mutating API calls and tool executions."""

    __tablename__ = "idempotency_keys"
    id_prefix = "idk"
    __table_args__ = (UniqueConstraint("org_id", "key", "scope"),)

    key: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(120), default="api", nullable=False)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    response_ref: Mapped[str | None] = mapped_column(String(120))
    response_payload: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="completed", nullable=False)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)


# --------------------------------------------------------------------------- observability
TRACE_KINDS = ("api", "workflow", "agent", "engine", "ingestion", "integration", "scheduler", "optimization", "test")
SPAN_KINDS = ("INTERNAL", "CLIENT", "SERVER", "PRODUCER", "CONSUMER")
SPAN_OPERATIONS = (
    "http",
    "db",
    "gen_ai",
    "tool",
    "retrieval",
    "workflow_node",
    "engine",
    "optimization",
    "ingestion",
    "integration",
    "custom",
)


class Trace(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """OTel-aligned trace root."""

    __tablename__ = "traces"
    id_prefix = "trc"

    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), default="api", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="ok", nullable=False, index=True)
    root_span_id: Mapped[str | None] = mapped_column(String(32))
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    engine_run_id: Mapped[str | None] = mapped_column(String(64))
    service: Mapped[str] = mapped_column(String(80), default="drillai-api", nullable=False)
    environment: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    span_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    total_tokens: Mapped[int | None] = mapped_column(IntType)
    total_cost_usd: Mapped[float | None] = mapped_column(Float)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(TextType)


class Span(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """A single operation with OTel semantic-convention attributes.

    GenAI spans carry ``gen_ai.*`` keys (``gen_ai.operation.name``,
    ``gen_ai.provider.name``, ``gen_ai.request.model``, ``gen_ai.usage.input_tokens``,
    ``gen_ai.tool.name`` …) so traces map 1:1 onto the industry-standard schema and can be
    exported to any OTLP backend without re-modelling.
    """

    __tablename__ = "spans"
    id_prefix = "spn"

    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    span_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    parent_span_id: Mapped[str | None] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), default="INTERNAL", nullable=False)
    operation_type: Mapped[str] = mapped_column(String(32), default="custom", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="ok", nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    node_id: Mapped[str | None] = mapped_column(String(80))
    engine_run_id: Mapped[str | None] = mapped_column(String(64))
    llm_call_id: Mapped[str | None] = mapped_column(String(64))
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    ended_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    events: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    error: Mapped[str | None] = mapped_column(TextType)


class MetricPoint(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Counter/gauge/histogram sample used for dashboards and SLOs."""

    __tablename__ = "metric_points"
    id_prefix = "mtr"

    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(24), default="counter", nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40))
    dimensions: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    recorded_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    period_seconds: Mapped[float | None] = mapped_column(Float)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)


class EvaluationSuite(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A regression/evaluation suite for agents, workflows, engines or retrieval."""

    __tablename__ = "evaluation_suites"
    id_prefix = "evs"

    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str | None] = mapped_column(TextType)
    target_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_key: Mapped[str | None] = mapped_column(String(160), index=True)
    grading: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    pass_threshold: Mapped[float | None] = mapped_column(Float)
    is_regression: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    case_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    last_run_id: Mapped[str | None] = mapped_column(String(64))
    last_run_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    last_pass_rate: Mapped[float | None] = mapped_column(Float)
    created_by: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class EvaluationCase(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    __tablename__ = "evaluation_cases"
    id_prefix = "evc"

    suite_id: Mapped[str] = mapped_column(
        ForeignKey("evaluation_suites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    inputs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    expected: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    assertions: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="fixture", nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class EvaluationRun(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    __tablename__ = "evaluation_runs"
    id_prefix = "evr"

    suite_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    git_sha: Mapped[str | None] = mapped_column(String(64))
    model_config: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    total_cases: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    passed: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    errored: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    pass_rate: Mapped[float | None] = mapped_column(Float)
    results: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    summary: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(TextType)


# --------------------------------------------------------------------------- integrations
CONNECTOR_PROVIDERS = (
    "telegram",
    "whatsapp",
    "email",
    "webhook",
    "rest_api",
    "database",
    "witsml",
    "etp",
    "mcp",
    "scheduler",
    "filesystem",
    "object_storage",
)


class Connector(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Configured integration endpoint (inbound, outbound or both)."""

    __tablename__ = "connectors"
    id_prefix = "cnc"
    __table_args__ = (UniqueConstraint("org_id", "key"),)

    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(24), default="inbound", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="disabled", nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    description: Mapped[str | None] = mapped_column(TextType)
    config: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    secret_refs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    capabilities: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    default_document_type: Mapped[str | None] = mapped_column(String(40))
    default_well_id: Mapped[str | None] = mapped_column(String(64))
    cursor: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    last_error: Mapped[str | None] = mapped_column(TextType)
    error_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class ConnectorEvent(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Inbound or outbound integration message with dedup and processing state."""

    __tablename__ = "connector_events"
    id_prefix = "cev"

    connector_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(24), default="inbound", nullable=False, index=True)
    external_id: Mapped[str | None] = mapped_column(String(200), index=True)
    dedup_key: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="received", nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    raw_ref: Mapped[str | None] = mapped_column(String(400))
    normalized_event_id: Mapped[str | None] = mapped_column(String(64))
    document_id: Mapped[str | None] = mapped_column(String(64))
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    error: Mapped[str | None] = mapped_column(TextType)
    attempts: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    occurred_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    processed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)


class OutboundMessage(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Outbox row for notifications (email/Telegram/WhatsApp/webhook)."""

    __tablename__ = "outbound_messages"
    id_prefix = "obm"

    connector_id: Mapped[str | None] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    recipient: Mapped[str | None] = mapped_column(String(300))
    subject: Mapped[str | None] = mapped_column(String(400))
    body: Mapped[str | None] = mapped_column(TextType)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    attachments: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="queued", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(IntType, default=3, nullable=False)
    next_attempt_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    sent_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    error: Mapped[str | None] = mapped_column(TextType)
    provider_message_id: Mapped[str | None] = mapped_column(String(200))
    trigger_kind: Mapped[str | None] = mapped_column(String(40), index=True)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    alert_id: Mapped[str | None] = mapped_column(String(64), index=True)
    recommendation_id: Mapped[str | None] = mapped_column(String(64))
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(160), index=True)
    evidence_refs: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Schedule(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Cron/interval trigger for workflows, connectors, reports or evaluations."""

    __tablename__ = "schedules"
    id_prefix = "sch"
    __table_args__ = (UniqueConstraint("org_id", "key"),)

    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(40), default="workflow", nullable=False, index=True)
    target_key: Mapped[str | None] = mapped_column(String(160))
    workflow_id: Mapped[str | None] = mapped_column(String(64), index=True)
    connector_id: Mapped[str | None] = mapped_column(String(64), index=True)
    cron: Mapped[str | None] = mapped_column(String(120))
    interval_seconds: Mapped[int | None] = mapped_column(IntType)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False, index=True)
    next_run_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    last_run_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    last_status: Mapped[str | None] = mapped_column(String(24))
    run_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    config: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
