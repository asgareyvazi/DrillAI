"""Version-bound review rules for inferred extraction units."""

from types import SimpleNamespace

import pytest

from drillai.core.errors import ValidationFailed
from drillai.documents.scope import validate_source_record
from drillai.documents.unit_review import public_extracted_record_payload, unit_inference_review_is_current


def _source_record(*, quality_flags=None, payload=None, review_version=1):
    return SimpleNamespace(
        validation_state="human_validated",
        confidence=0.95,
        quality_flags=quality_flags or [],
        payload=payload or {},
        review_version=review_version,
        project_id=None,
        well_id=None,
        wellbore_id=None,
        section_id=None,
        operation_id=None,
    )


def _validate_source(record):
    validate_source_record(
        record,
        project_id=None,
        well_id=None,
        wellbore_id=None,
        section_id=None,
        operation_id=None,
    )


def test_unit_inference_attestation_is_bound_to_current_integer_review_version():
    current = {"_unit_inference_review": {"unit_inference_reviewed": True, "review_version": 3}}
    assert unit_inference_review_is_current(current, 3)
    assert not unit_inference_review_is_current(current, 4)
    assert not unit_inference_review_is_current(
        {"_unit_inference_review": {"unit_inference_reviewed": True, "review_version": True}}, 1
    )
    assert not unit_inference_review_is_current(
        {"_unit_inference_review": {"unit_inference_reviewed": 1, "review_version": 1}}, 1
    )


def test_source_record_linkage_requires_review_and_public_payload_hides_attestation():
    unreviewed = _source_record(quality_flags=["unit_inferred"])
    with pytest.raises(ValidationFailed, match="inferred units without explicit review"):
        _validate_source(unreviewed)

    reviewed_payload = {
        "md_source_unit": None,
        "_unit_inference_review": {"unit_inference_reviewed": True, "review_version": 2},
    }
    reviewed = _source_record(
        quality_flags=["unit_inferred"], payload=reviewed_payload, review_version=2
    )
    _validate_source(reviewed)
    assert public_extracted_record_payload(reviewed_payload) == {"md_source_unit": None}

    near_match = _source_record(quality_flags=["not_unit_inferred"])
    _validate_source(near_match)
