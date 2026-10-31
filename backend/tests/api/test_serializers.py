"""Serializer boundaries must emit strict JSON even for corrupt/non-finite stored values."""

from __future__ import annotations

import json
import math

from drillai.api import serializers
from drillai.api.serializers import bounded_json_preview


def test_bounded_json_preview_replaces_nonfinite_numbers_and_marks_the_preview_incomplete() -> None:
    preview, truncated = bounded_json_preview(
        {"nan": float("nan"), "positive_infinity": float("inf"), "negative_infinity": -math.inf, "valid": 1.25}
    )

    assert preview == {
        "nan": None,
        "positive_infinity": None,
        "negative_infinity": None,
        "valid": 1.25,
    }
    assert truncated is True
    # JSONResponse uses strict JSON encoding; this must not leak a Python NaN/Infinity token.
    assert json.dumps(preview, allow_nan=False)


def test_bounded_json_preview_preserves_finite_numbers_without_a_truncation_claim() -> None:
    preview, truncated = bounded_json_preview({"integer": 12, "finite": 1.5, "flag": True})

    assert preview == {"integer": 12, "finite": 1.5, "flag": True}
    assert truncated is False


def test_bounded_json_preview_never_encodes_the_unbounded_source_string(monkeypatch) -> None:
    original_dumps = serializers.json.dumps
    encoded_string_lengths: list[int] = []

    def watched_dumps(value, *args, **kwargs):
        if isinstance(value, str):
            encoded_string_lengths.append(len(value))
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(serializers.json, "dumps", watched_dumps)
    preview, truncated = bounded_json_preview("x" * 1_000_000, max_bytes=128, max_string_chars=64)

    assert preview == "x" * 64 + "…"
    assert truncated is True
    assert encoded_string_lengths
    assert max(encoded_string_lengths) <= 65


def test_bounded_json_preview_replaces_an_unserializable_extreme_integer() -> None:
    # This number exceeds Python's decimal-conversion safety limit on recent runtimes.
    value = 10**5000
    preview, truncated = bounded_json_preview({"value": value})

    assert preview == {"value": None}
    assert truncated is True
