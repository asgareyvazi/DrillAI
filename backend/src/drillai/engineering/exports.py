"""Deterministic, provenance-preserving exports for versioned engineering products."""

from __future__ import annotations

import json
from typing import Any

from drillai.db.models import Artifact, ArtifactVersion


def product_export_payload(
    artifact: Artifact,
    version: ArtifactVersion,
    *,
    linked_program_current_state: str | None = None,
) -> dict[str, Any]:
    summary = dict(version.validation_summary or {})
    return {
        "export_schema": "drillai.engineering_product_export.v1",
        "artifact": {
            "id": artifact.id,
            "kind": artifact.kind,
            "title": artifact.title,
            "code": artifact.code,
            "status": artifact.status,
            "project_id": artifact.project_id,
            "well_id": artifact.well_id,
            "wellbore_id": artifact.wellbore_id,
            "section_id": artifact.section_id,
            "operation_id": artifact.operation_id,
        },
        "revision": {
            "id": version.id,
            "number": version.version,
            "state": version.state,
            "schema_key": version.schema_key,
            "schema_version": version.schema_version,
            "content_hash": version.content_hash,
            "authored_by": version.authored_by,
            "authored_at": version.authored_at.isoformat() if version.authored_at else None,
            "change_reason": version.change_reason,
            "supersedes_version_id": version.supersedes_version_id,
            "payload": version.payload or {},
            "source_lineage": summary.get("source_lineage") or [],
            "linked_program_snapshot": summary.get("linked_program"),
            "linked_program_current_state": linked_program_current_state,
            "engine_run_ids": version.engine_run_ids or [],
            "validation": summary.get("validation"),
            "field_acceptance": (summary.get("validation") or {}).get("field_acceptance", "not_established"),
        },
        "export_limits": {
            "engineering_calculation": "not_performed_by_export",
            "approval": "not_granted_by_export",
            "operational_control": "not_available",
        },
    }


def render_product_json(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _quantity_text(value: Any) -> str:
    if not isinstance(value, dict):
        return "not provided"
    state = value.get("state", "unknown")
    if state == "known":
        entered = f"{value.get('value')} {value.get('unit')}"
        canonical = value.get("value_si")
        unit = value.get("canonical_unit")
        suffix = f"; canonical {canonical} {unit}" if canonical is not None and unit else ""
        source = value.get("source_ref") or value.get("source_kind") or "source not specified"
        return f"{entered}{suffix}; {source}"
    if state == "conflicting":
        observations = "; ".join(
            f"{item.get('value')} {item.get('unit')} ({item.get('source_ref') or item.get('source_kind')})"
            for item in value.get("observations", [])
            if isinstance(item, dict)
        )
        return f"conflicting; {value.get('reason') or 'reason not provided'}; {observations}"
    return f"unknown; {value.get('reason') or 'reason not provided'}"


def _list(items: Any) -> str:
    if not isinstance(items, list) or not items:
        return "- Not recorded."
    return "\n".join(f"- {item}" for item in items)


def render_product_markdown(payload: dict[str, Any]) -> bytes:
    artifact = payload["artifact"]
    revision = payload["revision"]
    content = revision.get("payload") or {}
    lines = [
        f"# {artifact['title']}",
        "",
        f"- Product: `{artifact['kind']}`",
        f"- Artifact ID: `{artifact['id']}`",
        f"- Revision: `{revision['number']}` (`{revision['id']}`)",
        f"- Revision state: `{revision['state']}`",
        f"- Schema: `{revision['schema_key']}@{revision['schema_version']}`",
        f"- Content SHA-256: `{revision['content_hash']}`",
        f"- Well ID: `{artifact['well_id']}`",
        f"- Project ID: `{artifact['project_id']}`",
        f"- Field acceptance: `{revision['field_acceptance']}`",
        "",
        f"## Purpose\n\n{content.get('purpose') or 'Not recorded.'}",
    ]
    if artifact["kind"] == "program":
        lines.extend(["", "## Objectives"])
        objectives = content.get("objectives") or []
        if objectives:
            for item in objectives:
                lines.append(f"- {item.get('sequence')}. {item.get('statement')}")
                if item.get("target_md") is not None:
                    lines.append(f"  - Target measured depth: {_quantity_text(item['target_md'])}")
        else:
            lines.append("- Not recorded.")
        lines.extend(["", "## Design basis"])
        for item in content.get("design_basis") or []:
            lines.append(f"- **{item.get('topic')}** ({item.get('basis')}): {item.get('statement')}")
            if item.get("reason"):
                lines.append(f"  - Reason: {item['reason']}")
            references = [f"{ref.get('kind')}:{ref.get('id')}" for ref in item.get("source_references", [])]
            lines.append(f"  - Source references: {_list(references)}")
        if not content.get("design_basis"):
            lines.append("- Not recorded.")
        lines.extend(["", "## Well sections"])
        sections = content.get("sections") or []
        if sections:
            for section in sections:
                lines.extend(
                    [
                        f"### {section.get('sequence')}. {section.get('name')}",
                        f"- Well-section ID: `{section.get('section_id') or 'not specified'}`",
                        f"- Hole diameter: {_quantity_text(section.get('hole_diameter'))}",
                        f"- Planned MD from: {_quantity_text(section.get('planned_md_from'))}",
                        f"- Planned MD to: {_quantity_text(section.get('planned_md_to'))}",
                        f"- Mud density minimum: {_quantity_text(section.get('mud_density_min'))}",
                        f"- Mud density maximum: {_quantity_text(section.get('mud_density_max'))}",
                        "- Operating parameters:",
                    ]
                )
                for parameter in section.get("operating_parameters") or []:
                    lines.append(f"  - {parameter.get('label')} (`{parameter.get('key')}`): {_quantity_text(parameter.get('value'))}")
                lines.append("- Planned operations:")
                for operation in section.get("planned_operations") or []:
                    lines.append(f"  - {operation.get('sequence')}. {operation.get('activity')}: {operation.get('purpose')}")
                    lines.append(f"    - Acceptance criteria: {_list(operation.get('acceptance_criteria'))}")
        else:
            lines.append("- Not recorded.")
        lines.extend(["", "## Risks"])
        for risk in content.get("risks") or []:
            lines.extend(
                [
                    f"- **{risk.get('sequence')}. {risk.get('hazard')}**",
                    f"  - Initiating causes: {_list(risk.get('initiating_causes'))}",
                    f"  - Consequences: {_list(risk.get('consequences'))}",
                    f"  - Safeguards: {_list(risk.get('safeguards'))}",
                    f"  - Contingency: {risk.get('contingency') or 'Not recorded.'}",
                ]
            )
    else:
        lines.extend(["", "## Scope", "", content.get("scope_statement") or "Not recorded.", "", "## Prerequisites"])
        for item in content.get("prerequisites") or []:
            lines.extend(
                [
                    f"- {item.get('sequence')}. {item.get('requirement')}",
                    f"  - Verification: {item.get('verification')}",
                ]
            )
        if not content.get("prerequisites"):
            lines.append("- Not recorded.")
        lines.extend(["", "## Controlled steps"])
        for step in content.get("steps") or []:
            lines.extend(
                [
                    f"### {step.get('sequence')}. {step.get('title')}",
                    f"- Responsible role: {step.get('responsible_role')}",
                    f"- Instruction: {step.get('instruction')}",
                    f"- Preconditions: {_list(step.get('preconditions'))}",
                    f"- Hazards: {_list(step.get('hazards'))}",
                    f"- Control measures: {_list(step.get('control_measures'))}",
                    f"- Verification: {_list(step.get('verification_criteria'))}",
                    f"- Response if not met: {step.get('response_if_not_met')}",
                    f"- Hold point: {bool(step.get('hold_point'))}",
                ]
            )
        lines.extend(["", "## Stop-work conditions"])
        for item in content.get("stop_work_conditions") or []:
            lines.extend(
                [
                    f"- {item.get('sequence')}. **Trigger:** {item.get('trigger')}",
                    f"  - Immediate response: {item.get('immediate_response')}",
                    f"  - Escalation role: {item.get('escalation_role')}",
                ]
            )
        if not content.get("stop_work_conditions"):
            lines.append("- Not recorded.")
    lines.extend(["", "## Exact linked program revision", ""])
    link = revision.get("linked_program_snapshot")
    if link:
        lines.extend(
            [
                f"- Artifact: `{link.get('artifact_id')}`",
                f"- Version: `{link.get('version')}` / `{link.get('version_id')}`",
                f"- Snapshot SHA-256: `{link.get('content_hash')}`",
                f"- State at link time: `{link.get('state_at_link')}`",
                f"- State at export time: `{revision.get('linked_program_current_state')}`",
                "- The reference is pinned to this revision and is not rewritten when a newer program exists.",
            ]
        )
    else:
        lines.append("- No linked program revision was recorded.")
    lines.extend(["", "## Source lineage", ""])
    lineage = revision.get("source_lineage") or []
    if lineage:
        for item in lineage:
            lines.append(
                f"- `{item.get('kind')}` `{item.get('id')}`; SHA/output hash `{item.get('sha256') or item.get('outputs_hash')}`; scope `{json.dumps(item.get('scope') or {}, sort_keys=True)}`"
            )
    else:
        lines.append("- No source documents or engine runs were recorded.")
    lines.extend(["", "## Validation", ""])
    validation = revision.get("validation")
    if validation:
        lines.append(f"- State: `{validation.get('state')}`; ruleset `{validation.get('ruleset')}`; validated at `{validation.get('validated_at')}`")
        for finding in validation.get("findings") or []:
            lines.append(f"- **{finding.get('severity')} — {finding.get('code')}**: {finding.get('message')}")
    else:
        lines.append("- Not validated.")
    lines.extend(
        [
            "",
            "> This export is a versioned authoring record. It does not perform calculations, grant approval, establish field acceptance, or control equipment.",
            "",
        ]
    )
    return ("\n".join(lines)).encode("utf-8")
