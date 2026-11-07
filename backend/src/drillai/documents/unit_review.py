"""Version-bound unit-inference review attestations."""

from __future__ import annotations

from typing import Any

_UNIT_INFERENCE_REVIEW_KEY = "_unit_inference_review"


def public_extracted_record_payload(payload: Any) -> dict[str, Any]:
    """Return client/context-safe extracted fields without private server-managed metadata."""
    if not isinstance(payload, dict):
        return {}
    return {key: value for key, value in payload.items() if key != _UNIT_INFERENCE_REVIEW_KEY}


def unit_inference_review_is_current(payload: Any, review_version: Any) -> bool:
    """Return true only for a server-managed attestation bound to this exact record version."""
    if not isinstance(payload, dict):
        return False
    review = payload.get(_UNIT_INFERENCE_REVIEW_KEY)
    return (
        isinstance(review, dict)
        and review.get("unit_inference_reviewed") is True
        and type(review.get("review_version")) is int
        and type(review_version) is int
        and review.get("review_version") == review_version
    )


def has_unreviewed_inferred_units(quality_flags: Any, payload: Any, review_version: Any) -> bool:
    """Return whether a record's inferred unit defaults lack a current human acknowledgement."""
    flags = quality_flags if isinstance(quality_flags, (list, tuple, set, frozenset)) else ()
    return "unit_inferred" in flags and not unit_inference_review_is_current(payload, review_version)
