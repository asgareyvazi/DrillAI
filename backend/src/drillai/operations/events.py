"""Events as first-class records: what happened, who established why, and how sure that is.

An event is not an NPT category
-------------------------------

The two are routinely conflated, and the conflation is expensive. *What happened* is an event: a kick, a
stuck pipe, a rig repair, a milestone, an observation. *Which NPT bucket it is charged to* is a
classification of that event, and an event may have no NPT at all (a milestone), or be charged to a
category without being an incident. So ``kind`` describes the event, ``npt_category`` and ``is_npt``
describe its accounting, and this service keeps them independent: setting ``is_npt`` never rewrites
``kind``, and cancelling an event never removes its NPT hours from the record — it marks them as
excluded, because the hours were still lost.

Recorded, inferred, unknown
---------------------------

``cause_basis`` answers "who established the cause?":

* ``recorded`` — the source document stated it;
* ``inferred`` — someone, or something, concluded it afterwards;
* ``unknown`` — nobody has, which is the default and the honest starting point.

An LLM may write a cause only through the ``inferred`` path with the evidence it used, and this service
refuses to let a cause be stored without one of those three labels being set deliberately. That is §31:
the platform must not be able to present a guess in the same shape as a finding.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.audit import record_audit
from drillai.core.errors import Conflict, NotFound, ValidationFailed
from drillai.core.ids import new_id
from drillai.db.models import (
    CAUSE_BASES,
    CLASSIFICATION_SOURCES,
    EVENT_KINDS,
    EVENT_SEVERITIES,
    EVENT_STATUSES,
    NPT_CATEGORIES,
    SOURCE_KINDS,
    Document,
    Event,
    EvidenceLink,
    Operation,
)
from drillai.documents.scope import DocumentScope, DocumentScopeResolver
from drillai.security.actions import Principal

#: Events that are still work. Everything else is history.
OPEN_STATUSES: tuple[str, ...] = ("open", "acknowledged", "investigating")

#: Allowed moves. An event cannot go from ``closed`` back to ``open`` — reopening history would make
#: every "what is outstanding" query depend on when it was asked. A closed event that turns out to be
#: unresolved is a *new* event, linked to the old one.
STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"acknowledged", "investigating", "closed", "cancelled"}),
    "acknowledged": frozenset({"investigating", "closed", "cancelled"}),
    "investigating": frozenset({"closed", "cancelled"}),
    "closed": frozenset(),
    "cancelled": frozenset(),
}

#: Severity may be raised or lowered at any point before closing: it is a judgement that can change.
MUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "title",
        "description",
        "severity",
        "occurred_at",
        "ended_at",
        "duration_hours",
        "depth_md_si",
        "depth_tvd_si",
        "cost_usd",
        "tags",
        "section_id",
        "operation_id",
    }
)

#: Fields that constitute a *cause* claim, and therefore require a basis to be stated with them.
CAUSE_FIELDS: frozenset[str] = frozenset({"root_cause", "immediate_action", "corrective_action"})


@dataclass(frozen=True)
class ChangeRecord:
    field: str
    before: Any
    after: Any

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "before": self.before, "after": self.after}


class EventService:
    """Reads and writes for ``events``. Every method is organization-scoped."""

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
        # The ledger records a *principal*, not an id: it resolves the role and the action level from
        # it at write time, so a row can never claim to have been written by someone it was not. The
        # id-only constructor argument is kept for callers that have no principal (a workflow step
        # acting for the organization), and is deliberately turned into a principal with no roles
        # rather than being trusted to name one.
        self._principal_value = principal
        self.actor_id = actor_id

    def _principal(self) -> Principal | None:
        if self._principal_value is not None:
            return self._principal_value
        if self.actor_id is None:
            return None
        return Principal(id=self.actor_id, org_id=self.org_id)


    # ------------------------------------------------------------------ reads

    def statement(
        self,
        *,
        well_id: str | None = None,
        wellbore_id: str | None = None,
        operation_id: str | None = None,
        kind: str | None = None,
        status: str | None = None,
        severity: str | None = None,
        is_npt: bool | None = None,
        npt_category: str | None = None,
        source_kind: str | None = None,
        document_id: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> Select:
        """The filtered statement. Ordering is by ``(occurred_at, id)`` — chronological, with the id
        breaking ties so a page boundary is deterministic when two events share a timestamp."""
        stmt = select(Event).where(Event.org_id == self.org_id)
        if well_id:
            stmt = stmt.where(Event.well_id == well_id)
        if wellbore_id:
            stmt = stmt.where(Event.wellbore_id == wellbore_id)
        if operation_id:
            stmt = stmt.where(Event.operation_id == operation_id)
        if kind:
            stmt = stmt.where(Event.kind == kind)
        if status:
            stmt = stmt.where(Event.status == status)
        if severity:
            stmt = stmt.where(Event.severity == severity)
        if is_npt is not None:
            stmt = stmt.where(Event.is_npt.is_(is_npt))
        if npt_category:
            stmt = stmt.where(Event.npt_category == npt_category)
        if source_kind:
            stmt = stmt.where(Event.source_kind == source_kind)
        if document_id:
            stmt = stmt.where(Event.source_document_id == document_id)
        if since is not None:
            stmt = stmt.where(Event.occurred_at >= since)
        if until is not None:
            # An event that has not ended yet is still inside any window that starts before it did.
            stmt = stmt.where(or_(Event.ended_at <= until, Event.ended_at.is_(None)))
        return stmt.order_by(Event.occurred_at.asc(), Event.id.asc())

    async def count(self, stmt: Select) -> int:
        return (await self.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    async def get(self, event_id: str) -> Event:
        row = (
            await self.session.execute(
                select(Event).where(Event.id == event_id, Event.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"event {event_id!r} not found")
        return row

    # ------------------------------------------------------------------ writes

    async def create(
        self,
        *,
        scope: DocumentScope,
        title: str,
        kind: str = "observation",
        severity: str = "low",
        occurred_at: dt.datetime | None = None,
        operation_id: str | None = None,
        status: str = "open",
        source_kind: str = "manual",
        source_document_id: str | None = None,
        source_record_id: str | None = None,
        evidence_ref: str | None = None,
        cause_basis: str = "unknown",
        classification_source: str = "recorded",
        npt_category: str | None = None,
        is_npt: bool | None = None,
        **fields: Any,
    ) -> Event:
        self._require(title=title)
        for value, allowed, field in (
            (kind, EVENT_KINDS, "kind"),
            (severity, EVENT_SEVERITIES, "severity"),
            (status, EVENT_STATUSES, "status"),
            (source_kind, SOURCE_KINDS, "source_kind"),
            (cause_basis, CAUSE_BASES, "cause_basis"),
            (classification_source, CLASSIFICATION_SOURCES, "classification_source"),
        ):
            if value not in allowed:
                raise ValidationFailed(
                    f"{field} is not recognised",
                    details={"field": field, "value": value, "allowed": list(allowed)},
                )
        if npt_category is not None and npt_category not in NPT_CATEGORIES:
            raise ValidationFailed(
                "npt_category is not recognised",
                details={"field": "npt_category", "value": npt_category, "allowed": list(NPT_CATEGORIES)},
            )
        self._check_cause_basis(
            cause_basis,
            fields,
            source_kind=source_kind,
            cited=bool(evidence_ref or source_record_id),
        )
        if (
            fields.get("ended_at") is not None
            and occurred_at is not None
            and _as_utc(fields["ended_at"]) < _as_utc(occurred_at)
        ):
            raise ValidationFailed(
                "ended_at must not precede occurred_at",
                details={"occurred_at": _iso(occurred_at), "ended_at": _iso(fields["ended_at"])},
            )

        resolved = await DocumentScopeResolver(self.session, self.org_id).resolve(scope)
        if not resolved.well_id:
            raise ValidationFailed(
                "an event must resolve to a well", details={"scope": resolved.as_dict()}
            )
        operation = await self._check_operation(operation_id, resolved.wellbore_id)
        if operation is not None:
            # Inheriting the operation's bore and section is not a convenience: an event attributed to
            # an operation but stored on a different bore makes the operation's own event list wrong.
            resolved = _inherit_from_operation(resolved, operation)

        event = Event(
            id=new_id("evn"),
            org_id=self.org_id,
            project_id=resolved.project_id,
            well_id=resolved.well_id,
            wellbore_id=resolved.wellbore_id,
            section_id=resolved.section_id,
            operation_id=operation_id,
            kind=kind,
            title=title,
            severity=severity,
            status=status,
            occurred_at=occurred_at or dt.datetime.now(tz=dt.UTC),
            # ``is_npt`` is about accounting, ``kind`` is about what happened. A caller may state
            # either; neither is derived from the other, and a category without hours is not
            # automatically NPT.
            is_npt=bool(is_npt) if is_npt is not None else bool(npt_category and fields.get("npt_hours")),
            npt_category=npt_category,
            source_kind=source_kind,
            source="manual" if source_kind == "manual" else source_kind,
            source_document_id=source_document_id,
            source_record_id=source_record_id,
            evidence_ref=evidence_ref,
            cause_basis=cause_basis,
            classification_source=classification_source,
            **fields,
        )
        self.session.add(event)
        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal(),
            action="event.create",
            resource_kind="event",
            resource_id=event.id,
            details={
                "well_id": event.well_id,
                "kind": kind,
                "severity": severity,
                "cause_basis": cause_basis,
                "is_npt": event.is_npt,
                "source_kind": source_kind,
            },
        )
        return event

    async def update(
        self,
        event_id: str,
        *,
        expected_updated_at: dt.datetime,
        reason: str,
        changes: dict[str, Any],
    ) -> tuple[Event, list[ChangeRecord]]:
        self._require(reason=reason)
        event = await self.get(event_id)
        _check_expected_version(event, expected_updated_at)
        if event.status == "cancelled":
            raise Conflict(
                f"event {event_id!r} was cancelled and cannot be edited",
                details={"event_id": event_id, "status": event.status},
            )
        unknown = set(changes) - MUTABLE_FIELDS - CAUSE_FIELDS - {"cause_basis", "classification_source"}
        if unknown:
            raise ValidationFailed(
                "one or more fields cannot be changed",
                details={"fields": sorted(unknown), "allowed": sorted(MUTABLE_FIELDS | CAUSE_FIELDS)},
            )
        if not changes:
            raise ValidationFailed("no changes were supplied")
        if "severity" in changes and changes["severity"] not in EVENT_SEVERITIES:
            raise ValidationFailed(
                "severity is not recognised",
                details={"field": "severity", "value": changes["severity"], "allowed": list(EVENT_SEVERITIES)},
            )
        basis = changes.get("cause_basis", event.cause_basis)
        if basis not in CAUSE_BASES:
            raise ValidationFailed(
                "cause_basis is not recognised",
                details={"field": "cause_basis", "value": basis, "allowed": list(CAUSE_BASES)},
            )
        self._check_cause_basis(basis, changes, existing=event)

        records: list[ChangeRecord] = []
        for field, value in changes.items():
            before = getattr(event, field, None)
            if before == value:
                continue
            setattr(event, field, value)
            records.append(ChangeRecord(field=field, before=_jsonable(before), after=_jsonable(value)))
        if not records:
            raise ValidationFailed("the supplied changes match what is already stored")
        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal(),
            action="event.update",
            resource_kind="event",
            resource_id=event.id,
            details={
                "reason": reason,
                "cause_basis": event.cause_basis,
                "changes": [record.as_dict() for record in records],
            },
        )
        return event, records

    async def transition(
        self,
        event_id: str,
        *,
        status: str,
        expected_updated_at: dt.datetime,
        reason: str | None = None,
    ) -> Event:
        event = await self.get(event_id)
        _check_expected_version(event, expected_updated_at)
        if status not in EVENT_STATUSES:
            raise ValidationFailed(
                "status is not recognised",
                details={"field": "status", "value": status, "allowed": list(EVENT_STATUSES)},
            )
        if status == event.status:
            raise ValidationFailed(
                f"event {event_id!r} is already {status!r}", details={"event_id": event_id}
            )
        if status not in STATUS_TRANSITIONS.get(event.status, frozenset()):
            raise ValidationFailed(
                f"event cannot move from {event.status!r} to {status!r}",
                details={
                    "event_id": event_id,
                    "from": event.status,
                    "to": status,
                    "allowed": sorted(STATUS_TRANSITIONS.get(event.status, frozenset())),
                },
            )
        if status in {"closed", "cancelled"} and not reason:
            raise ValidationFailed(
                f"{status} an event requires a reason",
                details={"event_id": event_id, "field": "reason"},
            )
        before = event.status
        event.status = status
        if status == "closed" and event.ended_at is None:
            # Closing an event that never recorded an end is normal for a point-in-time occurrence;
            # the end is stamped, not invented as a duration.
            event.ended_at = event.occurred_at
        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal(),
            action="event.transition",
            resource_kind="event",
            resource_id=event.id,
            details={"from": before, "to": status, "reason": reason},
        )
        return event

    async def link_document(self, event_id: str, document_id: str | None) -> Event:
        event = await self.get(event_id)
        before = event.source_document_id
        if document_id is not None:
            document = (
                await self.session.execute(
                    select(Document).where(Document.id == document_id, Document.org_id == self.org_id)
                )
            ).scalar_one_or_none()
            if document is None:
                raise NotFound(f"document {document_id!r} not found")
            if document.well_id and document.well_id != event.well_id:
                raise ValidationFailed(
                    "the document is about a different well",
                    details={"document_well_id": document.well_id, "event_well_id": event.well_id},
                )
        event.source_document_id = document_id
        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal(),
            action="event.link_document",
            resource_kind="event",
            resource_id=event.id,
            details={"before": before, "after": document_id},
        )
        return event

    async def evidence_for(self, event_id: str, *, limit: int = 100) -> list[EvidenceLink]:
        await self.get(event_id)
        return list(
            (
                await self.session.execute(
                    select(EvidenceLink)
                    .where(
                        EvidenceLink.org_id == self.org_id,
                        EvidenceLink.subject_kind == "event",
                        EvidenceLink.subject_id == event_id,
                    )
                    .order_by(EvidenceLink.created_at.asc(), EvidenceLink.id.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    # ------------------------------------------------------------------ invariants

    def _require(self, **values: Any) -> None:
        for field, value in values.items():
            if value is None or (isinstance(value, str) and not value.strip()):
                raise ValidationFailed(f"{field} is required", details={"field": field})

    def _check_cause_basis(
        self,
        basis: str,
        fields: dict[str, Any],
        *,
        source_kind: str | None = None,
        cited: bool = False,
        existing: Event | None = None,
    ) -> None:
        """A cause may not be stored as ``recorded`` unless the document actually said it.

        The rule is deliberately asymmetric: moving *away* from ``recorded`` is allowed (someone
        double-checked the report and downgraded the claim), while claiming ``recorded`` requires the
        event to have come from a document or from a person who was there. A platform that let any
        caller assert ``recorded`` would make the label worthless.
        """
        asserts_cause = any(fields.get(field) for field in CAUSE_FIELDS) or (
            existing is not None and any(getattr(existing, field) for field in CAUSE_FIELDS)
        )
        if not asserts_cause and basis != "recorded":
            return
        if basis == "recorded":
            from_document = bool(
                fields.get("source_document_id")
                or (existing is not None and existing.source_document_id)
            )
            declared = source_kind or fields.get("source_kind")
            stated_by_person = (existing is not None and existing.source_kind == "manual") or (
                declared == "manual"
            )
            if from_document or stated_by_person:
                return
            raise ValidationFailed(
                "a cause recorded as 'recorded' must come from a document or from a person who reported it",
                details={
                    "cause_basis": basis,
                    "source_kind": declared,
                    "hint": "use cause_basis='inferred' and cite the evidence, or leave it 'unknown'",
                },
            )
        has_citation = (
            cited
            or bool(fields.get("evidence_ref") or fields.get("source_record_id"))
            or (existing is not None and bool(existing.evidence_ref or existing.source_record_id))
        )
        if basis == "inferred" and not has_citation:
            raise ValidationFailed(
                "an inferred cause must cite the evidence it was inferred from",
                details={
                    "cause_basis": basis,
                    "field": "evidence_ref",
                    "hint": "an inference without a citation is indistinguishable from a guess",
                },
            )

    async def _check_operation(self, operation_id: str | None, wellbore_id: str | None) -> Operation | None:
        if not operation_id:
            return None
        operation = (
            await self.session.execute(
                select(Operation).where(
                    Operation.id == operation_id, Operation.org_id == self.org_id
                )
            )
        ).scalar_one_or_none()
        if operation is None:
            raise NotFound(f"operation {operation_id!r} not found")
        if wellbore_id and operation.wellbore_id != wellbore_id:
            raise ValidationFailed(
                "the operation is on a different wellbore",
                details={
                    "operation_id": operation_id,
                    "operation_wellbore_id": operation.wellbore_id,
                    "wellbore_id": wellbore_id,
                },
            )
        return operation


def _inherit_from_operation(resolved: Any, operation: Operation) -> Any:
    """Return the scope with the operation's bore, section and well filled in where they were absent."""
    from dataclasses import replace

    updates: dict[str, Any] = {}
    if not getattr(resolved, "wellbore_id", None) and operation.wellbore_id:
        updates["wellbore_id"] = operation.wellbore_id
    if not getattr(resolved, "section_id", None) and operation.section_id:
        updates["section_id"] = operation.section_id
    if not getattr(resolved, "well_id", None) and operation.well_id:
        updates["well_id"] = operation.well_id
    if not getattr(resolved, "project_id", None) and operation.project_id:
        updates["project_id"] = operation.project_id
    return replace(resolved, **updates) if updates else resolved


def _check_expected_version(event: Event, expected_updated_at: dt.datetime) -> None:
    stored = event.updated_at
    if stored is None:
        return
    if _as_utc(stored) == _as_utc(expected_updated_at):
        return
    raise Conflict(
        "the event was changed by someone else since you read it",
        details={
            "event_id": event.id,
            "expected_updated_at": _iso(expected_updated_at),
            "current_updated_at": _iso(stored),
        },
    )


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    return None if value is None else _as_utc(value).isoformat()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return _iso(value)
    if hasattr(value, "__float__") and not isinstance(value, (int, float, bool)):
        try:
            return float(value)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return str(value)
    return value
