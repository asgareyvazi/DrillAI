"""Exercise secret isolation through the durable notification worker's real session lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from drillai.api.app import create_app
from drillai.core.config import Settings, reset_settings_cache
from drillai.db.models import Organization, OutboundMessage, SecretRef
from drillai.security.secrets import encrypt_secret_value
from drillai.workflow.notification_worker import NotificationWorker
from drillai.workflow.notifications import (
    DeliveryAttemptOutcome,
    NotificationAdapter,
    NotificationDispatcher,
    resolve_notification_secret_async,
)


@pytest.mark.asyncio
async def test_two_notification_workers_use_their_own_secrets(monkeypatch):
    keys = ("worker-a-master-key-long-enough-123456", "worker-b-master-key-long-enough-123456")
    monkeypatch.setenv("DRILLAI_SECRET_MASTER_KEY", keys[0])
    reset_settings_cache()
    apps = [create_app(Settings(environment="test", database_url="sqlite+aiosqlite:///:memory:", secret_master_key=key)) for key in keys]
    observed = {}

    class CaptureAdapter(NotificationAdapter):
        channel = "capture"

        async def send(self, session, message):
            observed[message.recipient] = await resolve_notification_secret_async(session, message.org_id, "secret_ref:mail")
            return DeliveryAttemptOutcome(status="accepted", retryable=False, acceptance_evidence="local_in_app_outbox")

    try:
        for index, app in enumerate(apps):
            await app.state.database.create_all()
            envelope, _ = encrypt_secret_value(f"credential-{index}", org_id="org_shared", secret_key="mail", settings=app.state.settings)
            async with app.state.database.session() as session:
                session.add(Organization(id="org_shared", slug="shared", name="Shared"))
                await session.flush()
                session.add(SecretRef(org_id="org_shared", key="mail", backend="db_encrypted", locator=envelope))
                session.add(OutboundMessage(org_id="org_shared", channel="capture", recipient=str(index), status="queued"))
        dispatcher = NotificationDispatcher(adapters={"capture": CaptureAdapter()})
        workers = [NotificationWorker(app.state.database, dispatcher=dispatcher) for app in apps]
        results = await asyncio.gather(*(worker.run_once() for worker in workers))
        assert [batch[0]["status"] for batch in results] == ["accepted", "accepted"]
        assert observed == {"0": "credential-0", "1": "credential-1"}
    finally:
        for app in apps:
            await app.state.database.dispose()
        reset_settings_cache()
