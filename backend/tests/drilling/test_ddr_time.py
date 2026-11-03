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
    assert _parse_report_instant("2026-03-15", on_date=report_day) is None
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


def test_npt_event_time_uses_only_source_backed_record_times():
    source_time = dt.datetime(2026, 3, 15, 7, 15, tzinfo=dt.UTC)
    record = SimpleNamespace(
        observed_at=source_time,
        observed_at_basis="source_record",
        created_at=dt.datetime(2026, 3, 15, 8, tzinfo=dt.UTC),
    )
    assert DdrProcessor._event_time(record=record) == (source_time, "source_record")


def test_npt_event_time_does_not_promote_report_header_or_ingestion_time():
    report_day = dt.datetime(2026, 3, 15, tzinfo=dt.UTC)
    record = SimpleNamespace(
        observed_at=report_day,
        observed_at_basis="report_header_date",
        created_at=dt.datetime(2026, 3, 15, 8, tzinfo=dt.UTC),
    )
    assert DdrProcessor._event_time(record=record) == (None, "unknown")


def test_duration_validation_rejects_nonfinite_negative_and_unbounded_values():
    for value in (True, "2.5", -0.1, 100.0, float("nan"), float("inf"), 10**10000):
        assert _valid_report_duration(value) is False
    assert _valid_report_duration(0.0) is True
    assert _valid_report_duration(99.999) is True


def test_event_time_preserves_a_previously_source_backed_occurrence_only():
    old_occurrence = dt.datetime(2026, 3, 14, 23, tzinfo=dt.UTC)
    record = SimpleNamespace(
        observed_at=None,
        observed_at_basis="unknown",
        created_at=dt.datetime(2026, 3, 15, 8, tzinfo=dt.UTC),
    )
    trusted_existing = SimpleNamespace(
        occurred_at=old_occurrence,
        occurred_at_basis="source_record",
    )
    untrusted_existing = SimpleNamespace(
        occurred_at=old_occurrence,
        occurred_at_basis="report_date_anchor",
    )

    assert DdrProcessor._event_time(record=record, existing=trusted_existing) == (
        old_occurrence,
        "source_record",
    )
    assert DdrProcessor._event_time(record=record, existing=untrusted_existing) == (None, "unknown")
