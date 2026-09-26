"""Event abstraction.

Engineering operations are naturally event-shaped: a DDR is received, a survey is
recorded, a casing string is run, an NPT event starts. The platform therefore defines a
single, versioned event envelope used by:

* the realtime hub (WebSocket fan-out to the UI),
* the workflow trigger layer,
* the durable outbox (transactional publication — the authoritative record),
* integration adapters (inbound Telegram/email/WITSML become platform events).

Delivery guarantees are explicit: the **database outbox** is the source of truth
(at-least-once with idempotency via ``event_id``); the in-process bus is best-effort
fan-out for live subscribers only.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from drillai.core.clock import UTC
from drillai.core.ids import new_id


class Actor(BaseModel):
    """Who or what caused the event."""

    model_config = ConfigDict(frozen=True)
    kind: str = Field(default="system", description="user | agent | system | integration | engine")
    id: str | None = None
    display_name: str | None = None


class TraceContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    trace_id: str | None = None
    span_id: str | None = None
    workflow_run_id: str | None = None
    agent_run_id: str | None = None


class EventEnvelope(BaseModel):
    """The canonical platform event."""

    model_config = ConfigDict(frozen=True)
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    type: str = Field(description="dotted event type, e.g. document.ingested")
    version: int = 1
    occurred_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(tz=UTC))
    recorded_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(tz=UTC))
    org_id: str | None = None
    project_id: str | None = None
    subject_type: str | None = Field(default=None, description="e.g. well, operation, document")
    subject_id: str | None = None
    partition_key: str | None = Field(
        default=None, description="ordering key; defaults to subject_id"
    )
    actor: Actor = Field(default_factory=Actor)
    trace: TraceContext = Field(default_factory=TraceContext)
    payload: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None

    def ordering_key(self) -> str:
        return self.partition_key or self.subject_id or self.event_id


EventHandler = Callable[[EventEnvelope], Awaitable[None]]


@runtime_checkable
class EventBus(Protocol):
    async def publish(self, event: EventEnvelope) -> None: ...

    def subscribe(self, pattern: str, handler: EventHandler) -> str: ...

    def unsubscribe(self, subscription_id: str) -> None: ...


class InProcessEventBus:
    """Fan-out bus for live subscribers (WebSocket hub, in-app triggers).

    Wildcards: ``*`` matches everything, ``prefix.*`` matches a dotted prefix.
    Handler exceptions are isolated so one bad subscriber cannot break ingestion.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, tuple[str, EventHandler]] = {}
        self._counter = 0

    def subscribe(self, pattern: str, handler: EventHandler) -> str:
        self._counter += 1
        subscription_id = f"sub_{self._counter}"
        self._subscribers[subscription_id] = (pattern, handler)
        return subscription_id

    def unsubscribe(self, subscription_id: str) -> None:
        self._subscribers.pop(subscription_id, None)

    async def publish(self, event: EventEnvelope) -> None:
        for subscription_id, (pattern, handler) in list(self._subscribers.items()):
            if not matches(pattern, event.type):
                continue
            try:
                await handler(event)
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "event handler failed", extra={"extra_fields": {"subscription_id": subscription_id}}
                )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


def matches(pattern: str, event_type: str) -> bool:
    if pattern in ("*", "#"):
        return True
    if pattern.endswith(".*"):
        return event_type.startswith(pattern[:-1])
    if pattern.endswith(".>"):
        return event_type.startswith(pattern[:-2])
    return pattern == event_type


# --------------------------------------------------------------------------- catalog
# Canonical platform event types. Node/trigger definitions and integration adapters
# subscribe to these; adding a new type is an additive change.
EVENT_TYPES: dict[str, str] = {
    "project.created": "A project was created",
    "well.created": "A well was created",
    "well.updated": "Well master data changed",
    "wellbore.updated": "Wellbore state changed",
    "survey.recorded": "A survey station was recorded",
    "operation.planned": "An operation entered the plan",
    "operation.started": "An operation started",
    "operation.completed": "An operation completed",
    "event.recorded": "An engineering event was recorded",
    "event.npt_classified": "An event was classified as NPT",
    "document.uploaded": "A source document was stored",
    "document.ingested": "A document was processed into regions/records",
    "record.extracted": "An engineering record was extracted from evidence",
    "record.validated": "An extracted record was validated by a human or rule",
    "evidence.linked": "Evidence was attached to a subject",
    "engine.run_completed": "An engineering engine produced a result",
    "optimization.run_completed": "An optimization run produced candidates",
    "recommendation.raised": "A recommendation was produced (with evidence)",
    "approval.requested": "A human approval was requested",
    "approval.decided": "A human approval was granted or denied",
    "workflow.run_started": "A workflow run started",
    "workflow.run_paused": "A workflow run paused (approval/breakpoint)",
    "workflow.run_completed": "A workflow run completed",
    "workflow.run_failed": "A workflow run failed",
    "alert.raised": "An alert was raised",
    "alert.cleared": "An alert was cleared",
    "readiness.assessed": "Operational readiness was assessed",
    "twin.aspect_updated": "A digital well twin aspect changed",
    "connector.status_changed": "An integration connector changed status",
    "message.received": "An inbound integration message was received",
    "message.sent": "An outbound integration message was sent",
    "schedule.triggered": "A scheduler tick produced a run",
}
