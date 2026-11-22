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


def reconcile_event_intervals(
    rows: list[IntervalEvidence], *, since: dt.datetime | None = None, until: dt.datetime | None = None
) -> dict[str, Any]:
    """Union complete, explicitly timed, full-NPT intervals within the requested UTC window.

    The result is elapsed time of matching recorded intervals across the selected well, not
    evidence of unique physical rig downtime. Overlapping causes remain unresolved. A narrow
    query's source filter can exclude events with unknown timestamps; completeness only refers
    to rows returned for the requested scope, not to unseen physical events.
    """
    since = ensure_utc(since) if since is not None else None
    until = ensure_utc(until) if until is not None else None
    if since is not None and until is not None and since >= until:
        raise ValueError("the interval window must have since earlier than until")
    if len(rows) > MAX_RECONCILIATION_EVENTS:
        return {"status": "unavailable_limit", "union_hours": None, "recorded_interval_hours": None,
                "overlap_hours": None, "overlap_event_count": None, "invalid_event_count": None,
                "missing_npt_hours_count": None, "event_count": len(rows)}
    intervals: list[tuple[dt.datetime, dt.datetime]] = []
    invalid = 0
    missing_npt_hours = 0
    for row in rows:
        if row.npt_hours is None:
            missing_npt_hours += 1
        if (row.start_basis not in TIME_WINDOW_BASES or row.end_basis not in TIME_WINDOW_BASES
                or row.hours_basis not in ("observed", "reported")
                or row.start is None or row.end is None or row.npt_hours is None
                or isinstance(row.npt_hours, bool) or not isinstance(row.npt_hours, (int, float))
                or not math.isfinite(row.npt_hours) or row.npt_hours <= 0):
            invalid += 1
            continue
        try:
            start, end = ensure_utc(row.start), ensure_utc(row.end)
        except ValueError:
            invalid += 1
            continue
        elapsed = (end - start).total_seconds()
        if elapsed <= 0 or abs(elapsed - row.npt_hours * 3600) > MAX_DURATION_DIFFERENCE_SECONDS:
            invalid += 1
            continue
        low = max(start, since) if since else start
        high = min(end, until) if until else end
        if high > low:
            intervals.append((low, high))
    if invalid:
        return {"status": "unavailable_incomplete", "union_hours": None, "recorded_interval_hours": None,
                "overlap_hours": None, "overlap_event_count": None, "invalid_event_count": invalid,
                "missing_npt_hours_count": missing_npt_hours, "event_count": len(rows)}
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
        "overlap_event_count": overlap_events, "invalid_event_count": 0,
        "missing_npt_hours_count": 0, "event_count": len(rows),
    }
