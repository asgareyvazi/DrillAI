"""Optimization runs, candidate solutions, scenarios and scenario results.

An optimizer in this platform never returns "one unexplained number". It returns a
**candidate set**: each candidate carries its decision variables, objective values,
constraint evaluations, supporting engine runs, evidence (offsets and their weights),
an explanation and feasibility — with sensitivity/uncertainty at the run level and a
Pareto flag for trade-off analysis.

Scenarios use the same substrate: a ``Scenario`` is a named delta against a baseline twin
snapshot; evaluating it runs the registered engines and stores the deltas, which is what
makes "what if we set the casing shoe 150 m deeper?" an answerable question rather than a
meeting.
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

OPTIMIZATION_PROBLEMS = (
    "rop_parameters",
    "bit_selection",
    "bit_bha_parameters",
    "hydraulics_nozzles",
    "hole_cleaning",
    "pressure_window",
    "trajectory",
    "torque_drag",
    "casing_design",
    "casing_seat",
    "cement_job",
    "mud_program",
    "tripping_speed",
    "completion_design",
    "service_selection",
    "tool_selection",
    "consumables_plan",
    "npt_reduction",
    "cost_time",
    "drilling_sequence",
    "readiness_prioritization",
    "generic",
)
SCENARIO_STATUSES = ("draft", "evaluating", "evaluated", "compared", "selected", "rejected", "archived")


class OptimizationRun(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    __tablename__ = "optimization_runs"
    id_prefix = "opt"

    problem_key: Mapped[str] = mapped_column(String(80), default="generic", nullable=False, index=True)
    optimizer_key: Mapped[str] = mapped_column(String(120), default="enumerative", nullable=False)
    optimizer_version: Mapped[str | None] = mapped_column(String(40))
    title: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(24), default="running", nullable=False, index=True)
    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    section_id: Mapped[str | None] = mapped_column(String(64), index=True)
    operation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    baseline_snapshot_id: Mapped[str | None] = mapped_column(String(64), index=True)
    scenario_id: Mapped[str | None] = mapped_column(String(64), index=True)
    objectives: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    constraints: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    decision_variables: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    engine_keys: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    offset_analysis: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    assumption_set: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    candidates_evaluated: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    feasible_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    pareto_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    best_candidate_id: Mapped[str | None] = mapped_column(String(64))
    sensitivity: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    uncertainty: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    recommendation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    engine_run_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    started_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    finished_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    error: Mapped[str | None] = mapped_column(TextType)
    triggered_by: Mapped[str] = mapped_column(String(32), default="api", nullable=False)
    initiated_by: Mapped[str | None] = mapped_column(String(64))
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class CandidateSolution(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    __tablename__ = "candidate_solutions"
    id_prefix = "cnd"
    __table_args__ = (UniqueConstraint("run_id", "rank"),)

    run_id: Mapped[str] = mapped_column(
        ForeignKey("optimization_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rank: Mapped[int] = mapped_column(IntType, default=1, nullable=False)
    label: Mapped[str | None] = mapped_column(String(200))
    is_feasible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_pareto: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    is_baseline: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    decisions: Mapped[list] = mapped_column(
        JsonType, default=list, nullable=False, comment="[{name,value,unit,bounds,source}]"
    )
    objectives: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    constraint_results: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    feasibility_margin: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    predicted_metrics: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    risk: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    cost: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    time: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    evidence: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    explanation: Mapped[str | None] = mapped_column(TextType)
    rejected_because: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    engine_run_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    workflow_run_id: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class Scenario(Base, IdMixin, TimestampMixin, OrgScopedMixin):
    """A named what-if case: baseline snapshot + explicit changes."""

    __tablename__ = "scenarios"
    id_prefix = "scn"

    project_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str | None] = mapped_column(TextType)
    kind: Mapped[str] = mapped_column(String(40), default="design_alternative", nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(24), default="draft", nullable=False, index=True)
    baseline_snapshot_id: Mapped[str | None] = mapped_column(String(64), index=True)
    snapshot_id: Mapped[str | None] = mapped_column(String(64), index=True)
    tags: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(64))
    evaluated_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    selected_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    selection_note: Mapped[str | None] = mapped_column(TextType)
    optimization_run_ids: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    summary: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)

    changes: Mapped[list[ScenarioChange]] = relationship(
        back_populates="scenario", cascade="all, delete-orphan"
    )


class ScenarioChange(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """One explicit modification within a scenario (path, before, after, unit, reason)."""

    __tablename__ = "scenario_changes"
    id_prefix = "scc"

    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    target_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    target_id: Mapped[str | None] = mapped_column(String(64), index=True)
    path: Mapped[str] = mapped_column(String(400), nullable=False)
    change_type: Mapped[str] = mapped_column(String(24), default="set", nullable=False)
    before: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    after: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(TextType)
    created_by: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)

    scenario: Mapped[Scenario] = relationship(back_populates="changes")


class ScenarioResult(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Engine outcome for a scenario, with deltas against the baseline."""

    __tablename__ = "scenario_results"
    id_prefix = "scr"

    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("scenarios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    snapshot_id: Mapped[str | None] = mapped_column(String(64), index=True)
    engine_key: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    engine_version: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(24), default="ok", nullable=False, index=True)
    outputs: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    deltas: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    feasibility: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    warnings: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    metrics: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
    engine_run_id: Mapped[str | None] = mapped_column(String(64), index=True)
    computed_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    error: Mapped[str | None] = mapped_column(TextType)
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)


class ImpactAnalysis(Base, IdMixin, CreatedAtMixin, OrgScopedMixin):
    """Result of a change-impact walk over the derivation graph."""

    __tablename__ = "impact_analyses"
    id_prefix = "imp"

    trigger_kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    trigger_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    change_record_id: Mapped[str | None] = mapped_column(String(64), index=True)
    well_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wellbore_id: Mapped[str | None] = mapped_column(String(64), index=True)
    affected: Mapped[list] = mapped_column(
        JsonType, default=list, nullable=False, comment="[{node_kind,node_id,engine_key,depth,reason}]"
    )
    affected_count: Mapped[int] = mapped_column(IntType, default=0, nullable=False)
    stale_engine_runs: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    stale_recommendations: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    stale_artifacts: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    recomputed: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    unresolved: Mapped[list] = mapped_column(JsonType, default=list, nullable=False)
    severity: Mapped[str] = mapped_column(String(24), default="medium", nullable=False)
    summary: Mapped[str | None] = mapped_column(TextType)
    computed_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    computed_by: Mapped[str | None] = mapped_column(String(64))
    workflow_run_id: Mapped[str | None] = mapped_column(String(64))
    attributes: Mapped[dict] = mapped_column(JsonType, default=dict, nullable=False)
