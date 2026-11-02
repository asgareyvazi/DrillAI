"""Daily drilling report API.

Every route is tenant-authorized at the HTTP boundary and delegates its domain rules to
``DailyReportService``, which repeats authorization and validates scope, provenance, edit versions and
lifecycle state. Mutations support the platform idempotency-key contract and commit together with audit,
revision history and the outbox event.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, get_db, require
from drillai.core.idempotency import complete, replay_or_reserve
from drillai.db.models import (
    DAILY_REPORT_PARAMETER_SPECS,
    REPORT_ACTIVITY_KINDS,
    REPORT_DEPTH_REFERENCES,
    REPORT_RECONCILIATION_STATES,
    REPORT_REVISION_STATUSES,
    REPORT_SHIFT_CODES,
    REPORT_SOURCE_KINDS,
    REPORT_TIME_BASES,
)
from drillai.db.models.operations import CAUSE_BASES, NPT_CATEGORIES, OPERATION_KINDS
from drillai.drilling.daily_reports import DailyReportService
from drillai.security.actions import authorize
from drillai.units.registry import get_unit, known_units

router = APIRouter(tags=["daily-reports"])

_TIMESTAMP_HELP = "timestamps must include a timezone offset, e.g. 2026-03-15T08:00:00-06:00"


def _require_aware_timestamp(value: dt.datetime | None, field: str) -> dt.datetime | None:
    if value is not None and (value.tzinfo is None or value.utcoffset() is None):
        raise ValueError(f"{field} {_TIMESTAMP_HELP}")
    return value


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DailyReportActivityInput(_StrictModel):
    activity_kind: str = Field(min_length=1, max_length=24)
    operation_kind: str | None = Field(default=None, max_length=40)
    npt_category: str | None = Field(default=None, max_length=60)
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10000)
    start_at: dt.datetime | None = None
    start_time_basis: str = "unknown"
    end_at: dt.datetime | None = None
    end_time_basis: str = "unknown"
    duration_value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    duration_unit: str | None = Field(default=None, max_length=24)
    duration_basis: str = "unknown"
    depth_from_value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    depth_from_unit: str | None = Field(default=None, max_length=24)
    depth_to_value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    depth_to_unit: str | None = Field(default=None, max_length=24)
    depth_reference: str = "unknown"
    depth_basis: str = "unknown"
    cause_basis: str = "unknown"
    source_document_id: str | None = Field(default=None, max_length=64)
    source_record_id: str | None = Field(default=None, max_length=64)
    source_region_id: str | None = Field(default=None, max_length=64)
    source_page_number: int | None = Field(default=None, ge=1)
    operation_id: str | None = Field(default=None, max_length=64)
    event_id: str | None = Field(default=None, max_length=64)

    @field_validator("start_at", "end_at")
    @classmethod
    def activity_timestamps_are_aware(cls, value: dt.datetime | None, info) -> dt.datetime | None:
        return _require_aware_timestamp(value, info.field_name)


class DailyReportMeasurementInput(_StrictModel):
    parameter_key: str = Field(min_length=1, max_length=48)
    value: float = Field(allow_inf_nan=False)
    unit: str = Field(min_length=1, max_length=24)
    value_basis: str = "reported"
    observed_at: dt.datetime | None = None
    time_basis: str = "unknown"
    depth_value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    depth_unit: str | None = Field(default=None, max_length=24)
    depth_reference: str | None = Field(default=None, max_length=16)
    depth_basis: str = "unknown"
    source_document_id: str | None = Field(default=None, max_length=64)
    source_record_id: str | None = Field(default=None, max_length=64)
    source_region_id: str | None = Field(default=None, max_length=64)
    source_page_number: int | None = Field(default=None, ge=1)

    @field_validator("observed_at")
    @classmethod
    def measurement_timestamp_is_aware(cls, value: dt.datetime | None, info) -> dt.datetime | None:
        return _require_aware_timestamp(value, info.field_name)


class DailyReportContentInput(_StrictModel):
    period_start_at: dt.datetime | None = None
    period_end_at: dt.datetime | None = None
    summary: str | None = Field(default=None, max_length=10000)
    operations_summary: str | None = Field(default=None, max_length=10000)
    safety_notes: str | None = Field(default=None, max_length=10000)
    next_period_plan: str | None = Field(default=None, max_length=10000)
    environmental_notes: str | None = Field(default=None, max_length=10000)
    source_document_id: str | None = Field(default=None, max_length=64)
    source_record_id: str | None = Field(default=None, max_length=64)
    source_region_id: str | None = Field(default=None, max_length=64)
    source_page_number: int | None = Field(default=None, ge=1)
    activities: list[DailyReportActivityInput] = Field(default_factory=list, max_length=500)
    measurements: list[DailyReportMeasurementInput] = Field(default_factory=list, max_length=500)

    @field_validator("period_start_at", "period_end_at")
    @classmethod
    def period_timestamps_are_aware(cls, value: dt.datetime | None, info) -> dt.datetime | None:
        return _require_aware_timestamp(value, info.field_name)


class DailyReportCreate(_StrictModel):
    wellbore_id: str = Field(min_length=1, max_length=64)
    report_date: dt.date
    report_timezone: str = Field(min_length=1, max_length=64)
    shift_code: str = Field(min_length=1, max_length=24)
    shift_label: str | None = Field(default=None, max_length=80)
    report_number: str | None = Field(default=None, max_length=120)
    content: DailyReportContentInput = Field(default_factory=DailyReportContentInput)


class DailyReportFieldSource(_StrictModel):
    field_key: str = Field(min_length=1, max_length=48)
    source_record_id: str = Field(min_length=1, max_length=64)
    source_path: str = Field(
        min_length=8,
        max_length=256,
        pattern=r"^payload(?:\.[A-Za-z0-9_-]+)*$",
        description="A field path within the immutable extracted-record payload, e.g. payload.summary.",
    )


class DailyReportImport(DailyReportCreate):
    source_document_id: str = Field(min_length=1, max_length=64)
    source_record_id: str | None = Field(default=None, max_length=64)
    source_region_id: str | None = Field(default=None, max_length=64)
    source_page_number: int | None = Field(default=None, ge=1)
    field_sources: list[DailyReportFieldSource] = Field(default_factory=list, max_length=32)


class DailyReportReconciliationChange(_StrictModel):
    target_key: str = Field(min_length=1, max_length=180)
    decision: Literal["accepted", "corrected", "rejected", "unresolved"]


class DailyReportReconciliation(_StrictModel):
    expected_edit_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    decisions: list[DailyReportReconciliationChange] = Field(min_length=1, max_length=500)


class DailyReportEdit(_StrictModel):
    expected_edit_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    content: DailyReportContentInput


class DailyReportRevisionReason(_StrictModel):
    reason: str = Field(min_length=1, max_length=2000)


class DailyReportDecision(DailyReportRevisionReason):
    expected_edit_version: int = Field(ge=1)


def _service(session: AsyncSession, auth: AuthContext, request: Request) -> DailyReportService:
    return DailyReportService(
        session,
        auth.org_id or "",
        principal=auth.principal,
        request_id=getattr(request.state, "request_id", None),
    )


async def _replay(
    session: AsyncSession,
    *,
    org_id: str,
    key: str | None,
    scope: str,
    body: dict[str, Any],
) -> dict[str, Any] | None:
    return await replay_or_reserve(session, org_id=org_id, key=key, scope=scope, payload=body)


async def _remember(
    session: AsyncSession,
    *,
    org_id: str,
    key: str | None,
    scope: str,
    response: dict[str, Any],
) -> dict[str, Any]:
    await complete(session, org_id=org_id, key=key, scope=scope, response=response)
    return response


@router.get("/daily-report-parameters", summary="Daily report parameter and provenance contract")
async def daily_report_parameters(
    _: Annotated[AuthContext, Depends(require("daily_report.read"))],
) -> dict[str, Any]:
    units_by_dimension: dict[str, list[str]] = {}
    for unit in known_units():
        dimension = str(getattr(unit.dimension, "value", unit.dimension))
        units_by_dimension.setdefault(dimension, []).append(unit.symbol)
    parameters = []
    for key, spec in sorted(DAILY_REPORT_PARAMETER_SPECS.items()):
        canonical_unit = get_unit(spec[1])
        physical_dimension = str(getattr(canonical_unit.dimension, "value", canonical_unit.dimension))
        parameters.append(
            {
                "key": key,
                "dimension": spec[0],
                "canonical_unit": spec[1],
                "accepted_units": units_by_dimension.get(physical_dimension, []),
                "required_depth_reference": spec[2],
            }
        )
    return {
        "parameters": parameters,
        "activity_kinds": list(REPORT_ACTIVITY_KINDS),
        "operation_kinds": list(OPERATION_KINDS),
        "npt_categories": list(NPT_CATEGORIES),
        "cause_bases": list(CAUSE_BASES),
        "shift_codes": list(REPORT_SHIFT_CODES),
        "revision_statuses": list(REPORT_REVISION_STATUSES),
        "source_kinds": list(REPORT_SOURCE_KINDS),
        "time_bases": list(REPORT_TIME_BASES),
        "depth_references": list(REPORT_DEPTH_REFERENCES),
        "reconciliation_states": list(REPORT_RECONCILIATION_STATES),
    }


@router.get("/wells/{well_id}/daily-reports", summary="List daily reports for a well")
async def list_daily_reports(
    well_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.read"))],
    report_date_from: dt.date | None = Query(default=None),
    report_date_to: dt.date | None = Query(default=None),
    shift_code: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return await _service(session, auth, request).list_reports(
        well_id,
        report_date_from=report_date_from,
        report_date_to=report_date_to,
        shift_code=shift_code,
        status=status,
        limit=limit,
        offset=offset,
    )


@router.post("/wells/{well_id}/daily-reports", status_code=201, summary="Create a manual report draft")
async def create_daily_report(
    well_id: str,
    payload: DailyReportCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.write"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "daily_report.create", context={"well_id": well_id})
    org_id = auth.org_id or ""
    scope = "daily_report.create"
    body = {"well_id": well_id, **payload.model_dump(mode="json")}
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, body=body)) is not None:
        return replayed
    response = await _service(session, auth, request).create_manual(
        well_id,
        payload.model_dump(mode="python"),
    )
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.post(
    "/wells/{well_id}/daily-reports/imports",
    status_code=201,
    summary="Create an unapproved draft from a source document",
)
async def import_daily_report(
    well_id: str,
    payload: DailyReportImport,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.write"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "daily_report.import", context={"well_id": well_id})
    org_id = auth.org_id or ""
    scope = f"daily_report.import:{well_id}"
    body = {"well_id": well_id, **payload.model_dump(mode="json")}
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, body=body)) is not None:
        return replayed
    response = await _service(session, auth, request).create_import(
        well_id,
        payload.model_dump(mode="python"),
    )
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.get("/wells/{well_id}/daily-reports/{report_id}", summary="Read the current report revision")
async def get_daily_report(
    well_id: str,
    report_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.read"))],
) -> dict[str, Any]:
    return await _service(session, auth, request).get_report(well_id, report_id)


@router.get("/wells/{well_id}/daily-reports/{report_id}/history", summary="Read numbered report history")
async def daily_report_history(
    well_id: str,
    report_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.read"))],
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    return await _service(session, auth, request).history(well_id, report_id, limit=limit, offset=offset)


@router.get(
    "/wells/{well_id}/daily-reports/{report_id}/revisions/{revision_number}",
    summary="Read one immutable report revision",
)
async def get_daily_report_revision(
    well_id: str,
    report_id: str,
    revision_number: int,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.read"))],
) -> dict[str, Any]:
    return await _service(session, auth, request).get_revision(well_id, report_id, revision_number)


@router.patch(
    "/wells/{well_id}/daily-reports/{report_id}/revisions/{revision_number}",
    summary="Edit a draft revision using an expected edit version",
)
async def update_daily_report_draft(
    well_id: str,
    report_id: str,
    revision_number: int,
    payload: DailyReportEdit,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.write"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "daily_report.edit", context={"well_id": well_id, "daily_report_id": report_id})
    org_id = auth.org_id or ""
    scope = f"daily_report.edit:{report_id}:{revision_number}"
    body = payload.model_dump(mode="json")
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, body=body)) is not None:
        return replayed
    response = await _service(session, auth, request).update_draft(
        well_id,
        report_id,
        revision_number,
        expected_edit_version=payload.expected_edit_version,
        reason=payload.reason,
        content=payload.content.model_dump(mode="python"),
    )
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.post(
    "/wells/{well_id}/daily-reports/{report_id}/revisions/{revision_number}/reconcile",
    summary="Reconcile source-linked fields and rows in an imported draft",
)
async def reconcile_daily_report(
    well_id: str,
    report_id: str,
    revision_number: int,
    payload: DailyReportReconciliation,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.write"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(
        auth.principal,
        "daily_report.reconcile",
        context={"well_id": well_id, "daily_report_id": report_id},
    )
    org_id = auth.org_id or ""
    scope = f"daily_report.reconcile:{report_id}:{revision_number}"
    body = payload.model_dump(mode="json")
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, body=body)) is not None:
        return replayed
    response = await _service(session, auth, request).reconcile(
        well_id,
        report_id,
        revision_number,
        expected_edit_version=payload.expected_edit_version,
        reason=payload.reason,
        decisions=[item.model_dump(mode="python") for item in payload.decisions],
    )
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.post(
    "/wells/{well_id}/daily-reports/{report_id}/corrections",
    status_code=201,
    summary="Create a new numbered correction from the latest terminal revision",
)
async def create_daily_report_correction(
    well_id: str,
    report_id: str,
    payload: DailyReportRevisionReason,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.write"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "daily_report.revise", context={"well_id": well_id, "daily_report_id": report_id})
    org_id = auth.org_id or ""
    scope = f"daily_report.revise:{report_id}"
    body = payload.model_dump(mode="json")
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, body=body)) is not None:
        return replayed
    response = await _service(session, auth, request).create_correction(
        well_id,
        report_id,
        reason=payload.reason,
    )
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.post(
    "/wells/{well_id}/daily-reports/{report_id}/revisions/{revision_number}/submit",
    summary="Submit a reconciled draft for independent review",
)
async def submit_daily_report(
    well_id: str,
    report_id: str,
    revision_number: int,
    payload: DailyReportDecision,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.submit"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "daily_report.submit", context={"well_id": well_id, "daily_report_id": report_id})
    org_id = auth.org_id or ""
    scope = f"daily_report.submit:{report_id}:{revision_number}"
    body = payload.model_dump(mode="json")
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, body=body)) is not None:
        return replayed
    response = await _service(session, auth, request).submit(
        well_id,
        report_id,
        revision_number,
        expected_edit_version=payload.expected_edit_version,
        reason=payload.reason,
    )
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.post(
    "/wells/{well_id}/daily-reports/{report_id}/revisions/{revision_number}/approve",
    summary="Approve a submitted revision as an independent reviewer",
)
async def approve_daily_report(
    well_id: str,
    report_id: str,
    revision_number: int,
    payload: DailyReportDecision,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.approve"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "daily_report.approve", context={"well_id": well_id, "daily_report_id": report_id})
    org_id = auth.org_id or ""
    scope = f"daily_report.approve:{report_id}:{revision_number}"
    body = payload.model_dump(mode="json")
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, body=body)) is not None:
        return replayed
    response = await _service(session, auth, request).decide(
        well_id,
        report_id,
        revision_number,
        decision="approved",
        expected_edit_version=payload.expected_edit_version,
        reason=payload.reason,
    )
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.post(
    "/wells/{well_id}/daily-reports/{report_id}/revisions/{revision_number}/reject",
    summary="Reject a submitted revision with a reason",
)
async def reject_daily_report(
    well_id: str,
    report_id: str,
    revision_number: int,
    payload: DailyReportDecision,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.reject"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "daily_report.reject", context={"well_id": well_id, "daily_report_id": report_id})
    org_id = auth.org_id or ""
    scope = f"daily_report.reject:{report_id}:{revision_number}"
    body = payload.model_dump(mode="json")
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, body=body)) is not None:
        return replayed
    response = await _service(session, auth, request).decide(
        well_id,
        report_id,
        revision_number,
        decision="rejected",
        expected_edit_version=payload.expected_edit_version,
        reason=payload.reason,
    )
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)


@router.post(
    "/wells/{well_id}/daily-reports/{report_id}/revisions/{revision_number}/withdraw",
    summary="Withdraw an owned draft or submission",
)
async def withdraw_daily_report(
    well_id: str,
    report_id: str,
    revision_number: int,
    payload: DailyReportDecision,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("daily_report.withdraw"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "daily_report.withdraw", context={"well_id": well_id, "daily_report_id": report_id})
    org_id = auth.org_id or ""
    scope = f"daily_report.withdraw:{report_id}:{revision_number}"
    body = payload.model_dump(mode="json")
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, body=body)) is not None:
        return replayed
    response = await _service(session, auth, request).withdraw(
        well_id,
        report_id,
        revision_number,
        expected_edit_version=payload.expected_edit_version,
        reason=payload.reason,
    )
    return await _remember(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
