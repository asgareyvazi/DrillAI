"""Operations and events — the work the well was drilled with, and what happened while it was.

These are the two records everything else in the operational stack hangs from: the timeline orders them,
NPT charges against them, the twin's plan-vs-actual view compares them and the report exports them. Both
were reachable only through the DDR promotion path before CP8 — there was no way to record an operation
by hand, correct one, or link a document to one — which meant the only operations in the platform were
the ones a parser happened to produce.

The module is thin on purpose. It validates the wire shape, resolves the caller's organization, declares
the permission and the action, and hands the work to
:class:`~drillai.operations.service.OperationService` / :class:`~drillai.operations.events.EventService`,
where the vocabulary, the transitions, the sequencing invariants, the version check and the audit ledger
live. A rule that exists only in a router is a rule the domain layer eventually contradicts, and a
mutation that exists only in a router leaves no ledger entry.

No delete endpoints. An operation that did not happen is cancelled; an event raised in error is
cancelled. Both keep their row, because the row is the evidence that they were once recorded.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, OptionalFilter, get_db, require
from drillai.api.serializers import event_out, evidence_out, operation_out
from drillai.core.idempotency import complete, replay_or_reserve
from drillai.documents.scope import DocumentScope
from drillai.operations.events import STATUS_TRANSITIONS as EVENT_TRANSITIONS
from drillai.operations.events import EventService
from drillai.operations.service import STATUS_TRANSITIONS as OPERATION_TRANSITIONS
from drillai.operations.service import OperationService
from drillai.security.actions import authorize

router = APIRouter(tags=["operations"])

#: A timestamp with no timezone offset is refused, in words, rather than guessed at. The columns store
#: UTC instants; reading "2026-01-15" as the server's local time would put an operation hours away from
#: where the caller meant, and nothing downstream would notice.
_TZ_HELP = "timestamps must include a timezone offset, e.g. 2026-01-15T08:00:00+00:00"


class OperationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    well_id: str | None = None
    wellbore_id: str | None = None
    section_id: str | None = None
    name: str = Field(min_length=1, max_length=300)
    operation_class: str = "actual"
    kind: str = "drilling"
    phase: str | None = None
    status: str | None = None
    sequence: int | None = Field(default=None, ge=0)
    predecessor_operation_id: str | None = None
    parent_operation_id: str | None = None
    code: str | None = Field(default=None, max_length=60)
    planned_start: dt.datetime | None = None
    planned_end: dt.datetime | None = None
    actual_start: dt.datetime | None = None
    actual_end: dt.datetime | None = None
    planned_duration_hours: float | None = Field(default=None, ge=0)
    actual_duration_hours: float | None = Field(default=None, ge=0)
    depth_from_md_si: float | None = None
    depth_to_md_si: float | None = None
    hole_diameter_si: float | None = None
    npt_hours: float | None = Field(default=None, ge=0)
    invisible_lost_time_hours: float | None = Field(default=None, ge=0)
    is_productive: bool | None = None
    remarks: str | None = None
    source_kind: str = "manual"
    source_document_id: str | None = None
    source_record_id: str | None = None

    def scope(self) -> DocumentScope:
        return DocumentScope(
            project_id=self.project_id,
            well_id=self.well_id,
            wellbore_id=self.wellbore_id,
            section_id=self.section_id,
        )

    def fields(self) -> dict[str, Any]:
        """Everything that becomes a column, minus the parts the service takes by name.

        Provenance is taken by name on purpose: an operation cites the document it was read from and
        the record inside it, and both are checked against the caller's organization before the row is
        written. A free-form ``evidence_ref`` here would be a second, unchecked way into the evidence
        graph.
        """
        taken = {
            "project_id",
            "well_id",
            "wellbore_id",
            "section_id",
            "name",
            "operation_class",
            "kind",
            "phase",
            "status",
            "sequence",
            "predecessor_operation_id",
            "parent_operation_id",
            "source_kind",
            "source_document_id",
            "source_record_id",
        }
        return {key: value for key, value in self.model_dump().items() if key not in taken}


class OperationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_updated_at: dt.datetime
    reason: str = Field(min_length=1, max_length=2000)
    changes: dict[str, Any] = Field(min_length=1)

    def validated_changes(self) -> dict[str, Any]:
        """The caller's changes, unexamined.

        Field ownership is checked in the service, which also knows *who owns* each field and reports
        that. Checking here as well would mean two answers to the same question, and the one the caller
        sees would depend on which ran first — the router's, which cannot name the owner.
        """
        return self.changes


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    expected_updated_at: dt.datetime
    reason: str | None = Field(default=None, max_length=2000)


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predecessor_operation_id: str | None = None


class LinkDocumentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str | None = None


class EventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    well_id: str | None = None
    wellbore_id: str | None = None
    section_id: str | None = None
    operation_id: str | None = None
    title: str = Field(min_length=1, max_length=400)
    kind: str = "observation"
    severity: str = "low"
    status: str = "open"
    occurred_at: dt.datetime | None = None
    ended_at: dt.datetime | None = None
    description: str | None = None
    duration_hours: float | None = Field(default=None, ge=0)
    depth_md_si: float | None = None
    depth_tvd_si: float | None = None
    npt_hours: float | None = Field(default=None, ge=0)
    npt_category: str | None = None
    is_npt: bool | None = None
    cost_usd: float | None = Field(default=None, ge=0)
    root_cause: str | None = None
    immediate_action: str | None = None
    corrective_action: str | None = None
    cause_basis: str = "unknown"
    classification_source: str = "recorded"
    tags: list[str] | None = None
    source_kind: str = "manual"
    source_document_id: str | None = None
    source_record_id: str | None = None
    evidence_ref: str | None = None

    def scope(self) -> DocumentScope:
        return DocumentScope(
            project_id=self.project_id,
            well_id=self.well_id,
            wellbore_id=self.wellbore_id,
            section_id=self.section_id,
        )

    def fields(self) -> dict[str, Any]:
        """Everything that becomes a column, minus the parts the service takes by name.

        ``npt_category`` and ``is_npt`` are taken by name because the service decides whether a
        category implies NPT hours — keeping them in both places would let the two answers disagree.
        """
        taken = {
            "project_id",
            "well_id",
            "wellbore_id",
            "section_id",
            "operation_id",
            "title",
            "kind",
            "severity",
            "status",
            "occurred_at",
            "cause_basis",
            "classification_source",
            "source_kind",
            "source_document_id",
            "source_record_id",
            "evidence_ref",
            "npt_category",
            "is_npt",
        }
        return {key: value for key, value in self.model_dump().items() if key not in taken}


class EventPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_updated_at: dt.datetime
    reason: str = Field(min_length=1, max_length=2000)
    changes: dict[str, Any] = Field(min_length=1)


def _operation_service(session: AsyncSession, auth: AuthContext) -> OperationService:
    return OperationService(session, auth.org_id or "", principal=auth.principal)


def _event_service(session: AsyncSession, auth: AuthContext) -> EventService:
    return EventService(session, auth.org_id or "", principal=auth.principal)


async def _replay(
    session: AsyncSession, *, org_id: str, key: str | None, scope: str, payload: Any
) -> dict[str, Any] | None:
    return await replay_or_reserve(session, org_id=org_id, key=key, scope=scope, payload=payload)


async def _remember(
    session: AsyncSession, *, org_id: str, key: str | None, scope: str, response: dict[str, Any]
) -> dict[str, Any]:
    await complete(session, org_id=org_id, key=key, scope=scope, response=response)
    return response


def _with_transitions(row: Any, serializer: Any, transitions: dict[str, frozenset[str]]) -> dict[str, Any]:
    payload = serializer(row)
    payload["allowed_transitions"] = sorted(transitions.get(row.status, frozenset()))
    return payload


# --- operations ------------------------------------------------------------------------------


@router.get("/operations", summary="List operations")
async def list_operations(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("operation.read"))],
    well_id: OptionalFilter = None,
    wellbore_id: OptionalFilter = None,
    section_id: OptionalFilter = None,
    operation_class: OptionalFilter = None,
    status: OptionalFilter = None,
    kind: OptionalFilter = None,
    source_kind: OptionalFilter = None,
    document_id: OptionalFilter = None,
    since: dt.datetime | None = Query(default=None, description=_TZ_HELP),
    until: dt.datetime | None = Query(default=None, description=_TZ_HELP),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Filtering and paging happen in SQL, and the ordering is the drilling sequence.

    ``total`` is the count *before* paging, computed by the database over the same filtered statement —
    a client that pages without it cannot tell an empty result from a truncated one.
    """
    service = _operation_service(session, auth)
    stmt = service.statement(
        well_id=well_id,
        wellbore_id=wellbore_id,
        section_id=section_id,
        operation_class=operation_class,
        status=status,
        kind=kind,
        source_kind=source_kind,
        document_id=document_id,
        since=since,
        until=until,
    )
    total = await service.count(stmt)
    rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return {
        "items": [_with_transitions(row, operation_out, OPERATION_TRANSITIONS) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/operations", summary="Create an operation", status_code=201)
async def create_operation(
    payload: OperationCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("operation.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "operation.create")
    org_id = auth.org_id or ""
    scope = "operation.create"
    body = payload.model_dump(mode="json")
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    operation = await _operation_service(session, auth).create(
        scope=payload.scope(),
        name=payload.name,
        operation_class=payload.operation_class,
        kind=payload.kind,
        phase=payload.phase,
        status=payload.status,
        sequence=payload.sequence,
        predecessor_operation_id=payload.predecessor_operation_id,
        parent_operation_id=payload.parent_operation_id,
        source_kind=payload.source_kind,
        source_document_id=payload.source_document_id,
        source_record_id=payload.source_record_id,
        **payload.fields(),
    )
    return await _remember(
        session,
        org_id=org_id,
        key=idempotency_key,
        scope=scope,
        response=_with_transitions(operation, operation_out, OPERATION_TRANSITIONS),
    )


@router.get("/operations/{operation_id}", summary="One operation, with its events and evidence")
async def get_operation(
    operation_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("operation.read"))],
) -> dict[str, Any]:
    service = _operation_service(session, auth)
    operation = await service.get(operation_id)
    events = await service.events_for(operation_id)
    evidence = await service.evidence_for(operation_id)
    return {
        **_with_transitions(operation, operation_out, OPERATION_TRANSITIONS),
        "events": [event_out(row) for row in events],
        "evidence_links": [evidence_out(row) for row in evidence],
    }


@router.patch("/operations/{operation_id}", summary="Correct an operation")
async def update_operation(
    operation_id: str,
    payload: OperationPatch,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("operation.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    """Correct a field on an existing operation. The reason is required, and so is the version."""
    authorize(auth.principal, "operation.update")
    org_id = auth.org_id or ""
    changes = payload.validated_changes()
    scope = f"operation.update:{operation_id}"
    body = {"changes": changes, "reason": payload.reason}
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    operation, recorded = await _operation_service(session, auth).update(
        operation_id,
        expected_updated_at=payload.expected_updated_at,
        reason=payload.reason,
        changes=changes,
    )
    return await _remember(
        session,
        org_id=org_id,
        key=idempotency_key,
        scope=scope,
        response={
            **_with_transitions(operation, operation_out, OPERATION_TRANSITIONS),
            "changes": [record.as_dict() for record in recorded],
        },
    )


@router.post("/operations/{operation_id}/transition", summary="Move an operation's status")
async def transition_operation(
    operation_id: str,
    payload: TransitionRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("operation.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "operation.transition")
    org_id = auth.org_id or ""
    scope = f"operation.transition:{operation_id}"
    body = {"status": payload.status, "reason": payload.reason}
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    operation = await _operation_service(session, auth).transition(
        operation_id,
        status=payload.status,
        expected_updated_at=payload.expected_updated_at,
        reason=payload.reason,
    )
    return await _remember(
        session,
        org_id=org_id,
        key=idempotency_key,
        scope=scope,
        response=_with_transitions(operation, operation_out, OPERATION_TRANSITIONS),
    )


@router.post("/operations/{operation_id}/predecessor", summary="Link an operation to the one before it")
async def link_predecessor(
    operation_id: str,
    payload: LinkRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("operation.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "operation.link_predecessor")
    operation = await _operation_service(session, auth).link_predecessor(
        operation_id, payload.predecessor_operation_id
    )
    return _with_transitions(operation, operation_out, OPERATION_TRANSITIONS)


@router.post("/operations/{operation_id}/document", summary="Link an operation to its source document")
async def link_operation_document(
    operation_id: str,
    payload: LinkDocumentRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("operation.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "operation.link_document")
    operation = await _operation_service(session, auth).link_document(operation_id, payload.document_id)
    return _with_transitions(operation, operation_out, OPERATION_TRANSITIONS)


# --- events ----------------------------------------------------------------------------------


@router.get("/events", summary="List events")
async def list_events(
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("event.read"))],
    well_id: OptionalFilter = None,
    wellbore_id: OptionalFilter = None,
    operation_id: OptionalFilter = None,
    kind: OptionalFilter = None,
    status: OptionalFilter = None,
    severity: OptionalFilter = None,
    npt_category: OptionalFilter = None,
    source_kind: OptionalFilter = None,
    document_id: OptionalFilter = None,
    is_npt: bool | None = Query(default=None),
    since: dt.datetime | None = Query(default=None, description=_TZ_HELP),
    until: dt.datetime | None = Query(default=None, description=_TZ_HELP),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    service = _event_service(session, auth)
    stmt = service.statement(
        well_id=well_id,
        wellbore_id=wellbore_id,
        operation_id=operation_id,
        kind=kind,
        status=status,
        severity=severity,
        is_npt=is_npt,
        npt_category=npt_category,
        source_kind=source_kind,
        document_id=document_id,
        since=since,
        until=until,
    )
    total = await service.count(stmt)
    rows = (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()
    return {
        "items": [_with_transitions(row, event_out, EVENT_TRANSITIONS) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/events", summary="Create an event", status_code=201)
async def create_event(
    payload: EventCreate,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("event.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "event.create")
    org_id = auth.org_id or ""
    scope = "event.create"
    body = payload.model_dump(mode="json")
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    event = await _event_service(session, auth).create(
        scope=payload.scope(),
        title=payload.title,
        kind=payload.kind,
        severity=payload.severity,
        occurred_at=payload.occurred_at,
        operation_id=payload.operation_id,
        status=payload.status,
        source_kind=payload.source_kind,
        source_document_id=payload.source_document_id,
        source_record_id=payload.source_record_id,
        evidence_ref=payload.evidence_ref,
        cause_basis=payload.cause_basis,
        classification_source=payload.classification_source,
        npt_category=payload.npt_category,
        is_npt=payload.is_npt,
        **payload.fields(),
    )
    return await _remember(
        session,
        org_id=org_id,
        key=idempotency_key,
        scope=scope,
        response=_with_transitions(event, event_out, EVENT_TRANSITIONS),
    )


@router.get("/events/{event_id}", summary="One event, with its evidence")
async def get_event(
    event_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("event.read"))],
) -> dict[str, Any]:
    service = _event_service(session, auth)
    event = await service.get(event_id)
    evidence = await service.evidence_for(event_id)
    return {
        **_with_transitions(event, event_out, EVENT_TRANSITIONS),
        "evidence_links": [evidence_out(row) for row in evidence],
    }


@router.patch("/events/{event_id}", summary="Correct an event")
async def update_event(
    event_id: str,
    payload: EventPatch,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("event.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "event.update")
    org_id = auth.org_id or ""
    scope = f"event.update:{event_id}"
    body = {"changes": payload.changes, "reason": payload.reason}
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    event, recorded = await _event_service(session, auth).update(
        event_id,
        expected_updated_at=payload.expected_updated_at,
        reason=payload.reason,
        changes=payload.changes,
    )
    return await _remember(
        session,
        org_id=org_id,
        key=idempotency_key,
        scope=scope,
        response={
            **_with_transitions(event, event_out, EVENT_TRANSITIONS),
            "changes": [record.as_dict() for record in recorded],
        },
    )


@router.post("/events/{event_id}/transition", summary="Move an event's status")
async def transition_event(
    event_id: str,
    payload: TransitionRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("event.read"))],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=160)] = None,
) -> dict[str, Any]:
    authorize(auth.principal, "event.transition")
    org_id = auth.org_id or ""
    scope = f"event.transition:{event_id}"
    body = {"status": payload.status, "reason": payload.reason}
    if (replayed := await _replay(session, org_id=org_id, key=idempotency_key, scope=scope, payload=body)) is not None:
        return replayed
    event = await _event_service(session, auth).transition(
        event_id,
        status=payload.status,
        expected_updated_at=payload.expected_updated_at,
        reason=payload.reason,
    )
    return await _remember(
        session,
        org_id=org_id,
        key=idempotency_key,
        scope=scope,
        response=_with_transitions(event, event_out, EVENT_TRANSITIONS),
    )


@router.post("/events/{event_id}/document", summary="Link an event to its source document")
async def link_event_document(
    event_id: str,
    payload: LinkDocumentRequest,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("event.read"))],
) -> dict[str, Any]:
    authorize(auth.principal, "event.link_document")
    event = await _event_service(session, auth).link_document(event_id, payload.document_id)
    return _with_transitions(event, event_out, EVENT_TRANSITIONS)
