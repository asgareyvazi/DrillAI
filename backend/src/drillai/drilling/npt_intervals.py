"""Conservative reconciliation of source-qualified NPT event intervals.

This reports elapsed interval union separately from legacy recorded-row sums. It never infers
which overlapping cause owns time, never interprets an operation's entire duration as NPT, and
returns an unavailable total when even one matched row cannot establish a full NPT interval.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

from drillai.core.clock import ensure_utc
from drillai.operations.time_windows import TIME_WINDOW_BASES

MAX_RECONCILIATION_EVENTS = 10_000
MAX_BLOCKER_DETAILS = 100
# Timestamp and duration rounding can differ by at most one second without treating
# the entire event as an unrelated partial NPT duration.
MAX_DURATION_DIFFERENCE_SECONDS = 1.0


@dataclass(frozen=True)
class IntervalEvidence:
    start: dt.datetime | None
    end: dt.datetime | None
    start_basis: str
    end_basis: str
    npt_hours: float | None
    hours_basis: str
    event_id: str | None = None
    source_document_id: str | None = None
    source_record_id: str | None = None


def _explicit_npt_row_sum(
    rows: list[IntervalEvidence], *, since: dt.datetime | None, until: dt.datetime | None
) -> dict[str, Any]:
    """Sum explicitly reported/observed NPT quantities without assuming event duration is NPT.

    This is a row sum, not a union, cause allocation, reviewed fact, or physical downtime. A
    time-filtered query cannot assign a row with unknown/unqualified endpoints to its window.
    Full row hours are included for overlapping windows, without proration. Incomplete records
    never become zero, and the partial subtotal is explicitly distinguished from a complete sum.
    """
    if len(rows) > MAX_RECONCILIATION_EVENTS:
        return {"status": "unavailable_limit", "known_row_sum_hours": None,
                "qualified_event_count": None, "missing_hours_count": None,
                "unqualified_hours_count": None, "invalid_hours_count": None,
                "unassignable_window_count": None, "outside_window_count": None}
    hours: list[float] = []
    missing = unqualified = invalid = unassignable = outside = 0
    for row in rows:
        if since is not None or until is not None:
            try:
                if (row.start is None or row.end is None or row.start_basis not in TIME_WINDOW_BASES
                        or row.end_basis not in TIME_WINDOW_BASES):
                    raise ValueError("unqualified interval")
                start, end = ensure_utc(row.start), ensure_utc(row.end)
                if end <= start:
                    raise ValueError("unordered interval")
            except (ValueError, OverflowError):
                unassignable += 1
                continue
            if (since is not None and end <= since) or (until is not None and start >= until):
                outside += 1
                continue
        if row.npt_hours is None:
            missing += 1
        elif (isinstance(row.npt_hours, bool) or not isinstance(row.npt_hours, (int, float))
              or not math.isfinite(row.npt_hours) or row.npt_hours < 0):
            invalid += 1
        elif row.hours_basis not in ("observed", "reported"):
            unqualified += 1
        else:
            hours.append(float(row.npt_hours))
    incomplete = missing + unqualified + invalid + unassignable
    try:
        total = math.fsum(hours) if hours else None
    except OverflowError:
        total = None
    if total is not None and not math.isfinite(total):
        total = None
    status = ("unavailable_numerical" if hours and total is None else
              "partial" if incomplete else "complete_for_matching_events" if hours else "unavailable_no_evidence")
    return {
        "status": status,
        "known_row_sum_hours": round(total, 6) if total is not None else None,
        "qualified_event_count": len(hours), "missing_hours_count": missing,
        "unqualified_hours_count": unqualified, "invalid_hours_count": invalid,
        "unassignable_window_count": unassignable, "outside_window_count": outside,
    }


def reconcile_event_intervals(
    rows: list[IntervalEvidence], *, since: dt.datetime | None = None, until: dt.datetime | None = None
) -> dict[str, Any]:
    """Union complete, explicitly timed, full-NPT intervals within the requested UTC window.

    ``rows`` must include the entire non-temporal scope, including rows whose times cannot be
    assigned to a window. A row is outside only when both qualified endpoints form an ordered
    interval wholly outside the window. Otherwise unknown time prevents a complete union.
    This remains a recorded-event calculation, not evidence of physical downtime or attribution.
    """
    try:
        since = ensure_utc(since) if since is not None else None
        until = ensure_utc(until) if until is not None else None
    except OverflowError as exc:
        raise ValueError("window timestamps must be representable as UTC instants") from exc
    if since is not None and until is not None and since >= until:
        raise ValueError("the interval window must have since earlier than until")
    explicit_row_sum = _explicit_npt_row_sum(rows, since=since, until=until)
    if len(rows) > MAX_RECONCILIATION_EVENTS:
        return {"status": "unavailable_limit", "union_hours": None, "recorded_interval_hours": None,
                "overlap_hours": None, "overlap_event_count": None, "invalid_event_count": None,
                "missing_npt_hours_count": None, "event_count": None, "scoped_event_count": None,
                "unassignable_time_count": None, "outside_window_count": None, "overlapping_window_count": None,
                "blocker_reason_counts": None, "blockers": None, "blockers_truncated": None,
                "explicit_npt_row_sum": explicit_row_sum}
    intervals: list[tuple[dt.datetime, dt.datetime]] = []
    invalid = missing_npt_hours = unassignable = outside = overlapping = 0
    blockers: list[dict[str, str | None]] = []
    reason_counts: dict[str, int] = {}

    def record_blocker(row: IntervalEvidence, reason: str) -> None:
        # A single first-failing reason per row makes counts additive, not an exhaustive list of
        # defects. Identity is explicit source lineage, never a timestamp/title-based merge.
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if len(blockers) < MAX_BLOCKER_DETAILS:
            blockers.append({"event_id": row.event_id, "source_document_id": row.source_document_id,
                             "source_record_id": row.source_record_id, "reason": reason})

    for row in rows:
        start = end = None
        if (row.start_basis in TIME_WINDOW_BASES and row.end_basis in TIME_WINDOW_BASES
                and row.start is not None and row.end is not None):
            try:
                start, end = ensure_utc(row.start), ensure_utc(row.end)
            except (ValueError, OverflowError):
                start = end = None
        if start is None or end is None or end <= start:
            unassignable += 1
            invalid += 1
            if row.npt_hours is None:
                missing_npt_hours += 1
            record_blocker(row, "invalid_or_unqualified_time" if start is None or end is None else "unordered_time")
            continue
        # A qualified interval ending or starting exactly at the boundary has no duration
        # in the half-open reporting window. Its NPT quantity is irrelevant to this window.
        if (since is not None and end <= since) or (until is not None and start >= until):
            outside += 1
            continue
        overlapping += 1
        if row.npt_hours is None:
            missing_npt_hours += 1
        reason = None
        if row.npt_hours is None:
            reason = "missing_npt_hours"
        elif row.hours_basis not in ("observed", "reported"):
            reason = "unqualified_npt_hours"
        elif (isinstance(row.npt_hours, bool) or not isinstance(row.npt_hours, (int, float))
              or not math.isfinite(row.npt_hours) or row.npt_hours <= 0):
            reason = "invalid_npt_hours"
        elif abs((end - start).total_seconds() - row.npt_hours * 3600) > MAX_DURATION_DIFFERENCE_SECONDS:
            reason = "npt_hours_mismatch"
        if reason is not None:
            invalid += 1
            record_blocker(row, reason)
            continue
        low = max(start, since) if since else start
        high = min(end, until) if until else end
        intervals.append((low, high))
    counts = {"scoped_event_count": len(rows), "unassignable_time_count": unassignable,
              "outside_window_count": outside, "overlapping_window_count": overlapping,
              "event_count": overlapping + unassignable, "invalid_event_count": invalid,
              "missing_npt_hours_count": missing_npt_hours, "blocker_reason_counts": reason_counts,
              "blockers": blockers, "blockers_truncated": invalid > len(blockers),
              "explicit_npt_row_sum": explicit_row_sum}
    if invalid or not intervals:
        return {"status": "unavailable_incomplete" if invalid else "unavailable_no_evidence",
                "union_hours": None, "recorded_interval_hours": None, "overlap_hours": None,
                "overlap_event_count": None, **counts}
    intervals.sort()
    union_seconds = recorded_seconds = 0.0
    overlap_events = 0
    current_start = current_end = None
    for start, end in intervals:
        recorded_seconds += (end - start).total_seconds()
        if current_end is None:
            current_start, current_end = start, end
        elif start < current_end:
            overlap_events += 1
            current_end = max(current_end, end)
        else:
            union_seconds += (current_end - current_start).total_seconds()
            current_start, current_end = start, end
    if current_end is not None:
        union_seconds += (current_end - current_start).total_seconds()
    return {
        "status": "complete_for_matching_events", "union_hours": round(union_seconds / 3600, 6),
        "recorded_interval_hours": round(recorded_seconds / 3600, 6),
        "overlap_hours": round((recorded_seconds - union_seconds) / 3600, 6),
        "overlap_event_count": overlap_events, **counts,
    }
