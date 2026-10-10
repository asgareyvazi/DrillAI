"""The rule engine: deterministic arithmetic over a fixed set of points.

Every assertion here is a statement about *arithmetic*, not about a running system, so the points are
written out and the evaluation is called directly — no database, no clock, no stream. That is the point
of keeping the engine pure: a shift's worth of behaviour can be checked in a millisecond, and the answer
is the same on every machine.

The two anti-noise mechanisms are tested at their boundaries (sustain exactly met, exactly not met), and
the mechanisms the engine deliberately does *not* have are tested too: no expression evaluation, no
guessing at a missing value, and a clear line that cannot be configured to be unreachable.
"""

from __future__ import annotations

import datetime as dt

import pytest

from drillai.core.errors import ValidationFailed
from drillai.telemetry.rules import (
    Evaluation,
    Observation,
    RuleSpec,
    compare,
    evaluate,
    validate_rule,
)
from drillai.telemetry.vocabulary import RULE_OPERATORS

NOW = dt.datetime(2026, 3, 15, 12, 0, tzinfo=dt.UTC)


def spec(**overrides) -> RuleSpec:
    base = {
        "id": "arl_1",
        "rule_key": "spp-high",
        "name": "Standpipe pressure high",
        "channel_key": "spp",
        "operator": "gt",
        "threshold": 4000.0,
        "severity": "high",
        "unit": "Pa",
    }
    return RuleSpec(**{**base, **overrides})


def points(*values: float, step: int = 20, quality: str = "good") -> list[Observation]:
    """Newest last, ascending in time — the engine sorts, but a test reads better in time order."""

    start = NOW - dt.timedelta(seconds=step * (len(values) - 1))
    return [
        Observation(ts=start + dt.timedelta(seconds=step * index), value=value, quality=quality, point_id=f"p{index}")
        for index, value in enumerate(values)
    ]


def run(rule: RuleSpec, observations: list[Observation]) -> Evaluation:
    return evaluate(rule, observations, channel_id="tms_1", channel_key="spp", now=NOW)


# --------------------------------------------------------------------------- comparisons


def test_every_operator_in_the_vocabulary_is_implemented() -> None:
    """A rule the catalogue accepts must be evaluable: the two lists are the same list."""

    for operator in RULE_OPERATORS:
        assert compare(1.0, operator, 1.0) is not None
    assert compare(1.0, "gt", 0.5) is True
    assert compare(1.0, "gte", 1.0) is True
    assert compare(1.0, "lt", 1.0) is False
    assert compare(1.0, "lte", 1.0) is True
    assert compare(1.0, "eq", 1.0) is True
    with pytest.raises(ValidationFailed):
        compare(1.0, "matches", 1.0)  # a regex or an expression is not a comparison the engine knows


def test_a_missing_value_is_not_a_breach_and_not_a_clear() -> None:
    """``None`` propagates instead of behaving like zero: a channel that stopped reporting must not
    look like a channel reading 0, in either direction."""

    assert compare(None, "gt", 0.0) is None
    assert compare(None, "lt", 0.0) is None


# --------------------------------------------------------------------------- raising


def test_a_breach_raises_immediately_when_no_sustain_is_configured() -> None:
    outcome = run(spec(), points(3900.0, 4200.0))
    assert outcome.should_raise is True
    assert outcome.observed_value == 4200.0
    assert outcome.sustained_seconds == 0.0
    assert "4200" in outcome.reason, "the reason names the reading that breached"
    assert "4000" in outcome.reason, "and the line it crossed"


def test_a_single_spike_does_not_raise_when_sustain_is_configured() -> None:
    """The spike is real and it breaches; it just has not lasted long enough to be an alert."""

    outcome = run(spec(sustain_seconds=60.0), points(3900.0, 3900.0, 4200.0))
    assert outcome.outcome == "hold"
    assert outcome.breaching_samples == 1
    assert outcome.sustained_seconds == 0.0
    assert "not held for long enough" in outcome.reason


def test_sustain_is_the_measured_span_not_the_sample_count() -> None:
    """Three samples a second apart are one second of over-pressure, not three seconds of it."""

    outcome = run(spec(sustain_seconds=5.0), points(4100.0, 4100.0, 4100.0, step=1))
    assert outcome.outcome == "hold", "one second of breach is not five"

    sustained = run(spec(sustain_seconds=5.0), points(*([4100.0] * 6), step=1))
    assert sustained.outcome == "raise"
    assert sustained.sustained_seconds == 5.0, "five seconds measured, five seconds sustained"


def test_the_sustain_boundary_is_inclusive() -> None:
    rule = spec(sustain_seconds=40.0)
    assert run(rule, points(4100.0, 4100.0, 4100.0, step=20)).outcome == "raise"
    assert run(rule, points(4100.0, 4100.0, step=20, quality="good")).outcome == "hold"


def test_a_gap_in_the_breach_stops_the_sustain_window() -> None:
    """An over-pressure, a normal reading, then another over-pressure is not one sustained event: the
    seconds between them were spent below the limit."""

    outcome = run(spec(sustain_seconds=30.0), points(4100.0, 4100.0, 3900.0, 4100.0, 4100.0, step=20))
    assert outcome.outcome == "hold"
    assert outcome.sustained_seconds == 20.0, "only the newest unbroken run counts"


def test_an_upper_limit_rule_ignores_values_below_the_threshold() -> None:
    assert run(spec(), points(3000.0, 3500.0)).outcome == "clear"


def test_a_lower_limit_rule_raises_on_a_low_value() -> None:
    rule = spec(rule_key="pit-low", operator="lt", threshold=50.0, severity="critical")
    outcome = run(rule, points(80.0, 40.0))
    assert outcome.should_raise is True
    assert outcome.observed_value == 40.0
    assert outcome.severity == "critical"


# --------------------------------------------------------------------------- hysteresis


def test_the_default_clear_line_is_the_inverse_of_the_raise_line() -> None:
    assert spec(operator="gt", threshold=4000.0).clear_line() == ("lte", 4000.0)
    assert spec(operator="gte", threshold=4000.0).clear_line() == ("lt", 4000.0)
    assert spec(operator="lt", threshold=50.0).clear_line() == ("gte", 50.0)
    assert spec(operator="lte", threshold=50.0).clear_line() == ("gt", 50.0)
    # an equality rule clears when the value stops being equal, which is the same comparison
    assert spec(operator="eq", threshold=4000.0).clear_line() == ("eq", 4000.0)


def test_hysteresis_keeps_a_value_hovering_at_the_limit_from_flapping() -> None:
    """Raise at 4000, clear at 3800: a value sitting at 3950 raises and then stays raised, rather than
    alternating every time the noise crosses one line."""

    rule = spec(threshold=4000.0, clear_operator="lt", clear_threshold=3800.0)
    assert rule.clear_line() == ("lt", 3800.0)
    raised = run(rule, points(3700.0, 4050.0))
    assert raised.should_raise is True

    hovering = run(rule, points(4050.0, 3950.0))
    assert hovering.outcome == "hold", "3950 is above the clear line, so the alert stands"
    assert hovering.observed_value == 3950.0

    cleared = run(rule, points(3950.0, 3700.0))
    assert cleared.should_clear is True
    assert cleared.observed_value == 3700.0
    assert cleared.clear_threshold == 3800.0


def test_clear_sustain_also_has_to_be_earned() -> None:
    rule = spec(clear_operator="lt", clear_threshold=3800.0, clear_sustain_seconds=60.0)
    assert run(rule, points(3700.0, 3700.0, 3700.0, 3700.0, step=20)).outcome == "clear", "60s below the line"
    assert run(rule, points(3700.0, 3700.0, 3700.0, step=20)).outcome == "hold", "40s is not 60s"


# --------------------------------------------------------------------------- quality and data


def test_points_whose_quality_is_not_trustworthy_do_not_drive_a_decision() -> None:
    """A suspect reading cannot raise an alert and cannot clear one; it is counted so the decision can be
    explained, and the newest trustworthy value is what the decision is made from."""

    observations = [
        *points(3900.0, 3900.0),
        Observation(ts=NOW, value=9999.0, quality="bad", point_id="p-bad"),
    ]
    outcome = run(spec(), observations)
    assert outcome.observed_value == 3900.0
    assert outcome.excluded_quality == 1
    assert outcome.outcome == "clear", "the bad spike is not an alert and the good data is below the line"


def test_a_channel_with_only_bad_points_is_insufficient_data_not_a_clear() -> None:
    outcome = run(spec(), points(9999.0, 9999.0, quality="bad"))
    assert outcome.outcome == "insufficient_data"
    assert outcome.samples == 2 and outcome.breaching_samples == 0
    assert "excluded on quality" in outcome.reason


def test_an_empty_series_is_insufficient_data() -> None:
    outcome = run(spec(), [])
    assert outcome.outcome == "insufficient_data"
    assert outcome.samples == 0
    assert outcome.observed_value is None


def test_a_point_with_no_value_does_not_raise() -> None:
    observations = [Observation(ts=NOW, value=None, quality="missing", point_id="p-null")]
    outcome = run(spec(), observations)
    assert outcome.outcome == "insufficient_data", "a point with no value is not a breach"
    assert outcome.observed_value is None
    assert outcome.excluded_quality == 1


# --------------------------------------------------------------------------- the evaluation is explainable


def test_the_evaluation_carries_the_arithmetic_that_produced_it() -> None:
    outcome = run(spec(sustain_seconds=20.0), points(4100.0, 4100.0, step=20))
    payload = outcome.to_dict()
    assert payload["outcome"] == "raise"
    assert payload["observed"]["value"] == 4100.0
    assert payload["observed"]["threshold"] == 4000.0
    assert payload["observed"]["unit"] == "Pa"
    assert payload["observed"]["channel_key"] == "spp"
    assert payload["sustained_seconds"] == 20.0
    assert payload["samples"] == 2 and payload["breaching_samples"] == 2
    basis = payload["basis"]
    assert basis["method"] == "sustained_span_over_consecutive_trustworthy_points"
    assert basis["window_to"] is not None and basis["window_from"] is not None
    assert basis["evaluated_at"] == NOW.isoformat()


def test_the_same_points_always_produce_the_same_evaluation() -> None:
    """Determinism is a property of the engine: no clock, no randomness, no database."""

    rule = spec(sustain_seconds=20.0)
    sample = points(3900.0, 4100.0, 4100.0)
    first = run(rule, sample).to_dict()
    second = run(rule, list(reversed(sample))).to_dict()
    assert first == second, "the engine sorts its input; the caller's order does not change the answer"


# --------------------------------------------------------------------------- rule validation


def test_a_rule_with_an_unreachable_clear_line_is_refused() -> None:
    """An upper-limit rule whose clear threshold sits above its raise threshold would raise and then never
    clear — the failure mode that looks like a stuck alert."""

    with pytest.raises(ValidationFailed) as failure:
        validate_rule(
            operator="gt",
            threshold=4000.0,
            severity="high",
            sustain_seconds=0.0,
            clear_sustain_seconds=0.0,
            cooldown_seconds=0.0,
            clear_operator="lt",
            clear_threshold=4500.0,
        )
    assert failure.value.details["field"] == "clear_threshold"

    with pytest.raises(ValidationFailed):
        validate_rule(
            operator="lt",
            threshold=50.0,
            severity="high",
            sustain_seconds=0.0,
            clear_sustain_seconds=0.0,
            cooldown_seconds=0.0,
            clear_operator="gt",
            clear_threshold=20.0,
        )


def test_a_reachable_clear_line_is_accepted() -> None:
    validate_rule(
        operator="gt",
        threshold=4000.0,
        severity="high",
        sustain_seconds=30.0,
        clear_sustain_seconds=10.0,
        cooldown_seconds=300.0,
        clear_operator="lt",
        clear_threshold=3800.0,
    )


def test_a_negative_duration_is_refused() -> None:
    with pytest.raises(ValidationFailed) as failure:
        validate_rule(
            operator="gt",
            threshold=1.0,
            severity="high",
            sustain_seconds=-1.0,
            clear_sustain_seconds=0.0,
            cooldown_seconds=0.0,
        )
    assert failure.value.details["field"] == "sustain_seconds"


def test_an_unknown_operator_or_severity_is_refused_at_definition_time() -> None:
    with pytest.raises(ValidationFailed):
        validate_rule(
            operator="exists",  # an expression language sneaking in as an operator
            threshold=1.0,
            severity="high",
            sustain_seconds=0.0,
            clear_sustain_seconds=0.0,
            cooldown_seconds=0.0,
        )
    with pytest.raises(ValidationFailed):
        validate_rule(
            operator="gt",
            threshold=1.0,
            severity="apocalyptic",
            sustain_seconds=0.0,
            clear_sustain_seconds=0.0,
            cooldown_seconds=0.0,
        )


def test_a_threshold_that_is_not_a_finite_number_is_refused_by_name() -> None:
    """Rule data is written through the API and by integrations, so a threshold is checked rather than
    trusted: a string, a boolean or a NaN would otherwise surface as a bare ``TypeError`` inside a
    comparison — or, worse, as a comparison that is quietly always false."""

    for bad in ("4000", True, float("nan"), float("inf")):
        with pytest.raises(ValidationFailed) as failure:
            validate_rule(
                operator="gt",
                threshold=bad,
                severity="high",
                sustain_seconds=0.0,
                clear_sustain_seconds=0.0,
                cooldown_seconds=0.0,
            )
        assert failure.value.details["field"] == "threshold"
        with pytest.raises(ValidationFailed):
            run(spec(threshold=bad), points(4100.0))


def test_the_raise_reason_quotes_the_reading_not_only_the_comparison() -> None:
    """\"why was this raised?\" is answered by the value against the line, not by the word ``gt``."""

    outcome = run(spec(sustain_seconds=20.0), points(4100.0, 4100.0, step=20))
    assert "4100" in outcome.reason
    assert "4000" in outcome.reason
    assert "20s" in outcome.reason
