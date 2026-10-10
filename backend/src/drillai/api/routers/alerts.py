"""Alerts over HTTP: the list an operator works from, the evidence behind each one, and the life cycle.

Three things this router is deliberate about.

**There is no delete route.** An alert that turned out to be wrong is *cancelled* with a reason, which
keeps the record that it was raised and that somebody judged it; deleting it would remove the evidence
that the rule fired, and the rule's tuning depends on knowing how often it fires.

**Every mutation is idempotency-keyed, version-checked and reasoned.** ``Idempotency-Key`` makes a retry
after a timeout safe, ``expected_updated_at`` makes two people acting on one alert a conflict rather than
a silent overwrite, and a reason is required because "who closed this, and on what basis" is the first
question a review asks.

**The evaluation endpoint exists so that rules can be run without waiting for a stream.** It is
deterministic: the same stored points and the same rule set produce the same decisions, which is what
makes an alert explainable after the fact.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, get_db, require
from drillai.api.serializers import alert_out, alert_rule_out
from drillai.core.idempotency import complete, replay_or_reserve
from drillai.security.actions import authorize
from drillai.telemetry.alerts import AlertRuleService, AlertService
from drillai.telemetry.vocabulary import ALERT_SEVERITIES, ALERT_STATUSES, RULE_OPERATORS

router = APIRouter(tags=["alerts"])

_TZ_HELP = "timestamps must include a timezone offset, e.g. 2026-01-15T08:00:00+00:00"


def _alerts(session: AsyncSession, auth: AuthContext) -> AlertService:
    return AlertService(session, auth.org_id or "", principal=auth.principal)


def _rules(session: AsyncSession, auth: AuthContext) -> AlertRuleService:
    return AlertRuleService(session, auth.org_id or "", principal=auth.principal)


class AlertRuleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_key: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    channel_key: str = Field(min_length=1, max_length=160)
    operator: str = Field(pattern="^(gt|gte|lt|lte|eq)$")
    threshold: float
    severity: str = "medium"
    unit: str | None = Field(default=None, max_length=24, description="unit the thresholds are written in")
    well_id: str | None = None
    wellbore_id: str | None = None
    operation_id: str | None = None
    clear_operator: str | None = Field(default=None, pattern="^(gt|gte|lt|lte|eq)$")
    clear_threshold: float | None = None
    sustain_seconds: float = Field(default=0.0, ge=0, le=86_400)
    clear_sustain_seconds: float = Field(default=0.0, ge=0, le=86_400)
    cooldown_seconds: float = Field(default=0.0, ge=0, le=604_800)
    description: str | None = None
    enabled: bool = True


class AlertRuleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_updated_at: dt.datetime
    reason: str = Field(min_length=1, max_length=1000)
    name: str | None = None
    operator: str | None = Field(default=None, pattern="^(gt|gte|lt|lte|eq)$")
    threshold: float | None = None
    severity: str | None = None
    unit: str | None = Field(default=None, max_length=24)
    clear_operator: str | None = Field(default=None, pattern="^(gt|gte|lt|lte|eq)$")
    clear_threshold: float | None = None
    sustain_seconds: float | None = Field(default=None, ge=0, le=86_400)
    clear_sustain_seconds: float | None = Field(default=None, ge=0, le=86_400)
    cooldown_seconds: float | None = Field(default=None, ge=0, le=604_800)
    description: str | None = None
    enabled: bool | None = None


class AlertClose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_updated_at: dt.datetime
    reason: str = Field(min_length=1, max_length=2000)
    observed_value: float | None = None


class AlertAcknowledge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_updated_at: dt.datetime
    reason: str | None = Field(default=None, max_length=1000)


# --------------------------------------------------------------------------- alert rules


@router.get("/alert-rules", summary="List alert rules")
async def list_rules(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("alert.read"))],
    well_id: OptionalFilter = None,
    channel_key: OptionalFilter = None,
    enabled: bool | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    rows, total = await _rules(session, auth).list(
        well_id=well_id, channel_key=channel_key, enabled=enabled, limit=limit, offset=offset
    )
    return {
        "items": [alert_rule_out(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "operators": list(RULE_OPERATORS),
        "severities": list(ALERT_SEVERITIES),
    }


@router.post("/alert-rules", summary="Define an alert rule", status_code=201)
async def create_rule(
    payload: AlertRuleCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("alert.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    """A rule is data: a comparison, a threshold, a duration and a severity. Nothing executable."""

    authorize(auth.principal, "alert.rule_manage")
    org_id = auth.org_id or ""
    scope = "alert.rule_manage"
    body = payload.model_dump(mode="json")
    if (replayed := await replay_or_reserve(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    row = await _rules(session, auth).create(**payload.model_dump())
    response = alert_rule_out(row)
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    await session.commit()
    return response


@router.patch("/alert-rules/{rule_id}", summary="Change an alert rule")
async def update_rule(
    rule_id: str,
    payload: AlertRuleUpdate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("alert.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "alert.rule_manage")
    org_id = auth.org_id or ""
    scope = "alert.rule_manage"
    body = payload.model_dump(mode="json")
    if (replayed := await replay_or_reserve(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    changes = {key: value for key, value in payload.model_dump().items() if key not in {"expected_updated_at", "reason"}}
    row = await _rules(session, auth).update(
        rule_id,
        expected_updated_at=payload.expected_updated_at,
        reason=payload.reason,
        **changes,
    )
    response = alert_rule_out(row)
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    await session.commit()
    return response


@router.get("/alert-rules/{rule_id}", summary="One alert rule")
async def get_rule(
    rule_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("alert.read"))],
) -> dict[str, Any]:
    return alert_rule_out(await _rules(session, auth).get(rule_id))


# --------------------------------------------------------------------------- alerts


@router.get("/alerts", summary="List alerts")
async def list_alerts(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("alert.read"))],
    well_id: OptionalFilter = None,
    wellbore_id: OptionalFilter = None,
    status: OptionalFilter = None,
    severity: OptionalFilter = None,
    rule_ref: OptionalFilter = None,
    series_id: OptionalFilter = None,
    since: dt.datetime | None = Query(default=None, description=_TZ_HELP),
    until: dt.datetime | None = Query(default=None, description=_TZ_HELP),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Every alert carries its status, severity, what it was raised from and when it was observed."""

    rows, total = await _alerts(session, auth).list(
        well_id=well_id,
        wellbore_id=wellbore_id,
        status=status,
        severity=severity,
        rule_ref=rule_ref,
        series_id=series_id,
        since=since,
        until=until,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [alert_out(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "statuses": list(ALERT_STATUSES),
        "severities": list(ALERT_SEVERITIES),
    }


@router.get("/alerts/{alert_id}", summary="One alert")
async def get_alert(
    alert_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("alert.read"))],
) -> dict[str, Any]:
    return alert_out(await _alerts(session, auth).get(alert_id))


@router.get("/alerts/{alert_id}/evidence", summary="Why this alert was raised")
async def alert_evidence(
    alert_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("alert.read"))],
    points: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    """The rule (and the rule as it was when it fired), the observed values, the stored points behind
    them, and the life cycle so far — assembled from records, not recomputed from a rule."""

    service = _alerts(session, auth)
    evidence = await service.evidence(alert_id, points=points)
    return {
        "alert": alert_out(evidence["alert"]),
        "rule": alert_rule_out(evidence["rule"]) if evidence["rule"] is not None else None,
        "rule_snapshot": evidence["rule_snapshot"],
        "observed": evidence["observed"],
        "provenance": evidence["provenance"],
        "points": evidence["points"],
        "timeline": evidence["timeline"],
    }


@router.post("/alerts/{alert_id}/acknowledge", summary="Acknowledge an alert")
async def acknowledge_alert(
    alert_id: str,
    payload: AlertAcknowledge,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("alert.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "alert.acknowledge")
    org_id = auth.org_id or ""
    scope = "alert.acknowledge"
    body = payload.model_dump(mode="json")
    if (replayed := await replay_or_reserve(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    alert = await _alerts(session, auth).acknowledge(
        alert_id, expected_updated_at=payload.expected_updated_at, reason=payload.reason
    )
    response = alert_out(alert)
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    await session.commit()
    return response


@router.post("/alerts/{alert_id}/clear", summary="Clear an alert whose condition has ended")
async def clear_alert(
    alert_id: str,
    payload: AlertClose,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("alert.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "alert.clear")
    org_id = auth.org_id or ""
    scope = "alert.clear"
    body = payload.model_dump(mode="json")
    if (replayed := await replay_or_reserve(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    alert = await _alerts(session, auth).clear(
        alert_id,
        expected_updated_at=payload.expected_updated_at,
        reason=payload.reason,
        observed_value=payload.observed_value,
    )
    response = alert_out(alert)
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    await session.commit()
    return response


@router.post("/alerts/{alert_id}/cancel", summary="Cancel an alert a person judged not actionable")
async def cancel_alert(
    alert_id: str,
    payload: AlertClose,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("alert.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "alert.cancel")
    org_id = auth.org_id or ""
    scope = "alert.cancel"
    body = payload.model_dump(mode="json")
    if (replayed := await replay_or_reserve(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    alert = await _alerts(session, auth).cancel(
        alert_id, expected_updated_at=payload.expected_updated_at, reason=payload.reason
    )
    response = alert_out(alert)
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    await session.commit()
    return response


@router.post("/wells/{well_id}/alerts/evaluate", summary="Run the well's rules against its stored points")
async def evaluate_well(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("alert.read"))],
    channel_id: list[str] | None = Query(default=None, description="repeatable; narrow the pass"),
) -> dict[str, Any]:
    """Deterministic: the same stored points and rules produce the same decisions, every time.

    This is the endpoint that makes an alert explainable *before* a stream exists: a rule can be written,
    run against the history that is already stored, and reviewed — rather than waiting for a rig to
    repeat the condition.
    """

    authorize(auth.principal, "alert.rule_manage")
    report = await _alerts(session, auth).evaluate_well(well_id, channel_ids=channel_id)
    payload = report.to_dict()
    await session.commit()
    return payload
