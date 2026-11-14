"""Governed daily drilling report lifecycle.

The database models hold typed, source-aware data. This service owns the rules the schema alone
cannot express: well/wellbore hierarchy, IANA reporting zones, dimensional unit conversion, revision
immutability, compare-and-swap edits, review separation, source freshness, tenant authorization and
one-transaction audit/history writes. HTTP routers are deliberately thin callers of this service.

A report's local ``report_date`` and named timezone are identity, not an inferred UTC midnight. Its
period bounds are explicit instants; no activity or observation time is synthesized from either bound
or from ingestion time. Manual measurements keep the entered number/unit beside the canonical value.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import and_, delete, exists, func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.audit import record_audit
from drillai.core.clock import ensure_utc, utc_now
from drillai.core.errors import Conflict, NotFound, PermissionDenied, UnitError, ValidationFailed
from drillai.core.serialization import content_hash
from drillai.db.models import (
    DAILY_REPORT_PARAMETER_SPECS,
    REPORT_ACTIVITY_KINDS,
    REPORT_DEPTH_REFERENCES,
    REPORT_RECONCILIATION_IDENTITY_FIELDS,
    REPORT_RECONCILIATION_REVISION_FIELDS,
    REPORT_REVISION_STATUSES,
    REPORT_SHIFT_CODES,
    REPORT_TIME_BASES,
    DailyDrillingReport,
    DailyDrillingReportRevision,
    DailyReportActivity,
    DailyReportMeasurement,
    DailyReportReconciliation,
    Document,
    DocumentRegion,
    Event,
    ExtractedRecord,
    Operation,
    Project,
    RawArtifact,
    Well,
    Wellbore,
)
from drillai.db.models.operations import CAUSE_BASES, NPT_CATEGORIES, OPERATION_KINDS, canonical_npt_category
from drillai.documents.access import document_not_quarantined_condition, operational_source_conditions
from drillai.documents.scope import (
    DocumentScope,
    DocumentScopeResolver,
    validate_source_record,
    validate_source_scope,
)
from drillai.drilling.classifiers import classify_npt, classify_operation_kind
from drillai.security.actions import Principal, authorize
from drillai.telemetry.outbox import emit
from drillai.twin.service import ChangeEvent, TwinService
from drillai.units.registry import convert, get_unit

__all__ = [
    "REVISION_TRANSITIONS",
    "DailyReportService",
]

REVISION_TRANSITIONS: dict[str, frozenset[str]] = {
    "draft": frozenset({"submitted", "withdrawn"}),
    "submitted": frozenset({"approved", "rejected", "withdrawn"}),
    "approved": frozenset(),
    "rejected": frozenset(),
    "withdrawn": frozenset(),
}

_REVISION_HISTORY_SOURCES = frozenset({"approved", "rejected", "withdrawn"})
_MAX_ACTIVITIES = 500
_MAX_MEASUREMENTS = 500
_MAX_REASON = 2000
_MAX_CLASSIFICATION_SOURCE_CHARS = 20_000
_NPT_DURATION_METRIC_BASES = frozenset({"observed", "reported", "calculated"})
_NPT_DURATION_EXCLUDED_BASES = ("planned", "inferred", "unknown")
_PERIOD_INTEGRITY_NOTE = (
    "Reports are grouped by wellbore and local report date. Within one date, reports are ordered only by distinct "
    "explicit period-start instants; every report in that date group must have a latest-approved revision with a "
    "verified fingerprint and both bounds. Consecutive date groups are compared only when their explicit period "
    "starts preserve date order. Unapproved, unverified, incomplete, ambiguous, and truncated-boundary reports "
    "remain unassessed and are never skipped to bridge intervals. A gap or overlap is only a difference between "
    "reported boundary instants; it does not prove activity coverage, actual operations, physical downtime, or "
    "report reconciliation. Results select at most the requested number of report identities by local report "
    "date, with truncation reported."
)


@dataclass(frozen=True)
class _ResolvedWell:
    project_id: str
    well_id: str
    wellbore_id: str


@dataclass(frozen=True)
class _PreparedContent:
    revision_fields: dict[str, Any]
    activities: list[dict[str, Any]]
    measurements: list[dict[str, Any]]


class DailyReportService:
    """Tenant-scoped, authorized, auditable DDR operations."""

    def __init__(
        self,
        session: AsyncSession,
        org_id: str,
        *,
        principal: Principal | None,
        request_id: str | None = None,
    ) -> None:
        self.session = session
        self.org_id = org_id
        self.principal = principal
        self.request_id = request_id

    # ------------------------------------------------------------------ authority and scope

    def _authorize(self, action: str, *, well_id: str | None = None, report_id: str | None = None) -> None:
        principal = self.principal
        if principal is None or principal.is_anonymous:
            raise PermissionDenied("an authenticated principal is required for daily report access")
        if not self.org_id or principal.org_id != self.org_id:
            raise PermissionDenied(
                "daily report organization does not match the authenticated principal",
                details={"principal_org_id": principal.org_id, "service_org_id": self.org_id},
            )
        authorize(
            principal,
            action,
            context={"well_id": well_id, "daily_report_id": report_id, "org_id": self.org_id},
        )

    async def _resolve_well(self, well_id: str, wellbore_id: str) -> _ResolvedWell:
        resolved = await DocumentScopeResolver(self.session, self.org_id).resolve(
            DocumentScope(well_id=well_id, wellbore_id=wellbore_id)
        )
        if resolved.well_id != well_id or resolved.wellbore_id != wellbore_id or not resolved.project_id:
            raise ValidationFailed(
                "daily reports require a wellbore belonging to the path well",
                details={"well_id": well_id, "wellbore_id": wellbore_id, "resolved_scope": resolved.as_dict()},
            )
        return _ResolvedWell(project_id=resolved.project_id, well_id=well_id, wellbore_id=wellbore_id)

    async def _require_source_section_scope(self, scope: _ResolvedWell, section_id: str | None) -> None:
        """A report may cite a section-scoped source only inside its exact wellbore hierarchy.

        Daily-report identities are wellbore-scoped, while an imported source document can be narrower.
        Keep that narrower provenance intact: validate the source's recorded section against the same
        project, well and wellbore instead of treating ``section_id=None`` as a wildcard or dropping it.
        """
        if section_id is None:
            return
        resolved = await DocumentScopeResolver(self.session, self.org_id).resolve(
            DocumentScope(
                project_id=scope.project_id,
                well_id=scope.well_id,
                wellbore_id=scope.wellbore_id,
                section_id=section_id,
            )
        )
        if (
            resolved.project_id != scope.project_id
            or resolved.well_id != scope.well_id
            or resolved.wellbore_id != scope.wellbore_id
            or resolved.section_id != section_id
        ):
            raise ValidationFailed(
                "source section scope does not match the owning wellbore",
                details={"section_id": section_id, "well_id": scope.well_id, "wellbore_id": scope.wellbore_id},
            )

    async def _require_well(self, well_id: str) -> Project:
        """Check that a well exists in this organization before list/read queries."""
        well = (
            await self.session.execute(
                select(Well).where(Well.id == well_id, Well.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if well is None:
            raise NotFound(f"well {well_id!r} not found")
        project = (
            await self.session.execute(
                select(Project).where(Project.id == well.project_id, Project.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if project is None:
            # An orphaned well must not make a report list look complete or be used as a report scope.
            raise NotFound(f"well {well_id!r} not found")
        return project

    async def _report(self, well_id: str, report_id: str, *, lock: bool = False) -> DailyDrillingReport:
        stmt = select(DailyDrillingReport).where(
            DailyDrillingReport.id == report_id,
            DailyDrillingReport.org_id == self.org_id,
            DailyDrillingReport.well_id == well_id,
        )
        if lock:
            stmt = stmt.with_for_update()
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFound(f"daily report {report_id!r} not found")
        await self._resolve_well(well_id, row.wellbore_id)
        return row

    async def _revision(
        self,
        report_id: str,
        revision_number: int,
        *,
        lock: bool = False,
    ) -> DailyDrillingReportRevision:
        stmt = select(DailyDrillingReportRevision).where(
            DailyDrillingReportRevision.report_id == report_id,
            DailyDrillingReportRevision.revision_number == revision_number,
            DailyDrillingReportRevision.org_id == self.org_id,
        )
        if lock:
            stmt = stmt.with_for_update()
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            raise NotFound(f"daily report revision {revision_number} was not found")
        return row

    # ------------------------------------------------------------------ reads

    async def preview_activity_classification(
        self,
        well_id: str,
        *,
        wellbore_id: str,
        source_record_id: str,
        source_document_id: str | None = None,
    ) -> dict[str, Any]:
        """Suggest rule-based activity labels from one current, in-scope extracted record.

        This is a read-only aid: it does not alter the source record or create an activity. The
        extracted text remains attributed to its document and validation state, and every label is
        accompanied by the exact matched text and deterministic rule that produced it.
        """
        self._authorize("daily_report.read", well_id=well_id)
        self._authorize("document.read", well_id=well_id)
        await self._require_well(well_id)
        scope = await self._resolve_well(well_id, wellbore_id)
        evidence = await self._validate_evidence(
            scope,
            source_document_id=source_document_id,
            source_record_id=source_record_id,
            allow_unvalidated_record=True,
        )
        record = (
            await self.session.execute(
                select(ExtractedRecord).where(
                    ExtractedRecord.id == evidence["source_record_id"],
                    ExtractedRecord.org_id == self.org_id,
                )
            )
        ).scalar_one_or_none()
        if record is None:
            raise NotFound(f"source record {source_record_id!r} not found")

        payload = record.payload if isinstance(record.payload, dict) else {}
        text_parts = [value for value in payload.values() if isinstance(value, str)]
        source_text = " | ".join(text_parts) if text_parts else str(payload)
        source_text_truncated = len(source_text) > _MAX_CLASSIFICATION_SOURCE_CHARS
        if source_text_truncated:
            source_text = source_text[:_MAX_CLASSIFICATION_SOURCE_CHARS]
        operation = classify_operation_kind(source_text)
        npt, npt_code, npt_controllable = classify_npt(source_text)
        operation_kind = operation.label if operation.label in OPERATION_KINDS else None
        npt_category = npt.label if npt.label in NPT_CATEGORIES else None

        return {
            "source_document_id": evidence["source_document_id"],
            "source_record_id": record.id,
            "source_region_id": evidence["source_region_id"],
            "source_page_number": evidence["source_page_number"],
            "source_record_type": record.record_type,
            "source_validation_state": record.validation_state,
            "source_review_version": record.review_version,
            "source_fingerprint": record.fingerprint,
            "source_extraction_method": record.method,
            "source_extraction_method_version": record.method_version,
            "method": "deterministic_content_rules",
            "source_text_truncated": source_text_truncated,
            "notice": (
                "Rule matches are review aids, not calibrated accuracy estimates or verified field facts. "
                "Check the cited source and select a label manually."
            ),
            "operation": {
                "kind": operation_kind,
                "matched_text": operation.matched_text if operation_kind else None,
                "matched_pattern": operation.matched_pattern if operation_kind else None,
                "heuristic_score": operation.confidence if operation_kind else 0.0,
            },
            "npt": {
                "category": npt_category,
                "code": npt_code if npt_category else None,
                "matched_text": npt.matched_text if npt_category else None,
                "matched_pattern": npt.matched_pattern if npt_category else None,
                "heuristic_score": npt.confidence if npt_category else 0.0,
                "is_operator_controllable": npt_controllable if npt_category else None,
            },
        }

    async def list_reports(
        self,
        well_id: str,
        *,
        report_date_from: dt.date | None = None,
        report_date_to: dt.date | None = None,
        shift_code: str | None = None,
        status: str | None = None,
        report_number: str | None = None,
        wellbore_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._authorize("daily_report.read", well_id=well_id)
        await self._require_well(well_id)
        self._validate_list_filters(report_date_from, report_date_to, shift_code, status, limit, offset)
        if report_number is not None:
            report_number = _nonempty_text(report_number, "report_number", 120)
        if wellbore_id is not None:
            wellbore_id = _nonempty_text(wellbore_id, "wellbore_id", 128)
            await self._resolve_well(well_id, wellbore_id)

        stmt = (
            select(DailyDrillingReport)
            .join(Wellbore, Wellbore.id == DailyDrillingReport.wellbore_id)
            .where(
                DailyDrillingReport.org_id == self.org_id,
                DailyDrillingReport.well_id == well_id,
                Wellbore.org_id == self.org_id,
                Wellbore.well_id == well_id,
            )
        )
        latest_match = exists(
            select(DailyDrillingReportRevision.id).where(
                DailyDrillingReportRevision.org_id == self.org_id,
                DailyDrillingReportRevision.report_id == DailyDrillingReport.id,
                DailyDrillingReportRevision.revision_number == DailyDrillingReport.latest_revision_number,
                DailyDrillingReportRevision.status == status,
            )
        )
        if report_date_from is not None:
            stmt = stmt.where(DailyDrillingReport.report_date >= report_date_from)
        if report_date_to is not None:
            stmt = stmt.where(DailyDrillingReport.report_date <= report_date_to)
        if shift_code is not None:
            stmt = stmt.where(DailyDrillingReport.shift_code == shift_code)
        if report_number is not None:
            stmt = stmt.where(DailyDrillingReport.report_number == report_number)
        if wellbore_id is not None:
            stmt = stmt.where(DailyDrillingReport.wellbore_id == wellbore_id)
        if status is not None:
            stmt = stmt.where(latest_match)
        total = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(stmt.order_by(None).subquery())
                )
            ).scalar_one()
        )
        reports = (
            await self.session.execute(
                stmt.order_by(
                    DailyDrillingReport.report_date.desc(),
                    DailyDrillingReport.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        if not reports:
            return {"items": [], "total": total, "limit": limit, "offset": offset}

        revisions = (
            await self.session.execute(
                select(DailyDrillingReportRevision).where(
                    DailyDrillingReportRevision.org_id == self.org_id,
                    DailyDrillingReportRevision.report_id.in_([row.id for row in reports]),
                )
            )
        ).scalars().all()
        by_key = {(row.report_id, row.revision_number): row for row in revisions}
        return {
            "items": [
                self._report_out(row, by_key.get((row.id, row.latest_revision_number)))
                for row in reports
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    async def period_integrity(
        self,
        well_id: str,
        *,
        wellbore_id: str | None = None,
        report_date_from: dt.date | None = None,
        report_date_to: dt.date | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Compare explicit reporting-period bounds from fingerprint-verified approved revisions.

        This is a bounded data-quality diagnostic, not a claim that all activity or physical downtime
        is covered. Revisions with absent/unknown boundaries remain unassessed and are never placed at
        a guessed time or bridged into an asserted full-coverage interval.
        """
        self._authorize("daily_report.read", well_id=well_id)
        await self._require_well(well_id)
        if wellbore_id is not None:
            await self._resolve_well(well_id, wellbore_id)
        self._validate_list_filters(report_date_from, report_date_to, None, None, limit, 0)

        stmt = (
            select(DailyDrillingReport)
            .join(
                Wellbore,
                and_(
                    Wellbore.id == DailyDrillingReport.wellbore_id,
                    Wellbore.org_id == self.org_id,
                    Wellbore.well_id == well_id,
                ),
            )
            .where(
                DailyDrillingReport.org_id == self.org_id,
                DailyDrillingReport.well_id == well_id,
            )
        )
        if wellbore_id is not None:
            stmt = stmt.where(DailyDrillingReport.wellbore_id == wellbore_id)
        if report_date_from is not None:
            stmt = stmt.where(DailyDrillingReport.report_date >= report_date_from)
        if report_date_to is not None:
            stmt = stmt.where(DailyDrillingReport.report_date <= report_date_to)
        total = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(stmt.order_by(None).subquery())
                )
            ).scalar_one()
        )
        reports = (
            await self.session.execute(
                stmt.order_by(DailyDrillingReport.report_date.desc(), DailyDrillingReport.id.desc()).limit(limit)
            )
        ).scalars().all()
        if not reports:
            return {
                "well_id": well_id,
                "wellbore_id": wellbore_id,
                "items": [],
                "comparisons": [],
                "total": total,
                "limit": limit,
                "truncated": total > 0,
                "bounded_period_count": 0,
                "unassessed_period_count": 0,
                "unassessed_comparison_count": 0,
                "comparison_method": "approved_explicit_periods_by_report_date_and_start_v2",
                "note": _PERIOD_INTEGRITY_NOTE,
            }

        approved_numbers = {row.id: row.latest_approved_revision_number for row in reports}
        approved_revision_keys = [
            (report.id, revision_number)
            for report in reports
            if (revision_number := approved_numbers[report.id]) is not None
        ]
        revisions: list[DailyDrillingReportRevision] = []
        if approved_revision_keys:
            revisions = (
                await self.session.execute(
                    select(DailyDrillingReportRevision).where(
                        DailyDrillingReportRevision.org_id == self.org_id,
                        tuple_(
                            DailyDrillingReportRevision.report_id,
                            DailyDrillingReportRevision.revision_number,
                        ).in_(approved_revision_keys),
                    )
                )
            ).scalars().all()
        revision_by_key = {(row.report_id, row.revision_number): row for row in revisions}
        approved_pairs = [
            (report, revision_by_key.get((report.id, revision_number)))
            for report in reports
            if (revision_number := approved_numbers[report.id]) is not None
        ]
        approved_revisions = [revision for _report, revision in approved_pairs if revision is not None]
        revision_ids = [revision.id for revision in approved_revisions]
        activities_by_revision: dict[str, list[DailyReportActivity]] = {}
        measurements_by_revision: dict[str, list[DailyReportMeasurement]] = {}
        if revision_ids:
            activity_rows = (
                await self.session.execute(
                    select(DailyReportActivity)
                    .where(
                        DailyReportActivity.org_id == self.org_id,
                        DailyReportActivity.revision_id.in_(revision_ids),
                    )
                    .order_by(
                        DailyReportActivity.revision_id.asc(),
                        DailyReportActivity.sequence.asc(),
                        DailyReportActivity.id.asc(),
                    )
                )
            ).scalars().all()
            measurement_rows = (
                await self.session.execute(
                    select(DailyReportMeasurement)
                    .where(
                        DailyReportMeasurement.org_id == self.org_id,
                        DailyReportMeasurement.revision_id.in_(revision_ids),
                    )
                    .order_by(
                        DailyReportMeasurement.revision_id.asc(),
                        DailyReportMeasurement.parameter_key.asc(),
                        DailyReportMeasurement.ordinal.asc(),
                    )
                )
            ).scalars().all()
            for row in activity_rows:
                activities_by_revision.setdefault(row.revision_id, []).append(row)
            for row in measurement_rows:
                measurements_by_revision.setdefault(row.revision_id, []).append(row)

        items: list[dict[str, Any]] = []
        report_by_wellbore: dict[str, list[DailyDrillingReport]] = {}
        revision_by_report: dict[str, DailyDrillingReportRevision | None] = {}
        state_by_report: dict[str, str] = {}
        for report in reports:
            report_by_wellbore.setdefault(report.wellbore_id, []).append(report)
            revision_number = approved_numbers[report.id]
            revision = revision_by_key.get((report.id, revision_number)) if revision_number is not None else None
            item: dict[str, Any] = {
                "report_id": report.id,
                "wellbore_id": report.wellbore_id,
                "report_date": report.report_date.isoformat(),
                "report_timezone": report.report_timezone,
                "shift_code": report.shift_code,
                "shift_label": report.shift_label,
                "report_number": report.report_number,
                "approved_revision_number": revision_number,
                "revision_status": revision.status if revision is not None else None,
                "revision_content_hash": revision.content_hash if revision is not None else None,
                "revision_fingerprint_verified": False,
                "period_start_at": None,
                "period_end_at": None,
                "period_state": "no_approved_revision" if revision_number is None else "approved_revision_unavailable",
            }
            if revision is not None:
                if revision.status != "approved":
                    item["period_state"] = "approved_revision_status_mismatch"
                else:
                    revision_activities = activities_by_revision.get(revision.id, [])
                    revision_measurements = measurements_by_revision.get(revision.id, [])
                    fingerprint_verified = bool(
                        revision.content_hash
                        and revision.content_hash
                        == self._content_hash(report, revision, revision_activities, revision_measurements)
                    )
                    item["revision_fingerprint_verified"] = fingerprint_verified
                    if not fingerprint_verified:
                        item["period_state"] = "fingerprint_mismatch"
                    else:
                        item["period_start_at"] = _iso(revision.period_start_at)
                        item["period_end_at"] = _iso(revision.period_end_at)
                        if revision.period_start_at is None and revision.period_end_at is None:
                            item["period_state"] = "missing_both_bounds"
                        elif revision.period_start_at is None:
                            item["period_state"] = "missing_start_bound"
                        elif revision.period_end_at is None:
                            item["period_state"] = "missing_end_bound"
                        else:
                            item["period_state"] = "bounded"
            revision_by_report[report.id] = revision
            state_by_report[report.id] = item["period_state"]
            items.append(item)

        comparisons: list[dict[str, Any]] = []
        unassessed_comparison_count = 0
        truncated = total > len(reports)
        for scoped_wellbore_id, identities in sorted(report_by_wellbore.items()):
            identities_by_date: dict[dt.date, list[DailyDrillingReport]] = {}
            for report in identities:
                identities_by_date.setdefault(report.report_date, []).append(report)
            ordered_dates = sorted(identities_by_date)
            if not ordered_dates:
                continue
            oldest_selected_date = ordered_dates[0]
            groups: dict[dt.date, tuple[list[DailyDrillingReport], bool, bool]] = {}
            for report_date in ordered_dates:
                date_identities = identities_by_date[report_date]
                date_revisions = [revision_by_report[report.id] for report in date_identities]
                orderable = all(
                    state_by_report[report.id] == "bounded"
                    and revision is not None
                    and revision.period_start_at is not None
                    and revision.period_end_at is not None
                    for report, revision in zip(date_identities, date_revisions, strict=True)
                )
                if orderable:
                    start_instants = [revision.period_start_at for revision in date_revisions if revision is not None]
                    orderable = len(set(start_instants)) == len(start_instants)
                if orderable:
                    date_identities = sorted(
                        date_identities,
                        key=lambda report: (revision_by_report[report.id].period_start_at, report.id),
                    )
                else:
                    # Stable but non-authoritative ordering is used only to count unassessed candidate pairs.
                    date_identities = sorted(date_identities, key=lambda report: report.id)
                groups[report_date] = (
                    date_identities,
                    orderable,
                    truncated and report_date == oldest_selected_date,
                )

            def add_comparison(
                previous_report: DailyDrillingReport,
                next_report: DailyDrillingReport,
                *,
                ordering_basis: str,
                wellbore_id: str = scoped_wellbore_id,
            ) -> None:
                previous_revision = revision_by_report[previous_report.id]
                next_revision = revision_by_report[next_report.id]
                assert previous_revision is not None and previous_revision.period_end_at is not None
                assert next_revision is not None and next_revision.period_start_at is not None
                delta_seconds = (next_revision.period_start_at - previous_revision.period_end_at).total_seconds()
                relationship = "gap" if delta_seconds > 0 else "overlap" if delta_seconds < 0 else "contiguous"
                comparisons.append(
                    {
                        "wellbore_id": wellbore_id,
                        "ordering_basis": ordering_basis,
                        "previous_report_id": previous_report.id,
                        "previous_report_date": previous_report.report_date.isoformat(),
                        "previous_report_number": previous_report.report_number,
                        "previous_revision_number": previous_revision.revision_number,
                        "previous_period_end_at": _iso(previous_revision.period_end_at),
                        "next_report_id": next_report.id,
                        "next_report_date": next_report.report_date.isoformat(),
                        "next_report_number": next_report.report_number,
                        "next_revision_number": next_revision.revision_number,
                        "next_period_start_at": _iso(next_revision.period_start_at),
                        "relationship": relationship,
                        "gap_seconds": delta_seconds if delta_seconds > 0 else None,
                        "overlap_seconds": -delta_seconds if delta_seconds < 0 else None,
                        "unit": "s",
                    }
                )

            for date_index, report_date in enumerate(ordered_dates):
                date_identities, orderable, truncated_boundary = groups[report_date]
                if not orderable or truncated_boundary:
                    unassessed_comparison_count += max(0, len(date_identities) - 1)
                else:
                    for previous_report, next_report in pairwise(date_identities):
                        add_comparison(
                            previous_report,
                            next_report,
                            ordering_basis="explicit_period_start_at_within_report_date_v1",
                        )

                if date_index + 1 == len(ordered_dates):
                    continue
                next_date = ordered_dates[date_index + 1]
                previous_group, previous_orderable, previous_truncated = groups[report_date]
                next_group, next_orderable, next_truncated = groups[next_date]
                if (
                    not previous_orderable
                    or not next_orderable
                    or previous_truncated
                    or next_truncated
                ):
                    unassessed_comparison_count += 1
                    continue
                previous_report = previous_group[-1]
                next_report = next_group[0]
                previous_revision = revision_by_report[previous_report.id]
                next_revision = revision_by_report[next_report.id]
                assert previous_revision is not None and previous_revision.period_start_at is not None
                assert next_revision is not None and next_revision.period_start_at is not None
                if previous_revision.period_start_at >= next_revision.period_start_at:
                    # Conflicting date and explicit-start order is ambiguous, so neither report is moved or skipped.
                    unassessed_comparison_count += 1
                    continue
                add_comparison(
                    previous_report,
                    next_report,
                    ordering_basis="consecutive_report_dates_with_explicit_start_order_v1",
                )

        bounded_count = sum(item["period_state"] == "bounded" for item in items)
        return {
            "well_id": well_id,
            "wellbore_id": wellbore_id,
            "items": items,
            "comparisons": comparisons,
            "total": total,
            "limit": limit,
            "truncated": truncated,
            "bounded_period_count": bounded_count,
            "unassessed_period_count": len(items) - bounded_count,
            "unassessed_comparison_count": unassessed_comparison_count,
            "comparison_method": "approved_explicit_periods_by_report_date_and_start_v2",
            "note": _PERIOD_INTEGRITY_NOTE,
        }

    async def get_report(self, well_id: str, report_id: str) -> dict[str, Any]:
        self._authorize("daily_report.read", well_id=well_id, report_id=report_id)
        await self._require_well(well_id)
        report = await self._report(well_id, report_id)
        latest = await self._revision(report.id, report.latest_revision_number)
        approved = (
            await self.session.execute(
                select(DailyDrillingReportRevision).where(
                    DailyDrillingReportRevision.org_id == self.org_id,
                    DailyDrillingReportRevision.report_id == report.id,
                    DailyDrillingReportRevision.revision_number == report.latest_approved_revision_number,
                )
            )
        ).scalar_one_or_none() if report.latest_approved_revision_number is not None else None
        latest_detail = await self._revision_detail(report, latest)
        return {
            "report": self._report_out(report, latest),
            "latest_revision": latest_detail,
            "latest_approved_revision": self._revision_summary(approved) if approved else None,
        }

    async def get_revision(self, well_id: str, report_id: str, revision_number: int) -> dict[str, Any]:
        self._authorize("daily_report.read", well_id=well_id, report_id=report_id)
        await self._require_well(well_id)
        report = await self._report(well_id, report_id)
        revision = await self._revision(report.id, revision_number)
        latest = await self._revision(report.id, report.latest_revision_number)
        return {"report": self._report_out(report, latest), "revision": await self._revision_detail(report, revision)}

    async def activity_metrics(self, well_id: str, report_id: str, revision_number: int) -> dict[str, Any]:
        """Summarize only explicit, fingerprint-verified NPT durations for one revision.

        Planned, inferred, and unknown durations are counted as exclusions. Missing evidence is never
        presented as zero hours, and the result is an accounting summary—not measured physical downtime.
        """
        self._authorize("daily_report.read", well_id=well_id, report_id=report_id)
        await self._require_well(well_id)
        report = await self._report(well_id, report_id)
        revision = await self._revision(report.id, revision_number)
        detail = await self._revision_detail(report, revision)
        if detail["content_hash_verified"] is not True:
            raise Conflict("daily-report activity metrics require a verified revision fingerprint")

        npt_rows = [row for row in detail["activities"] if row["activity_kind"] == "npt"]
        included = [
            row for row in npt_rows
            if row["duration_seconds"] is not None and row["duration_basis"] in _NPT_DURATION_METRIC_BASES
        ]
        excluded_by_basis = dict.fromkeys(_NPT_DURATION_EXCLUDED_BASES, 0)
        for row in npt_rows:
            eligible = row["duration_seconds"] is not None and row["duration_basis"] in _NPT_DURATION_METRIC_BASES
            if not eligible:
                basis = row["duration_basis"]
                excluded_by_basis[basis if basis in excluded_by_basis else "unknown"] += 1

        if not npt_rows:
            coverage = "not_applicable"
        elif not included:
            coverage = "unavailable"
        elif len(included) < len(npt_rows):
            coverage = "partial"
        else:
            coverage = "complete"

        return {
            "well_id": report.well_id,
            "wellbore_id": report.wellbore_id,
            "report_id": report.id,
            "report_date": report.report_date.isoformat(),
            "report_timezone": report.report_timezone,
            "revision_id": revision.id,
            "revision_number": revision.revision_number,
            "revision_edit_version": revision.edit_version,
            "revision_status": revision.status,
            "revision_source_kind": revision.source_kind,
            "revision_content_hash": revision.content_hash,
            "period_start_at": _iso(revision.period_start_at),
            "period_end_at": _iso(revision.period_end_at),
            "activity_count": len(detail["activities"]),
            "npt_activity_count": len(npt_rows),
            "npt_duration_included_count": len(included),
            "npt_duration_excluded_count": len(npt_rows) - len(included),
            "npt_duration_excluded_by_basis": excluded_by_basis,
            "npt_duration_total": {
                "value": math.fsum(row["duration_seconds"] for row in included) if included else None,
                "unit": "s",
            },
            "npt_duration_coverage": coverage,
            "calculation_method": "sum_explicit_npt_durations_v1",
            "note": (
                "Only stored durations with observed, reported, or calculated basis are summed. "
                "Planned, inferred, and unknown durations are excluded; no time is derived from activity "
                "endpoints or report-period bounds. This is not a measurement of physical downtime and does "
                "not approve or reconcile any report row."
            ),
        }

    async def history(
        self,
        well_id: str,
        report_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._authorize("daily_report.read", well_id=well_id, report_id=report_id)
        await self._require_well(well_id)
        report = await self._report(well_id, report_id)
        if not 1 <= limit <= 500 or offset < 0:
            raise ValidationFailed("history limit must be 1..500 and offset must be non-negative")
        base = select(DailyDrillingReportRevision).where(
            DailyDrillingReportRevision.org_id == self.org_id,
            DailyDrillingReportRevision.report_id == report.id,
        )
        total = int(
            (await self.session.execute(select(func.count()).select_from(base.order_by(None).subquery()))).scalar_one()
        )
        rows = (
            await self.session.execute(
                base.order_by(DailyDrillingReportRevision.revision_number.desc()).limit(limit).offset(offset)
            )
        ).scalars().all()
        latest = await self._revision(report.id, report.latest_revision_number)
        return {
            "report": self._report_out(report, latest),
            "items": [self._revision_summary(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    # ------------------------------------------------------------------ report creation and editing

    async def create_manual(self, well_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._authorize("daily_report.create", well_id=well_id)
        wellbore_id = _nonempty_text(payload.get("wellbore_id"), "wellbore_id", 64)
        scope = await self._resolve_well(well_id, wellbore_id)
        identity = self._normalize_identity(payload)
        content = await self._prepare_content(
            payload.get("content"),
            scope=scope,
            source_kind="manual",
            allow_unvalidated_source_records=False,
        )
        duplicate = (
            await self.session.execute(
                select(DailyDrillingReport).where(
                    DailyDrillingReport.org_id == self.org_id,
                    DailyDrillingReport.wellbore_id == scope.wellbore_id,
                    DailyDrillingReport.report_date == identity["report_date"],
                    DailyDrillingReport.identity_key == identity["identity_key"],
                )
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise Conflict(
                "a daily report already exists for this wellbore, local date and report identity",
                details={"report_id": duplicate.id, "revision_number": duplicate.latest_revision_number},
            )

        report = DailyDrillingReport(
            org_id=self.org_id,
            well_id=scope.well_id,
            wellbore_id=scope.wellbore_id,
            created_by=self.principal.id if self.principal else "",
            latest_revision_number=1,
            **identity,
        )
        try:
            # Keep the initial revision in the same savepoint as the report: the report's latest-revision
            # pointer is a deferred FK and must not be released before its target exists.
            async with self.session.begin_nested():
                self.session.add(report)
                await self.session.flush()
                revision = DailyDrillingReportRevision(
                    org_id=self.org_id,
                    report_id=report.id,
                    revision_number=1,
                    edit_version=1,
                    status="draft",
                    source_kind="manual",
                    created_by=self.principal.id if self.principal else "",
                    **content.revision_fields,
                )
                self.session.add(revision)
                await self.session.flush()
        except IntegrityError as exc:
            if not _is_named_constraint(exc, "uq_daily_drilling_report_identity"):
                raise
            existing = (
                await self.session.execute(
                    select(DailyDrillingReport).where(
                        DailyDrillingReport.org_id == self.org_id,
                        DailyDrillingReport.wellbore_id == scope.wellbore_id,
                        DailyDrillingReport.report_date == identity["report_date"],
                        DailyDrillingReport.identity_key == identity["identity_key"],
                    )
                )
            ).scalar_one_or_none()
            raise Conflict(
                "a daily report was created concurrently for this wellbore, local date and report identity",
                details={"report_id": existing.id if existing else None},
            ) from exc
        activities = self._activity_rows(revision.id, content.activities, resolution_state="manual")
        measurements = self._measurement_rows(revision.id, content.measurements, resolution_state="manual")
        self.session.add_all([*activities, *measurements])
        await self.session.flush()
        revision.content_hash = self._content_hash(report, revision, activities, measurements)
        await self.session.flush()

        after = self._revision_audit_state(revision, activities, measurements)
        await self._record_change(
            report,
            revision,
            action="daily_report.create",
            change_type="created",
            permission="daily_report.write",
            fields=["identity", "content", "status"],
            before={},
            after=after,
            reason=None,
        )
        await emit(
            self.session,
            org_id=self.org_id,
            type="daily_report.changed",
            subject_kind="daily_drilling_report",
            subject_id=report.id,
            well_id=report.well_id,
            wellbore_id=report.wellbore_id,
            payload={"report_id": report.id, "revision_number": 1, "status": "draft", "action": "created"},
        )
        return {
            "report": self._report_out(report, revision),
            "revision": await self._revision_detail(report, revision, activities, measurements),
        }

    async def create_import(self, well_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a proposed report revision from explicitly mapped document evidence.

        This operation never validates extracted records or accepts their content. It creates a
        normal report draft, retains source links, and persists proposed hashes and versions so a
        separate human reconciliation step can accept, correct, reject or leave each mapped field/row
        unresolved before submission.
        """
        self._authorize("daily_report.import", well_id=well_id)
        wellbore_id = _nonempty_text(payload.get("wellbore_id"), "wellbore_id", 64)
        source_document_id = _nonempty_text(payload.get("source_document_id"), "source_document_id", 64)
        scope = await self._resolve_well(well_id, wellbore_id)
        identity = self._normalize_identity(payload)
        source_document = await self._validate_evidence(
            scope,
            source_document_id=source_document_id,
            source_record_id=payload.get("source_record_id"),
            source_region_id=payload.get("source_region_id"),
            source_page_number=payload.get("source_page_number"),
            allow_unvalidated_record=True,
        )
        document = (
            await self.session.execute(
                select(Document).where(
                    Document.id == source_document["source_document_id"],
                    Document.org_id == self.org_id,
                )
            )
        ).scalar_one_or_none()
        if document is None:
            raise NotFound(f"source document {source_document_id!r} not found")
        if document.doc_type not in {"ddr", "drilling_document"}:
            raise ValidationFailed(
                "only documents classified as daily drilling reports can seed an imported report draft",
                details={"source_document_id": document.id, "doc_type": document.doc_type},
            )
        if document.well_id != well_id or document.wellbore_id not in {None, wellbore_id}:
            raise ValidationFailed(
                "source document scope must match the report well and wellbore",
                details={"source_document_id": document.id, "well_id": document.well_id, "wellbore_id": document.wellbore_id},
            )

        content_input = payload.get("content")
        if not isinstance(content_input, dict):
            raise ValidationFailed("content must be an object")
        content_input = dict(content_input)
        supplied_source_document = content_input.get("source_document_id")
        if supplied_source_document not in {None, source_document_id}:
            raise ValidationFailed("content source_document_id must match the import source document")
        content_input["source_document_id"] = source_document_id
        if source_document["source_record_id"] is not None:
            content_input.setdefault("source_record_id", source_document["source_record_id"])
        if source_document["source_region_id"] is not None:
            content_input.setdefault("source_region_id", source_document["source_region_id"])
        if source_document["source_page_number"] is not None:
            content_input.setdefault("source_page_number", source_document["source_page_number"])
        prepared = await self._prepare_content(
            content_input,
            scope=scope,
            source_kind="import",
            allow_unvalidated_source_records=True,
        )
        for child in (*prepared.activities, *prepared.measurements):
            record_id = child.get("source_record_id")
            if record_id and child.get("source_document_id") != source_document_id:
                raise ValidationFailed(
                    "each imported row's extracted record must belong to the selected source document",
                    details={"source_record_id": record_id, "source_document_id": source_document_id},
                )

        duplicate = (
            await self.session.execute(
                select(DailyDrillingReport).where(
                    DailyDrillingReport.org_id == self.org_id,
                    DailyDrillingReport.wellbore_id == scope.wellbore_id,
                    DailyDrillingReport.report_date == identity["report_date"],
                    DailyDrillingReport.identity_key == identity["identity_key"],
                )
            )
        ).scalar_one_or_none()
        if duplicate is not None:
            raise Conflict(
                "a report with this identity already exists; no duplicate draft was created. Open the existing report and review or correct it through the normal revision lifecycle",
                details={
                    "report_id": duplicate.id,
                    "revision_number": duplicate.latest_revision_number,
                    "source_document_id": source_document_id,
                    "report_date": identity["report_date"].isoformat(),
                    "identity_key": identity["identity_key"],
                },
            )

        report = DailyDrillingReport(
            org_id=self.org_id,
            well_id=scope.well_id,
            wellbore_id=scope.wellbore_id,
            created_by=self.principal.id if self.principal else "",
            latest_revision_number=1,
            **identity,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(report)
                await self.session.flush()
                revision = DailyDrillingReportRevision(
                    org_id=self.org_id,
                    report_id=report.id,
                    revision_number=1,
                    edit_version=1,
                    status="draft",
                    source_kind="import",
                    created_by=self.principal.id if self.principal else "",
                    **prepared.revision_fields,
                )
                self.session.add(revision)
                await self.session.flush()
        except IntegrityError as exc:
            if not _is_named_constraint(exc, "uq_daily_drilling_report_identity"):
                raise
            existing = (
                await self.session.execute(
                    select(DailyDrillingReport).where(
                        DailyDrillingReport.org_id == self.org_id,
                        DailyDrillingReport.wellbore_id == scope.wellbore_id,
                        DailyDrillingReport.report_date == identity["report_date"],
                        DailyDrillingReport.identity_key == identity["identity_key"],
                    )
                )
            ).scalar_one_or_none()
            raise Conflict(
                "another report was created concurrently for this import identity; no duplicate draft was created. Open the existing report and review or correct it through the normal revision lifecycle",
                details={"report_id": existing.id if existing else None, "source_document_id": source_document_id},
            ) from exc
        activities = self._activity_rows(revision.id, prepared.activities, resolution_state="proposed")
        measurements = self._measurement_rows(revision.id, prepared.measurements, resolution_state="proposed")
        # Rows that the operator entered directly in the import editor are manual facts, not extracted
        # proposals. Only rows linked to the selected source record require reconciliation.
        for row in activities:
            if row.source_record_id is None:
                row.resolution_state = "manual"
        for row in measurements:
            if row.source_record_id is None:
                row.resolution_state = "manual"
        self.session.add_all([*activities, *measurements])
        await self.session.flush()

        field_sources = payload.get("field_sources", [])
        if not isinstance(field_sources, list) or len(field_sources) > 32:
            raise ValidationFailed("field_sources must be a list with at most 32 entries")
        seen_fields: set[str] = set()
        decisions: list[DailyReportReconciliation] = []
        for item in field_sources:
            if not isinstance(item, dict):
                raise ValidationFailed("each field source must be an object")
            field_key = _nonempty_text(item.get("field_key"), "field_key", 48)
            is_identity_field = field_key in REPORT_RECONCILIATION_IDENTITY_FIELDS
            if field_key not in REPORT_RECONCILIATION_REVISION_FIELDS and not is_identity_field:
                raise ValidationFailed(
                    "field source target is not a reconcilable report or revision field",
                    details={
                        "field_key": field_key,
                        "allowed": [*REPORT_RECONCILIATION_REVISION_FIELDS, *REPORT_RECONCILIATION_IDENTITY_FIELDS],
                    },
                )
            if field_key in seen_fields:
                raise ValidationFailed("only one source proposal may be mapped to each report field", details={"field_key": field_key})
            seen_fields.add(field_key)
            source_path = _nonempty_text(item.get("source_path"), "source_path", 256)
            if is_identity_field and source_path != f"payload.{field_key}":
                raise ValidationFailed(
                    "report identity sources must map to their matching extracted header field",
                    details={"field_key": field_key, "source_path": source_path},
                )
            record = await self._import_source_record(
                scope,
                source_document_id=source_document_id,
                source_record_id=item.get("source_record_id"),
                expected_record_type="report_header" if is_identity_field else None,
            )
            source_value = _record_payload_path(record.payload or {}, source_path)
            if source_value is _MISSING:
                raise ValidationFailed(
                    "source_path does not identify a value in the extracted record payload",
                    details={"source_record_id": record.id, "source_path": source_path},
                )
            if is_identity_field:
                target_value = (
                    report.report_date.isoformat()
                    if field_key == "report_date"
                    else report.report_number
                )
                if not isinstance(source_value, str) or source_value != target_value:
                    raise ValidationFailed(
                        "a mapped report identity must exactly match its selected report-header evidence",
                        details={"field_key": field_key, "source_record_id": record.id},
                    )
                target_kind = "report_identity"
                target_key = f"identity:{field_key}"
            else:
                target_value = prepared.revision_fields[field_key]
                target_kind = "revision_field"
                target_key = f"field:{field_key}"
            decisions.append(
                self._new_reconciliation(
                    revision=revision,
                    target_key=target_key,
                    target_kind=target_kind,
                    target_id=None,
                    field_key=field_key,
                    source_document_id=source_document_id,
                    source_record=record,
                    source_path=source_path,
                    source_value=source_value,
                    proposed_value=target_value,
                )
            )
        for row in activities:
            if row.source_record_id is None:
                continue
            record = await self._import_source_record(
                scope,
                source_document_id=source_document_id,
                source_record_id=row.source_record_id,
            )
            decisions.append(
                self._new_reconciliation(
                    revision=revision,
                    target_key=f"activity:{record.id}:{row.sequence}",
                    target_kind="activity",
                    target_id=row.id,
                    field_key="activity",
                    source_document_id=source_document_id,
                    source_record=record,
                    source_path="payload",
                    source_value=record.payload or {},
                    proposed_value=self._activity_proposal_value(row),
                )
            )
        for row in measurements:
            if row.source_record_id is None:
                continue
            record = await self._import_source_record(
                scope,
                source_document_id=source_document_id,
                source_record_id=row.source_record_id,
            )
            decisions.append(
                self._new_reconciliation(
                    revision=revision,
                    target_key=f"measurement:{record.id}:{row.parameter_key}:{row.ordinal}",
                    target_kind="measurement",
                    target_id=row.id,
                    field_key=row.parameter_key,
                    source_document_id=source_document_id,
                    source_record=record,
                    source_path="payload",
                    source_value=record.payload or {},
                    proposed_value=self._measurement_proposal_value(row),
                )
            )
        self.session.add_all(decisions)
        revision.content_hash = self._content_hash(report, revision, activities, measurements)
        await self.session.flush()

        after = self._revision_audit_state(revision, activities, measurements)
        after["reconciliation"] = [self._reconciliation_out(row) for row in decisions]
        await self._record_change(
            report,
            revision,
            action="daily_report.import",
            change_type="import_proposed",
            permission="daily_report.write",
            fields=["source_document", "identity", "content", "reconciliation"],
            before={},
            after=after,
            reason="Source extraction is proposed only; no field or row was automatically accepted.",
        )
        await emit(
            self.session,
            org_id=self.org_id,
            type="daily_report.changed",
            subject_kind="daily_drilling_report",
            subject_id=report.id,
            well_id=report.well_id,
            wellbore_id=report.wellbore_id,
            payload={
                "report_id": report.id,
                "revision_number": 1,
                "status": "draft",
                "action": "import_proposed",
                "source_document_id": source_document_id,
            },
        )
        return {
            "report": self._report_out(report, revision),
            "revision": await self._revision_detail(report, revision, activities, measurements),
        }

    async def _import_source_record(
        self,
        scope: _ResolvedWell,
        *,
        source_document_id: str,
        source_record_id: Any,
        expected_record_type: str | None = None,
    ) -> ExtractedRecord:
        record_id = _nonempty_text(source_record_id, "source_record_id", 64)
        record = (
            await self.session.execute(
                select(ExtractedRecord)
                .where(
                    ExtractedRecord.id == record_id,
                    ExtractedRecord.org_id == self.org_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if record is None:
            raise NotFound(f"source record {record_id!r} not found")
        if record.document_id != source_document_id:
            raise ValidationFailed(
                "source record does not belong to the selected import document",
                details={"source_record_id": record.id, "source_document_id": source_document_id},
            )
        if expected_record_type is not None and record.record_type != expected_record_type:
            raise ValidationFailed(
                "the selected extracted record is not a compatible source for this report field",
                details={"source_record_id": record.id, "expected_record_type": expected_record_type},
            )
        if record.validation_state in {"rejected", "superseded"}:
            raise ValidationFailed(
                "rejected or superseded extracted records cannot be imported",
                details={"source_record_id": record.id, "validation_state": record.validation_state},
            )
        await self._validate_evidence(
            scope,
            source_document_id=source_document_id,
            source_record_id=record.id,
            source_region_id=record.region_id,
            source_page_number=record.page_number,
            allow_unvalidated_record=True,
        )
        return record

    @staticmethod
    def _set_reconciliation_target_id(row: DailyReportReconciliation, target_id: str) -> None:
        if row.target_kind == "activity":
            row.activity_target_id = target_id
            row.measurement_target_id = None
        elif row.target_kind == "measurement":
            row.activity_target_id = None
            row.measurement_target_id = target_id
        else:
            raise ValidationFailed("only row reconciliation targets have persisted target IDs")

    def _new_reconciliation(
        self,
        *,
        revision: DailyDrillingReportRevision,
        target_key: str,
        target_kind: str,
        target_id: str | None,
        field_key: str,
        source_document_id: str,
        source_record: ExtractedRecord,
        source_path: str,
        source_value: Any,
        proposed_value: Any,
    ) -> DailyReportReconciliation:
        return DailyReportReconciliation(
            org_id=self.org_id,
            revision_id=revision.id,
            target_key=target_key,
            target_kind=target_kind,
            activity_target_id=target_id if target_kind == "activity" else None,
            measurement_target_id=target_id if target_kind == "measurement" else None,
            field_key=field_key,
            source_document_id=source_document_id,
            source_record_id=source_record.id,
            source_region_id=source_record.region_id,
            source_page_number=source_record.page_number,
            source_path=source_path,
            source_review_version=source_record.review_version,
            source_fingerprint=source_record.fingerprint or content_hash(source_record.payload or {}),
            source_value_hash=content_hash(source_value),
            proposed_value_hash=content_hash(proposed_value),
            resolution_state="proposed",
            created_by=self.principal.id if self.principal else "",
        )

    @staticmethod
    def _activity_proposal_value(row: DailyReportActivity) -> dict[str, Any]:
        values = DailyReportService._activity_hash_value(row)
        values.pop("resolution_state", None)
        return values

    @staticmethod
    def _measurement_proposal_value(row: DailyReportMeasurement) -> dict[str, Any]:
        values = DailyReportService._measurement_hash_value(row)
        values.pop("resolution_state", None)
        return values

    async def update_draft(
        self,
        well_id: str,
        report_id: str,
        revision_number: int,
        *,
        expected_edit_version: int,
        reason: str,
        content: dict[str, Any],
    ) -> dict[str, Any]:
        self._authorize("daily_report.edit", well_id=well_id, report_id=report_id)
        reason = _required_reason(reason)
        report = await self._report(well_id, report_id, lock=True)
        revision = await self._revision(report.id, revision_number, lock=True)
        self._require_editable(revision, expected_edit_version)
        scope = await self._resolve_well(well_id, report.wellbore_id)
        if revision.source_kind == "import":
            content = dict(content)
            if content.get("source_document_id") not in {None, revision.source_document_id}:
                raise ValidationFailed("an imported draft cannot change its source document")
            content["source_document_id"] = revision.source_document_id
            if revision.source_record_id is not None:
                content.setdefault("source_record_id", revision.source_record_id)
            if revision.source_region_id is not None:
                content.setdefault("source_region_id", revision.source_region_id)
            if revision.source_page_number is not None:
                content.setdefault("source_page_number", revision.source_page_number)
        prepared = await self._prepare_content(
            content,
            scope=scope,
            source_kind=revision.source_kind,
            allow_unvalidated_source_records=revision.source_kind != "manual",
        )
        if revision.source_kind == "import":
            for child in (*prepared.activities, *prepared.measurements):
                if child.get("source_record_id") and child.get("source_document_id") != revision.source_document_id:
                    raise ValidationFailed("imported rows must retain the report revision's source document")
        before_activities, before_measurements = await self._children(revision.id)
        reconciliation_rows = (
            await self.session.execute(
                select(DailyReportReconciliation).where(
                    DailyReportReconciliation.org_id == self.org_id,
                    DailyReportReconciliation.revision_id == revision.id,
                )
            )
        ).scalars().all()
        if revision.source_kind == "import":
            for item in reconciliation_rows:
                await self._assert_reconciliation_source_current(item, scope)
        before_hash = self._content_hash(report, revision, before_activities, before_measurements)
        self._require_hash_matches(revision, before_hash)
        before = self._revision_audit_state(revision, before_activities, before_measurements)
        before["reconciliation"] = [self._reconciliation_out(row) for row in reconciliation_rows]
        now = utc_now()
        result = await self.session.execute(
            update(DailyDrillingReportRevision)
            .where(
                DailyDrillingReportRevision.id == revision.id,
                DailyDrillingReportRevision.org_id == self.org_id,
                DailyDrillingReportRevision.status == "draft",
                DailyDrillingReportRevision.edit_version == expected_edit_version,
            )
            .values(
                **prepared.revision_fields,
                edit_version=expected_edit_version + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await self._raise_stale_revision(report.id, revision_number, expected_edit_version)

        await self.session.execute(
            delete(DailyReportActivity).where(
                DailyReportActivity.org_id == self.org_id,
                DailyReportActivity.revision_id == revision.id,
            )
        )
        await self.session.execute(
            delete(DailyReportMeasurement).where(
                DailyReportMeasurement.org_id == self.org_id,
                DailyReportMeasurement.revision_id == revision.id,
            )
        )
        revision.edit_version = expected_edit_version + 1
        revision.updated_at = now
        for key, value in prepared.revision_fields.items():
            setattr(revision, key, value)
        resolution_state = "manual" if revision.source_kind == "manual" else "proposed"
        activities = self._activity_rows(revision.id, prepared.activities, resolution_state=resolution_state)
        measurements = self._measurement_rows(revision.id, prepared.measurements, resolution_state=resolution_state)
        self.session.add_all([*activities, *measurements])
        await self.session.flush()
        if revision.source_kind == "import":
            reconciliation_by_key = {item.target_key: item for item in reconciliation_rows}
            before_activities_by_id = {row.id: row for row in before_activities}
            before_measurements_by_id = {row.id: row for row in before_measurements}
            retained_keys: set[str] = set()
            new_reconciliation: list[DailyReportReconciliation] = []
            for item in reconciliation_rows:
                if item.target_kind == "revision_field":
                    value_hash = content_hash(getattr(revision, item.field_key))
                    if item.resolution_state != "rejected" and value_hash != item.proposed_value_hash:
                        item.resolution_state = "proposed"
                        item.reason = None
                        item.decided_by = None
                        item.decided_at = None
            for row in activities:
                if row.source_record_id is None:
                    continue
                target_key = f"activity:{row.source_record_id}:{row.sequence}"
                retained_keys.add(target_key)
                decision_row = reconciliation_by_key.get(target_key)
                if decision_row is None:
                    record = await self._import_source_record(
                        scope,
                        source_document_id=revision.source_document_id or "",
                        source_record_id=row.source_record_id,
                    )
                    decision_row = self._new_reconciliation(
                        revision=revision,
                        target_key=target_key,
                        target_kind="activity",
                        target_id=row.id,
                        field_key="activity",
                        source_document_id=revision.source_document_id or "",
                        source_record=record,
                        source_path="payload",
                        source_value=record.payload or {},
                        proposed_value=self._activity_proposal_value(row),
                    )
                    new_reconciliation.append(decision_row)
                elif decision_row.resolution_state == "rejected":
                    previous = before_activities_by_id.get(decision_row.target_id or "")
                    if previous is None or content_hash(self._activity_proposal_value(row)) != content_hash(
                        self._activity_proposal_value(previous)
                    ):
                        raise Conflict("a rejected source activity is immutable in this revision")
                    self._set_reconciliation_target_id(decision_row, row.id)
                    row.resolution_state = "rejected"
                else:
                    self._set_reconciliation_target_id(decision_row, row.id)
                    if content_hash(self._activity_proposal_value(row)) != decision_row.proposed_value_hash:
                        decision_row.resolution_state = "proposed"
                        decision_row.reason = None
                        decision_row.decided_by = None
                        decision_row.decided_at = None
                    row.resolution_state = decision_row.resolution_state
            for row in measurements:
                if row.source_record_id is None:
                    continue
                target_key = f"measurement:{row.source_record_id}:{row.parameter_key}:{row.ordinal}"
                retained_keys.add(target_key)
                decision_row = reconciliation_by_key.get(target_key)
                if decision_row is None:
                    record = await self._import_source_record(
                        scope,
                        source_document_id=revision.source_document_id or "",
                        source_record_id=row.source_record_id,
                    )
                    decision_row = self._new_reconciliation(
                        revision=revision,
                        target_key=target_key,
                        target_kind="measurement",
                        target_id=row.id,
                        field_key=row.parameter_key,
                        source_document_id=revision.source_document_id or "",
                        source_record=record,
                        source_path="payload",
                        source_value=record.payload or {},
                        proposed_value=self._measurement_proposal_value(row),
                    )
                    new_reconciliation.append(decision_row)
                elif decision_row.resolution_state == "rejected":
                    previous = before_measurements_by_id.get(decision_row.target_id or "")
                    if previous is None or content_hash(self._measurement_proposal_value(row)) != content_hash(
                        self._measurement_proposal_value(previous)
                    ):
                        raise Conflict("a rejected source measurement is immutable in this revision")
                    self._set_reconciliation_target_id(decision_row, row.id)
                    row.resolution_state = "rejected"
                else:
                    self._set_reconciliation_target_id(decision_row, row.id)
                    if content_hash(self._measurement_proposal_value(row)) != decision_row.proposed_value_hash:
                        decision_row.resolution_state = "proposed"
                        decision_row.reason = None
                        decision_row.decided_by = None
                        decision_row.decided_at = None
                    row.resolution_state = decision_row.resolution_state
            old_activities_by_id = {row.id: row for row in before_activities}
            old_measurements_by_id = {row.id: row for row in before_measurements}
            rejected_clones: list[tuple[DailyReportReconciliation, DailyReportActivity | DailyReportMeasurement]] = []
            for item in reconciliation_rows:
                if item.target_kind not in {"activity", "measurement"} or item.target_key in retained_keys:
                    continue
                if item.resolution_state != "rejected":
                    item.resolution_state = "rejected"
                    item.reason = reason
                    item.decided_by = self.principal.id if self.principal else ""
                    item.decided_at = now
                old_row = (
                    old_activities_by_id.get(item.target_id)
                    if item.target_kind == "activity"
                    else old_measurements_by_id.get(item.target_id)
                )
                if old_row is not None:
                    if item.target_kind == "activity":
                        rejected_row = self._clone_activities(revision.id, [old_row])[0]
                        activities.append(rejected_row)
                    else:
                        rejected_row = self._clone_measurements(revision.id, [old_row])[0]
                        used_ordinals = {
                            row.ordinal for row in measurements if row.parameter_key == rejected_row.parameter_key
                        }
                        if rejected_row.ordinal in used_ordinals:
                            rejected_row.ordinal = max(used_ordinals, default=-1) + 1
                        measurements.append(rejected_row)
                    rejected_row.resolution_state = "rejected"
                    retained_keys.add(item.target_key)
                    self.session.add(rejected_row)
                    rejected_clones.append((item, rejected_row))
            if new_reconciliation:
                self.session.add_all(new_reconciliation)
            if rejected_clones:
                await self.session.flush()
                for item, row in rejected_clones:
                    self._set_reconciliation_target_id(item, row.id)
            await self.session.flush()
        revision.content_hash = self._content_hash(report, revision, activities, measurements)
        await self.session.flush()
        after = self._revision_audit_state(revision, activities, measurements)
        current_reconciliation = (
            await self.session.execute(
                select(DailyReportReconciliation).where(
                    DailyReportReconciliation.org_id == self.org_id,
                    DailyReportReconciliation.revision_id == revision.id,
                ).order_by(DailyReportReconciliation.target_key.asc())
            )
        ).scalars().all()
        after["reconciliation"] = [self._reconciliation_out(row) for row in current_reconciliation]
        await self._record_change(
            report,
            revision,
            action="daily_report.edit",
            change_type="updated",
            permission="daily_report.write",
            fields=["content", "activities", "measurements"],
            before=before,
            after=after,
            reason=reason,
        )
        await emit(
            self.session,
            org_id=self.org_id,
            type="daily_report.changed",
            subject_kind="daily_drilling_report",
            subject_id=report.id,
            well_id=report.well_id,
            wellbore_id=report.wellbore_id,
            payload={
                "report_id": report.id,
                "revision_number": revision.revision_number,
                "edit_version": revision.edit_version,
                "status": revision.status,
                "action": "edited",
            },
        )
        return {"report": self._report_out(report, revision), "revision": await self._revision_detail(report, revision, activities, measurements)}

    async def reconcile(
        self,
        well_id: str,
        report_id: str,
        revision_number: int,
        *,
        expected_edit_version: int,
        reason: str,
        decisions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Record human decisions for imported fields/rows using compare-and-swap semantics."""
        self._authorize("daily_report.reconcile", well_id=well_id, report_id=report_id)
        reason = _required_reason(reason)
        if not decisions or len(decisions) > 500:
            raise ValidationFailed("reconciliation must contain between 1 and 500 decisions")
        keys = [_nonempty_text(item.get("target_key"), "target_key", 180) for item in decisions]
        if len(keys) != len(set(keys)):
            raise ValidationFailed("a reconciliation request may not repeat a target_key")
        report = await self._report(well_id, report_id, lock=True)
        revision = await self._revision(report.id, revision_number, lock=True)
        self._require_editable(revision, expected_edit_version)
        if revision.source_kind != "import":
            raise ValidationFailed("only imported revisions have source reconciliation decisions")
        scope = await self._resolve_well(well_id, report.wellbore_id)
        rows = (
            await self.session.execute(
                select(DailyReportReconciliation).where(
                    DailyReportReconciliation.org_id == self.org_id,
                    DailyReportReconciliation.revision_id == revision.id,
                    DailyReportReconciliation.target_key.in_(keys),
                )
            )
        ).scalars().all()
        by_key = {row.target_key: row for row in rows}
        missing = sorted(set(keys) - set(by_key))
        if missing:
            raise NotFound("one or more reconciliation targets were not found", details={"target_keys": missing})
        activities, measurements = await self._children(revision.id)
        self._require_hash_matches(revision, self._content_hash(report, revision, activities, measurements))
        all_before_decisions = (
            await self.session.execute(
                select(DailyReportReconciliation).where(
                    DailyReportReconciliation.org_id == self.org_id,
                    DailyReportReconciliation.revision_id == revision.id,
                ).order_by(DailyReportReconciliation.target_key.asc())
            )
        ).scalars().all()
        before = self._revision_audit_state(revision, activities, measurements)
        before["reconciliation"] = [self._reconciliation_out(row) for row in all_before_decisions]
        now = utc_now()
        result = await self.session.execute(
            update(DailyDrillingReportRevision)
            .where(
                DailyDrillingReportRevision.id == revision.id,
                DailyDrillingReportRevision.org_id == self.org_id,
                DailyDrillingReportRevision.status == "draft",
                DailyDrillingReportRevision.edit_version == expected_edit_version,
            )
            .values(edit_version=expected_edit_version + 1, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await self._raise_stale_revision(report.id, revision_number, expected_edit_version)
        revision.edit_version = expected_edit_version + 1
        revision.updated_at = now

        for change in decisions:
            target_key = _nonempty_text(change.get("target_key"), "target_key", 180)
            requested_state = change.get("decision")
            if requested_state not in {"accepted", "corrected", "rejected", "unresolved"}:
                raise ValidationFailed("reconciliation decision is not recognized")
            decision_row = by_key[target_key]
            if decision_row.resolution_state == "rejected" and requested_state != "rejected":
                raise Conflict("a rejected import proposal cannot be re-opened; create a correction from source evidence")
            await self._assert_reconciliation_source_current(decision_row, scope)
            current_value: Any
            target_row: DailyReportActivity | DailyReportMeasurement | None = None
            if decision_row.target_kind == "revision_field":
                if decision_row.field_key not in REPORT_RECONCILIATION_REVISION_FIELDS:
                    raise ValidationFailed("stored reconciliation field is outside the supported revision contract")
                current_value = getattr(revision, decision_row.field_key)
            elif decision_row.target_kind == "report_identity":
                if decision_row.field_key not in REPORT_RECONCILIATION_IDENTITY_FIELDS:
                    raise ValidationFailed("stored reconciliation field is outside the supported report identity contract")
                if requested_state == "corrected":
                    raise ValidationFailed("report identity is immutable; create a separate report for a corrected identity")
                current_value = getattr(report, decision_row.field_key)
                if isinstance(current_value, dt.date):
                    current_value = current_value.isoformat()
            elif decision_row.target_kind == "activity":
                target_row = (
                    await self.session.execute(
                        select(DailyReportActivity).where(
                            DailyReportActivity.id == decision_row.target_id,
                            DailyReportActivity.org_id == self.org_id,
                            DailyReportActivity.revision_id == revision.id,
                        )
                    )
                ).scalar_one_or_none()
                if target_row is None:
                    raise Conflict("the imported activity changed or was removed; reload the revision")
                current_value = self._activity_proposal_value(target_row)
            elif decision_row.target_kind == "measurement":
                target_row = (
                    await self.session.execute(
                        select(DailyReportMeasurement).where(
                            DailyReportMeasurement.id == decision_row.target_id,
                            DailyReportMeasurement.org_id == self.org_id,
                            DailyReportMeasurement.revision_id == revision.id,
                        )
                    )
                ).scalar_one_or_none()
                if target_row is None:
                    raise Conflict("the imported measurement changed or was removed; reload the revision")
                current_value = self._measurement_proposal_value(target_row)
            else:
                raise ValidationFailed("stored reconciliation target kind is invalid")

            current_hash = content_hash(current_value)
            if requested_state == "accepted" and current_hash != decision_row.proposed_value_hash:
                raise ValidationFailed(
                    "the mapped value changed after import; mark it corrected or restore the original proposal",
                    details={"target_key": target_key},
                )
            if requested_state == "corrected" and current_hash == decision_row.proposed_value_hash:
                raise ValidationFailed(
                    "a corrected decision requires an edited value that differs from the imported proposal",
                    details={"target_key": target_key},
                )
            if requested_state == "rejected":
                if decision_row.target_kind == "revision_field" and current_value not in (None, ""):
                    raise ValidationFailed(
                        "clear a rejected optional field in the draft before rejecting its source proposal",
                        details={"target_key": target_key},
                    )
                if target_row is not None:
                    target_row.resolution_state = "rejected"
            elif target_row is not None:
                target_row.resolution_state = requested_state

            decision_row.resolution_state = requested_state
            decision_row.reason = reason
            decision_row.decided_by = self.principal.id if self.principal else ""
            decision_row.decided_at = now
            decision_row.updated_at = now

        await self.session.flush()
        activities, measurements = await self._children(revision.id)
        revision.content_hash = self._content_hash(report, revision, activities, measurements)
        await self.session.flush()
        after = self._revision_audit_state(revision, activities, measurements)
        all_decisions = (
            await self.session.execute(
                select(DailyReportReconciliation).where(
                    DailyReportReconciliation.org_id == self.org_id,
                    DailyReportReconciliation.revision_id == revision.id,
                ).order_by(DailyReportReconciliation.target_key.asc())
            )
        ).scalars().all()
        after["reconciliation"] = [self._reconciliation_out(row) for row in all_decisions]
        await self._record_change(
            report,
            revision,
            action="daily_report.reconcile",
            change_type="reconciled",
            permission="daily_report.write",
            fields=["reconciliation", "content"],
            before=before,
            after=after,
            reason=reason,
        )
        await emit(
            self.session,
            org_id=self.org_id,
            type="daily_report.changed",
            subject_kind="daily_drilling_report",
            subject_id=report.id,
            well_id=report.well_id,
            wellbore_id=report.wellbore_id,
            payload={
                "report_id": report.id,
                "revision_number": revision.revision_number,
                "edit_version": revision.edit_version,
                "status": revision.status,
                "action": "reconciled",
                "targets": len(decisions),
            },
        )
        return {
            "report": self._report_out(report, revision),
            "revision": await self._revision_detail(report, revision, activities, measurements),
        }

    async def _assert_reconciliation_source_current(
        self,
        decision: DailyReportReconciliation,
        scope: _ResolvedWell,
    ) -> None:
        record = await self._import_source_record(
            scope,
            source_document_id=decision.source_document_id,
            source_record_id=decision.source_record_id,
        )
        current_fingerprint = record.fingerprint or content_hash(record.payload or {})
        source_value = _record_payload_path(record.payload or {}, decision.source_path)
        current_source_hash = None if source_value is _MISSING else content_hash(source_value)
        if (
            record.review_version != decision.source_review_version
            or current_fingerprint != decision.source_fingerprint
            or current_source_hash != decision.source_value_hash
        ):
            raise Conflict(
                "source evidence changed after import; refresh the extraction and create or reconcile a new proposal",
                details={
                    "source_record_id": record.id,
                    "source_path": decision.source_path,
                    "expected_review_version": decision.source_review_version,
                    "current_review_version": record.review_version,
                    "expected_fingerprint": decision.source_fingerprint,
                    "current_fingerprint": current_fingerprint,
                    "expected_source_value_hash": decision.source_value_hash,
                    "current_source_value_hash": current_source_hash,
                },
            )

    async def create_correction(
        self,
        well_id: str,
        report_id: str,
        source_revision_number: int | None = None,
        *,
        reason: str,
    ) -> dict[str, Any]:
        self._authorize("daily_report.revise", well_id=well_id, report_id=report_id)
        reason = _required_reason(reason)
        report = await self._report(well_id, report_id, lock=True)
        if source_revision_number is not None and source_revision_number != report.latest_revision_number:
            raise Conflict(
                "a correction must be based on the latest report revision",
                details={"latest_revision_number": report.latest_revision_number},
            )
        source_revision_number = report.latest_revision_number
        source = await self._revision(report.id, source_revision_number, lock=True)
        if source.status not in _REVISION_HISTORY_SOURCES:
            raise Conflict(
                "a new correction can only branch from an approved, rejected or withdrawn revision",
                details={"revision_number": source.revision_number, "status": source.status},
            )
        scope = await self._resolve_well(well_id, report.wellbore_id)
        source_activities, source_measurements = await self._children(source.id)
        self._require_hash_matches(source, self._content_hash(report, source, source_activities, source_measurements))
        await self._validate_revision_sources(
            report, source, source_activities, source_measurements, scope=scope
        )
        source_reconciliation = (
            await self.session.execute(
                select(DailyReportReconciliation).where(
                    DailyReportReconciliation.org_id == self.org_id,
                    DailyReportReconciliation.revision_id == source.id,
                )
            )
        ).scalars().all()

        # Reserve the next revision number with a database compare-and-swap. The unique constraint is
        # the final authority on PostgreSQL and SQLite; the parent pointer and new revision commit or
        # roll back together.
        next_number = report.latest_revision_number + 1
        result = await self.session.execute(
            update(DailyDrillingReport)
            .where(
                DailyDrillingReport.id == report.id,
                DailyDrillingReport.org_id == self.org_id,
                DailyDrillingReport.latest_revision_number == report.latest_revision_number,
            )
            .values(latest_revision_number=next_number, updated_at=utc_now())
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            raise Conflict("another correction was created concurrently; reload the report and retry")
        report.latest_revision_number = next_number
        report.updated_at = utc_now()
        revision = DailyDrillingReportRevision(
            org_id=self.org_id,
            report_id=report.id,
            revision_number=next_number,
            edit_version=1,
            status="draft",
            source_kind=source.source_kind,
            source_document_id=source.source_document_id,
            source_record_id=source.source_record_id,
            source_region_id=source.source_region_id,
            source_page_number=source.source_page_number,
            period_start_at=source.period_start_at,
            period_end_at=source.period_end_at,
            summary=source.summary,
            operations_summary=source.operations_summary,
            safety_notes=source.safety_notes,
            next_period_plan=source.next_period_plan,
            environmental_notes=source.environmental_notes,
            supersedes_revision_id=source.id,
            created_by=self.principal.id if self.principal else "",
        )
        self.session.add(revision)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            if not _is_named_constraint(exc, "uq_daily_report_revision_number"):
                raise
            raise Conflict("another correction was created concurrently; reload the report and retry") from exc

        activities = self._clone_activities(
            revision.id, [row for row in source_activities if row.resolution_state != "rejected"]
        )
        measurements = self._clone_measurements(
            revision.id, [row for row in source_measurements if row.resolution_state != "rejected"]
        )
        if revision.source_kind == "import":
            for row in activities:
                row.resolution_state = "proposed" if row.source_record_id is not None else "manual"
            for row in measurements:
                row.resolution_state = "proposed" if row.source_record_id is not None else "manual"
        self.session.add_all([*activities, *measurements])
        await self.session.flush()
        if revision.source_kind == "import":
            source_decisions = {item.target_key: item for item in source_reconciliation}
            correction_decisions: list[DailyReportReconciliation] = []
            for row in activities:
                if row.source_record_id is None:
                    continue
                target_key = f"activity:{row.source_record_id}:{row.sequence}"
                source_decision = source_decisions.get(target_key)
                if source_decision is None or source_decision.resolution_state == "rejected":
                    raise Conflict("the correction lost its source activity reconciliation lineage")
                correction_decisions.append(
                    DailyReportReconciliation(
                        org_id=self.org_id,
                        revision_id=revision.id,
                        target_key=target_key,
                        target_kind="activity",
                        activity_target_id=row.id,
                        field_key=source_decision.field_key,
                        source_document_id=source_decision.source_document_id,
                        source_record_id=source_decision.source_record_id,
                        source_region_id=source_decision.source_region_id,
                        source_page_number=source_decision.source_page_number,
                        source_path=source_decision.source_path,
                        source_review_version=source_decision.source_review_version,
                        source_fingerprint=source_decision.source_fingerprint,
                        source_value_hash=source_decision.source_value_hash,
                        proposed_value_hash=content_hash(self._activity_proposal_value(row)),
                        resolution_state="proposed",
                        created_by=self.principal.id if self.principal else "",
                    )
                )
            for row in measurements:
                if row.source_record_id is None:
                    continue
                target_key = f"measurement:{row.source_record_id}:{row.parameter_key}:{row.ordinal}"
                source_decision = source_decisions.get(target_key)
                if source_decision is None or source_decision.resolution_state == "rejected":
                    raise Conflict("the correction lost its source measurement reconciliation lineage")
                correction_decisions.append(
                    DailyReportReconciliation(
                        org_id=self.org_id,
                        revision_id=revision.id,
                        target_key=target_key,
                        target_kind="measurement",
                        measurement_target_id=row.id,
                        field_key=source_decision.field_key,
                        source_document_id=source_decision.source_document_id,
                        source_record_id=source_decision.source_record_id,
                        source_region_id=source_decision.source_region_id,
                        source_page_number=source_decision.source_page_number,
                        source_path=source_decision.source_path,
                        source_review_version=source_decision.source_review_version,
                        source_fingerprint=source_decision.source_fingerprint,
                        source_value_hash=source_decision.source_value_hash,
                        proposed_value_hash=content_hash(self._measurement_proposal_value(row)),
                        resolution_state="proposed",
                        created_by=self.principal.id if self.principal else "",
                    )
                )
            for source_decision in source_reconciliation:
                if source_decision.resolution_state == "rejected":
                    continue
                if source_decision.target_kind == "revision_field":
                    if source_decision.field_key not in REPORT_RECONCILIATION_REVISION_FIELDS:
                        raise Conflict("the correction source contains an unsupported revision field mapping")
                    target_value = getattr(revision, source_decision.field_key)
                elif source_decision.target_kind == "report_identity":
                    if source_decision.field_key not in REPORT_RECONCILIATION_IDENTITY_FIELDS:
                        raise Conflict("the correction source contains an unsupported report identity mapping")
                    target_value = getattr(report, source_decision.field_key)
                    if isinstance(target_value, dt.date):
                        target_value = target_value.isoformat()
                else:
                    continue
                correction_decisions.append(
                    DailyReportReconciliation(
                        org_id=self.org_id,
                        revision_id=revision.id,
                        target_key=source_decision.target_key,
                        target_kind=source_decision.target_kind,
                        activity_target_id=None,
                        measurement_target_id=None,
                        field_key=source_decision.field_key,
                        source_document_id=source_decision.source_document_id,
                        source_record_id=source_decision.source_record_id,
                        source_region_id=source_decision.source_region_id,
                        source_page_number=source_decision.source_page_number,
                        source_path=source_decision.source_path,
                        source_review_version=source_decision.source_review_version,
                        source_fingerprint=source_decision.source_fingerprint,
                        source_value_hash=source_decision.source_value_hash,
                        proposed_value_hash=content_hash(target_value),
                        resolution_state="proposed",
                        created_by=self.principal.id if self.principal else "",
                    )
                )
            self.session.add_all(correction_decisions)
            await self.session.flush()
        revision.content_hash = self._content_hash(report, revision, activities, measurements)
        await self.session.flush()
        before = self._revision_audit_state(source, source_activities, source_measurements)
        before["reconciliation"] = [self._reconciliation_out(row) for row in source_reconciliation]
        after = self._revision_audit_state(revision, activities, measurements)
        correction_ledger = (
            await self.session.execute(
                select(DailyReportReconciliation).where(
                    DailyReportReconciliation.org_id == self.org_id,
                    DailyReportReconciliation.revision_id == revision.id,
                ).order_by(DailyReportReconciliation.target_key.asc())
            )
        ).scalars().all()
        after["reconciliation"] = [self._reconciliation_out(row) for row in correction_ledger]
        await self._record_change(
            report,
            revision,
            action="daily_report.revise",
            change_type="revision_created",
            permission="daily_report.write",
            fields=["revision", "supersedes_revision_id", "status"],
            before=before,
            after=after,
            reason=reason,
        )
        await emit(
            self.session,
            org_id=self.org_id,
            type="daily_report.changed",
            subject_kind="daily_drilling_report",
            subject_id=report.id,
            well_id=report.well_id,
            wellbore_id=report.wellbore_id,
            payload={
                "report_id": report.id,
                "revision_number": next_number,
                "supersedes_revision_number": source.revision_number,
                "status": "draft",
                "action": "revision_created",
            },
        )
        return {"report": self._report_out(report, revision), "revision": await self._revision_detail(report, revision, activities, measurements)}

    # ------------------------------------------------------------------ status transitions

    async def submit(
        self,
        well_id: str,
        report_id: str,
        revision_number: int,
        *,
        expected_edit_version: int,
        reason: str,
    ) -> dict[str, Any]:
        self._authorize("daily_report.submit", well_id=well_id, report_id=report_id)
        reason = _required_reason(reason)
        report = await self._report(well_id, report_id, lock=True)
        revision = await self._revision(report.id, revision_number, lock=True)
        self._require_editable(revision, expected_edit_version)
        activities, measurements = await self._children(revision.id)
        if not _has_meaningful_content(revision, activities, measurements):
            raise ValidationFailed("a blank report cannot be submitted")
        await self._require_reconciled(activities, measurements, revision_id=revision.id)
        scope = await self._resolve_well(well_id, report.wellbore_id)
        await self._validate_revision_sources(report, revision, activities, measurements, scope=scope)
        digest = self._content_hash(report, revision, activities, measurements)
        self._require_hash_matches(revision, digest)
        now = utc_now()
        before = self._revision_audit_state(revision, activities, measurements)
        result = await self.session.execute(
            update(DailyDrillingReportRevision)
            .where(
                DailyDrillingReportRevision.id == revision.id,
                DailyDrillingReportRevision.org_id == self.org_id,
                DailyDrillingReportRevision.status == "draft",
                DailyDrillingReportRevision.edit_version == expected_edit_version,
            )
            .values(
                status="submitted",
                submitted_by=self.principal.id if self.principal else None,
                submitted_at=now,
                edit_version=expected_edit_version + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await self._raise_stale_revision(report.id, revision_number, expected_edit_version)
        revision.status = "submitted"
        revision.submitted_by = self.principal.id if self.principal else None
        revision.submitted_at = now
        revision.edit_version = expected_edit_version + 1
        revision.updated_at = now
        after = self._revision_audit_state(revision, activities, measurements)
        await self._record_change(
            report,
            revision,
            action="daily_report.submit",
            change_type="submitted",
            permission="daily_report.submit",
            fields=["status", "submitted_by", "submitted_at", "edit_version"],
            before=before,
            after=after,
            reason=reason,
        )
        await emit(
            self.session,
            org_id=self.org_id,
            type="daily_report.changed",
            subject_kind="daily_drilling_report",
            subject_id=report.id,
            well_id=report.well_id,
            wellbore_id=report.wellbore_id,
            payload={"report_id": report.id, "revision_number": revision_number, "status": "submitted", "action": "submitted"},
        )
        return {"report": self._report_out(report, revision), "revision": await self._revision_detail(report, revision, activities, measurements)}

    async def decide(
        self,
        well_id: str,
        report_id: str,
        revision_number: int,
        *,
        decision: str,
        expected_edit_version: int,
        reason: str,
    ) -> dict[str, Any]:
        if decision not in {"approved", "rejected"}:
            raise ValidationFailed("daily report decision must be approved or rejected")
        action = "daily_report.approve" if decision == "approved" else "daily_report.reject"
        self._authorize(action, well_id=well_id, report_id=report_id)
        reason = _required_reason(reason)
        report = await self._report(well_id, report_id, lock=True)
        revision = await self._revision(report.id, revision_number, lock=True)
        self._require_current_version(revision, expected_edit_version, expected_status="submitted")
        if revision.submitted_by == (self.principal.id if self.principal else None):
            raise PermissionDenied(
                "the report submitter cannot approve or reject the same revision",
                details={"report_id": report.id, "revision_number": revision_number},
            )
        activities, measurements = await self._children(revision.id)
        await self._require_reconciled(activities, measurements, revision_id=revision.id)
        scope = await self._resolve_well(well_id, report.wellbore_id)
        await self._validate_revision_sources(report, revision, activities, measurements, scope=scope)
        self._require_hash_matches(revision, self._content_hash(report, revision, activities, measurements))
        now = utc_now()
        before = self._revision_audit_state(revision, activities, measurements)
        result = await self.session.execute(
            update(DailyDrillingReportRevision)
            .where(
                DailyDrillingReportRevision.id == revision.id,
                DailyDrillingReportRevision.org_id == self.org_id,
                DailyDrillingReportRevision.status == "submitted",
                DailyDrillingReportRevision.edit_version == expected_edit_version,
            )
            .values(
                status=decision,
                decided_by=self.principal.id if self.principal else None,
                decided_at=now,
                decision_reason=reason,
                edit_version=expected_edit_version + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await self._raise_stale_revision(report.id, revision_number, expected_edit_version)
        revision.status = decision
        revision.decided_by = self.principal.id if self.principal else None
        revision.decided_at = now
        revision.decision_reason = reason
        revision.edit_version = expected_edit_version + 1
        revision.updated_at = now
        if decision == "approved":
            report.latest_approved_revision_number = revision_number
            report.updated_at = now
        after = self._revision_audit_state(revision, activities, measurements)
        await self._record_change(
            report,
            revision,
            action=action,
            change_type=decision,
            permission=action,
            fields=["status", "decided_by", "decided_at", "decision_reason", "edit_version"],
            before=before,
            after=after,
            reason=reason,
        )
        await emit(
            self.session,
            org_id=self.org_id,
            type="daily_report.changed",
            subject_kind="daily_drilling_report",
            subject_id=report.id,
            well_id=report.well_id,
            wellbore_id=report.wellbore_id,
            payload={"report_id": report.id, "revision_number": revision_number, "status": decision, "action": decision},
        )
        return {"report": self._report_out(report, revision), "revision": await self._revision_detail(report, revision, activities, measurements)}

    async def withdraw(
        self,
        well_id: str,
        report_id: str,
        revision_number: int,
        *,
        expected_edit_version: int,
        reason: str,
    ) -> dict[str, Any]:
        self._authorize("daily_report.withdraw", well_id=well_id, report_id=report_id)
        reason = _required_reason(reason)
        report = await self._report(well_id, report_id, lock=True)
        revision = await self._revision(report.id, revision_number, lock=True)
        self._require_current_version(revision, expected_edit_version, expected_status=None)
        if revision.status not in {"draft", "submitted"}:
            raise Conflict(
                "only a draft or submitted revision can be withdrawn",
                details={"revision_number": revision_number, "status": revision.status},
            )
        expected_owner = revision.submitted_by if revision.status == "submitted" else revision.created_by
        if expected_owner != (self.principal.id if self.principal else None):
            raise PermissionDenied("only the draft creator or submitter may withdraw this revision")
        activities, measurements = await self._children(revision.id)
        before = self._revision_audit_state(revision, activities, measurements)
        now = utc_now()
        result = await self.session.execute(
            update(DailyDrillingReportRevision)
            .where(
                DailyDrillingReportRevision.id == revision.id,
                DailyDrillingReportRevision.org_id == self.org_id,
                DailyDrillingReportRevision.status == revision.status,
                DailyDrillingReportRevision.edit_version == expected_edit_version,
            )
            .values(
                status="withdrawn",
                decision_reason=reason,
                decided_by=self.principal.id if self.principal else None,
                decided_at=now,
                edit_version=expected_edit_version + 1,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await self._raise_stale_revision(report.id, revision_number, expected_edit_version)
        revision.status = "withdrawn"
        revision.decision_reason = reason
        revision.decided_by = self.principal.id if self.principal else None
        revision.decided_at = now
        revision.edit_version = expected_edit_version + 1
        revision.updated_at = now
        after = self._revision_audit_state(revision, activities, measurements)
        await self._record_change(
            report,
            revision,
            action="daily_report.withdraw",
            change_type="withdrawn",
            permission="daily_report.withdraw",
            fields=["status", "decided_by", "decided_at", "decision_reason", "edit_version"],
            before=before,
            after=after,
            reason=reason,
        )
        await emit(
            self.session,
            org_id=self.org_id,
            type="daily_report.changed",
            subject_kind="daily_drilling_report",
            subject_id=report.id,
            well_id=report.well_id,
            wellbore_id=report.wellbore_id,
            payload={"report_id": report.id, "revision_number": revision_number, "status": "withdrawn", "action": "withdrawn"},
        )
        return {"report": self._report_out(report, revision), "revision": await self._revision_detail(report, revision, activities, measurements)}

    # ------------------------------------------------------------------ input, source and invariant validation

    @staticmethod
    def _validate_list_filters(
        date_from: dt.date | None,
        date_to: dt.date | None,
        shift_code: str | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> None:
        if date_from is not None and not isinstance(date_from, dt.date):
            raise ValidationFailed("report_date_from must be a local calendar date")
        if date_to is not None and not isinstance(date_to, dt.date):
            raise ValidationFailed("report_date_to must be a local calendar date")
        if date_from is not None and date_to is not None and date_to < date_from:
            raise ValidationFailed("report_date_to must not precede report_date_from")
        if shift_code is not None and shift_code not in REPORT_SHIFT_CODES:
            raise ValidationFailed("shift_code is not recognized", details={"allowed": list(REPORT_SHIFT_CODES)})
        if status is not None and status not in REPORT_REVISION_STATUSES:
            raise ValidationFailed("status is not recognized", details={"allowed": list(REPORT_REVISION_STATUSES)})
        if not 1 <= limit <= 500 or offset < 0:
            raise ValidationFailed("limit must be 1..500 and offset must be non-negative")

    def _normalize_identity(self, payload: dict[str, Any]) -> dict[str, Any]:
        report_date = payload.get("report_date")
        if isinstance(report_date, str):
            try:
                report_date = dt.date.fromisoformat(report_date)
            except ValueError as exc:
                raise ValidationFailed("report_date must be an ISO local calendar date") from exc
        if not isinstance(report_date, dt.date) or isinstance(report_date, dt.datetime):
            raise ValidationFailed("report_date must be a local calendar date, not a timestamp")
        timezone = _nonempty_text(payload.get("report_timezone"), "report_timezone", 64)
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValidationFailed(
                "report_timezone must be a valid IANA timezone identifier",
                details={"field": "report_timezone", "value": timezone},
            ) from exc
        shift_code = payload.get("shift_code")
        if shift_code not in REPORT_SHIFT_CODES:
            raise ValidationFailed(
                "shift_code is not recognized",
                details={"field": "shift_code", "value": shift_code, "allowed": list(REPORT_SHIFT_CODES)},
            )
        shift_label = _optional_text(payload.get("shift_label"), "shift_label", 80)
        if shift_code == "custom" and not shift_label:
            raise ValidationFailed("shift_label is required for a custom shift")
        if shift_code != "custom" and shift_label is not None:
            raise ValidationFailed("shift_label is only allowed when shift_code is custom")
        report_number = _optional_text(payload.get("report_number"), "report_number", 120)
        if report_number is not None:
            identity_key = f"number:{_normalize_identifier(report_number)}"
        elif shift_code == "custom":
            identity_key = f"shift:custom:{_normalize_identifier(shift_label or '')}"
        else:
            identity_key = f"shift:{shift_code}"
        if len(identity_key) > 180:
            raise ValidationFailed("normalized report identity exceeds 180 characters")
        return {
            "report_date": report_date,
            "report_timezone": timezone,
            "shift_code": shift_code,
            "shift_label": shift_label,
            "report_number": report_number,
            "identity_key": identity_key,
        }

    async def _prepare_content(
        self,
        value: Any,
        *,
        scope: _ResolvedWell,
        source_kind: str,
        allow_unvalidated_source_records: bool,
    ) -> _PreparedContent:
        if not isinstance(value, dict):
            raise ValidationFailed("content must be an object")
        period_start = _aware_utc(value.get("period_start_at"), "period_start_at")
        period_end = _aware_utc(value.get("period_end_at"), "period_end_at")
        if period_start is not None and period_end is not None and period_end < period_start:
            raise ValidationFailed("period_end_at must not precede period_start_at")
        revision_source = await self._validate_evidence(
            scope,
            source_document_id=value.get("source_document_id"),
            source_record_id=value.get("source_record_id"),
            source_region_id=value.get("source_region_id"),
            source_page_number=value.get("source_page_number"),
            allow_unvalidated_record=allow_unvalidated_source_records,
        )
        revision_fields = {
            "source_document_id": revision_source["source_document_id"],
            "source_record_id": revision_source["source_record_id"],
            "source_region_id": revision_source["source_region_id"],
            "source_page_number": revision_source["source_page_number"],
            "period_start_at": period_start,
            "period_end_at": period_end,
            "summary": _optional_text(value.get("summary"), "summary", 10000),
            "operations_summary": _optional_text(value.get("operations_summary"), "operations_summary", 10000),
            "safety_notes": _optional_text(value.get("safety_notes"), "safety_notes", 10000),
            "next_period_plan": _optional_text(value.get("next_period_plan"), "next_period_plan", 10000),
            "environmental_notes": _optional_text(value.get("environmental_notes"), "environmental_notes", 10000),
        }
        raw_activities = value.get("activities", [])
        raw_measurements = value.get("measurements", [])
        if not isinstance(raw_activities, list) or len(raw_activities) > _MAX_ACTIVITIES:
            raise ValidationFailed(f"activities must be a list with at most {_MAX_ACTIVITIES} entries")
        if not isinstance(raw_measurements, list) or len(raw_measurements) > _MAX_MEASUREMENTS:
            raise ValidationFailed(f"measurements must be a list with at most {_MAX_MEASUREMENTS} entries")
        activities = [
            await self._prepare_activity(
                item,
                sequence=index,
                scope=scope,
                source_kind=source_kind,
                allow_unvalidated_record=allow_unvalidated_source_records,
            )
            for index, item in enumerate(raw_activities)
        ]
        measurements = [
            await self._prepare_measurement(
                item,
                scope=scope,
                source_kind=source_kind,
                allow_unvalidated_record=allow_unvalidated_source_records,
            )
            for item in raw_measurements
        ]
        return _PreparedContent(revision_fields=revision_fields, activities=activities, measurements=measurements)

    async def _prepare_activity(
        self,
        value: Any,
        *,
        sequence: int,
        scope: _ResolvedWell,
        source_kind: str,
        allow_unvalidated_record: bool,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationFailed("each activity must be an object", details={"sequence": sequence})
        kind = value.get("activity_kind")
        if kind not in REPORT_ACTIVITY_KINDS:
            raise ValidationFailed(
                "activity_kind is not recognized",
                details={"sequence": sequence, "value": kind, "allowed": list(REPORT_ACTIVITY_KINDS)},
            )
        operation_kind = value.get("operation_kind")
        if operation_kind is not None and operation_kind not in OPERATION_KINDS:
            raise ValidationFailed(
                "operation_kind is not recognized",
                details={"sequence": sequence, "value": operation_kind, "allowed": list(OPERATION_KINDS)},
            )
        if kind == "operation" and operation_kind is None:
            raise ValidationFailed("operation_kind is required for an operation activity", details={"sequence": sequence})
        if kind != "operation" and operation_kind is not None:
            raise ValidationFailed("operation_kind is only allowed for an operation activity", details={"sequence": sequence})
        npt_category = value.get("npt_category")
        if npt_category is not None:
            npt_category = canonical_npt_category(str(npt_category))
            if npt_category not in NPT_CATEGORIES:
                raise ValidationFailed(
                    "npt_category is not recognized",
                    details={"sequence": sequence, "value": npt_category, "allowed": list(NPT_CATEGORIES)},
                )
        if kind == "npt" and npt_category is None:
            raise ValidationFailed("npt_category is required for an NPT activity", details={"sequence": sequence})
        if kind != "npt" and npt_category is not None:
            raise ValidationFailed("npt_category is only allowed for an NPT activity", details={"sequence": sequence})
        cause_basis = value.get("cause_basis", "unknown")
        if cause_basis not in CAUSE_BASES:
            raise ValidationFailed(
                "cause_basis is not recognized",
                details={"sequence": sequence, "value": cause_basis, "allowed": list(CAUSE_BASES)},
            )

        start_at = _aware_utc(value.get("start_at"), f"activities[{sequence}].start_at")
        end_at = _aware_utc(value.get("end_at"), f"activities[{sequence}].end_at")
        start_basis = _time_basis(value.get("start_time_basis", "unknown"), "start_time_basis", present=start_at is not None)
        end_basis = _time_basis(value.get("end_time_basis", "unknown"), "end_time_basis", present=end_at is not None)
        if start_at is not None and end_at is not None and end_at < start_at:
            raise ValidationFailed("activity end_at must not precede start_at", details={"sequence": sequence})

        duration_value = _finite_number(value.get("duration_value"), "duration_value", minimum=0.0)
        duration_unit = _optional_text(value.get("duration_unit"), "duration_unit", 24)
        duration_basis_value = value.get("duration_basis", "unknown")
        if (duration_value is None) != (duration_unit is None):
            raise ValidationFailed("duration_value and duration_unit must be supplied together", details={"sequence": sequence})
        if duration_value is None:
            duration_basis = _time_basis(duration_basis_value, "duration_basis", present=False)
            duration_seconds = None
        else:
            duration_basis = _time_basis(duration_basis_value, "duration_basis", present=True)
            duration_seconds = _convert_checked(duration_value, duration_unit or "", "s", "duration_value", minimum=0.0)

        depth_from_value = _finite_number(value.get("depth_from_value"), "depth_from_value", minimum=0.0)
        depth_from_unit = _optional_text(value.get("depth_from_unit"), "depth_from_unit", 24)
        depth_to_value = _finite_number(value.get("depth_to_value"), "depth_to_value", minimum=0.0)
        depth_to_unit = _optional_text(value.get("depth_to_unit"), "depth_to_unit", 24)
        if (depth_from_value is None) != (depth_from_unit is None):
            raise ValidationFailed("depth_from_value and depth_from_unit must be supplied together")
        if (depth_to_value is None) != (depth_to_unit is None):
            raise ValidationFailed("depth_to_value and depth_to_unit must be supplied together")
        has_depth = depth_from_value is not None or depth_to_value is not None
        depth_reference = value.get("depth_reference", "unknown")
        if depth_reference not in REPORT_DEPTH_REFERENCES:
            raise ValidationFailed("depth_reference is not recognized", details={"allowed": list(REPORT_DEPTH_REFERENCES)})
        depth_basis = _time_basis(value.get("depth_basis", "unknown"), "depth_basis", present=has_depth)
        if has_depth and depth_reference == "unknown":
            raise ValidationFailed("a depth reference is required when activity depth is supplied")
        depth_from_si = (
            _convert_checked(depth_from_value, depth_from_unit or "", "m", "depth_from_value", minimum=0.0)
            if depth_from_value is not None
            else None
        )
        depth_to_si = (
            _convert_checked(depth_to_value, depth_to_unit or "", "m", "depth_to_value", minimum=0.0)
            if depth_to_value is not None
            else None
        )
        if depth_from_si is not None and depth_to_si is not None and depth_to_si < depth_from_si:
            raise ValidationFailed("activity depth_to must not be shallower than depth_from")

        title = _nonempty_text(value.get("title"), "title", 300)
        description = _optional_text(value.get("description"), "description", 10000)
        operation_id = _optional_text(value.get("operation_id"), "operation_id", 64)
        event_id = _optional_text(value.get("event_id"), "event_id", 64)
        operation = await self._validate_operation_link(scope, operation_id, operation_kind)
        event = await self._validate_event_link(scope, event_id, operation_id)
        evidence = await self._validate_evidence(
            scope,
            source_document_id=value.get("source_document_id"),
            source_record_id=value.get("source_record_id"),
            source_region_id=value.get("source_region_id"),
            source_page_number=value.get("source_page_number"),
            operation_id=operation_id,
            allow_unvalidated_record=allow_unvalidated_record,
        )
        if operation is not None and operation_kind != operation.kind:
            raise ValidationFailed(
                "operation_kind does not match the linked operation",
                details={"operation_id": operation.id, "provided": operation_kind, "linked": operation.kind},
            )
        if event is not None and operation_id is not None and event.operation_id not in {None, operation_id}:
            raise ValidationFailed("linked event and operation do not agree", details={"event_id": event.id})
        return {
            "sequence": sequence,
            "activity_kind": kind,
            "operation_kind": operation_kind,
            "npt_category": npt_category,
            "title": title,
            "description": description,
            "start_at": start_at,
            "start_time_basis": start_basis,
            "end_at": end_at,
            "end_time_basis": end_basis,
            "duration_seconds": duration_seconds,
            "duration_source_value": duration_value,
            "duration_source_unit": duration_unit,
            "duration_basis": duration_basis,
            "depth_from_si": depth_from_si,
            "depth_from_source_value": depth_from_value,
            "depth_from_source_unit": depth_from_unit,
            "depth_to_si": depth_to_si,
            "depth_to_source_value": depth_to_value,
            "depth_to_source_unit": depth_to_unit,
            "depth_reference": depth_reference,
            "depth_basis": depth_basis,
            "cause_basis": cause_basis,
            "resolution_state": "manual" if source_kind == "manual" else "proposed",
            **evidence,
            "operation_id": operation.id if operation is not None else operation_id,
            "event_id": event.id if event is not None else event_id,
        }

    async def _prepare_measurement(
        self,
        value: Any,
        *,
        scope: _ResolvedWell,
        source_kind: str,
        allow_unvalidated_record: bool,
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValidationFailed("each measurement must be an object")
        key = value.get("parameter_key")
        if key not in DAILY_REPORT_PARAMETER_SPECS:
            raise ValidationFailed(
                "parameter_key is not recognized",
                details={"value": key, "allowed": sorted(DAILY_REPORT_PARAMETER_SPECS)},
            )
        dimension, canonical, required_reference = DAILY_REPORT_PARAMETER_SPECS[key]
        input_value = _finite_number(value.get("value"), "value")
        if input_value is None:
            raise ValidationFailed("measurement value is required")
        input_unit = _nonempty_text(value.get("unit"), "unit", 24)
        if input_value < 0 and key != "mud_temperature":
            raise ValidationFailed("measurement value must be non-negative", details={"parameter_key": key})
        value_si = _convert_checked(input_value, input_unit, canonical, "value")
        if value_si < 0 and key != "mud_temperature":
            raise ValidationFailed("canonical measurement value must be non-negative", details={"parameter_key": key})
        value_basis = _time_basis(value.get("value_basis", "reported"), "value_basis", present=True)
        observed_at = _aware_utc(value.get("observed_at"), "observed_at")
        time_basis = _time_basis(value.get("time_basis", "unknown"), "time_basis", present=observed_at is not None)

        depth_value = _finite_number(value.get("depth_value"), "depth_value", minimum=0.0)
        depth_unit = _optional_text(value.get("depth_unit"), "depth_unit", 24)
        if (depth_value is None) != (depth_unit is None):
            raise ValidationFailed("depth_value and depth_unit must be supplied together")
        depth_reference = value.get("depth_reference") or required_reference or "unknown"
        if depth_reference not in REPORT_DEPTH_REFERENCES:
            raise ValidationFailed("depth_reference is not recognized", details={"allowed": list(REPORT_DEPTH_REFERENCES)})
        if required_reference is not None and depth_reference != required_reference:
            raise ValidationFailed(
                "measurement depth reference does not match its parameter contract",
                details={"parameter_key": key, "required": required_reference, "provided": depth_reference},
            )
        depth_basis = _time_basis(value.get("depth_basis", "unknown"), "depth_basis", present=depth_value is not None)
        if depth_value is not None and depth_reference == "unknown":
            raise ValidationFailed("a depth reference is required when measurement depth is supplied")
        depth_si = (
            _convert_checked(depth_value, depth_unit or "", "m", "depth_value", minimum=0.0)
            if depth_value is not None
            else None
        )
        evidence = await self._validate_evidence(
            scope,
            source_document_id=value.get("source_document_id"),
            source_record_id=value.get("source_record_id"),
            source_region_id=value.get("source_region_id"),
            source_page_number=value.get("source_page_number"),
            allow_unvalidated_record=allow_unvalidated_record,
        )
        return {
            "parameter_key": key,
            "ordinal": 0,
            "dimension": dimension,
            "canonical_unit": canonical,
            "value_si": value_si,
            "source_value": input_value,
            "source_unit": input_unit,
            "accepted_input_value": input_value,
            "accepted_input_unit": input_unit,
            "value_basis": value_basis,
            "observed_at": observed_at,
            "time_basis": time_basis,
            "depth_si": depth_si,
            "depth_source_value": depth_value,
            "depth_source_unit": depth_unit,
            "depth_reference": depth_reference,
            "depth_basis": depth_basis,
            "resolution_state": "manual" if source_kind == "manual" else "proposed",
            **evidence,
        }

    async def _validate_evidence(
        self,
        scope: _ResolvedWell,
        *,
        source_document_id: Any = None,
        source_record_id: Any = None,
        source_region_id: Any = None,
        source_page_number: Any = None,
        operation_id: str | None = None,
        allow_unvalidated_record: bool,
    ) -> dict[str, Any]:
        document_id = _optional_text(source_document_id, "source_document_id", 64)
        record_id = _optional_text(source_record_id, "source_record_id", 64)
        region_id = _optional_text(source_region_id, "source_region_id", 64)
        page_number = source_page_number
        if page_number is not None and (isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1):
            raise ValidationFailed("source_page_number must be a positive integer")

        record: ExtractedRecord | None = None
        source_section_id: str | None = None
        if record_id:
            record = (
                await self.session.execute(
                    select(ExtractedRecord).where(
                        ExtractedRecord.id == record_id,
                        ExtractedRecord.org_id == self.org_id,
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                raise NotFound(f"source record {record_id!r} not found")
            if record.validation_state in {"rejected", "superseded"}:
                raise ValidationFailed(
                    "rejected or superseded extracted records cannot be used as current report evidence",
                    details={"source_record_id": record.id, "validation_state": record.validation_state},
                )
            source_section_id = record.section_id
            if not allow_unvalidated_record:
                validate_source_record(
                    record,
                    project_id=scope.project_id,
                    well_id=scope.well_id,
                    wellbore_id=scope.wellbore_id,
                    section_id=source_section_id,
                    operation_id=operation_id,
                )
            else:
                validate_source_scope(
                    record,
                    project_id=scope.project_id,
                    well_id=scope.well_id,
                    wellbore_id=scope.wellbore_id,
                    section_id=source_section_id,
                    operation_id=operation_id,
                )
            await self._require_source_section_scope(scope, source_section_id)
            if document_id is not None and document_id != record.document_id:
                raise ValidationFailed("source_record_id does not belong to source_document_id")
            document_id = document_id or record.document_id
            if record.region_id is not None:
                if region_id is not None and region_id != record.region_id:
                    raise ValidationFailed("source_record_id and source_region_id identify different regions")
                region_id = region_id or record.region_id
            if record.page_number is not None:
                if page_number is not None and page_number != record.page_number:
                    raise ValidationFailed("source_record_id and source_page_number identify different pages")
                page_number = page_number or record.page_number

        async def validate_document_scope(source_document: Document) -> None:
            nonlocal source_section_id
            if record is None:
                source_section_id = source_document.section_id
            elif source_document.section_id != source_section_id:
                raise ValidationFailed(
                    "source_record_id and source_document_id have different section scopes",
                    details={
                        "source_record_id": record.id,
                        "record_section_id": source_section_id,
                        "document_section_id": source_document.section_id,
                    },
                )
            validate_source_scope(
                source_document,
                project_id=scope.project_id,
                well_id=scope.well_id,
                wellbore_id=scope.wellbore_id,
                section_id=source_section_id,
                operation_id=operation_id,
            )
            await self._require_source_section_scope(scope, source_section_id)

        document: Document | None = None
        if document_id:
            document = (
                await self.session.execute(
                    select(Document)
                    .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
                    .where(
                        Document.id == document_id,
                        Document.org_id == self.org_id,
                        document_not_quarantined_condition(Document, RawArtifact),
                    )
                )
            ).scalar_one_or_none()
            if document is None:
                raise NotFound(f"source document {document_id!r} not found or is quarantined")
            await validate_document_scope(document)
        if region_id:
            region = (
                await self.session.execute(
                    select(DocumentRegion).where(
                        DocumentRegion.id == region_id,
                        DocumentRegion.org_id == self.org_id,
                    )
                )
            ).scalar_one_or_none()
            if region is None:
                raise NotFound(f"source region {region_id!r} not found")
            if document_id is not None and region.document_id != document_id:
                raise ValidationFailed("source_region_id does not belong to source_document_id")
            if document_id is None:
                document_id = region.document_id
                document = (
                    await self.session.execute(
                        select(Document)
                        .outerjoin(RawArtifact, RawArtifact.id == Document.raw_artifact_id)
                        .where(
                            Document.id == document_id,
                            Document.org_id == self.org_id,
                            document_not_quarantined_condition(Document, RawArtifact),
                        )
                    )
                ).scalar_one_or_none()
                if document is None:
                    raise NotFound(f"source document {document_id!r} not found or is quarantined")
                await validate_document_scope(document)
            if region.page_number is not None:
                if page_number is not None and page_number != region.page_number:
                    raise ValidationFailed("source_region_id and source_page_number identify different pages")
                page_number = page_number or region.page_number
        if page_number is not None and document_id is None:
            raise ValidationFailed("source_page_number requires a source document or source record")
        return {
            "source_document_id": document_id,
            "source_record_id": record.id if record else record_id,
            "source_region_id": region_id,
            "source_page_number": page_number,
        }

    async def _validate_operation_link(
        self,
        scope: _ResolvedWell,
        operation_id: str | None,
        operation_kind: str | None,
    ) -> Operation | None:
        if operation_id is None:
            return None
        operation = (
            await self.session.execute(
                select(Operation).where(
                    Operation.id == operation_id,
                    Operation.org_id == self.org_id,
                    Operation.well_id == scope.well_id,
                    Operation.wellbore_id == scope.wellbore_id,
                    *operational_source_conditions(Operation),
                )
            )
        ).scalar_one_or_none()
        if operation is None:
            raise NotFound(f"linked operation {operation_id!r} not found")
        if operation.operation_class != "actual" or operation.status == "cancelled":
            raise ValidationFailed(
                "daily report activities may link only to non-cancelled actual operations",
                details={"operation_id": operation.id, "operation_class": operation.operation_class, "status": operation.status},
            )
        return operation

    async def _validate_event_link(
        self,
        scope: _ResolvedWell,
        event_id: str | None,
        operation_id: str | None,
    ) -> Event | None:
        if event_id is None:
            return None
        event = (
            await self.session.execute(
                select(Event).where(
                    Event.id == event_id,
                    Event.org_id == self.org_id,
                    Event.well_id == scope.well_id,
                    (Event.wellbore_id.is_(None) | (Event.wellbore_id == scope.wellbore_id)),
                    *operational_source_conditions(Event),
                )
            )
        ).scalar_one_or_none()
        if event is None:
            raise NotFound(f"linked event {event_id!r} not found")
        if event.status == "cancelled":
            raise ValidationFailed("a cancelled event cannot be linked to a current report activity")
        if operation_id is not None and event.operation_id not in {None, operation_id}:
            raise ValidationFailed("linked event and operation do not agree", details={"event_id": event.id})
        return event

    # ------------------------------------------------------------------ persistence helpers

    def _activity_rows(
        self,
        revision_id: str,
        values: list[dict[str, Any]],
        *,
        resolution_state: str,
    ) -> list[DailyReportActivity]:
        rows = []
        for value in values:
            row_values = dict(value)
            # The default is selected by the lifecycle, not by a client-controlled field.
            row_values["resolution_state"] = (
                "manual" if resolution_state == "manual" or row_values.get("source_record_id") is None else resolution_state
            )
            rows.append(
                DailyReportActivity(
                    org_id=self.org_id,
                    revision_id=revision_id,
                    **row_values,
                )
            )
        return rows

    def _measurement_rows(
        self,
        revision_id: str,
        values: list[dict[str, Any]],
        *,
        resolution_state: str,
    ) -> list[DailyReportMeasurement]:
        rows = []
        ordinals: dict[str, int] = {}
        for value in values:
            row_values = dict(value)
            key = row_values["parameter_key"]
            row_values["ordinal"] = ordinals.get(key, 0)
            ordinals[key] = row_values["ordinal"] + 1
            row_values["resolution_state"] = (
                "manual" if resolution_state == "manual" or row_values.get("source_record_id") is None else resolution_state
            )
            rows.append(
                DailyReportMeasurement(
                    org_id=self.org_id,
                    revision_id=revision_id,
                    **row_values,
                )
            )
        return rows

    @staticmethod
    def _clone_activities(revision_id: str, source: list[DailyReportActivity]) -> list[DailyReportActivity]:
        names = (
            "sequence", "activity_kind", "operation_kind", "npt_category", "title", "description",
            "start_at", "start_time_basis", "end_at", "end_time_basis", "duration_seconds",
            "duration_source_value", "duration_source_unit", "duration_basis", "depth_from_si",
            "depth_from_source_value", "depth_from_source_unit", "depth_to_si", "depth_to_source_value",
            "depth_to_source_unit", "depth_reference", "depth_basis", "cause_basis", "resolution_state",
            "source_document_id", "source_record_id", "source_region_id", "source_page_number", "operation_id", "event_id",
        )
        return [
            DailyReportActivity(
                org_id=row.org_id,
                revision_id=revision_id,
                **{name: getattr(row, name) for name in names},
            )
            for row in source
        ]

    @staticmethod
    def _clone_measurements(revision_id: str, source: list[DailyReportMeasurement]) -> list[DailyReportMeasurement]:
        names = (
            "parameter_key", "ordinal", "dimension", "canonical_unit", "value_si", "source_value", "source_unit",
            "accepted_input_value", "accepted_input_unit", "value_basis", "observed_at", "time_basis", "depth_si",
            "depth_source_value", "depth_source_unit", "depth_reference", "depth_basis", "resolution_state",
            "source_document_id", "source_record_id", "source_region_id", "source_page_number",
        )
        return [
            DailyReportMeasurement(
                org_id=row.org_id,
                revision_id=revision_id,
                **{name: getattr(row, name) for name in names},
            )
            for row in source
        ]

    async def _children(
        self, revision_id: str
    ) -> tuple[list[DailyReportActivity], list[DailyReportMeasurement]]:
        activities = (
            await self.session.execute(
                select(DailyReportActivity)
                .where(DailyReportActivity.org_id == self.org_id, DailyReportActivity.revision_id == revision_id)
                .order_by(DailyReportActivity.sequence.asc(), DailyReportActivity.id.asc())
            )
        ).scalars().all()
        measurements = (
            await self.session.execute(
                select(DailyReportMeasurement)
                .where(DailyReportMeasurement.org_id == self.org_id, DailyReportMeasurement.revision_id == revision_id)
                .order_by(DailyReportMeasurement.parameter_key.asc(), DailyReportMeasurement.ordinal.asc())
            )
        ).scalars().all()
        return list(activities), list(measurements)

    def _content_hash(
        self,
        report: DailyDrillingReport,
        revision: DailyDrillingReportRevision,
        activities: list[DailyReportActivity],
        measurements: list[DailyReportMeasurement],
    ) -> str:
        report_state = {
            "id": report.id,
            "well_id": report.well_id,
            "wellbore_id": report.wellbore_id,
            "report_date": report.report_date,
            "report_timezone": report.report_timezone,
            "shift_code": report.shift_code,
            "shift_label": report.shift_label,
            "report_number": report.report_number,
            "identity_key": report.identity_key,
        }
        revision_state = {
            "revision_number": revision.revision_number,
            "source_kind": revision.source_kind,
            "source_document_id": revision.source_document_id,
            "source_record_id": revision.source_record_id,
            "source_region_id": revision.source_region_id,
            "source_page_number": revision.source_page_number,
            "period_start_at": revision.period_start_at,
            "period_end_at": revision.period_end_at,
            "summary": revision.summary,
            "operations_summary": revision.operations_summary,
            "safety_notes": revision.safety_notes,
            "next_period_plan": revision.next_period_plan,
            "environmental_notes": revision.environmental_notes,
            "supersedes_revision_id": revision.supersedes_revision_id,
        }
        return content_hash(
            {
                "report": report_state,
                "revision": revision_state,
                "activities": [self._activity_hash_value(row) for row in activities],
                "measurements": [self._measurement_hash_value(row) for row in measurements],
            }
        )

    @staticmethod
    def _activity_hash_value(row: DailyReportActivity) -> dict[str, Any]:
        names = (
            "sequence", "activity_kind", "operation_kind", "npt_category", "title", "description",
            "start_at", "start_time_basis", "end_at", "end_time_basis", "duration_seconds",
            "duration_source_value", "duration_source_unit", "duration_basis", "depth_from_si",
            "depth_from_source_value", "depth_from_source_unit", "depth_to_si", "depth_to_source_value",
            "depth_to_source_unit", "depth_reference", "depth_basis", "cause_basis", "resolution_state",
            "source_document_id", "source_record_id", "source_region_id", "source_page_number", "operation_id", "event_id",
        )
        return {name: getattr(row, name) for name in names}

    @staticmethod
    def _measurement_hash_value(row: DailyReportMeasurement) -> dict[str, Any]:
        names = (
            "parameter_key", "ordinal", "dimension", "canonical_unit", "value_si", "source_value", "source_unit",
            "accepted_input_value", "accepted_input_unit", "value_basis", "observed_at", "time_basis", "depth_si",
            "depth_source_value", "depth_source_unit", "depth_reference", "depth_basis", "resolution_state",
            "source_document_id", "source_record_id", "source_region_id", "source_page_number",
        )
        return {name: getattr(row, name) for name in names}

    @staticmethod
    def _require_hash_matches(revision: DailyDrillingReportRevision, computed: str) -> None:
        if not revision.content_hash or revision.content_hash != computed:
            raise Conflict(
                "daily report content does not match its stored revision fingerprint",
                details={
                    "revision_number": revision.revision_number,
                    "expected_content_hash": revision.content_hash,
                    "computed_content_hash": computed,
                },
            )

    async def _validate_revision_sources(
        self,
        report: DailyDrillingReport,
        revision: DailyDrillingReportRevision,
        activities: list[DailyReportActivity],
        measurements: list[DailyReportMeasurement],
        *,
        scope: _ResolvedWell,
    ) -> None:
        await self._validate_evidence(
            scope,
            source_document_id=revision.source_document_id,
            source_record_id=revision.source_record_id,
            source_region_id=revision.source_region_id,
            source_page_number=revision.source_page_number,
            allow_unvalidated_record=revision.source_kind != "manual",
        )
        for row in activities:
            await self._validate_evidence(
                scope,
                source_document_id=row.source_document_id,
                source_record_id=row.source_record_id,
                source_region_id=row.source_region_id,
                source_page_number=row.source_page_number,
                operation_id=row.operation_id,
                allow_unvalidated_record=revision.source_kind != "manual",
            )
            await self._validate_operation_link(scope, row.operation_id, row.operation_kind)
            await self._validate_event_link(scope, row.event_id, row.operation_id)
        for row in measurements:
            await self._validate_evidence(
                scope,
                source_document_id=row.source_document_id,
                source_record_id=row.source_record_id,
                source_region_id=row.source_region_id,
                source_page_number=row.source_page_number,
                allow_unvalidated_record=revision.source_kind != "manual",
            )
        reconciliation = (
            await self.session.execute(
                select(DailyReportReconciliation).where(
                    DailyReportReconciliation.org_id == self.org_id,
                    DailyReportReconciliation.revision_id == revision.id,
                )
            )
        ).scalars().all()
        for item in reconciliation:
            await self._assert_reconciliation_source_current(item, scope)

    async def _raise_stale_revision(self, report_id: str, revision_number: int, expected: int) -> None:
        current = (
            await self.session.execute(
                select(DailyDrillingReportRevision).where(
                    DailyDrillingReportRevision.org_id == self.org_id,
                    DailyDrillingReportRevision.report_id == report_id,
                    DailyDrillingReportRevision.revision_number == revision_number,
                )
            )
        ).scalar_one_or_none()
        raise Conflict(
            "daily report revision changed since it was read",
            details={
                "revision_number": revision_number,
                "expected_edit_version": expected,
                "current_edit_version": current.edit_version if current else None,
                "current_status": current.status if current else None,
            },
        )

    @staticmethod
    def _require_editable(revision: DailyDrillingReportRevision, expected: int) -> None:
        DailyReportService._require_current_version(revision, expected, expected_status="draft")

    @staticmethod
    def _require_current_version(
        revision: DailyDrillingReportRevision,
        expected: int,
        *,
        expected_status: str | None,
    ) -> None:
        if expected < 1:
            raise ValidationFailed("expected_edit_version must be a positive integer")
        if revision.edit_version != expected or (expected_status is not None and revision.status != expected_status):
            raise Conflict(
                "daily report revision changed since it was read",
                details={
                    "revision_number": revision.revision_number,
                    "expected_edit_version": expected,
                    "current_edit_version": revision.edit_version,
                    "current_status": revision.status,
                },
            )

    async def _require_reconciled(
        self,
        activities: list[DailyReportActivity],
        measurements: list[DailyReportMeasurement],
        *,
        revision_id: str,
    ) -> None:
        pending_activities = [row.id for row in activities if row.resolution_state in {"proposed", "unresolved"}]
        pending_measurements = [row.id for row in measurements if row.resolution_state in {"proposed", "unresolved"}]
        pending_decisions = (
            await self.session.execute(
                select(DailyReportReconciliation.target_key).where(
                    DailyReportReconciliation.org_id == self.org_id,
                    DailyReportReconciliation.revision_id == revision_id,
                    DailyReportReconciliation.resolution_state.in_(("proposed", "unresolved")),
                ).order_by(DailyReportReconciliation.target_key.asc())
            )
        ).scalars().all()
        if pending_activities or pending_measurements or pending_decisions:
            raise ValidationFailed(
                "all imported or unresolved report fields and rows must be reconciled before submission",
                details={
                    "activity_ids": pending_activities[:50],
                    "measurement_ids": pending_measurements[:50],
                    "activity_count": len(pending_activities),
                    "measurement_count": len(pending_measurements),
                    "target_keys": list(pending_decisions[:100]),
                    "target_count": len(pending_decisions),
                },
            )

    async def _record_change(
        self,
        report: DailyDrillingReport,
        revision: DailyDrillingReportRevision,
        *,
        action: str,
        change_type: str,
        permission: str,
        fields: list[str],
        before: dict[str, Any],
        after: dict[str, Any],
        reason: str | None,
    ) -> None:
        project_id = (
            await self.session.execute(
                select(Well.project_id).where(Well.id == report.well_id, Well.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self.principal,
            action=action,
            resource_kind="daily_drilling_report",
            resource_id=report.id,
            project_id=project_id,
            well_id=report.well_id,
            permission=permission,
            request_id=self.request_id,
            before=before,
            after=after,
            details={
                "report_id": report.id,
                "wellbore_id": report.wellbore_id,
                "revision_number": revision.revision_number,
                "edit_version": revision.edit_version,
                "reason": reason,
                "change_type": change_type,
                "content_hash": revision.content_hash,
            },
        )
        principal = self.principal
        await TwinService(self.session, org_id=self.org_id).record_change(
            report.well_id,
            ChangeEvent(
                subject_kind="daily_drilling_report",
                subject_id=report.id,
                change_type=change_type,
                field_paths=fields,
                before=before,
                after=after,
                reason=reason,
                actor_kind=principal.kind if principal else "system",
                actor_id=principal.id if principal else None,
                source="daily_report",
                source_ref=f"{report.id}:revision:{revision.revision_number}",
            ),
            project_id=project_id,
            version=revision.edit_version,
            trace_id=self.request_id,
        )

    # ------------------------------------------------------------------ explicit response serializers

    def _report_out(
        self,
        report: DailyDrillingReport,
        latest: DailyDrillingReportRevision | None,
    ) -> dict[str, Any]:
        return {
            "id": report.id,
            "well_id": report.well_id,
            "wellbore_id": report.wellbore_id,
            "report_date": report.report_date.isoformat(),
            "report_timezone": report.report_timezone,
            "shift_code": report.shift_code,
            "shift_label": report.shift_label,
            "report_number": report.report_number,
            "identity_key": report.identity_key,
            "latest_revision_number": report.latest_revision_number,
            "latest_approved_revision_number": report.latest_approved_revision_number,
            "created_by": report.created_by,
            "created_at": _iso(report.created_at),
            "updated_at": _iso(report.updated_at),
            "latest_revision": self._revision_summary(latest) if latest else None,
        }

    def _revision_summary(self, revision: DailyDrillingReportRevision | None) -> dict[str, Any] | None:
        if revision is None:
            return None
        return {
            "id": revision.id,
            "report_id": revision.report_id,
            "revision_number": revision.revision_number,
            "edit_version": revision.edit_version,
            "status": revision.status,
            "source_kind": revision.source_kind,
            "source_document_id": revision.source_document_id,
            "source_record_id": revision.source_record_id,
            "source_region_id": revision.source_region_id,
            "source_page_number": revision.source_page_number,
            "period_start_at": _iso(revision.period_start_at),
            "period_end_at": _iso(revision.period_end_at),
            "summary": revision.summary,
            "operations_summary": revision.operations_summary,
            "safety_notes": revision.safety_notes,
            "next_period_plan": revision.next_period_plan,
            "environmental_notes": revision.environmental_notes,
            "supersedes_revision_id": revision.supersedes_revision_id,
            "created_by": revision.created_by,
            "created_at": _iso(revision.created_at),
            "updated_at": _iso(revision.updated_at),
            "submitted_by": revision.submitted_by,
            "submitted_at": _iso(revision.submitted_at),
            "decided_by": revision.decided_by,
            "decided_at": _iso(revision.decided_at),
            "decision_reason": revision.decision_reason,
            "content_hash": revision.content_hash,
        }

    async def _revision_detail(
        self,
        report: DailyDrillingReport,
        revision: DailyDrillingReportRevision,
        activities: list[DailyReportActivity] | None = None,
        measurements: list[DailyReportMeasurement] | None = None,
    ) -> dict[str, Any]:
        if activities is None or measurements is None:
            activities, measurements = await self._children(revision.id)
        calculated_hash = self._content_hash(report, revision, activities, measurements)
        reconciliation_rows = (
            await self.session.execute(
                select(DailyReportReconciliation)
                .where(
                    DailyReportReconciliation.org_id == self.org_id,
                    DailyReportReconciliation.revision_id == revision.id,
                )
                .order_by(DailyReportReconciliation.target_key.asc())
            )
        ).scalars().all()
        summary = self._revision_summary(revision) or {}
        activity_by_id = {row.id: row for row in activities}
        measurement_by_id = {row.id: row for row in measurements}
        reconciliation_out = []
        for row in reconciliation_rows:
            current_value: Any = None
            target_exists = False
            if row.target_kind == "revision_field" and row.field_key in REPORT_RECONCILIATION_REVISION_FIELDS:
                current_value = getattr(revision, row.field_key)
                target_exists = True
            elif row.target_kind == "report_identity" and row.field_key in REPORT_RECONCILIATION_IDENTITY_FIELDS:
                current_value = getattr(report, row.field_key)
                if isinstance(current_value, dt.date):
                    current_value = current_value.isoformat()
                target_exists = True
            elif row.target_kind == "activity":
                activity = activity_by_id.get(row.target_id or "")
                if activity is not None:
                    current_value = self._activity_proposal_value(activity)
                    target_exists = True
            elif row.target_kind == "measurement":
                measurement = measurement_by_id.get(row.target_id or "")
                if measurement is not None:
                    current_value = self._measurement_proposal_value(measurement)
                    target_exists = True
            serialized = self._reconciliation_out(row)
            serialized["proposal_matches_current"] = target_exists and content_hash(current_value) == row.proposed_value_hash
            reconciliation_out.append(serialized)
        return {
            **summary,
            "content_hash_verified": bool(revision.content_hash and revision.content_hash == calculated_hash),
            "activities": [self._activity_out(row) for row in activities],
            "measurements": [self._measurement_out(row) for row in measurements],
            "reconciliation": reconciliation_out,
        }

    @staticmethod
    def _reconciliation_out(row: DailyReportReconciliation) -> dict[str, Any]:
        return {
            "id": row.id,
            "target_key": row.target_key,
            "target_kind": row.target_kind,
            "target_id": row.target_id,
            "field_key": row.field_key,
            "source_document_id": row.source_document_id,
            "source_record_id": row.source_record_id,
            "source_region_id": row.source_region_id,
            "source_page_number": row.source_page_number,
            "source_path": row.source_path,
            "source_review_version": row.source_review_version,
            "source_fingerprint": row.source_fingerprint,
            "source_value_hash": row.source_value_hash,
            "proposed_value_hash": row.proposed_value_hash,
            "resolution_state": row.resolution_state,
            "reason": row.reason,
            "decided_by": row.decided_by,
            "decided_at": _iso(row.decided_at),
        }

    @staticmethod
    def _activity_out(row: DailyReportActivity) -> dict[str, Any]:
        return {
            "id": row.id,
            "sequence": row.sequence,
            "activity_kind": row.activity_kind,
            "operation_kind": row.operation_kind,
            "npt_category": row.npt_category,
            "title": row.title,
            "description": row.description,
            "start_at": _iso(row.start_at),
            "start_time_basis": row.start_time_basis,
            "end_at": _iso(row.end_at),
            "end_time_basis": row.end_time_basis,
            "duration_seconds": row.duration_seconds,
            "duration_source_value": row.duration_source_value,
            "duration_source_unit": row.duration_source_unit,
            "duration_basis": row.duration_basis,
            "depth_from_si": row.depth_from_si,
            "depth_from_source_value": row.depth_from_source_value,
            "depth_from_source_unit": row.depth_from_source_unit,
            "depth_to_si": row.depth_to_si,
            "depth_to_source_value": row.depth_to_source_value,
            "depth_to_source_unit": row.depth_to_source_unit,
            "depth_reference": row.depth_reference,
            "depth_basis": row.depth_basis,
            "cause_basis": row.cause_basis,
            "resolution_state": row.resolution_state,
            "source_document_id": row.source_document_id,
            "source_record_id": row.source_record_id,
            "source_region_id": row.source_region_id,
            "source_page_number": row.source_page_number,
            "operation_id": row.operation_id,
            "event_id": row.event_id,
        }

    @staticmethod
    def _measurement_out(row: DailyReportMeasurement) -> dict[str, Any]:
        return {
            "id": row.id,
            "parameter_key": row.parameter_key,
            "ordinal": row.ordinal,
            "dimension": row.dimension,
            "canonical_unit": row.canonical_unit,
            "value_si": row.value_si,
            "source_value": row.source_value,
            "source_unit": row.source_unit,
            "accepted_input_value": row.accepted_input_value,
            "accepted_input_unit": row.accepted_input_unit,
            "value_basis": row.value_basis,
            "observed_at": _iso(row.observed_at),
            "time_basis": row.time_basis,
            "depth_si": row.depth_si,
            "depth_source_value": row.depth_source_value,
            "depth_source_unit": row.depth_source_unit,
            "depth_reference": row.depth_reference,
            "depth_basis": row.depth_basis,
            "resolution_state": row.resolution_state,
            "source_document_id": row.source_document_id,
            "source_record_id": row.source_record_id,
            "source_region_id": row.source_region_id,
            "source_page_number": row.source_page_number,
        }

    def _revision_audit_state(
        self,
        revision: DailyDrillingReportRevision,
        activities: list[DailyReportActivity],
        measurements: list[DailyReportMeasurement],
    ) -> dict[str, Any]:
        return {
            "revision_number": revision.revision_number,
            "edit_version": revision.edit_version,
            "status": revision.status,
            "content_hash": revision.content_hash,
            "activity_count": len(activities),
            "measurement_count": len(measurements),
            "source_kind": revision.source_kind,
        }


# --------------------------------------------------------------------------- pure validation helpers


_MISSING = object()


def _record_payload_path(payload: dict[str, Any], source_path: str) -> Any:
    """Resolve a bounded dot path within an extracted record payload; never accepts ORM/SQL paths."""
    parts = source_path.split(".")
    if not parts or parts[0] != "payload" or any(not part for part in parts):
        raise ValidationFailed("source_path must begin with payload and use non-empty field names")
    value: Any = payload
    for part in parts[1:]:
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def _normalize_identifier(value: str) -> str:
    return " ".join(value.casefold().split())


def _nonempty_text(value: Any, field: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValidationFailed(f"{field} must be text")
    normalized = value.strip()
    if not normalized:
        raise ValidationFailed(f"{field} is required")
    if len(normalized) > max_length:
        raise ValidationFailed(f"{field} exceeds {max_length} characters")
    return normalized


def _optional_text(value: Any, field: str, max_length: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationFailed(f"{field} must be text or null")
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > max_length:
        raise ValidationFailed(f"{field} exceeds {max_length} characters")
    return normalized


def _finite_number(value: Any, field: str, *, minimum: float | None = None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValidationFailed(f"{field} must be a finite number")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ValidationFailed(f"{field} must be greater than or equal to {minimum}")
    return number


def _convert_checked(value: float, source_unit: str, target_unit: str, field: str, *, minimum: float | None = None) -> float:
    try:
        source = get_unit(source_unit)
        target = get_unit(target_unit)
        if source.dimension is not target.dimension:
            raise UnitError(
                f"{field} unit has the wrong dimension",
                details={"field": field, "source_unit": source_unit, "expected_unit": target_unit},
            )
        converted = convert(value, source_unit, target_unit)
    except UnitError as exc:
        raise ValidationFailed(
            f"{field} cannot be converted from {source_unit!r} to {target_unit!r}",
            details={"field": field, "source_unit": source_unit, "target_unit": target_unit, "reason": exc.message},
        ) from exc
    if not math.isfinite(converted):
        raise ValidationFailed(f"{field} conversion did not produce a finite value")
    if minimum is not None and converted < minimum:
        raise ValidationFailed(f"{field} must be greater than or equal to {minimum} after conversion")
    return converted


def _aware_utc(value: Any, field: str) -> dt.datetime | None:
    if value is None:
        return None
    if not isinstance(value, dt.datetime):
        raise ValidationFailed(f"{field} must be a timezone-aware timestamp")
    try:
        return ensure_utc(value)
    except ValueError as exc:
        raise ValidationFailed(f"{field} must include a timezone offset") from exc


def _time_basis(value: Any, field: str, *, present: bool) -> str:
    if value not in REPORT_TIME_BASES:
        raise ValidationFailed(
            f"{field} is not recognized",
            details={"field": field, "value": value, "allowed": list(REPORT_TIME_BASES)},
        )
    if present and value == "unknown":
        raise ValidationFailed(f"{field} must be explicit when its timestamp/value is present")
    if not present and value != "unknown":
        raise ValidationFailed(f"{field} must be unknown when its timestamp/value is absent")
    return value


def _required_reason(value: Any) -> str:
    return _nonempty_text(value, "reason", _MAX_REASON)


def _is_named_constraint(exc: IntegrityError, constraint_name: str) -> bool:
    diagnostic = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
    message = f"{diagnostic or ''} {exc.orig}".lower()
    return constraint_name.lower() in message or (
        constraint_name == "uq_daily_drilling_report_identity"
        and "unique constraint failed: daily_drilling_reports.org_id" in message
    ) or (
        constraint_name == "uq_daily_report_revision_number"
        and "unique constraint failed: daily_drilling_report_revisions.report_id" in message
    )


def _has_meaningful_content(
    revision: DailyDrillingReportRevision,
    activities: list[DailyReportActivity],
    measurements: list[DailyReportMeasurement],
) -> bool:
    if any(row.resolution_state != "rejected" for row in activities) or any(
        row.resolution_state != "rejected" for row in measurements
    ):
        return True
    return any(
        value and value.strip()
        for value in (
            revision.summary,
            revision.operations_summary,
            revision.safety_notes,
            revision.next_period_plan,
            revision.environmental_notes,
        )
    )


def _iso(value: dt.datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z")
