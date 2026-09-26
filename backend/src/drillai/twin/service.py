"""Digital Well Twin service.

Responsibilities, and deliberately nothing else:

* **write an aspect revision** in a given state kind — superseding the previous *current*
  revision and demoting it to *historical* (never overwriting; time travel is a read);
* **resolve current state** for a well/section, reconciling planned vs actual and recording
  which was chosen and why;
* **snapshot** the twin (a reproducible label such as "as-approved 2026-01-10" or
  "scenario: 12¼in mud weight +50");
* **record changes** with before/after and the actor, and compute their **impact** through the
  engineering dependency graph.

The service is storage-facing but engine-agnostic: engine results arrive as payloads produced
by ``drillai.engines``; the twin does not compute engineering values itself.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import utc_now
from drillai.core.errors import TwinStateError, ValidationFailed
from drillai.core.serialization import content_hash
from drillai.db.models import ChangeRecord, Scenario, TwinAspect, TwinSnapshot
from drillai.twin.aspects import ASPECTS_BY_KEY, AspectDefinition, StateKind, aspect

__all__ = [
    "AspectRevision",
    "ChangeEvent",
    "TwinService",
    "resolve_current",
]


@dataclass(frozen=True)
class ChangeEvent:
    """A change worth recording (and, later, analysing for impact)."""

    subject_kind: str
    subject_id: str
    change_type: str
    field_paths: list[str] = field(default_factory=list)
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None
    actor_kind: str = "system"
    actor_id: str | None = None
    source: str | None = None
    source_ref: str | None = None


@dataclass(frozen=True)
class AspectRevision:
    """A value to be written into the twin for one aspect/state kind."""

    aspect: str
    state_kind: StateKind
    payload: dict[str, Any]
    summary: str | None = None
    schema_version: int = 1
    confidence: float | None = None  # 0..1
    data_quality: str = "unknown"  # unknown | poor | fair | good | verified
    computed_by: str = "user"  # user | engine | workflow | agent | integration | import
    engine_run_id: str | None = None
    source_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    valid_from: dt.datetime | None = None
    valid_to: dt.datetime | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> AspectDefinition:
        definition = ASPECTS_BY_KEY.get(self.aspect)
        if definition is None:
            raise ValidationFailed(
                f"unknown twin aspect {self.aspect!r}",
                details={"aspect": self.aspect, "known": sorted(ASPECTS_BY_KEY)},
            )
        if not definition.allows(self.state_kind):
            raise TwinStateError(
                f"aspect {self.aspect!r} does not carry {StateKind(self.state_kind).value} state",
                details={"aspect": self.aspect, "state_kind": str(self.state_kind), "allowed": [str(k) for k in definition.state_kinds]},
            )
        if not isinstance(self.payload, dict) or not self.payload:
            raise ValidationFailed(f"aspect {self.aspect!r} needs a non-empty object payload")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValidationFailed("confidence must be within [0, 1]")
        return definition


class TwinService:
    """Reads and writes the digital well twin."""

    def __init__(self, session: AsyncSession, *, org_id: str) -> None:
        self.session = session
        self.org_id = org_id

    # ------------------------------------------------------------------ writes
    async def write_aspect(
        self,
        *,
        well_id: str,
        revision: AspectRevision,
        wellbore_id: str | None = None,
        section_id: str | None = None,
    ) -> TwinAspect:
        """Write an aspect revision, superseding the previous current revision.

        Only non-``historical`` state kinds are superseded: a plan and a measurement coexist, and
        a recommendation does not invalidate either. History is append-only.
        """
        definition = revision.validate()
        self._check_scope(definition, section_id)
        state_kind = StateKind(revision.state_kind)

        previous: TwinAspect | None = None
        if state_kind is not StateKind.HISTORICAL:
            previous = await self._current_entity(
                well_id=well_id,
                wellbore_id=wellbore_id,
                section_id=section_id,
                aspect_key=revision.aspect,
                state_kind=state_kind,
            )
            if previous is not None and previous.content_hash == content_hash(revision.payload):
                # Identical revision: keep one row per value, refresh provenance only.
                previous.computed_at = utc_now()
                previous.source_refs = sorted({*previous.source_refs, *revision.source_refs})
                previous.evidence_refs = sorted({*previous.evidence_refs, *revision.evidence_refs})
                await self.session.flush()
                return previous

        if previous is not None:
            previous.is_current = False
            previous.valid_to = previous.valid_to or utc_now()

        record = TwinAspect(
            org_id=self.org_id,
            well_id=well_id,
            wellbore_id=wellbore_id,
            section_id=section_id,
            aspect=revision.aspect,
            state_kind=state_kind.value,
            schema_key=definition.schema_key,
            schema_version=revision.schema_version,
            payload=revision.payload,
            summary=revision.summary,
            confidence=revision.confidence,
            data_quality=revision.data_quality,
            computed_at=utc_now(),
            computed_by=revision.computed_by,
            engine_run_id=revision.engine_run_id,
            source_refs=sorted(set(revision.source_refs)),
            evidence_refs=sorted(set(revision.evidence_refs)),
            assumptions=sorted(set(revision.assumptions)),
            valid_from=revision.valid_from,
            valid_to=revision.valid_to,
            supersedes_id=previous.id if previous else None,
            is_current=True,
            content_hash=content_hash(revision.payload),
            attributes=revision.attributes,
        )
        self.session.add(record)
        await self.session.flush()
        if previous is not None:
            previous.attributes = {**(previous.attributes or {}), "superseded_by": record.id}
        return record

    async def record_change(
        self,
        well_id: str,
        change: ChangeEvent,
        *,
        project_id: str | None = None,
        version: int | None = None,
        trace_id: str | None = None,
        workflow_run_id: str | None = None,
        approval_id: str | None = None,
    ) -> ChangeRecord:
        record = ChangeRecord(
            org_id=self.org_id,
            subject_kind=change.subject_kind,
            subject_id=change.subject_id,
            project_id=project_id,
            well_id=well_id,
            change_type=change.change_type,
            version=version,
            field_paths=change.field_paths,
            before=change.before,
            after=change.after,
            reason=change.reason,
            actor_kind=change.actor_kind,
            actor_id=change.actor_id,
            source=change.source,
            source_ref=change.source_ref,
            impact={},
            occurred_at=utc_now(),
            trace_id=trace_id,
            workflow_run_id=workflow_run_id,
            approval_id=approval_id,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def snapshot(
        self,
        *,
        well_id: str,
        label: str,
        kind: str = "manual",
        wellbore_id: str | None = None,
        aspect_keys: Sequence[str] | None = None,
        scenario_id: str | None = None,
        parent_snapshot_id: str | None = None,
        created_by: str | None = None,
        notes: str | None = None,
    ) -> TwinSnapshot:
        """Freeze the current twin state under a label (baseline, approval, scenario)."""
        aspects = await self.list_aspects(well_id=well_id, wellbore_id=wellbore_id, current_only=True)
        if aspect_keys is not None:
            wanted = set(aspect_keys)
            aspects = [item for item in aspects if item.aspect in wanted]
        if not aspects:
            raise TwinStateError("cannot snapshot an empty twin: no current aspects for this scope")

        summary: dict[str, Any] = {}
        for item in aspects:
            summary.setdefault(item.aspect, {})[item.state_kind] = {
                "id": item.id,
                "summary": item.summary,
                "data_quality": item.data_quality,
            }
        snapshot = TwinSnapshot(
            org_id=self.org_id,
            well_id=well_id,
            wellbore_id=wellbore_id,
            label=label,
            kind=kind,
            as_of=utc_now(),
            aspect_ids=[item.id for item in aspects],
            aspect_hashes={item.id: item.content_hash for item in aspects},
            summary=summary,
            scenario_id=scenario_id,
            parent_snapshot_id=parent_snapshot_id,
            created_by=created_by,
            twin_state=kind,
            completeness=self._completeness(aspects),
            notes=notes,
        )
        scenario = None
        if scenario_id:
            scenario = await self.session.get(Scenario, scenario_id)
            if scenario is None:
                raise ValidationFailed(f"scenario {scenario_id!r} not found")
        self.session.add(snapshot)
        await self.session.flush()
        for item in aspects:
            item.snapshot_id = snapshot.id
        if scenario is not None:
            scenario.snapshot_id = snapshot.id
        return snapshot

    # ------------------------------------------------------------------ reads
    async def _current_entity(
        self,
        *,
        well_id: str,
        wellbore_id: str | None,
        section_id: str | None,
        aspect_key: str,
        state_kind: StateKind,
    ) -> TwinAspect | None:
        stmt = (
            select(TwinAspect)
            .where(
                TwinAspect.org_id == self.org_id,
                TwinAspect.well_id == well_id,
                TwinAspect.aspect == aspect_key,
                TwinAspect.state_kind == state_kind.value,
                TwinAspect.is_current.is_(True),
            )
            .order_by(desc(TwinAspect.computed_at))
            .limit(1)
        )
        stmt = self._scope_filter(stmt, wellbore_id=wellbore_id, section_id=section_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def list_aspects(
        self,
        *,
        well_id: str,
        wellbore_id: str | None = None,
        section_id: str | None = None,
        aspect_key: str | None = None,
        state_kind: StateKind | None = None,
        current_only: bool = False,
        include_superseded: bool = False,
        limit: int = 500,
    ) -> list[TwinAspect]:
        stmt = select(TwinAspect).where(TwinAspect.org_id == self.org_id, TwinAspect.well_id == well_id)
        if aspect_key:
            stmt = stmt.where(TwinAspect.aspect == aspect_key)
        if state_kind:
            stmt = stmt.where(TwinAspect.state_kind == StateKind(state_kind).value)
        if current_only or not include_superseded:
            stmt = stmt.where(TwinAspect.is_current.is_(True))
        stmt = self._scope_filter(stmt, wellbore_id=wellbore_id, section_id=section_id)
        stmt = stmt.order_by(TwinAspect.aspect, TwinAspect.state_kind, desc(TwinAspect.computed_at)).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def current_state(
        self,
        *,
        well_id: str,
        wellbore_id: str | None = None,
        section_id: str | None = None,
    ) -> dict[str, dict[str, TwinAspect]]:
        """Return ``{aspect: {state_kind: revision}}`` for the current revisions of a scope."""
        aspects = await self.list_aspects(
            well_id=well_id, wellbore_id=wellbore_id, section_id=section_id, current_only=True
        )
        state: dict[str, dict[str, TwinAspect]] = {}
        for item in aspects:
            state.setdefault(item.aspect, {})[item.state_kind] = item
        return state

    async def history(
        self,
        *,
        well_id: str,
        aspect_key: str,
        state_kind: StateKind | None = None,
        limit: int = 100,
    ) -> list[TwinAspect]:
        """Full revision history, newest first — the time-travel read."""
        stmt = (
            select(TwinAspect)
            .where(TwinAspect.org_id == self.org_id, TwinAspect.well_id == well_id, TwinAspect.aspect == aspect_key)
            .order_by(desc(TwinAspect.computed_at))
            .limit(limit)
        )
        if state_kind:
            stmt = stmt.where(TwinAspect.state_kind == StateKind(state_kind).value)
        return list((await self.session.execute(stmt)).scalars().all())

    async def as_of(self, *, well_id: str, moment: dt.datetime, aspect_key: str | None = None) -> list[TwinAspect]:
        """Reconstruct the twin as it was at ``moment`` (revisions valid at that time)."""
        stmt = select(TwinAspect).where(
            TwinAspect.org_id == self.org_id,
            TwinAspect.well_id == well_id,
            TwinAspect.computed_at <= moment,
            (TwinAspect.valid_to.is_(None)) | (TwinAspect.valid_to > moment),
        )
        if aspect_key:
            stmt = stmt.where(TwinAspect.aspect == aspect_key)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_snapshots(self, *, well_id: str, limit: int = 50) -> list[TwinSnapshot]:
        stmt = (
            select(TwinSnapshot)
            .where(TwinSnapshot.org_id == self.org_id, TwinSnapshot.well_id == well_id)
            .order_by(desc(TwinSnapshot.as_of))
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def compare_snapshots(self, left_id: str, right_id: str) -> dict[str, Any]:
        """Structural diff between two snapshots, keyed by ``aspect/state_kind``.

        Keying by aspect (not by revision id) is what makes the diff readable: superseding a plan
        shows up as *changed*, not as one aspect added and another removed.
        """
        left = await self.session.get(TwinSnapshot, left_id)
        right = await self.session.get(TwinSnapshot, right_id)
        if left is None or right is None:
            raise ValidationFailed("both snapshots must exist to compare them")

        async def _keyed(snapshot: TwinSnapshot) -> dict[str, tuple[str, str | None, str | None]]:
            ids = list(snapshot.aspect_ids or [])
            if not ids:
                return {}
            rows = (await self.session.execute(select(TwinAspect).where(TwinAspect.id.in_(ids)))).scalars()
            keyed: dict[str, tuple[str, str | None, str | None]] = {}
            for row in rows:
                key = f"{row.aspect}/{row.state_kind}"
                keyed[key] = (row.id, (snapshot.aspect_hashes or {}).get(row.id), row.summary)
            return keyed

        left_keyed = await _keyed(left)
        right_keyed = await _keyed(right)
        changed: list[dict[str, Any]] = []
        unchanged: list[str] = []
        for key in sorted(left_keyed.keys() & right_keyed.keys()):
            left_revision = left_keyed[key]
            right_revision = right_keyed[key]
            if left_revision[1] != right_revision[1]:
                changed.append(
                    {
                        "aspect_state": key,
                        "left_summary": left_revision[2],
                        "right_summary": right_revision[2],
                    }
                )
            else:
                unchanged.append(key)
        return {
            "left": {"id": left.id, "label": left.label, "as_of": left.as_of.isoformat()},
            "right": {"id": right.id, "label": right.label, "as_of": right.as_of.isoformat()},
            "only_left": sorted(left_keyed.keys() - right_keyed.keys()),
            "only_right": sorted(right_keyed.keys() - left_keyed.keys()),
            "changed": changed,
            "unchanged": unchanged,
        }

    async def change_history(self, *, well_id: str, limit: int = 100) -> list[ChangeRecord]:
        stmt = (
            select(ChangeRecord)
            .where(ChangeRecord.org_id == self.org_id, ChangeRecord.well_id == well_id)
            .order_by(desc(ChangeRecord.occurred_at))
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _scope_filter(stmt, *, wellbore_id: str | None, section_id: str | None):
        if wellbore_id is not None:
            stmt = stmt.where(TwinAspect.wellbore_id == wellbore_id)
        if section_id is not None:
            stmt = stmt.where(TwinAspect.section_id == section_id)
        return stmt

    @staticmethod
    def _check_scope(definition: AspectDefinition, section_id: str | None) -> None:
        if definition.scope == "well" and section_id is not None:
            raise ValidationFailed(
                f"aspect {definition.key!r} is well-scoped and cannot be written for a single section",
                details={"aspect": definition.key, "section_id": section_id},
            )

    @staticmethod
    def _completeness(aspects: Iterable[TwinAspect]) -> float:
        present = {item.aspect for item in aspects}
        return round(len(present) / len(ASPECTS_BY_KEY), 4)


def resolve_current(
    state: dict[str, dict[str, TwinAspect]],
    aspect_key: str,
) -> dict[str, Any]:
    """Resolve an aspect's current value, preferring measured reality over the plan.

    Returns a dict with ``value``, ``source_state_kind`` and a short ``rationale`` so that a UI
    or an LLM prompt can explain *which* value it is looking at — a twin must never present a
    plan as if it were a measurement without saying so.
    """
    revisions = state.get(aspect_key) or {}
    if not revisions:
        return {"value": None, "source_state_kind": None, "rationale": f"no twin data for {aspect_key}"}
    actual = revisions.get(StateKind.ACTUAL.value) or revisions.get(StateKind.CURRENT.value)
    planned = revisions.get(StateKind.PLANNED.value)
    if actual is not None and planned is not None:
        same = actual.content_hash == planned.content_hash
        return {
            "value": actual.payload,
            "source_state_kind": actual.state_kind,
            "planned": planned.payload,
            "actual_matches_plan": same,
            "rationale": (
                f"using {aspect(aspect_key).name} as-measured; the plan "
                + ("matches" if same else "differs from")
                + " the measurement"
            ),
        }
    chosen = actual or planned
    assert chosen is not None  # both were None → fell through to the empty case above
    return {
        "value": chosen.payload,
        "source_state_kind": chosen.state_kind,
        "rationale": f"only {chosen.state_kind} data exists for {aspect(aspect_key).name}",
    }
