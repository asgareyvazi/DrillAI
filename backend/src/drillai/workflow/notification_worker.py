"""Opt-in process worker for the durable notification outbox.

The worker dispatches only queued/due-retry rows, commits each bounded batch through the
``Database.session`` transaction scope, and exposes process-local diagnostics. Multiple PostgreSQL
workers use ``FOR UPDATE SKIP LOCKED`` in the dispatcher. Delivery remains at-least-once: a process
failure after remote acceptance but before the database commit can cause a replay; webhook receivers
should honor the sent idempotency key, and all ambiguous old ``sending`` rows require reconciliation.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from typing import Any

from drillai.core.clock import utc_now
from drillai.core.logging import get_logger
from drillai.workflow.notifications import (
    NotificationDispatcher,
    _dispatch_claimed_notification,
    claim_pending_notifications,
)

logger = get_logger(__name__)


class NotificationWorker:
    """Poll and dispatch notification outbox rows when explicitly enabled by deployment config."""

    def __init__(
        self,
        database: Any,
        *,
        poll_seconds: float = 5.0,
        batch_size: int = 50,
        dispatcher: NotificationDispatcher | None = None,
    ) -> None:
        if not 0.2 <= poll_seconds <= 60.0:
            raise ValueError("notification poll interval must be between 0.2 and 60 seconds")
        if not 1 <= batch_size <= 100:
            raise ValueError("notification batch size must be between 1 and 100")
        self.database = database
        self.poll_seconds = poll_seconds
        self.batch_size = batch_size
        self.dispatcher = dispatcher
        self.cycles_completed = 0
        self.last_cycle_at: dt.datetime | None = None
        self.last_batch_size = 0
        self.last_error_type: str | None = None
        self._stop_event: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None

    def start_background(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="drillai-notification-outbox")

    async def run_once(self) -> list[dict[str, Any]]:
        """Commit claims before transport I/O, then persist each bounded outcome independently."""
        async with self.database.session() as session:
            claims = await claim_pending_notifications(session, limit=self.batch_size, now=utc_now())
        results = [*claims["reconciled"], *claims["suppressed"]]
        cycle_error: str | None = None
        for message_id in claims["claimed_ids"]:
            try:
                async with self.database.session() as session:
                    outcome = await _dispatch_claimed_notification(
                        session,
                        message_id,
                        now=utc_now(),
                        dispatcher=self.dispatcher,
                    )
                results.append(outcome)
            except Exception as exc:  # the committed claim remains for conservative stale reconciliation
                cycle_error = type(exc).__name__
                logger.error(
                    "notification outbox dispatch left a durable claim for reconciliation",
                    extra={"extra_fields": {"message_id": message_id, "error_type": cycle_error}},
                )
                results.append(
                    {"message_id": message_id, "status": "sending", "dispatch_error_type": cycle_error}
                )
        self.cycles_completed += 1
        self.last_cycle_at = utc_now()
        self.last_batch_size = len(results)
        self.last_error_type = cycle_error
        return results

    async def _run(self) -> None:
        assert self._stop_event is not None
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # keep the worker alive; details may contain endpoint data
                self.last_error_type = type(exc).__name__
                logger.warning(
                    "notification worker cycle failed",
                    extra={"extra_fields": {"error_type": self.last_error_type}},
                )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        try:
            await asyncio.wait_for(task, timeout=30.0)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            self._task = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()
