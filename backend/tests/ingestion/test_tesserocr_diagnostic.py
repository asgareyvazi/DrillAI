"""Pass attribution for the optional, synthetic-only native-library diagnostic."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest


@pytest.fixture
def diagnostic():
    script = Path(__file__).resolve().parents[3] / "scripts" / "diagnose_synthetic_ocr_tesserocr.py"
    return runpy.run_path(str(script))


def test_synthetic_passes_are_attributed_only_in_complete_order(diagnostic):
    cases = [{"id": "a", "status": "evaluated"}, {"id": "b", "status": "evaluated"}]
    events = [
        {"languages": "eng+fas", "psm": 6},
        {"languages": "eng+fas", "psm": 11},
        {"languages": "fas", "psm": 11},
    ]
    groups = diagnostic["_group_passes"](events, cases)
    assert groups == [[events[0]], events[1:]]
    assert cases == [{"id": "a", "status": "evaluated"}, {"id": "b", "status": "evaluated"}]

    scored = [{"status": "evaluated", "language": "fas", "critical_fields_expected": 2, "critical_fields_matched": 1}]
    records = [{"language": "fas", "field_matches": [{"key": "date", "matched": True}, {"key": "depth", "matched": False}]}]
    assert diagnostic["_attribute_fields"](scored, records) is True
    assert diagnostic["_attribute_fields"](scored, []) is False
    assert diagnostic["_attribute_fields"](scored, [{**records[0], "language": "eng"}]) is False
    assert diagnostic["_attribute_fields"]([{**scored[0], "critical_fields_matched": 2}], records) is False


def test_synthetic_pass_attribution_rejects_incomplete_or_unexpected_traces(diagnostic):
    cases = [{"id": "a", "status": "evaluated"}, {"id": "b", "status": "evaluated"}]
    invalid = [
        ([{"languages": "eng+fas", "psm": 6}], cases),
        ([{"languages": "fas", "psm": 6}], cases),
        ([{"languages": "eng+fas", "psm": 6}, {"languages": "fas", "psm": 11}], cases),
        ([{"languages": "eng+fas", "psm": 6, "text": "do not store"}], cases),
        ([{"languages": "eng+fas", "psm": 6}], [{"id": "a", "status": "failed"}]),
    ]
    for events, inputs in invalid:
        with pytest.raises(ValueError):
            diagnostic["_group_passes"](events, inputs)
