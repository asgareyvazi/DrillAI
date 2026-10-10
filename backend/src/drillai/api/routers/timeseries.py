"""Telemetry over HTTP: channels, batches, windows, latest values.

The router is thin, and thin in a specific way: it validates the wire shape, resolves the caller's
organization, declares the permission and the action, and hands everything to
:class:`~drillai.telemetry.service.TelemetryService`. The vocabulary, the unit conversion, the identity
rules and the freshness policy live in the domain layer — a rule that exists only in a router is a rule
the service eventually contradicts.

Two habits worth naming, because they are the difference between an API that reports and one that
reassures:

* every list answer carries ``total`` beside its page, so a client can tell an empty well from a
  truncated one;
* a latest-values answer carries each reading's ``quality``, ``observed_at`` and ``freshness`` together
  with the value, so no screen can render a number without what it is worth.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, get_db, require
from drillai.api.serializers import channel_out, point_out
from drillai.core.clock import utc_now
from drillai.core.idempotency import complete, replay_or_reserve
from drillai.security.actions import authorize
from drillai.telemetry.adapters import (
    AdapterRunner,
    ChannelDescriptor,
    SyntheticAdapter,
)
from drillai.telemetry.identity import resolve_channel_scope
from drillai.telemetry.service import PointIn, TelemetryService

router = APIRouter(tags=["telemetry"])

_TZ_HELP = "timestamps must include a timezone offset, e.g. 2026-01-15T08:00:00+00:00"


def _service(session: AsyncSession, auth: AuthContext) -> TelemetryService:
    # Settings come from the one cached accessor every other router uses. The thresholds they carry are
    # the contract the browser is told about as well (see the latest endpoint's answer), so a screen and
    # this service can never disagree about what "stale" means.
    from drillai.core.config import get_settings

    settings = get_settings()
    return TelemetryService(
        session,
        auth.org_id or "",
        fresh_seconds=settings.telemetry_fresh_seconds,
        stale_seconds=settings.telemetry_stale_seconds,
        late_seconds=settings.telemetry_late_seconds,
        max_latest_channels=settings.telemetry_max_latest_channels,
        max_window_points=settings.telemetry_max_window_points,
        max_batch_points=settings.telemetry_max_batch_points,
    )


class ChannelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    well_id: str
    wellbore_id: str | None = None
    operation_id: str | None = None
    channel_key: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=200)
    dimension: str
    unit: str | None = Field(default=None, description="the unit the source sends")
    is_realtime: bool = False
    source: str = "manual"
    source_ref: str | None = None
    description: str | None = None
    sampling_hint_seconds: float | None = Field(default=None, gt=0)


class PointInModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: dt.datetime
    value: float | None = None
    unit: str | None = None
    quality: str = "good"
    source_point_id: str | None = None
    source_ref: str | None = None
    sequence: int | None = None
    depth_md_si: float | None = None
    received_at: dt.datetime | None = None


class PointsBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    points: list[PointInModel] = Field(min_length=1)
    source_ref: str | None = None
    default_unit: str | None = None
    #: What to do when a source identity arrives again with a *different* value. ``reject`` (default)
    #: refuses and reports it; ``revise`` updates the stored point and keeps the previous value in its
    #: revision trail. Neither choice overwrites silently.
    on_conflict: str = "reject"


@router.get("/timeseries", summary="List telemetry channels")
async def list_channels(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("timeseries.read"))],
    well_id: OptionalFilter = None,
    wellbore_id: OptionalFilter = None,
    operation_id: OptionalFilter = None,
    channel_key: OptionalFilter = None,
    dimension: OptionalFilter = None,
    is_realtime: bool | None = Query(default=None),
    source: OptionalFilter = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    if well_id is not None:
        # A filter that names a well is a question about *that* well. Answering "no channels" for a well
        # that does not exist — or that belongs to another organization — would make the two
        # indistinguishable to the caller, which is how a tenant probe starts.
        await resolve_channel_scope(
            session, org_id=auth.org_id or "", well_id=well_id,
            wellbore_id=wellbore_id, operation_id=operation_id,
        )
    service = _service(session, auth)
    rows, total = await service.list_channels(
        limit=limit,
        offset=offset,
        well_id=well_id,
        wellbore_id=wellbore_id,
        operation_id=operation_id,
        channel_key=channel_key,
        dimension=dimension,
        is_realtime=is_realtime,
        source=source,
    )
    return {
        "items": [channel_out(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/timeseries", summary="Register a telemetry channel", status_code=201)
async def create_channel(
    payload: ChannelCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("timeseries.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "timeseries.create")
    org_id = auth.org_id or ""
    scope = "timeseries.create"
    body = payload.model_dump(mode="json")
    if (replayed := await replay_or_reserve(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    service = _service(session, auth)
    channel, created = await service.create_channel(
        well_id=payload.well_id,
        wellbore_id=payload.wellbore_id,
        operation_id=payload.operation_id,
        channel_key=payload.channel_key,
        name=payload.name,
        dimension=payload.dimension,
        unit=payload.unit,
        is_realtime=payload.is_realtime,
        source=payload.source,
        source_ref=payload.source_ref,
        description=payload.description,
        sampling_hint_seconds=payload.sampling_hint_seconds,
        principal=auth.principal,
    )
    await session.commit()
    response = channel_out(channel, created=created)
    # 201 for a creation, 200 for "this channel already exists" — the second is the answer a retried
    # request needs, and it is not the same fact as the first.
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    await session.commit()
    return response


@router.get("/timeseries/{series_id}", summary="One telemetry channel")
async def get_channel(
    series_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("timeseries.read"))],
) -> dict[str, Any]:
    service = _service(session, auth)
    channel = await service.channel_by_id(series_id)
    return channel_out(channel)


@router.get("/timeseries/{series_id}/points", summary="A page of measurements")
async def get_points(
    series_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("timeseries.read"))],
    since: dt.datetime | None = Query(default=None, description=_TZ_HELP),
    until: dt.datetime | None = Query(default=None, description=_TZ_HELP),
    limit: int = Query(default=500, ge=1, le=2000),
    cursor: str | None = Query(default=None, description="opaque position returned as next_cursor"),
    order: str = Query(default="asc", pattern="^(asc|desc)$"),
) -> dict[str, Any]:
    service = _service(session, auth)
    page = await service.window(
        series_id, since=since, until=until, limit=limit, cursor=cursor, order=order
    )
    return {
        "channel_id": page.channel_id,
        "unit": page.unit,
        "items": [point_out(row) for row in page.points],
        "total": page.count,
        "returned": page.returned,
        "limit": page.limit,
        "next_cursor": page.next_cursor,
        "truncated": page.truncated,
    }


@router.post("/timeseries/{series_id}/points", summary="Append measurements", status_code=201)
async def append_points(
    series_id: str,
    payload: PointsBatch,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("timeseries.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    """Append a batch and report what was actually done with it.

    The response is a reconciliation, not an acknowledgement: ``received``, ``accepted``, ``duplicates``,
    ``rejected``, ``late``, ``out_of_order``, ``revised`` and the quality distribution. A caller that is
    told 982 of 1 000 were accepted can find 982 rows.
    """

    authorize(auth.principal, "timeseries.append")
    org_id = auth.org_id or ""
    scope = "timeseries.append"
    # The idempotency payload is the *batch's own* identity: the channel, the source and the points'
    # source identities. Hashing the full point list would make a legitimate second batch of identical
    # values a "different request"; hashing nothing would make every retry a fresh ingestion.
    fingerprint_payload = {
        "series_id": series_id,
        "source_ref": payload.source_ref,
        "points": [
            {
                "ts": point.ts.isoformat(),
                "source_point_id": point.source_point_id,
                "value": point.value,
                "sequence": point.sequence,
            }
            for point in payload.points
        ],
        "on_conflict": payload.on_conflict,
    }
    if (replayed := await replay_or_reserve(session, org_id=org_id, key=idempotency_key, scope=scope, payload=fingerprint_payload)) is not None:
        return replayed

    service = _service(session, auth)
    report = await service.append_points(
        series_id,
        [
            PointIn(
                ts=point.ts,
                value=point.value,
                unit=point.unit,
                quality=point.quality,
                source_point_id=point.source_point_id,
                source_ref=point.source_ref,
                sequence=point.sequence,
                depth_md_si=point.depth_md_si,
                received_at=point.received_at,
            )
            for point in payload.points
        ],
        source_ref=payload.source_ref,
        default_unit=payload.default_unit,
        on_conflict=payload.on_conflict,
        principal=auth.principal,
        trace_id=getattr(request.state, "request_id", None),
    )
    response = report.to_dict()
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    await session.commit()
    return response


@router.get("/wells/{well_id}/timeseries/latest", summary="The newest reading per channel")
async def well_latest(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("timeseries.read"))],
    wellbore_id: OptionalFilter = None,
    operation_id: OptionalFilter = None,
    channel_key: list[str] | None = Query(default=None, description="repeatable channel key filter"),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict[str, Any]:
    """Every channel the well has, each with its newest value and what that value is worth.

    A channel with no measurement is returned with ``value: null`` and ``freshness: "missing"`` rather
    than omitted: "this channel exists and has never reported" is a fact an operator needs, and a
    stripped list would make it look like the channel does not exist. The values of a channel that has
    stopped reporting arrive with ``freshness: "stale"`` and their age, so no screen can present them as
    current.
    """

    # The same rule the write path enforces, on the read path: the well must exist inside the caller's
    # organization. "This well has no channels" and "this well is not yours" are different answers.
    await resolve_channel_scope(
        session, org_id=auth.org_id or "", well_id=well_id,
        wellbore_id=wellbore_id, operation_id=operation_id,
    )
    service = _service(session, auth)
    readings = await service.latest(
        well_id=well_id,
        wellbore_id=wellbore_id,
        operation_id=operation_id,
        channel_keys=channel_key,
        limit=limit,
    )
    from drillai.api.serializers import latest_reading_out

    return {
        "well_id": well_id,
        "generated_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "items": [latest_reading_out(reading) for reading in readings],
        "total": len(readings),
        "fresh_seconds": service.fresh_seconds,
        "stale_seconds": service.stale_seconds,
    }


class SyntheticChannelPlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_key: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=200)
    dimension: str
    unit: str
    values: list[float] = Field(min_length=1, max_length=500)
    start: dt.datetime | None = None
    step_seconds: float = Field(default=5.0, gt=0)
    quality: str = "good"
    source_prefix: str = "syn"


class SyntheticCommissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wellbore_id: str | None = None
    operation_id: str | None = None
    channels: list[SyntheticChannelPlanModel] = Field(min_length=1, max_length=50)
    subscribe: list[str] | None = None


@router.post(
    "/wells/{well_id}/timeseries/commission-synthetic",
    summary="Execute a deterministic synthetic telemetry commissioning run through AdapterRunner",
    status_code=201,
)
async def commission_synthetic_telemetry(
    well_id: str,
    payload: SyntheticCommissionRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("timeseries.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    """Run a declared :class:`SyntheticAdapter` plan through :class:`AdapterRunner` and
    :class:`TelemetryService`, exercising channel registration, unit normalization, automatic rule
    evaluation, outbox emission, and runtime health reporting."""

    import contextlib

    authorize(auth.principal, "timeseries.append")
    org_id = auth.org_id or ""
    scope = "timeseries.commission_synthetic"
    body = {"well_id": well_id, **payload.model_dump(mode="json")}
    if (
        replayed := await replay_or_reserve(
            session, org_id=org_id, key=idempotency_key, scope=scope, payload=body
        )
    ) is not None:
        return replayed

    await resolve_channel_scope(
        session,
        org_id=org_id,
        well_id=well_id,
        wellbore_id=payload.wellbore_id,
        operation_id=payload.operation_id,
    )
    default_start = next(
        (item.start for item in payload.channels if item.start is not None),
        utc_now() - dt.timedelta(seconds=30),
    )
    descriptors = [
        ChannelDescriptor(
            channel_key=item.channel_key,
            name=item.name,
            dimension=item.dimension,
            unit=item.unit,
            is_realtime=True,
        )
        for item in payload.channels
    ]
    plan: dict[str, list[tuple[float, float | None]]] = {
        item.channel_key: [
            (float(idx * item.step_seconds), float(val)) for idx, val in enumerate(item.values)
        ]
        for item in payload.channels
    }
    adapter = SyntheticAdapter(descriptors, plan, start=default_start)

    @contextlib.asynccontextmanager
    async def _borrowed_session():
        yield session

    runner = AdapterRunner(
        session_factory=_borrowed_session,
        org_id=org_id,
        well_id=well_id,
        wellbore_id=payload.wellbore_id,
        operation_id=payload.operation_id,
        adapter=adapter,
        subscribe=payload.subscribe,
        principal=auth.principal,
    )
    max_polls = max((len(item.values) for item in payload.channels), default=1) + 2
    report = await runner.run_until_drained(max_polls=max_polls)
    response = {
        "well_id": well_id,
        "report": report.to_dict(),
        "health": runner.health().to_dict(),
    }
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    await session.commit()
    return response

