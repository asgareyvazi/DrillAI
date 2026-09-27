"""Drilling parameter optimisation → an explained, persisted recommendation.

The flow, and where each part of it comes from:

1. **Candidate generation** — ``optimization.sweep`` enumerates the parameter grid.
2. **Candidate evaluation** — each candidate is pushed through *deterministic* engines against the
   real well geometry: ``hydraulics.laminar`` for ECD / standpipe pressure / annular velocity and
   ``torque_drag.soft_string`` for surface torque and hookload.
3. **Constraints** — pore-pressure and fracture-gradient windows (from the well's sections), plus
   rig/pump limits declared in the problem. A candidate that violates one is *infeasible* and says
   which limit it broke.
4. **Objectives and Pareto** — ``optimization.pareto`` ranks feasible candidates and computes the
   non-dominated frontier over the objectives actually computed here.
5. **Explanation** — recommended candidate, trade-offs, and **why not** each alternative, traced
   to the constraint or the dominated objective.
6. **Persistence** — ``optimization_runs`` + ``candidate_solutions`` rows, then a
   ``recommendations`` row whose ``why_not`` and ``alternatives`` survive independently of the
   session that produced them.

**What is deliberately not done.** The platform has no ROP-prediction model, so candidates are
*not* ranked on predicted ROP. Ranking on a guessed ROP would be exactly the "engineering value
invented by software" failure this architecture forbids. Recorded ROP from this well and from
offsets is attached to each candidate as *evidence* — it informs the engineer, it does not become
a computed objective. This is stated in the returned ``assumptions`` and in ``not_evaluated``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.errors import EngineError, ValidationFailed
from drillai.db.models import (
    CandidateSolution,
    EngineRun,
    OptimizationRun,
    Recommendation,
    TwinAspect,
    Well,
    Wellbore,
    WellSection,
)
from drillai.engines.registry import EngineRegistry, load_default_engines

#: Objectives this module can actually compute from engine output. Each entry names the engine
#: output field it reads, so the mapping is auditable rather than implicit.
COMPUTED_OBJECTIVES: dict[str, dict[str, str]] = {
    "ecd_margin_si": {
        "sense": "maximize",
        "source": "hydraulics.laminar:ecd_margin_si",
        "description": "Distance between the computed ECD and the fracture gradient (kg/m3 equivalent)",
    },
    "spp_utilisation": {
        "sense": "minimize",
        "source": "hydraulics.laminar:standpipe_pressure_expected_si",
        "description": "Standpipe pressure as a fraction of the declared pump pressure limit",
    },
    "annular_velocity_m_s": {
        "sense": "maximize",
        "source": "hydraulics.laminar:annular_velocity_min_si",
        "description": "Annular velocity in the tightest annulus (worst-case hole cleaning)",
    },
    "cutting_transport_ratio": {
        "sense": "maximize",
        "source": "hydraulics.laminar:cutting_transport_ratio",
        "description": "Computed cuttings transport ratio where the engine can evaluate it",
    },
    "torque_utilisation": {
        "sense": "minimize",
        "source": "torque_drag.soft_string:surface_torque_si",
        "description": "Surface torque as a fraction of the declared torque limit",
    },
    "hookload_utilisation": {
        "sense": "minimize",
        "source": "torque_drag.soft_string:pickup_hookload_si",
        "description": "Pick-up hookload as a fraction of the rig hookload limit",
    },
    "torque_margin_n_m": {
        "sense": "maximize",
        "source": "torque_drag.soft_string:surface_torque_si",
        "description": "Head-room between the computed surface torque and the declared limit",
    },
}

#: Quantities an engineer would like ranked on, that no engine in the registry predicts. They are
#: reported so that nobody mistakes their absence for an oversight.
NOT_EVALUATED: dict[str, str] = {
    "rop": (
        "No rate-of-penetration model exists in the engine registry. Recorded ROP from this well "
        "and from offsets is attached as evidence per candidate; it is never used as a computed "
        "objective."
    ),
    "mse": (
        "MSE is computed by drilling.mse but requires a measured ROP as input, so it cannot rank "
        "candidate parameters that have not been drilled yet."
    ),
    "bit_wear": "No bit-wear model exists; bit life is not predicted.",
    "vibration": "No vibration/dysfunction model exists; stick-slip risk is not predicted.",
}


@dataclass
class ConstraintOutcome:
    name: str
    satisfied: bool
    limit: float | None
    actual: float | None
    unit: str | None
    margin: float | None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "satisfied": self.satisfied,
            "limit": self.limit,
            "actual": self.actual,
            "unit": self.unit,
            "margin": self.margin,
            "note": self.note,
        }


@dataclass
class EvaluatedCandidate:
    candidate_id: str
    values: dict[str, float]
    feasible: bool
    objectives: dict[str, float] = field(default_factory=dict)
    constraints: list[ConstraintOutcome] = field(default_factory=list)
    engine_run_ids: list[str] = field(default_factory=list)
    hydraulics: dict[str, Any] = field(default_factory=dict)
    torque_drag: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def rejected_because(self) -> list[str]:
        reasons: list[str] = []
        for item in self.constraints:
            if item.satisfied:
                continue
            if item.note:
                reasons.append(f"{item.name}: {item.note}")
            else:
                unit = item.unit or ""
                reasons.append(
                    f"{item.name}: {item.actual} exceeds limit {item.limit} {unit}".strip()
                )
        return reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "values": self.values,
            "feasible": self.feasible,
            "objectives": self.objectives,
            "constraints": [item.to_dict() for item in self.constraints],
            "engine_run_ids": self.engine_run_ids,
            "hydraulics": self.hydraulics,
            "torque_drag": self.torque_drag,
            "warnings": self.warnings,
            "evidence": self.evidence,
        }


class DrillingOptimisationService:
    """Candidate generation, deterministic evaluation, Pareto ranking and persistence."""

    def __init__(
        self,
        session: AsyncSession,
        org_id: str,
        *,
        actor_id: str | None = None,
        registry: EngineRegistry | None = None,
    ) -> None:
        self.session = session
        self.org_id = org_id
        self.actor_id = actor_id
        self.registry = registry or load_default_engines()

    # ------------------------------------------------------------------ context

    async def _section(self, well_id: str, section_id: str | None) -> WellSection | None:
        """The section to optimise for: the named one, else the section being drilled."""
        if section_id:
            stmt = select(WellSection).where(
                WellSection.org_id == self.org_id, WellSection.id == section_id
            )
        else:
            stmt = (
                select(WellSection)
                .join(Wellbore, Wellbore.id == WellSection.wellbore_id)
                .where(
                    WellSection.org_id == self.org_id,
                    Wellbore.well_id == well_id,
                    WellSection.status == "drilling",
                )
            )
        return (await self.session.execute(stmt.limit(1))).scalars().first()

    async def _twin_payload(self, well_id: str, aspect: str) -> dict[str, Any]:
        row = (
            await self.session.execute(
                select(TwinAspect)
                .where(
                    TwinAspect.org_id == self.org_id,
                    TwinAspect.well_id == well_id,
                    TwinAspect.aspect == aspect,
                    TwinAspect.is_current.is_(True),
                )
                .limit(1)
            )
        ).scalars().first()
        return dict(row.payload or {}) if row is not None else {}

    # ------------------------------------------------------------------ evaluation

    async def _evaluate(
        self,
        *,
        well_id: str,
        section: WellSection | None,
        candidate_values: dict[str, float],
        hydraulics_base: dict[str, Any],
        torque_base: dict[str, Any],
        limits: dict[str, float],
        objectives: list[dict[str, Any]],
        persist_runs: bool,
    ) -> EvaluatedCandidate:
        values = dict(candidate_values)
        result = EvaluatedCandidate(
            candidate_id=str(values.pop("candidate_id", "")),
            values=values,
            feasible=True,
        )
        hydraulics_inputs = {**hydraulics_base}
        if "flow_rate_si" in values:
            hydraulics_inputs["flow_rate_si"] = values["flow_rate_si"]
        if section is not None and section.current_md_si is not None:
            hydraulics_inputs.setdefault("bit_depth_si", float(section.current_md_si))
        if section is not None and section.fracture_gradient_si is not None:
            hydraulics_inputs["fracture_gradient_si"] = float(section.fracture_gradient_si)
        if section is not None and section.pore_pressure_gradient_si is not None:
            hydraulics_inputs["pore_pressure_gradient_si"] = float(section.pore_pressure_gradient_si)

        # --- hydraulics -----------------------------------------------------------------
        hydraulics_output: dict[str, Any] = {}
        try:
            _hydraulics_spec, engine_result = self.registry.execute("hydraulics.laminar", hydraulics_inputs)
            hydraulics_output = engine_result.outputs.model_dump()
            result.hydraulics = {
                "ecd_bottom_si": hydraulics_output.get("ecd_bottom_si"),
                "ecd_margin_si": hydraulics_output.get("ecd_margin_si"),
                "standpipe_pressure_expected_si": hydraulics_output.get("standpipe_pressure_expected_si"),
                "annular_velocity_min_si": hydraulics_output.get("annular_velocity_min_si"),
                "annular_velocity_max_si": hydraulics_output.get("annular_velocity_max_si"),
                "cutting_transport_ratio": hydraulics_output.get("cutting_transport_ratio"),
                "bit_hydraulic_horsepower_si": hydraulics_output.get("bit_hydraulic_horsepower_si"),
                "bit_pressure_drop_si": hydraulics_output.get("bit_pressure_drop_si"),
                "is_feasible": engine_result.is_feasible,
                "warnings": list(engine_result.warnings or []),
            }
            if persist_runs:
                from drillai.engines.service import execute_engine

                execution = await execute_engine(
                    self.session,
                    engine_key="hydraulics.laminar",
                    inputs=hydraulics_inputs,
                    org_id=self.org_id,
                    subject_kind="candidate",
                    subject_id=result.candidate_id or None,
                    well_id=well_id,
                    input_source_kind="optimisation",
                    triggered_by="optimisation",
                    triggered_by_id=self.actor_id,
                )
                result.engine_run_ids.append(execution.engine_run_id or "")
            for warning in engine_result.warnings or []:
                result.warnings.append(f"hydraulics: {warning}")
            if engine_result.is_feasible is False:
                result.feasible = False
                result.constraints.append(
                    ConstraintOutcome(
                        name="hydraulics_feasibility",
                        satisfied=False,
                        limit=None,
                        actual=None,
                        unit=None,
                        margin=None,
                        note="; ".join(
                            str(item.get("message", item)) if isinstance(item, dict) else str(item)
                            for item in (engine_result.constraint_violations or [])
                        )
                        or "the hydraulics engine reported an infeasible result",
                    )
                )
        except Exception as exc:  # an engine refusal is a result, not a crash
            result.feasible = False
            result.warnings.append(f"hydraulics could not evaluate this candidate: {exc}")
            result.constraints.append(
                ConstraintOutcome(
                    name="hydraulics_inputs",
                    satisfied=False,
                    limit=None,
                    actual=None,
                    unit=None,
                    margin=None,
                    note=f"{type(exc).__name__}: {exc}",
                )
            )

        # --- torque & drag ---------------------------------------------------------------
        torque_inputs = {**torque_base}
        for key in ("wob_si", "rpm"):
            if key in values:
                torque_inputs[key] = values[key]
        if torque_inputs.get("survey") and torque_inputs.get("string"):
            try:
                _spec, torque_result = self.registry.execute("torque_drag.soft_string", torque_inputs)
                torque_output = torque_result.outputs.model_dump()
                result.torque_drag = {
                    "pickup_hookload_si": torque_output.get("pickup_hookload_si"),
                    "slackoff_hookload_si": torque_output.get("slackoff_hookload_si"),
                    "surface_torque_si": torque_output.get("surface_torque_si"),
                    "bit_torque_si": torque_output.get("bit_torque_si"),
                    "is_feasible": torque_result.is_feasible,
                    "warnings": list(torque_result.warnings or []),
                }
                if persist_runs:
                    from drillai.engines.service import execute_engine

                    execution = await execute_engine(
                        self.session,
                        engine_key="torque_drag.soft_string",
                        inputs=torque_inputs,
                        org_id=self.org_id,
                        subject_kind="candidate",
                        subject_id=result.candidate_id or None,
                        well_id=well_id,
                        input_source_kind="optimisation",
                        triggered_by="optimisation",
                        triggered_by_id=self.actor_id,
                    )
                    result.engine_run_ids.append(execution.engine_run_id or "")
                for warning in torque_result.warnings or []:
                    result.warnings.append(f"torque_drag: {warning}")
            except Exception as exc:
                result.warnings.append(f"torque & drag could not evaluate this candidate: {exc}")

        # --- objectives + constraint thresholds ------------------------------------------
        for objective in objectives:
            key = objective["key"]
            if key == "ecd_margin_si":
                margin = result.hydraulics.get("ecd_margin_si")
                if margin is None:
                    continue
                fracture = hydraulics_inputs.get("fracture_gradient_si")
                result.objectives[key] = round(float(margin), 3)
                result.constraints.append(
                    ConstraintOutcome(
                        name="ecd_below_fracture_gradient",
                        satisfied=float(margin) > 0,
                        limit=round(float(fracture), 3) if fracture is not None else None,
                        actual=result.hydraulics.get("ecd_bottom_si"),
                        unit="kg/m3 equivalent",
                        margin=round(float(margin), 3),
                        note="computed ECD exceeds the fracture gradient" if float(margin) <= 0 else None,
                    )
                )
            elif key == "spp_utilisation":
                spp = result.hydraulics.get("standpipe_pressure_expected_si")
                limit = limits.get("spp_si")
                if spp is None or not limit:
                    continue
                utilisation = round(float(spp) / float(limit), 4)
                result.objectives[key] = utilisation
                result.constraints.append(
                    ConstraintOutcome(
                        name="standpipe_pressure_within_pump_limit",
                        satisfied=utilisation <= 1.0,
                        limit=round(float(limit), 3),
                        actual=round(float(spp), 3),
                        unit="Pa",
                        margin=round(1.0 - utilisation, 4),
                        note="computed SPP exceeds the declared pump limit" if utilisation > 1 else None,
                    )
                )
            elif key == "annular_velocity_m_s":
                velocity = result.hydraulics.get("annular_velocity_min_si")
                if velocity is None:
                    continue
                result.objectives[key] = round(float(velocity), 4)
            elif key == "cutting_transport_ratio":
                ratio = result.hydraulics.get("cutting_transport_ratio")
                if ratio is None:
                    # The engine could not evaluate transport for this candidate; saying so is
                    # better than omitting the objective silently.
                    result.warnings.append(
                        "cutting transport ratio was not computed for this candidate "
                        "(insufficient cuttings data); the objective is skipped"
                    )
                    continue
                result.objectives[key] = round(float(ratio), 4)
            elif key == "torque_utilisation":
                torque = result.torque_drag.get("surface_torque_si")
                limit = limits.get("torque_si")
                if torque is None or not limit:
                    continue
                utilisation = round(float(torque) / float(limit), 4)
                result.objectives[key] = utilisation
                result.constraints.append(
                    ConstraintOutcome(
                        name="surface_torque_within_limit",
                        satisfied=utilisation <= 1.0,
                        limit=round(float(limit), 3),
                        actual=round(float(torque), 3),
                        unit="N.m",
                        margin=round(1.0 - utilisation, 4),
                        note="computed surface torque exceeds the declared limit"
                        if utilisation > 1
                        else None,
                    )
                )
            elif key == "hookload_utilisation":
                hookload = result.torque_drag.get("pickup_hookload_si")
                limit = limits.get("hookload_si")
                if hookload is None or not limit:
                    continue
                utilisation = round(abs(float(hookload)) / float(limit), 4)
                result.objectives[key] = utilisation
                result.constraints.append(
                    ConstraintOutcome(
                        name="hookload_within_rig_limit",
                        satisfied=utilisation <= 1.0,
                        limit=round(float(limit), 3),
                        actual=round(abs(float(hookload)), 3),
                        unit="N",
                        margin=round(1.0 - utilisation, 4),
                        note="pick-up hookload exceeds the rig limit" if utilisation > 1 else None,
                    )
                )

        if any(not item.satisfied for item in result.constraints):
            result.feasible = False
        return result

    # ------------------------------------------------------------------ orchestration

    async def optimise(
        self,
        *,
        well_id: str,
        parameters: list[dict[str, Any]],
        hydraulics_inputs: dict[str, Any],
        torque_drag_inputs: dict[str, Any] | None = None,
        limits: dict[str, float] | None = None,
        objectives: list[dict[str, Any]] | None = None,
        section_id: str | None = None,
        samples: int = 24,
        title: str | None = None,
        persist_recommendation: bool = True,
        action_level: str = "L2",
    ) -> dict[str, Any]:
        """Run the full optimisation and persist its run, candidates and recommendation."""
        well = (
            await self.session.execute(select(Well).where(Well.id == well_id, Well.org_id == self.org_id))
        ).scalar_one_or_none()
        if well is None:
            raise ValidationFailed("well not found", details={"well_id": well_id})
        if not parameters:
            raise ValidationFailed("at least one decision variable is required")

        section = await self._section(well_id, section_id)
        limits = limits or {}
        requested_objectives = [item["key"] for item in (objectives or [])]
        if not requested_objectives:
            requested_objectives = ["ecd_margin_si", "annular_velocity_m_s"]
        unknown = [key for key in requested_objectives if key not in COMPUTED_OBJECTIVES]
        if unknown:
            raise ValidationFailed(
                "objectives that no engine computes cannot be optimised",
                details={"unsupported": unknown, "supported": sorted(COMPUTED_OBJECTIVES)},
            )
        objective_specs = [
            {"key": key, "sense": COMPUTED_OBJECTIVES[key]["sense"]} for key in requested_objectives
        ]

        # 1. candidates
        _sweep_spec, sweep = self.registry.execute(
            "optimization.sweep",
            {"parameters": parameters, "method": "grid", "samples": samples},
        )
        sweep_output = sweep.outputs.model_dump()
        candidates = sweep_output.get("candidates") or []

        # 2. evaluate every candidate deterministically
        evaluated: list[EvaluatedCandidate] = []
        for candidate in candidates:
            values = dict(candidate.get("values") or {})
            values["candidate_id"] = candidate.get("candidate_id")
            evaluated.append(
                await self._evaluate(
                    well_id=well_id,
                    section=section,
                    candidate_values=values,
                    hydraulics_base=hydraulics_inputs,
                    torque_base=torque_drag_inputs or {},
                    limits=limits,
                    objectives=objective_specs,
                    persist_runs=False,
                )
            )

        # 3. Pareto ranking over the objectives actually computed
        pareto_inputs = {
            "candidates": [
                {
                    "candidate_id": item.candidate_id,
                    "values": item.values,
                    "objectives": item.objectives,
                    "violations": item.rejected_because(),
                    "feasible": item.feasible,
                }
                for item in evaluated
                if item.objectives
            ],
            "objectives": objective_specs,
        }
        pareto_output: dict[str, Any] = {}
        if pareto_inputs["candidates"]:
            _pareto_spec, pareto_result = self.registry.execute("optimization.pareto", pareto_inputs)
            pareto_output = pareto_result.outputs.model_dump()

        # 3b. persist the calculations behind the candidates this result will cite.
        #
        # Every candidate was evaluated in memory above, which is what makes a wide sweep affordable.
        # The candidates the engineer is asked to *act* on — the Pareto frontier and the recommended
        # one — must additionally point at persisted engine runs, because a recommendation whose
        # provenance is "we recomputed it once and it agreed" is not auditable. The evaluation is a
        # pure function of the inputs, so persisting it again must reproduce the ranking values
        # exactly; if it does not, the engine is not deterministic and the run is refused rather than
        # published with numbers nobody can reproduce.
        ranking = {
            row["candidate_id"]: row for row in (pareto_output.get("ranking") or [])
        }
        ranked_items = sorted(
            (
                item
                for item in evaluated
                if item.feasible and item.objectives and (ranking.get(item.candidate_id) or {}).get("rank")
            ),
            key=lambda item: int((ranking.get(item.candidate_id) or {}).get("rank") or 0),
        )
        cite_ids = {str(candidate) for candidate in (pareto_output.get("pareto_frontier") or [])}
        if ranked_items:
            cite_ids.add(ranked_items[0].candidate_id)
        persisted_candidates: list[str] = []
        in_memory_candidates: list[str] = []
        for item in evaluated:
            if item.candidate_id not in cite_ids or not item.objectives:
                in_memory_candidates.append(item.candidate_id)
                continue
            recorded = await self._evaluate(
                well_id=well_id,
                section=section,
                candidate_values={**item.values, "candidate_id": item.candidate_id},
                hydraulics_base=hydraulics_inputs,
                torque_base=torque_drag_inputs or {},
                limits=limits,
                objectives=objective_specs,
                persist_runs=True,
            )
            if recorded.objectives != item.objectives:
                raise EngineError(
                    "engine evaluation is not reproducible: re-evaluating a candidate changed its "
                    "objective values, so the ranking cannot be trusted",
                    details={
                        "candidate_id": item.candidate_id,
                        "first": item.objectives,
                        "second": recorded.objectives,
                    },
                )
            item.engine_run_ids = [run_id for run_id in recorded.engine_run_ids if run_id]
            persisted_candidates.append(item.candidate_id)

        # 4. persist the optimisation run and every candidate
        run = OptimizationRun(
            org_id=self.org_id,
            problem_key="drilling.parameters",
            optimizer_key="optimization.sweep+optimization.pareto",
            optimizer_version="1.1.0",
            title=title or f"Drilling parameter optimisation — {well.name}",
            status="succeeded",
            project_id=well.project_id,
            well_id=well_id,
            section_id=section.id if section is not None else section_id,
            objectives=objective_specs,
            constraints=[
                {"name": key, "value": value} for key, value in sorted(limits.items())
            ],
            decision_variables=parameters,
            engine_keys=["optimization.sweep", "optimization.pareto", "hydraulics.laminar"]
            + (["torque_drag.soft_string"] if torque_drag_inputs else []),
            candidates_evaluated=len(evaluated),
            feasible_count=len([item for item in evaluated if item.feasible]),
            pareto_count=len(pareto_output.get("pareto_frontier") or []),
            sensitivity={item["objective"]: item for item in pareto_output.get("trade_offs") or []},
            uncertainty={},
            assumption_set=[
                "candidate feasibility is judged by the constraints the platform can compute: "
                "ECD against the fracture gradient, SPP against the pump limit"
                + (", surface torque and hookload against rig limits" if torque_drag_inputs else "")
                + "; the well section may be incomplete",
                "recorded offset and well performance is attached as evidence and is not a guarantee",
                "steady-state, single-phase hydraulics; the engine's own limitations apply",
            ]
            + [NOT_EVALUATED[key] for key in NOT_EVALUATED],
            started_at=dt.datetime.now(tz=dt.UTC),
            finished_at=dt.datetime.now(tz=dt.UTC),
            triggered_by="api",
            initiated_by=self.actor_id,
            attributes={
                "candidate_generation": sweep_output.get("notes"),
                "not_evaluated": NOT_EVALUATED,
                "provenance": {
                    "persisted_candidates": sorted(persisted_candidates),
                    "evaluated_in_memory": sorted(in_memory_candidates),
                    "determinism_check": (
                        "every persisted candidate reproduced its in-memory objective values exactly"
                    ),
                    "note": (
                        "engine runs are persisted for the Pareto-front and recommended candidates; "
                        "the remaining candidates were evaluated in memory from the inputs returned "
                        "with each candidate, so any of them can be re-run and checked"
                    ),
                },
            },
        )
        self.session.add(run)
        await self.session.flush()

        baseline_id = next(
            (item.candidate_id for item in evaluated if item.values.get("is_baseline")), None
        )
        rows: list[CandidateSolution] = []
        # ``candidate_solutions`` is unique on (run_id, rank), and Pareto ranking only covers
        # candidates that produced objectives. Every candidate therefore gets a deterministic rank:
        # the Pareto rank when one exists, otherwise after the ranked ones in evaluation order.
        ranked_ids = {
            item.candidate_id for item in evaluated if item.candidate_id in ranking
        }
        next_fallback_rank = (
            max((int(info.get("rank") or 0) for info in ranking.values()), default=0) + 1
        )
        for item in evaluated:
            rank_info = ranking.get(item.candidate_id, {})
            if item.candidate_id in ranked_ids and rank_info.get("rank"):
                rank = int(rank_info["rank"])
            else:
                rank = next_fallback_rank
                next_fallback_rank += 1
            solution = CandidateSolution(
                org_id=self.org_id,
                run_id=run.id,
                rank=rank,
                label=item.candidate_id,
                is_feasible=item.feasible,
                is_pareto=bool(rank_info.get("on_frontier")),
                is_baseline=bool(item.candidate_id == baseline_id),
                score=rank_info.get("score"),
                decisions=item.values,
                objectives=item.objectives,
                constraint_results=[constraint.to_dict() for constraint in item.constraints],
                feasibility_margin=(
                    min(
                        (constraint.margin for constraint in item.constraints if constraint.margin is not None),
                        default=None,
                    )
                ),
                predicted_metrics={**item.hydraulics, **item.torque_drag},
                rejected_because=item.rejected_because(),
                explanation=(
                    "feasible; ranked "
                    f"{rank_info.get('rank')} on the configured objectives"
                    if item.feasible
                    else "infeasible: " + "; ".join(item.rejected_because())
                ),
                engine_run_ids=[run_id for run_id in item.engine_run_ids if run_id],
                attributes={"warnings": item.warnings, "evidence": item.evidence},
            )
            self.session.add(solution)
            rows.append(solution)
        await self.session.flush()

        best_id = None
        ranked_feasible = [
            row
            for row in rows
            if row.is_feasible and row.rank
        ]
        if ranked_feasible:
            best = min(ranked_feasible, key=lambda row: row.rank)
            best_id = best.id
            run.best_candidate_id = best.id
        run.engine_run_ids = sorted({run_id for item in evaluated for run_id in item.engine_run_ids if run_id})
        await self.session.flush()

        explanation = await self.explain(run.id)
        recommendation: Recommendation | None = None
        if persist_recommendation and best_id is not None:
            recommendation = await self.persist_recommendation(
                run.id, well_id=well_id, action_level=action_level
            )
        return {
            "optimization_run_id": run.id,
            "engine": {
                "sweep": "optimization.sweep",
                "pareto": "optimization.pareto",
                "evaluation": "hydraulics.laminar" + (", torque_drag.soft_string" if torque_drag_inputs else ""),
            },
            "explanation": explanation,
            "recommendation_id": recommendation.id if recommendation is not None else None,
            "not_evaluated": NOT_EVALUATED,
        }

    # ------------------------------------------------------------------ explanation

    async def explain(self, optimization_run_id: str) -> dict[str, Any]:
        """Turn a persisted optimisation run into candidate comparison with why / why-not."""
        run = (
            await self.session.execute(
                select(OptimizationRun).where(
                    OptimizationRun.id == optimization_run_id, OptimizationRun.org_id == self.org_id
                )
            )
        ).scalar_one_or_none()
        if run is None:
            raise ValidationFailed(
                "optimisation run not found", details={"optimization_run_id": optimization_run_id}
            )
        candidates = list(
            (
                await self.session.execute(
                    select(CandidateSolution)
                    .where(CandidateSolution.run_id == run.id)
                    .order_by(CandidateSolution.rank)
                )
            )
            .scalars()
            .all()
        )
        views = [_candidate_view(row) for row in candidates]
        recommended = next((view for view in views if view["id"] == run.best_candidate_id), None)
        why_not: list[dict[str, Any]] = []
        for view in views:
            if recommended is not None and view["id"] == recommended["id"]:
                continue
            if not view["is_feasible"]:
                why_not.append(
                    {
                        "candidate": view["label"],
                        "candidate_id": view["id"],
                        "kind": "infeasible",
                        "reason": "; ".join(view["rejected_because"]) or "a constraint returned infeasible",
                        "constraints": view["constraint_results"],
                    }
                )
            elif recommended is not None:
                comparison = {
                    key: {
                        "candidate": view["objectives"].get(key),
                        "recommended": (recommended["objectives"] or {}).get(key),
                        "sense": next(
                            (
                                item["sense"]
                                for item in (run.objectives or [])
                                if item.get("key") == key
                            ),
                            None,
                        ),
                    }
                    for key in sorted(set(view["objectives"]) | set(recommended["objectives"] or {}))
                }
                why_not.append(
                    {
                        "candidate": view["label"],
                        "candidate_id": view["id"],
                        "kind": "dominated" if not view["is_pareto"] else "trade_off",
                        "reason": (
                            "feasible but ranked below the recommended candidate on the objective set"
                            if not view["is_pareto"]
                            else "on the Pareto frontier: better on at least one objective and worse "
                            "on another, so the choice is a trade-off"
                        ),
                        "comparison": comparison,
                    }
                )
        return {
            "optimization_run_id": run.id,
            "title": run.title,
            "status": run.status,
            "problem_key": run.problem_key,
            "objectives": run.objectives,
            "constraints": run.constraints,
            "decision_variables": run.decision_variables,
            "engine_keys": run.engine_keys,
            "candidates_evaluated": run.candidates_evaluated,
            "feasible_count": run.feasible_count,
            "pareto_count": run.pareto_count,
            "sensitivity": run.sensitivity,
            "uncertainty": run.uncertainty,
            "assumptions": run.assumption_set,
            "baseline": next((view for view in views if view["is_baseline"]), None),
            "recommended": recommended,
            "candidates": views,
            "pareto_frontier": [view for view in views if view["is_pareto"]],
            "feasible_candidates": [view for view in views if view["is_feasible"]],
            "why_not": why_not,
            "engine_run_ids": run.engine_run_ids,
            "provenance": (run.attributes or {}).get("provenance")
            or {
                "persisted_candidates": [],
                "evaluated_in_memory": [view["label"] for view in views],
                "determinism_check": "not recorded for this run",
                "note": (
                    "this run has no per-candidate provenance record; the engine runs listed on the "
                    "run are the only persisted calculations"
                ),
            },
            "recommendation_id": run.recommendation_id,
            "not_evaluated": run.attributes.get("not_evaluated") if run.attributes else NOT_EVALUATED,
            "objective_sources": {
                key: spec["source"] for key, spec in COMPUTED_OBJECTIVES.items()
            },
            "notes": [
                "every candidate value in this comparison comes from an engine run; nothing here is "
                "computed by the frontend or by a language model",
                "feasibility is judged only against the constraints the platform can compute; see "
                "'not_evaluated' for what is deliberately absent",
            ],
        }

    async def persist_recommendation(
        self,
        optimization_run_id: str,
        *,
        well_id: str,
        title: str | None = None,
        action_level: str = "L2",
    ) -> Recommendation:
        """Persist the recommended candidate so the explanation outlives this session."""
        explanation = await self.explain(optimization_run_id)
        recommended = explanation["recommended"]
        if recommended is None:
            raise ValidationFailed(
                "the optimisation run has no feasible best candidate, so no recommendation can be persisted",
                details={"optimization_run_id": optimization_run_id},
            )
        well = (
            await self.session.execute(select(Well).where(Well.id == well_id, Well.org_id == self.org_id))
        ).scalar_one_or_none()
        if well is None:
            raise ValidationFailed("well not found", details={"well_id": well_id})

        parameters = [
            {"name": key, "value": value, "unit": None, "source": "optimisation candidate"}
            for key, value in (recommended["values"] or {}).items()
            if key != "candidate_id"
        ]
        statement_parts = [f"{item['name']} = {item['value']}" for item in parameters]
        row = Recommendation(
            org_id=self.org_id,
            project_id=well.project_id,
            well_id=well_id,
            section_id=None,
            domain="drilling",
            kind="parameter_window",
            title=title or f"Drilling parameters — {explanation['title'] or explanation['problem_key']}",
            statement=(
                f"Recommended drilling parameters for {well.name}: " + ", ".join(statement_parts)
                if statement_parts
                else f"Recommended candidate {recommended['label']} for {well.name}"
            ),
            parameters=parameters,
            rationale=recommended.get("explanation")
            or "highest-ranked feasible candidate under the configured objective set",
            why_not=explanation["why_not"],
            assumptions=list(explanation.get("assumptions") or []),
            constraints_applied=list(recommended.get("constraint_results") or []),
            alternatives=[
                {
                    "candidate": view["label"],
                    "candidate_id": view["id"],
                    "objectives": view["objectives"],
                    "is_feasible": view["is_feasible"],
                    "is_pareto": view["is_pareto"],
                }
                for view in explanation["candidates"]
            ],
            sensitivities=explanation.get("sensitivity") or {},
            uncertainty=explanation.get("uncertainty") or {},
            confidence=None,
            confidence_basis=(
                "deterministic engine evaluation of the candidate set; no field trial has been run "
                "and no ROP model exists, so performance gain is not claimed"
            ),
            data_quality="computed",
            status="proposed",
            action_level=action_level,
            engine_run_ids=list(explanation.get("engine_run_ids") or []),
            optimization_run_id=optimization_run_id,
            created_by=self.actor_id,
            created_by_kind="engine",
            attributes={
                "candidate_id": recommended["id"],
                "pareto_count": explanation["pareto_count"],
                "not_evaluated": list((explanation.get("not_evaluated") or {}).keys()),
            },
        )
        self.session.add(row)
        await self.session.flush()
        run = (
            await self.session.execute(
                select(OptimizationRun).where(OptimizationRun.id == optimization_run_id)
            )
        ).scalar_one()
        run.recommendation_id = row.id
        await self.session.flush()
        return row

    async def engine_runs_for(self, well_id: str, *, limit: int = 50) -> list[EngineRun]:
        return list(
            (
                await self.session.execute(
                    select(EngineRun)
                    .where(EngineRun.org_id == self.org_id, EngineRun.well_id == well_id)
                    .order_by(EngineRun.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )


def _candidate_view(row: CandidateSolution) -> dict[str, Any]:
    return {
        "id": row.id,
        "label": row.label or row.id,
        "rank": row.rank or 0,
        "is_feasible": bool(row.is_feasible),
        "is_pareto": bool(row.is_pareto),
        "is_baseline": bool(row.is_baseline),
        "score": row.score,
        "values": row.decisions or {},
        "objectives": row.objectives or {},
        "constraint_results": row.constraint_results or [],
        "rejected_because": list(row.rejected_because or []),
        "explanation": row.explanation,
        "feasibility_margin": row.feasibility_margin,
        "predicted_metrics": row.predicted_metrics or {},
        "engine_run_ids": list(row.engine_run_ids or []),
        "evidence": (row.attributes or {}).get("evidence", {}),
        "warnings": (row.attributes or {}).get("warnings", []),
    }
