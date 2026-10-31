from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from drillai.drilling.ddr import (
    DdrProcessor,
    _parse_report_instant,
    _valid_report_duration,
)


def test_report_time_parser_normalizes_offsets_and_never_invents_a_missing_date():
    report_day = dt.datetime(2026, 3, 15, tzinfo=dt.UTC)
    assert _parse_report_instant("2026-03-15T12:00:00+05:30", on_date=None) == dt.datetime(
        2026, 3, 15, 6, 30, tzinfo=dt.UTC
    )
    assert _parse_report_instant("06:45", on_date=report_day) == dt.datetime(
        2026, 3, 15, 6, 45, tzinfo=dt.UTC
    )
    assert _parse_report_instant("064500", on_date=report_day) == dt.datetime(
        2026, 3, 15, 6, 45, tzinfo=dt.UTC
    )
    assert _parse_report_instant("06:45", on_date=None) is None
    assert _parse_report_instant("25:45", on_date=report_day) is None
    assert _parse_report_instant("2026-03-15T06:45:00", on_date=report_day) is None


def test_ddr_report_date_prefers_explicit_period_then_issue_date_then_header():
    period_start = dt.datetime(2026, 3, 10, 4, tzinfo=dt.UTC)
    issue_date = dt.datetime(2026, 3, 12, tzinfo=dt.UTC)
    header = SimpleNamespace(record_type="report_header", payload={"report_date": "2026-03-15"})

    assert DdrProcessor._report_date(
        SimpleNamespace(period_start=period_start, issue_date=issue_date), [header]
    ) == period_start
    assert DdrProcessor._report_date(
        SimpleNamespace(period_start=None, issue_date=issue_date), [header]
    ) == issue_date
    assert DdrProcessor._report_date(
        SimpleNamespace(period_start=None, issue_date=None), [header]
    ) == dt.datetime(2026, 3, 15, tzinfo=dt.UTC)
    assert DdrProcessor._report_date(
        SimpleNamespace(period_start=None, issue_date=None, period_end=dt.datetime(2026, 3, 20, tzinfo=dt.UTC)),
        [],
    ) is None, "a period-end boundary alone is not a source event timestamp"


def test_npt_event_time_uses_source_timestamps_before_the_marked_ingestion_fallback():
    fallback = dt.datetime(2026, 3, 15, 8, tzinfo=dt.UTC)
    report_day = dt.datetime(2026, 3, 15, tzinfo=dt.UTC)
    record = SimpleNamespace(observed_at=None, created_at=fallback)
    linked = SimpleNamespace(actual_start=dt.datetime.fromisoformat("2026-03-15T09:00:00+01:00"))

    assert DdrProcessor._event_time(record=record, linked=linked, report_date=report_day) == (
        dt.datetime(2026, 3, 15, 8, tzinfo=dt.UTC),
        "linked_operation_start",
    )
    assert DdrProcessor._event_time(record=record, linked=None, report_date=report_day) == (
        report_day,
        "document_report_date",
    )
    assert DdrProcessor._event_time(record=record, linked=None, report_date=None) == (
        fallback,
        "ingestion_timestamp_fallback",
    )


def test_duration_validation_rejects_nonfinite_negative_and_unbounded_values():
    for value in (True, "2.5", -0.1, 100.0, float("nan"), float("inf"), 10**10000):
        assert _valid_report_duration(value) is False
    assert _valid_report_duration(0.0) is True
    assert _valid_report_duration(99.999) is True


def test_event_time_falls_back_to_the_existing_event_before_ingestion_time():
    old_occurrence = dt.datetime(2026, 3, 14, 23, tzinfo=dt.UTC)
    record = SimpleNamespace(observed_at=None, created_at=dt.datetime(2026, 3, 15, 8, tzinfo=dt.UTC))
    existing = SimpleNamespace(occurred_at=old_occurrence, attributes={"occurred_at_basis": "document_report_date"})

    assert DdrProcessor._event_time(record=record, linked=None, report_date=None, existing=existing) == (
        old_occurrence,
        "document_report_date",
    )
