"""Operations as first-class records: creation, allowed updates, transitions, sequencing, links.

Why a service and not router code
---------------------------------

Every write here has to do five things in one transaction: authorize against an action, check the
record is in a state that permits the change, write the row, record the audit entry, and (on a
correction) prove the caller was looking at the version they meant to change. A router that did four of
them would look identical in a code review to one that did five, which is how the platform ends up with
mutations that are not audited. The rules live here once, and the router is a thin caller.

What "correct" means for an operation
-------------------------------------

* **A planned operation is not an actual one.** ``operation_class`` is checked, not inferred from
  whether dates are present, and a planned row may not carry an ``actual_start`` — the platform must
  never let a plan be rewritten as history, because every depth and NPT number downstream asks
  "was this done?" and would get a different answer depending on which row it read.
* **Sequence is an invariant, not a suggestion.** Predecessors must exist, belong to the same wellbore,
  and not chain into cycle. A cycle would make any "operations before this one" question unanswerable,
  so it is refused at write time rather than tolerated.
* **Nothing is deleted.** An operation that did not happen is ``cancelled`` with a reason; the row is
  the evidence that it was once planned.
* **A correction carries the version it corrects.** ``expected_updated_at`` is required for a mutation
  of an existing row, so two people editing the same operation cannot silently overwrite each other.
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
    OPERATION_CLASSES,
    OPERATION_KINDS,
    OPERATION_PHASES,
    OPERATION_STATUSES,
    SOURCE_KINDS,
    Document,
    Event,
    EvidenceLink,
    Operation,
)
from drillai.documents.scope import DocumentScope, DocumentScopeResolver
from drillai.security.actions import Principal

#: Statuses from which no further work happens. A row here is history.
TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "cancelled"})

#: Allowed status moves. ``cancelled`` is reachable from anything that has started, because work that
#: has begun can still be abandoned — and abandoning it is exactly the case that has to be recorded.
STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "planned": frozenset({"ready", "in_progress", "cancelled"}),
    "ready": frozenset({"in_progress", "cancelled"}),
    "in_progress": frozenset({"completed", "suspended", "cancelled"}),
    "suspended": frozenset({"in_progress", "cancelled"}),
    "completed": frozenset(),
    "cancelled": frozenset(),
}

#: Fields a caller may change on an existing operation, and whether the change needs a reason.
#: ``operation_class`` is absent on purpose: promoting a plan to actual is a *transition* with its own
#: endpoint and its own audit action, not a field edit.
MUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "name",
        "code",
        "kind",
        "phase",
        "planned_start",
        "planned_end",
        "planned_duration_hours",
        "actual_start",
        "actual_end",
        "actual_duration_hours",
        "depth_from_md_si",
        "depth_to_md_si",
        "hole_diameter_si",
        "npt_hours",
        "invisible_lost_time_hours",
        "cost_usd",
        "remarks",
        "section_id",
        "is_productive",
    }
)


@dataclass(frozen=True)
class ChangeRecord:
    """One field's before and after, so an audit entry names what moved rather than that it moved."""

    field: str
    before: Any
    after: Any

    def as_dict(self) -> dict[str, Any]:
        return {"field": self.field, "before": self.before, "after": self.after}


def _status_transition_error(operation: Operation, target: str) -> ValidationFailed:
    return ValidationFailed(
        f"operation cannot move from {operation.status!r} to {target!r}",
        details={
            "operation_id": operation.id,
            "from": operation.status,
            "to": target,
            "allowed": sorted(STATUS_TRANSITIONS.get(operation.status, frozenset())),
        },
    )


class OperationService:
    """Reads and writes for ``operations``. Every method is organization-scoped."""

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
        section_id: str | None = None,
        operation_class: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        source_kind: str | None = None,
        document_id: str | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
    ) -> Select:
        """The filtered statement, exposed so callers can count and page it without re-deriving it.

        Ordering is by ``(sequence, id)``: sequence is the chain the well was drilled in, and the id
        breaks ties so that pagination is stable when two rows share a sequence. Ordering by
        ``created_at`` — the obvious choice — is *not* the drilling order, and a timeline that
        reorders itself between pages is worse than one that is merely slow.
        """
        stmt = select(Operation).where(Operation.org_id == self.org_id)
        if well_id:
            stmt = stmt.where(Operation.well_id == well_id)
        if wellbore_id:
            stmt = stmt.where(Operation.wellbore_id == wellbore_id)
        if section_id:
            stmt = stmt.where(Operation.section_id == section_id)
        if operation_class:
            stmt = stmt.where(Operation.operation_class == operation_class)
        if status:
            stmt = stmt.where(Operation.status == status)
        if kind:
            stmt = stmt.where(Operation.kind == kind)
        if source_kind:
            stmt = stmt.where(Operation.source_kind == source_kind)
        if document_id:
            stmt = stmt.where(Operation.source_document_id == document_id)
        if since is not None:
            stmt = stmt.where(
                or_(Operation.actual_start >= since, Operation.planned_start >= since)
            )
        if until is not None:
            stmt = stmt.where(
                or_(Operation.actual_end <= until, Operation.planned_end <= until)
            )
        return stmt.order_by(Operation.sequence.asc(), Operation.id.asc())

    async def count(self, stmt: Select) -> int:
        return (await self.session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    async def get(self, operation_id: str) -> Operation:
        row = (
            await self.session.execute(
                select(Operation).where(
                    Operation.id == operation_id, Operation.org_id == self.org_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"operation {operation_id!r} not found")
        return row

    async def next_sequence(self, wellbore_id: str) -> int:
        """One past the highest sequence on this bore.

        Read inside the same transaction as the insert that uses it. The unique index on
        ``(org_id, wellbore_id, sequence)`` is what makes a concurrent double-insert fail rather than
        produce two operations claiming the same position.
        """
        highest = (
            await self.session.execute(
                select(func.max(Operation.sequence)).where(
                    Operation.org_id == self.org_id, Operation.wellbore_id == wellbore_id
                )
            )
        ).scalar_one()
        return int(highest or 0) + 1

    # ------------------------------------------------------------------ writes

    async def create(
        self,
        *,
        scope: DocumentScope,
        name: str,
        operation_class: str = "actual",
        kind: str = "drilling",
        phase: str | None = None,
        status: str | None = None,
        sequence: int | None = None,
        predecessor_operation_id: str | None = None,
        parent_operation_id: str | None = None,
        source_kind: str = "manual",
        source_document_id: str | None = None,
        source_record_id: str | None = None,
        **fields: Any,
    ) -> Operation:
        """Create one operation, after checking its scope, its class and its position in the chain."""
        self._require(name=name)
        if operation_class not in OPERATION_CLASSES:
            raise ValidationFailed(
                "operation_class is not recognised",
                details={"field": "operation_class", "value": operation_class, "allowed": list(OPERATION_CLASSES)},
            )
        if kind not in OPERATION_KINDS:
            raise ValidationFailed(
                "kind is not a recognised operation kind",
                details={"field": "kind", "value": kind, "allowed": sorted(OPERATION_KINDS)},
            )
        if source_kind not in SOURCE_KINDS:
            raise ValidationFailed(
                "source_kind is not recognised",
                details={"field": "source_kind", "value": source_kind, "allowed": list(SOURCE_KINDS)},
            )
        if phase is not None and phase not in OPERATION_PHASES:
            raise ValidationFailed(
                "phase is not recognised",
                details={"field": "phase", "value": phase, "allowed": list(OPERATION_PHASES)},
            )
        target_status = status or self._default_status(operation_class)
        if target_status not in OPERATION_STATUSES:
            raise ValidationFailed(
                "status is not recognised",
                details={"field": "status", "value": target_status, "allowed": list(OPERATION_STATUSES)},
            )
        self._check_class_fields(operation_class, fields)

        resolved = await DocumentScopeResolver(self.session, self.org_id).resolve(scope)
        if not resolved.wellbore_id:
            # An operation always happens in a hole. Refusing here rather than storing a null bore is
            # what keeps "which operations belong to this section" answerable for every row.
            raise ValidationFailed(
                "an operation must name a wellbore",
                details={"scope": resolved.as_dict(), "derived": list(resolved.derived)},
            )
        wellbore_id = resolved.wellbore_id
        well_id = resolved.well_id or ""
        if not well_id:
            raise ValidationFailed("an operation must resolve to a well", details={"scope": resolved.as_dict()})
        await self._check_scope_agreement(resolved.well_id, resolved.wellbore_id, resolved.section_id)

        if sequence is None:
            sequence = await self.next_sequence(wellbore_id)
        await self._check_predecessor(predecessor_operation_id, wellbore_id)
        await self._check_parent(parent_operation_id, wellbore_id)
        await self._check_linkage(source_document_id=source_document_id, source_record_id=source_record_id)

        operation = Operation(
            id=new_id("opr"),
            org_id=self.org_id,
            project_id=resolved.project_id,
            well_id=well_id,
            wellbore_id=wellbore_id,
            section_id=resolved.section_id,
            parent_operation_id=parent_operation_id,
            predecessor_operation_id=predecessor_operation_id,
            operation_class=operation_class,
            sequence=sequence,
            name=name,
            kind=kind,
            phase=phase or self._default_phase(target_status),
            status=target_status,
            source_kind=source_kind,
            source="manual" if source_kind == "manual" else source_kind,
            source_document_id=source_document_id,
            source_record_id=source_record_id,
            **fields,
        )
        self.session.add(operation)
        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal(),
            action="operation.create",
            resource_kind="operation",
            resource_id=operation.id,
            details={
                "well_id": well_id,
                "wellbore_id": wellbore_id,
                "sequence": operation.sequence,
                "operation_class": operation_class,
                "source_kind": source_kind,
                "scope_derived": list(resolved.derived),
            },
        )
        return operation

    async def update(
        self,
        operation_id: str,
        *,
        expected_updated_at: dt.datetime,
        reason: str,
        changes: dict[str, Any],
    ) -> tuple[Operation, list[ChangeRecord]]:
        """Apply an allowed change to an existing operation, with the version and a reason."""
        self._require(reason=reason)
        operation = await self.get(operation_id)
        self._check_expected_version(operation, expected_updated_at)
        if operation.status == "cancelled":
            # A cancelled operation is closed history: it was abandoned, and revising its numbers
            # afterwards would make the reason for abandoning it unreadable. A *completed* operation,
            # by contrast, is exactly where transcription mistakes land, and correcting one is what
            # this method is for — the version check, the mandatory reason and the before/after ledger
            # entry are what make the correction accountable rather than a silent overwrite.
            raise Conflict(
                f"operation {operation_id!r} was cancelled and cannot be edited",
                details={"operation_id": operation_id, "status": operation.status},
            )
        unknown = set(changes) - MUTABLE_FIELDS
        if unknown:
            raise ValidationFailed(
                "one or more fields cannot be changed",
                details={
                    "fields": sorted(unknown),
                    "allowed": sorted(MUTABLE_FIELDS),
                    "owned_by": {
                        field: "the transition endpoint" if field == "status" else "the record's class"
                        for field in unknown
                        if field in {"status", "operation_class"}
                    },
                },
            )
        if not changes:
            raise ValidationFailed("no changes were supplied")
        if "kind" in changes and changes["kind"] not in OPERATION_KINDS:
            raise ValidationFailed(
                "kind is not a recognised operation kind",
                details={"field": "kind", "value": changes["kind"], "allowed": sorted(OPERATION_KINDS)},
            )
        if changes.get("section_id"):
            await self._check_section_belongs(changes["section_id"], operation.wellbore_id)

        records: list[ChangeRecord] = []
        for field, value in changes.items():
            before = getattr(operation, field)
            if before == value:
                continue
            setattr(operation, field, value)
            records.append(ChangeRecord(field=field, before=_jsonable(before), after=_jsonable(value)))
        if not records:
            raise ValidationFailed("the supplied changes match what is already stored")
        self._check_class_fields(operation.operation_class, {record.field: record.after for record in records})

        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal(),
            action="operation.update",
            resource_kind="operation",
            resource_id=operation.id,
            details={"reason": reason, "changes": [record.as_dict() for record in records]},
        )
        return operation, records

    async def transition(
        self,
        operation_id: str,
        *,
        status: str,
        expected_updated_at: dt.datetime,
        reason: str | None = None,
    ) -> Operation:
        """Move an operation to another status, if the move is allowed."""
        operation = await self.get(operation_id)
        self._check_expected_version(operation, expected_updated_at)
        if status not in OPERATION_STATUSES:
            raise ValidationFailed(
                "status is not recognised",
                details={"field": "status", "value": status, "allowed": list(OPERATION_STATUSES)},
            )
        if status == operation.status:
            raise ValidationFailed(
                f"operation {operation_id!r} is already {status!r}",
                details={"operation_id": operation_id, "status": status},
            )
        if status not in STATUS_TRANSITIONS.get(operation.status, frozenset()):
            raise _status_transition_error(operation, status)
        before = operation.status
        operation.status = status
        operation.phase = self._default_phase(status)
        if status == "cancelled" and not reason:
            # Cancelling work is a decision someone made; recording it without a reason loses the why.
            raise ValidationFailed(
                "cancelling an operation requires a reason",
                details={"operation_id": operation_id, "field": "reason"},
            )
        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal(),
            action="operation.transition",
            resource_kind="operation",
            resource_id=operation.id,
            details={"from": before, "to": status, "reason": reason},
        )
        return operation

    async def link_predecessor(
        self, operation_id: str, predecessor_operation_id: str | None
    ) -> Operation:
        """Set or clear the operation this one follows, refusing anything that could form a cycle."""
        operation = await self.get(operation_id)
        if predecessor_operation_id == operation_id:
            raise ValidationFailed(
                "an operation cannot follow itself", details={"operation_id": operation_id}
            )
        await self._check_predecessor(predecessor_operation_id, operation.wellbore_id)
        if predecessor_operation_id:
            await self._check_no_cycle(operation_id, predecessor_operation_id)
        before = operation.predecessor_operation_id
        operation.predecessor_operation_id = predecessor_operation_id
        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal(),
            action="operation.link_predecessor",
            resource_kind="operation",
            resource_id=operation.id,
            details={"before": before, "after": predecessor_operation_id},
        )
        return operation

    async def link_document(self, operation_id: str, document_id: str | None) -> Operation:
        """Attach (or detach) the document an operation was read from.

        The document must belong to the caller's organization *and* to the operation's well: a citation
        to another well's report is the sort of link that makes an audit trail worse than none, because
        it looks authoritative and is wrong.
        """
        operation = await self.get(operation_id)
        before = operation.source_document_id
        if document_id is not None:
            document = (
                await self.session.execute(
                    select(Document).where(
                        Document.id == document_id, Document.org_id == self.org_id
                    )
                )
            ).scalar_one_or_none()
            if document is None:
                raise NotFound(f"document {document_id!r} not found")
            if document.well_id and document.well_id != operation.well_id:
                raise ValidationFailed(
                    "the document is about a different well",
                    details={
                        "document_id": document_id,
                        "document_well_id": document.well_id,
                        "operation_well_id": operation.well_id,
                    },
                )
        operation.source_document_id = document_id
        await self.session.flush()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal(),
            action="operation.link_document",
            resource_kind="operation",
            resource_id=operation.id,
            details={"before": before, "after": document_id},
        )
        return operation

    async def events_for(self, operation_id: str, *, limit: int = 100) -> list[Event]:
        await self.get(operation_id)
        return list(
            (
                await self.session.execute(
                    select(Event)
                    .where(Event.org_id == self.org_id, Event.operation_id == operation_id)
                    .order_by(Event.occurred_at.asc(), Event.id.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def evidence_for(self, operation_id: str, *, limit: int = 100) -> list[EvidenceLink]:
        await self.get(operation_id)
        return list(
            (
                await self.session.execute(
                    select(EvidenceLink)
                    .where(
                        EvidenceLink.org_id == self.org_id,
                        EvidenceLink.subject_kind == "operation",
                        EvidenceLink.subject_id == operation_id,
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

    def _default_status(self, operation_class: str) -> str:
        return "planned" if operation_class in {"plan", "forecast"} else "completed"

    def _default_phase(self, status: str) -> str:
        return {
            "planned": "planning",
            "ready": "preparation",
            "in_progress": "executing",
            "completed": "completed",
            "suspended": "suspended",
            "cancelled": "cancelled",
        }[status]

    def _check_class_fields(self, operation_class: str, fields: dict[str, Any]) -> None:
        """A plan may not carry actuals, and an actual may not carry a plan's window.

        The rule is asymmetric on purpose: ``planned_*`` on an actual is useful (it is the plan the
        operation was executed against, and the twin's plan-vs-actual view needs it), while
        ``actual_*`` on a plan is a claim that something happened, which is exactly what must not be
        representable.
        """
        if operation_class not in {"plan", "forecast"}:
            return
        for field, value in fields.items():
            if field.startswith("actual_") and value is not None:
                raise ValidationFailed(
                    f"a {operation_class} may not carry {field}",
                    details={
                        "field": field,
                        "operation_class": operation_class,
                        "hint": "record execution as a separate actual operation and link it with predecessor_operation_id",
                    },
                )

    async def _check_predecessor(self, predecessor_id: str | None, wellbore_id: str) -> None:
        if not predecessor_id:
            return
        row = (
            await self.session.execute(
                select(Operation).where(
                    Operation.id == predecessor_id, Operation.org_id == self.org_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"predecessor operation {predecessor_id!r} not found")
        if row.wellbore_id != wellbore_id:
            raise ValidationFailed(
                "the predecessor operation is on a different wellbore",
                details={
                    "predecessor_operation_id": predecessor_id,
                    "predecessor_wellbore_id": row.wellbore_id,
                    "wellbore_id": wellbore_id,
                },
            )

    async def _check_parent(self, parent_id: str | None, wellbore_id: str) -> None:
        if not parent_id:
            return
        row = (
            await self.session.execute(
                select(Operation).where(Operation.id == parent_id, Operation.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"parent operation {parent_id!r} not found")
        if row.wellbore_id != wellbore_id:
            raise ValidationFailed(
                "the parent operation is on a different wellbore",
                details={"parent_operation_id": parent_id, "parent_wellbore_id": row.wellbore_id},
            )

    async def _check_no_cycle(self, operation_id: str, predecessor_id: str) -> None:
        """Walk the predecessor chain from the new link; if it reaches this operation, it is a cycle."""
        seen: set[str] = set()
        current: str | None = predecessor_id
        while current is not None:
            if current == operation_id:
                raise ValidationFailed(
                    "the predecessor chain would form a cycle",
                    details={"operation_id": operation_id, "chain_reached": current},
                )
            if current in seen:
                # An existing cycle, written before this check existed. Refusing to join it is the
                # only safe answer: the alternative is extending it.
                raise ValidationFailed(
                    "the predecessor chain already contains a cycle",
                    details={"operation_id": operation_id, "repeated_at": current},
                )
            seen.add(current)
            current = (
                await self.session.execute(
                    select(Operation.predecessor_operation_id).where(
                        Operation.id == current, Operation.org_id == self.org_id
                    )
                )
            ).scalar_one_or_none()

    async def _check_scope_agreement(
        self, well_id: str | None, wellbore_id: str | None, section_id: str | None
    ) -> None:
        if section_id and wellbore_id:
            await self._check_section_belongs(section_id, wellbore_id)

    async def _check_section_belongs(self, section_id: str, wellbore_id: str) -> None:
        from drillai.db.models import WellSection

        row = (
            await self.session.execute(
                select(WellSection).where(
                    WellSection.id == section_id, WellSection.org_id == self.org_id
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"section {section_id!r} not found")
        if row.wellbore_id != wellbore_id:
            raise ValidationFailed(
                "section_id does not belong to the operation's wellbore",
                details={"section_id": section_id, "section_wellbore_id": row.wellbore_id},
            )

    async def _check_linkage(
        self, *, source_document_id: str | None, source_record_id: str | None
    ) -> None:
        if source_document_id:
            document = (
                await self.session.execute(
                    select(Document).where(
                        Document.id == source_document_id, Document.org_id == self.org_id
                    )
                )
            ).scalar_one_or_none()
            if document is None:
                raise NotFound(f"document {source_document_id!r} not found")
        elif source_record_id:
            raise ValidationFailed(
                "source_record_id requires the source_document_id it belongs to",
                details={"field": "source_document_id"},
            )

    def _check_expected_version(self, operation: Operation, expected_updated_at: dt.datetime) -> None:
        stored = operation.updated_at
        if stored is None:
            return
        if _same_instant(stored, expected_updated_at):
            return
        raise Conflict(
            "the operation was changed by someone else since you read it",
            details={
                "operation_id": operation.id,
                "expected_updated_at": _iso(expected_updated_at),
                "current_updated_at": _iso(stored),
            },
        )


def _same_instant(left: dt.datetime, right: dt.datetime) -> bool:
    """Compare two timestamps as instants, tolerating naive/aware differences.

    SQLite hands back naive datetimes and PostgreSQL hands back aware ones. Comparing them directly
    raises ``TypeError``, and comparing them *loosely* (by string) would accept a stale version — so
    naive values are read as UTC, which is what the column stores.
    """
    return _as_utc(left) == _as_utc(right)


def _as_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return _as_utc(value).isoformat()


def _jsonable(value: Any) -> Any:
    """Numbers and dates as the audit entry can store them, without losing precision."""
    if isinstance(value, dt.datetime):
        return _iso(value)
    if hasattr(value, "__float__") and not isinstance(value, (int, float, bool)):
        try:
            return float(value)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return str(value)
    return value
