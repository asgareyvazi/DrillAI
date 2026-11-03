"""SQL predicates for inclusive overlap with optional, provenance-aware time windows.

Only a timezone-bearing source/operator timestamp can place an operation on a narrowed window.
Report-level anchors, derived/replayed times, and legacy timestamps remain
readable with their basis but cannot silently become observed time.
"""

from __future__ import annotations

from sqlalchemy import case

from drillai.db.models import Event, Operation

TIME_WINDOW_BASES = ("source_record", "operator_entered")
DEPTH_WINDOW_BASES = ("source_record", "operator_entered", "engine_derived")
_START_WINDOW_BASES = TIME_WINDOW_BASES
_END_WINDOW_BASES = TIME_WINDOW_BASES
_EVENT_TIME_BASES = TIME_WINDOW_BASES


def event_time_expressions():
    """Return event start/end expressions qualified by occurrence-time provenance."""
    return (
        case(
            (Event.occurred_at_basis.in_(_EVENT_TIME_BASES), Event.occurred_at),
            else_=None,
        ),
        case(
            (Event.ended_at_basis.in_(_EVENT_TIME_BASES), Event.ended_at),
            else_=None,
        ),
    )


def interval_overlap_conditions(start, end, *, since=None, until=None):
    """Return SQL conditions for overlap with ``[since, until]``.

    A single known endpoint is treated as a point; an interval with no trusted time evidence does not
    match a narrowed window. Boundaries are inclusive.
    """
    low = case((start.is_not(None), start), else_=end)
    high = case((end.is_not(None), end), else_=start)
    conditions = [low.is_not(None), high.is_not(None)]
    if since is not None:
        conditions.append(high >= since)
    if until is not None:
        conditions.append(low <= until)
    return conditions


def operation_time_expressions():
    """Select one coherent, provenance-qualified interval for plans or actual work.

    Combining an actual start with a planned end invents a schedule neither performed nor planned.
    The selected endpoints are nulled unless their typed basis is precise enough for time-window
    filtering. ``report_date_anchor`` and ``duration_replayed`` remain in the row for audit/display,
    but are not event-time evidence.
    """
    planned = Operation.operation_class.in_(("plan", "forecast"))
    start = case(
        (
            planned & Operation.planned_start_basis.in_(_START_WINDOW_BASES),
            Operation.planned_start,
        ),
        (
            ~planned & Operation.actual_start_basis.in_(_START_WINDOW_BASES),
            Operation.actual_start,
        ),
        else_=None,
    )
    end = case(
        (
            planned & Operation.planned_end_basis.in_(_END_WINDOW_BASES),
            Operation.planned_end,
        ),
        (
            ~planned & Operation.actual_end_basis.in_(_END_WINDOW_BASES),
            Operation.actual_end,
        ),
        else_=None,
    )
    return start, end
