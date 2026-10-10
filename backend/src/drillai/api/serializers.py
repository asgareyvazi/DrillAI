"""Row → JSON shaping.

Explicit by design: every field a client receives is listed here, so widening the API is a
deliberate act and a database column can never leak into a response by accident. Timestamps are
ISO-8601 with an explicit offset (the columns are stored UTC by construction).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from drillai.assets.vocabulary import SECTION_NUMBER_SEMANTICS

__all__ = [
    "approval_out",
    "audit_log_out",
    "channel_out",
    "document_out",
    "engine_run_out",
    "event_out",
    "evidence_out",
    "extracted_record_out",
    "field_out",
    "ingestion_job_out",
    "latest_reading_out",
    "node_run_out",
    "operation_out",
    "point_out",
    "project_out",
    "recommendation_out",
    "rig_out",
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
        # `updated_at` is part of the contract because two people can open the same master-data form:
        # a client sends back the version it read, and the server refuses the write if the row moved
        # underneath it. Without the column there is nothing to be stale *against*.
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def well_out(row: Any) -> dict[str, Any]:
    """One well, with every classified field the master-data contract exposes.

    The rules behind this shape: identifiers a person may search by are present (`uwi`,
    `api_number`, `name`); the location and datum block is present because a depth without a datum is
    not a depth; `field_id` and `rig_id` are present because they are the two references a well
    carries; and ``attributes`` — the free-form JSON bag — is deliberately *not* exposed. Nothing
    permanent is stored there, and offering it would create a second, unvalidated way to hold master
    data that the columns already model.
    """
    return {
        "id": row.id,
        "project_id": row.project_id,
        "field_id": row.field_id,
        "name": row.name,
        "uwi": row.uwi,
        "api_number": row.api_number,
        "well_type": row.well_type,
        "status": row.status,
        "spud_date": _iso(row.spud_date),
        "release_date": _iso(row.release_date),
        "operator": row.operator,
        "rig_id": row.rig_id,
        "is_offshore": row.is_offshore,
        "surface_lat": row.surface_lat,
        "surface_lon": row.surface_lon,
        "kb_elevation_si": row.kb_elevation_si,
        "ground_elevation_si": row.ground_elevation_si,
        "water_depth_si": row.water_depth_si,
        "elevation_datum": row.elevation_datum,
        "slot": row.slot,
        "pad_name": row.pad_name,
        "total_depth_planned_si": row.total_depth_planned_si,
        "twin_state": row.twin_state,
        "objectives": row.objectives,
        "target_formations": list(row.target_formations or []),
        "tags": list(row.tags or []),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def field_out(row: Any) -> dict[str, Any]:
    """A field, including the aliases that make it findable under the names people use for it."""
    return {
        "id": row.id,
        "project_id": row.project_id,
        "name": row.name,
        "aliases": list(row.aliases or []),
        "country": row.country,
        "basin": row.basin,
        "water_depth_si": row.water_depth_si,
        "centroid_lat": row.centroid_lat,
        "centroid_lon": row.centroid_lon,
        "status": row.status,
        "notes": row.notes,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def rig_out(row: Any) -> dict[str, Any]:
    """A rig, as reference data: enough to choose one and to see whether it is already working."""
    return {
        "id": row.id,
        "name": row.name,
        "contractor": row.contractor,
        "rig_type": row.rig_type,
        "status": row.status,
        "country": row.country,
        "current_well_id": row.current_well_id,
    }


def audit_log_out(row: Any) -> dict[str, Any]:
    """One governance-ledger entry: who did what, to which record, and what changed."""
    return {
        "id": row.id,
        "occurred_at": _iso(row.occurred_at),
        "actor_kind": row.actor_kind,
        "actor_id": row.actor_id,
        "actor_display": row.actor_display,
        "action": row.action,
        "action_level": row.action_level,
        "resource_kind": row.resource_kind,
        "resource_id": row.resource_id,
        "outcome": row.outcome,
        "permission_decision": dict(row.permission_decision or {}),
        "details": dict(row.details or {}),
        "before": dict(row.before or {}),
        "after": dict(row.after or {}),
        "request_id": row.request_id,
    }


def wellbore_out(row: Any) -> dict[str, Any]:
    """One wellbore.

    ``parent_wellbore_id`` is exposed because lineage is structured data and a client that cannot see
    the parent cannot draw the branch; ``datum`` is exposed because every depth on the wellbore is
    relative to it. Which wellbore is *current* is stated twice on purpose — ``is_active`` says the
    well's own answer, and the well carries ``active_wellbore_id`` when the structure is read — because
    "the hole being drilled" has to be unambiguous from either direction.
    """
    return {
        "id": row.id,
        "well_id": row.well_id,
        "name": row.name,
        "purpose": row.purpose,
        "sequence": row.sequence,
        "parent_wellbore_id": row.parent_wellbore_id,
        "status": row.status,
        "is_active": row.is_active,
        "datum": row.datum,
        "kickoff_md_si": row.kickoff_md_si,
        "planned_td_md_si": row.planned_td_md_si,
        "planned_td_tvd_si": row.planned_td_tvd_si,
        "actual_td_md_si": row.actual_td_md_si,
        "actual_td_tvd_si": row.actual_td_tvd_si,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def section_out(row: Any) -> dict[str, Any]:
    """One hole section, with every number labelled by what it *is*.

    The ``semantics`` map is the part that matters. A section carries plan depths, as-drilled
    measurements, an interpretation read off a leak-off test and the current position of the bit, and
    all four are metres. The classification comes from the server (``SECTION_NUMBER_SEMANTICS``) rather
    than from a client guessing by column name, so that "planned bottom" can never be rendered in the
    place of "current depth": a client that wants a current depth asks for ``current_md_si`` and sees
    ``null`` when nobody has recorded one, instead of borrowing the plan's number for it.
    """
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
        "casing_od_si": row.casing_od_si,
        "casing_od_nominal": row.casing_od_nominal,
        "casing_weight_si": row.casing_weight_si,
        "casing_grade": row.casing_grade,
        "casing_connection": row.casing_connection,
        "casing_top_md_si": row.casing_top_md_si,
        "casing_shoe_md_si": row.casing_shoe_md_si,
        "cement_top_md_si": row.cement_top_md_si,
        "cement_planned_top_md_si": row.cement_planned_top_md_si,
        "mud_weight_si": row.mud_weight_si,
        "mud_weight_min_si": row.mud_weight_min_si,
        "mud_weight_max_si": row.mud_weight_max_si,
        "pore_pressure_gradient_si": row.pore_pressure_gradient_si,
        "fracture_gradient_si": row.fracture_gradient_si,
        "collapse_gradient_si": row.collapse_gradient_si,
        "lot_fit_equivalent_mw_si": row.lot_fit_equivalent_mw_si,
        "pressure_source": row.pressure_source,
        "is_planned_only": row.is_planned_only,
        "notes": row.notes,
        "semantics": {key: kind for key, kind in SECTION_NUMBER_SEMANTICS.items() if hasattr(row, key)},
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
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
        # A client cannot show "this document still needs OCR before anything can be read from it"
        # without knowing it, and the ingestion summary is not the place for a fact that outlives the
        # run that discovered it.
        "ocr_required": row.ocr_required,
        # The artefact, not the document: two documents may share one. Exposing the id is what lets a
        # client see that the same bytes are filed against two wells, instead of deducing it from an
        # absence of information.
        "raw_artifact_id": row.raw_artifact_id,
        "logical_key": row.logical_key,
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
        # True when the extractor could not determine which region of the page the value came from.
        # `region_id` is then null — deliberately, rather than pointing at whichever region happened
        # to be nearby — and this flag is what tells a reader that the gap is known instead of
        # leaving them to guess from an absence.
        "region_unknown": row.region_unknown,
        "validation_rule": row.validation_rule,
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
        # Mechanical, not semantic: the excerpt was found in the region text, the page text, or
        # neither. `quote_check` records which of those was true.
        "quote_verified": row.quote_verified,
        "quote_check": row.quote_check,
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
        "section_id": row.section_id,
        "operation_id": row.operation_id,
        # The scope as it was recorded at start time. The individual columns are the queryable copy;
        # this is the record a reader can check against without joining anything.
        "context": row.context or {},
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
        # The approver decides on these three, so they have to leave the server: the risk the
        # requester declared, the evidence the decision should rest on, and — after a decision —
        # the conditions it was granted under. A decision recorded with conditions that the API
        # never returns would be a decision nobody can audit.
        "risk_notes": row.risk_notes,
        "evidence_refs": list(row.evidence_refs or []),
        # The column holds a list of conditions. Normalised here because the value crosses the wire
        # to clients that type it: an approval row may not be an object on one row and a list on the
        # next depending on which schema happened to have written it.
        "conditions": list(row.conditions) if isinstance(row.conditions, list) else [],
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


def operation_out(row: Any) -> dict[str, Any]:
    """One operation, with the fields that say *how* it is known and not only what it says.

    ``data_quality``, ``source_kind`` and ``promotion_fingerprint`` are included on purpose. An
    operation read from a report is not the same claim as one an engineer entered, and a client that
    cannot see which it is will render them identically — the exact failure the platform's evidence
    story exists to prevent.

    ``is_planned`` is returned as a derived convenience so a client never has to re-derive "was this
    done?" from the presence of dates: ``operation_class`` is the authority, and a plan is not history.
    """
    return {
        "id": row.id,
        "project_id": row.project_id,
        "well_id": row.well_id,
        "wellbore_id": row.wellbore_id,
        "section_id": row.section_id,
        "parent_operation_id": row.parent_operation_id,
        "predecessor_operation_id": row.predecessor_operation_id,
        "operation_class": row.operation_class,
        "is_planned": row.operation_class in {"plan", "forecast"},
        "sequence": row.sequence,
        "code": row.code,
        "name": row.name,
        "kind": row.kind,
        "phase": row.phase,
        "status": row.status,
        "planned_start": _iso(row.planned_start),
        "planned_end": _iso(row.planned_end),
        "actual_start": _iso(row.actual_start),
        "actual_end": _iso(row.actual_end),
        "planned_duration_hours": row.planned_duration_hours,
        "actual_duration_hours": row.actual_duration_hours,
        "depth_from_md_si": row.depth_from_md_si,
        "depth_to_md_si": row.depth_to_md_si,
        "hole_diameter_si": row.hole_diameter_si,
        "is_productive": row.is_productive,
        "npt_hours": row.npt_hours,
        "invisible_lost_time_hours": row.invisible_lost_time_hours,
        "cost_usd": float(row.cost_usd) if row.cost_usd is not None else None,
        "source": row.source,
        "source_kind": row.source_kind,
        "source_document_id": row.source_document_id,
        "source_record_id": row.source_record_id,
        "promotion_fingerprint": row.promotion_fingerprint,
        "data_quality": row.data_quality,
        "remarks": row.remarks,
        "is_demo_fixture": row.is_demo_fixture,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def event_out(row: Any) -> dict[str, Any]:
    """One event, keeping "what happened" separate from "how it is accounted for".

    ``kind`` is the event; ``npt_category`` and ``npt_hours`` are its accounting; ``cause_basis`` says
    who established the cause. The three are returned separately so no client can render an inferred
    cause as a recorded one, or charge an observation to NPT because it has a category.
    """
    return {
        "id": row.id,
        "project_id": row.project_id,
        "well_id": row.well_id,
        "wellbore_id": row.wellbore_id,
        "section_id": row.section_id,
        "operation_id": row.operation_id,
        "kind": row.kind,
        "category": row.category,
        "title": row.title,
        "description": row.description,
        "occurred_at": _iso(row.occurred_at),
        "ended_at": _iso(row.ended_at),
        "duration_hours": row.duration_hours,
        "depth_md_si": row.depth_md_si,
        "depth_tvd_si": row.depth_tvd_si,
        "severity": row.severity,
        "status": row.status,
        "is_npt": row.is_npt,
        "npt_code": row.npt_code,
        "npt_category": row.npt_category,
        # The spelling the source used, when it was not already canonical (CP9 unified the vocabulary and
        # translates raw spellings on write). Returned explicitly so a reader can audit the translation
        # instead of having to trust it; `null` means the value arrived canonical, which is the normal
        # case for anything written since.
        "npt_category_raw": (row.attributes or {}).get("npt_category_raw"),
        "npt_hours": row.npt_hours,
        "cost_usd": float(row.cost_usd) if row.cost_usd is not None else None,
        "root_cause": row.root_cause,
        "cause_basis": row.cause_basis,
        "classification_source": row.classification_source,
        "immediate_action": row.immediate_action,
        "corrective_action": row.corrective_action,
        "source": row.source,
        "source_kind": row.source_kind,
        "source_document_id": row.source_document_id,
        "source_record_id": row.source_record_id,
        "promotion_fingerprint": row.promotion_fingerprint,
        "evidence_ref": row.evidence_ref,
        "tags": list(row.tags or []),
        "is_demo_fixture": row.is_demo_fixture,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
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


# --------------------------------------------------------------------------- telemetry


def channel_out(row: Any, *, created: bool | None = None) -> dict[str, Any]:
    """One telemetry channel.

    ``scope`` is the token the platform's identity uses — ``well``, ``well/wellbore`` or
    ``well/wellbore/operation`` — returned explicitly rather than left for a client to reconstruct from
    three nullable ids, because a client that reconstructs identity will eventually reconstruct it
    differently. ``created`` is present only on a create answer, where "I made this" and "this already
    existed" are different facts about the same row.
    """

    payload = {
        "id": row.id,
        "well_id": row.well_id,
        "wellbore_id": row.wellbore_id,
        "operation_id": row.operation_id,
        "scope": row.scope_token,
        "channel_key": row.channel_key,
        "name": row.name,
        "dimension": row.dimension,
        "unit": row.unit,
        "source_unit": row.src_unit,
        "description": row.description,
        "is_realtime": row.is_realtime,
        "source": row.source,
        "source_ref": row.source_ref,
        "sampling_hint_seconds": row.sampling_hint_seconds,
        "first_ts": _iso(row.first_ts),
        "last_ts": _iso(row.last_ts),
        "point_count": row.point_count,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
    if created is not None:
        payload["created"] = created
    return payload


def point_out(row: Any) -> dict[str, Any]:
    """One measurement.

    Four things travel with the value and none of them is optional: the instant it was measured, the
    instant it was received, its quality, and whether it arrived late or out of order. ``value`` may be
    ``null`` — a quality-only record is a real record ("the sensor reported nothing"), and rendering it
    as a zero would invent a measurement.
    """

    return {
        "id": row.id,
        "channel_id": row.series_id,
        "ts": _iso(row.ts),
        "received_at": _iso(row.received_at),
        "value": row.value,
        "quality": row.quality,
        "quality_flags": [
            flag
            for flag, present in (("late", row.is_late), ("out_of_order", row.is_out_of_order))
            if present
        ],
        "sequence": row.sequence,
        "depth_md_si": row.depth_md_si,
        "source_point_id": row.source_point_id,
        "source_ref": row.source_ref,
        "identified_by": (row.attributes or {}).get("identified_by"),
        "source_value": row.src_value,
        "source_unit": row.src_unit,
        "revisions": len((row.attributes or {}).get("revisions", [])),
    }


def latest_reading_out(reading: Any) -> dict[str, Any]:
    """The newest value on one channel, with everything a screen must show beside it.

    The shape is deliberate: ``value``, ``unit``, ``observed_at``, ``received_at``, ``quality`` and
    ``freshness`` are one answer. A client that wanted only the number would have to ignore the rest on
    purpose, which is the opposite of how a plausible-looking stale reading reaches a rig floor.
    """

    return {
        "channel_id": reading.channel_id,
        "channel_key": reading.channel_key,
        "label": reading.label,
        "dimension": reading.dimension,
        "unit": reading.unit,
        "value": reading.value,
        "quality": reading.quality,
        "quality_flags": list(reading.quality_flags),
        "observed_at": _iso(reading.observed_at),
        "received_at": _iso(reading.received_at),
        "age_seconds": reading.age_seconds,
        "freshness": reading.freshness,
        "source": reading.source,
        "source_ref": reading.source_ref,
        "is_late": reading.is_late,
    }


# --------------------------------------------------------------------------- alerts


def alert_rule_out(row: Any) -> dict[str, Any]:
    """One rule, as data: the comparison, the durations and the clear line.

    The clear line is returned even when the rule did not set one explicitly, because the engine defaults
    it to the inverse of the raise condition — and an operator reading "what would clear this?" should get
    the answer the engine will actually use, not the absence of a field.
    """

    from drillai.telemetry.alerts import rule_spec_from_row

    spec = rule_spec_from_row(row)
    clear_operator, clear_threshold = spec.clear_line()
    return {
        "id": row.id,
        "rule_key": row.rule_key,
        "name": row.name,
        "description": row.description,
        "channel_key": row.channel_key,
        "well_id": row.well_id,
        "wellbore_id": row.wellbore_id,
        "operation_id": row.operation_id,
        "operator": row.operator,
        "threshold": row.threshold,
        "unit": row.unit,
        "clear_operator": clear_operator,
        "clear_threshold": clear_threshold,
        "clear_is_explicit": row.clear_threshold is not None,
        "sustain_seconds": row.sustain_seconds,
        "clear_sustain_seconds": row.clear_sustain_seconds,
        "cooldown_seconds": row.cooldown_seconds,
        "severity": row.severity,
        "enabled": row.is_enabled,
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def alert_out(row: Any) -> dict[str, Any]:
    """One alert, with everything needed to answer "why was this raised?" without a second request.

    ``transitioned_at`` is the instant of the *last* life-cycle action (acknowledged, cleared or
    cancelled) rather than the moment it was raised; ``version`` is that instant, which is what a client
    sends back as ``expected_updated_at`` to make its decision conflict-checked.
    """

    closed_at = row.cleared_at or row.acknowledged_at
    return {
        "id": row.id,
        "kind": row.kind,
        "severity": row.severity,
        "status": row.status,
        "title": row.title,
        "description": row.description,
        "action_level": row.action_level,
        "well_id": row.well_id,
        "wellbore_id": row.wellbore_id,
        "operation_id": row.operation_id,
        "section_id": row.section_id,
        "subject": {"kind": row.subject_kind, "id": row.subject_id},
        "series_id": row.series_id,
        "source_point_id": row.source_point_id,
        "rule_id": row.rule_id,
        "rule_ref": row.rule_ref,
        "observed": {
            "value": row.observed_value,
            "threshold": row.threshold_value,
            "unit": row.unit,
            # What the rule was written with: an alert that shows only 27 579 029 Pa makes a reader
            # reverse a unit conversion in their head, and the rule's own words are on the row already.
            "declared_threshold": (row.attributes or {}).get("rule_snapshot", {}).get("threshold"),
            "declared_unit": (row.attributes or {}).get("rule_snapshot", {}).get("unit"),
            "timestamp": _iso(row.observed_at),
            "sustained_seconds": row.sustained_seconds,
            "clear_value": row.clear_observed_value,
        },
        "raised_at": _iso(row.raised_at),
        "raised_by": row.raised_by,
        "acknowledged_at": _iso(row.acknowledged_at),
        "acknowledged_by": row.acknowledged_by,
        "cleared_at": _iso(row.cleared_at),
        "cancelled_reason": row.cancelled_reason,
        "reason": (row.attributes or {}).get("raise_reason"),
        "version": _iso(row.updated_at),
        "transitioned_at": _iso(closed_at),
        "evidence_url": f"/api/v1/alerts/{row.id}/evidence",
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }
