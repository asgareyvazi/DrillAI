"""Notification outbox due dispatch and ambiguous-send recovery are truthful and bounded."""

from __future__ import annotations

import contextlib
import datetime as dt
import smtplib

import pytest
from sqlalchemy import select

from drillai.core.clock import utc_now
from drillai.db.models import Organization, OutboundMessage
from drillai.security.actions import ActionLevel, Principal
from drillai.workflow.nodes import NodeContext, NotifyConfig, _notify
from drillai.workflow.notification_worker import NotificationWorker
from drillai.workflow.notifications import (
    STALE_SENDING_AFTER,
    DeliveryAttemptOutcome,
    EmailSmtpNotificationAdapter,
    NotificationAdapter,
    NotificationDispatcher,
    claim_pending_notifications,
)


@pytest.mark.asyncio
async def test_worker_dispatches_due_rows_and_quarantines_stale_sends_without_auto_retry(session):
    org = Organization(slug="notification-worker", name="Notification worker")
    session.add(org)
    await session.flush()
    now = utc_now()

    queued = OutboundMessage(org_id=org.id, channel="in_app", recipient="user:1", status="queued")
    due = OutboundMessage(
        org_id=org.id,
        channel="in_app",
        recipient="user:2",
        status="failed_retryable",
        attempts=1,
        next_attempt_at=now - dt.timedelta(seconds=1),
    )
    future = OutboundMessage(
        org_id=org.id,
        channel="in_app",
        recipient="user:3",
        status="failed_retryable",
        attempts=1,
        next_attempt_at=now + dt.timedelta(minutes=5),
    )
    stale = OutboundMessage(
        org_id=org.id,
        channel="webhook",
        recipient="https://example.invalid/hook",
        status="sending",
        attempts=1,
        updated_at=now - STALE_SENDING_AFTER - dt.timedelta(seconds=1),
    )
    session.add_all([queued, due, future, stale])
    await session.flush()

    class SessionScope:
        @contextlib.asynccontextmanager
        async def session(self):
            yield session

    worker = NotificationWorker(SessionScope(), poll_seconds=1.0, batch_size=10)
    results = await worker.run_once()
    by_id = {item["message_id"]: item for item in results}

    assert by_id[queued.id]["status"] == "accepted"
    assert by_id[queued.id]["acceptance_evidence"] == "local_in_app_outbox"
    assert by_id[queued.id]["delivery_confirmed"] is False
    assert by_id[due.id]["status"] == "accepted"
    assert by_id[stale.id]["status"] == "reconciliation_required"
    assert by_id[stale.id]["manual_reconciliation_required"] is True
    assert future.status == "failed_retryable"
    assert future.id not in by_id
    assert worker.cycles_completed == 1
    assert worker.last_batch_size == 3
    assert worker.last_cycle_at is not None


@pytest.mark.postgres
async def test_competing_notification_worker_claims_skip_locked_rows_on_postgres(postgres_url):
    """Two independent worker sessions cannot claim the same row while the first transaction is open."""
    from uuid import uuid4

    from drillai.db.session import Database

    database = Database(postgres_url)
    await database.create_all()
    org_id: str
    message_id: str
    async with database.session() as session:
        org = Organization(slug=f"notification-pg-claim-{uuid4().hex}", name="Notification PG claim")
        session.add(org)
        await session.flush()
        message = OutboundMessage(
            org_id=org.id,
            channel="in_app",
            recipient="user:concurrent",
            status="queued",
            idempotency_key="postgres-concurrent-claim",
        )
        session.add(message)
        await session.flush()
        org_id, message_id = org.id, message.id

    now = utc_now()
    try:
        # Keep the first claim transaction open so a distinct second session contends for the same row.
        async with database.session() as first_worker_session:
            first = await claim_pending_notifications(first_worker_session, limit=1, now=now)
            assert first["claimed_ids"] == [message_id]
            async with database.session() as second_worker_session:
                second = await claim_pending_notifications(second_worker_session, limit=1, now=now)
                assert message_id not in second["claimed_ids"]
                assert second["claimed_ids"] == []

        async with database.session() as session:
            persisted = (
                await session.execute(select(OutboundMessage).where(OutboundMessage.id == message_id))
            ).scalar_one()
            assert persisted.org_id == org_id
            assert persisted.status == "sending"
            assert persisted.attempts == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_worker_commits_claim_before_transport_and_quarantines_interrupted_send(tmp_path, monkeypatch):
    from sqlalchemy import select

    import drillai.workflow.notifications as notification_module
    from drillai.db.session import Database
    from drillai.workflow.notifications import NotificationAdapter

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'notification-claim.db'}")
    await database.create_all()
    org_id: str
    message_id: str
    async with database.session() as session:
        org = Organization(slug="durable-notify-claim", name="Durable notification claim")
        session.add(org)
        await session.flush()
        row = OutboundMessage(
            org_id=org.id,
            channel="claim_probe",
            recipient="probe:recipient",
            status="queued",
        )
        session.add(row)
        await session.flush()
        org_id, message_id = org.id, row.id

    observed_attempts: list[int] = []

    class FailingProbeAdapter(NotificationAdapter):
        channel = "claim_probe"

        async def send(self, session, message):
            async with database.session() as observer:
                persisted = (
                    await observer.execute(
                        select(OutboundMessage).where(OutboundMessage.id == message.id)
                    )
                ).scalar_one()
                assert persisted.org_id == org_id
                assert persisted.status == "sending"
                observed_attempts.append(persisted.attempts)
            raise TimeoutError("simulated process interruption after the durable claim")

    previous_adapter = notification_module._DEFAULT_DISPATCHER._adapters.get("claim_probe")
    notification_module._DEFAULT_DISPATCHER.register_adapter(FailingProbeAdapter())
    try:
        worker = NotificationWorker(database, poll_seconds=1.0, batch_size=5)
        first = await worker.run_once()
        assert first == [
            {"message_id": message_id, "status": "sending", "dispatch_error_type": "TimeoutError"}
        ]
        assert observed_attempts == [1]
        assert worker.last_error_type == "TimeoutError"

        old = utc_now() - STALE_SENDING_AFTER - dt.timedelta(seconds=1)
        async with database.session() as session:
            persisted = (
                await session.execute(
                    select(OutboundMessage).where(OutboundMessage.id == message_id)
                )
            ).scalar_one()
            persisted.updated_at = old

        recovered = await worker.run_once()
        assert len(recovered) == 1
        assert recovered[0]["message_id"] == message_id
        assert recovered[0]["status"] == "reconciliation_required"
        assert recovered[0]["manual_reconciliation_required"] is True
        assert observed_attempts == [1], "an ambiguous transport outcome must not be auto-retried"
    finally:
        if previous_adapter is None:
            notification_module._DEFAULT_DISPATCHER._adapters.pop("claim_probe", None)
        else:
            notification_module._DEFAULT_DISPATCHER.register_adapter(previous_adapter)
        await database.dispose()


@pytest.mark.asyncio
async def test_legacy_inline_dispatch_compatibility_entrypoints_are_disabled(session):
    from drillai.workflow.notifications import dispatch_outbound_message, dispatch_pending_notifications

    org = Organization(slug="notification-inline-compat", name="Notification inline compatibility")
    session.add(org)
    await session.flush()
    message = OutboundMessage(
        org_id=org.id,
        channel="in_app",
        recipient="user:legacy",
        status="queued",
    )
    session.add(message)
    await session.flush()

    with pytest.raises(RuntimeError, match="inline pending-notification dispatch is disabled"):
        await dispatch_pending_notifications(session, org_id=org.id)
    with pytest.raises(RuntimeError, match="inline notification dispatch is disabled"):
        await dispatch_outbound_message(session, message)
    with pytest.raises(RuntimeError, match="inline notification dispatch is disabled"):
        await NotificationDispatcher().dispatch_message(session, message)

    assert message.status == "queued"
    assert message.attempts == 0


@pytest.mark.asyncio
async def test_workflow_notification_immediate_request_stays_queued_until_after_commit(session):
    org = Organization(slug="notification-node-outbox", name="Notification node outbox")
    session.add(org)
    await session.flush()
    context = NodeContext(
        session=session,
        org_id=org.id,
        principal=Principal(id="usr_notification_node", max_action_level=ActionLevel.OBSERVE),
        run_id="run_notification_node",
        node_id="notify_1",
        node_name="Queue notification",
        inputs={},
        variables={},
        node_outputs={},
    )

    result = await _notify(
        context,
        NotifyConfig(
            channel="in_app",
            recipient="user:1",
            body="notification body",
            dispatch_immediately=True,
        ),
    )

    assert result.outputs["status"] == "queued"
    assert "deferred until after the workflow transaction" in result.notes[0]
    row = (
        await session.execute(
            select(OutboundMessage).where(OutboundMessage.workflow_run_id == context.run_id)
        )
    ).scalar_one()
    assert row.status == "queued"


@pytest.mark.asyncio
async def test_notification_ledger_sanitizes_adapter_errors_and_never_claims_provider_verification(tmp_path):
    from drillai.db.session import Database

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'notification-error-ledger.db'}")
    await database.create_all()
    try:
        async with database.session() as session:
            org = Organization(slug="notification-error-redaction", name="Notification error redaction")
            session.add(org)
            await session.flush()
            message = OutboundMessage(
                org_id=org.id,
                channel="leaky_probe",
                recipient="private-recipient",
                status="queued",
                max_attempts=1,
            )
            session.add(message)
            await session.flush()
            message_id = message.id

        class LeakyAdapter(NotificationAdapter):
            channel = "leaky_probe"

            async def send(self, session, message):
                return DeliveryAttemptOutcome(
                    status="failed_retryable",
                    retryable=True,
                    error_code="provider_503",
                    error="PRIVATE_TOKEN=do-not-persist",
                    external_provider_verified=True,
                )

        dispatcher = NotificationDispatcher(adapters={"leaky_probe": LeakyAdapter()})
        worker = NotificationWorker(database, poll_seconds=1.0, batch_size=1, dispatcher=dispatcher)
        result = (await worker.run_once())[0]
        async with database.session() as session:
            persisted = (
                await session.execute(select(OutboundMessage).where(OutboundMessage.id == message_id))
            ).scalar_one()
            ledger = persisted.attributes["delivery_ledger"]

        assert result["status"] == "dead_letter"
        assert result["delivery_confirmed"] is False
        assert result["external_provider_verified"] is False
        assert persisted.error == "notification attempt failed (provider_503)"
        assert "do-not-persist" not in str(persisted.error)
        assert "do-not-persist" not in str(ledger)
        assert ledger[-1]["external_provider_verified"] is False
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_smtp_permanent_recipient_refusal_is_terminal_and_does_not_store_server_text(
    session, monkeypatch
):
    import drillai.workflow.notifications as notification_module

    org = Organization(slug="notification-smtp-refusal", name="Notification SMTP refusal")
    session.add(org)
    await session.flush()
    message = OutboundMessage(
        org_id=org.id,
        channel="email",
        recipient="recipient@example.invalid",
        payload={"smtp_url": "smtps://mail.example.invalid:465"},
        status="queued",
        max_attempts=3,
    )
    session.add(message)
    await session.flush()

    class RefusingSmtp:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send_message(self, _message):
            raise smtplib.SMTPRecipientsRefused(
                {"recipient@example.invalid": (550, b"PRIVATE_SERVER_DIAGNOSTIC")}
            )

    monkeypatch.setattr(notification_module.smtplib, "SMTP_SSL", RefusingSmtp)
    adapter = EmailSmtpNotificationAdapter()
    outcome = await adapter.send(session, message)

    assert outcome.status == "failed_terminal"
    assert outcome.retryable is False
    assert outcome.status_code == 550
    assert outcome.error_code == "smtp_recipients_refused"
    assert "PRIVATE_SERVER_DIAGNOSTIC" not in str(outcome.error)
