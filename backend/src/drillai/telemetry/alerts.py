"""The alert life cycle: raising, acknowledging, clearing, cancelling — and the evidence for each.

An alert is a claim that something needs attention, so every alert must be able to answer four questions
without a reader having to reconstruct anything: *why was it raised* (rule, operator, threshold, the
value observed and when), *on what* (channel, series, well, wellbore, section, operation), *what has
happened to it since* (the life cycle, with who and why), and *what would clear it* (the clear line and
the sustain). All four are stored on the row by this service, not recomputed by a reader.

Three rules that keep the life cycle honest:

* **Transitions are the vocabulary's, not the caller's.** ``cleared`` is not a synonym for ``cancelled``:
  the first means a rule's condition stopped being true, the second means a person decided the alert was
  not worth acting on. The counts that management reads ("how many resolved themselves?") depend on the
  difference, so a transition the vocabulary does not allow is refused with the states it would allow.
* **A raise is idempotent per (rule, channel) while an alert is open.** A rule that is still true on the
  next evaluation does not raise a second alert; it is the same condition. The evaluation is recorded
  (``observations`` counter and last-checked time) rather than duplicated.
* **Nothing is ever deleted, and every mutation is audited** with who/what/why/when, the action level and
  the before/after state, in the platform's ledger — the same ledger every other mutation in this
  repository writes to.

The service never evaluates rules for a caller: :meth:`AlertService.evaluate_well` does that, and it is
deterministic — same points, same ruleset, same answer — because the arithmetic lives in
:mod:`drillai.telemetry.rules` and this module only decides what to *do* with the answer.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.audit import record_audit
from drillai.core.clock import utc_now
from drillai.core.errors import Conflict, NotFound, ValidationFailed
from drillai.core.ids import new_id
from drillai.core.logging import get_logger
from drillai.db.models import Alert, AlertRule, AuditLog, TimeSeries, TimeSeriesPoint, Well
from drillai.security.actions import Principal
from drillai.telemetry.outbox import emit
from drillai.telemetry.rules import (
    MAX_EVALUATION_POINTS,
    Evaluation,
    Observation,
    RuleSpec,
    evaluate,
    validate_rule,
)
from drillai.telemetry.units import convert, known_units, unit_dimensions
from drillai.telemetry.vocabulary import (
    ALERT_STATUSES,
    ALERT_TERMINAL_STATUSES,
    alert_transitions,
)

logger = get_logger(__name__)

EVALUATION_MODES = ("auto_ingest", "manual", "recovery")

_WELL_EVAL_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}


def _well_eval_lock(org_id: str, well_id: str) -> asyncio.Lock:
    key = (org_id, well_id)
    lock = _WELL_EVAL_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _WELL_EVAL_LOCKS[key] = lock
    return lock


__all__ = [
    "EVALUATION_MODES",
    "AlertRuleService",
    "AlertService",
    "EvaluationReport",
    "rule_spec_from_row",
]


def rule_spec_from_row(row: AlertRule, *, unit: str | None = None) -> RuleSpec:
    """A stored rule as the engine's data, with the thresholds still in the unit they were written in.

    ``unit`` is the *channel's* canonical unit, used for the label; conversion of a declared threshold
    happens in :func:`thresholds_in_channel_units`, next to the channel it is being evaluated against.
    """

    return RuleSpec(
        id=row.id,
        rule_key=row.rule_key,
        name=row.name,
        channel_key=row.channel_key,
        operator=row.operator,
        threshold=float(row.threshold),
        severity=row.severity,
        well_id=row.well_id,
        wellbore_id=row.wellbore_id,
        clear_operator=row.clear_operator,
        clear_threshold=float(row.clear_threshold) if row.clear_threshold is not None else None,
        sustain_seconds=float(row.sustain_seconds or 0.0),
        clear_sustain_seconds=float(row.clear_sustain_seconds or 0.0),
        cooldown_seconds=float(row.cooldown_seconds or 0.0),
        unit=unit,
        declared_unit=row.unit,
        description=row.description,
        enabled=bool(row.is_enabled),
    )


def thresholds_in_channel_units(rule: AlertRule, channel: TimeSeries) -> tuple[float, float | None]:
    """The rule's thresholds expressed in the unit the channel's values are stored in.

    Two cases, and no third:

    * the rule declares no unit — its thresholds are already in the channel's canonical unit (this is
      what every rule written before the column existed means);
    * the rule declares a unit — it is converted through the one central unit engine. A unit from
      another dimension is refused *by name* rather than converted, because a rule written in ``rpm``
      against a pressure channel is a rule somebody mis-configured, and a conversion factor would hide
      that forever.

    The conversion is recorded rather than hidden: the alert's evidence shows the threshold that was
    actually compared and the unit it was written in.
    """

    if not rule.unit or rule.unit == channel.unit:
        return float(rule.threshold), (
            float(rule.clear_threshold) if rule.clear_threshold is not None else None
        )
    try:
        threshold = convert(float(rule.threshold), rule.unit, channel.dimension).value
        clear = (
            convert(float(rule.clear_threshold), rule.unit, channel.dimension).value
            if rule.clear_threshold is not None
            else None
        )
    except ValidationFailed as failure:
        raise ValidationFailed(
            f"rule {rule.rule_key!r} is written in {rule.unit!r}, which the {channel.channel_key!r} "
            f"channel ({channel.dimension}) cannot be measured in",
            details={
                "field": "unit",
                "rule_unit": rule.unit,
                "channel_unit": channel.unit,
                "dimension": channel.dimension,
                "cause": failure.details,
            },
        ) from failure
    return float(threshold), (float(clear) if clear is not None else None)


@dataclass
class EvaluationReport:
    """What one evaluation pass did, in numbers that add up."""

    well_id: str
    evaluation_id: str = field(default_factory=lambda: new_id("evl"))
    mode: str = "manual"
    trigger: str | None = None
    evaluated_at: dt.datetime = field(default_factory=utc_now)
    duration_ms: float = 0.0
    trace_id: str | None = None
    channel_ids: list[str] = field(default_factory=list)
    evaluated: int = 0
    raised: int = 0
    cleared: int = 0
    held: int = 0
    suppressed_cooldown: int = 0
    already_open: int = 0
    insufficient_data: int = 0
    failed: int = 0
    channels_without_points: int = 0
    evaluations: list[Evaluation] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    alerts: list[Alert] = field(default_factory=list)

    @property
    def reconciled(self) -> bool:
        """Every evaluation landed in exactly one outcome bucket.

        A report whose counters do not add up to ``evaluated`` is a report that has lost a decision
        somewhere, so the arithmetic is part of the contract rather than a comment.
        """

        return self.evaluated == (
            self.raised
            + self.cleared
            + self.held
            + self.suppressed_cooldown
            + self.already_open
            + self.insufficient_data
            + self.failed
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_id": self.evaluation_id,
            "well_id": self.well_id,
            "mode": self.mode,
            "trigger": self.trigger,
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "duration_ms": self.duration_ms,
            "trace_id": self.trace_id,
            "channel_ids": list(self.channel_ids),
            "evaluated": self.evaluated,
            "raised": self.raised,
            "cleared": self.cleared,
            "held": self.held,
            "suppressed_cooldown": self.suppressed_cooldown,
            "already_open": self.already_open,
            "insufficient_data": self.insufficient_data,
            "failed": self.failed,
            "channels_without_points": self.channels_without_points,
            "alerts": [item.id for item in self.alerts],
            "evaluations": [item.to_dict() for item in self.evaluations],
            "failures": list(self.failures),
            "reconciled": self.reconciled,
        }


class AlertRuleService:
    """Rules are data: this is the only place they are written, and it validates every field."""

    def __init__(
        self,
        session: AsyncSession,
        org_id: str,
        *,
        principal: Principal | None = None,
        actor_id: str | None = None,
    ) -> None:
        self.session = session
        self.org_id = org_id
        self.actor_id = actor_id
        # The governance ledger records *who*, and it takes a principal rather than an id. A caller that
        # supplied an actor id and no principal (a worker acting on behalf of a named person) would
        # otherwise write an audit row that cannot answer "who did this?" — so the id becomes the
        # principal, rather than the row losing the actor.
        self._principal = principal or (Principal(id=actor_id) if actor_id else None)

    async def create(
        self,
        *,
        rule_key: str,
        name: str,
        channel_key: str,
        operator: str,
        threshold: float,
        severity: str = "medium",
        unit: str | None = None,
        well_id: str | None = None,
        wellbore_id: str | None = None,
        clear_operator: str | None = None,
        clear_threshold: float | None = None,
        sustain_seconds: float = 0.0,
        clear_sustain_seconds: float = 0.0,
        cooldown_seconds: float = 0.0,
        description: str | None = None,
        enabled: bool = True,
        operation_id: str | None = None,
    ) -> AlertRule:
        validate_rule(
            operator=operator,
            threshold=threshold,
            severity=severity,
            sustain_seconds=sustain_seconds,
            clear_sustain_seconds=clear_sustain_seconds,
            cooldown_seconds=cooldown_seconds,
            clear_operator=clear_operator,
            clear_threshold=clear_threshold,
        )
        key = rule_key.strip().lower()
        if not key:
            raise ValidationFailed("a rule needs a key", details={"field": "rule_key"})
        if not name.strip():
            raise ValidationFailed("a rule needs a name", details={"field": "name"})
        if unit is not None:
            if not unit.strip():
                raise ValidationFailed("a rule unit cannot be blank", details={"field": "unit"})
            # The symbol must be one the unit engine knows at all; *which* dimension it belongs to is
            # checked when the rule meets a channel, because that is the first moment the answer exists.
            if not unit_dimensions(unit):
                raise ValidationFailed(
                    "the rule unit is not one the platform knows",
                    details={"field": "unit", "value": unit, "known": list(known_units())[:20]},
                )

        row = AlertRule(
            id=new_id("arl"),
            org_id=self.org_id,
            # A rule is scoped to a well or wellbore; ``AlertRule`` has no project column, and a rule with
            # no well applies to every well its channel key matches.
            well_id=well_id,
            wellbore_id=wellbore_id,
            operation_id=operation_id,
            rule_key=key,
            name=name.strip(),
            description=description,
            channel_key=channel_key.strip().lower(),
            operator=operator,
            threshold=float(threshold),
            clear_operator=clear_operator,
            clear_threshold=float(clear_threshold) if clear_threshold is not None else None,
            sustain_seconds=float(sustain_seconds),
            clear_sustain_seconds=float(clear_sustain_seconds),
            cooldown_seconds=float(cooldown_seconds),
            severity=severity,
            unit=unit.strip() if unit and unit.strip() else None,
            is_enabled=bool(enabled),
            attributes={},
        )
        self.session.add(row)
        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal,
            action="alert.rule_manage",
            resource_kind="alert_rule",
            resource_id=row.id,
            after={
                "rule_key": row.rule_key,
                "channel_key": row.channel_key,
                "operator": row.operator,
                "threshold": row.threshold,
                "unit": row.unit,
                "severity": row.severity,
                "sustain_seconds": row.sustain_seconds,
                "cooldown_seconds": row.cooldown_seconds,
            },
            details={"action": "create"},
            well_id=well_id,
        )
        return row

    async def update(
        self,
        rule_id: str,
        *,
        expected_updated_at: dt.datetime,
        reason: str,
        **changes: Any,
    ) -> AlertRule:
        row = await self.get(rule_id)
        _check_version(row.updated_at, expected_updated_at, resource="alert rule")
        if not (reason or "").strip():
            raise ValidationFailed("a rule change needs a reason", details={"field": "reason"})
        before = _rule_snapshot(row)
        merged = {**_rule_snapshot(row), **{key: value for key, value in changes.items() if value is not None}}
        validate_rule(
            operator=merged["operator"],
            threshold=merged["threshold"],
            severity=merged["severity"],
            sustain_seconds=merged["sustain_seconds"],
            clear_sustain_seconds=merged["clear_sustain_seconds"],
            cooldown_seconds=merged["cooldown_seconds"],
            clear_operator=merged.get("clear_operator"),
            clear_threshold=merged.get("clear_threshold"),
        )
        for key, value in changes.items():
            if value is None:
                continue
            if key == "enabled":
                row.is_enabled = bool(value)
                continue
            if not hasattr(row, key):
                raise ValidationFailed(
                    "the rule has no such field", details={"field": key, "allowed": sorted(before)}
                )
            setattr(row, key, value)
        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal,
            action="alert.rule_manage",
            resource_kind="alert_rule",
            resource_id=row.id,
            before=before,
            after=_rule_snapshot(row),
            details={"action": "update", "reason": reason},
            well_id=row.well_id,
        )
        return row

    async def get(self, rule_id: str) -> AlertRule:
        row = (
            await self.session.execute(
                select(AlertRule).where(AlertRule.id == rule_id, AlertRule.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"alert rule {rule_id!r} not found")
        return row

    async def list(
        self,
        *,
        well_id: str | None = None,
        channel_key: str | None = None,
        enabled: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[AlertRule], int]:
        clauses: list[Any] = [AlertRule.org_id == self.org_id]
        if well_id is not None:
            clauses.append(AlertRule.well_id == well_id)
        if channel_key is not None:
            clauses.append(AlertRule.channel_key == channel_key.strip().lower())
        if enabled is not None:
            clauses.append(AlertRule.is_enabled.is_(enabled))
        total = int(
            (
                await self.session.execute(select(func.count()).select_from(select(AlertRule.id).where(*clauses).subquery()))
            ).scalar_one()
        )
        rows = (
            (
                await self.session.execute(
                    select(AlertRule)
                    .where(*clauses)
                    .order_by(AlertRule.rule_key.asc())
                    .limit(min(limit, 500))
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), total


def _rule_snapshot(row: AlertRule) -> dict[str, Any]:
    return {
        "rule_key": row.rule_key,
        "name": row.name,
        "channel_key": row.channel_key,
        "operator": row.operator,
        "threshold": float(row.threshold),
        "unit": row.unit,
        "severity": row.severity,
        "clear_operator": row.clear_operator,
        "clear_threshold": float(row.clear_threshold) if row.clear_threshold is not None else None,
        "sustain_seconds": float(row.sustain_seconds or 0.0),
        "clear_sustain_seconds": float(row.clear_sustain_seconds or 0.0),
        "cooldown_seconds": float(row.cooldown_seconds or 0.0),
        "enabled": bool(row.is_enabled),
        "well_id": row.well_id,
        "wellbore_id": row.wellbore_id,
    }


def _check_version(stored: dt.datetime | None, expected: dt.datetime, *, resource: str) -> None:
    """Optimistic concurrency, in the same shape the rest of the platform uses.

    ``expected_updated_at`` travels with the mutation so two people acting on one alert cannot both
    "win". A caller that read the alert before somebody else acknowledged it gets a 409 with the current
    state rather than silently overwriting the decision.
    """

    if stored is None:
        return
    stored_aware = stored if stored.tzinfo else stored.replace(tzinfo=dt.UTC)
    expected_aware = expected if expected.tzinfo else expected.replace(tzinfo=dt.UTC)
    if abs((stored_aware - expected_aware).total_seconds()) > 1e-6:
        raise Conflict(
            f"the {resource} changed since it was read",
            details={
                "resource": resource,
                "expected_updated_at": expected_aware.isoformat(),
                "stored_updated_at": stored_aware.isoformat(),
            },
        )


class AlertService:
    """Every alert read and write, with the tenant boundary applied first."""

    def __init__(
        self,
        session: AsyncSession,
        org_id: str,
        *,
        principal: Principal | None = None,
        actor_id: str | None = None,
    ) -> None:
        self.session = session
        self.org_id = org_id
        self.actor_id = actor_id
        # See AlertRuleService: the ledger needs a principal, and an actor id alone must not lose the
        # actor from the record.
        self._principal = principal or (Principal(id=actor_id) if actor_id else None)

    # ------------------------------------------------------------------ reads

    def statement(
        self,
        *,
        well_id: str | None = None,
        wellbore_id: str | None = None,
        status: str | None = None,
        severity: str | None = None,
        rule_id: str | None = None,
        rule_ref: str | None = None,
        series_id: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> Select:
        clauses: list[Any] = [Alert.org_id == self.org_id]
        if well_id is not None:
            clauses.append(Alert.well_id == well_id)
        if wellbore_id is not None:
            clauses.append(Alert.wellbore_id == wellbore_id)
        if status is not None:
            _require_status(status)
            clauses.append(Alert.status == status)
        if severity is not None:
            clauses.append(Alert.severity == severity)
        if rule_id is not None:
            clauses.append(Alert.rule_id == rule_id)
        if rule_ref is not None:
            clauses.append(Alert.rule_ref == rule_ref)
        if series_id is not None:
            clauses.append(Alert.series_id == series_id)
        if since is not None:
            clauses.append(Alert.raised_at >= since)
        if until is not None:
            clauses.append(Alert.raised_at <= until)
        return select(Alert).where(*clauses)

    async def list(self, *, limit: int = 100, offset: int = 0, **filters: Any) -> tuple[list[Alert], int]:
        statement = self.statement(**filters)
        total = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(statement.order_by(None).subquery())
                )
            ).scalar_one()
        )
        rows = (
            (
                await self.session.execute(
                    statement.order_by(Alert.raised_at.desc(), Alert.id.desc())
                    .limit(min(limit, 500))
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        return list(rows), total

    async def get(self, alert_id: str) -> Alert:
        row = (
            await self.session.execute(
                select(Alert).where(Alert.id == alert_id, Alert.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"alert {alert_id!r} not found")
        return row

    async def open_alerts(self, *, well_id: str | None = None, limit: int = 100) -> list[Alert]:
        """Everything not yet closed — what an operator is expected to be looking at right now."""

        clauses = [Alert.org_id == self.org_id, Alert.status.notin_(tuple(ALERT_TERMINAL_STATUSES))]
        if well_id is not None:
            clauses.append(Alert.well_id == well_id)
        rows = (
            (
                await self.session.execute(
                    select(Alert)
                    .where(*clauses)
                    .order_by(Alert.severity.desc(), Alert.raised_at.desc())
                    .limit(min(limit, 500))
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def evidence(self, alert_id: str, *, points: int = 20) -> dict[str, Any]:
        """Why this alert exists: the rule, the observed values, the points behind it, the life cycle.

        "Why was this raised?" is answered from stored facts, not from a re-run of the rule: the rule
        version that raised it is recorded on the row (``rule_snapshot``), the point that breached is
        named, and the surrounding measurements are the *actual* stored points up to ``alert.observed_at``
        so later telemetry at T2 never overwrites the historical T1 evidence that raised the alert.
        """

        alert = await self.get(alert_id)
        rule = None
        if alert.rule_id:
            rule = (
                await self.session.execute(
                    select(AlertRule).where(
                        AlertRule.id == alert.rule_id, AlertRule.org_id == self.org_id
                    )
                )
            ).scalar_one_or_none()
        surrounding: list[dict[str, Any]] = []
        if alert.series_id:
            point_clauses: list[Any] = [TimeSeriesPoint.series_id == alert.series_id]
            if alert.observed_at is not None:
                point_clauses.append(TimeSeriesPoint.ts <= alert.observed_at)
            rows = (
                (
                    await self.session.execute(
                        select(TimeSeriesPoint)
                        .where(*point_clauses)
                        .order_by(TimeSeriesPoint.ts.desc(), TimeSeriesPoint.id.desc())
                        .limit(min(points, 200))
                    )
                )
                .scalars()
                .all()
            )
            surrounding = [
                {
                    "id": row.id,
                    "ts": row.ts.isoformat() if row.ts else None,
                    "value": row.value,
                    "quality": row.quality,
                    "quality_flags": [
                        *(["late"] if row.is_late else []),
                        *(["out_of_order"] if row.is_out_of_order else []),
                        *list((row.attributes or {}).get("quality_flags") or []),
                    ],
                    "is_late": row.is_late,
                    "is_out_of_order": row.is_out_of_order,
                }
                for row in rows
            ]
        timeline = [
            {
                "state": "raised",
                "at": alert.raised_at.isoformat() if alert.raised_at else None,
                "by": alert.raised_by,
                "reason": (alert.attributes or {}).get("raise_reason"),
            }
        ]
        if alert.acknowledged_at:
            timeline.append(
                {
                    "state": "acknowledged",
                    "at": alert.acknowledged_at.isoformat(),
                    "by": alert.acknowledged_by,
                    "reason": (alert.attributes or {}).get("acknowledge_reason"),
                }
            )
        if alert.cleared_at:
            timeline.append(
                {
                    "state": alert.status,
                    "at": alert.cleared_at.isoformat(),
                    "by": (alert.attributes or {}).get("closed_by"),
                    "reason": (alert.attributes or {}).get("clear_reason")
                    or alert.cancelled_reason,
                }
            )
        audit_rows = (
            (
                await self.session.execute(
                    select(AuditLog)
                    .where(
                        AuditLog.org_id == self.org_id,
                        AuditLog.resource_kind == "alert",
                        AuditLog.resource_id == alert.id,
                    )
                    .order_by(AuditLog.occurred_at.asc(), AuditLog.id.asc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        audit_events = [
            {
                "id": row.id,
                "action": row.action,
                "actor_id": row.actor_id,
                "actor_kind": row.actor_kind,
                "occurred_at": row.occurred_at.isoformat() if row.occurred_at else None,
                "reason": (row.details or {}).get("reason"),
                "before": row.before,
                "after": row.after,
            }
            for row in audit_rows
        ]
        return {
            "alert": alert,
            "rule": rule,
            "rule_snapshot": (alert.attributes or {}).get("rule_snapshot"),
            "evaluation": (alert.attributes or {}).get("evaluation"),
            "observed": {
                "value": alert.observed_value,
                "threshold": alert.threshold_value,
                "clear_observed_value": alert.clear_observed_value,
                "unit": alert.unit,
                "observed_at": alert.observed_at.isoformat() if alert.observed_at else None,
                "sustained_seconds": alert.sustained_seconds,
                # What the rule was written with, when it declared a unit: the alert shows "4 000 psi"
                # beside the pascal figure it compared, so nobody has to reverse the conversion.
                "declared_threshold": (alert.attributes or {}).get("rule_snapshot", {}).get("threshold"),
                "declared_unit": (alert.attributes or {}).get("rule_snapshot", {}).get("unit"),
            },
            "provenance": {
                "series_id": alert.series_id,
                "channel_key": (alert.attributes or {}).get("channel_key"),
                "dimension": (alert.attributes or {}).get("dimension"),
                "point_id": alert.source_point_id,
                "rule_id": alert.rule_id,
                "rule_ref": alert.rule_ref,
                "well_id": alert.well_id,
                "wellbore_id": alert.wellbore_id,
                "section_id": alert.section_id,
                "operation_id": alert.operation_id,
            },
            "points": surrounding,
            "timeline": timeline,
            "audit_events": audit_events,
        }

    # ------------------------------------------------------------------ life cycle

    async def acknowledge(
        self,
        alert_id: str,
        *,
        expected_updated_at: dt.datetime,
        reason: str | None = None,
    ) -> Alert:
        alert = await self.get(alert_id)
        _check_version(alert.updated_at, expected_updated_at, resource="alert")
        self._require_transition(alert, "acknowledged")
        before = _alert_snapshot(alert)
        alert.status = "acknowledged"
        alert.acknowledged_at = utc_now()
        alert.acknowledged_by = self.actor_id or (self._principal.id if self._principal else None)
        alert.attributes = {
            **(alert.attributes or {}),
            "acknowledge_reason": reason or "",
        }
        await self.session.flush()
        await self._audit(alert, before, "alert.acknowledge", reason)
        await emit(
            self.session,
            org_id=self.org_id,
            type="alert.acknowledged",
            subject_kind="alert",
            subject_id=alert.id,
            well_id=alert.well_id,
            wellbore_id=alert.wellbore_id,
            payload={
                "alert_id": alert.id,
                "severity": alert.severity,
                "status": alert.status,
                "rule_ref": alert.rule_ref,
                "acknowledged_by": alert.acknowledged_by,
                "reason": reason or "",
            },
        )
        return alert

    async def clear(
        self,
        alert_id: str,
        *,
        expected_updated_at: dt.datetime,
        reason: str,
        observed_value: float | None = None,
    ) -> Alert:
        """Close an alert because its condition is no longer true. A reason is required: "who decided
        this is over, and on what basis" is the question an incident review asks first."""

        return await self._close(
            alert_id,
            target_status="cleared",
            expected_updated_at=expected_updated_at,
            reason=reason,
            observed_value=observed_value,
        )

    async def cancel(
        self,
        alert_id: str,
        *,
        expected_updated_at: dt.datetime,
        reason: str,
    ) -> Alert:
        """Close an alert because a person judged it not worth acting on. Distinct from ``clear`` on
        purpose: the difference is whether the *condition* ended or the *attention* did."""

        return await self._close(
            alert_id,
            target_status="cancelled",
            expected_updated_at=expected_updated_at,
            reason=reason,
            observed_value=None,
        )

    async def _close(
        self,
        alert_id: str,
        *,
        target_status: str,
        expected_updated_at: dt.datetime,
        reason: str,
        observed_value: float | None,
    ) -> Alert:
        alert = await self.get(alert_id)
        _check_version(alert.updated_at, expected_updated_at, resource="alert")
        self._require_transition(alert, target_status)
        if not (reason or "").strip():
            raise ValidationFailed(
                f"closing an alert as {target_status} needs a reason", details={"field": "reason"}
            )
        before = _alert_snapshot(alert)
        alert.status = target_status
        alert.cleared_at = utc_now()
        if observed_value is not None:
            alert.clear_observed_value = float(observed_value)
        alert.attributes = {
            **(alert.attributes or {}),
            "clear_reason": reason,
            "closed_by": self.actor_id or (self._principal.id if self._principal else None),
        }
        if target_status == "cancelled":
            alert.cancelled_reason = reason
        await self.session.flush()
        action = "alert.clear" if target_status == "cleared" else "alert.cancel"
        await self._audit(alert, before, action, reason)
        await emit(
            self.session,
            org_id=self.org_id,
            type=f"alert.{target_status}",
            subject_kind="alert",
            subject_id=alert.id,
            well_id=alert.well_id,
            wellbore_id=alert.wellbore_id,
            payload={
                "alert_id": alert.id,
                "severity": alert.severity,
                "status": alert.status,
                "rule_ref": alert.rule_ref,
                "reason": reason,
                "observed_value": alert.clear_observed_value,
            },
        )
        return alert

    def _require_transition(self, alert: Alert, target: str) -> None:
        current = alert.status if alert.status in ALERT_STATUSES else "raised"
        if target not in alert_transitions(current):
            raise Conflict(
                f"an alert in state {current!r} cannot become {target!r}",
                details={
                    "alert_id": alert.id,
                    "status": current,
                    "requested": target,
                    "allowed": sorted(alert_transitions(current)),
                },
            )

    async def _audit(self, alert: Alert, before: dict[str, Any], action: str, reason: str | None) -> None:
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal,
            action=action,
            resource_kind="alert",
            resource_id=alert.id,
            before=before,
            after=_alert_snapshot(alert),
            details={
                "reason": reason or "",
                "severity": alert.severity,
                "rule_ref": alert.rule_ref,
                # The alert's own action level (what acting on it would mean). The audit row's
                # ``action_level`` column is resolved from the action catalogue and is not passed here.
                "alert_action_level": alert.action_level,
                "well_id": alert.well_id,
            },
            well_id=alert.well_id,
        )

    # ------------------------------------------------------------------ evaluation

    async def evaluate_well(
        self,
        well_id: str,
        *,
        now: dt.datetime | None = None,
        channel_ids: Sequence[str] | None = None,
        mode: str = "manual",
        isolate_rule_errors: bool = False,
        trigger: str | None = None,
        trace_id: str | None = None,
    ) -> EvaluationReport:
        """Evaluate every enabled rule that watches this well, and act on the answers.

        ``channel_ids`` narrows the pass to channels a caller has just written to — the ingestion path
        passes the batch's channels so a rule fires as soon as its condition is sustained, without
        rescanning channels nothing happened to.
        """

        if mode not in EVALUATION_MODES:
            raise ValidationFailed(
                "unknown alert evaluation mode",
                details={"field": "mode", "value": mode, "allowed": list(EVALUATION_MODES)},
            )
        started_perf = time.perf_counter()
        moment = now or utc_now()
        isolate_errors = bool(isolate_rule_errors or mode in {"auto_ingest", "recovery"})

        # A well the caller's organisation does not own is *not found*, exactly as an unknown well id is:
        # an evaluation pass over somebody else's well must not be answerable with an empty report that
        # looks like a well with no channels.
        owned = (
            await self.session.execute(
                select(Well.id).where(Well.id == well_id, Well.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if owned is None:
            raise NotFound(f"well {well_id!r} not found")

        scoped_channels = list(channel_ids) if channel_ids is not None else []
        report = EvaluationReport(
            well_id=well_id,
            mode=mode,
            trigger=trigger or mode,
            evaluated_at=moment,
            trace_id=trace_id,
            channel_ids=scoped_channels,
        )

        async with _well_eval_lock(self.org_id, well_id):
            rules = await self._rules_for(well_id)
            if channel_ids is None:
                channels = await self._channels_for(well_id, None)
            else:
                channels = await self._channels_for(well_id, scoped_channels)
            report.channel_ids = [channel.id for channel in channels]
            by_key: dict[str, list[TimeSeries]] = {}
            for channel in channels:
                by_key.setdefault(channel.channel_key, []).append(channel)
            if not channels:
                report.duration_ms = round((time.perf_counter() - started_perf) * 1000.0, 3)
                return report

            locked_channels: set[str] = set()
            is_postgres = (
                self.session.bind is not None and self.session.bind.dialect.name == "postgresql"
            )

            for rule in rules:
                watched = by_key.get(rule.channel_key, [])
                if rule.wellbore_id:
                    watched = [channel for channel in watched if channel.wellbore_id == rule.wellbore_id]
                for channel in watched:
                    if is_postgres and channel.id not in locked_channels:
                        await self.session.execute(
                            select(TimeSeries.id)
                            .where(TimeSeries.id == channel.id, TimeSeries.org_id == self.org_id)
                            .with_for_update()
                        )
                        locked_channels.add(channel.id)
                    observations = await self._observations(channel.id)
                    if not observations:
                        report.channels_without_points += 1
                        continue
                    if isolate_errors:
                        try:
                            async with self.session.begin_nested():
                                await self._evaluate_rule_on_channel(
                                    rule=rule,
                                    channel=channel,
                                    observations=observations,
                                    moment=moment,
                                    report=report,
                                )
                        except Exception as exc:
                            report.evaluated += 1
                            report.failed += 1
                            failure = {
                                "rule_id": rule.id,
                                "rule_key": rule.rule_key,
                                "channel_id": channel.id,
                                "channel_key": channel.channel_key,
                                "error_code": getattr(exc, "code", "evaluation_error"),
                                "reason": str(exc),
                            }
                            report.failures.append(failure)
                            report.evaluations.append(
                                Evaluation(
                                    rule_id=rule.id,
                                    rule_key=rule.rule_key,
                                    channel_id=channel.id,
                                    channel_key=channel.channel_key,
                                    outcome="error",
                                    observed_value=None,
                                    observed_at=None,
                                    point_id=None,
                                    sustained_seconds=0.0,
                                    samples=len(observations),
                                    breaching_samples=0,
                                    excluded_quality=0,
                                    threshold=float(rule.threshold)
                                    if isinstance(rule.threshold, int | float)
                                    else 0.0,
                                    clear_threshold=float(rule.clear_threshold)
                                    if isinstance(rule.clear_threshold, int | float)
                                    else 0.0,
                                    unit=channel.unit,
                                    severity=rule.severity,
                                    reason=str(exc),
                                    basis={"error": failure},
                                )
                            )
                            logger.warning(
                                "alert rule evaluation failed in isolated mode",
                                extra={
                                    "extra_fields": {
                                        "evaluation_id": report.evaluation_id,
                                        "well_id": well_id,
                                        "mode": mode,
                                        "trace_id": trace_id,
                                        **failure,
                                    }
                                },
                            )
                    else:
                        await self._evaluate_rule_on_channel(
                            rule=rule,
                            channel=channel,
                            observations=observations,
                            moment=moment,
                            report=report,
                        )

        report.duration_ms = round((time.perf_counter() - started_perf) * 1000.0, 3)
        logger.info(
            "alert evaluation completed",
            extra={
                "extra_fields": {
                    "evaluation_id": report.evaluation_id,
                    "well_id": well_id,
                    "mode": report.mode,
                    "trigger": report.trigger,
                    "channel_ids": report.channel_ids,
                    "evaluated": report.evaluated,
                    "raised": report.raised,
                    "cleared": report.cleared,
                    "held": report.held,
                    "suppressed_cooldown": report.suppressed_cooldown,
                    "already_open": report.already_open,
                    "insufficient_data": report.insufficient_data,
                    "failed": report.failed,
                    "duration_ms": report.duration_ms,
                    "trace_id": report.trace_id,
                }
            },
        )
        return report

    async def _evaluate_rule_on_channel(
        self,
        *,
        rule: AlertRule,
        channel: TimeSeries,
        observations: list[Observation],
        moment: dt.datetime,
        report: EvaluationReport,
    ) -> None:
        spec = rule_spec_from_row(rule)
        # The thresholds are put into the unit the stored values are in — explicitly, through
        # the unit engine, and only when the rule declares a unit at all. A rule whose declared
        # unit cannot measure this channel refuses here, by name, rather than comparing numbers
        # from two different scales.
        threshold, clear_threshold = thresholds_in_channel_units(rule, channel)
        outcome = evaluate(
            RuleSpec(
                **{
                    **spec.__dict__,
                    "threshold": threshold,
                    "clear_threshold": clear_threshold,
                    "unit": channel.unit,
                    "declared_unit": rule.unit,
                    "declared_threshold": float(rule.threshold),
                }
            ),
            observations,
            channel_id=channel.id,
            channel_key=channel.channel_key,
            now=moment,
        )
        report.evaluated += 1
        report.evaluations.append(outcome)
        if outcome.outcome == "raise":
            await self._act_on_raise(rule, channel, outcome, moment, report)
        elif outcome.outcome == "clear":
            await self._act_on_clear(rule, channel, outcome, moment, report)
        elif outcome.outcome == "hold":
            report.held += 1
        else:
            report.insufficient_data += 1

    async def _rules_for(self, well_id: str) -> list[AlertRule]:
        rows = (
            (
                await self.session.execute(
                    select(AlertRule)
                    .where(
                        AlertRule.org_id == self.org_id,
                        AlertRule.is_enabled.is_(True),
                        (AlertRule.well_id == well_id) | (AlertRule.well_id.is_(None)),
                    )
                    .order_by(AlertRule.rule_key.asc())
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def _channels_for(self, well_id: str, channel_ids: list[str] | None) -> list[TimeSeries]:
        clauses: list[Any] = [TimeSeries.org_id == self.org_id, TimeSeries.well_id == well_id]
        if channel_ids is not None:
            clauses.append(TimeSeries.id.in_(channel_ids))
        rows = (
            (await self.session.execute(select(TimeSeries).where(*clauses).limit(500))).scalars().all()
        )
        return list(rows)

    async def _observations(self, series_id: str) -> list[Observation]:
        rows = (
            (
                await self.session.execute(
                    select(
                        TimeSeriesPoint.id,
                        TimeSeriesPoint.ts,
                        TimeSeriesPoint.value,
                        TimeSeriesPoint.quality,
                        TimeSeriesPoint.sequence,
                    )
                    .where(TimeSeriesPoint.series_id == series_id)
                    .order_by(TimeSeriesPoint.ts.desc(), TimeSeriesPoint.id.desc())
                    .limit(MAX_EVALUATION_POINTS)
                )
            )
            .all()
        )
        return [
            Observation(
                ts=row.ts if row.ts.tzinfo else row.ts.replace(tzinfo=dt.UTC),
                value=row.value,
                quality=row.quality or "missing",
                point_id=row.id,
                sequence=row.sequence,
            )
            for row in reversed(rows)
        ]

    async def _act_on_raise(
        self,
        rule: AlertRule,
        channel: TimeSeries,
        outcome: Evaluation,
        moment: dt.datetime,
        report: EvaluationReport,
    ) -> None:
        open_alert = await self._open_alert(rule.id, channel.id)
        if open_alert is not None:
            report.already_open += 1
            # The condition is still true: the observation is recorded on the open alert rather than
            # raising a second one. "Checked again at 09:41, still breaching" is useful; a duplicate
            # alert is noise that trains people to ignore the list.
            open_alert.attributes = {
                **(open_alert.attributes or {}),
                "last_evaluated_at": moment.isoformat(),
                "last_observed_value": outcome.observed_value,
                "evaluations": int((open_alert.attributes or {}).get("evaluations", 1)) + 1,
            }
            await self.session.flush()
            return

        cooled = await self._within_cooldown(rule, channel.id, moment)
        if cooled is not None:
            report.suppressed_cooldown += 1
            report.evaluations[-1] = Evaluation(
                **{
                    **outcome.__dict__,
                    "outcome": "hold",
                    "reason": (
                        f"the rule raised at {cooled.isoformat()} and its "
                        f"{rule.cooldown_seconds:.0f}s cooldown has not elapsed"
                    ),
                }
            )
            return

        alert = await self._raise(rule, channel, outcome, moment)
        report.raised += 1
        report.alerts.append(alert)

    async def _act_on_clear(
        self,
        rule: AlertRule,
        channel: TimeSeries,
        outcome: Evaluation,
        moment: dt.datetime,
        report: EvaluationReport,
    ) -> None:
        open_alert = await self._open_alert(rule.id, channel.id)
        if open_alert is None:
            # Nothing is open, so there is nothing to clear. Counting it as "held" keeps the report's
            # arithmetic honest — the evaluation happened and changed no state.
            report.held += 1
            return
        before = _alert_snapshot(open_alert)
        open_alert.status = "cleared"
        open_alert.cleared_at = moment
        open_alert.clear_observed_value = outcome.observed_value
        open_alert.attributes = {
            **(open_alert.attributes or {}),
            "clear_reason": outcome.reason,
            "closed_by": "rule",
            "cleared_by_rule_since": moment.isoformat(),
        }
        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal,
            action="alert.clear",
            resource_kind="alert",
            resource_id=open_alert.id,
            before=before,
            after=_alert_snapshot(open_alert),
            details={"reason": outcome.reason, "cleared_by": "rule", "rule_ref": rule.rule_key},
            well_id=open_alert.well_id,
        )
        await emit(
            self.session,
            org_id=self.org_id,
            type="alert.cleared",
            subject_kind="alert",
            subject_id=open_alert.id,
            well_id=open_alert.well_id,
            wellbore_id=open_alert.wellbore_id,
            payload={
                "alert_id": open_alert.id,
                "severity": open_alert.severity,
                "status": "cleared",
                "rule_ref": rule.rule_key,
                "reason": outcome.reason,
                "observed_value": outcome.observed_value,
                "cleared_by": "rule",
            },
        )
        report.cleared += 1
        report.alerts.append(open_alert)

    async def _raise(
        self, rule: AlertRule, channel: TimeSeries, outcome: Evaluation, moment: dt.datetime
    ) -> Alert:
        alert = Alert(
            id=new_id("alt"),
            org_id=self.org_id,
            well_id=channel.well_id,
            wellbore_id=channel.wellbore_id,
            operation_id=rule.operation_id,
            kind="telemetry",
            severity=rule.severity,
            title=f"{rule.name}: {channel.name} {outcome.observed_value:g} {channel.unit}",
            description=outcome.reason,
            status="raised",
            action_level="L2",
            raised_at=moment,
            raised_by="rule",
            rule_ref=rule.rule_key,
            rule_id=rule.id,
            subject_kind="time_series",
            subject_id=channel.id,
            series_id=channel.id,
            source_point_id=outcome.point_id,
            section_id=None,
            observed_value=outcome.observed_value,
            threshold_value=outcome.threshold,
            unit=channel.unit,
            observed_at=outcome.observed_at,
            sustained_seconds=outcome.sustained_seconds,
            notified_channels=[],
            attributes={
                "raise_reason": outcome.reason,
                "rule_snapshot": _rule_snapshot(rule),
                "evaluation": outcome.to_dict(),
                "evaluations": 1,
                "dimension": channel.dimension,
                "channel_key": channel.channel_key,
            },
        )
        self.session.add(alert)
        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal,
            action="alert.raise",
            resource_kind="alert",
            resource_id=alert.id,
            after=_alert_snapshot(alert),
            details={
                "reason": outcome.reason,
                "rule_ref": rule.rule_key,
                "observed": outcome.to_dict()["observed"],
                "severity": rule.severity,
            },
            well_id=alert.well_id,
        )
        await emit(
            self.session,
            org_id=self.org_id,
            type="alert.raised",
            subject_kind="alert",
            subject_id=alert.id,
            well_id=alert.well_id,
            wellbore_id=alert.wellbore_id,
            payload={
                "alert_id": alert.id,
                "severity": alert.severity,
                "status": "raised",
                "rule_ref": rule.rule_key,
                "title": alert.title,
                "observed": outcome.to_dict()["observed"],
                "sustained_seconds": alert.sustained_seconds,
                "channel_id": channel.id,
            },
        )
        return alert

    async def _open_alert(self, rule_id: str, series_id: str) -> Alert | None:
        return (
            await self.session.execute(
                select(Alert)
                .where(
                    Alert.org_id == self.org_id,
                    Alert.rule_id == rule_id,
                    Alert.series_id == series_id,
                    Alert.status.notin_(tuple(ALERT_TERMINAL_STATUSES)),
                )
                .order_by(Alert.raised_at.desc())
                .limit(1)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def _within_cooldown(
        self, rule: AlertRule, series_id: str, moment: dt.datetime
    ) -> dt.datetime | None:
        """The time the last alert for this rule and channel closed, if it is still inside the cooldown."""

        cooldown = float(rule.cooldown_seconds or 0.0)
        if cooldown <= 0:
            return None
        last_closed = (
            await self.session.execute(
                select(func.max(Alert.cleared_at)).where(
                    Alert.org_id == self.org_id,
                    Alert.rule_id == rule.id,
                    Alert.series_id == series_id,
                    Alert.status.in_(("cleared", "cancelled")),
                )
            )
        ).scalar_one_or_none()
        if last_closed is None:
            return None
        closed_at = last_closed if last_closed.tzinfo else last_closed.replace(tzinfo=dt.UTC)
        if (moment - closed_at).total_seconds() < cooldown:
            return closed_at
        return None


def _alert_snapshot(alert: Alert) -> dict[str, Any]:
    return {
        "status": alert.status,
        "severity": alert.severity,
        "acknowledged_at": alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
        "acknowledged_by": alert.acknowledged_by,
        "cleared_at": alert.cleared_at.isoformat() if alert.cleared_at else None,
        "clear_observed_value": alert.clear_observed_value,
        "observed_value": alert.observed_value,
        "threshold_value": alert.threshold_value,
    }


def _require_status(status: str) -> None:
    if status not in ALERT_STATUSES:
        raise ValidationFailed(
            "the alert status is not one the platform uses",
            details={"field": "status", "value": status, "allowed": list(ALERT_STATUSES)},
        )


def statuses() -> Iterable[str]:
    return ALERT_STATUSES
