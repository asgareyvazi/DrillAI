"""Row → JSON shaping.

Explicit by design: every field a client receives is listed here, so widening the API is a
deliberate act and a database column can never leak into a response by accident. Timestamps are
ISO-8601 with an explicit offset (the columns are stored UTC by construction).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

__all__ = [
    "approval_out",
    "document_out",
    "engine_run_out",
    "evidence_out",
    "extracted_record_out",
    "ingestion_job_out",
    "node_run_out",
    "project_out",
    "recommendation_out",
    "run_event_out",
    "run_out",
    "section_out",
    "twin_aspect_out",
    "well_out",
    "wellbore_out",
    "workflow_out",
]


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat() if value.tzinfo else value.replace(tzinfo=dt.UTC).isoformat()


def project_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "code": row.code,
        "operator": row.operator,
        "country": row.country,
        "basin": row.basin,
        "status": row.status,
        "phase": row.phase,
        "description": row.description,
        "created_at": _iso(row.created_at),
    }


def well_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "name": row.name,
        "uwi": row.uwi,
        "well_type": row.well_type,
        "status": row.status,
        "spud_date": _iso(row.spud_date),
        "operator": row.operator,
        "is_offshore": row.is_offshore,
        "kb_elevation_si": row.kb_elevation_si,
        "total_depth_planned_si": row.total_depth_planned_si,
        "twin_state": row.twin_state,
        "objectives": row.objectives,
        "target_formations": list(row.target_formations or []),
        "tags": list(row.tags or []),
    }


def wellbore_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "well_id": row.well_id,
        "name": row.name,
        "purpose": row.purpose,
        "sequence": row.sequence,
        "status": row.status,
        "is_active": row.is_active,
        "planned_td_md_si": row.planned_td_md_si,
        "planned_td_tvd_si": row.planned_td_tvd_si,
        "actual_td_md_si": row.actual_td_md_si,
        "actual_td_tvd_si": row.actual_td_tvd_si,
        "kickoff_md_si": row.kickoff_md_si,
    }


def section_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "wellbore_id": row.wellbore_id,
        "sequence": row.sequence,
        "name": row.name,
        "kind": row.kind,
        "status": row.status,
        "hole_diameter_si": row.hole_diameter_si,
        "hole_diameter_nominal": row.hole_diameter_nominal,
        "planned_top_md_si": row.planned_top_md_si,
        "planned_bottom_md_si": row.planned_bottom_md_si,
        "actual_top_md_si": row.actual_top_md_si,
        "actual_bottom_md_si": row.actual_bottom_md_si,
        "current_md_si": row.current_md_si,
        "casing_od_nominal": row.casing_od_nominal,
        "casing_shoe_md_si": row.casing_shoe_md_si,
        "mud_weight_si": row.mud_weight_si,
        "pore_pressure_gradient_si": row.pore_pressure_gradient_si,
        "fracture_gradient_si": row.fracture_gradient_si,
        "is_planned_only": row.is_planned_only,
    }


def document_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "well_id": row.well_id,
        "wellbore_id": row.wellbore_id,
        "section_id": row.section_id,
        "doc_type": row.doc_type,
        "title": row.title,
        "document_number": row.document_number,
        "revision": row.revision,
        "issue_date": _iso(row.issue_date),
        "period_start": _iso(row.period_start),
        "period_end": _iso(row.period_end),
        "language": row.language,
        "page_count": row.page_count,
        "status": row.status,
        "extraction_summary": row.extraction_summary,
        "has_tables": row.has_tables,
        "has_figures": row.has_figures,
        "is_demo_fixture": row.is_demo_fixture,
        "created_at": _iso(row.created_at),
    }


def ingestion_job_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "document_id": row.document_id,
        "raw_artifact_id": row.raw_artifact_id,
        "status": row.status,
        "trigger": row.trigger,
        "pipeline": row.pipeline,
        "stats": row.stats or {},
        "extractor_versions": row.extractor_versions or {},
        "attempts": row.attempts,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "duration_ms": row.duration_ms,
        "error": row.error,
        "warnings": list(row.warnings or []),
        "trace_id": row.trace_id,
    }


def extracted_record_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "document_id": row.document_id,
        "record_type": row.record_type,
        "payload": row.payload,
        "payload_schema_key": row.payload_schema_key,
        "payload_schema_version": row.payload_schema_version,
        "page_number": row.page_number,
        "region_id": row.region_id,
        "depth_md_si": row.depth_md_si,
        "depth_tvd_si": row.depth_tvd_si,
        "observed_at": _iso(row.observed_at),
        "method": row.method,
        "method_version": row.method_version,
        "confidence": row.confidence,
        "validation_state": row.validation_state,
        # Where this record ended up. The document → record → domain-object chain is the product's
        # evidence story; without the promotion target the UI could only say "extracted", not
        # "this row became operation opr_… on this well".
        "promoted_to_kind": row.promoted_to_kind,
        "promoted_to_id": row.promoted_to_id,
        "unit_context": row.unit_context,
        "quality_flags": list(row.quality_flags or []),
        "is_demo_fixture": row.is_demo_fixture,
    }


def evidence_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "subject_kind": row.subject_kind,
        "subject_id": row.subject_id,
        "evidence_kind": row.evidence_kind,
        "evidence_id": row.evidence_id,
        "document_id": row.document_id,
        "region_id": row.region_id,
        "page_number": row.page_number,
        "well_id": row.well_id,
        "locator": row.locator,
        "excerpt": row.excerpt,
        "confidence": row.confidence,
        "relevance": row.relevance,
        "weight": row.weight,
        "method": row.method,
        "quote_verified": row.quote_verified,
        "created_at": _iso(row.created_at),
    }


def workflow_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "key": row.key,
        "name": row.name,
        "description": row.description,
        "category": row.category,
        "domain_pack": row.domain_pack,
        "project_id": row.project_id,
        "status": row.status,
        "current_version": row.current_version,
        "published_version_id": row.published_version_id,
        "is_template": row.is_template,
        "is_system_default": row.is_system_default,
        "is_editable": row.is_editable,
        "forked_from_id": row.forked_from_id,
        "owner": row.owner,
        "tags": list(row.tags or []),
        "permissions": list(row.permissions or []),
        "action_level": row.action_level,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def run_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "workflow_id": row.workflow_id,
        "workflow_version_id": row.workflow_version_id,
        "workflow_key": row.workflow_key,
        "version": row.version,
        "status": row.status,
        "trigger_type": row.trigger_type,
        "project_id": row.project_id,
        "well_id": row.well_id,
        "wellbore_id": row.wellbore_id,
        "operation_id": row.operation_id,
        "inputs": row.inputs or {},
        "outputs": row.outputs or {},
        "variables": row.variables or {},
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "duration_ms": row.duration_ms,
        "step_count": row.step_count,
        "error": row.error,
        "error_node_id": row.error_node_id,
        "cursor_node_id": row.cursor_node_id,
        "pending_approval_id": row.pending_approval_id,
        "is_dry_run": row.is_dry_run,
        "initiated_by": row.initiated_by,
        "approved_by": row.approved_by,
        "metrics": row.metrics or {},
    }


def node_run_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "node_id": row.node_id,
        "node_type": row.node_type,
        "node_name": row.node_name,
        "status": row.status,
        "attempt": row.attempt,
        "inputs": row.inputs or {},
        "outputs": row.outputs or {},
        "error": row.error,
        "error_code": row.error_code,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "duration_ms": row.duration_ms,
        "branch_taken": row.branch_taken,
        "loop_index": row.loop_index,
        "resolution": row.resolution,
        "engine_run_id": row.engine_run_id,
        "llm_call_id": row.llm_call_id,
        "tool_call_id": row.tool_call_id,
        "is_breakpoint": row.is_breakpoint,
    }


def run_event_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "seq": row.seq,
        "type": row.type,
        "node_id": row.node_id,
        "node_run_id": row.node_run_id,
        "message": row.message,
        "payload": row.payload or {},
        "level": row.level,
        "occurred_at": _iso(row.occurred_at),
    }


def approval_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "subject_kind": row.subject_kind,
        "subject_id": row.subject_id,
        "run_id": row.run_id,
        "node_run_id": row.node_run_id,
        "node_id": row.node_id,
        "project_id": row.project_id,
        "well_id": row.well_id,
        "title": row.title,
        "description": row.description,
        "action_level": row.action_level,
        "proposed_action": row.proposed_action,
        "request_payload": row.request_payload or {},
        "evidence_refs": list(row.evidence_refs or []),
        "required_role": row.required_role,
        "requested_by": row.requested_by,
        "requested_at": _iso(row.requested_at),
        "expires_at": _iso(row.expires_at),
        "status": row.status,
        "decided_by": row.decided_by,
        "decided_at": _iso(row.decided_at),
        "decision_note": row.decision_note,
    }


def recommendation_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "domain": row.domain,
        "kind": row.kind,
        "title": row.title,
        "statement": row.statement,
        "parameters": row.parameters or {},
        "rationale": row.rationale,
        "why_not": row.why_not or [],
        "assumptions": row.assumptions or [],
        "constraints_applied": row.constraints_applied or [],
        "alternatives": row.alternatives or [],
        "sensitivities": row.sensitivities or [],
        "uncertainty": row.uncertainty,
        "confidence": row.confidence,
        "confidence_basis": row.confidence_basis,
        "data_quality": row.data_quality,
        "status": row.status,
        "action_level": row.action_level,
        "engine_run_ids": list(row.engine_run_ids or []),
        "workflow_run_id": row.workflow_run_id,
        "offset_well_ids": list(row.offset_well_ids or []),
        "depth_from_md_si": row.depth_from_md_si,
        "depth_to_md_si": row.depth_to_md_si,
        "created_by": row.created_by,
        "created_by_kind": row.created_by_kind,
        "created_at": _iso(row.created_at),
    }


def engine_run_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "engine_key": row.engine_key,
        "engine_version": row.engine_version,
        "status": row.status,
        "subject_kind": row.subject_kind,
        "subject_id": row.subject_id,
        "well_id": row.well_id,
        "wellbore_id": row.wellbore_id,
        "section_id": row.section_id,
        "inputs": row.inputs or {},
        "outputs": row.outputs or {},
        "inputs_hash": row.inputs_hash,
        "outputs_hash": row.outputs_hash,
        "assumptions": list(row.assumptions or []),
        "limitations": list(row.limitations or []),
        "warnings": list(row.warnings or []),
        "constraint_violations": list(row.constraint_violations or []),
        "is_feasible": row.is_feasible,
        "input_source_kind": row.input_source_kind,
        "input_source_id": row.input_source_id,
        "triggered_by": row.triggered_by,
        "workflow_run_id": row.workflow_run_id,
        "node_run_id": row.node_run_id,
        "duration_ms": row.duration_ms,
        "created_at": _iso(row.created_at),
    }


def twin_aspect_out(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "well_id": row.well_id,
        "wellbore_id": row.wellbore_id,
        "section_id": row.section_id,
        "aspect": row.aspect,
        "state_kind": row.state_kind,
        "schema_key": row.schema_key,
        "schema_version": row.schema_version,
        "payload": row.payload or {},
        "summary": row.summary,
        "confidence": row.confidence,
        "data_quality": row.data_quality,
        "computed_at": _iso(row.computed_at),
        "computed_by": row.computed_by,
        "engine_run_id": row.engine_run_id,
        "source_refs": list(row.source_refs or []),
        "evidence_refs": list(row.evidence_refs or []),
        "assumptions": list(row.assumptions or []),
        "is_current": row.is_current,
        "content_hash": row.content_hash,
        "valid_from": _iso(row.valid_from),
        "valid_to": _iso(row.valid_to),
        "supersedes_id": row.supersedes_id,
    }
