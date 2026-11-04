"""Notification outbox read/retry API is tenant-scoped, redacted, audited, and conservative."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from tests.api.conftest import headers

from drillai.api.deps import ensure_org
from drillai.core.clock import UTC
from drillai.db.models import AuditLog, Organization, OutboundMessage


@pytest.mark.asyncio
async def test_notification_outbox_redacts_content_and_enforces_tenant_scope(client, db, api_settings):
    async with db.session() as session:
        org = await ensure_org(session, api_settings.dev_org_slug)
        own = OutboundMessage(
            org_id=org.id,
            channel="webhook",
            recipient="https://hooks.example.invalid/private/path?token=never-return-this",
            subject="Sensitive subject",
            body="Sensitive notification body",
            status="accepted",
            accepted_at=dt.datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
            acceptance_evidence="http_2xx",
            provider_message_id="remote-id-17",
            attributes={
                "external_provider_verified": True,
                "delivery_ledger": [
                    {"error_code": "secret_ref:DRILLAI_SECRET_SMTP_PASSWORD"},
                ],
            },
        )
        foreign_org = Organization(slug="notify-other-tenant", name="Other notification tenant")
        session.add_all([own, foreign_org])
        await session.flush()
        foreign = OutboundMessage(
            org_id=foreign_org.id,
            channel="email",
            recipient="person@other.invalid",
            body="Foreign tenant body",
            status="dead_letter",
            attempts=3,
            max_attempts=3,
        )
        session.add(foreign)
        await session.flush()
        own_id, foreign_id = own.id, foreign.id

    response = await client.get("/api/v1/platform/notifications", headers=headers("admin"))
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["id"] for item in items] == [own_id]
    item = items[0]
    assert item["status"] == "accepted"
    assert item["acceptance_evidence"] == "http_2xx"
    assert item["delivery_confirmed"] is False
    assert item["external_provider_verified"] is False
    assert item["last_error_code"] == "adapter_error"
    assert item["provider_message_id"] == "remote-id-17"
    assert item["recipient_present"] is True
    serialized = response.text
    assert "Sensitive notification body" not in serialized
    assert "Sensitive subject" not in serialized
    assert "private/path" not in serialized
    assert "never-return-this" not in serialized
    assert "person@other.invalid" not in serialized

    forbidden = await client.get("/api/v1/platform/notifications", headers=headers("viewer"))
    assert forbidden.status_code == 403
    foreign_retry = await client.post(
        f"/api/v1/platform/notifications/{foreign_id}/retry",
        headers=headers("admin"),
        json={"reason": "reviewed retry", "acknowledge_duplicate_risk": True},
    )
    assert foreign_retry.status_code == 404


@pytest.mark.asyncio
async def test_notification_manual_requeue_requires_ack_and_audits_without_payload(client, db, api_settings):
    async with db.session() as session:
        org = await ensure_org(session, api_settings.dev_org_slug)
        row = OutboundMessage(
            org_id=org.id,
            channel="webhook",
            recipient="https://hooks.example.invalid/secret-path",
            body="Never copy this into the audit",
            status="reconciliation_required",
            attempts=2,
            max_attempts=2,
            error="remote outcome unknown",
            attributes={"delivery_ledger": [{"error_code": "stale_sending_ambiguous"}]},
        )
        session.add(row)
        await session.flush()
        message_id = row.id

    missing_ack = await client.post(
        f"/api/v1/platform/notifications/{message_id}/retry",
        headers=headers("admin"),
        json={"reason": "operator reviewed prior attempt", "acknowledge_duplicate_risk": False},
    )
    assert missing_ack.status_code == 422
    still_ambiguous = await client.get(
        "/api/v1/platform/notifications?status=reconciliation_required",
        headers=headers("admin"),
    )
    assert still_ambiguous.status_code == 200
    assert still_ambiguous.json()["items"][0]["id"] == message_id

    retried = await client.post(
        f"/api/v1/platform/notifications/{message_id}/retry",
        headers=headers("admin"),
        json={"reason": "operator reviewed prior attempt", "acknowledge_duplicate_risk": True},
    )
    assert retried.status_code == 200, retried.text
    item = retried.json()["item"]
    assert item["status"] == "queued"
    assert item["attempts"] == 0
    assert item["delivery_confirmed"] is False

    duplicate_retry = await client.post(
        f"/api/v1/platform/notifications/{message_id}/retry",
        headers=headers("admin"),
        json={"reason": "operator reviewed prior attempt", "acknowledge_duplicate_risk": True},
    )
    assert duplicate_retry.status_code == 409

    async with db.session() as session:
        org = await ensure_org(session, api_settings.dev_org_slug)
        audits = list(
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.org_id == org.id,
                        AuditLog.resource_id == message_id,
                        AuditLog.action == "integration.notification.retry",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(audits) == 1
    assert audits[0].action_level == "L4"
    assert audits[0].details["duplicate_risk_acknowledged"] is True
    assert "Never copy this into the audit" not in str(audits[0].details)
