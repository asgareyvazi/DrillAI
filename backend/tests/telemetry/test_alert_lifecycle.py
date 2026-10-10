"""The alert life cycle at the service level: transitions, versions, cooldowns, idempotence, evidence.

Everything here drives the real service against a real database, with points written through
``TelemetryService`` — the same append path the ingestion API uses, so a rule is evaluated over rows that
were actually stored rather than over a list handed to the engine. The rule arithmetic itself is tested in
``test_rules.py``; what is tested here is what the platform *does* with the answer, which is where the
interesting failures live: a second alert for the same open condition, a clear that arrives during a
cooldown, two people acknowledging at once, an alert nobody can explain afterwards.

The clock is moved explicitly (``now=`` on every evaluation) so "the cooldown has not elapsed yet" is a
statement about the arithmetic rather than about how fast the test machine ran.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from tests.fixtures.fabric import Fabric

from drillai.core.errors import Conflict, NotFound, ValidationFailed
from drillai.db.models import Alert, AlertRule, AuditLog, OutboxEvent
from drillai.telemetry.alerts import AlertRuleService, AlertService
from drillai.telemetry.service import PointIn, TelemetryService

NOW = dt.datetime(2026, 3, 15, 12, 0, tzinfo=dt.UTC)
#: The channel is registered in psi; stored values and compared thresholds are in the canonical Pa.
PSI = 6894.757293168


async def _well(fabric: Fabric, tenant) -> str:
    """A channel to alert on, with 4000 psi worth of pressure in it."""

    channel = await fabric.http.post(
        "/api/v1/timeseries",
        json={
            "well_id": tenant.well_id,
            "channel_key": "spp",
            "name": "Standpipe pressure",
            "dimension": "pressure",
            "unit": "psi",
            "source": "witsml",
        },
        headers=tenant.headers,
    )
    assert channel.status_code == 201, channel.text
    return channel.json()["id"]


async def _append(fabric: Fabric, tenant, series_id: str, values: list[tuple[dt.datetime, float]]) -> None:
    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(tenant))
        report = await service.append_points(
            series_id,
            [PointIn(ts=ts, value=value, unit="psi", quality="good") for ts, value in values],
            auto_evaluate=False,
        )
        assert report.accepted == len(values), report.to_dict()
        await session.commit()


async def _rule(fabric: Fabric, tenant, **overrides) -> AlertRule:
    from drillai.security.actions import Principal

    body = {
        "rule_key": "spp-high",
        "name": "Standpipe pressure high",
        "channel_key": "spp",
        "operator": "gt",
        "threshold": 4000.0,
        "unit": "psi",
        "severity": "high",
        "sustain_seconds": 0.0,
        "cooldown_seconds": 0.0,
        "well_id": tenant.well_id,
    }
    body.update(overrides)
    async with fabric.session() as session:
        row = await AlertRuleService(
            session, fabric.org_id(tenant), principal=Principal(id="usr_test", role_keys=("engineer",))
        ).create(**body)
        await session.commit()
        return row


async def _evaluate(fabric: Fabric, tenant, *, now: dt.datetime = NOW, channel_ids=None):
    async with fabric.session() as session:
        report = await AlertService(session, fabric.org_id(tenant)).evaluate_well(
            tenant.well_id, now=now, channel_ids=channel_ids
        )
        await session.commit()
        return report


async def _alerts(fabric: Fabric, tenant) -> list[Alert]:
    async with fabric.session() as session:
        return list(
            (
                await session.execute(
                    select(Alert).where(Alert.org_id == fabric.org_id(tenant)).order_by(Alert.raised_at)
                )
            )
            .scalars()
            .all()
        )


# --------------------------------------------------------------------------- raising


async def test_a_sustained_breach_raises_one_alert_with_its_arithmetic_attached(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(
        fabric, fabric.alpha, series_id, [(NOW - dt.timedelta(seconds=30), 4100.0), (NOW, 4200.0)]
    )
    rule = await _rule(fabric, fabric.alpha, sustain_seconds=20.0)

    report = await _evaluate(fabric, fabric.alpha)
    assert report.raised == 1 and report.reconciled is True

    alert = (await _alerts(fabric, fabric.alpha))[0]
    assert alert.status == "raised"
    assert alert.severity == "high"
    assert alert.rule_id == rule.id and alert.rule_ref == "spp-high"
    assert alert.series_id == series_id
    assert alert.observed_value == pytest.approx(4200.0 * PSI), "stored in the canonical unit"
    assert alert.threshold_value == pytest.approx(
        4000.0 * PSI
    ), "the psi threshold the rule was written with, converted once"
    assert alert.unit == "Pa", "the canonical unit, not the unit it was written in"
    assert alert.sustained_seconds == pytest.approx(30.0)
    assert alert.observed_at == NOW
    assert alert.well_id == fabric.alpha.well_id
    assert alert.action_level == "L2"
    attributes = alert.attributes or {}
    assert attributes["raise_reason"].startswith("spp =")
    assert attributes["rule_snapshot"]["threshold"] == 4000.0
    assert attributes["rule_snapshot"]["unit"] == "psi", "the rule remembers what it was written in"
    assert attributes["evaluation"]["observed"]["declared_unit"] == "psi"
    assert attributes["evaluation"]["observed"]["declared_threshold"] == 4000.0
    assert attributes["evaluation"]["observed"]["threshold"] == pytest.approx(4000.0 * PSI)
    assert attributes["channel_key"] == "spp" and attributes["dimension"] == "pressure"


async def test_a_short_spike_raises_nothing(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha, sustain_seconds=60.0)

    report = await _evaluate(fabric, fabric.alpha)
    assert report.raised == 0 and report.held == 1
    assert await _alerts(fabric, fabric.alpha) == []


async def test_a_second_evaluation_of_the_same_condition_does_not_raise_a_second_alert(fabric: Fabric) -> None:
    """The condition is still true; it is the same alert, and the evaluation is recorded on it."""

    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW - dt.timedelta(seconds=10), 4200.0), (NOW, 4250.0)])
    await _rule(fabric, fabric.alpha)

    first = await _evaluate(fabric, fabric.alpha)
    second = await _evaluate(fabric, fabric.alpha, now=NOW + dt.timedelta(minutes=5))
    third = await _evaluate(fabric, fabric.alpha, now=NOW + dt.timedelta(minutes=10))

    assert first.raised == 1
    assert second.raised == 0 and second.already_open == 1
    assert third.already_open == 1
    alerts = await _alerts(fabric, fabric.alpha)
    assert len(alerts) == 1, "one open condition is one alert"
    attributes = alerts[0].attributes or {}
    assert attributes["evaluations"] == 3
    assert attributes["last_evaluated_at"] == (NOW + dt.timedelta(minutes=10)).isoformat()
    assert attributes["last_observed_value"] == pytest.approx(4250.0 * PSI)


async def test_a_rule_that_is_not_enabled_does_not_fire(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    rule = await _rule(fabric, fabric.alpha, enabled=False)
    assert rule.is_enabled is False

    report = await _evaluate(fabric, fabric.alpha)
    assert report.evaluated == 0 and report.raised == 0


async def test_a_rule_for_another_well_does_not_fire_here(fabric: Fabric) -> None:
    other_well = await fabric.add_well(fabric.alpha, "ALPHA-2")
    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha, well_id=other_well)

    report = await _evaluate(fabric, fabric.alpha)
    assert report.raised == 0 and report.evaluated == 0


async def test_a_rule_with_no_well_applies_to_every_well(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha, well_id=None)

    report = await _evaluate(fabric, fabric.alpha)
    assert report.raised == 1


async def test_the_cooldown_suppresses_a_re_raise_and_then_stops_suppressing(fabric: Fabric) -> None:
    """An alert that cleared one minute ago must not re-raise while the rule asks for a five-minute
    cooldown — but the suppression must end, or a real recurrence would be swallowed.

    The clock is the evaluation's, not the process's: the clear happens *through the rule* at a
    controlled instant, so ``cleared_at`` is the moment the arithmetic under test says it is.
    """

    series_id = await _well(fabric, fabric.alpha)
    await _append(
        fabric, fabric.alpha, series_id, [(NOW - dt.timedelta(seconds=20), 4200.0), (NOW, 4200.0)]
    )
    await _rule(fabric, fabric.alpha, cooldown_seconds=300.0)

    raised = await _evaluate(fabric, fabric.alpha)
    assert raised.raised == 1

    # The pressure bleeds off; the rule clears its own alert at NOW + 1 minute.
    await _append(fabric, fabric.alpha, series_id, [(NOW + dt.timedelta(minutes=1), 3000.0)])
    cleared = await _evaluate(fabric, fabric.alpha, now=NOW + dt.timedelta(minutes=1))
    assert cleared.cleared == 1
    assert (await _alerts(fabric, fabric.alpha))[0].status == "cleared"

    # It comes back two minutes after the clear — inside the five-minute cooldown.
    await _append(fabric, fabric.alpha, series_id, [(NOW + dt.timedelta(minutes=2), 4200.0)])
    suppressed = await _evaluate(fabric, fabric.alpha, now=NOW + dt.timedelta(minutes=2))
    assert suppressed.raised == 0 and suppressed.suppressed_cooldown == 1
    assert len(await _alerts(fabric, fabric.alpha)) == 1, "no second alert during the cooldown"
    assert suppressed.evaluations[-1].outcome == "hold", "a suppressed raise is reported as held"
    assert "cooldown" in suppressed.evaluations[-1].reason

    # Ten minutes after the clear the cooldown has elapsed, so the recurrence is a new alert.
    await _append(fabric, fabric.alpha, series_id, [(NOW + dt.timedelta(minutes=10), 4300.0)])
    after_cooldown = await _evaluate(fabric, fabric.alpha, now=NOW + dt.timedelta(minutes=10))
    assert after_cooldown.raised == 1, "the cooldown ended, so the recurrence is a new alert"
    assert len(await _alerts(fabric, fabric.alpha)) == 2


async def test_a_channel_with_no_points_is_counted_rather_than_guessed(fabric: Fabric) -> None:
    await _well(fabric, fabric.alpha)
    await _rule(fabric, fabric.alpha)

    report = await _evaluate(fabric, fabric.alpha)
    assert report.channels_without_points == 1
    assert report.raised == 0 and report.evaluated == 0


async def test_the_same_points_and_rules_give_the_same_decision_every_time(fabric: Fabric) -> None:
    """Determinism over stored rows: run the pass, cancel the alert, run it again at the same instant."""

    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW - dt.timedelta(seconds=20), 4200.0), (NOW, 4300.0)])
    await _rule(fabric, fabric.alpha)

    first = await _evaluate(fabric, fabric.alpha)
    async with fabric.session() as session:
        alert = (await _alerts(fabric, fabric.alpha))[0]
        await AlertService(session, fabric.org_id(fabric.alpha)).cancel(
            alert.id, expected_updated_at=alert.updated_at, reason="testing determinism"
        )
        await session.commit()
    second = await _evaluate(fabric, fabric.alpha)

    assert [item.outcome for item in first.evaluations] == [item.outcome for item in second.evaluations]
    assert first.evaluations[-1].observed_value == second.evaluations[-1].observed_value
    assert first.evaluations[-1].sustained_seconds == second.evaluations[-1].sustained_seconds


async def test_evaluating_a_well_with_no_channels_reports_nothing_rather_than_failing(fabric: Fabric) -> None:
    report = await _evaluate(fabric, fabric.alpha)
    assert report.evaluated == 0 and report.raised == 0
    assert report.to_dict()["alerts"] == []


# --------------------------------------------------------------------------- the life cycle


async def test_the_full_life_cycle_acknowledge_then_clear(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha)
    await _evaluate(fabric, fabric.alpha)
    alert = (await _alerts(fabric, fabric.alpha))[0]
    assert alert.acknowledged_at is None

    async with fabric.session() as session:
        service = AlertService(session, fabric.org_id(fabric.alpha), actor_id="usr_supervisor")
        acknowledged = await service.acknowledge(
            alert.id, expected_updated_at=alert.updated_at, reason="watching it"
        )
        assert acknowledged.status == "acknowledged"
        assert acknowledged.acknowledged_at is not None
        assert acknowledged.acknowledged_by == "usr_supervisor"
        await session.commit()

    row = (await _alerts(fabric, fabric.alpha))[0]
    async with fabric.session() as session:
        cleared = await AlertService(session, fabric.org_id(fabric.alpha), actor_id="usr_supervisor").clear(
            row.id, expected_updated_at=row.updated_at, reason="bled off to 3 700", observed_value=3700.0
        )
        assert cleared.status == "cleared"
        assert cleared.cleared_at is not None
        assert cleared.clear_observed_value == pytest.approx(3700.0)
        assert cleared.cancelled_reason is None
        await session.commit()


async def test_a_terminal_alert_cannot_be_acknowledged_again(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha)
    await _evaluate(fabric, fabric.alpha)
    alert = (await _alerts(fabric, fabric.alpha))[0]

    async with fabric.session() as session:
        service = AlertService(session, fabric.org_id(fabric.alpha))
        await service.cancel(alert.id, expected_updated_at=alert.updated_at, reason="false positive")
        await session.commit()

    row = (await _alerts(fabric, fabric.alpha))[0]
    async with fabric.session() as session:
        with pytest.raises(Conflict) as failure:
            await AlertService(session, fabric.org_id(fabric.alpha)).acknowledge(
                row.id, expected_updated_at=row.updated_at
            )
    details = failure.value.details
    assert details["status"] == "cancelled" and details["requested"] == "acknowledged"
    assert details["allowed"] == [], "a terminal state admits no further transition"


async def test_clearing_without_a_reason_is_refused(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha)
    await _evaluate(fabric, fabric.alpha)
    alert = (await _alerts(fabric, fabric.alpha))[0]

    async with fabric.session() as session:
        with pytest.raises(ValidationFailed) as failure:
            await AlertService(session, fabric.org_id(fabric.alpha)).clear(
                alert.id, expected_updated_at=alert.updated_at, reason="   "
            )
        await session.rollback()
    assert failure.value.details["field"] == "reason"
    assert (await _alerts(fabric, fabric.alpha))[0].status == "raised", "the refused call changed nothing"


async def test_acting_on_a_stale_version_is_a_conflict_not_a_silent_overwrite(fabric: Fabric) -> None:
    """Two people looked at the same alert; the second one is told, rather than overwriting the first."""

    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha)
    await _evaluate(fabric, fabric.alpha)
    alert = (await _alerts(fabric, fabric.alpha))[0]
    stale = alert.updated_at

    async with fabric.session() as session:
        await AlertService(session, fabric.org_id(fabric.alpha), actor_id="usr_a").acknowledge(
            alert.id, expected_updated_at=stale, reason="first"
        )
        await session.commit()

    async with fabric.session() as session:
        with pytest.raises(Conflict) as failure:
            await AlertService(session, fabric.org_id(fabric.alpha), actor_id="usr_b").acknowledge(
                alert.id, expected_updated_at=stale, reason="second"
            )
        await session.rollback()
    assert failure.value.details["resource"] == "alert"
    stored = (await _alerts(fabric, fabric.alpha))[0]
    assert (stored.attributes or {}).get("acknowledge_reason") == "first", "the first writer's record stands"


async def test_cancelling_records_why_it_was_judged_not_actable(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha)
    await _evaluate(fabric, fabric.alpha)
    alert = (await _alerts(fabric, fabric.alpha))[0]

    async with fabric.session() as session:
        cancelled = await AlertService(
            session, fabric.org_id(fabric.alpha), actor_id="usr_driller"
        ).cancel(alert.id, expected_updated_at=alert.updated_at, reason="sensor calibration in progress")
        await session.commit()
    assert cancelled.status == "cancelled"
    assert cancelled.cancelled_reason == "sensor calibration in progress"


async def test_an_alert_in_another_tenant_is_not_found_rather_than_forbidden(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha)
    await _evaluate(fabric, fabric.alpha)
    alert = (await _alerts(fabric, fabric.alpha))[0]

    async with fabric.session() as session:
        service = AlertService(session, fabric.org_id(fabric.bravo))
        with pytest.raises(NotFound):
            await service.get(alert.id)
        with pytest.raises(NotFound):
            await service.evidence(alert.id)
        with pytest.raises(NotFound):
            await service.acknowledge(alert.id, expected_updated_at=alert.updated_at, reason="nope")


async def test_every_life_cycle_step_is_audited_with_who_and_why(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha)
    await _evaluate(fabric, fabric.alpha)
    alert = (await _alerts(fabric, fabric.alpha))[0]

    async with fabric.session() as session:
        service = AlertService(session, fabric.org_id(fabric.alpha), actor_id="usr_supervisor")
        await service.acknowledge(alert.id, expected_updated_at=alert.updated_at, reason="on it")
        await session.commit()
    row = (await _alerts(fabric, fabric.alpha))[0]
    async with fabric.session() as session:
        await AlertService(session, fabric.org_id(fabric.alpha), actor_id="usr_supervisor").clear(
            row.id, expected_updated_at=row.updated_at, reason="pressure recovered"
        )
        await session.commit()

    async with fabric.session() as session:
        rows = list(
            (
                await session.execute(
                    select(AuditLog)
                    .where(AuditLog.org_id == fabric.org_id(fabric.alpha), AuditLog.resource_id == alert.id)
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
    actions = [row.action for row in rows]
    assert actions == ["alert.raise", "alert.acknowledge", "alert.clear"], actions
    acknowledge_entry = rows[1]
    assert acknowledge_entry.actor_id == "usr_supervisor"
    assert acknowledge_entry.details["reason"] == "on it"
    assert acknowledge_entry.before["status"] == "raised"
    assert acknowledge_entry.after["status"] == "acknowledged"
    assert acknowledge_entry.action_level == "L2", "the level comes from the action catalogue"
    assert acknowledge_entry.details["alert_action_level"] == "L2"
    assert rows[2].before["status"] == "acknowledged" and rows[2].after["status"] == "cleared"


async def test_the_life_cycle_emits_an_event_for_each_transition(fabric: Fabric) -> None:
    """The live feed's alert frames come from these rows: a transition with no event is a UI that never
    learns about it."""

    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha)
    await _evaluate(fabric, fabric.alpha)
    alert = (await _alerts(fabric, fabric.alpha))[0]

    async with fabric.session() as session:
        service = AlertService(session, fabric.org_id(fabric.alpha), actor_id="usr_supervisor")
        await service.acknowledge(alert.id, expected_updated_at=alert.updated_at, reason="on it")
        await session.commit()
    row = (await _alerts(fabric, fabric.alpha))[0]
    async with fabric.session() as session:
        await AlertService(session, fabric.org_id(fabric.alpha), actor_id="usr_supervisor").clear(
            row.id, expected_updated_at=row.updated_at, reason="recovered"
        )
        await session.commit()

    async with fabric.session() as session:
        rows = list(
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(OutboxEvent.org_id == fabric.org_id(fabric.alpha))
                    .order_by(OutboxEvent.sequence)
                )
            )
            .scalars()
            .all()
        )
    types = [row.event_type for row in rows]
    assert types == [
        "telemetry.channel_created",
        # The channel's first measurement is a change in kind (missing → live), so it is announced as a
        # state change as well as a measurement.
        "well_state.changed",
        "telemetry.received",
        "alert.raised",
        "alert.acknowledged",
        "alert.cleared",
    ], types
    # Sequences are a total order with no gaps: the feed's cursor can rely on it.
    assert [row.sequence for row in rows] == [1, 2, 3, 4, 5, 6]
    state_change = rows[1]
    assert state_change.payload["reason"] == "first_measurement"
    assert state_change.payload["previous_as_of"] is None


async def test_a_rule_change_is_audited_and_version_checked(fabric: Fabric) -> None:
    rule = await _rule(fabric, fabric.alpha)

    async with fabric.session() as session:
        service = AlertRuleService(session, fabric.org_id(fabric.alpha))
        with pytest.raises(ValidationFailed):
            await service.update(rule.id, expected_updated_at=rule.updated_at, reason="", threshold=4100.0)
        updated = await service.update(
            rule.id,
            expected_updated_at=rule.updated_at,
            reason="tightening after a false positive",
            threshold=4100.0,
        )
        await session.commit()
    assert updated.threshold == pytest.approx(4100.0)

    async with fabric.session() as session:
        with pytest.raises(Conflict):
            await AlertRuleService(session, fabric.org_id(fabric.alpha)).update(
                rule.id, expected_updated_at=rule.updated_at, reason="racing", threshold=4200.0
            )
        await session.rollback()

    async with fabric.session() as session:
        entries = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.org_id == fabric.org_id(fabric.alpha),
                        AuditLog.resource_id == rule.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert {entry.action for entry in entries} == {"alert.rule_manage"}
    assert any(entry.details.get("action") == "update" for entry in entries)


async def test_an_impossible_rule_is_refused_with_the_field_named(fabric: Fabric) -> None:
    with pytest.raises(ValidationFailed) as failure:
        await _rule(fabric, fabric.alpha, operator="regex", threshold=1.0)
    assert failure.value.details["field"] == "operator"
    assert failure.value.details["value"] == "regex"

    with pytest.raises(ValidationFailed) as failure:
        await _rule(fabric, fabric.alpha, clear_operator="lt", clear_threshold=4500.0)
    assert failure.value.details["field"] == "clear_threshold"

    with pytest.raises(ValidationFailed) as failure:
        await _rule(fabric, fabric.alpha, unit="furlongs")
    assert failure.value.details["field"] == "unit"


async def test_a_rule_written_in_another_dimensions_unit_refuses_to_evaluate(fabric: Fabric) -> None:
    """A pressure channel cannot be measured in rpm. The refusal names both, rather than comparing a
    pressure in pascals against a number the author meant as a rotary speed."""

    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha, unit="rpm")

    with pytest.raises(ValidationFailed) as failure:
        await _evaluate(fabric, fabric.alpha)
    details = failure.value.details
    assert details["field"] == "unit"
    assert details["rule_unit"] == "rpm" and details["channel_unit"] == "Pa"
    assert details["dimension"] == "pressure"


async def test_a_rule_without_a_declared_unit_compares_in_the_channels_canonical_unit(fabric: Fabric) -> None:
    """The behaviour every rule written before the unit column keeps: 4 000 means 4 000 pascals here."""

    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0 * PSI)])
    await _rule(fabric, fabric.alpha, unit=None)

    report = await _evaluate(fabric, fabric.alpha)
    assert report.raised == 1, "4 200 Pa is above a 4 000 Pa line"
    alert = (await _alerts(fabric, fabric.alpha))[0]
    assert alert.threshold_value == pytest.approx(4000.0)
    assert (alert.attributes or {})["rule_snapshot"]["unit"] is None


# --------------------------------------------------------------------------- evidence


async def test_evidence_answers_why_the_alert_was_raised(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(
        fabric,
        fabric.alpha,
        series_id,
        [(NOW - dt.timedelta(seconds=40), 3900.0), (NOW - dt.timedelta(seconds=20), 4200.0), (NOW, 4300.0)],
    )
    rule = await _rule(fabric, fabric.alpha, sustain_seconds=20.0)
    await _evaluate(fabric, fabric.alpha)
    alert = (await _alerts(fabric, fabric.alpha))[0]

    async with fabric.session() as session:
        evidence = await AlertService(session, fabric.org_id(fabric.alpha)).evidence(alert.id)

    assert evidence["alert"].id == alert.id
    assert evidence["rule"].id == rule.id
    snapshot = evidence["rule_snapshot"]
    assert snapshot["rule_key"] == "spp-high" and snapshot["threshold"] == 4000.0
    observed = evidence["observed"]
    assert observed["value"] == pytest.approx(4300.0 * PSI)
    assert observed["threshold"] == pytest.approx(4000.0 * PSI)
    assert observed["unit"] == "Pa"
    assert observed["declared_threshold"] == 4000.0
    assert observed["declared_unit"] == "psi"
    assert evidence["rule_snapshot"]["unit"] == "psi"
    assert observed["observed_at"] == NOW.isoformat()
    assert observed["sustained_seconds"] == pytest.approx(20.0)
    provenance = evidence["provenance"]
    assert provenance["series_id"] == series_id
    assert provenance["rule_id"] == rule.id and provenance["rule_ref"] == "spp-high"
    assert provenance["point_id"], "the alert names the point that breached"
    points = evidence["points"]
    assert [point["value"] for point in points] == pytest.approx(
        [4300.0 * PSI, 4200.0 * PSI, 3900.0 * PSI]
    ), "newest first, from storage, in the canonical unit"
    assert evidence["timeline"][0]["state"] == "raised"
    assert evidence["timeline"][0]["reason"].startswith("spp =")


async def test_the_evidence_timeline_grows_as_the_alert_is_worked(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha)
    await _evaluate(fabric, fabric.alpha)
    alert = (await _alerts(fabric, fabric.alpha))[0]

    async with fabric.session() as session:
        service = AlertService(session, fabric.org_id(fabric.alpha), actor_id="usr_supervisor")
        await service.acknowledge(alert.id, expected_updated_at=alert.updated_at, reason="looking")
        await session.commit()
    row = (await _alerts(fabric, fabric.alpha))[0]
    async with fabric.session() as session:
        await AlertService(session, fabric.org_id(fabric.alpha), actor_id="usr_supervisor").clear(
            row.id, expected_updated_at=row.updated_at, reason="recovered"
        )
        await session.commit()

    async with fabric.session() as session:
        evidence = await AlertService(session, fabric.org_id(fabric.alpha)).evidence(alert.id)

    timeline = evidence["timeline"]
    assert [entry["state"] for entry in timeline] == ["raised", "acknowledged", "cleared"]
    assert timeline[1]["by"] == "usr_supervisor" and timeline[1]["reason"] == "looking"
    assert timeline[2]["reason"] == "recovered"


async def test_evidence_for_a_missing_alert_is_not_found(fabric: Fabric) -> None:
    async with fabric.session() as session:
        with pytest.raises(NotFound):
            await AlertService(session, fabric.org_id(fabric.alpha)).evidence("alt_nonexistent")


async def test_listing_filters_by_status_severity_and_well(fabric: Fabric) -> None:
    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha, severity="critical")
    await _evaluate(fabric, fabric.alpha)
    alert = (await _alerts(fabric, fabric.alpha))[0]

    async with fabric.session() as session:
        service = AlertService(session, fabric.org_id(fabric.alpha))
        rows, total = await service.list(status="raised", severity="critical")
        assert total == 1 and rows[0].id == alert.id
        assert (await service.list(status="cleared"))[1] == 0
        assert (await service.list(well_id=fabric.alpha.well_id))[1] == 1
        assert (await service.list(well_id="well_nowhere"))[1] == 0
        with pytest.raises(ValidationFailed):
            await service.list(status="exploded")
        open_rows = await service.open_alerts(well_id=fabric.alpha.well_id)
        assert [row.id for row in open_rows] == [alert.id]

    async with fabric.session() as session:
        await AlertService(session, fabric.org_id(fabric.alpha)).cancel(
            alert.id, expected_updated_at=alert.updated_at, reason="closed"
        )
        await session.commit()
    async with fabric.session() as session:
        assert await AlertService(session, fabric.org_id(fabric.alpha)).open_alerts() == []


async def test_the_alert_count_matches_the_rows_written(fabric: Fabric) -> None:
    """Reconciliation: the report's arithmetic and the table agree."""

    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4200.0)])
    await _rule(fabric, fabric.alpha)
    report = await _evaluate(fabric, fabric.alpha)

    async with fabric.session() as session:
        stored = (
            await session.execute(
                select(func.count()).select_from(Alert).where(Alert.org_id == fabric.org_id(fabric.alpha))
            )
        ).scalar_one()
    assert stored == report.raised == len(report.alerts) == 1


# --------------------------------------------------------------------------- CP10 automatic evaluation, 8-case semantics, fault isolation & evidence T1 integrity


async def test_auto_evaluation_on_append_raises_holds_and_clears_without_manual_endpoint(
    fabric: Fabric,
) -> None:
    """Ingesting telemetry through TelemetryService.append_points automatically evaluates rules,
    raises an alert on sustained breach, holds on duplicate/continued breach, and clears on recovery."""

    series_id = await _well(fabric, fabric.alpha)
    await _rule(
        fabric,
        fabric.alpha,
        sustain_seconds=10.0,
        clear_operator="lte",
        clear_threshold=3800.0,
        clear_sustain_seconds=10.0,
    )

    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        breach = await service.append_points(
            series_id,
            [
                PointIn(ts=NOW - dt.timedelta(seconds=15), value=4150.0, unit="psi", quality="good", source_point_id="b1"),
                PointIn(ts=NOW, value=4250.0, unit="psi", quality="good", source_point_id="b2"),
            ],
            received_at=NOW,
        )
        await session.commit()

    assert breach.alerts_raised == 1
    assert breach.alerts_cleared == 0
    assert breach.evaluation["status"] == "evaluated"
    assert breach.evaluation["decision"] == "new_measurement"
    assert breach.evaluation["mode"] == "auto_ingest"
    assert breach.evaluation["evaluation_id"].startswith("evl_")
    assert breach.evaluation["duration_ms"] >= 0.0

    # Exact duplicate replay skips evaluation and never raises a second alert.
    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        dup = await service.append_points(
            series_id,
            [
                PointIn(ts=NOW - dt.timedelta(seconds=15), value=4150.0, unit="psi", quality="good", source_point_id="b1"),
                PointIn(ts=NOW, value=4250.0, unit="psi", quality="good", source_point_id="b2"),
            ],
            received_at=NOW + dt.timedelta(seconds=1),
        )
        await session.commit()

    assert dup.duplicates == 2
    assert dup.alerts_raised == 0
    assert dup.evaluation["status"] == "skipped"
    assert dup.evaluation["decision"] == "skipped_duplicate_replay"

    # Conflicting replay under reject policy skips evaluation and keeps open alert intact.
    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        conflict = await service.append_points(
            series_id,
            [
                PointIn(ts=NOW, value=3100.0, unit="psi", quality="good", source_point_id="b2"),
            ],
            on_conflict="reject",
            received_at=NOW + dt.timedelta(seconds=2),
        )
        await session.commit()

    assert conflict.rejected == 1
    assert conflict.evaluation["status"] == "skipped"
    assert conflict.evaluation["decision"] == "skipped_conflicting_replay"

    # Untrustworthy quality ('bad' / 'missing') does not silently clear the open alert.
    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        bad_batch = await service.append_points(
            series_id,
            [
                PointIn(ts=NOW + dt.timedelta(seconds=5), value=3000.0, unit="psi", quality="bad", source_point_id="bad1"),
                PointIn(ts=NOW + dt.timedelta(seconds=16), value=3000.0, unit="psi", quality="bad", source_point_id="bad2"),
            ],
            received_at=NOW + dt.timedelta(seconds=16),
        )
        await session.commit()

    assert bad_batch.accepted == 2
    assert bad_batch.alerts_cleared == 0
    assert bad_batch.evaluation["status"] == "skipped"
    assert bad_batch.evaluation["decision"] == "skipped_untrustworthy_quality"

    # Historical out-of-order backfill far behind the horizon does not overwrite current state.
    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        historical = await service.append_points(
            series_id,
            [
                PointIn(
                    ts=NOW - dt.timedelta(minutes=30),
                    value=3000.0,
                    unit="psi",
                    quality="good",
                    source_point_id="hist1",
                ),
                PointIn(
                    ts=NOW - dt.timedelta(minutes=29),
                    value=3000.0,
                    unit="psi",
                    quality="good",
                    source_point_id="hist2",
                ),
            ],
            received_at=NOW + dt.timedelta(seconds=20),
        )
        await session.commit()

    assert historical.accepted == 2
    assert historical.out_of_order == 2
    assert historical.alerts_cleared == 0
    assert historical.evaluation["status"] == "skipped"
    assert historical.evaluation["decision"] == "skipped_historical_out_of_order"

    # Valid recovery measurements sustained for >= 10s automatically clear the alert.
    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        recovery = await service.append_points(
            series_id,
            [
                PointIn(ts=NOW + dt.timedelta(seconds=30), value=3600.0, unit="psi", quality="good", source_point_id="r1"),
                PointIn(ts=NOW + dt.timedelta(seconds=45), value=3550.0, unit="psi", quality="good", source_point_id="r2"),
            ],
            received_at=NOW + dt.timedelta(seconds=45),
        )
        await session.commit()

    assert recovery.alerts_cleared == 1
    assert recovery.evaluation["status"] == "evaluated"
    assert recovery.evaluation["decision"] == "new_measurement"
    alerts = await _alerts(fabric, fabric.alpha)
    assert len(alerts) == 1
    assert alerts[0].status == "cleared"


async def test_authorized_revision_triggers_automatic_re_evaluation(fabric: Fabric) -> None:
    """When on_conflict='revise' updates a measurement in place, automatic evaluation runs with
    decision='revised_measurement'."""

    series_id = await _well(fabric, fabric.alpha)
    await _rule(fabric, fabric.alpha, sustain_seconds=0.0)

    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        first = await service.append_points(
            series_id,
            [PointIn(ts=NOW, value=3500.0, unit="psi", quality="good", source_point_id="rev-pt")],
            received_at=NOW,
        )
        await session.commit()
    assert first.alerts_raised == 0

    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        revised = await service.append_points(
            series_id,
            [PointIn(ts=NOW, value=4300.0, unit="psi", quality="good", source_point_id="rev-pt")],
            on_conflict="revise",
            received_at=NOW + dt.timedelta(seconds=5),
        )
        await session.commit()

    assert revised.revised == 1
    assert revised.alerts_raised == 1
    assert revised.evaluation["decision"] == "revised_measurement"


async def test_misconfigured_rule_does_not_abort_telemetry_ingest_or_healthy_rules(
    fabric: Fabric,
) -> None:
    """During automatic evaluation (and recovery evaluation), a rule with an incompatible unit is
    isolated in a savepoint and recorded in failures, while the telemetry batch and healthy rules
    succeed."""

    series_id = await _well(fabric, fabric.alpha)
    await _rule(fabric, fabric.alpha, rule_key="spp-broken", unit="rpm", threshold=120.0)
    await _rule(fabric, fabric.alpha, rule_key="spp-valid", unit="psi", threshold=4000.0)

    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        report = await service.append_points(
            series_id,
            [PointIn(ts=NOW, value=4250.0, unit="psi", quality="good", source_point_id="iso-1")],
            received_at=NOW,
        )
        await session.commit()

    assert report.accepted == 1
    assert report.alerts_raised == 1
    assert report.evaluation["status"] == "degraded"
    assert report.evaluation["failed"] == 1
    assert report.evaluation["reconciled"] is True
    assert len(report.evaluation["failures"]) == 1
    assert report.evaluation["failures"][0]["rule_key"] == "spp-broken"

    # Recovery mode on AlertService.evaluate_well also isolates the broken rule.
    async with fabric.session() as session:
        recovery = await AlertService(session, fabric.org_id(fabric.alpha)).evaluate_well(
            fabric.alpha.well_id,
            now=NOW + dt.timedelta(seconds=5),
            mode="recovery",
        )
        await session.commit()

    assert recovery.mode == "recovery"
    assert recovery.failed == 1
    assert recovery.already_open == 1
    assert recovery.reconciled is True


async def test_concurrent_evaluators_on_same_channel_raise_only_one_alert(
    fabric: Fabric,
) -> None:
    """Two evaluators running concurrently for the same well and channel produce exactly one open alert."""

    import asyncio

    series_id = await _well(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, series_id, [(NOW, 4250.0)])
    await _rule(fabric, fabric.alpha, sustain_seconds=0.0)

    async def run_one():
        async with fabric.session() as session:
            rep = await AlertService(session, fabric.org_id(fabric.alpha)).evaluate_well(
                fabric.alpha.well_id,
                now=NOW,
            )
            await session.commit()
            return rep

    first, second = await asyncio.gather(run_one(), run_one())
    assert first.raised + second.raised == 1
    assert first.already_open + second.already_open == 1
    alerts = await _alerts(fabric, fabric.alpha)
    assert len(alerts) == 1


async def test_historical_evidence_at_t1_is_not_overwritten_by_later_t2_telemetry(
    fabric: Fabric,
) -> None:
    """When an alert is raised at T1 and 30 more points arrive at T2 > T1, evidence(alert_id) still
    returns the historical T1 points rather than replacing them with T2 points."""

    series_id = await _well(fabric, fabric.alpha)
    await _rule(fabric, fabric.alpha, sustain_seconds=10.0)

    t1 = NOW
    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        rep_t1 = await service.append_points(
            series_id,
            [
                PointIn(ts=t1 - dt.timedelta(seconds=10), value=4150.0, unit="psi", quality="good", source_point_id="t1-a"),
                PointIn(ts=t1, value=4250.0, unit="psi", quality="good", source_point_id="t1-b"),
            ],
            received_at=t1,
        )
        await session.commit()
    assert rep_t1.alerts_raised == 1
    alert = (await _alerts(fabric, fabric.alpha))[0]

    # Now append 30 later points at T2 > T1.
    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        await service.append_points(
            series_id,
            [
                PointIn(
                    ts=t1 + dt.timedelta(minutes=5, seconds=index),
                    value=4300.0 + index,
                    unit="psi",
                    quality="good",
                    source_point_id=f"t2-{index}",
                )
                for index in range(30)
            ],
            received_at=t1 + dt.timedelta(minutes=6),
        )
        await session.commit()

    async with fabric.session() as session:
        evidence = await AlertService(session, fabric.org_id(fabric.alpha)).evidence(alert.id, points=10)

    point_timestamps = [ dt.datetime.fromisoformat(pt["ts"]) for pt in evidence["points"] ]
    assert len(point_timestamps) == 2
    assert all(ts <= t1 for ts in point_timestamps), (
        f"Evidence points at T1 were polluted by T2 points: {point_timestamps}"
    )
    assert evidence["observed"]["observed_at"] == t1.isoformat()

