"""Resolving the asset hierarchy a document is attached to — once, and with the organization checked.

The invariant the resolver enforces
-----------------------------------

A document may name any subset of ``project``, ``well``, ``wellbore``, ``section``, ``operation``. For
the ones it names, all of these must hold:

* every referenced row belongs to the caller's organization, directly or transitively;
* every supplied parent agrees with the row it is supposed to contain — a ``well_id`` supplied beside
  a ``wellbore_id`` must be *that* wellbore's well;
* a reference that is omitted is derived only when the relationship makes it unambiguous, and the
  derivation is reported so a caller can see what it was given rather than what it asked for;
* a reference that disagrees is refused, never quietly corrected.

What was actually wrong before
------------------------------

The upload endpoint resolved this inline, and it lost the organization check on two of the five
lookups: ``wellbore_id`` and ``section_id`` were selected by primary key alone, while ``well_id`` was
scoped. The consequence was reproduced against the running application: organization *alpha* uploaded
a file naming organization *bravo*'s wellbore, received ``201``, and the response carried bravo's
``well_id`` back — a cross-tenant write performed by a read that was never authorized. The same
request with a ``section_id`` produced the same result. Neither is a hypothetical: both are the
literal first requests an attacker sends.

The second loss was quieter: nothing checked that a supplied ``well_id`` matched the supplied
``wellbore_id``. A caller could attach a document to well A *and* bravo's hole at the same time, and
the platform recorded both, producing a document whose scope is not a tree.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.errors import NotFound, ValidationFailed
from drillai.db.models import Operation, Project, Well, Wellbore, WellSection
from drillai.documents.access import operational_source_conditions
from drillai.documents.lifecycle import MIN_PROMOTION_CONFIDENCE, PROMOTABLE_VALIDATION_STATES

#: Field order used in every message, so two callers reporting the same disagreement describe it the
#: same way.
_SCOPE_FIELDS = ("project_id", "well_id", "wellbore_id", "section_id", "operation_id")


@dataclass(frozen=True)
class ResolvedScope:
    """The canonical scope, plus how each part of it was established.

    ``derived`` and ``supplied`` exist so the API can answer "what is this document attached to, and
    did the caller say so?" without re-deriving it. A client that sent only a ``section_id`` gets a
    document with a well and a project, and the response says which of them it never mentioned.
    """

    project_id: str | None = None
    well_id: str | None = None
    wellbore_id: str | None = None
    section_id: str | None = None
    operation_id: str | None = None

    #: Scope fields the caller supplied, in canonical order.
    supplied: tuple[str, ...] = ()
    #: Scope fields the resolver filled in from a relationship the caller did supply.
    derived: tuple[str, ...] = ()
    #: Human-readable joins, e.g. ``"wellbore_id → well_id"``. Serialised as scope provenance.
    chain: tuple[str, ...] = ()
    #: Non-fatal notes, e.g. a document attached to a wellbore with no well above it.
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, str | None]:
        return {
            "project_id": self.project_id,
            "well_id": self.well_id,
            "wellbore_id": self.wellbore_id,
            "section_id": self.section_id,
            "operation_id": self.operation_id,
        }


SOURCE_RECORD_STATES = PROMOTABLE_VALIDATION_STATES


def validate_source_record(
    record: Any,
    *,
    project_id: str | None,
    well_id: str | None,
    wellbore_id: str | None,
    section_id: str | None,
    operation_id: str | None,
) -> None:
    """Reject stale/rejected records before checking their hierarchy against the owner."""
    state = getattr(record, "validation_state", None)
    if state not in SOURCE_RECORD_STATES:
        raise ValidationFailed(
            f"source record validation state {state!r} is not eligible for linkage",
            details={"field": "source_record_id", "validation_state": state},
        )
    confidence = getattr(record, "confidence", None)
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0.0 <= float(confidence) <= 1.0
    ):
        raise ValidationFailed(
            "source record confidence must be finite and between 0 and 1",
            details={"field": "source_record_id", "confidence": confidence, "validation_state": state},
        )
    if state != "human_validated" and (
        confidence is None or float(confidence) < MIN_PROMOTION_CONFIDENCE
    ):
        raise ValidationFailed(
            "source record confidence is below the promotion threshold; human validation is required",
            details={
                "field": "source_record_id",
                "confidence": confidence,
                "minimum_confidence": MIN_PROMOTION_CONFIDENCE,
                "validation_state": state,
            },
        )
    validate_source_scope(
        record,
        project_id=project_id,
        well_id=well_id,
        wellbore_id=wellbore_id,
        section_id=section_id,
        operation_id=operation_id,
    )


def validate_source_scope(
    source: Any,
    *,
    project_id: str | None,
    well_id: str | None,
    wellbore_id: str | None,
    section_id: str | None,
    operation_id: str | None,
) -> None:
    """Require every non-null scope tag on a source to agree with its owning record.

    A broad source (for example a well-level report supporting an operation) is valid. A narrower or
    different source tag is not silently accepted, because it would make the link claim more than the
    source's hierarchy proves.
    """
    expected = {
        "project_id": project_id,
        "well_id": well_id,
        "wellbore_id": wellbore_id,
        "section_id": section_id,
        "operation_id": operation_id,
    }
    for field_name, expected_value in expected.items():
        actual_value = getattr(source, field_name, None)
        if actual_value is not None and actual_value != expected_value:
            raise ValidationFailed(
                f"source {field_name} does not match the owning record scope",
                details={
                    "field": field_name,
                    "source_value": actual_value,
                    "owner_value": expected_value,
                },
            )


@dataclass(frozen=True)
class DocumentScope:
    """What the resolver was handed. An empty string is treated as absent, not as a value to look up."""

    project_id: str | None = None
    well_id: str | None = None
    wellbore_id: str | None = None
    section_id: str | None = None
    operation_id: str | None = None

    def named(self) -> list[str]:
        return [name for name in _SCOPE_FIELDS if getattr(self, name)]

    def __bool__(self) -> bool:
        return bool(self.named())


class DocumentScopeResolver:
    """Resolve a document scope against one organization. One implementation, used by every writer.

    The resolver is deliberately a class over a session rather than a free function: resolution issues
    up to five queries, and every one of them is scoped by ``self.org_id`` so that "forgot the
    organization filter" is not a mistake that can be made by adding a line to the wrong query.

    **Derivation runs upward only.** A reference implies the references above it — a section implies its
    wellbore, a wellbore its well, a well its project — and never the reverse. Given only a well, the
    resolver leaves ``wellbore_id`` empty on purpose: a well may have several bores, and picking one
    would be inventing an attachment the caller never made. "This report is about the well" is a
    complete and legitimate scope, and the eight shapes the platform must answer for (project only,
    well only, well+wellbore, well+section, wellbore only, section only, operation only, and all of
    them together) therefore do not all end with the same number of fields filled in. A caller that
    wants the hole would send the hole.

    Every reference that *is* supplied must agree with every other one. Two that do not are refused
    with a 422 naming both sides, because the alternative — quietly preferring one — files the document
    somewhere the caller did not ask for while telling them it succeeded.
    """

    def __init__(self, session: AsyncSession, org_id: str) -> None:
        self.session = session
        self.org_id = org_id

    # ------------------------------------------------------------------ helpers

    async def _project(self, project_id: str) -> Project:
        row = (
            await self.session.execute(
                select(Project).where(Project.id == project_id, Project.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if row is None:
            # A referenced row in another organization is *not found* rather than *forbidden*: telling
            # the caller it exists would confirm the identifier, which is exactly what a guessed id is
            # trying to learn.
            raise NotFound(f"project {project_id!r} not found")
        return row

    async def _well(self, well_id: str) -> Well:
        row = (
            await self.session.execute(select(Well).where(Well.id == well_id, Well.org_id == self.org_id))
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"well {well_id!r} not found")
        return row

    async def _wellbore(self, wellbore_id: str) -> Wellbore:
        row = (
            await self.session.execute(
                select(Wellbore).where(Wellbore.id == wellbore_id, Wellbore.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"wellbore {wellbore_id!r} not found")
        return row

    async def _section(self, section_id: str) -> WellSection:
        row = (
            await self.session.execute(
                select(WellSection).where(WellSection.id == section_id, WellSection.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"section {section_id!r} not found")
        return row

    async def _operation(self, operation_id: str) -> Operation:
        row = (
            await self.session.execute(
                select(Operation).where(
                    Operation.id == operation_id,
                    Operation.org_id == self.org_id,
                    *operational_source_conditions(Operation),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"operation {operation_id!r} not found")
        return row

    # ------------------------------------------------------------------ resolution

    async def resolve(self, scope: DocumentScope | None = None, **named: str | None) -> ResolvedScope:
        """Turn the caller's references into a canonical scope, or refuse.

        Keyword form is accepted as well as a :class:`DocumentScope` so a router can pass its form
        fields straight through without building an intermediate object.
        """
        scope = scope or DocumentScope(**named)
        project_id, well_id = scope.project_id or None, scope.well_id or None
        wellbore_id, section_id, operation_id = (
            scope.wellbore_id or None,
            scope.section_id or None,
            scope.operation_id or None,
        )
        supplied = tuple(scope.named())
        chain: list[str] = []
        notes: list[str] = []

        # --- the deepest reference first: everything above it is derivable from it -------------
        operation: Operation | None = None
        if operation_id:
            operation = await self._operation(operation_id)
            if well_id and operation.well_id != well_id:
                raise ValidationFailed(
                    "operation_id does not belong to the supplied well_id",
                    details={
                        "operation_id": operation_id,
                        "operation_well_id": operation.well_id,
                        "well_id": well_id,
                    },
                )
            if wellbore_id and operation.wellbore_id != wellbore_id:
                raise ValidationFailed(
                    "operation_id does not belong to the supplied wellbore_id",
                    details={
                        "operation_id": operation_id,
                        "operation_wellbore_id": operation.wellbore_id,
                        "wellbore_id": wellbore_id,
                    },
                )
            if section_id and operation.section_id and operation.section_id != section_id:
                raise ValidationFailed(
                    "operation_id does not belong to the supplied section_id",
                    details={
                        "operation_id": operation_id,
                        "operation_section_id": operation.section_id,
                        "section_id": section_id,
                    },
                )
            if not well_id:
                well_id = operation.well_id
                chain.append("operation_id → well_id")
            if not wellbore_id:
                wellbore_id = operation.wellbore_id
                chain.append("operation_id → wellbore_id")
            if not section_id and operation.section_id:
                section_id = operation.section_id
                chain.append("operation_id → section_id")

        section: WellSection | None = None
        if section_id:
            section = await self._section(section_id)
            if wellbore_id and section.wellbore_id != wellbore_id:
                raise ValidationFailed(
                    "section_id does not belong to the supplied wellbore_id",
                    details={
                        "section_id": section_id,
                        "section_wellbore_id": section.wellbore_id,
                        "wellbore_id": wellbore_id,
                    },
                )
            if not wellbore_id:
                wellbore_id = section.wellbore_id
                chain.append("section_id → wellbore_id")

        wellbore: Wellbore | None = None
        if wellbore_id:
            wellbore = await self._wellbore(wellbore_id)
            # This is the check the inline version never made. It is the difference between a
            # document attached to a hole and a document attached to two different wells.
            if well_id and wellbore.well_id != well_id:
                raise ValidationFailed(
                    "wellbore_id does not belong to the supplied well_id",
                    details={
                        "wellbore_id": wellbore_id,
                        "wellbore_well_id": wellbore.well_id,
                        "well_id": well_id,
                    },
                )
            if not well_id:
                well_id = wellbore.well_id
                chain.append("wellbore_id → well_id")

        well: Well | None = None
        if well_id:
            well = await self._well(well_id)
            if well.project_id and not project_id:
                project_id = well.project_id
                chain.append("well_id → project_id")

        if project_id:
            project = await self._project(project_id)
            if well is not None and well.project_id and well.project_id != project.id:
                raise ValidationFailed(
                    "well_id does not belong to the supplied project_id",
                    details={
                        "well_id": well_id,
                        "well_project_id": well.project_id,
                        "project_id": project_id,
                    },
                )
            if wellbore is not None and well is None and wellbore.well_id:
                # A wellbore with no well above it cannot happen through the asset API, but the
                # resolver does not assume: if it did, the project check below would be skipped
                # silently, and a silently skipped check is how the original defect survived review.
                notes.append("wellbore has no well in this scope; project agreement not verified")

        if well is None and wellbore is not None and not project_id:
            notes.append("scope names a wellbore with no well and no project")

        resolved = {
            "project_id": project_id,
            "well_id": well_id,
            "wellbore_id": wellbore_id,
            "section_id": section_id,
            "operation_id": operation_id,
        }
        return ResolvedScope(
            project_id=project_id,
            well_id=well_id,
            wellbore_id=wellbore_id,
            section_id=section_id,
            operation_id=operation_id,
            supplied=supplied,
            # A field is *derived* when the caller did not name it and the relationships produced a
            # value anyway. This is computed from what was supplied rather than tracked by hand, so a
            # new scope field cannot be added to the resolver and forgotten here.
            derived=tuple(name for name in _SCOPE_FIELDS if name not in supplied and resolved[name]),
            chain=tuple(chain),
            notes=tuple(notes),
        )
