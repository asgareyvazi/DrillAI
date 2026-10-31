"""Time abstraction.

Every timestamp in the platform is timezone-aware UTC. Services depend on ``Clock``
rather than ``datetime.now`` so that:

* tests are deterministic (``FixedClock`` / ``TickClock``),
* workflow replay produces identical results,
* scheduled work and SLA computations are testable,
* time-series alignment can be simulated without sleeping.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Protocol, runtime_checkable

UTC = dt.UTC


@runtime_checkable
class Clock(Protocol):
    def now(self) -> dt.datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    """Wall-clock implementation used in production."""

    def now(self) -> dt.datetime:
        return dt.datetime.now(tz=UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FixedClock:
    """Clock frozen at an instant; ``advance`` moves it forward."""

    def __init__(self, instant: dt.datetime | None = None) -> None:
        self._instant = instant or dt.datetime(2026, 1, 1, tzinfo=UTC)
        self._mono = 0.0

    def now(self) -> dt.datetime:
        return self._instant

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float = 0.0, **kwargs: float) -> dt.datetime:
        delta = dt.timedelta(seconds=seconds, **kwargs)
        self._instant = self._instant + delta
        self._mono += delta.total_seconds()
        return self._instant

    def set(self, instant: dt.datetime) -> None:
        self._instant = instant


class TickClock(FixedClock):
    """Clock that advances by a fixed step on every read (used for run timelines)."""

    def __init__(self, instant: dt.datetime | None = None, step_seconds: float = 0.05) -> None:
        super().__init__(instant)
        self._step = step_seconds

    def now(self) -> dt.datetime:
        current = super().now()
        self.advance(self._step)
        return current


def ensure_utc(value: dt.datetime) -> dt.datetime:
    """Normalize a datetime to timezone-aware UTC.

    Naive datetimes are *rejected* for domain data: silently assuming a timezone in a
    drilling context (where DDRs, rig clocks and operator headquarters differ) is a
    data-integrity defect.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime is not allowed; provide a timezone-aware datetime")
    return value.astimezone(UTC)


def parse_iso_datetime_utc(value: str, *, assume_utc_for_naive: bool = False) -> dt.datetime:
    """Parse an ISO-8601 timestamp and return its UTC instant.

    Date-only report-header values are intentionally allowed to mean midnight UTC by callers that
    pass ``assume_utc_for_naive=True``. User-supplied interval bounds must instead carry an explicit
    offset; they should use :func:`ensure_utc` after their API parser has produced a datetime.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("an ISO-8601 datetime string is required")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if not assume_utc_for_naive:
            raise ValueError("ISO-8601 datetime must include a timezone offset")
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def utc_now() -> dt.datetime:
    return dt.datetime.now(tz=UTC)
