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
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, exists, func, select, update
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
    REPORT_REVISION_STATUSES,
    REPORT_SHIFT_CODES,
    REPORT_TIME_BASES,
    DailyDrillingReport,
    DailyDrillingReportRevision,
    DailyReportActivity,
    DailyReportMeasurement,
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

    async def list_reports(
        self,
        well_id: str,
        *,
        report_date_from: dt.date | None = None,
        report_date_to: dt.date | None = None,
        shift_code: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._authorize("daily_report.read", well_id=well_id)
        await self._require_well(well_id)
        self._validate_list_filters(report_date_from, report_date_to, shift_code, status, limit, offset)

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
            # Isolate the natural-key race to a savepoint so the request's idempotency reservation
            # and any authentication/session state in the outer transaction remain intact.
            async with self.session.begin_nested():
                self.session.add(report)
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
        prepared = await self._prepare_content(
            content,
            scope=scope,
            source_kind=revision.source_kind,
            allow_unvalidated_source_records=revision.source_kind != "manual",
        )
        before_activities, before_measurements = await self._children(revision.id)
        before_hash = self._content_hash(report, revision, before_activities, before_measurements)
        self._require_hash_matches(revision, before_hash)
        before = self._revision_audit_state(revision, before_activities, before_measurements)
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
        revision.content_hash = self._content_hash(report, revision, activities, measurements)
        await self.session.flush()
        after = self._revision_audit_state(revision, activities, measurements)
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

        activities = self._clone_activities(revision.id, source_activities)
        measurements = self._clone_measurements(revision.id, source_measurements)
        self.session.add_all([*activities, *measurements])
        await self.session.flush()
        revision.content_hash = self._content_hash(report, revision, activities, measurements)
        await self.session.flush()
        before = self._revision_audit_state(source, source_activities, source_measurements)
        after = self._revision_audit_state(revision, activities, measurements)
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
        self._require_reconciled(activities, measurements)
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
        self._require_reconciled(activities, measurements)
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
            if not allow_unvalidated_record:
                validate_source_record(
                    record,
                    project_id=scope.project_id,
                    well_id=scope.well_id,
                    wellbore_id=scope.wellbore_id,
                    section_id=None,
                    operation_id=operation_id,
                )
            else:
                validate_source_scope(
                    record,
                    project_id=scope.project_id,
                    well_id=scope.well_id,
                    wellbore_id=scope.wellbore_id,
                    section_id=None,
                    operation_id=operation_id,
                )
            if record.document_id is not None:
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
            validate_source_scope(
                document,
                project_id=scope.project_id,
                well_id=scope.well_id,
                wellbore_id=scope.wellbore_id,
                section_id=None,
                operation_id=operation_id,
            )
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
                validate_source_scope(
                    document,
                    project_id=scope.project_id,
                    well_id=scope.well_id,
                    wellbore_id=scope.wellbore_id,
                    section_id=None,
                    operation_id=operation_id,
                )
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
            row_values["resolution_state"] = resolution_state
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
            row_values["resolution_state"] = resolution_state
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

    def _require_reconciled(
        self,
        activities: list[DailyReportActivity],
        measurements: list[DailyReportMeasurement],
    ) -> None:
        pending_activities = [row.id for row in activities if row.resolution_state in {"proposed", "unresolved"}]
        pending_measurements = [row.id for row in measurements if row.resolution_state in {"proposed", "unresolved"}]
        if pending_activities or pending_measurements:
            raise ValidationFailed(
                "all imported or unresolved report fields must be reconciled before submission",
                details={
                    "activity_ids": pending_activities[:50],
                    "measurement_ids": pending_measurements[:50],
                    "activity_count": len(pending_activities),
                    "measurement_count": len(pending_measurements),
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
        summary = self._revision_summary(revision) or {}
        return {
            **summary,
            "content_hash_verified": bool(revision.content_hash and revision.content_hash == calculated_hash),
            "activities": [self._activity_out(row) for row in activities],
            "measurements": [self._measurement_out(row) for row in measurements],
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
    if activities or measurements:
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
