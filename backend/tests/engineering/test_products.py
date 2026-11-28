"""Deterministic schema/rule tests for structured engineering-product revisions."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import ValidationError

from drillai.engineering.products import (
    ControlledProcedurePayload,
    DrillingProgramPayload,
    EngineeringQuantity,
    normalize_payload,
    parse_product_payload,
    product_validation_findings,
    validation_state,
)


def quantity(
    value: float,
    unit: str,
    *,
    dimension: str = "length",
    source_kind: str = "operator_entered",
    source_ref: str | None = None,
) -> dict:
    return {
        "state": "known",
        "dimension": dimension,
        "value": value,
        "unit": unit,
        "source_kind": source_kind,
        "source_ref": source_ref,
    }


def valid_program_payload() -> dict:
    return {
        "purpose": "Structured synthetic program for schema tests.",
        "objectives": [
            {
                "sequence": 1,
                "statement": "Reach the explicitly entered target depth.",
                "target_md": quantity(2000.0, "m"),
            }
        ],
        "sections": [
            {
                "sequence": 1,
                "name": "8-1/2 in section",
                "hole_diameter": quantity(8.5, "in"),
                "planned_md_from": quantity(0.0, "m"),
                "planned_md_to": quantity(2000.0, "m"),
                "mud_density_min": quantity(9.0, "ppg", dimension="density"),
                "mud_density_max": quantity(12.0, "ppg", dimension="density"),
                "operating_parameters": [
                    {
                        "key": "flow_rate",
                        "label": "Explicit test flow rate",
                        "value": quantity(800.0, "gpm", dimension="volume_flow"),
                    }
                ],
                "planned_operations": [
                    {
                        "sequence": 1,
                        "activity": "Drill the section",
                        "purpose": "Synthetic test activity only.",
                        "acceptance_criteria": ["Use approved, sourced design inputs."],
                    }
                ],
            }
        ],
        "risks": [
            {
                "sequence": 1,
                "hazard": "Synthetic unresolved test hazard",
                "initiating_causes": ["unknown"],
                "consequences": ["unknown"],
                "safeguards": ["Requires qualified review."],
                "contingency": "Stop and consult the approved local procedure.",
                "owner_role": "drilling_supervisor",
            }
        ],
    }


def valid_procedure_payload() -> dict:
    return {
        "purpose": "Structured synthetic test procedure.",
        "scope_statement": "Synthetic test scope only; not operational instructions.",
        "prerequisites": [
            {
                "sequence": 1,
                "requirement": "Required authorization is recorded.",
                "verification": "A qualified person checks the approval record.",
            }
        ],
        "steps": [
            {
                "sequence": 1,
                "title": "Verify prerequisites",
                "instruction": "Review the explicitly linked approved revision.",
                "responsible_role": "drilling_supervisor",
                "preconditions": ["The referenced revision is readable."],
                "hazards": ["Out-of-date reference."],
                "control_measures": ["Check the immutable version ID and hash."],
                "verification_criteria": ["The revision ID and hash match the reviewed record."],
                "response_if_not_met": "Stop work and escalate to the responsible supervisor.",
                "hold_point": True,
            }
        ],
        "stop_work_conditions": [
            {
                "sequence": 1,
                "trigger": "The referenced program revision is no longer approved.",
                "immediate_response": "Stop; do not rewrite the reference automatically.",
                "escalation_role": "drilling_supervisor",
            }
        ],
    }


def test_known_quantity_preserves_entry_and_adds_only_registry_conversion() -> None:
    model = EngineeringQuantity.model_validate(quantity(8.5, "in"))
    normalized = normalize_payload(
        model,
        actor_id="usr_test",
        authored_at=dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.UTC),
    )

    assert normalized["value"] == 8.5
    assert normalized["unit"] == "in"
    assert normalized["value_si"] == pytest.approx(0.2159)
    assert normalized["canonical_unit"] == "m"
    assert normalized["source_kind"] == "operator_entered"
    assert normalized["entered_by"] == "usr_test"
    assert normalized["entered_at"] == "2026-01-02T03:04:00+00:00"


def test_unknown_and_conflicting_quantities_retain_state_without_selecting_a_value() -> None:
    unknown = EngineeringQuantity.model_validate(
        {"state": "unknown", "dimension": "length", "reason": "No value in the reviewed source."}
    )
    assert "value_si" not in normalize_payload(
        unknown, actor_id="usr_test", authored_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC)
    )

    conflicting = EngineeringQuantity.model_validate(
        {
            "state": "conflicting",
            "dimension": "density",
            "reason": "Two retained sources report different values.",
            "observations": [
                {"value": 10.0, "unit": "ppg", "source_kind": "source_document", "source_ref": "doc_source"},
                {"value": 1200.0, "unit": "kg/m3", "source_kind": "engine_run", "source_ref": "eng_source"},
            ],
        }
    )
    normalized = normalize_payload(
        conflicting,
        actor_id="usr_test",
        authored_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
    )
    assert normalized["state"] == "conflicting"
    assert "value_si" not in normalized
    assert [item["source_ref"] for item in normalized["observations"]] == ["doc_source", "eng_source"]
    assert normalized["observations"][0]["value_si"] == pytest.approx(10.0 * 119.82642731689663)
    assert normalized["observations"][1]["value_si"] == 1200.0


def test_quantity_schema_rejects_dimension_mismatch_and_missing_provenance() -> None:
    with pytest.raises(ValidationError):
        EngineeringQuantity.model_validate(quantity(10.0, "ppg", dimension="length"))
    with pytest.raises(ValidationError):
        EngineeringQuantity.model_validate(
            {"state": "known", "dimension": "length", "value": 10.0, "unit": "m"}
        )
    with pytest.raises(ValidationError):
        EngineeringQuantity.model_validate(
            {"state": "conflicting", "dimension": "length", "reason": "Not resolved.", "observations": []}
        )


def test_valid_program_has_no_blocking_structural_findings() -> None:
    model = parse_product_payload("program", valid_program_payload())
    assert isinstance(model, DrillingProgramPayload)
    normalized = normalize_payload(
        model,
        actor_id="usr_author",
        authored_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
    )

    findings = product_validation_findings("program", normalized)

    assert validation_state(findings) == "passed"
    assert findings == []
    assert normalized["sections"][0]["hole_diameter"]["value_si"] == pytest.approx(0.2159)
    assert normalized["sections"][0]["mud_density_min"]["canonical_unit"] == "kg/m3"


def test_program_validation_reports_sequence_depth_and_range_errors_without_reordering() -> None:
    payload = valid_program_payload()
    payload["objectives"][0]["sequence"] = 2
    payload["sections"][0]["planned_md_from"] = quantity(2200.0, "m")
    payload["sections"][0]["planned_md_to"] = quantity(2100.0, "m")
    payload["sections"][0]["mud_density_min"] = quantity(12.0, "ppg", dimension="density")
    payload["sections"][0]["mud_density_max"] = quantity(9.0, "ppg", dimension="density")
    model = parse_product_payload("program", payload)
    normalized = normalize_payload(
        model,
        actor_id="usr_author",
        authored_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
    )

    findings = product_validation_findings("program", normalized)
    codes = {finding.code for finding in findings}

    assert "sequence_not_contiguous" in codes
    assert "section_depth_order_invalid" in codes
    assert "mud_density_window_reversed" in codes
    assert validation_state(findings) == "blocked"
    assert [item["sequence"] for item in normalized["objectives"]] == [2]


def test_unknown_required_program_quantity_is_not_treated_as_zero_or_known() -> None:
    payload = valid_program_payload()
    payload["sections"][0]["hole_diameter"] = {
        "state": "unknown",
        "dimension": "length",
        "reason": "The source did not state a hole diameter.",
    }
    model = parse_product_payload("program", payload)
    normalized = normalize_payload(
        model,
        actor_id="usr_author",
        authored_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
    )

    findings = product_validation_findings("program", normalized)
    finding = next(item for item in findings if item.path == "sections.0.hole_diameter")

    assert finding.code == "required_quantity_not_known"
    assert "value_si" not in normalized["sections"][0]["hole_diameter"]
    assert validation_state(findings) == "blocked"


def test_procedure_has_structured_hold_points_and_blocks_superseded_program_link() -> None:
    model = parse_product_payload("procedure", valid_procedure_payload())
    assert isinstance(model, ControlledProcedurePayload)
    normalized = normalize_payload(
        model,
        actor_id="usr_author",
        authored_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
    )

    current = product_validation_findings("procedure", normalized, linked_program_state="approved")
    stale = product_validation_findings("procedure", normalized, linked_program_state="superseded")

    assert current == []
    assert validation_state(current) == "passed"
    assert any(item.code == "linked_program_revision_not_currently_approved" for item in stale)
    assert validation_state(stale) == "blocked"


def test_empty_procedure_is_not_a_submission_ready_free_text_form() -> None:
    model = parse_product_payload("procedure", {})
    normalized = normalize_payload(
        model,
        actor_id="usr_author",
        authored_at=dt.datetime(2026, 1, 2, tzinfo=dt.UTC),
    )

    codes = {item.code for item in product_validation_findings("procedure", normalized)}

    assert {"procedure_scope_missing", "procedure_steps_missing", "stop_work_conditions_missing"} <= codes


@pytest.mark.parametrize(
    ("next_top_m", "expect_overlap"),
    [(999.0, True), (1000.0, False), (1100.0, False)],
)
def test_program_sections_cannot_overlap_in_measured_depth(next_top_m, expect_overlap):
    import copy

    payload = valid_program_payload()
    first = payload["sections"][0]
    first["planned_md_to"] = quantity(1000, "m")
    second = copy.deepcopy(first)
    second.update(sequence=2, name="Next section", planned_md_from=quantity(next_top_m, "m"),
                  planned_md_to=quantity(2000, "m"))
    payload["sections"].append(second)
    model = parse_product_payload("program", payload)
    normalized = normalize_payload(model, actor_id="reviewer", authored_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    issues = product_validation_findings("program", normalized)
    overlap = [item for item in issues if item.code == "section_depth_overlap"]
    assert bool(overlap) is expect_overlap
    if expect_overlap:
        assert overlap[0].severity == "error"
        assert overlap[0].path == "sections.1.planned_md_from"
        assert validation_state(issues) == "blocked"


def test_program_section_boundary_converts_feet_without_inventing_overlap() -> None:
    import copy

    payload = valid_program_payload()
    first = payload["sections"][0]
    first["planned_md_to"] = quantity(1000.0, "m")
    second = copy.deepcopy(first)
    second.update(sequence=2, name="Converted boundary", planned_md_from=quantity(1000.0 / 0.3048, "ft"),
                  planned_md_to=quantity(2000.0, "m"))
    payload["sections"].append(second)
    normalized = normalize_payload(parse_product_payload("program", payload), actor_id="reviewer",
                                   authored_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    assert normalized["sections"][1]["planned_md_from"]["value_si"] == pytest.approx(1000.0)
    assert not any(issue.code == "section_depth_overlap" for issue in product_validation_findings("program", normalized))
