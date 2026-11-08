"""CP130 retryable notification and workflow failure-state contracts."""

from __future__ import annotations

import smtplib
from types import SimpleNamespace

import pytest

from drillai.db.models import Organization, OutboundMessage
from drillai.workflow.notifications import EmailSmtpNotificationAdapter


@pytest.mark.asyncio
async def test_temporary_smtp_response_is_retryable_and_does_not_copy_server_text(session, monkeypatch):
    import drillai.core.config as config_module
    import drillai.workflow.notifications as notification_module

    monkeypatch.setenv("DRILLAI_SMTP_URL", "smtps://mail.example.invalid:465")
    monkeypatch.setattr(config_module, "get_settings", lambda: SimpleNamespace(is_production=False))

    class TemporarySmtp:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def send_message(self, _message):
            raise smtplib.SMTPResponseException(421, b"PRIVATE_SERVER_RESPONSE recipient@example.invalid")

    monkeypatch.setattr(notification_module.smtplib, "SMTP_SSL", TemporarySmtp)
    org = Organization(slug="cp130-smtp-temporary", name="CP130 synthetic SMTP retry")
    session.add(org)
    await session.flush()
    message = OutboundMessage(
        org_id=org.id,
        channel="email",
        recipient="recipient@example.invalid",
        status="queued",
        payload={},
        max_attempts=3,
    )

    outcome = await EmailSmtpNotificationAdapter().send(session, message)

    assert outcome.status == "failed_retryable"
    assert outcome.retryable is True
    assert outcome.status_code == 421
    assert outcome.error_code == "smtp_421"
    assert outcome.error == "SMTP server returned status 421"
    assert "PRIVATE_SERVER_RESPONSE" not in str(outcome)
    assert "recipient@example.invalid" not in str(outcome.error)
