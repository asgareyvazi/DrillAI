from __future__ import annotations

import datetime as dt

import pytest

from drillai.core.clock import ensure_utc, parse_iso_datetime_utc


def test_ensure_utc_normalizes_an_explicit_offset():
    value = dt.datetime.fromisoformat("2026-03-15T12:00:00+05:30")

    assert ensure_utc(value) == dt.datetime(2026, 3, 15, 6, 30, tzinfo=dt.UTC)


def test_ensure_utc_rejects_naive_values():
    with pytest.raises(ValueError, match="naive datetime"):
        ensure_utc(dt.datetime(2026, 3, 15, 12, 0))


def test_iso_parser_normalizes_offsets_and_supports_explicit_date_only_mode():
    assert parse_iso_datetime_utc("2026-03-15T12:00:00+05:30") == dt.datetime(
        2026, 3, 15, 6, 30, tzinfo=dt.UTC
    )
    assert parse_iso_datetime_utc("2026-03-15T06:30:00Z") == dt.datetime(
        2026, 3, 15, 6, 30, tzinfo=dt.UTC
    )
    assert parse_iso_datetime_utc("2026-03-15", assume_utc_for_naive=True) == dt.datetime(
        2026, 3, 15, tzinfo=dt.UTC
    )
    with pytest.raises(ValueError, match="timezone offset"):
        parse_iso_datetime_utc("2026-03-15")
