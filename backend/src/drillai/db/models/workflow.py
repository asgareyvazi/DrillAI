"""Workflow definitions, versions, runs, node runs, run events and approvals.

The runtime is designed for **durable, resumable, inspectable execution** rather than
fire-and-forget: every run appends to an ordered ``RunEvent`` log (the same pattern that
makes Temporal-style engines survivable), node runs record inputs/outputs/attempts, runs
can be paused for human approval, resumed, re-run from a node and cancelled.

Definitions are versioned and never mutated in place: a run pins ``workflow_version_id``,
so a workflow edited tomorrow cannot rewrite what happened today. That also gives the
"everything default, everything editable" property: built-in workflows are seeded as
versions that users fork and edit.
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

WORKFLOW_STATUSES = ("draft", "published", "deprecated", "archived")
RUN_STATUSES = (
    "queued",
    "running",
    "paused",
    "waiting_approval",
    "succeeded",
    "failed",
    "cancelled",
    "timed_out",
)
NODE_RUN_STATUSES = (
    "pending",
    "running",
    "retrying",
    "succeeded",
    "failed",
    "skipped",
    "paused",
    "waiting_approval",
    "cancelled",
)
RUN_EVENT_TYPES = (
    "run_started",
    "run_paused",
    "run_resumed",
    "run_completed",
    "run_failed",
    "run_cancelled",
    "node_started",
    "node_succeeded",
    "node_failed",
    "node_retrying",
    "node_skipped",
    "node_paused",
    "log",
    "variable_set",
    "approval_requested",
    "approval_decided",
    "artifact_created",
    "breakpoint_hit",
    "metric",
)
APPROVAL_KINDS = (
    "workflow_node",
    "recommendation",
    "action",
    "artifact_publish",
    "offset_selection",
    "parameter_change",
    "readiness_gate",
    "scenario_adoption",
)
APPROVAL_STATUSES = ("pending", "approved", "rejected", "expired", "cancelled")


class Workflow(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    __tablename__ = "workflows"
    id_prefix = "wfl"
    __table_args__ = (UniqueConstraint("org_id", "key"),)

    key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str | None] = mapped_column(TextType)
    category: Mapped[str] = mapped_column(String(60), default="engineering", nullable=False, index=True)
    domain_pack: Mapped[str | None] = mapped_column(String(64), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False, index=True)
    current_version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    published_version_id: Mapped[str | None] = mapped_column(String(64))
    is_template: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_system_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_editable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    forked_from_id: Mapped[str | None] = mapped_column(String(64), index=True)
    owner: Mapped[str | None] = mapped_column(String(64))
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    trigger_config: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    input_schema: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    output_schema: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    permissions: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    action_level: Mapped[str] = mapped_column(String(8), default="L1", nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)

    versions: Mapped[list[WorkflowVersion]] = relationship(
        back_populates="workflow", cascade="all, delete-orphan", order_by="WorkflowVersion.version"
    )


class WorkflowVersion(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    __tablename__ = "workflow_versions"
    id_prefix = "wfv"
    __table_args__ = (UniqueConstraint("workflow_id", "version"),)

    workflow_id: Mapped[str] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(IntType, nullable=False)
    graph: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    graph_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(TextType)
    change_reason: Mapped[str | None] = mapped_column(TextType)
    validation: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    node_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    edge_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    published_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    published_by: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[str | None] = mapped_column(String(64))

    workflow: Mapped[Workflow] = relationship(back_populates="versions")


class WorkflowRun(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    __tablename__ = "workflow_runs"
    id_prefix = "run"

    workflow_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    workflow_version_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    workflow_key: Mapped[str | None] = mapped_column(String(120), index=True)
    version: Mapped[int | None] = mapped_column(IntType)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False, index=True)
    trigger_type: Mapped[str] = mapped_column(String(32), default="manual", nullable=False, index=True)
    trigger_ref: Mapped[str | None] = mapped_column(String(120))
    trigger_payload: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    context: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    inputs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    outputs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    variables: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    started_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    step_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(TextType)
    error_node_id: Mapped[str | None] = mapped_column(String(80))
    cursor_node_id: Mapped[str | None] = mapped_column(
        String(80), comment="node the run is waiting at / will resume from"
    )
    pending_approval_id: Mapped[str | None] = mapped_column(String(64), index=True)
    breakpoints: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    resolved_node_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    initiated_by: Mapped[str | None] = mapped_column(String(64))
    initiated_by_kind: Mapped[str] = mapped_column(String(24), default="user", nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    is_dry_run: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    parent_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    budget: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    metrics: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class NodeRun(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    __tablename__ = "node_runs"
    id_prefix = "nrn"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    node_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    node_type: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    node_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    attempt: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    max_attempts: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    inputs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    outputs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(TextType)
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime, index=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    duration_ms: Mapped[float | None] = mapped_column(Float)
    logs: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    metrics: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    engine_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    llm_call_id: Mapped[str | None] = mapped_column(String(64))
    tool_call_id: Mapped[str | None] = mapped_column(String(64))
    workflow_run_ref: Mapped[str | None] = mapped_column(String(64), comment="child run for sub-workflows")
    branch_taken: Mapped[str | None] = mapped_column(String(80))
    loop_index: Mapped[int | None] = mapped_column(IntType)
    resolution: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    is_breakpoint: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resumed_from_node_run_id: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class RunEvent(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Append-only run log (sequence per run) — the replay/resume substrate."""

    __tablename__ = "run_events"
    id_prefix = "rev"

    run_id: Mapped[str] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(IntType, nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    node_id: Mapped[str | None] = mapped_column(String(80), index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(TextType)
    payload: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    level: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    occurred_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    span_id: Mapped[str | None] = mapped_column(String(32))


class ApprovalRequest(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Human decision gate. Nothing at L4/L5 happens without one of these."""

    __tablename__ = "approval_requests"
    id_prefix = "apr"

    kind: Mapped[str] = mapped_column(String(40), default="workflow_node", nullable=False, index=True)
    subject_kind: Mapped[str | None] = mapped_column(String(40), index=True)
    subject_id: Mapped[str | None] = mapped_column(String(64), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(64))
    node_id: Mapped[str | None] = mapped_column(String(80))
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(TextType)
    request_payload: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    proposed_action: Mapped[str | None] = mapped_column(TextType)
    action_level: Mapped[str] = mapped_column(String(8), default="L4", nullable=False, index=True)
    risk_notes: Mapped[str | None] = mapped_column(TextType)
    required_role: Mapped[str | None] = mapped_column(String(64), default="approver")
    evidence_refs: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(64))
    requested_by_kind: Mapped[str] = mapped_column(String(24), default="user", nullable=False)
    requested_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False, index=True)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    decision_note: Mapped[str | None] = mapped_column(TextType)
    conditions: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    notification_state: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class RunArtifact(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Output produced by a run node (table, chart, report, recommendation, dataset)."""

    __tablename__ = "run_artifacts"
    id_prefix = "rta"

    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    node_id: Mapped[str | None] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(40), default="dataset", nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(120))
    blob_ref: Mapped[str | None] = mapped_column(String(400))
    payload: Mapped[dict | None] = mapped_column(JsonType)
    row_count: Mapped[int | None] = mapped_column(IntType)
    byte_size: Mapped[int | None] = mapped_column(IntType)
    evidence_refs: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    document_id: Mapped[str | None] = mapped_column(String(64))
    recommendation_id: Mapped[str | None] = mapped_column(String(64))
    engine_run_id: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
