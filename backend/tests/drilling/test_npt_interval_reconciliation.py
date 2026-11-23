"""Recorded-interval reconciliation is separate from NPT cause attribution."""

import datetime as dt
from dataclasses import replace

import pytest

from drillai.drilling.npt_intervals import (
    MAX_BLOCKER_DETAILS,
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
    assert sum(summary["blocker_reason_counts"].values()) == 1
    assert len(summary["blockers"]) == 1
    assert not summary["blockers_truncated"]


def test_empty_and_bounded_scope():
    empty = reconcile_event_intervals([])
    assert empty["status"] == "unavailable_no_evidence"
    assert empty["union_hours"] is None  # No recorded evidence does not establish physical zero.
    result = reconcile_event_intervals([event(0, 1)] * (MAX_RECONCILIATION_EVENTS + 1))
    assert result["status"] == "unavailable_limit"
    assert result["union_hours"] is None
    assert result["blockers"] is None  # A limited prefix is not a complete assessment.
    assert result["blocker_reason_counts"] is None

    changes = [
        ({"start": None}, "invalid_or_unqualified_time"),
        ({"end": START}, "unordered_time"),
        ({"npt_hours": None}, "missing_npt_hours"),
        ({"hours_basis": "planned"}, "unqualified_npt_hours"),
        ({"npt_hours": float("nan")}, "invalid_npt_hours"),
        ({"npt_hours": 0.5}, "npt_hours_mismatch"),
    ]
    for change, reason in changes:
        row = replace(event(0, 1), event_id="event-1", source_document_id="doc-1", source_record_id="record-1", **change)
        rec = reconcile_event_intervals([row])
        assert rec["blocker_reason_counts"] == {reason: 1}
        assert rec["blockers"] == [{"event_id": "event-1", "source_document_id": "doc-1",
                                     "source_record_id": "record-1", "reason": reason}]
    many = reconcile_event_intervals([replace(event(0, 1), start=None)] * (MAX_BLOCKER_DETAILS + 2))
    assert many["blocker_reason_counts"] == {"invalid_or_unqualified_time": MAX_BLOCKER_DETAILS + 2}
    assert len(many["blockers"]) == MAX_BLOCKER_DETAILS
    assert many["blockers_truncated"] is True


def test_timezone_offsets_touching_and_partial_window_clipping():
    offset = dt.timezone(dt.timedelta(hours=3, minutes=30))
    first = event(0, 2)
    shifted = replace(event(2, 4), start=event(2, 4).start.astimezone(offset), end=event(2, 4).end.astimezone(offset))
    summary = reconcile_event_intervals([shifted, first], since=START + dt.timedelta(minutes=30), until=START + dt.timedelta(hours=3))
    assert summary["union_hours"] == 2.5
    assert summary["recorded_interval_hours"] == 2.5
    assert summary["overlap_hours"] == 0
    assert summary["overlap_event_count"] == 0


def test_missing_explicit_npt_hours_are_counted_without_publishing_partial_union():
    summary = reconcile_event_intervals([event(0, 1), replace(event(2, 3), npt_hours=None, hours_basis="unknown")])
    assert summary["invalid_event_count"] == 1
    assert summary["missing_npt_hours_count"] == 1
    assert summary["union_hours"] is None


def test_interval_window_rejects_naive_equal_and_reversed_bounds():
    with pytest.raises(ValueError, match="naive"):
        reconcile_event_intervals([event(0, 1)], since=START.replace(tzinfo=None))
    with pytest.raises(ValueError, match="earlier"):
        reconcile_event_intervals([event(0, 1)], since=START, until=START)
    with pytest.raises(ValueError, match="earlier"):
        reconcile_event_intervals([event(0, 1)], since=START + dt.timedelta(hours=1), until=START)


def test_window_classifies_outside_overlap_and_unassignable_without_hiding_unknowns():
    rows = [event(0, 2), event(2, 4), replace(event(10, 11), npt_hours=None),
            replace(event(20, 21), start_basis="legacy_unverified")]
    result = reconcile_event_intervals(rows, since=START + dt.timedelta(hours=1), until=START + dt.timedelta(hours=3))
    assert result["status"] == "unavailable_incomplete"
    assert result["union_hours"] is None
    assert result["scoped_event_count"] == 4
    assert result["overlapping_window_count"] == 2
    assert result["outside_window_count"] == 1
    assert result["unassignable_time_count"] == 1
    assert result["missing_npt_hours_count"] == 0  # The qualified outside interval cannot overlap.
    assert result["invalid_event_count"] == 1


def test_window_with_only_qualified_outside_events_is_not_measured_zero():
    result = reconcile_event_intervals([event(0, 1), event(4, 5)], since=START + dt.timedelta(hours=1), until=START + dt.timedelta(hours=4))
    assert result["status"] == "unavailable_no_evidence"
    assert result["union_hours"] is None
    assert result["outside_window_count"] == 2
    assert result["overlapping_window_count"] == 0
