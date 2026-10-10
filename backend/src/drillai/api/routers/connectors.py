"""Telemetry connector management over HTTP: registry, configuration, connection tests,
governed lifecycle transitions, channel mappings, and run ledger inspection.

Every endpoint enforces tenant isolation (`org_id`), RBAC (`connector.read`, `connector.test`,
`connector.manage`, `connector.control`), secret redaction, and SSRF validation. Mutating lifecycle
and configuration endpoints support ``Idempotency-Key`` and optimistic concurrency via
``expected_config_version``.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, get_db, require
from drillai.core.clock import utc_now
from drillai.core.errors import PermissionDenied, ValidationFailed
from drillai.core.idempotency import complete, replay_or_reserve
from drillai.security.actions import authorize
from drillai.telemetry.connectors import (
    ALLOWED_SECRET_SLOTS,
    CONNECTOR_DESIRED_STATES,
    CONNECTOR_PROFILES,
    CONNECTOR_RUNTIME_STATUSES,
    PROTOCOL_PROFILE_SPECS,
    ConnectorService,
)

router = APIRouter(tags=["connectors"])


def _service(session: AsyncSession, auth: AuthContext) -> ConnectorService:
    return ConnectorService(session, auth.org_id or "", principal=auth.principal)


class ConnectorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=3, max_length=120)
    name: str = Field(min_length=1, max_length=240)
    protocol_profile: str = Field(min_length=3, max_length=64)
    well_id: str = Field(min_length=1, max_length=64)
    wellbore_id: str | None = Field(default=None, max_length=64)
    operation_id: str | None = Field(default=None, max_length=64)
    endpoint_url: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    config: dict[str, Any] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)


class ConnectorUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_config_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=1000)
    name: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = Field(default=None, max_length=2000)
    endpoint_url: str | None = Field(default=None, max_length=500)
    config: dict[str, Any] | None = None
    secret_refs: dict[str, str] | None = None
    clear_secret_slots: list[str] | None = None


class ConnectorTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1000)
    expected_config_version: int | None = Field(default=None, ge=1)


class ConnectorPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_limit: int = Field(default=20, ge=1, le=50)


class HarnessConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol: str = Field(pattern="^(witsml|etp)$")
    fault_mode: str | None = Field(default=None, max_length=64)
    append_values: dict[str, float | None] | None = None
    depth_md: float = Field(default=2500.0, ge=0.0, le=20000.0)
    quality: str = Field(default="good", max_length=24)
    reset_rows: bool = Field(default=False)


@router.get("/connectors/profiles", summary="Supported telemetry connector protocol profiles")
async def list_connector_profiles(
    _: Annotated[AuthContext, Depends(require("connector.read"))],
) -> dict[str, Any]:
    return {
        "profiles": [spec.to_dict() for spec in PROTOCOL_PROFILE_SPECS.values()],
        "allowed_secret_slots": list(ALLOWED_SECRET_SLOTS),
        "desired_states": list(CONNECTOR_DESIRED_STATES),
        "runtime_statuses": list(CONNECTOR_RUNTIME_STATUSES),
    }


@router.post(
    "/connectors/harness/ensure",
    summary="Start or inspect non-production local WITSML SOAP and ETP WebSocket test servers",
)
async def ensure_protocol_harness(
    request: Request,
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "connector.manage")
    harness = getattr(request.app.state, "protocol_harness", None)
    if harness is None:
        raise PermissionDenied(
            "local protocol test harness is disabled in production",
            details={"reason": "production_forbidden"},
        )
    return await harness.ensure_started()


@router.post(
    "/connectors/harness/configure",
    summary="Configure fault modes or append test frames on the non-production local protocol harness",
)
async def configure_protocol_harness(
    payload: HarnessConfigureRequest,
    request: Request,
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "connector.manage")
    harness = getattr(request.app.state, "protocol_harness", None)
    if harness is None:
        raise PermissionDenied(
            "local protocol test harness is disabled in production",
            details={"reason": "production_forbidden"},
        )
    return await harness.configure(
        protocol=payload.protocol,
        fault_mode=payload.fault_mode,
        append_values=payload.append_values,
        depth_md=payload.depth_md,
        quality=payload.quality,
        reset_rows=payload.reset_rows,
    )


@router.get("/connectors", summary="List telemetry connectors")
async def list_connectors(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
    well_id: OptionalFilter = None,
    provider: OptionalFilter = None,
    status: OptionalFilter = None,
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    items, total = await _service(session, auth).list(
        well_id=well_id,
        provider=provider,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "profiles": list(CONNECTOR_PROFILES),
        "statuses": list(CONNECTOR_RUNTIME_STATUSES),
    }


@router.post("/connectors", summary="Create a telemetry connector", status_code=201)
async def create_connector(
    payload: ConnectorCreateRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "connector.manage")
    org_id = auth.org_id or ""
    scope = "connector.create"
    body = payload.model_dump(mode="json")
    if (
        replayed := await replay_or_reserve(
            session, org_id=org_id, key=idempotency_key, scope=scope, payload=body
        )
    ) is not None:
        return replayed

    response = await _service(session, auth).create(**payload.model_dump())
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    await session.commit()
    return response


@router.get("/connectors/{connector_id}", summary="Inspect one telemetry connector")
async def get_connector(
    connector_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
) -> dict[str, Any]:
    return await _service(session, auth).inspect(connector_id)


@router.patch("/connectors/{connector_id}", summary="Update a telemetry connector configuration")
async def update_connector(
    connector_id: str,
    payload: ConnectorUpdateRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "connector.manage")
    org_id = auth.org_id or ""
    scope = f"connector.update:{connector_id}"
    body = payload.model_dump(mode="json")
    if (
        replayed := await replay_or_reserve(
            session, org_id=org_id, key=idempotency_key, scope=scope, payload=body
        )
    ) is not None:
        return replayed

    response = await _service(session, auth).update(
        connector_id,
        name=payload.name,
        description=payload.description,
        endpoint_url=payload.endpoint_url,
        config=payload.config,
        secret_refs=payload.secret_refs,
        clear_secret_slots=payload.clear_secret_slots,
        expected_config_version=payload.expected_config_version,
        reason=payload.reason,
    )
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    await session.commit()
    return response


@router.post("/connectors/{connector_id}/test", summary="Test connectivity without persistent ingestion")
async def test_connector_connection(
    connector_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "connector.test")
    result = await _service(session, auth).test_connection(connector_id)
    await session.commit()
    return result


@router.post("/connectors/{connector_id}/preview", summary="Preview channel descriptors and bounded sample frames")
async def preview_connector(
    connector_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
    payload: ConnectorPreviewRequest | None = None,
) -> dict[str, Any]:
    authorize(auth.principal, "connector.test")
    limit = payload.sample_limit if payload else 20
    return await _service(session, auth).preview(connector_id, sample_limit=limit)


async def _run_transition(
    connector_id: str,
    action: str,
    payload: ConnectorTransitionRequest | None,
    session: AsyncSession,
    auth: AuthContext,
    idempotency_key: str | None,
) -> dict[str, Any]:
    authorize(auth.principal, "connector.control")
    org_id = auth.org_id or ""
    scope = f"connector.{action}:{connector_id}"
    body = payload.model_dump(mode="json") if payload else {}
    if (
        replayed := await replay_or_reserve(
            session, org_id=org_id, key=idempotency_key, scope=scope, payload=body
        )
    ) is not None:
        return replayed

    response = await _service(session, auth).transition(
        connector_id,
        action=action,
        reason=payload.reason if payload else None,
        expected_config_version=payload.expected_config_version if payload else None,
    )
    await complete(session, org_id=org_id, key=idempotency_key, scope=scope, response=response)
    await session.commit()
    return response


@router.post("/connectors/{connector_id}/start", summary="Enable and mark a connector to start")
async def start_connector(
    connector_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
    payload: ConnectorTransitionRequest | None = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    return await _run_transition(connector_id, "start", payload, session, auth, idempotency_key)


@router.post("/connectors/{connector_id}/stop", summary="Stop a connector with an audited reason")
async def stop_connector(
    connector_id: str,
    payload: ConnectorTransitionRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    return await _run_transition(connector_id, "stop", payload, session, auth, idempotency_key)


@router.post("/connectors/{connector_id}/restart", summary="Restart a connector and reset failure counters")
async def restart_connector(
    connector_id: str,
    payload: ConnectorTransitionRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    return await _run_transition(connector_id, "restart", payload, session, auth, idempotency_key)


@router.post("/connectors/{connector_id}/disable", summary="Disable a connector with an audited reason")
async def disable_connector(
    connector_id: str,
    payload: ConnectorTransitionRequest,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    return await _run_transition(connector_id, "disable", payload, session, auth, idempotency_key)


@router.post(
    "/connectors/{connector_id}/poll",
    summary="Execute an immediate fenced worker poll cycle for an enabled connector",
)
async def poll_connector_now(
    connector_id: str,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "connector.control")
    svc = _service(session, auth)
    row = await svc.get(connector_id)
    if not row.is_enabled or row.desired_state != "enabled":
        raise ValidationFailed(
            "connector must be started (desired_state='enabled') before polling",
            details={"connector_id": connector_id, "desired_state": row.desired_state},
        )
    now = utc_now()
    row.next_poll_at = now
    if row.status == "failed":
        row.status = "starting"
        row.error_count = 0
    row.lease_expires_at = None
    await session.commit()

    worker = request.app.state.connector_worker
    claimed = await worker.claim_connector(connector_id, now=now)
    if claimed is None:
        raise ValidationFailed(
            "connector could not be claimed by worker (already leased or not eligible)",
            details={"connector_id": connector_id},
        )
    outcome = await worker.execute_claimed_poll(claimed)
    session.expire_all()
    updated = await svc.inspect(connector_id)
    return {
        "outcome": asdict(outcome),
        "connector": updated,
    }


@router.get("/connectors/{connector_id}/runs", summary="Bounded connector run ledger")
async def list_connector_runs(
    connector_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    items = await _service(session, auth).list_runs(connector_id, limit=limit)
    return {
        "connector_id": connector_id,
        "items": items,
        "total": len(items),
        "limit": limit,
    }


@router.get("/connectors/{connector_id}/mappings", summary="Inspect connector channel mappings and masked secret references")
async def get_connector_mappings(
    connector_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("connector.read"))],
) -> dict[str, Any]:
    info = await _service(session, auth).inspect(connector_id)
    return {
        "connector_id": info["id"],
        "protocol_profile": info["protocol_profile"],
        "config_version": info["config_version"],
        "channel_mappings": info["channel_mappings"],
        "secret_refs": info["secret_refs"],
    }
