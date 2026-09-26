"""Engineering engine runs and recommendations.

``EngineRun`` is the durable record of a deterministic calculation: which engine and
version, which inputs (canonical SI + input hash), which outputs, which assumptions were
applied, which constraint violations were found and how long it took. Re-running the same
engine with the same inputs must reproduce the same ``outputs_hash`` — that is what makes
an engineering result defensible months later (and testable in CI).

``Recommendation`` is the governed output of an engineering or AI workflow: parameters,
ranges, rationale, alternatives, constraints, sensitivity, uncertainty, action level and
— through ``EvidenceLink`` — the documents, calculations and offsets that support it.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, Float, String
from sqlalchemy.orm import Mapped, mapped_column

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

RECOMMENDATION_STATUSES = ("draft", "proposed", "under_review", "accepted", "rejected", "applied", "superseded")
ACTION_LEVELS = ("L0", "L1", "L2", "L3", "L4", "L5")


class EngineRun(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    __tablename__ = "engine_runs"
    id_prefix = "enr"

    engine_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    engine_version: Mapped[str] = mapped_column(String(40), nullable=False)
    domain_pack: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(24), default="ok", nullable=False, index=True)
    subject_kind: Mapped[str] = mapped_column(String(40), default="wellbore", nullable=False)
    subject_id: Mapped[str | None] = mapped_column(String(64), index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    inputs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    outputs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    inputs_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    outputs_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    units: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    assumptions: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    limitations: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    warnings: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    constraint_violations: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    is_feasible: Mapped[bool | None] = mapped_column(Boolean)
    input_source_kind: Mapped[str | None] = mapped_column(String(40))
    input_source_id: Mapped[str | None] = mapped_column(String(64), comment="twin snapshot id when applicable")
    duration_ms: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False, index=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    error: Mapped[str | None] = mapped_column(TextType)
    triggered_by: Mapped[str] = mapped_column(String(32), default="api", nullable=False, index=True)
    triggered_by_id: Mapped[str | None] = mapped_column(String(64))
    workflow_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    node_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    span_id: Mapped[str | None] = mapped_column(String(32))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Recommendation(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    __tablename__ = "recommendations"
    id_prefix = "rcm"

    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    domain: Mapped[str] = mapped_column(String(60), default="drilling", nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(80), default="parameter_window", nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    statement: Mapped[str] = mapped_column(TextType, nullable=False)
    parameters: Mapped[list] = mapped_column(
        JsonType, default=list, nullable=False, comment="[{name,value,unit,min,max,recommended}]"
    )
    rationale: Mapped[str | None] = mapped_column(TextType)
    why_not: Mapped[list] = mapped_column(
        JsonType, default=list, nullable=False, comment='answers to "why not X?" for rejected alternatives'
    )
    assumptions: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    constraints_applied: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    alternatives: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    sensitivities: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    uncertainty: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    confidence_basis: Mapped[str | None] = mapped_column(TextType)
    data_quality: Mapped[str] = mapped_column(String(24), default="unverified", nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="proposed", nullable=False, index=True)
    action_level: Mapped[str] = mapped_column(String(8), default="L2", nullable=False, index=True)
    engine_run_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    llm_call_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    offset_well_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    optimization_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    scenario_id: Mapped[str | None] = mapped_column(String(64))
    depth_from_md_si: Mapped[float | None] = mapped_column(Float)
    depth_to_md_si: Mapped[float | None] = mapped_column(Float)
    valid_from: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    valid_to: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_by_kind: Mapped[str] = mapped_column(String(24), default="engine", nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(64))
    reviewed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    decision_note: Mapped[str | None] = mapped_column(TextType)
    approval_request_id: Mapped[str | None] = mapped_column(String(64))
    supersedes_id: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)

    @property
    def requires_approval(self) -> bool:
        return self.action_level in ("L4", "L5")

    @property
    def parameter_summary(self) -> str:
        parts = []
        for parameter in self.parameters or []:
            name = parameter.get("name")
            recommended = parameter.get("recommended")
            low, high = parameter.get("min"), parameter.get("max")
            unit = parameter.get("unit", "")
            if recommended is not None:
                parts.append(f"{name}={recommended} {unit}".strip())
            elif low is not None and high is not None:
                parts.append(f"{name}={low}-{high} {unit}".strip())
        return ", ".join(parts)


class OffsetCandidate(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """Offset-well candidate with a scored, explainable similarity to a subject well.

    Similarity is *not* a naive average: each criterion (formation, hole size, depth,
    inclination, BHA, bit, mud, geography, completion type, data quality…) contributes a
    weighted, evidence-carrying score, and the resulting weight is what downstream
    statistics must use. Rows are versioned per analysis so a past recommendation can be
    rebuilt exactly.
    """

    __tablename__ = "offset_candidates"
    id_prefix = "off"

    analysis_key: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    version: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    subject_well_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    subject_section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    candidate_well_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    candidate_wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    candidate_section_id: Mapped[str | None] = mapped_column(String(64))
    candidate_name: Mapped[str | None] = mapped_column(String(200))
    overall_similarity: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    weight: Mapped[float | None] = mapped_column(Float, comment="statistical weight for aggregation")
    rank: Mapped[int | None] = mapped_column(IntType)
    criteria: Mapped[list] = mapped_column(
        JsonType, default=list, nullable=False, comment="[{criterion,value,subject_value,score,weight,method}]"
    )
    criteria_weights: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    rationale: Mapped[str | None] = mapped_column(TextType)
    data_quality: Mapped[str] = mapped_column(String(24), default="unverified", nullable=False)
    exclusions: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    comparison: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    lessons: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    evidence_refs: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    engine_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    is_selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    selected_by: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
