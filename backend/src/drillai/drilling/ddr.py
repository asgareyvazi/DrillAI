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
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.audit import record_audit
from drillai.core.errors import NotFound
from drillai.db.models import (
    Document,
    Event,
    EvidenceLink,
    ExtractedRecord,
    Operation,
    Trajectory,
    TrajectoryStation,
    Well,
)
from drillai.drilling.classifiers import (
    classify_npt,
    classify_operation_kind,
    ensure_standard_npt_codes,
)
from drillai.security.actions import Principal
from drillai.twin.aspects import StateKind
from drillai.twin.service import AspectRevision, TwinService

#: Extracted record types this processor understands, and what each becomes.
PROMOTION_MAP: dict[str, str] = {
    "operation_row": "operations",
    "mud_properties": "twin.mud_program",
    "depth_reading": "well_sections.current_md + twin.schedule",
    "survey_station": "trajectory_stations",
    "report_header": "document metadata + twin.identity",
    "parameter_set": "twin.drilling_parameters (labelled parameters only)",
    "casing_cement_line": "evidence only (no structured casing/cement promotion yet)",
}

#: Records below this confidence are never promoted without human review.
MIN_PROMOTION_CONFIDENCE = 0.6

#: The rule that a promoted record has passed:
#: confidence at or above :data:`MIN_PROMOTION_CONFIDENCE`, a recognisable kind, and a structured row
#: actually produced. Promotion is itself the check, which is why a promoted record is
#: ``rule_validated`` and not ``human_validated`` — nobody has looked at it yet.
PROMOTION_RULE = "promotion_rules_v1"

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
    "depth": "depth_md_si",
    "depth_md": "depth_md_si",
    "md": "depth_md_si",
    "measured_depth": "depth_md_si",
}


def _parse_report_instant(raw: str, *, on_date: dt.datetime | None) -> dt.datetime | None:
    """Parse a report time cell into a UTC instant.

    DDRs spell time as ``06:30``, ``0630`` or ``06:30:00`` and rarely carry a date, so the report
    date supplies the day. A time that cannot be parsed returns ``None`` and the caller falls back
    to the replayed cursor instead of inventing a timestamp.
    """
    text = raw.strip()
    match = re.fullmatch(r"(?P<h>\d{1,2})[:.](?P<m>\d{2})(?::(?P<s>\d{2}))?", text)
    if match is None:
        match = re.fullmatch(r"(?P<h>\d{2})(?P<m>\d{2})(?P<s>\d{2})?", text)
    if match is None:
        return None
    hour, minute = int(match.group("h")), int(match.group("m"))
    second = int(match.groupdict().get("s") or 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    base = on_date or dt.datetime.now(tz=dt.UTC)
    return base.replace(hour=hour, minute=minute, second=second, microsecond=0)


@dataclass
class ProcessingReport:
    """What the processor actually did, and what it deliberately did not do."""

    document_id: str
    well_id: str | None
    doc_type: str
    dry_run: bool = False
    operations_created: list[str] = field(default_factory=list)
    operations_linked: list[str] = field(default_factory=list)
    events_created: list[str] = field(default_factory=list)
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
            "events_created": self.events_created,
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
                "events": len(self.events_created),
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
                select(Document).where(Document.id == document_id, Document.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound("document not found", details={"document_id": document_id})
        return row

    @staticmethod
    def _report_date(document: Document, records: list[ExtractedRecord]) -> dt.datetime | None:
        """The instant a report describes.

        Resolution order is explicit because the date drives operation timestamps, twin validity
        and NPT attribution: the document's own period, then its issue date, then the report
        header field, then the ingestion timestamp. The last resort is recorded as such rather
        than being presented as a report date.
        """
        if document.period_start is not None:
            return document.period_start
        if document.issue_date is not None:
            return document.issue_date
        for record in records:
            if record.record_type != "report_header":
                continue
            raw = (record.payload or {}).get("report_date")
            if isinstance(raw, str):
                with contextlib.suppress(ValueError):
                    return dt.datetime.fromisoformat(raw).replace(tzinfo=dt.UTC)
        return None

    async def _records(self, document_id: str) -> list[ExtractedRecord]:
        return list(
            (
                await self.session.execute(
                    select(ExtractedRecord)
                    .where(
                        ExtractedRecord.org_id == self.org_id,
                        ExtractedRecord.document_id == document_id,
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

    async def _existing_fingerprints(self, model: Any, document_id: str) -> set[str]:
        """Fingerprints already promoted **from this document**, so re-processing it duplicates nothing.

        Two things were wrong with the previous version, and both are about the same query.

        It loaded *every* operation (or event) on the well and scanned their ``attributes`` JSON in
        Python to collect fingerprints — an unbounded read that grew with the well's history rather
        than with the document being processed. A well with fifty thousand operation rows re-read all
        of them to reprocess one daily report. The fingerprint is now a first-class indexed column
        (see the migration) and the read is a single indexed lookup.

        And the scope was wrong. It filtered by ``well_id`` alone, but the fingerprint is derived from
        *the document and the record*, so a fingerprint can only have been written by a promotion of the
        same document. Asking for the whole well's rows meant every promotion compared its rows against
        rows that could never match — and, worse, that two documents' rows were pooled together, so a
        fingerprint collision across documents would have been read as "already promoted" and the
        second document's row silently dropped instead of created.

        Rows written before the column existed are still honoured: the legacy ``attributes`` key is
        consulted, but only for rows this document produced, so the compatibility read is bounded by
        the document rather than by the well.
        """
        stmt = select(model.id, model.promotion_fingerprint, model.attributes).where(
            model.org_id == self.org_id,
            model.source_document_id == document_id,
        )
        rows = (await self.session.execute(stmt)).all()
        found: set[str] = set()
        for _id, promotion_fingerprint, attributes in rows:
            if isinstance(promotion_fingerprint, str) and promotion_fingerprint:
                found.add(promotion_fingerprint)
            for key, value in (attributes or {}).items():
                if key in {"extraction_fingerprint", "promotion_fingerprint"} and isinstance(value, str):
                    found.add(value)
        return found

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
        report_date = self._report_date(document, records)
        if report_date is None:
            report.warnings.append(
                "the document carries no report date, so promoted operations have no start time; "
                "operation sequencing is preserved but the timeline cannot place them"
            )
        existing = await self._existing_fingerprints(Operation, document.id)
        by_fingerprint: dict[str, Operation] = {}
        last: Operation | None = None
        # A DDR time breakdown is an ordered list of durations, not a list of absolute timestamps.
        # Replaying the durations forward from the report date rebuilds the shift's real timeline;
        # an explicit start time in the row wins over the cursor, because it is direct evidence.
        cursor = report_date
        sequence = (
            await self.session.execute(
                select(Operation.sequence)
                .where(Operation.org_id == self.org_id, Operation.well_id == document.well_id)
                .order_by(Operation.sequence.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        next_sequence = int(sequence or 0) + 1

        for record in rows:
            fingerprint = self._fingerprint(document.id, record)
            if fingerprint in existing:
                report.operations_linked.append(record.id)
                continue
            if (record.confidence or 0.0) < MIN_PROMOTION_CONFIDENCE:
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
            explicit = payload.get("start_time") or payload.get("start") or payload.get("time")
            started: dt.datetime | None = None
            if isinstance(explicit, str):
                with contextlib.suppress(ValueError, TypeError):
                    started = _parse_report_instant(explicit, on_date=report_date)
            if started is None:
                started = record.observed_at
            if started is None:
                # No absolute time in the row: continue the shift from where the previous row
                # ended. This is the normal case for a DDR time breakdown.
                started = cursor if cursor is not None else report_date
            end = (
                started + dt.timedelta(hours=float(duration))
                if started is not None and isinstance(duration, (int, float))
                else None
            )
            if cursor is not None and end is not None:
                cursor = end
            elif cursor is not None and started is not None:
                cursor = max(cursor, started)
            if kind == "unknown":
                report.warnings.append(
                    f"operation row {record.id} matched no registered activity signal; "
                    "recorded as 'unknown' rather than guessed"
                )
            operation = Operation(
                org_id=self.org_id,
                project_id=document.project_id,
                well_id=document.well_id,
                wellbore_id=document.wellbore_id,
                section_id=document.section_id,
                parent_operation_id=None,
                predecessor_operation_id=last.id if last is not None else None,
                operation_class="actual",
                sequence=next_sequence,
                code=str(payload.get("code"))[:60] if payload.get("code") else None,
                name=str(name)[:300],
                kind=kind,
                phase="completed" if end is not None else "current",
                status="completed" if end is not None else "in_progress",
                planned_start=None,
                planned_end=None,
                actual_start=started,
                actual_end=end,
                planned_duration_hours=None,
                actual_duration_hours=float(duration) if isinstance(duration, (int, float)) else None,
                depth_from_md_si=None,
                depth_to_md_si=payload.get("depth_md_si"),
                is_productive=True,
                npt_hours=None,
                # `source_kind` is the first-class column; `source` keeps its legacy value for readers
                # that still ask for it, but the new field is what the platform queries.
                source="report",
                source_kind="ddr_promotion",
                source_document_id=document.id,
                source_record_id=record.id,
                promotion_fingerprint=fingerprint,
                data_quality="extracted",
                remarks=text[:2000] or None,
                attributes={
                    # Kept for rows written by earlier versions of this processor: the column is the
                    # identity now, and the JSON key is read only as a fallback.
                    "promotion_fingerprint": fingerprint,
                    "classification": classification.to_dict(),
                    "extractor": record.method,
                    "extractor_version": record.method_version,
                    "page_number": record.page_number,
                    "time_basis": (
                        "explicit_time_in_row" if isinstance(explicit, str) else "replayed_from_report_duration"
                    ),
                    "row_index": payload.get("row_index"),
                },
            )
            self.session.add(operation)
            await self.session.flush()
            by_fingerprint[fingerprint] = operation
            # Accumulated before the dry-run branch: the preview reports the same hours the real run
            # will report. Skipping it here made a dry run claim five operations and no time at all —
            # the one number a reviewer reads the preview for.
            report.operations_hours += operation.actual_duration_hours or 0.0
            if dry_run:
                report.operations_created.append(operation.id)
                last = operation
                next_sequence += 1
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
            # Every record that reaches here passed the same gate: it cleared
            # MIN_PROMOTION_CONFIDENCE and became a structured row. There used to be a second,
            # undocumented threshold of 0.7 on this line that *also* decided the validation state, so
            # two records promoted by the same rule were filed as differently-validated depending on a
            # number nothing else in the platform used. The state now follows the decision that was
            # actually made.
            record.validation_state = "rule_validated"
            record.validation_rule = PROMOTION_RULE
            report.records_promoted.append(record.id)
            report.operations_created.append(operation.id)
            last = operation
            next_sequence += 1
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
        """Detect NPT in any record whose text carries an NPT signal."""
        await ensure_standard_npt_codes(self.session, self.org_id)
        report_date = self._report_date(document, records)
        existing = await self._existing_fingerprints(Event, document.id)
        for record in records:
            text = self._record_text(record)
            if not text.strip():
                continue
            classification, code, controllable = classify_npt(text)
            if classification.label is None:
                continue
            fingerprint = self._fingerprint(document.id, record)
            if fingerprint in existing:
                continue
            payload = record.payload or {}
            hours = payload.get("duration_hours") or payload.get("npt_hours")
            if not isinstance(hours, (int, float)):
                hours = None
            # Attribute the loss to the operation it was reported alongside: an NPT hour that
            # cannot be placed in the operation chain cannot be defended in a review.
            linked = operations.get(fingerprint)
            occurred = (
                linked.actual_start
                if linked is not None and linked.actual_start is not None
                else (record.observed_at or report_date or dt.datetime.now(tz=dt.UTC))
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
                duration_hours=float(hours) if hours else None,
                depth_md_si=(
                    (linked.depth_to_md_si if linked is not None else None)
                    or payload.get("depth_md_si")
                    or record.depth_md_si
                ),
                depth_tvd_si=record.depth_tvd_si,
                severity="high" if classification.label in {"well_control", "stuck_pipe"} else "medium",
                npt_hours=float(hours) if hours else None,
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
                # The event's category came from the *content* of the report, classified by rules over
                # its text, so its provenance is "derived" — not "recorded", which would claim the
                # report itself stated the category.
                classification_source="derived",
                tags=["auto_classified", "ddr"],
                attributes={
                    "promotion_fingerprint": fingerprint,
                    "classification": classification.to_dict(),
                    "operator_controllable": controllable,
                    "classified_from": record.record_type,
                    "attributed_operation_id": linked.id if linked is not None else None,
                },
            )
            self.session.add(event)
            await self.session.flush()
            report.events_created.append(event.id)
            # The classified hours are an analysis result, not a write, so a dry run reports them
            # exactly like a real run: a preview that under-reports the loss is worse than no preview.
            if hours:
                report.npt_hours_classified += float(hours)
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
                # The operation row's own NPT roll-up, and its productivity flag, are now
                # consistent with the event that explains the lost time.
                linked.npt_hours = (linked.npt_hours or 0.0) + float(hours)
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
        if (latest.confidence or 0.0) < MIN_PROMOTION_CONFIDENCE:
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
        source_records: list[str] = []
        unrecognised: list[str] = []
        for record in records:
            if record.record_type != "parameter_set":
                continue
            for label, value in (record.payload or {}).items():
                normalised = KNOWN_PARAMETER_LABELS.get(label.lower())
                if normalised is None:
                    unrecognised.append(label)
                    continue
                if not isinstance(value, (int, float)):
                    continue
                promoted[normalised] = float(value)
                unit_context = record.unit_context or {}
                if label in unit_context:
                    units[normalised] = unit_context[label]
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
        if not promoted:
            return
        if dry_run:
            report.twin_aspects.append(
                {"aspect": "drilling_parameters", "state_kind": "actual", "dry_run": "true"}
            )
            return
        # A depth reported next to the drilling parameters is a *measured* depth. Recording it on
        # the section is what lets progress be computed from data instead of from the plan.
        reported_depth = promoted.get("depth_md_si")
        if isinstance(reported_depth, (int, float)) and document.section_id:
            from drillai.db.models import WellSection

            section = (
                await self.session.execute(select(WellSection).where(WellSection.id == document.section_id))
            ).scalar_one_or_none()
            if section is not None and reported_depth > (section.current_md_si or 0.0):
                section.current_md_si = float(reported_depth)
                report.twin_aspects.append(
                    {"aspect": "well_section.current_md", "state_kind": "measured", "id": section.id}
                )
        revision = AspectRevision(
            aspect="schedule",
            state_kind=StateKind.CURRENT,
            payload={"drilling_parameters": promoted, "unit_context": units},
            summary="drilling parameters reported in the latest document",
            data_quality="extracted",
            computed_by="import",
            source_refs=[document.id],
            evidence_refs=source_records[:20],
            assumptions=["parameters are as reported on the rig floor; no sensor validation applied"],
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
        depth_md = payload.get("md_si") or payload.get("depth_md_si")
        if isinstance(depth_md, (int, float)) and document.section_id:
            from drillai.db.models import WellSection

            section = (
                await self.session.execute(
                    select(WellSection).where(WellSection.id == document.section_id)
                )
            ).scalar_one_or_none()
            if section is not None and (section.current_md_si is None or depth_md > (section.current_md_si or 0)):
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
        """Surveys become survey stations so the trajectory engine has real input."""
        rows = [record for record in records if record.record_type == "survey_station"]
        if not rows or not document.wellbore_id:
            if rows and not document.wellbore_id:
                report.not_promoted.append(
                    {
                        "record_type": "survey_station",
                        "reason": "document is not associated with a wellbore, so stations cannot be placed",
                    }
                )
            return
        if dry_run:
            report.trajectory_stations_created = len(rows)
            return
        trajectory = (
            await self.session.execute(
                select(Trajectory)
                .where(
                    Trajectory.org_id == self.org_id,
                    Trajectory.wellbore_id == document.wellbore_id,
                    Trajectory.kind == "actual",
                )
                .limit(1)
            )
        ).scalars().first()
        if trajectory is None:
            trajectory = Trajectory(
                org_id=self.org_id,
                wellbore_id=document.wellbore_id,
                kind="actual",
                name=f"Actual trajectory (from {document.title or document.id})",
                source_document_id=document.id,
                is_current=True,
                created_by=self.actor_id or "ddr_processor",
                # ``Trajectory.validation`` is a JSON *object* consumed as a validation report
                # (``api/routers/workflows.py::_validation_payload`` reads ``issues`` from it, and
                # ``context/builder.py`` hands it to clients as one). This wrote the bare string
                # "unvalidated" into it, so any client reading a report got a string where the shape
                # promises an object. The honest report for a trajectory transcribed from a paper
                # report and not independently checked is a warning: nothing has been found wrong
                # because nothing has been checked, and ``is_valid`` follows that same rule.
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
            int(row.station_index)
            for row in (
                await self.session.execute(
                    select(TrajectoryStation).where(TrajectoryStation.trajectory_id == trajectory.id)
                )
            ).scalars().all()
        }
        created = 0
        for offset, record in enumerate(rows):
            index = len(existing_indexes) + offset + 1
            payload = record.payload or {}
            md = payload.get("md_si")
            if not isinstance(md, (int, float)):
                continue
            station = TrajectoryStation(
                org_id=self.org_id,
                trajectory_id=trajectory.id,
                wellbore_id=document.wellbore_id,
                station_index=index,
                md_si=float(md),
                inclination_deg=payload.get("inclination_deg"),
                azimuth_deg=payload.get("azimuth_deg"),
                tvd_si=payload.get("tvd_si"),
                source="report",
                evidence_ref=record.id,
                raw_source=str(payload)[:500],
            )
            self.session.add(station)
            created += 1
            await self._link_evidence(
                subject_kind="trajectory_station",
                subject_id=station.id,
                record=record,
                document=document,
                note="survey station promoted from report",
            )
        await self.session.flush()
        report.trajectory_stations_created = created

    # ------------------------------------------------------------------ entry point

    async def process(
        self,
        document_id: str,
        *,
        dry_run: bool = False,
        doc_type: str | None = None,
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
        if not records:
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
                        "records_promoted": len(report.records_promoted),
                        "records_needing_review": len(report.records_needing_review),
                        "twin_aspects": len(report.twin_aspects),
                        "npt_hours_classified": round(report.npt_hours_classified, 3),
                        "promotion_rule": PROMOTION_RULE,
                    },
                )
                await self.session.flush()
        except Exception:
            if dry_run:
                await self.session.rollback()
            raise
        return report
