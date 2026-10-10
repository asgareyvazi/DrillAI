"""One well timeline, assembled from every recorded stream.

The well timeline is not a new store: it is a *view* that merges the streams the platform already
persists, each entry keeping its own kind, its own id and — where one exists — its evidence
reference, so clicking an entry leads back to the row that produced it.

Streams merged, in a fixed order so the response is stable:

============================  ==========================================================
operations                    planned/actual operations with durations and depth
events                        incidents, NPT, hazards, milestones
documents                     ingestion (what arrived, when, how well it extracted)
engine runs                   deterministic calculations with engine key *and version*
recommendations               proposed engineering actions and their decision state
twin changes                  what changed in the digital twin and why
approvals                     human decisions on L3+ actions
============================  ==========================================================

Ordering is by ``at`` (start of the entry) then by a deterministic tiebreak, so the same data
always renders the same timeline.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.errors import NotFound
from drillai.db.models import (
    ApprovalRequest,
    ChangeRecord,
    Document,
    EngineRun,
    Event,
    Operation,
    Recommendation,
    Well,
)


def sort_key(entry: Any) -> tuple[bool, dt.datetime, str, str]:
    """The merged order, written once and used for both sorting and the cursor.

    Entries with no time sort last: an undated document is not older than 1970, it is simply undated,
    and placing it at the start of a well's history would be a claim the data does not make.
    """
    return (
        entry.at is None,
        entry.at or dt.datetime.min.replace(tzinfo=dt.UTC),
        entry.kind,
        entry.id,
    )


def encode_cursor(entry: Any) -> str:
    """The keyset position of ``entry``: its ordering key, in a form a client can pass back."""
    payload = {
        "at": entry.at.isoformat() if entry.at else None,
        "kind": entry.kind,
        "id": entry.id,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def parse_cursor(token: str) -> tuple[dt.datetime | None, str, str]:
    """Decode a cursor, refusing anything that is not one rather than starting from the beginning."""
    from drillai.core.errors import ValidationFailed

    try:
        padded = token + "=" * (-len(token) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        at = payload["at"]
        kind = payload["kind"]
        identifier = payload["id"]
    except Exception as exc:
        raise ValidationFailed(
            "cursor is not a timeline cursor",
            details={"hint": "pass back the next_cursor from the previous page unchanged"},
        ) from exc
    if not isinstance(kind, str) or not isinstance(identifier, str) or kind not in TIMELINE_KINDS:
        raise ValidationFailed(
            "cursor names a timeline kind that does not exist",
            details={"known": list(TIMELINE_KINDS)},
        )
    if at is not None and not isinstance(at, str):
        raise ValidationFailed("cursor carries a malformed timestamp")
    moment = dt.datetime.fromisoformat(at) if at else None
    if moment is not None and moment.tzinfo is None:
        raise ValidationFailed("cursor carries a timestamp without a timezone")
    return moment, kind, identifier


def _after_condition(
    at_column: Any, id_column: Any, kind: str, after: tuple[dt.datetime | None, str, str] | None
) -> Any | None:
    """SQL for "this row ranks after the cursor", within one stream.

    Each stream has a single kind, so the ``(kind, id)`` half of the ordering key collapses to
    constants here — which is what keeps the comparison in SQL instead of in a loaded list.
    """
    from sqlalchemy import and_, false, or_

    if after is None:
        return None
    at, after_kind, after_id = after
    if at is None:
        # The cursor is already in the undated tail; only later undated rows of later streams remain.
        if kind > after_kind:
            return at_column.is_(None)
        if kind < after_kind:
            return false()
        return and_(at_column.is_(None), id_column > after_id)
    clause = at_column > at
    if kind > after_kind:
        # Undated rows of a later stream, and same-instant rows, both rank after the cursor.
        clause = or_(clause, at_column.is_(None), at_column == at)
    elif kind == after_kind:
        clause = or_(clause, and_(at_column == at, id_column > after_id))
    return clause


TIMELINE_KINDS = (
    "operation",
    "event",
    "document",
    "engine_run",
    "recommendation",
    "twin_change",
    "approval",
)


@dataclass
class TimelineEntry:
    kind: str
    id: str
    at: dt.datetime | None
    end_at: dt.datetime | None
    title: str
    summary: str | None = None
    status: str | None = None
    category: str | None = None
    operation_id: str | None = None
    section_id: str | None = None
    depth_md_si: float | None = None
    duration_hours: float | None = None
    severity: str | None = None
    npt: bool = False
    npt_hours: float | None = None
    evidence_refs: list[str] = field(default_factory=list)
    document_id: str | None = None
    links: dict[str, str] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "at": self.at.isoformat() if self.at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "title": self.title,
            "summary": self.summary,
            "status": self.status,
            "category": self.category,
            "operation_id": self.operation_id,
            "section_id": self.section_id,
            "depth_md_si": self.depth_md_si,
            "duration_hours": self.duration_hours,
            "severity": self.severity,
            "npt": self.npt,
            "npt_hours": self.npt_hours,
            "evidence_refs": self.evidence_refs,
            "document_id": self.document_id,
            "links": self.links,
            "attributes": self.attributes,
        }


class TimelineService:
    def __init__(self, session: AsyncSession, org_id: str) -> None:
        self.session = session
        self.org_id = org_id

    async def _stream(
        self,
        model: Any,
        at_column: Any,
        kind: str,
        *,
        well_id: str,
        since: dt.datetime | None,
        until: dt.datetime | None,
        after: tuple[dt.datetime | None, str, str] | None,
        limit: int,
    ) -> list[Any]:
        """One stream, filtered, ordered and truncated in SQL.

        The time window, the keyset position and the page size are all in the query. The previous
        version loaded every row of every stream for the well and then filtered, sorted and cut the
        list in Python: a page of 500 meant reading the well's whole history, and the cost of a
        timeline request grew with the well rather than with the page.
        """
        stmt = select(model).where(model.org_id == self.org_id, model.well_id == well_id)
        if since is not None:
            # An undated row is not "since" anything: the previous behaviour dropped it when a window
            # was given, and dropping it in SQL keeps the same answer.
            stmt = stmt.where(at_column.is_not(None), at_column >= since)
        if until is not None:
            stmt = stmt.where(at_column.is_not(None), at_column <= until)
        condition = _after_condition(at_column, model.id, kind, after)
        if condition is not None:
            stmt = stmt.where(condition)
        stmt = stmt.order_by(at_column.is_(None), at_column.asc(), model.id.asc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def build(
        self,
        well_id: str,
        *,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        kinds: list[str] | None = None,
        limit: int = 500,
        after: tuple[dt.datetime | None, str, str] | None = None,
    ) -> list[TimelineEntry]:
        """The merged timeline, one page at a time.

        Each stream contributes at most ``limit`` rows — enough for a global top-``limit``, because a
        row beyond the first ``limit`` of its own stream is already preceded, inside that stream, by
        ``limit`` rows that outrank it.
        """
        exists = (
            await self.session.execute(
                select(Well.id).where(Well.id == well_id, Well.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if exists is None:
            # An empty timeline is a statement about a well that exists and has nothing on it. For a
            # well this caller cannot see, the honest answer is that it is not here.
            raise NotFound("well not found", details={"well_id": well_id})
        wanted = set(kinds or TIMELINE_KINDS)
        entries: list[TimelineEntry] = []
        stream = lambda model, column, kind: self._stream(  # noqa: E731 - local shorthand
            model,
            column,
            kind,
            well_id=well_id,
            since=since,
            until=until,
            after=after,
            limit=limit,
        )

        if "operation" in wanted:
            rows = await stream(
                Operation,
                func.coalesce(Operation.actual_start, Operation.planned_start),
                "operation",
            )
            for row in rows:
                started = row.actual_start or row.planned_start
                entries.append(
                    TimelineEntry(
                        kind="operation",
                        id=row.id,
                        at=started,
                        end_at=row.actual_end or row.planned_end,
                        title=row.name,
                        summary=row.remarks,
                        status=row.status,
                        category=f"{row.operation_class}:{row.kind}",
                        operation_id=row.id,
                        section_id=row.section_id,
                        depth_md_si=row.depth_to_md_si or row.depth_from_md_si,
                        duration_hours=(
                            float(row.actual_duration_hours)
                            if row.actual_duration_hours is not None
                            else (
                                float(row.planned_duration_hours)
                                if row.planned_duration_hours is not None
                                else None
                            )
                        ),
                        npt=bool(row.npt_hours),
                        npt_hours=float(row.npt_hours) if row.npt_hours is not None else None,
                        document_id=row.source_document_id,
                        attributes={
                            "sequence": row.sequence,
                            "phase": row.phase,
                            "is_productive": row.is_productive,
                            "planned_duration_hours": row.planned_duration_hours,
                            "data_quality": row.data_quality,
                            "source": row.source,
                        },
                    )
                )

        if "event" in wanted:
            rows = await stream(Event, Event.occurred_at, "event")
            for row in rows:
                entries.append(
                    TimelineEntry(
                        kind="event",
                        id=row.id,
                        at=row.occurred_at,
                        end_at=row.ended_at,
                        title=row.title,
                        summary=row.description or row.root_cause,
                        status=row.status,
                        category=row.npt_category or row.category or row.kind,
                        operation_id=row.operation_id,
                        section_id=row.section_id,
                        depth_md_si=row.depth_md_si,
                        duration_hours=float(row.duration_hours) if row.duration_hours else None,
                        severity=row.severity,
                        npt=bool(row.is_npt),
                        npt_hours=float(row.npt_hours) if row.npt_hours is not None else None,
                        evidence_refs=[row.evidence_ref] if row.evidence_ref else [],
                        document_id=row.source_document_id,
                        attributes={
                            "kind": row.kind,
                            "npt_code": row.npt_code,
                            "immediate_action": row.immediate_action,
                            "corrective_action": row.corrective_action,
                        },
                    )
                )

        if "document" in wanted:
            rows = await stream(
                Document, func.coalesce(Document.period_start, Document.created_at), "document"
            )
            for row in rows:
                summary = row.extraction_summary or {}
                entries.append(
                    TimelineEntry(
                        kind="document",
                        id=row.id,
                        at=row.period_start or row.created_at,
                        end_at=row.period_end,
                        title=row.title or row.document_number or row.id,
                        summary=(
                            f"{row.doc_type} · status {row.status} · "
                            f"{summary.get('records', 0)} records · {summary.get('chunks', 0)} chunks"
                        ),
                        status=row.status,
                        category=row.doc_type,
                        evidence_refs=[],
                        document_id=row.id,
                        links={"document": f"/#/documents/{row.id}"},
                        attributes={
                            "page_count": row.page_count,
                            "ocr_required": row.ocr_required,
                            "extraction_summary": summary,
                            "revision": row.revision,
                        },
                    )
                )

        if "engine_run" in wanted:
            rows = await stream(
                EngineRun, func.coalesce(EngineRun.finished_at, EngineRun.started_at), "engine_run"
            )
            for row in rows:
                entries.append(
                    TimelineEntry(
                        kind="engine_run",
                        id=row.id,
                        at=row.finished_at or row.started_at,
                        end_at=None,
                        title=f"{row.engine_key} v{row.engine_version}",
                        summary=(
                            f"status {row.status}"
                            + (f" · {len(row.warnings)} warnings" if row.warnings else "")
                            + (" · INFEASIBLE" if row.is_feasible is False else "")
                        ),
                        status=row.status,
                        category=row.domain_pack,
                        operation_id=row.operation_id,
                        section_id=row.section_id,
                        duration_hours=None,
                        severity="warning" if row.warnings else None,
                        evidence_refs=[row.id],
                        links={"engine_run": f"/#/engineering/runs/{row.id}"},
                        attributes={
                            "engine_key": row.engine_key,
                            "engine_version": row.engine_version,
                            "is_feasible": row.is_feasible,
                            "duration_ms": row.duration_ms,
                            "triggered_by": row.triggered_by,
                            "workflow_run_id": row.workflow_run_id,
                            "inputs_hash": row.inputs_hash,
                        },
                    )
                )

        if "recommendation" in wanted:
            rows = await stream(Recommendation, Recommendation.created_at, "recommendation")
            for row in rows:
                entries.append(
                    TimelineEntry(
                        kind="recommendation",
                        id=row.id,
                        at=row.created_at,
                        end_at=row.reviewed_at,
                        title=row.title,
                        summary=row.statement,
                        status=row.status,
                        category=f"{row.domain}:{row.kind}",
                        operation_id=row.operation_id,
                        section_id=row.section_id,
                        evidence_refs=list(row.engine_run_ids or []) + list(row.offset_well_ids or []),
                        attributes={
                            "action_level": row.action_level,
                            "confidence": row.confidence,
                            "confidence_basis": row.confidence_basis,
                            "data_quality": row.data_quality,
                            "why_not": row.why_not,
                            "created_by_kind": row.created_by_kind,
                            "workflow_run_id": row.workflow_run_id,
                        },
                    )
                )

        if "twin_change" in wanted:
            rows = await stream(ChangeRecord, ChangeRecord.occurred_at, "twin_change")
            for row in rows:
                entries.append(
                    TimelineEntry(
                        kind="twin_change",
                        id=row.id,
                        at=row.occurred_at,
                        end_at=None,
                        title=f"{row.subject_kind} · {row.change_type}",
                        summary=row.reason,
                        status=row.change_type,
                        category=row.subject_kind,
                        section_id=None,
                        evidence_refs=[row.source_ref] if row.source_ref else [],
                        attributes={
                            "subject_id": row.subject_id,
                            "version": row.version,
                            "field_paths": row.field_paths,
                            "actor_kind": row.actor_kind,
                            "actor_id": row.actor_id,
                            "source": row.source,
                            "workflow_run_id": row.workflow_run_id,
                            "approval_id": row.approval_id,
                            "impact": row.impact,
                        },
                    )
                )

        if "approval" in wanted:
            rows = await stream(ApprovalRequest, ApprovalRequest.requested_at, "approval")
            for row in rows:
                entries.append(
                    TimelineEntry(
                        kind="approval",
                        id=row.id,
                        at=row.requested_at,
                        end_at=row.decided_at,
                        title=row.title or f"{row.kind} approval",
                        summary=row.description or row.decision_note,
                        status=row.status,
                        category=row.kind,
                        section_id=None,
                        evidence_refs=list(row.evidence_refs or []),
                        links={"approval": f"/#/runs?approval={row.id}"},
                        attributes={
                            "action_level": row.action_level,
                            "required_role": row.required_role,
                            "requested_by": row.requested_by,
                            "decided_by": row.decided_by,
                            "run_id": row.run_id,
                            "node_id": row.node_id,
                            "proposed_action": row.proposed_action,
                        },
                    )
                )

        entries.sort(key=sort_key)
        return entries[:limit]
