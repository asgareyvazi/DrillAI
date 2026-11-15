"""CP148 adversarial authorization regressions through the real ASGI/API boundary."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from drillai.api.app import create_app
from drillai.core.clock import UTC
from drillai.core.config import get_settings, reset_settings_cache
from drillai.db.models import (
    ApiToken,
    AuditLog,
    Base,
    Membership,
    Organization,
    OutboundMessage,
    User,
)
from drillai.security.passwords import new_api_token


def _make_cp148_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/{name}.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_NOTIFICATION_WORKER_ENABLED", "false")
    reset_settings_cache()
    return create_app(get_settings())


async def _create_schema(app) -> None:
    async with app.state.database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def test_cp148_scoped_bearer_token_cannot_read_or_mutate_another_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real tenant-scoped API token cannot use a foreign row ID as an IDOR primitive."""
    app = _make_cp148_app(tmp_path, monkeypatch, "cp148_tenant_idor")
    await _create_schema(app)
    raw_token, token_prefix, token_digest = new_api_token(prefix="dk")
    now = dt.datetime.now(tz=UTC)

    async with app.state.database.session() as session:
        org_a = Organization(id="org_cp148_a", slug="cp148-alpha", name="CP148 Alpha")
        org_b = Organization(id="org_cp148_b", slug="cp148-beta", name="CP148 Beta")
        user = User(id="usr_cp148_shared", email="shared@cp148.invalid", display_name="Scoped operator")
        session.add_all([org_a, org_b, user])
        await session.flush()
        # The same user has an independent membership in both organizations. The credential itself
        # is bound only to A; B membership must not turn an A token into a cross-tenant principal.
        session.add_all(
            [
                Membership(org_id=org_a.id, user_id=user.id, role_key="admin", scope_kind="org"),
                Membership(org_id=org_b.id, user_id=user.id, role_key="admin", scope_kind="org"),
                ApiToken(
                    org_id=org_a.id,
                    user_id=user.id,
                    name="CP148 tenant-A test token",
                    token_prefix=token_prefix,
                    token_hash=token_digest,
                    scopes=["integration.*"],
                    expires_at=now + dt.timedelta(days=1),
                ),
            ]
        )
        row_a = OutboundMessage(
            org_id=org_a.id,
            channel="webhook",
            recipient="tenant-a-recipient.invalid",
            body="synthetic tenant A body must not leak",
            status="dead_letter",
            attempts=3,
            max_attempts=3,
        )
        row_b = OutboundMessage(
            org_id=org_b.id,
            channel="webhook",
            recipient="tenant-b-recipient.invalid",
            body="synthetic tenant B secret body must stay hidden",
            status="dead_letter",
            attempts=3,
            max_attempts=3,
        )
        session.add_all([row_a, row_b])
        await session.flush()
        row_a_id, row_b_id, org_a_id, org_b_id = row_a.id, row_b.id, org_a.id, org_b.id

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            headers = {
                "Authorization": f"Bearer {raw_token}",
                # An attacker-controlled org hint must not override the organization on the token.
                "X-Org-ID": org_b_id,
            }
            listed = await client.get("/api/v1/platform/notifications", headers=headers)
            assert listed.status_code == 200, listed.text
            listed_ids = {item["id"] for item in listed.json()["items"]}
            assert listed_ids == {row_a_id}
            assert row_b_id not in listed_ids
            assert "synthetic tenant A body" not in listed.text
            assert "synthetic tenant B secret body" not in listed.text
            assert "tenant-b-recipient.invalid" not in listed.text

            foreign_retry = await client.post(
                f"/api/v1/platform/notifications/{row_b_id}/retry",
                headers=headers,
                json={"reason": "reviewed foreign identifier probe", "acknowledge_duplicate_risk": True},
            )
            assert foreign_retry.status_code == 404, foreign_retry.text
            assert "tenant B secret body" not in foreign_retry.text

            own_retry = await client.post(
                f"/api/v1/platform/notifications/{row_a_id}/retry",
                headers=headers,
                json={"reason": "reviewed same-tenant recovery", "acknowledge_duplicate_risk": True},
            )
            assert own_retry.status_code == 200, own_retry.text
            assert own_retry.json()["requeued"] is True
            assert own_retry.json()["item"]["status"] == "queued"
            assert own_retry.json()["item"]["recipient_delivery_status"] == "unknown"
            assert own_retry.json()["item"]["delivery_confirmed"] is False

        async with app.state.database.session() as session:
            persisted_a = await session.get(OutboundMessage, row_a_id)
            persisted_b = await session.get(OutboundMessage, row_b_id)
            assert persisted_a is not None and persisted_a.status == "queued" and persisted_a.attempts == 0
            assert persisted_b is not None and persisted_b.status == "dead_letter" and persisted_b.attempts == 3
            audits = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "integration.notification.retry")
                )
            ).scalars().all()
            assert len(audits) == 1
            assert audits[0].org_id == org_a_id
            assert audits[0].resource_id == row_a_id
            assert audits[0].details["delivery_confirmed_before_retry"] is False
            assert row_b_id not in str(audits[0].details)
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


async def test_cp148_bearer_scope_and_role_cannot_be_escalated_with_development_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth-enabled requests ignore X-Dev-Roles; a read scope cannot mutate an outbox row."""
    app = _make_cp148_app(tmp_path, monkeypatch, "cp148_scope_escalation")
    await _create_schema(app)
    raw_token, token_prefix, token_digest = new_api_token(prefix="dk")
    now = dt.datetime.now(tz=UTC)

    async with app.state.database.session() as session:
        org = Organization(id="org_cp148_scope", slug="cp148-scope", name="CP148 Scope")
        user = User(id="usr_cp148_reader", email="reader@cp148.invalid", display_name="Scoped reader")
        session.add_all([org, user])
        await session.flush()
        session.add_all(
            [
                Membership(org_id=org.id, user_id=user.id, role_key="org_admin", scope_kind="org"),
                ApiToken(
                    org_id=org.id,
                    user_id=user.id,
                    name="CP148 read-only test token",
                    token_prefix=token_prefix,
                    token_hash=token_digest,
                    scopes=["integration.read"],
                    expires_at=now + dt.timedelta(days=1),
                ),
            ]
        )
        row = OutboundMessage(
            org_id=org.id,
            channel="webhook",
            recipient="synthetic-recipient.invalid",
            status="dead_letter",
            attempts=2,
            max_attempts=2,
        )
        session.add(row)
        await session.flush()
        row_id = row.id

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            read_headers = {"Authorization": f"Bearer {raw_token}", "X-Dev-Roles": "admin"}
            read = await client.get("/api/v1/platform/notifications", headers=read_headers)
            assert read.status_code == 200, read.text
            assert [item["id"] for item in read.json()["items"]] == [row_id]

            elevated_retry = await client.post(
                f"/api/v1/platform/notifications/{row_id}/retry",
                headers=read_headers,
                json={"reason": "attempt to exceed token scope", "acknowledge_duplicate_risk": True},
            )
            assert elevated_retry.status_code == 403, elevated_retry.text

            header_only = await client.get(
                "/api/v1/platform/notifications", headers={"X-Dev-Roles": "admin"}
            )
            assert header_only.status_code == 401, header_only.text

        async with app.state.database.session() as session:
            persisted = await session.get(OutboundMessage, row_id)
            assert persisted is not None and persisted.status == "dead_letter" and persisted.attempts == 2
            audits = (
                await session.execute(
                    select(AuditLog).where(AuditLog.action == "integration.notification.retry")
                )
            ).scalars().all()
            assert audits == []
    finally:
        await app.state.database.dispose()
        reset_settings_cache()
