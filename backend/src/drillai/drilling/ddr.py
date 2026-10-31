"""Daily Drilling Report → structured engineering data.

The pipeline this module implements, end to end::

    Raw DDR
      → Parsed Document            (drillai.ingestion.parsers)
      → Extraction Records         (drillai.ingestion.extractors, with page/region provenance)
      → Validation                 (confidence + required-field rules, explicit review state)
      → Engineering Records        (Operation / Event / TwinAspect / TrajectoryStation rows)
      → Operation Events & NPT     (content-signal classification, never filename matching)
      → Daily Well State           (sections, twin aspects, evidence links)
      → Digital Well Twin          (planned/actual/current state, history preserved)
      → KPI / Reports              (drillai.drilling.state, .npt, .reporting)

Deliberate properties:

* **Idempotent.** Re-processing a document produces no duplicate operations or events: the
  fingerprint of each extracted record is stored on the created row's ``attributes`` and checked
  before insert.
* **Non-destructive.** Nothing is updated in place in the twin; every value written goes through
  :class:`~drillai.twin.service.TwinService` so history is preserved and a change record explains
  the change.
* **Explicit about what it did not do.** Every extracted record type that is *not* promoted to a
  structured row is listed in the report under ``not_promoted`` with the reason. Silence is not
  allowed to look like success.
* **Dry run.** ``dry_run=True`` computes exactly what would be written (creating nothing but the
  session-local objects it rolls back), which is what the workflow editor's dry-run uses.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import math
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.audit import record_audit
from drillai.core.clock import ensure_utc, parse_iso_datetime_utc
from drillai.core.errors import NotFound
from drillai.db.models import (
    AuditLog,
    Document,
    Event,
    EvidenceLink,
    ExtractedRecord,
    Operation,
    RawArtifact,
    Trajectory,
    TrajectoryStation,
    Well,
    Wellbore,
)
from drillai.documents.access import (
    document_not_quarantined_condition,
    extracted_record_scope_conditions,
)
from drillai.documents.lifecycle import MIN_PROMOTION_CONFIDENCE, PROMOTABLE_VALIDATION_STATES
from drillai.documents.scope import DocumentScope, DocumentScopeResolver
from drillai.drilling.classifiers import (
    classify_npt,
    classify_operation_kind,
    ensure_standard_npt_codes,
)
from drillai.operations.service import OperationService
from drillai.security.actions import Principal
from drillai.twin.aspects import StateKind
from drillai.twin.service import AspectRevision, TwinService

#: Extracted record types this processor understands, and what each becomes.
PROMOTION_MAP: dict[str, str] = {
    "operation_row": "operations",
    "mud_properties": "twin.mud_program",
    "depth_reading": "well_sections.current_md + twin.schedule",
    "survey_station": "trajectory_stations",
    "report_header": "document metadata (well.identity twin promotion not implemented)",
    "parameter_set": "twin.drilling_parameters (labelled parameters only)",
    "casing_cement_line": "evidence only (no structured casing/cement promotion yet)",
}

#: The rule that a promoted machine-extracted record has passed: confidence at or above
#: :data:`MIN_PROMOTION_CONFIDENCE`, a recognisable kind, and a structured row actually produced. An
#: explicitly ``human_validated`` record may bypass the minimum, but never a missing/non-finite or
#: out-of-range score when one is present; promotion never downgrades that human validation.
PROMOTION_RULE = "promotion_rules_v1"

_PROMOTION_IDENTITY_CONSTRAINTS = {
    "uq_operations_org_promotion_fingerprint",
    "uq_events_org_promotion_fingerprint",
}


def _is_promotion_identity_conflict(exc: IntegrityError) -> bool:
    """Recognize only the operation/event promotion identity race across supported SQL dialects."""
    original = getattr(exc, "orig", exc)
    diagnostic = getattr(original, "constraint_name", None) or getattr(
        getattr(original, "diag", None), "constraint_name", None
    )
    if diagnostic in _PROMOTION_IDENTITY_CONSTRAINTS:
        return True
    message = str(original).casefold()
    return any(
        f"{table}.org_id, {table}.promotion_fingerprint" in message
        for table in ("operations", "events")
    ) and "unique constraint failed" in message

#: Labels in a ``parameter_set`` that are safe to write into the twin as drilling parameters.
#: Anything not on this list stays as evidence — an unlabelled number is not a measurement.
KNOWN_PARAMETER_LABELS: dict[str, str] = {
    "wob": "wob",
    "weight_on_bit": "wob",
    "rpm": "rpm",
    "rotary_speed": "rpm",
    "flow_rate": "flow_rate",
    "flow": "flow_rate",
    "spp": "spp",
    "standpipe_pressure": "spp",
    "torque": "torque",
    "hookload": "hookload",
    "rop": "rop",
    "ecd": "ecd",
    "mud_weight": "mud_weight",
    "pump_pressure": "spp",
    # Generic "depth" is not proof of measured depth; only explicitly identified MD fields map here.
    "depth_md": "depth_md_si",
    "md": "depth_md_si",
    "measured_depth": "depth_md_si",
}


_DEPTH_UNIT_TO_METRES = {
    "m": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "metre": 1.0,
    "metres": 1.0,
    "ft": 0.3048,
    "foot": 0.3048,
    "feet": 0.3048,
    "'": 0.3048,
    "in": 0.0254,
    "inch": 0.0254,
    "inches": 0.0254,
    '"': 0.0254,
    "″": 0.0254,
}


def _depth_parameter_to_si(value: float, unit: str | None) -> float | None:
    """Convert an explicitly labelled measured-depth parameter to metres.

    ``depth_md_si`` is an SI field; copying a number with ``ft`` into it makes the twin wrong by a
    factor of 3.28084. A missing or unknown unit is not guessed: the source record remains available
    for review, but the value is not promoted into measured well state.
    """
    if not isinstance(unit, str) or not unit.strip():
        return None
    normalized = unit.strip().casefold().rstrip(".")
    factor = _DEPTH_UNIT_TO_METRES.get(normalized)
    if factor is None:
        return None
    converted = value * factor
    return converted if math.isfinite(converted) else None


def _parse_report_instant(raw: str, *, on_date: dt.datetime | None) -> dt.datetime | None:
    """Parse a report time cell into a UTC instant without inventing a missing date.

    DDRs commonly spell time as ``06:30``, ``0630`` or ``06:30:00``. A full ISO timestamp must
    carry its own offset; a clock-only value needs a trusted report/observation date. If either is
    absent or malformed, return ``None`` so the caller can preserve sequence without fabricating a
    wall-clock time.
    """
    text = raw.strip()
    try:
        return parse_iso_datetime_utc(text)
    except ValueError:
        pass
    match = re.fullmatch(r"(?P<h>\d{1,2})[:.](?P<m>\d{2})(?::(?P<s>\d{2}))?", text)
    if match is None:
        match = re.fullmatch(r"(?P<h>\d{2})(?P<m>\d{2})(?P<s>\d{2})?", text)
    if match is None or on_date is None:
        return None
    hour, minute = int(match.group("h")), int(match.group("m"))
    second = int(match.groupdict().get("s") or 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    base = ensure_utc(on_date)
    return base.replace(hour=hour, minute=minute, second=second, microsecond=0)


def _valid_report_duration(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(numeric) and 0.0 <= numeric < 100.0


def _audit_jsonable(value: Any) -> Any:
    """Convert audit values to JSON scalars without losing timezone meaning."""
    if isinstance(value, dt.datetime):
        return ensure_utc(value).isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _audit_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_jsonable(item) for item in value]
    return value


def _reported_duration(payload: dict[str, Any]) -> Any:
    for key in ("duration_hours", "npt_hours"):
        if payload.get(key) is not None:
            return payload[key]
    return None


def _valid_nonnegative_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        numeric = float(value)
    except (OverflowError, ValueError):
        return False
    return math.isfinite(numeric) and numeric >= 0.0


def _first_valid_depth(*values: Any) -> float | None:
    """Choose the first finite non-negative MD source without treating zero as absent."""
    for value in values:
        if _valid_nonnegative_number(value):
            return float(value)
    return None


@dataclass
class ProcessingReport:
    """What the processor actually did, and what it deliberately did not do."""

    document_id: str
    well_id: str | None
    doc_type: str
    dry_run: bool = False
    operations_created: list[str] = field(default_factory=list)
    operations_linked: list[str] = field(default_factory=list)
    operations_updated: list[str] = field(default_factory=list)
    events_created: list[str] = field(default_factory=list)
    events_updated: list[str] = field(default_factory=list)
    twin_aspects: list[dict[str, str]] = field(default_factory=list)
    trajectory_stations_created: int = 0
    records_promoted: list[str] = field(default_factory=list)
    records_needing_review: list[str] = field(default_factory=list)
    not_promoted: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    npt_hours_classified: float = 0.0
    operations_hours: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "well_id": self.well_id,
            "doc_type": self.doc_type,
            "dry_run": self.dry_run,
            "operations_created": self.operations_created,
            "operations_linked": self.operations_linked,
            "operations_updated": self.operations_updated,
            "events_created": self.events_created,
            "events_updated": self.events_updated,
            "twin_aspects": self.twin_aspects,
            "trajectory_stations_created": self.trajectory_stations_created,
            "records_promoted": self.records_promoted,
            "records_needing_review": self.records_needing_review,
            "not_promoted": self.not_promoted,
            "warnings": self.warnings,
            "npt_hours_classified": round(self.npt_hours_classified, 3),
            "operations_hours": round(self.operations_hours, 3),
            "counts": {
                "operations": len(self.operations_created),
                "operations_linked": len(self.operations_linked),
                "operations_updated": len(self.operations_updated),
                "events": len(self.events_created),
                "events_updated": len(self.events_updated),
                "twin_aspects": len(self.twin_aspects),
                "trajectory_stations": self.trajectory_stations_created,
                "promoted": len(self.records_promoted),
                "needs_review": len(self.records_needing_review),
            },
        }


class DdrProcessor:
    """Promotes an ingested DDR into structured drilling data."""

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
        self._principal = principal
        self.actor_id = actor_id
        self.twin = TwinService(session, org_id=org_id)

    async def _document(self, document_id: str) -> Document:
        row = (
            await self.session.execute(
                select(Document)
                .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
                .where(
                    Document.id == document_id,
                    Document.org_id == self.org_id,
                    document_not_quarantined_condition(),
                )
                .with_for_update(of=Document)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound("document not found", details={"document_id": document_id})
        # Locking the document serializes retries for this source on PostgreSQL. Validate the stored
        # hierarchy as well: a corrupt document must not promote records into a different wellbore.
        await DocumentScopeResolver(self.session, self.org_id).resolve(
            DocumentScope(
                project_id=row.project_id,
                well_id=row.well_id,
                wellbore_id=row.wellbore_id,
                section_id=row.section_id,
                operation_id=row.operation_id,
            )
        )
        return row

    @staticmethod
    def _report_date(document: Document, records: list[ExtractedRecord]) -> dt.datetime | None:
        """The best source-backed instant describing the report.

        Resolution order is the explicit reporting period start, the document issue date, then the
        parsed report-header value. A lone period end is an interval boundary, not evidence of when an
        event occurred, so it is not promoted into a point timestamp. Ingestion time is deliberately
        not a report-date fallback: absent source time stays unknown and is marked separately where a
        required event timestamp forces an ingestion-time placeholder.
        """
        if document.period_start is not None:
            return ensure_utc(document.period_start)
        if document.issue_date is not None:
            return ensure_utc(document.issue_date)
        for record in records:
            if record.record_type != "report_header":
                continue
            raw = (record.payload or {}).get("report_date")
            if isinstance(raw, str):
                with contextlib.suppress(ValueError, TypeError):
                    return parse_iso_datetime_utc(raw, assume_utc_for_naive=True)
        return None

    @staticmethod
    def _event_time(
        *,
        record: ExtractedRecord,
        linked: Operation | None,
        report_date: dt.datetime | None,
        existing: Event | None = None,
    ) -> tuple[dt.datetime, str]:
        if linked is not None and linked.actual_start is not None:
            return ensure_utc(linked.actual_start), "linked_operation_start"
        if record.observed_at is not None:
            return ensure_utc(record.observed_at), "extracted_record_observed_at"
        if report_date is not None:
            return ensure_utc(report_date), "document_report_date"
        if existing is not None:
            return ensure_utc(existing.occurred_at), (existing.attributes or {}).get(
                "occurred_at_basis", "previously_recorded_timestamp"
            )
        # Event.occurred_at is required in storage. When source time is absent, use the persisted
        # extraction record creation time and label it as ingestion time, never as incident time.
        return ensure_utc(record.created_at), "ingestion_timestamp_fallback"

    async def _records(self, document_id: str) -> list[ExtractedRecord]:
        return list(
            (
                await self.session.execute(
                    select(ExtractedRecord)
                    .join(Document, Document.id == ExtractedRecord.document_id)
                    .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
                    .where(
                        ExtractedRecord.org_id == self.org_id,
                        Document.org_id == self.org_id,
                        ExtractedRecord.document_id == document_id,
                        document_not_quarantined_condition(),
                        *extracted_record_scope_conditions(),
                    )
                    .order_by(ExtractedRecord.page_number, ExtractedRecord.created_at)
                )
            )
            .scalars()
            .all()
        )

    @staticmethod
    def _record_text(record: ExtractedRecord) -> str:
        payload = record.payload or {}
        parts = [str(value) for value in payload.values() if isinstance(value, str)]
        return " | ".join(parts) if parts else str(payload)

    @staticmethod
    def _fingerprint(document_id: str, record: ExtractedRecord) -> str:
        import hashlib

        raw = f"{document_id}:{record.record_type}:{record.fingerprint or record.id}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    async def _existing_operations_for_document(self, document: Document) -> list[Operation]:
        """Read and lock this document's rows, including corrupt-scope identities that block retries."""
        return list(
            (
                await self.session.execute(
                    select(Operation)
                    .where(
                        Operation.org_id == self.org_id,
                        Operation.source_document_id == document.id,
                    )
                    .with_for_update(of=Operation)
                )
            )
            .scalars()
            .all()
        )

    async def _existing_events_for_document(self, document: Document) -> list[Event]:
        """Read and lock this document's events, including scope-corrupt identity conflicts."""
        return list(
            (
                await self.session.execute(
                    select(Event)
                    .where(
                        Event.org_id == self.org_id,
                        Event.source_document_id == document.id,
                    )
                    .with_for_update(of=Event)
                )
            )
            .scalars()
            .all()
        )

    def _promotion_row_matches_document(self, row: Operation | Event, document: Document) -> bool:
        if row.org_id != self.org_id or row.well_id != document.well_id:
            return False
        for field_name in ("project_id", "wellbore_id", "section_id"):
            expected = getattr(document, field_name)
            if expected is not None and getattr(row, field_name) != expected:
                return False
        return True

    async def _operation_has_manual_edits(self, operation: Operation) -> bool:
        """Do not let a source retry overwrite edits or state transitions made by a person.

        A human may use the same free-text reason as the processor, so the protected distinction is an
        internal audit marker, never the reason string alone. Older unmarked updates fail closed as
        person-authored edits.
        """
        rows = (
            await self.session.execute(
                select(AuditLog.action, AuditLog.details).where(
                    AuditLog.org_id == self.org_id,
                    AuditLog.resource_kind == "operation",
                    AuditLog.resource_id == operation.id,
                    AuditLog.action.in_(("operation.update", "operation.transition")),
                )
            )
        ).all()
        return any(
            action != "operation.update"
            or not isinstance(details, dict)
            or not isinstance(details.get("audit_metadata"), dict)
            or details["audit_metadata"].get("source") != "ddr_reconciliation"
            or details["audit_metadata"].get("source_document_id") != operation.source_document_id
            or details["audit_metadata"].get("source_record_id") != operation.source_record_id
            for action, details in rows
        )

    async def _event_has_manual_edits(self, event: Event) -> bool:
        """Protect person-authored edits even if an event remains open and marked as derived."""
        rows = (
            await self.session.execute(
                select(AuditLog.action, AuditLog.details).where(
                    AuditLog.org_id == self.org_id,
                    AuditLog.resource_kind == "event",
                    AuditLog.resource_id == event.id,
                    AuditLog.action.in_(
                        ("event.update", "event.transition", "event.link_document")
                    ),
                )
            )
        ).all()
        return any(
            action != "event.update"
            or not isinstance(details, dict)
            or not isinstance(details.get("audit_metadata"), dict)
            or details["audit_metadata"].get("source") != "ddr_reconciliation"
            or details["audit_metadata"].get("source_document_id") != event.source_document_id
            or details["audit_metadata"].get("source_record_id") != event.source_record_id
            for action, details in rows
        )

    async def _warn_existing_promotions_for_ineligible_records(
        self, document: Document, record_ids: set[str], report: ProcessingReport
    ) -> None:
        """Surface previously promoted rows whose source is no longer eligible, without erasing history."""
        if not record_ids:
            return
        operation_ids = list(
            (
                await self.session.execute(
                    select(Operation.id).where(
                        Operation.org_id == self.org_id,
                        Operation.source_document_id == document.id,
                        Operation.source_record_id.in_(record_ids),
                    )
                )
            ).scalars()
        )
        event_ids = list(
            (
                await self.session.execute(
                    select(Event.id).where(
                        Event.org_id == self.org_id,
                        Event.source_document_id == document.id,
                        Event.source_record_id.in_(record_ids),
                    )
                )
            ).scalars()
        )
        if operation_ids or event_ids:
            report.warnings.append(
                f"{len(operation_ids)} previously promoted operation(s) and {len(event_ids)} event(s) "
                "reference source records that no longer pass the validation-state, confidence, or "
                "duration gates; they were retained for audit and human review, not automatically cancelled"
            )

    async def _link_evidence(
        self,
        *,
        subject_kind: str,
        subject_id: str,
        record: ExtractedRecord,
        document: Document,
        note: str | None = None,
    ) -> str:
        link = EvidenceLink(
            org_id=self.org_id,
            subject_kind=subject_kind,
            subject_id=subject_id,
            evidence_kind=f"extracted_record.{record.record_type}",
            evidence_id=record.id,
            document_id=document.id,
            region_id=record.region_id,
            page_number=record.page_number,
            well_id=document.well_id,
            locator={
                "page": record.page_number,
                "record_type": record.record_type,
                "record_id": record.id,
                "method": record.method,
                "method_version": record.method_version,
            },
            excerpt=(record.payload and str(record.payload))[:2000] or None,
            confidence=record.confidence,
            relevance=1.0,
            weight=1.0,
            method=f"ddr_promotion:{record.method}",
            quote_verified=False,
            note=note,
            created_by=self.actor_id,
        )
        self.session.add(link)
        await self.session.flush()
        return link.id

    # ------------------------------------------------------------------ promotion steps

    async def _promote_operations(
        self,
        document: Document,
        records: list[ExtractedRecord],
        report: ProcessingReport,
        *,
        dry_run: bool,
    ) -> dict[str, Operation]:
        rows = [record for record in records if record.record_type == "operation_row"]
        if not rows:
            return {}
        if not document.wellbore_id:
            for record in rows:
                report.records_needing_review.append(record.id)
                report.not_promoted.append(
                    {
                        "record_id": record.id,
                        "record_type": "operation_row",
                        "reason": "document has no wellbore; operations require a validated wellbore scope",
                    }
                )
            report.warnings.append("operation rows were retained for review because the document has no wellbore")
            return {}
        report_date = self._report_date(document, records)
        if report_date is None:
            report.warnings.append(
                "the document carries no report date, so promoted operations have no start time; "
                "operation sequencing is preserved but the timeline cannot place them"
            )
        existing_operations = await self._existing_operations_for_document(document)
        existing: set[str] = set()
        existing_by_fingerprint: dict[str, Operation] = {}
        existing_by_record: dict[str, Operation] = {}
        for operation in existing_operations:
            if operation.source_record_id:
                existing_by_record.setdefault(operation.source_record_id, operation)
            fingerprints = {operation.promotion_fingerprint}
            fingerprints.update(
                value
                for key, value in (operation.attributes or {}).items()
                if key in {"extraction_fingerprint", "promotion_fingerprint"}
                and isinstance(value, str)
            )
            for fingerprint_value in fingerprints:
                if isinstance(fingerprint_value, str) and fingerprint_value:
                    existing.add(fingerprint_value)
                    existing_by_fingerprint.setdefault(fingerprint_value, operation)
        by_fingerprint: dict[str, Operation] = {}
        last: Operation | None = None
        # A DDR time breakdown is an ordered list of durations, not a list of absolute timestamps.
        # Replaying the durations forward from the report date rebuilds the shift's real timeline;
        # an explicit start time in the row wins over the cursor, because it is direct evidence.
        cursor = report_date

        operation_service = OperationService(
            self.session,
            self.org_id,
            principal=self._principal,
            actor_id=self.actor_id or "ddr_promotion",
        )
        for record in rows:
            fingerprint = self._fingerprint(document.id, record)
            operation = existing_by_fingerprint.get(fingerprint) or existing_by_record.get(record.id)
            is_existing = fingerprint in existing or record.id in existing_by_record
            if (
                not is_existing
                and record.validation_state != "human_validated"
                and (record.confidence is None or record.confidence < MIN_PROMOTION_CONFIDENCE)
            ):
                report.records_needing_review.append(record.id)
                report.not_promoted.append(
                    {
                        "record_id": record.id,
                        "record_type": record.record_type,
                        "reason": (
                            f"confidence {record.confidence} below promotion threshold "
                            f"{MIN_PROMOTION_CONFIDENCE}; queued for human review instead"
                        ),
                    }
                )
                continue

            payload = record.payload or {}
            text = self._record_text(record)
            classification = classify_operation_kind(text)
            kind = classification.label or "unknown"
            name = (
                payload.get("operation")
                or payload.get("activity")
                or payload.get("description")
                or (text[:120] if text else "Operation from report")
            )
            duration = payload.get("duration_hours")
            duration_hours = float(duration) if duration is not None and _valid_report_duration(duration) else None
            explicit = payload.get("start_time") or payload.get("start") or payload.get("time")
            started: dt.datetime | None = None
            time_basis = "unknown"
            if isinstance(explicit, str):
                with contextlib.suppress(ValueError, TypeError):
                    started = _parse_report_instant(
                        explicit,
                        on_date=report_date or record.observed_at,
                    )
                if started is not None:
                    time_basis = "explicit_time_in_row"
            observed_at = ensure_utc(record.observed_at) if record.observed_at is not None else None
            if started is None and observed_at is not None and (report_date is None or observed_at != report_date):
                started = observed_at
                time_basis = "record_observed_at"
            if started is None and cursor is not None:
                # A document-level report date is a day anchor, not an explicit per-row timestamp.
                # Replaying the ordered durations keeps the activity chain instead of pinning every
                # operation to the same midnight observation.
                started = cursor
                time_basis = "replayed_from_report_duration" if last is not None else "report_date"
            end = (
                started + dt.timedelta(hours=duration_hours)
                if started is not None and duration_hours is not None
                else None
            )
            if kind == "unknown":
                report.warnings.append(
                    f"operation row {record.id} matched no registered activity signal; "
                    "recorded as 'unknown' rather than guessed"
                )

            if is_existing:
                if operation is not None and not self._promotion_row_matches_document(operation, document):
                    report.records_needing_review.append(record.id)
                    report.not_promoted.append(
                        {
                            "record_id": record.id,
                            "record_type": "operation_row",
                            "reason": (
                                f"existing operation {operation.id} conflicts with the document scope; "
                                "its identity blocks a duplicate and it was left unchanged for review"
                            ),
                        }
                    )
                    report.warnings.append(
                        f"operation identity for source record {record.id} exists outside the document scope; "
                        "no duplicate was created"
                    )
                    continue
                if operation is not None:
                    if (
                        operation.source_kind == "ddr_promotion"
                        and operation.source_record_id == record.id
                        and operation.status != "cancelled"
                    ):
                        changes: dict[str, Any] = {
                            "name": str(name)[:300],
                            "kind": kind,
                            "remarks": text[:2000] or None,
                        }
                        if payload.get("code") is not None:
                            changes["code"] = str(payload["code"])[:60]
                        if started is not None:
                            changes["actual_start"] = started
                        if duration_hours is not None:
                            changes["actual_duration_hours"] = duration_hours
                            if end is not None:
                                changes["actual_end"] = end
                        depth_md = payload.get("depth_md_si")
                        if _valid_nonnegative_number(depth_md):
                            changes["depth_to_md_si"] = float(depth_md)
                        changed = {
                            key: value for key, value in changes.items() if getattr(operation, key) != value
                        }
                        if changed:
                            if await self._operation_has_manual_edits(operation):
                                report.warnings.append(
                                    f"source record {record.id} changed, but operation {operation.id} has "
                                    "a person-authored update; it was retained for human review"
                                )
                            else:
                                report.operations_updated.append(operation.id)
                                if dry_run:
                                    report.warnings.append(
                                        f"dry run: operation {operation.id} would be refreshed from "
                                        f"source record {record.id}"
                                    )
                                else:
                                    await operation_service.update(
                                        operation.id,
                                        expected_updated_at=operation.updated_at,
                                        reason="DDR source record changed on retry",
                                        changes=changed,
                                        audit_metadata={
                                            "source": "ddr_reconciliation",
                                            "source_document_id": document.id,
                                            "source_record_id": record.id,
                                        },
                                    )
                    else:
                        report.warnings.append(
                            f"existing operation {operation.id} is not a source-derived row for "
                            f"record {record.id}; it was linked without overwriting it"
                        )
                    by_fingerprint[fingerprint] = operation
                    last = operation
                    if record.promoted_to_id is None:
                        record.promoted_to_kind = "operation"
                        record.promoted_to_id = operation.id
                    report.operations_hours += duration_hours if duration_hours is not None else (
                        operation.actual_duration_hours or 0.0
                    )
                    effective_end = end if end is not None else operation.actual_end
                    effective_start = started if started is not None else operation.actual_start
                    if effective_end is not None:
                        cursor = effective_end
                    elif cursor is not None and effective_start is not None:
                        cursor = max(cursor, effective_start)
                report.operations_linked.append(record.id)
                continue

            # Promotion goes through the domain service rather than around it. The status is derived
            # from the source-backed interval, and sequence is allocated per wellbore in this transaction.
            operation = await operation_service.create(
                scope=DocumentScope(
                    project_id=document.project_id,
                    well_id=document.well_id,
                    wellbore_id=document.wellbore_id,
                    section_id=document.section_id,
                ),
                name=str(name)[:300],
                operation_class="actual",
                kind=kind,
                status="completed" if end is not None else "in_progress",
                predecessor_operation_id=last.id if last is not None else None,
                source_kind="ddr_promotion",
                source_document_id=document.id,
                source_record_id=record.id,
                code=str(payload.get("code"))[:60] if payload.get("code") else None,
                actual_start=started,
                actual_end=end,
                actual_duration_hours=duration_hours,
                depth_to_md_si=(float(payload["depth_md_si"]) if _valid_nonnegative_number(payload.get("depth_md_si")) else None),
                is_productive=True,
                promotion_fingerprint=fingerprint,
                data_quality="extracted",
                remarks=text[:2000] or None,
                attributes={
                    "promotion_fingerprint": fingerprint,
                    "classification": classification.to_dict(),
                    "extractor": record.method,
                    "extractor_version": record.method_version,
                    "page_number": record.page_number,
                    "time_basis": time_basis,
                    "row_index": payload.get("row_index"),
                },
            )
            await self.session.flush()
            by_fingerprint[fingerprint] = operation
            report.operations_hours += operation.actual_duration_hours or 0.0
            if dry_run:
                report.operations_created.append(operation.id)
                last = operation
                if end is not None:
                    cursor = end
                elif started is not None and cursor is not None:
                    cursor = max(cursor, started)
                continue
            await self._link_evidence(
                subject_kind="operation",
                subject_id=operation.id,
                record=record,
                document=document,
                note=f"promoted from {record.record_type}",
            )
            record.promoted_to_kind = "operation"
            record.promoted_to_id = operation.id
            if record.validation_state != "human_validated":
                record.validation_state = "rule_validated"
                record.validation_rule = PROMOTION_RULE
            report.records_promoted.append(record.id)
            report.operations_created.append(operation.id)
            last = operation
            if end is not None:
                cursor = end
            elif started is not None and cursor is not None:
                cursor = max(cursor, started)
        return by_fingerprint

    async def _promote_npt_events(
        self,
        document: Document,
        records: list[ExtractedRecord],
        operations: dict[str, Operation],
        report: ProcessingReport,
        *,
        dry_run: bool,
    ) -> None:
        """Detect NPT signals and reconcile still-open derived events on a safe retry.

        Re-extraction can change a record's classification while retaining the record identity. A
        retry therefore must not treat "fingerprint already exists" as proof that the derived event is
        still current. Only an open event that is still marked derived and still points at this record
        may be refreshed; acknowledged/closed or recorded events are left for a person to resolve.
        """
        await ensure_standard_npt_codes(self.session, self.org_id)
        report_date = self._report_date(document, records)
        existing_events = await self._existing_events_for_document(document)
        existing_by_fingerprint: dict[str, Event] = {}
        existing_by_record: dict[str, Event] = {}
        for event in existing_events:
            if event.source_record_id:
                existing_by_record.setdefault(event.source_record_id, event)
            fingerprints = {event.promotion_fingerprint}
            fingerprints.update(
                value
                for key, value in (event.attributes or {}).items()
                if key in {"extraction_fingerprint", "promotion_fingerprint"}
                and isinstance(value, str)
            )
            for fingerprint_value in fingerprints:
                if isinstance(fingerprint_value, str) and fingerprint_value:
                    existing_by_fingerprint.setdefault(fingerprint_value, event)

        for record in records:
            text = self._record_text(record)
            if not text.strip():
                continue
            classification, code, controllable = classify_npt(text)
            fingerprint = self._fingerprint(document.id, record)
            existing_event = existing_by_fingerprint.get(fingerprint) or existing_by_record.get(record.id)
            if existing_event is not None and not self._promotion_row_matches_document(existing_event, document):
                report.records_needing_review.append(record.id)
                report.not_promoted.append(
                    {
                        "record_id": record.id,
                        "record_type": record.record_type,
                        "reason": (
                            f"existing event {existing_event.id} conflicts with the document scope; "
                            "its identity blocks a duplicate and it was left unchanged for review"
                        ),
                    }
                )
                report.warnings.append(
                    f"event identity for source record {record.id} exists outside the document scope; "
                    "no duplicate was created"
                )
                continue
            payload = record.payload or {}
            raw_hours = _reported_duration(payload)
            hours: float | None = None
            if raw_hours is not None:
                if _valid_report_duration(raw_hours) and float(raw_hours) > 0:
                    hours = float(raw_hours)
                elif not _valid_report_duration(raw_hours):
                    report.warnings.append(
                        f"record {record.id} has an invalid NPT duration; its event may be classified "
                        "but no duration will be assigned"
                    )

            if classification.label is None:
                if existing_event is not None:
                    report.warnings.append(
                        f"existing derived event {existing_event.id} no longer matches the current "
                        f"NPT rules for record {record.id}; it was retained for human review and not cancelled"
                    )
                continue

            linked = operations.get(fingerprint)
            if hours:
                report.npt_hours_classified += hours
            if existing_event is not None:
                desired_attributes = {
                    **(existing_event.attributes or {}),
                    "promotion_fingerprint": fingerprint,
                    "classification": classification.to_dict(),
                    "operator_controllable": controllable,
                    "classified_from": record.record_type,
                    "attributed_operation_id": (
                        existing_event.operation_id or (linked.id if linked is not None else None)
                    ),
                }
                event_occurred_at, event_time_basis = self._event_time(
                    record=record,
                    linked=linked,
                    report_date=report_date,
                    existing=existing_event,
                )
                desired_attributes["occurred_at_basis"] = event_time_basis
                depth_md_si = _first_valid_depth(
                    linked.depth_to_md_si if linked is not None else None,
                    payload.get("depth_md_si"),
                    record.depth_md_si,
                )
                desired = {
                    "kind": "npt" if hours else "problem",
                    "category": classification.label,
                    "npt_code": code,
                    "npt_category": classification.label,
                    "title": (
                        f"{classification.label.replace('_', ' ').title()} reported in "
                        f"{document.title or document.id}"
                    ),
                    "description": text[:4000] or None,
                    "occurred_at": event_occurred_at,
                    "ended_at": linked.actual_end if linked is not None else existing_event.ended_at,
                    "duration_hours": hours,
                    "npt_hours": hours,
                    "depth_md_si": depth_md_si,
                    "is_npt": bool(hours),
                    "severity": "high"
                    if classification.label in {"well_control", "stuck_pipe"}
                    else "medium",
                    "attributes": desired_attributes,
                }
                changes = {
                    field_name: {
                        "before": getattr(existing_event, field_name),
                        "after": value,
                    }
                    for field_name, value in desired.items()
                    if getattr(existing_event, field_name) != value
                }
                if existing_event.operation_id is None and linked is not None:
                    changes["operation_id"] = {"before": None, "after": linked.id}
                    if existing_event.section_id is None and linked.section_id is not None:
                        changes["section_id"] = {"before": None, "after": linked.section_id}

                if not changes:
                    continue
                if (
                    existing_event.source_kind != "ddr_promotion"
                    or existing_event.classification_source != "derived"
                    or existing_event.source_record_id != record.id
                    or existing_event.status != "open"
                ):
                    report.warnings.append(
                        f"existing event {existing_event.id} differs from the current source record, "
                        "but is no longer an open, derived event; it was left unchanged for human review"
                    )
                    continue
                if await self._event_has_manual_edits(existing_event):
                    report.warnings.append(
                        f"existing event {existing_event.id} has a person-authored edit; "
                        "the source retry left it unchanged for human review"
                    )
                    continue
                report.events_updated.append(existing_event.id)
                if dry_run:
                    report.warnings.append(
                        f"dry run: derived event {existing_event.id} would be refreshed from record {record.id}"
                    )
                    continue

                before = {
                    field_name: _audit_jsonable(change["before"])
                    for field_name, change in changes.items()
                }
                after = {
                    field_name: _audit_jsonable(change["after"])
                    for field_name, change in changes.items()
                }
                previous_event_operation_id = existing_event.operation_id
                previous_event_npt_hours = existing_event.npt_hours
                for field_name, value in desired.items():
                    if field_name in changes:
                        setattr(existing_event, field_name, value)
                if "operation_id" in changes:
                    existing_event.operation_id = linked.id
                    if "section_id" in changes:
                        existing_event.section_id = linked.section_id

                # Keep the operation roll-up consistent when this open derived event's duration
                # changes. Subtract only this event's previous contribution, then add its new one;
                # other NPT events on the operation remain accounted for.
                operation_rollup_change: dict[str, Any] | None = None
                rollup_operation_id = existing_event.operation_id or (
                    linked.id if linked is not None else None
                )
                if rollup_operation_id is not None:
                    operation = (
                        await self.session.execute(
                            select(Operation)
                            .where(
                                Operation.org_id == self.org_id,
                                Operation.id == rollup_operation_id,
                                Operation.well_id == document.well_id,
                                *(
                                    [Operation.project_id == document.project_id]
                                    if document.project_id is not None
                                    else []
                                ),
                                *(
                                    [Operation.wellbore_id == document.wellbore_id]
                                    if document.wellbore_id is not None
                                    else []
                                ),
                                *(
                                    [Operation.section_id == document.section_id]
                                    if document.section_id is not None
                                    else []
                                ),
                            )
                            .with_for_update()
                        )
                    ).scalars().first()
                    if operation is None:
                        report.warnings.append(
                            f"operation {rollup_operation_id} for derived event {existing_event.id} "
                            "was not found; its NPT roll-up was not changed"
                        )
                    else:
                        current_total = float(operation.npt_hours or 0.0)
                        previous_contribution = (
                            float(previous_event_npt_hours or 0.0)
                            if previous_event_operation_id == rollup_operation_id
                            else 0.0
                        )
                        next_contribution = float(hours or 0.0)
                        if (
                            not math.isfinite(current_total)
                            or current_total < 0.0
                            or not math.isfinite(previous_contribution)
                            or previous_contribution < 0.0
                        ):
                            report.warnings.append(
                                f"operation {rollup_operation_id} or event {existing_event.id} has an "
                                "invalid NPT roll-up; the operation total was left unchanged"
                            )
                        else:
                            next_total = max(
                                0.0, current_total - previous_contribution + next_contribution
                            )
                            if next_total != current_total:
                                operation.npt_hours = next_total
                                if next_total > 0.0:
                                    operation.is_productive = False
                                operation_rollup_change = {
                                    "operation_id": operation.id,
                                    "before": current_total,
                                    "after": next_total,
                                }
                await self.session.flush()
                await record_audit(
                    self.session,
                    org_id=self.org_id,
                    principal=self._principal,
                    action="event.update",
                    resource_kind="event",
                    resource_id=existing_event.id,
                    project_id=existing_event.project_id,
                    well_id=existing_event.well_id,
                    before=before,
                    after=after,
                    details={
                        "reason": "DDR source record was reclassified on retry",
                        "source_document_id": document.id,
                        "source_record_id": record.id,
                        "classification_source": "derived",
                        "audit_metadata": {
                            "source": "ddr_reconciliation",
                            "source_document_id": document.id,
                            "source_record_id": record.id,
                        },
                        "operation_npt_rollup": operation_rollup_change,
                    },
                )
                report.warnings.append(
                    f"refreshed derived event {existing_event.id} from the current classification "
                    f"of record {record.id}"
                )
                continue

            occurred, event_time_basis = self._event_time(
                record=record,
                linked=linked,
                report_date=report_date,
            )
            if event_time_basis == "ingestion_timestamp_fallback":
                report.warnings.append(
                    f"NPT record {record.id} has no source-backed event time; the required occurred_at "
                    "uses extraction-record creation time, not a verified incident time"
                )
            event = Event(
                org_id=self.org_id,
                project_id=document.project_id,
                well_id=document.well_id,
                wellbore_id=document.wellbore_id,
                section_id=(linked.section_id if linked is not None else document.section_id),
                operation_id=linked.id if linked is not None else None,
                kind="npt" if hours else "problem",
                category=classification.label,
                npt_code=code,
                npt_category=classification.label,
                is_npt=bool(hours),
                title=f"{classification.label.replace('_', ' ').title()} reported in {document.title or document.id}",
                description=text[:4000] or None,
                occurred_at=occurred,
                ended_at=(linked.actual_end if linked is not None else None),
                duration_hours=hours,
                depth_md_si=_first_valid_depth(
                    linked.depth_to_md_si if linked is not None else None,
                    payload.get("depth_md_si"),
                    record.depth_md_si,
                ),
                depth_tvd_si=record.depth_tvd_si,
                severity="high" if classification.label in {"well_control", "stuck_pipe"} else "medium",
                npt_hours=hours,
                # **Deliberately no root cause.** The text below is the record's own words and it is
                # stored as the description; writing the same string into `root_cause` asserted a
                # causal claim the report never made. Neither the extractors nor the classifier
                # identify a cause, so the field stays empty with `cause_basis="unknown"` — a reader
                # then sees "nobody has established why", which is true, instead of a sentence that
                # reads like the report's diagnosis. `npt.summarise` and the timeline both fall back to
                # the description, so nothing downstream loses the text.
                root_cause=None,
                cause_basis="unknown",
                immediate_action=None,
                corrective_action=None,
                status="open",
                source="report",
                source_kind="ddr_promotion",
                source_document_id=document.id,
                source_record_id=record.id,
                promotion_fingerprint=fingerprint,
                # This is a classification derived from the report's wording, not a category the
                # report stated as a fact.
                classification_source="derived",
                tags=["auto_classified", "ddr"],
                attributes={
                    "promotion_fingerprint": fingerprint,
                    "classification": classification.to_dict(),
                    "operator_controllable": controllable,
                    "classified_from": record.record_type,
                    "attributed_operation_id": linked.id if linked is not None else None,
                    "occurred_at_basis": event_time_basis,
                },
            )
            self.session.add(event)
            await self.session.flush()
            report.events_created.append(event.id)
            if dry_run:
                continue
            await self._link_evidence(
                subject_kind="event",
                subject_id=event.id,
                record=record,
                document=document,
                note="NPT classification evidence",
            )
            if hours and linked is not None:
                # The operation row's NPT roll-up, and its productivity flag, agree with the event.
                linked.npt_hours = (linked.npt_hours or 0.0) + hours
                linked.is_productive = False
            if record.promoted_to_id is None:
                record.promoted_to_kind = "event"
                record.promoted_to_id = event.id

    async def _promote_mud_properties(
        self,
        document: Document,
        records: list[ExtractedRecord],
        report: ProcessingReport,
        *,
        dry_run: bool,
    ) -> None:
        rows = [record for record in records if record.record_type == "mud_properties"]
        if not rows:
            return
        latest = rows[-1]
        payload = {key: value for key, value in (latest.payload or {}).items() if value is not None}
        if not payload:
            return
        if (
            latest.validation_state != "human_validated"
            and (latest.confidence is None or latest.confidence < MIN_PROMOTION_CONFIDENCE)
        ):
            report.records_needing_review.append(latest.id)
            return
        if dry_run:
            report.twin_aspects.append({"aspect": "mud_program", "state_kind": "actual", "dry_run": "true"})
            return
        revision = AspectRevision(
            aspect="mud_program",
            state_kind=StateKind.ACTUAL,
            payload=payload,
            summary=f"mud properties from {document.title or document.id}",
            confidence=latest.confidence,
            data_quality="extracted",
            computed_by="import",
            source_refs=[document.id],
            evidence_refs=[latest.id],
            valid_from=document.period_start,
            assumptions=[
                "values are transcribed from the report; they are not laboratory-verified",
            ],
        )
        aspect = await self.twin.write_aspect(well_id=document.well_id, revision=revision)
        report.twin_aspects.append({"aspect": "mud_program", "state_kind": "actual", "id": aspect.id})
        report.records_promoted.append(latest.id)
        latest.promoted_to_kind = "twin_aspect"
        latest.promoted_to_id = aspect.id

    async def _promote_drilling_parameters(
        self,
        document: Document,
        records: list[ExtractedRecord],
        report: ProcessingReport,
        *,
        dry_run: bool,
    ) -> None:
        """Write *labelled* drilling parameters into the twin.

        Only labels on :data:`KNOWN_PARAMETER_LABELS` are promoted. An unrecognised label stays in
        the evidence layer: a number whose meaning is unknown must not become well state.
        """
        promoted: dict[str, Any] = {}
        units: dict[str, str] = {}
        source_units: dict[str, str] = {}
        source_records: list[str] = []
        unrecognised: list[str] = []
        depth_unit_issues: list[str] = []
        for record in records:
            if record.record_type != "parameter_set":
                continue
            unit_context = record.unit_context or {}
            for label, value in (record.payload or {}).items():
                normalised = KNOWN_PARAMETER_LABELS.get(label.strip().casefold())
                if normalised is None:
                    unrecognised.append(label)
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    continue
                numeric_value = float(value)
                if not math.isfinite(numeric_value):
                    continue
                if normalised == "depth_md_si":
                    source_unit = unit_context.get(label)
                    if source_unit is None:
                        source_unit = unit_context.get(label.strip().casefold())
                    depth_in_metres = _depth_parameter_to_si(numeric_value, source_unit)
                    if depth_in_metres is None:
                        depth_unit_issues.append(
                            f"{label} ({source_unit if source_unit else 'unit missing'})"
                        )
                        continue
                    numeric_value = depth_in_metres
                    units[normalised] = "m"
                    source_units[normalised] = str(source_unit)
                elif label in unit_context:
                    units[normalised] = unit_context[label]
                promoted[normalised] = numeric_value
                if record.id not in source_records:
                    source_records.append(record.id)
        if unrecognised:
            report.not_promoted.append(
                {
                    "record_type": "parameter_set",
                    "reason": (
                        "labels without a registered engineering meaning are kept as evidence only: "
                        + ", ".join(sorted(set(unrecognised))[:20])
                    ),
                }
            )
        if depth_unit_issues:
            report.not_promoted.append(
                {
                    "record_type": "parameter_set",
                    "reason": (
                        "explicit measured-depth parameters with missing or unsupported units are kept "
                        "as evidence only: "
                        + ", ".join(sorted(set(depth_unit_issues))[:20])
                    ),
                }
            )
            report.warnings.append(
                "one or more explicit measured-depth parameters were not promoted because their units "
                "were missing or unsupported"
            )
        if not promoted:
            return
        if dry_run:
            report.twin_aspects.append(
                {"aspect": "drilling_parameters", "state_kind": "actual", "dry_run": "true"}
            )
            return
        # Only an explicitly labelled MD parameter with a supported unit reaches this SI field.
        # A generic ``depth`` label remains evidence and cannot overwrite measured progress.
        reported_depth = promoted.get("depth_md_si")
        if isinstance(reported_depth, (int, float)) and document.section_id:
            from drillai.db.models import WellSection

            section = (
                await self.session.execute(
                    select(WellSection).where(
                        WellSection.id == document.section_id,
                        WellSection.org_id == self.org_id,
                        WellSection.wellbore_id == document.wellbore_id,
                    )
                )
            ).scalar_one_or_none()
            progress_floor = max(
                section.current_md_si or 0.0,
                section.actual_bottom_md_si or 0.0,
            ) if section is not None else 0.0
            if section is not None and reported_depth > progress_floor:
                section.current_md_si = float(reported_depth)
                report.twin_aspects.append(
                    {"aspect": "well_section.current_md", "state_kind": "measured", "id": section.id}
                )
        revision = AspectRevision(
            aspect="schedule",
            state_kind=StateKind.CURRENT,
            payload={
                "drilling_parameters": promoted,
                "unit_context": units,
                "source_unit_context": source_units,
            },
            summary="drilling parameters reported in the latest document",
            data_quality="extracted",
            computed_by="import",
            source_refs=[document.id],
            evidence_refs=source_records[:20],
            assumptions=[
                "parameters are as reported on the rig floor; no sensor validation applied",
                "explicit measured-depth units were converted to metres before promotion",
            ],
        )
        aspect = await self.twin.write_aspect(well_id=document.well_id, revision=revision)
        report.twin_aspects.append({"aspect": "schedule", "state_kind": "current", "id": aspect.id})
        for record in records:
            if record.id not in source_records:
                continue
            report.records_promoted.append(record.id)
            # A record reported as promoted must say *where* it went, otherwise the document →
            # record → twin chain breaks in the UI at the last hop. A record already promoted into
            # an operation keeps that target; the twin revision is recorded as its second use.
            if record.promoted_to_id is None:
                record.promoted_to_kind = "twin_aspect"
                record.promoted_to_id = aspect.id

    async def _promote_depth(
        self,
        document: Document,
        records: list[ExtractedRecord],
        report: ProcessingReport,
        *,
        dry_run: bool,
    ) -> None:
        rows = [record for record in records if record.record_type == "depth_reading"]
        if not rows:
            return
        payload: dict[str, Any] = {}
        for record in rows:
            payload.update({key: value for key, value in (record.payload or {}).items() if value is not None})
        if not payload:
            return
        if dry_run:
            report.twin_aspects.append({"aspect": "schedule", "state_kind": "actual", "dry_run": "true"})
            return
        revision = AspectRevision(
            aspect="schedule",
            state_kind=StateKind.ACTUAL,
            payload=payload,
            summary="depth readings from the report",
            data_quality="extracted",
            computed_by="import",
            source_refs=[document.id],
            evidence_refs=[row.id for row in rows][:20],
            valid_from=document.period_start,
        )
        aspect = await self.twin.write_aspect(well_id=document.well_id, revision=revision)
        report.twin_aspects.append({"aspect": "schedule", "state_kind": "actual", "id": aspect.id})
        # Depth also advances the section's current MD, which is what progress is computed from.
        depth_md = payload.get("md_si")
        if depth_md is None:
            depth_md = payload.get("depth_md_si")
        if _valid_nonnegative_number(depth_md) and document.section_id:
            from drillai.db.models import WellSection

            section = (
                await self.session.execute(
                    select(WellSection).where(
                        WellSection.id == document.section_id,
                        WellSection.org_id == self.org_id,
                        WellSection.wellbore_id == document.wellbore_id,
                    )
                )
            ).scalar_one_or_none()
            progress_floor = max(
                section.current_md_si or 0.0,
                section.actual_bottom_md_si or 0.0,
            ) if section is not None else 0.0
            if section is not None and float(depth_md) > progress_floor:
                section.current_md_si = float(depth_md)
                report.twin_aspects.append(
                    {"aspect": "well_section.current_md", "state_kind": "actual", "id": section.id}
                )

    async def _promote_surveys(
        self,
        document: Document,
        records: list[ExtractedRecord],
        report: ProcessingReport,
        *,
        dry_run: bool,
    ) -> None:
        """Promote only complete, finite survey stations and keep retries idempotent."""
        rows = [record for record in records if record.record_type == "survey_station"]
        if not rows:
            return
        if not document.wellbore_id:
            for record in rows:
                report.records_needing_review.append(record.id)
                report.not_promoted.append(
                    {
                        "record_id": record.id,
                        "record_type": "survey_station",
                        "reason": "document is not associated with a wellbore, so stations cannot be placed",
                    }
                )
            return

        valid_rows: list[tuple[ExtractedRecord, dict[str, float | None]]] = []
        for record in rows:
            payload = record.payload or {}
            values: dict[str, float | None] = {}
            invalid: list[str] = []
            for field_name in ("md_si", "inclination_deg", "azimuth_deg"):
                value = payload.get(field_name)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    invalid.append(f"missing or non-numeric {field_name}")
                    continue
                numeric = float(value)
                if not math.isfinite(numeric):
                    invalid.append(f"non-finite {field_name}")
                    continue
                values[field_name] = numeric
            if "md_si" in values and values["md_si"] <= 0:
                invalid.append("md_si must be greater than zero")
            if "inclination_deg" in values and not 0 <= values["inclination_deg"] <= 180:
                invalid.append("inclination_deg must be between 0 and 180 degrees")
            if "azimuth_deg" in values and not 0 <= values["azimuth_deg"] <= 360:
                invalid.append("azimuth_deg must be between 0 and 360 degrees")
            tvd = payload.get("tvd_si")
            if tvd is not None:
                if isinstance(tvd, bool) or not isinstance(tvd, (int, float)) or not math.isfinite(float(tvd)):
                    invalid.append("tvd_si must be finite when supplied")
                else:
                    values["tvd_si"] = float(tvd)
            if invalid:
                report.records_needing_review.append(record.id)
                report.not_promoted.append(
                    {
                        "record_id": record.id,
                        "record_type": "survey_station",
                        "reason": "invalid survey station; " + "; ".join(invalid) + "; queued for human review",
                    }
                )
                continue
            values.setdefault("tvd_si", None)
            valid_rows.append((record, values))

        if not valid_rows:
            return

        # Serialize station-index allocation and the read-before-write idempotency check across
        # different source documents for the same wellbore on PostgreSQL. The document row itself is
        # separately locked in _document() to serialize retries of a single source.
        wellbore = (
            await self.session.execute(
                select(Wellbore)
                .where(
                    Wellbore.id == document.wellbore_id,
                    Wellbore.org_id == self.org_id,
                    Wellbore.well_id == document.well_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if wellbore is None:
            for record, _values in valid_rows:
                report.records_needing_review.append(record.id)
                report.not_promoted.append(
                    {
                        "record_id": record.id,
                        "record_type": "survey_station",
                        "reason": "document wellbore does not belong to this organization and well",
                    }
                )
            return

        record_ids = [record.id for record, _values in valid_rows]
        existing_stations = (
            await self.session.execute(
                select(TrajectoryStation)
                .join(Trajectory, Trajectory.id == TrajectoryStation.trajectory_id)
                .where(
                    TrajectoryStation.org_id == self.org_id,
                    TrajectoryStation.wellbore_id == document.wellbore_id,
                    TrajectoryStation.evidence_ref.in_(record_ids),
                    Trajectory.org_id == self.org_id,
                    Trajectory.wellbore_id == document.wellbore_id,
                )
                .order_by(TrajectoryStation.created_at, TrajectoryStation.id)
            )
        ).scalars().all()
        existing_by_record: dict[str, TrajectoryStation] = {}
        for station in existing_stations:
            if station.evidence_ref:
                existing_by_record.setdefault(station.evidence_ref, station)

        if not dry_run:
            for record, _values in valid_rows:
                existing_station = existing_by_record.get(record.id)
                if existing_station is not None:
                    record.promoted_to_kind = "trajectory_station"
                    record.promoted_to_id = existing_station.id
                    if record.validation_state != "human_validated":
                        record.validation_state = "rule_validated"
                        record.validation_rule = PROMOTION_RULE
            if existing_by_record:
                report.warnings.append(
                    f"reused {len(existing_by_record)} existing survey station(s) on retry; no duplicate stations were created"
                )

        new_rows = [
            (record, values)
            for record, values in valid_rows
            if record.id not in existing_by_record
        ]
        if dry_run:
            report.trajectory_stations_created = len(new_rows)
            return
        if not new_rows:
            return

        trajectory = (
            await self.session.execute(
                select(Trajectory)
                .where(
                    Trajectory.org_id == self.org_id,
                    Trajectory.wellbore_id == document.wellbore_id,
                    Trajectory.kind == "actual",
                )
                .order_by(Trajectory.is_current.desc(), Trajectory.version.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalars().first()
        if trajectory is None or not trajectory.is_current:
            version = (trajectory.version + 1) if trajectory is not None else 1
            trajectory = Trajectory(
                org_id=self.org_id,
                wellbore_id=document.wellbore_id,
                kind="actual",
                name=f"Actual trajectory (from {document.title or document.id})",
                source_document_id=document.id,
                is_current=True,
                version=version,
                created_by=self.actor_id or "ddr_processor",
                validation={
                    "is_valid": True,
                    "issues": [
                        {
                            "severity": "warning",
                            "code": "not_independently_validated",
                            "message": (
                                "stations transcribed from a report; positional accuracy depends on "
                                "the survey programme and has not been independently verified"
                            ),
                        }
                    ],
                },
                notes=(
                    "stations transcribed from a report; positional accuracy depends on the survey "
                    "programme and has not been independently verified"
                ),
            )
            self.session.add(trajectory)
            await self.session.flush()

        existing_indexes = {
            int(index)
            for index in (
                await self.session.execute(
                    select(TrajectoryStation.station_index).where(
                        TrajectoryStation.org_id == self.org_id,
                        TrajectoryStation.trajectory_id == trajectory.id,
                        TrajectoryStation.wellbore_id == document.wellbore_id,
                    )
                )
            ).scalars().all()
        }
        next_index = max(existing_indexes, default=-1) + 1
        report_date = self._report_date(document, records)
        for record, values in new_rows:
            while next_index in existing_indexes:
                next_index += 1
            station = TrajectoryStation(
                org_id=self.org_id,
                trajectory_id=trajectory.id,
                wellbore_id=document.wellbore_id,
                station_index=next_index,
                md_si=values["md_si"],
                inclination_deg=values["inclination_deg"],
                azimuth_deg=values["azimuth_deg"],
                tvd_si=values["tvd_si"],
                source="report",
                measured_at=(
                    ensure_utc(record.observed_at or report_date)
                    if record.observed_at is not None or report_date is not None
                    else None
                ),
                evidence_ref=record.id,
                raw_source={"record_id": record.id, "payload": record.payload or {}},
            )
            self.session.add(station)
            await self.session.flush()
            existing_indexes.add(next_index)
            next_index += 1
            await self._link_evidence(
                subject_kind="trajectory_station",
                subject_id=station.id,
                record=record,
                document=document,
                note="survey station promoted from report",
            )
            record.promoted_to_kind = "trajectory_station"
            record.promoted_to_id = station.id
            if record.validation_state != "human_validated":
                record.validation_state = "rule_validated"
                record.validation_rule = PROMOTION_RULE
            report.records_promoted.append(record.id)
        stations = (
            await self.session.execute(
                select(TrajectoryStation)
                .where(
                    TrajectoryStation.org_id == self.org_id,
                    TrajectoryStation.trajectory_id == trajectory.id,
                    TrajectoryStation.wellbore_id == document.wellbore_id,
                )
                .order_by(TrajectoryStation.station_index)
            )
        ).scalars().all()
        if stations:
            deepest = max(stations, key=lambda station: (station.md_si, station.station_index))
            trajectory.station_count = len(stations)
            trajectory.total_md_si = deepest.md_si
            trajectory.total_tvd_si = deepest.tvd_si
            trajectory.max_inclination_deg = max(station.inclination_deg for station in stations)
        report.trajectory_stations_created = len(new_rows)

    # ------------------------------------------------------------------ entry point

    async def process(
        self,
        document_id: str,
        *,
        dry_run: bool = False,
        doc_type: str | None = None,
        _identity_retry: bool = True,
    ) -> ProcessingReport:
        document = await self._document(document_id)
        effective_type = doc_type or document.doc_type or "other"
        report = ProcessingReport(
            document_id=document.id, well_id=document.well_id, doc_type=effective_type, dry_run=dry_run
        )
        if document.well_id is None:
            report.warnings.append(
                "document is not associated with a well: operations, events and twin updates are "
                "skipped because a well is required for structured drilling data"
            )
            return report
        if effective_type not in {"ddr", "drilling_document"}:
            report.warnings.append(
                f"document type {effective_type!r} is not a daily drilling report; structured "
                "drilling promotion runs only for ddr/drilling_document"
            )
            return report

        records = await self._records(document.id)
        eligible_records: list[ExtractedRecord] = []
        ineligible_record_ids: set[str] = set()
        for record in records:
            state = record.validation_state
            if state not in PROMOTABLE_VALIDATION_STATES:
                ineligible_record_ids.add(record.id)
                report.not_promoted.append(
                    {
                        "record_id": record.id,
                        "record_type": record.record_type,
                        "reason": f"validation state {state!r} is not eligible for promotion",
                    }
                )
                continue
            confidence = record.confidence
            invalid_confidence = False
            if confidence is not None:
                if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                    invalid_confidence = True
                else:
                    try:
                        numeric_confidence = float(confidence)
                    except (OverflowError, ValueError):
                        invalid_confidence = True
                    else:
                        invalid_confidence = not math.isfinite(numeric_confidence) or not (
                            0.0 <= numeric_confidence <= 1.0
                        )
            if invalid_confidence:
                ineligible_record_ids.add(record.id)
                report.records_needing_review.append(record.id)
                report.not_promoted.append(
                    {
                        "record_id": record.id,
                        "record_type": record.record_type,
                        "reason": (
                            f"confidence {confidence!r} is invalid; expected a finite value from 0 to 1"
                        ),
                    }
                )
                continue
            if state != "human_validated" and confidence is None:
                ineligible_record_ids.add(record.id)
                report.records_needing_review.append(record.id)
                report.not_promoted.append(
                    {
                        "record_id": record.id,
                        "record_type": record.record_type,
                        "reason": "confidence is missing or non-finite; queued for human review",
                    }
                )
                continue
            if state != "human_validated" and float(confidence) < MIN_PROMOTION_CONFIDENCE:
                ineligible_record_ids.add(record.id)
                report.records_needing_review.append(record.id)
                report.not_promoted.append(
                    {
                        "record_id": record.id,
                        "record_type": record.record_type,
                        "reason": (
                            f"confidence {confidence} below promotion threshold "
                            f"{MIN_PROMOTION_CONFIDENCE}; queued for human review instead"
                        ),
                    }
                )
                continue
            payload = record.payload or {}
            invalid_duration_fields = [
                field_name
                for field_name in ("duration_hours", "npt_hours")
                if payload.get(field_name) is not None and not _valid_report_duration(payload[field_name])
            ]
            if invalid_duration_fields:
                ineligible_record_ids.add(record.id)
                report.records_needing_review.append(record.id)
                report.not_promoted.append(
                    {
                        "record_id": record.id,
                        "record_type": record.record_type,
                        "reason": (
                            "invalid duration field(s) "
                            + ", ".join(invalid_duration_fields)
                            + "; expected finite values from 0 up to but not including 100 hours"
                        ),
                    }
                )
                continue
            eligible_records.append(record)
        await self._warn_existing_promotions_for_ineligible_records(
            document, ineligible_record_ids, report
        )
        records = eligible_records
        if not records:
            if report.not_promoted:
                report.warnings.append("no extracted records passed validation-state and confidence gates")
            else:
                report.warnings.append("document has no extracted records to promote")
            return report

        well = (
            await self.session.execute(
                select(Well).where(Well.id == document.well_id, Well.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if well is None:
            raise NotFound("well not found for document", details={"well_id": document.well_id})

        try:
            operations = await self._promote_operations(document, records, report, dry_run=dry_run)
            await self._promote_npt_events(document, records, operations, report, dry_run=dry_run)
            await self._promote_mud_properties(document, records, report, dry_run=dry_run)
            await self._promote_drilling_parameters(document, records, report, dry_run=dry_run)
            await self._promote_depth(document, records, report, dry_run=dry_run)
            await self._promote_surveys(document, records, report, dry_run=dry_run)
            for record in records:
                if record.record_type == "casing_cement_line":
                    report.not_promoted.append(
                        {
                            "record_id": record.id,
                            "record_type": record.record_type,
                            "reason": (
                                "casing/cement lines are retained as evidence; structured casing "
                                "design promotion is not implemented"
                            ),
                        }
                    )
                elif record.record_type == "report_header":
                    report.not_promoted.append(
                        {
                            "record_id": record.id,
                            "record_type": record.record_type,
                            "reason": (
                                "header metadata is merged into the document during ingestion; "
                                "well-identity twin promotion is not implemented"
                            ),
                        }
                    )
            if dry_run:
                # Nothing was written, so nothing is recorded as having been written. A dry run that
                # left a ledger entry behind would make the ledger claim a promotion that never
                # happened.
                await self.session.rollback()
            else:
                document.attributes = {
                    **(document.attributes or {}),
                    "ddr_processing": report.to_dict(),
                }
                await self.session.flush()
                # One entry for the run, not one per promoted row: the run is the act a person
                # authorised, and every row it created carries `source_document_id` +
                # `source_record_id` + `promotion_fingerprint`, which is how the entry is traced to
                # them. Per-row entries would also make a 400-row report write 400 ledger rows.
                await record_audit(
                    self.session,
                    org_id=self.org_id,
                    action="document.process",
                    resource_kind="document",
                    resource_id=document.id,
                    principal=self._principal,
                    project_id=document.project_id,
                    well_id=document.well_id,
                    details={
                        "doc_type": effective_type,
                        "operations_created": len(report.operations_created),
                        "operations_linked": len(report.operations_linked),
                        "events_created": len(report.events_created),
                        "events_updated": len(report.events_updated),
                        "records_promoted": len(report.records_promoted),
                        "records_needing_review": len(report.records_needing_review),
                        "twin_aspects": len(report.twin_aspects),
                        "npt_hours_classified": round(report.npt_hours_classified, 3),
                        "promotion_rule": PROMOTION_RULE,
                    },
                )
                await self.session.flush()
        except IntegrityError as exc:
            # PostgreSQL serializes this source on the locked Document row. The unique index is a
            # second line of defense for SQLite and for any writer that did not acquire that lock.
            # Once the winner commits, re-read the document's rows once and produce the normal linked
            # retry result instead of returning a duplicate-key failure to the caller.
            await self.session.rollback()
            if _identity_retry and _is_promotion_identity_conflict(exc):
                return await self.process(
                    document_id,
                    dry_run=dry_run,
                    doc_type=doc_type,
                    _identity_retry=False,
                )
            raise
        except Exception:
            if dry_run:
                await self.session.rollback()
            raise
        return report
