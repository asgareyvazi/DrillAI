"""Deterministic rules: a rule is data, and this is the only thing that reads it.

A rule is a comparison, a threshold, a duration and a severity. There is no expression language, no
``eval``/``exec``, and no way for a rule definition to reach into Python: the operator is one of five
comparisons (:data:`~drillai.telemetry.vocabulary.RULE_OPERATORS`), and anything else is refused when the
rule is written. That is the whole reason the operator set is closed — a rule store that an integration
can write to is an execution surface unless it is deliberately made not to be.

The evaluation is a pure function over the points it is given. It has no clock of its own (``now`` is a
parameter), no database access and no side effects, so a test can evaluate a shift's worth of points in a
fixed instant and get the same answer every run. Everything that needs the database — which channels a
rule watches, whether an alert is already open, whether a cooldown is still running — happens in
:mod:`drillai.telemetry.alerts`, around this function rather than inside it.

Two anti-noise mechanisms, and they are different:

* **sustain** — the condition must hold across a *measurement* span of at least ``sustain_seconds``. A
  single spike in a stream does not raise an alert; forty seconds of an over-pressure does.
* **hysteresis** — clearing uses its own threshold (``clear_threshold``/``clear_operator``) rather than
  the raising one, so a value hovering at the threshold does not flap. A well sitting at exactly the
  limit raises when it exceeds it and clears only when it drops below the clear line.

The third mechanism — a cooldown after an alert closes — needs to know when the last alert closed, which
is a fact about stored alerts rather than about the points, so it lives in the service.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from drillai.core.errors import ValidationFailed
from drillai.telemetry.vocabulary import (
    ALERT_SEVERITIES,
    QUALITY_STATES,
    RULE_OPERATORS,
    TRUSTWORTHY_QUALITY,
)

#: How many of a channel's most recent points one evaluation reads. Bounded on purpose: a rule is a
#: statement about "now", and reading a well's whole history to decide whether to raise an alert would
#: make the cost of a rule grow with the age of the well.
MAX_EVALUATION_POINTS = 500


@dataclass(frozen=True)
class Observation:
    """One point as the rule engine sees it: the value, when it was measured, and how much to trust it."""

    ts: dt.datetime
    value: float | None
    quality: str
    point_id: str | None = None
    sequence: int | None = None


@dataclass(frozen=True)
class RuleSpec:
    """A rule as data. Everything here is a value the database stores, not a callable."""

    id: str
    rule_key: str
    name: str
    channel_key: str
    operator: str
    threshold: float
    severity: str = "medium"
    well_id: str | None = None
    wellbore_id: str | None = None
    clear_operator: str | None = None
    clear_threshold: float | None = None
    sustain_seconds: float = 0.0
    clear_sustain_seconds: float = 0.0
    cooldown_seconds: float = 0.0
    #: The unit the thresholds are compared in (the channel's canonical unit at evaluation time).
    unit: str | None = None
    #: The unit the thresholds were *written* in, when the rule declares one. Reported so an operator
    #: can see "4 000 psi" on the alert rather than only the converted pascal figure.
    declared_unit: str | None = None
    #: The threshold as written, before conversion into the comparison unit.
    declared_threshold: float | None = None
    description: str | None = None
    enabled: bool = True

    def clear_line(self) -> tuple[str, float]:
        """The condition that ends the alert: the rule's own clear setting, or the inverse of its raise.

        The default is *not* "the opposite comparison at the same threshold" for ``eq`` — an equality rule
        clears when the value stops being equal — and it keeps the raise threshold for the ordered
        comparisons, where the comparison's inverse is the correct default. A rule that wants a margin
        sets ``clear_threshold`` explicitly, which is the only way to get hysteresis.
        """

        if self.clear_operator is not None and self.clear_threshold is not None:
            return self.clear_operator, self.clear_threshold
        inverse = {"gt": "lte", "gte": "lt", "lt": "gte", "lte": "gt", "eq": "neq"}[self.operator]
        if inverse == "neq":
            return "eq", self.threshold
        return inverse, self.threshold

    def describe(self) -> str:
        return f"{self.channel_key} {self.operator} {self.threshold:g}"


def _finite_number(value: Any, *, field_name: str) -> float:
    """A threshold is a finite number, and that is checked rather than assumed.

    Rule data is written through the API and can be written by an integration; a threshold that is a
    string, a ``None`` or a NaN would otherwise surface as a ``TypeError`` deep inside a comparison or,
    worse, as a comparison that is quietly always false. It is refused here, by name.
    """

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValidationFailed(
            f"the rule's {field_name} is not a number",
            details={"field": field_name, "value": value, "type": type(value).__name__},
        )
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ValidationFailed(
            f"the rule's {field_name} is not a finite number", details={"field": field_name, "value": value}
        )
    return number


def compare(value: float | None, operator: str, threshold: float) -> bool | None:
    """``value operator threshold`` — ``None`` when there is no value to compare.

    ``None`` is not ``False``: "the channel reported nothing" and "the channel reported a value that does
    not breach" are different, and the evaluation reports them separately. Folding them together is how a
    dead feed looks like a healthy one.
    """

    if value is None:
        return None
    threshold = _finite_number(threshold, field_name="threshold")
    value = _finite_number(value, field_name="value")
    if operator == "gt":
        return value > threshold
    if operator == "gte":
        return value >= threshold
    if operator == "lt":
        return value < threshold
    if operator == "lte":
        return value <= threshold
    if operator == "eq":
        return value == threshold
    raise ValidationFailed(
        "the rule operator is not one the platform evaluates",
        details={"field": "operator", "value": operator, "allowed": list(RULE_OPERATORS)},
    )


@dataclass(frozen=True)
class Evaluation:
    """What one rule makes of one channel's recent points, with the arithmetic shown."""

    rule_id: str
    rule_key: str
    channel_id: str
    channel_key: str
    outcome: str  # raise | clear | hold | insufficient_data
    observed_value: float | None
    observed_at: dt.datetime | None
    point_id: str | None
    sustained_seconds: float
    samples: int
    breaching_samples: int
    excluded_quality: int
    threshold: float
    clear_threshold: float
    unit: str | None
    severity: str
    reason: str
    basis: dict[str, Any] = field(default_factory=dict)

    @property
    def should_raise(self) -> bool:
        return self.outcome == "raise"

    @property
    def should_clear(self) -> bool:
        return self.outcome == "clear"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_key": self.rule_key,
            "channel_id": self.channel_id,
            "channel_key": self.channel_key,
            "outcome": self.outcome,
            "observed": {
                "value": self.observed_value,
                "threshold": self.threshold,
                "clear_threshold": self.clear_threshold,
                "declared_threshold": self.basis.get("stated_threshold"),
                "declared_unit": self.basis.get("stated_unit"),
                "unit": self.unit,
                "timestamp": self.observed_at.isoformat() if self.observed_at else None,
                "channel_key": self.channel_key,
                "point_id": self.point_id,
            },
            "sustained_seconds": round(self.sustained_seconds, 3),
            "samples": self.samples,
            "breaching_samples": self.breaching_samples,
            "excluded_quality": self.excluded_quality,
            "severity": self.severity,
            "reason": self.reason,
            "basis": self.basis,
        }


def validate_rule(
    *,
    operator: str,
    threshold: float | None,
    severity: str,
    sustain_seconds: float,
    clear_sustain_seconds: float,
    cooldown_seconds: float,
    clear_operator: str | None = None,
    clear_threshold: float | None = None,
) -> None:
    """Refuse a rule the engine could not evaluate — at the boundary, not at evaluation time."""

    if operator not in RULE_OPERATORS:
        raise ValidationFailed(
            "the rule operator is not one the platform evaluates",
            details={"field": "operator", "value": operator, "allowed": list(RULE_OPERATORS)},
        )
    if threshold is None:
        raise ValidationFailed("a rule needs a threshold", details={"field": "threshold"})
    _finite_number(threshold, field_name="threshold")
    if severity not in ALERT_SEVERITIES:
        raise ValidationFailed(
            "the rule severity is not one the platform uses",
            details={"field": "severity", "value": severity, "allowed": list(ALERT_SEVERITIES)},
        )
    for name, value in (
        ("sustain_seconds", sustain_seconds),
        ("clear_sustain_seconds", clear_sustain_seconds),
        ("cooldown_seconds", cooldown_seconds),
    ):
        if value is None or value < 0:
            raise ValidationFailed(
                f"{name} must be zero or a positive number of seconds",
                details={"field": name, "value": value},
            )
    if clear_threshold is not None:
        _finite_number(clear_threshold, field_name="clear_threshold")
        if clear_operator not in RULE_OPERATORS:
            raise ValidationFailed(
                "a clear threshold needs a clear operator",
                details={"field": "clear_operator", "value": clear_operator, "allowed": list(RULE_OPERATORS)},
            )
        # A clear line that is *worse* than the raise line can never be reached before the raise, so the
        # alert would never clear. Refusing it here is cheaper than explaining the silence later.
        if operator in {"gt", "gte"} and clear_threshold >= threshold:
            raise ValidationFailed(
                "the clear threshold must be below the raising threshold for an upper-limit rule",
                details={"field": "clear_threshold", "value": clear_threshold, "raise": threshold},
            )
        if operator in {"lt", "lte"} and clear_threshold <= threshold:
            raise ValidationFailed(
                "the clear threshold must be above the raising threshold for a lower-limit rule",
                details={"field": "clear_threshold", "value": clear_threshold, "raise": threshold},
            )


def evaluate(
    rule: RuleSpec,
    observations: Sequence[Observation],
    *,
    channel_id: str,
    channel_key: str,
    now: dt.datetime,
) -> Evaluation:
    """Decide what ``rule`` makes of ``observations`` — newest first, as stored.

    The walk is deliberately the same shape for raising and clearing: take the newest point, and while
    consecutive points (in time order, newest backwards) satisfy the condition, extend the window. The
    sustained span is then the *measurement* time between the newest and oldest point in that window —
    not wall-clock since the alert condition was noticed, and not a count of samples. A channel that
    reports once a minute cannot claim sixty seconds of sustain from two samples a second apart, and a
    channel that reported a burst ten minutes ago and nothing since has not been over-pressure since.
    """

    samples = [item for item in observations if item is not None]
    usable = [item for item in samples if item.quality in TRUSTWORTHY_QUALITY]
    excluded = len(samples) - len(usable)
    ordered = sorted(usable, key=lambda item: item.ts, reverse=True)

    base = {
        "rule": rule.rule_key,
        "operator": rule.operator,
        "stated_threshold": rule.declared_threshold if rule.declared_threshold is not None else rule.threshold,
        "stated_unit": rule.declared_unit or rule.unit,
        "clear_operator": rule.clear_line()[0],
        "clear_threshold": rule.clear_line()[1],
        "samples_read": len(samples),
        "excluded_quality": excluded,
        "window_from": ordered[-1].ts.isoformat() if ordered else None,
        "window_to": ordered[0].ts.isoformat() if ordered else None,
        "evaluated_at": now.isoformat(),
        "method": "sustained_span_over_consecutive_trustworthy_points",
    }

    def result(
        outcome: str,
        *,
        value: float | None,
        at: dt.datetime | None,
        point_id: str | None,
        sustained: float,
        breaching: int,
        threshold: float,
        reason: str,
        clear_threshold: float,
    ) -> Evaluation:
        return Evaluation(
            rule_id=rule.id,
            rule_key=rule.rule_key,
            channel_id=channel_id,
            channel_key=channel_key,
            outcome=outcome,
            observed_value=value,
            observed_at=at,
            point_id=point_id,
            sustained_seconds=sustained,
            samples=len(samples),
            breaching_samples=breaching,
            excluded_quality=excluded,
            threshold=threshold,
            clear_threshold=clear_threshold,
            unit=rule.unit,
            severity=rule.severity,
            reason=reason,
            basis=dict(base),
        )

    clear_operator, clear_threshold = rule.clear_line()

    if not ordered:
        return result(
            "insufficient_data",
            value=None,
            at=None,
            point_id=None,
            sustained=0.0,
            breaching=0,
            threshold=rule.threshold,
            clear_threshold=clear_threshold,
            reason=(
                "the channel has no trustworthy measurement inside the evaluation window"
                + (f" ({excluded} point(s) excluded on quality)" if excluded else "")
            ),
        )

    newest = ordered[0]
    # ---- raising
    window: list[Observation] = []
    for point in ordered:
        verdict = compare(point.value, rule.operator, rule.threshold)
        if verdict is True:
            window.append(point)
            continue
        break
    if window:
        span = (window[0].ts - window[-1].ts).total_seconds()
        if span >= rule.sustain_seconds:
            return result(
                "raise",
                value=window[0].value,
                at=window[0].ts,
                point_id=window[0].point_id,
                sustained=span,
                breaching=len(window),
                threshold=rule.threshold,
                clear_threshold=clear_threshold,
                reason=(
                    # The message carries the reading, not only the comparison: "why was this raised?"
                    # is answered by 4 210 Pa against a 4 000 Pa line, not by the word 'gt'.
                    f"{rule.channel_key} = {window[0].value:g} has been {rule.operator} "
                    f"{rule.threshold:g} for {span:.0f}s (required {rule.sustain_seconds:.0f}s)"
                ),
            )

    # ---- clearing: the newest value must satisfy the clear condition, sustained
    cleared_window: list[Observation] = []
    newest_clears = compare(newest.value, clear_operator, clear_threshold)
    if newest_clears is True:
        for point in ordered:
            if compare(point.value, clear_operator, clear_threshold) is True:
                cleared_window.append(point)
            else:
                break
        span = (cleared_window[0].ts - cleared_window[-1].ts).total_seconds()
        if span >= rule.clear_sustain_seconds:
            return result(
                "clear",
                value=newest.value,
                at=newest.ts,
                point_id=newest.point_id,
                sustained=span,
                breaching=0,
                threshold=rule.threshold,
                clear_threshold=clear_threshold,
                reason=(
                    f"{rule.channel_key} = {newest.value:g} has been {clear_operator} "
                    f"{clear_threshold:g} for {span:.0f}s (required {rule.clear_sustain_seconds:.0f}s)"
                ),
            )

    # ---- neither: the condition is not sustained in either direction, so nothing changes
    breaching_span = (window[0].ts - window[-1].ts).total_seconds() if window else 0.0
    return result(
        "hold",
        value=newest.value,
        at=newest.ts,
        point_id=newest.point_id,
        sustained=breaching_span,
        breaching=len(window),
        threshold=rule.threshold,
        clear_threshold=clear_threshold,
        reason=(
            f"the condition has not held for long enough to change the alert state "
            f"(breaching window {breaching_span:.0f}s of the required {rule.sustain_seconds:.0f}s)"
        ),
    )


def validate_quality(value: str) -> str:
    """Refuse a quality the platform does not record — used by rule-related payload paths."""

    if value not in QUALITY_STATES:
        raise ValidationFailed(
            "the quality is not one the platform records",
            details={"field": "quality", "value": value, "allowed": list(QUALITY_STATES)},
        )
    return value
