"""Durable telemetry connector worker: persistent discovery, competing-worker lease/fencing,
at-least-once ingestion through ``TelemetryService``, exponential backoff, and bounded run ledger.

Can run either as:
- a dedicated standalone process: ``python -m drillai.telemetry.worker``
- an in-process background supervisor when ``DRILLAI_CONNECTOR_WORKER_ENABLED=true``
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import os
import random
import signal
import socket
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import utc_now
from drillai.core.config import Settings, get_settings
from drillai.core.ids import new_id
from drillai.core.logging import configure_logging, get_logger
from drillai.db.models import Connector, ConnectorRun
from drillai.db.session import Database
from drillai.observability.tracing import get_metrics
from drillai.telemetry.adapters import ingest_frames
from drillai.telemetry.connectors import (
    ConnectorService,
    classify_connector_error,
    redact_sensitive_text,
    resolve_secret_refs,
    validate_connector_endpoint,
)
from drillai.telemetry.outbox import emit
from drillai.telemetry.service import TelemetryService

logger = get_logger(__name__)

ELIGIBLE_RUNTIME_STATUSES = (
    "configured",
    "starting",
    "running",
    "backing_off",
    "degraded",
)


def _ensure_aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


@dataclass(frozen=True)
class ClaimedConnector:
    connector_id: str
    org_id: str
    well_id: str
    wellbore_id: str | None
    operation_id: str | None
    protocol_profile: str
    fencing_token: int
    config_version: int
    cursor_before: dict[str, Any]
    previous_status: str
    recovered_expired_lease: bool


@dataclass(frozen=True)
class PollCycleOutcome:
    connector_id: str
    protocol_profile: str
    status: str  # "succeeded", "failed", "fenced"
    fencing_token: int
    frames_received: int = 0
    points_accepted: int = 0
    points_duplicates: int = 0
    points_revised: int = 0
    points_rejected: int = 0
    alerts_raised: int = 0
    alerts_cleared: int = 0
    cursor_before: dict[str, Any] | None = None
    cursor_after: dict[str, Any] | None = None
    error_category: str | None = None
    error_message: str | None = None
    duration_ms: float = 0.0


class ConnectorWorker:
    """Durable connector runtime worker with compare-and-swap leases and fencing tokens."""

    def __init__(
        self,
        database: Database,
        *,
        worker_id: str | None = None,
        lease_seconds: float | None = None,
        poll_interval_seconds: float | None = None,
        max_concurrency: int | None = None,
        retention_limit: int | None = None,
        allow_loopback: bool | None = None,
        before_commit_hook: Callable[[ClaimedConnector], Awaitable[None]] | None = None,
    ) -> None:
        settings = get_settings()
        host = socket.gethostname().split(".")[0]
        self.database = database
        self.worker_id = worker_id or f"wrk_{host}_{os.getpid()}_{new_id('w')[-6:]}"
        self.lease_seconds = float(
            lease_seconds
            if lease_seconds is not None
            else settings.connector_worker_lease_seconds
        )
        self.poll_interval_seconds = float(
            poll_interval_seconds
            if poll_interval_seconds is not None
            else settings.connector_worker_poll_seconds
        )
        self.max_concurrency = int(
            max_concurrency
            if max_concurrency is not None
            else settings.connector_worker_max_concurrency
        )
        self.retention_limit = int(
            retention_limit
            if retention_limit is not None
            else settings.connector_run_retention_limit
        )
        self.allow_loopback = allow_loopback
        self._before_commit_hook = before_commit_hook
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.cycles_completed = 0
        self.last_cycle_at: dt.datetime | None = None

    async def discover_candidate_ids(
        self, session: AsyncSession, *, now: dt.datetime | None = None
    ) -> list[str]:
        """Return IDs of enabled connectors that are due for polling and unleased or lease-expired."""
        current = now or utc_now()
        rows = (
            (
                await session.execute(
                    select(Connector)
                    .where(
                        Connector.is_enabled.is_(True),
                        Connector.desired_state == "enabled",
                        Connector.status.in_(ELIGIBLE_RUNTIME_STATUSES),
                    )
                    .order_by(Connector.next_poll_at.asc().nullsfirst(), Connector.id.asc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        candidates: list[str] = []
        for row in rows:
            next_poll = _ensure_aware(row.next_poll_at)
            if next_poll is not None and next_poll > current:
                continue
            lease_exp = _ensure_aware(row.lease_expires_at)
            if (
                row.worker_id is not None
                and row.worker_id != self.worker_id
                and lease_exp is not None
                and lease_exp > current
            ):
                continue
            candidates.append(row.id)
        return candidates

    async def claim_connector(
        self, connector_id: str, *, now: dt.datetime | None = None
    ) -> ClaimedConnector | None:
        """Atomically claim or renew ownership of a connector with a monotonically increasing fencing token."""
        current = now or utc_now()
        lease_until = current + dt.timedelta(seconds=self.lease_seconds)

        async with self.database.session() as session:
            row = (
                await session.execute(select(Connector).where(Connector.id == connector_id))
            ).scalar_one_or_none()
            if row is None:
                return None
            if not row.is_enabled or row.desired_state != "enabled":
                return None
            if row.status not in ELIGIBLE_RUNTIME_STATUSES:
                return None
            next_poll = _ensure_aware(row.next_poll_at)
            if next_poll is not None and next_poll > current:
                return None
            lease_exp = _ensure_aware(row.lease_expires_at)
            if (
                row.worker_id is not None
                and row.worker_id != self.worker_id
                and lease_exp is not None
                and lease_exp > current
            ):
                return None

            recovered_expired = bool(
                row.worker_id is not None
                and row.worker_id != self.worker_id
                and lease_exp is not None
                and lease_exp <= current
            )
            prev_token = int(row.fencing_token or 0)
            next_token = prev_token + 1
            next_reconnect = int(row.reconnect_count or 0) + (1 if recovered_expired else 0)
            prev_status = row.status
            cursor_before = dict(row.cursor or {})
            cfg_version = int(row.config_version or 1)
            org_id = row.org_id
            well_id = row.well_id or ""
            wellbore_id = row.wellbore_id
            operation_id = row.operation_id
            profile = row.protocol_profile

            stmt = (
                update(Connector)
                .where(
                    Connector.id == connector_id,
                    Connector.fencing_token == prev_token,
                    Connector.is_enabled.is_(True),
                    Connector.desired_state == "enabled",
                )
                .values(
                    worker_id=self.worker_id,
                    lease_expires_at=lease_until,
                    last_heartbeat_at=current,
                    fencing_token=next_token,
                    reconnect_count=next_reconnect,
                    updated_at=current,
                )
            )
            res = await session.execute(stmt)
            if (res.rowcount or 0) != 1:
                await session.rollback()
                return None
            await session.commit()

            if recovered_expired:
                get_metrics().counter(
                    "drillai.connector.reconnections_total",
                    "connector lease recoveries and reconnects",
                ).inc(1, protocol_profile=profile, reason="expired_lease_recovery")
                logger.info(
                    "reclaimed expired connector lease",
                    extra={
                        "connector_id": connector_id,
                        "worker_id": self.worker_id,
                        "fencing_token": next_token,
                    },
                )

            return ClaimedConnector(
                connector_id=connector_id,
                org_id=org_id,
                well_id=well_id,
                wellbore_id=wellbore_id,
                operation_id=operation_id,
                protocol_profile=profile,
                fencing_token=next_token,
                config_version=cfg_version,
                cursor_before=cursor_before,
                previous_status=prev_status,
                recovered_expired_lease=recovered_expired,
            )

    async def _prune_runs(self, session: AsyncSession, connector_id: str) -> None:
        """Keep at most ``self.retention_limit`` ConnectorRun rows per connector."""
        if self.retention_limit <= 0:
            return
        keep_ids = (
            (
                await session.execute(
                    select(ConnectorRun.id)
                    .where(ConnectorRun.connector_id == connector_id)
                    .order_by(ConnectorRun.started_at.desc(), ConnectorRun.id.desc())
                    .limit(self.retention_limit)
                )
            )
            .scalars()
            .all()
        )
        if len(keep_ids) >= self.retention_limit:
            await session.execute(
                delete(ConnectorRun).where(
                    ConnectorRun.connector_id == connector_id,
                    ConnectorRun.id.notin_(keep_ids),
                )
            )

    async def execute_claimed_poll(self, claimed: ClaimedConnector) -> PollCycleOutcome:
        """Execute one poll/stream batch for a claimed connector with fencing-token protection."""
        started = utc_now()
        t0 = time.perf_counter()
        trace_id = new_id("trc")
        metrics = get_metrics()
        resolved_secrets: dict[str, str] = {}

        try:
            # Phase 1: Load config & resolve secrets in a short read session (never hold a DB transaction across network I/O).
            async with self.database.session() as read_session:
                row = (
                    await read_session.execute(
                        select(Connector).where(Connector.id == claimed.connector_id)
                    )
                ).scalar_one_or_none()
                if (
                    row is None
                    or row.worker_id != self.worker_id
                    or int(row.fencing_token or 0) != claimed.fencing_token
                    or not row.is_enabled
                    or row.desired_state != "enabled"
                ):
                    return self._record_fenced_outcome(claimed, t0)

                cfg = dict(row.config or {})
                row_config_version = int(row.config_version or claimed.config_version)
                validate_connector_endpoint(
                    row.endpoint_url,
                    row.protocol_profile,
                    allow_loopback=self.allow_loopback,
                )
                resolved_secrets = await resolve_secret_refs(
                    read_session, row.org_id, dict(row.secret_refs or {})
                )
                conn_svc = ConnectorService(read_session, row.org_id)
                adapter = await conn_svc.build_adapter(row, resolved_secrets=resolved_secrets)

            # Perform external protocol connect/fetch outside the DB transaction.
            await adapter.connect()
            if self._before_commit_hook is not None:
                await self._before_commit_hook(claimed)

            # Phase 2: Short atomic DB transaction — verify fencing token, ingest via TelemetryService, advance cursor, commit.
            async with self.database.session() as session:
                current_row = (
                    await session.execute(
                        select(Connector).where(Connector.id == claimed.connector_id)
                    )
                ).scalar_one_or_none()
                if (
                    current_row is None
                    or current_row.worker_id != self.worker_id
                    or int(current_row.fencing_token or 0) != claimed.fencing_token
                    or not current_row.is_enabled
                    or current_row.desired_state != "enabled"
                ):
                    await adapter.close()
                    await session.rollback()
                    return self._record_fenced_outcome(claimed, t0)

                telemetry_svc = TelemetryService(session, claimed.org_id)
                ingest_report = await ingest_frames(
                    telemetry_svc,
                    adapter,
                    well_id=claimed.well_id,
                    wellbore_id=claimed.wellbore_id,
                    operation_id=claimed.operation_id,
                )

                new_cursor = dict(getattr(adapter, "cursor", None) or claimed.cursor_before)
                report_dict = ingest_report.to_dict()
                totals = report_dict["totals"]
                protocol_dups = int(
                    getattr(getattr(adapter, "last_batch", None), "duplicates_suppressed", 0)
                    or 0
                )
                frames_received = int(ingest_report.frames)
                accepted = int(totals.get("accepted", 0))
                duplicates = int(totals.get("duplicates", 0)) + protocol_dups
                revised = int(totals.get("revised", 0))
                rejected = int(totals.get("rejected", 0))
                alerts_raised = int(report_dict.get("alerts_raised", 0))
                alerts_cleared = int(report_dict.get("alerts_cleared", 0))

                low_quality_count = 0
                for ch_rep in ingest_report.reports.values():
                    for q_label, q_num in (ch_rep.quality_counts or {}).items():
                        if str(q_label).lower() not in ("good", "questionable"):
                            low_quality_count += int(q_num or 0)
                if frames_received > 0:
                    new_cursor["last_batch_low_quality"] = low_quality_count > 0

                finished = utc_now()
                duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                poll_interval = float(cfg.get("poll_interval_seconds", 2.0))
                next_poll_at = finished + dt.timedelta(seconds=poll_interval)
                lease_until = finished + dt.timedelta(seconds=self.lease_seconds)

                update_values: dict[str, Any] = {
                    "status": "running",
                    "cursor": new_cursor,
                    "error_count": 0,
                    "backoff_seconds": 0.0,
                    "last_error": None,
                    "last_error_category": None,
                    "last_error_at": None,
                    "last_connected_at": finished,
                    "last_poll_at": finished,
                    "last_successful_poll_at": finished,
                    "next_poll_at": next_poll_at,
                    "lease_expires_at": lease_until,
                    "last_heartbeat_at": finished,
                    "last_trace_id": trace_id,
                    "updated_at": finished,
                }
                if frames_received > 0:
                    update_values["last_frame_at"] = finished
                    update_values["last_ingest_at"] = finished
                    update_values["last_sync_at"] = finished
                if claimed.previous_status != "running":
                    update_values["last_transition_at"] = finished

                # Atomic fencing-token check on Connector update before committing the transaction!
                fenced_update = await session.execute(
                    update(Connector)
                    .where(
                        Connector.id == claimed.connector_id,
                        Connector.worker_id == self.worker_id,
                        Connector.fencing_token == claimed.fencing_token,
                        Connector.is_enabled.is_(True),
                        Connector.desired_state == "enabled",
                    )
                    .values(**update_values)
                )
                if (fenced_update.rowcount or 0) != 1:
                    await session.rollback()
                    return self._record_fenced_outcome(claimed, t0)

                run_row = ConnectorRun(
                    id=new_id("crn"),
                    org_id=claimed.org_id,
                    connector_id=claimed.connector_id,
                    well_id=claimed.well_id,
                    worker_id=self.worker_id,
                    fencing_token=claimed.fencing_token,
                    config_version=row_config_version,
                    run_kind="poll",
                    status="succeeded",
                    started_at=started,
                    finished_at=finished,
                    duration_ms=duration_ms,
                    frames_received=frames_received,
                    points_accepted=accepted,
                    points_duplicates=duplicates,
                    points_revised=revised,
                    points_rejected=rejected,
                    alerts_raised=alerts_raised,
                    alerts_cleared=alerts_cleared,
                    cursor_before=claimed.cursor_before,
                    cursor_after=new_cursor,
                    trace_id=trace_id,
                    details={
                        "channels": ingest_report.to_dict().get("channels", []),
                    },
                )
                session.add(run_row)
                await session.flush()
                await self._prune_runs(session, claimed.connector_id)

                if claimed.previous_status != "running" or frames_received > 0:
                    await emit(
                        session,
                        org_id=claimed.org_id,
                        type="connector.changed",
                        subject_kind="connector",
                        subject_id=claimed.connector_id,
                        well_id=claimed.well_id,
                        wellbore_id=claimed.wellbore_id,
                        payload={
                            "connector_id": claimed.connector_id,
                            "action": "poll_succeeded",
                            "status": "running",
                            "desired_state": "enabled",
                            "is_enabled": True,
                            "fencing_token": claimed.fencing_token,
                            "frames_received": frames_received,
                            "points_accepted": accepted,
                        },
                    )

                await session.commit()

                metrics.counter(
                    "drillai.connector.polls_total", "total connector poll cycles"
                ).inc(1, protocol_profile=claimed.protocol_profile, status="succeeded")
                metrics.counter(
                    "drillai.connector.frames_received_total", "total frames received by connectors"
                ).inc(frames_received, protocol_profile=claimed.protocol_profile)
                if accepted:
                    metrics.counter(
                        "drillai.connector.points_ingested_total", "points processed by connectors"
                    ).inc(accepted, protocol_profile=claimed.protocol_profile, outcome="accepted")
                if duplicates:
                    metrics.counter(
                        "drillai.connector.points_ingested_total", "points processed by connectors"
                    ).inc(duplicates, protocol_profile=claimed.protocol_profile, outcome="duplicates")
                metrics.histogram(
                    "drillai.connector.poll_duration_ms", "connector poll duration in ms"
                ).observe(duration_ms, protocol_profile=claimed.protocol_profile, status="succeeded")

                return PollCycleOutcome(
                    connector_id=claimed.connector_id,
                    protocol_profile=claimed.protocol_profile,
                    status="succeeded",
                    fencing_token=claimed.fencing_token,
                    frames_received=frames_received,
                    points_accepted=accepted,
                    points_duplicates=duplicates,
                    points_revised=revised,
                    points_rejected=rejected,
                    alerts_raised=alerts_raised,
                    alerts_cleared=alerts_cleared,
                    cursor_before=claimed.cursor_before,
                    cursor_after=new_cursor,
                    duration_ms=duration_ms,
                )

        except Exception as exc:
            return await self._record_failed_outcome(
                claimed=claimed,
                exc=exc,
                resolved_secrets=resolved_secrets,
                started=started,
                t0=t0,
                trace_id=trace_id,
            )

    def _record_fenced_outcome(
        self, claimed: ClaimedConnector, t0: float
    ) -> PollCycleOutcome:
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        metrics = get_metrics()
        metrics.counter("drillai.connector.polls_total", "total connector poll cycles").inc(
            1, protocol_profile=claimed.protocol_profile, status="fenced"
        )
        metrics.counter("drillai.connector.errors_total", "connector errors by category").inc(
            1, protocol_profile=claimed.protocol_profile, error_category="lease_fenced"
        )
        logger.warning(
            "stale connector worker fenced before checkpoint commit",
            extra={
                "connector_id": claimed.connector_id,
                "worker_id": self.worker_id,
                "fencing_token": claimed.fencing_token,
            },
        )
        return PollCycleOutcome(
            connector_id=claimed.connector_id,
            protocol_profile=claimed.protocol_profile,
            status="fenced",
            fencing_token=claimed.fencing_token,
            cursor_before=claimed.cursor_before,
            cursor_after=claimed.cursor_before,
            error_category="lease_fenced",
            error_message="worker lease was preempted by a newer fencing token; checkpoint update aborted",
            duration_ms=duration_ms,
        )

    async def _record_failed_outcome(
        self,
        *,
        claimed: ClaimedConnector,
        exc: Exception,
        resolved_secrets: dict[str, str],
        started: dt.datetime,
        t0: float,
        trace_id: str,
    ) -> PollCycleOutcome:
        finished = utc_now()
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        category = classify_connector_error(exc)
        safe_msg = (
            redact_sensitive_text(str(exc), resolved_secrets)
            or f"connector poll failed ({category})"
        )
        metrics = get_metrics()

        async with self.database.session() as err_session:
            row = (
                await err_session.execute(
                    select(Connector).where(Connector.id == claimed.connector_id)
                )
            ).scalar_one_or_none()
            if (
                row is None
                or row.worker_id != self.worker_id
                or int(row.fencing_token or 0) != claimed.fencing_token
                or not row.is_enabled
                or row.desired_state != "enabled"
            ):
                await err_session.rollback()
                return self._record_fenced_outcome(claimed, t0)

            cfg = dict(row.config or {})
            max_failures = int(cfg.get("max_consecutive_failures", 5))
            base_backoff = float(cfg.get("base_backoff_seconds", 1.0))
            max_backoff = float(cfg.get("max_backoff_seconds", 30.0))

            new_error_count = int(row.error_count or 0) + 1
            if new_error_count >= max_failures:
                new_status = "failed"
                backoff = 0.0
                next_poll_at = None
            else:
                new_status = "backing_off"
                raw_backoff = min(max_backoff, base_backoff * (2 ** (new_error_count - 1)))
                jitter = random.uniform(0.9, 1.1)
                backoff = round(min(max_backoff, max(base_backoff, raw_backoff * jitter)), 3)
                next_poll_at = finished + dt.timedelta(seconds=backoff)

            lease_until = finished + dt.timedelta(seconds=self.lease_seconds)
            # Notice: cursor is NEVER updated on failure!
            fenced_err_update = await err_session.execute(
                update(Connector)
                .where(
                    Connector.id == claimed.connector_id,
                    Connector.worker_id == self.worker_id,
                    Connector.fencing_token == claimed.fencing_token,
                )
                .values(
                    status=new_status,
                    error_count=new_error_count,
                    backoff_seconds=backoff,
                    next_poll_at=next_poll_at,
                    last_poll_at=finished,
                    last_error=safe_msg,
                    last_error_category=category,
                    last_error_at=finished,
                    last_trace_id=trace_id,
                    last_transition_at=finished,
                    lease_expires_at=lease_until,
                    last_heartbeat_at=finished,
                    updated_at=finished,
                )
            )
            if (fenced_err_update.rowcount or 0) != 1:
                await err_session.rollback()
                return self._record_fenced_outcome(claimed, t0)

            run_row = ConnectorRun(
                id=new_id("crn"),
                org_id=claimed.org_id,
                connector_id=claimed.connector_id,
                well_id=claimed.well_id,
                worker_id=self.worker_id,
                fencing_token=claimed.fencing_token,
                config_version=int(row.config_version or claimed.config_version),
                run_kind="poll",
                status="failed",
                started_at=started,
                finished_at=finished,
                duration_ms=duration_ms,
                cursor_before=claimed.cursor_before,
                cursor_after=claimed.cursor_before,
                error_category=category,
                error_message=safe_msg,
                trace_id=trace_id,
                details={
                    "consecutive_failures": new_error_count,
                    "backoff_seconds": backoff,
                    "next_status": new_status,
                },
            )
            err_session.add(run_row)
            await err_session.flush()
            await self._prune_runs(err_session, claimed.connector_id)

            await emit(
                err_session,
                org_id=claimed.org_id,
                type="connector.changed",
                subject_kind="connector",
                subject_id=claimed.connector_id,
                well_id=claimed.well_id,
                wellbore_id=claimed.wellbore_id,
                payload={
                    "connector_id": claimed.connector_id,
                    "action": "poll_failed",
                    "status": new_status,
                    "desired_state": "enabled",
                    "is_enabled": True,
                    "fencing_token": claimed.fencing_token,
                    "error_category": category,
                    "consecutive_failures": new_error_count,
                },
            )
            await err_session.commit()

        metrics.counter("drillai.connector.polls_total", "total connector poll cycles").inc(
            1, protocol_profile=claimed.protocol_profile, status="failed"
        )
        metrics.counter("drillai.connector.errors_total", "connector errors by category").inc(
            1, protocol_profile=claimed.protocol_profile, error_category=category
        )
        metrics.histogram(
            "drillai.connector.poll_duration_ms", "connector poll duration in ms"
        ).observe(duration_ms, protocol_profile=claimed.protocol_profile, status="failed")

        logger.warning(
            "connector poll cycle failed",
            extra={
                "connector_id": claimed.connector_id,
                "worker_id": self.worker_id,
                "fencing_token": claimed.fencing_token,
                "error_category": category,
                "error_message": safe_msg,
                "duration_ms": duration_ms,
            },
        )
        return PollCycleOutcome(
            connector_id=claimed.connector_id,
            protocol_profile=claimed.protocol_profile,
            status="failed",
            fencing_token=claimed.fencing_token,
            cursor_before=claimed.cursor_before,
            cursor_after=claimed.cursor_before,
            error_category=category,
            error_message=safe_msg,
            duration_ms=duration_ms,
        )

    async def run_once(self) -> list[PollCycleOutcome]:
        """Discover all due connectors, claim them with CAS + fencing tokens, and execute one poll cycle."""
        async with self.database.session() as session:
            candidate_ids = await self.discover_candidate_ids(session)

        if not candidate_ids:
            self.cycles_completed += 1
            self.last_cycle_at = utc_now()
            return []

        sem = asyncio.Semaphore(max(1, self.max_concurrency))

        async def _claim_and_run(cid: str) -> PollCycleOutcome | None:
            async with sem:
                claimed = await self.claim_connector(cid)
                if claimed is None:
                    return None
                return await self.execute_claimed_poll(claimed)

        results = await asyncio.gather(*(_claim_and_run(cid) for cid in candidate_ids))
        self.cycles_completed += 1
        self.last_cycle_at = utc_now()
        return [r for r in results if r is not None]

    async def release_owned_leases(self) -> int:
        """Gracefully release active leases held by this worker on shutdown."""
        now = utc_now()
        async with self.database.session() as session:
            res = await session.execute(
                update(Connector)
                .where(Connector.worker_id == self.worker_id)
                .values(
                    worker_id=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )
            await session.commit()
            return int(res.rowcount or 0)

    async def run_forever(self) -> None:
        """Continuously poll due connectors until ``stop()`` is called."""
        logger.info(
            "connector worker started",
            extra={
                "worker_id": self.worker_id,
                "lease_seconds": self.lease_seconds,
                "poll_interval_seconds": self.poll_interval_seconds,
            },
        )
        try:
            while not self._stop_event.is_set():
                try:
                    await self.run_once()
                except Exception as exc:
                    logger.error(
                        "unexpected error in connector worker loop",
                        extra={"worker_id": self.worker_id, "error": str(exc)},
                    )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.poll_interval_seconds
                    )
        finally:
            with contextlib.suppress(Exception):
                await self.release_owned_leases()
            logger.info("connector worker stopped", extra={"worker_id": self.worker_id})

    def start_background(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(self.run_forever(), name=f"connector-worker-{self.worker_id}")
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=5.0)
            self._task = None
        else:
            with contextlib.suppress(Exception):
                await self.release_owned_leases()


async def _async_main(once: bool = False) -> int:
    settings: Settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    database = Database(settings=settings)
    worker = ConnectorWorker(database)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker._stop_event.set)

    try:
        if once:
            await worker.run_once()
        else:
            await worker.run_forever()
    finally:
        await database.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DrillAI durable telemetry connector worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Execute a single discovery and poll cycle, then exit",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_async_main(once=args.once))


if __name__ == "__main__":
    raise SystemExit(main())
