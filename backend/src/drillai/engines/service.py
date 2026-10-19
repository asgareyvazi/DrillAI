"""Engine execution with provenance.

Every engine execution — whether it was requested by a person through the API, by an agent through
a tool call, or by a workflow node — is persisted as an :class:`EngineRun` row carrying the engine
key/version, canonical inputs and outputs, their content hashes, warnings, constraint violations
and the declared assumptions and limitations.

Two reasons this lives in one service rather than at each call site:

* **auditability** — "which numbers were produced, from which inputs, by which engine version"
  must be answerable for agent-proposed calculations too, not only for workflow runs;
* **no duplicate maths** — inputs are validated against the engine's own Pydantic model, and the
  canonical SI payload is what gets hashed, so a later reproducibility check cannot drift.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import UTC
from drillai.core.ids import new_id
from drillai.core.serialization import content_hash
from drillai.db.models import EngineRun
from drillai.engines.registry import EngineRegistry, load_default_engines

__all__ = ["EngineExecution", "engine_catalogue", "execute_engine"]


def _infer_si_unit_for_field_name(name: str) -> str | None:
    if name.endswith("_deg_per_30m"):
        return "deg/30m"
    if name.endswith("_deg"):
        return "deg"
    if name.endswith("_psi"):
        return "psi"
    if name.endswith("_ppg"):
        return "ppg"
    if name.endswith("_kpa"):
        return "kPa"
    if name.endswith("_lpm"):
        return "L/min"
    if name.endswith("_m3_per_m") or name.endswith("_capacity_si"):
        return "m3/m"
    if not name.endswith("_si"):
        return None
    if any(
        tok in name
        for tok in (
            "pressure",
            "sidpp",
            "sicp",
            "maasp",
            "mse",
            "strength",
            "modulus",
            "yield_point",
            "stress",
            "integrity",
            "shoe_margin",
        )
    ):
        return "Pa"
    if any(tok in name for tok in ("mud_weight", "ecd", "density")):
        return "kg/m3"
    if any(tok in name for tok in ("flow_rate", "kill_rate")):
        return "m3/s"
    if "rop" in name:
        return "m/h"
    if "velocity" in name:
        return "m/s"
    if any(tok in name for tok in ("wob", "hookload", "tension", "drag", "force", "weight_air", "weight_buoyed")):
        return "N"
    if "torque" in name:
        return "N.m"
    if "horsepower_per_area" in name:
        return "W/m2"
    if "horsepower" in name:
        return "W"
    if "plastic_viscosity" in name:
        return "Pa.s"
    if "area" in name or name == "tfa_si" or "capacity_at_shoe" in name:
        return "m2"
    if any(tok in name for tok in ("volume", "volumetric", "pit_gain", "output_per_stroke")):
        return "m3"
    if "time" in name:
        return "s"
    if "gradient" in name:
        return "Pa/m"
    if "linear_mass" in name:
        return "kg/m"
    if "rpm" in name:
        return "rpm"
    return "m"


def _extract_model_units(model_cls: type[BaseModel]) -> dict[str, str]:
    units: dict[str, str] = {}
    for fname, finfo in model_cls.model_fields.items():
        extra = finfo.json_schema_extra
        if isinstance(extra, dict) and isinstance(extra.get("unit"), str):
            units[fname] = extra["unit"]
        else:
            inferred = _infer_si_unit_for_field_name(fname)
            if inferred is not None:
                units[fname] = inferred
    return units


@dataclass
class EngineExecution:
    """The engine result plus the audit trail that was written for it."""

    engine_key: str
    engine_version: str
    outputs: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    is_feasible: bool = True
    inputs_hash: str = ""
    outputs_hash: str = ""
    engine_run_id: str | None = None
    validation_status: str = "verified_against_reference"
    domain_pack: str = ""
    category: str = ""
    units: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Canonical response body (also the shape a workflow node exposes to references)."""
        return {
            "engine_key": self.engine_key,
            "engine_version": self.engine_version,
            "domain_pack": self.domain_pack,
            "category": self.category,
            "validation_status": self.validation_status,
            "units": self.units,
            "result": self.outputs,
            "is_feasible": self.is_feasible,
            "warnings": self.warnings,
            "violations": self.violations,
            "assumptions": self.assumptions,
            "limitations": self.limitations,
            "engine_run_id": self.engine_run_id,
            "inputs_hash": self.inputs_hash,
            "outputs_hash": self.outputs_hash,
        }


async def execute_engine(
    session: AsyncSession,
    *,
    engine_key: str,
    inputs: BaseModel | dict[str, Any],
    org_id: str,
    subject_kind: str = "none",
    subject_id: str | None = None,
    project_id: str | None = None,
    well_id: str | None = None,
    wellbore_id: str | None = None,
    section_id: str | None = None,
    operation_id: str | None = None,
    input_source_kind: str = "api",
    input_source_id: str | None = None,
    triggered_by: str = "api",
    triggered_by_id: str | None = None,
    workflow_run_id: str | None = None,
    node_run_id: str | None = None,
    agent_run_id: str | None = None,
    registry: EngineRegistry | None = None,
    persist: bool = True,
) -> EngineExecution:
    """Run an engine and persist the resulting :class:`EngineRun` row (when ``persist=True``)."""
    registry = registry or load_default_engines()
    spec, result = registry.execute(engine_key, inputs)
    input_payload = (
        inputs.model_dump(mode="json") if isinstance(inputs, BaseModel) else dict(inputs)
    )
    output_payload = result.outputs.model_dump(mode="json")
    inputs_hash = content_hash(input_payload)
    outputs_hash = content_hash(output_payload)
    units_map: dict[str, Any] = {
        "unit_system": "SI",
        "input_units": _extract_model_units(spec.inputs_model),
        "output_units": _extract_model_units(spec.outputs_model),
    }

    engine_run_id: str | None = None
    if persist:
        row = EngineRun(
            id=new_id("eng"),
            org_id=org_id,
            engine_key=spec.key,
            engine_version=spec.version,
            domain_pack=spec.domain_pack,
            status="succeeded" if result.is_feasible else "succeeded_with_violations",
            subject_kind=subject_kind,
            subject_id=subject_id or well_id or project_id or org_id,
            project_id=project_id,
            well_id=well_id,
            wellbore_id=wellbore_id,
            section_id=section_id,
            operation_id=operation_id,
            inputs=input_payload,
            outputs=output_payload,
            inputs_hash=inputs_hash,
            outputs_hash=outputs_hash,
            units=units_map,
            assumptions=list(result.assumptions_applied),
            limitations=list(spec.limitations),
            warnings=list(result.warnings),
            constraint_violations=[violation.model_dump(mode="json") for violation in result.violations],
            is_feasible=result.is_feasible,
            input_source_kind=input_source_kind,
            input_source_id=input_source_id,
            triggered_by=triggered_by,
            triggered_by_id=triggered_by_id,
            workflow_run_id=workflow_run_id,
            node_run_id=node_run_id,
            agent_run_id=agent_run_id,
            started_at=dt.datetime.now(tz=UTC),
            finished_at=dt.datetime.now(tz=UTC),
        )
        session.add(row)
        await session.flush()
        engine_run_id = row.id
    return EngineExecution(
        engine_key=spec.key,
        engine_version=spec.version,
        outputs=output_payload,
        warnings=list(result.warnings),
        violations=[violation.model_dump(mode="json") for violation in result.violations],
        assumptions=list(result.assumptions_applied),
        limitations=list(spec.limitations),
        is_feasible=result.is_feasible,
        inputs_hash=inputs_hash,
        outputs_hash=outputs_hash,
        engine_run_id=engine_run_id,
        validation_status=spec.validation_status,
        domain_pack=spec.domain_pack,
        category=spec.category,
        units=units_map,
    )


def engine_catalogue() -> list[dict[str, Any]]:
    """Full engine catalogue (schemas included) for registry/UIs."""
    return load_default_engines().describe_all()
