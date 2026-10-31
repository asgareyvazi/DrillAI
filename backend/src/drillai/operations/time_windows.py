"""SQL predicates for inclusive overlap with an optional time window.

An interval with one known endpoint is treated as a point at that timestamp. This is the same
fail-closed convention used by context and retrieval filters: a row with no time evidence cannot
match a narrowed window.
"""

from __future__ import annotations

from sqlalchemy import case, func

from drillai.db.models import Operation


def interval_overlap_conditions(start, end, *, since=None, until=None):
    """Return SQL conditions for overlap with ``[since, until]``.

    ``coalesce`` makes a missing endpoint a point at the endpoint that is known. The conditions are
    inclusive at both boundaries and require at least one timestamp, preventing undated rows from
    leaking into time-scoped results.
    """
    low = func.coalesce(start, end)
    high = func.coalesce(end, start)
    conditions = [low.is_not(None), high.is_not(None)]
    if since is not None:
        conditions.append(high >= since)
    if until is not None:
        conditions.append(low <= until)
    return conditions


def operation_time_expressions():
    """Select one coherent interval: planned for plans/forecasts, actual otherwise.

    Combining an actual start with a planned end (or vice versa) invents a schedule that was neither
    performed nor planned. Missing endpoints are handled as points by ``interval_overlap_conditions``.
    """
    planned = Operation.operation_class.in_(("plan", "forecast"))
    return (
        case((planned, Operation.planned_start), else_=Operation.actual_start),
        case((planned, Operation.planned_end), else_=Operation.actual_end),
    )
