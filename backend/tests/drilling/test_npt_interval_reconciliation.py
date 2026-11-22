"""Recorded-interval reconciliation is separate from NPT cause attribution."""

import datetime as dt
from dataclasses import replace

import pytest

from drillai.drilling.npt_intervals import (
    MAX_RECONCILIATION_EVENTS,
    IntervalEvidence,
    reconcile_event_intervals,
)

START = dt.datetime(2026, 3, 15, tzinfo=dt.UTC)


def event(start, end):
    return IntervalEvidence(
        START + dt.timedelta(hours=start), START + dt.timedelta(hours=end),
        "source_record", "operator_entered", end - start, "reported",
    )


def test_union_nested_duplicate_adjacent_and_window():
    rows = [event(0, 4), event(2, 3), event(0, 4), event(4, 5), event(8, 9)]
    summary = reconcile_event_intervals(rows, since=START + dt.timedelta(hours=1), until=START + dt.timedelta(hours=8.5))
    assert summary["status"] == "complete_for_matching_events"
    assert summary["union_hours"] == 4.5
    assert summary["recorded_interval_hours"] == 8.5
    assert summary["overlap_hours"] == 4
    assert summary["overlap_event_count"] == 2
    assert summary["event_count"] == 5


@pytest.mark.parametrize("change", [
    {"start": None}, {"end": None}, {"start_basis": "legacy_unverified"},
    {"end_basis": "duration_replayed"}, {"npt_hours": None}, {"npt_hours": 0.5},
    {"npt_hours": float("nan")}, {"hours_basis": "planned"}, {"hours_basis": "legacy_unverified"},
    {"start": START.replace(tzinfo=None)}, {"end": START},
])
def test_incomplete_evidence_prevents_any_partial_union(change):
    summary = reconcile_event_intervals([event(0, 1), replace(event(2, 3), **change)])
    assert summary["status"] == "unavailable_incomplete"
    assert summary["invalid_event_count"] == 1
    assert summary["union_hours"] is None
    assert summary["overlap_hours"] is None


def test_empty_and_bounded_scope():
    assert reconcile_event_intervals([])["union_hours"] == 0
    result = reconcile_event_intervals([event(0, 1)] * (MAX_RECONCILIATION_EVENTS + 1))
    assert result["status"] == "unavailable_limit"
    assert result["union_hours"] is None
