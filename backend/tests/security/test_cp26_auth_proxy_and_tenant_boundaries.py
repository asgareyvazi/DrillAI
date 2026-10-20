"""CP26 authentication trust-boundary, token-scope, and tenant-account isolation regressions."""

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
    AuthRateLimitBucket,
    Base,
    Membership,
    Organization,
    PasswordResetToken,
    User,
    UserSession,
)
from drillai.security.passwords import hash_password, hash_token, new_api_token

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str):
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/{name}.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    reset_settings_cache()
    return create_app(get_settings())


async def _create_schema(app) -> None:
    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def test_cp26_01_untrusted_forwarded_for_cannot_choose_rate_limit_or_audit_ip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct ASGI peers, not caller-supplied X-Forwarded-For, own IP buckets and audit IPs."""
    app = _make_app(tmp_path, monkeypatch, "cp26_forwarded_for")
    await _create_schema(app)
    raw_token, token_prefix, token_digest = new_api_token(prefix="dk")
    now = dt.datetime.now(tz=UTC)

    async with app.state.database.session() as session:
        org = Organization(id="org_cp26", slug="cp26", name="CP26 Test")
        user = User(
            id="usr_cp26",
            email="admin@cp26.example",
            display_name="CP26 Admin",
            password_hash=hash_password("CP26-Adequate-Password!2026"),
        )
        session.add_all([org, user])
        await session.flush()
        session.add(
            Membership(
                id="mbr_cp26",
                org_id=org.id,
                user_id=user.id,
                role_key="org_admin",
                scope_kind="org",
            )
        )
        # Legacy empty-scope credentials are rejected rather than interpreted as unrestricted.
        session.add(
            ApiToken(
                id="tok_cp26_empty",
                org_id=org.id,
                user_id=user.id,
                name="legacy-empty-scope",
                token_prefix=token_prefix,
                token_hash=token_digest,
                scopes=[],
                expires_at=now + dt.timedelta(days=1),
            )
        )

    transport = httpx.ASGITransport(app=app, client=("198.51.100.24", 43123))
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            failed = await client.post(
                "/api/v1/auth/login",
                headers={"X-Forwarded-For": "203.0.113.99, 203.0.113.100"},
                json={"email": "missing@cp26.example", "password": "WrongPassword!2026"},
            )
            assert failed.status_code == 401

            async with app.state.database.session() as session:
                buckets = (await session.execute(select(AuthRateLimitBucket))).scalars().all()
                ip_buckets = [bucket.bucket_key for bucket in buckets if bucket.scope == "login_ip"]
                assert ip_buckets == ["login_ip:198.51.100.24"]
                audit = (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.action == "auth.login",
                            AuditLog.outcome == "failure",
                        )
                    )
                ).scalar_one()
                assert audit.ip_address == "198.51.100.24"

            empty_scope = await client.get(
                "/api/v1/wells",
                headers={"Authorization": f"Bearer {raw_token}", "X-Forwarded-For": "203.0.113.99"},
            )
            assert empty_scope.status_code == 401
            assert "no authorization scopes" in empty_scope.json()["error"]["message"]
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


def test_cp26_02_forwarded_ip_configuration_trusts_only_the_pinned_proxy() -> None:
    """The Compose Nginx peer is static and Uvicorn's trust list is explicit, not wildcarded."""
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    nginx = (REPO_ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")

    assert "FORWARDED_ALLOW_IPS: ${DRILLAI_UVICORN_FORWARDED_ALLOW_IPS:-172.31.240.10}" in compose
    assert "ipv4_address: ${DRILLAI_FRONTEND_PROXY_IP:-172.31.240.10}" in compose
    assert "subnet: ${DRILLAI_EDGE_SUBNET:-172.31.240.0/28}" in compose
    assert "DRILLAI_UVICORN_FORWARDED_ALLOW_IPS=172.31.240.10" in env_example
    assert "FORWARDED_ALLOW_IPS: *" not in compose
    # Nginx still forwards the chain for Uvicorn's trusted-proxy parser, but does not log raw
    # caller-controlled X-Forwarded-For values as if they were verified client addresses.
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in nginx
    assert "$http_x_forwarded_for" not in nginx


async def test_cp26_03_tenant_admin_cannot_reset_or_deactivate_a_shared_user_or_grant_global_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tenant-scoped user actions cannot mutate global account state across organizations."""
    app = _make_app(tmp_path, monkeypatch, "cp26_tenant_boundaries")
    await _create_schema(app)
    now = dt.datetime.now(tz=UTC)

    async with app.state.database.session() as session:
        org_a = Organization(id="org_a", slug="alpha-cp26", name="Alpha")
        org_b = Organization(id="org_b", slug="beta-cp26", name="Beta")
        admin_a = User(
            id="usr_admin_a",
            email="admin@alpha-cp26.example",
            display_name="Alpha Administrator",
            password_hash=hash_password("Alpha-CP26-Admin-Password!2026"),
        )
        shared = User(
            id="usr_shared",
            email="shared@both-cp26.example",
            display_name="Shared Engineer",
            password_hash=hash_password("Shared-CP26-Engineer-Password!2026"),
        )
        session.add_all([org_a, org_b, admin_a, shared])
        await session.flush()
        session.add_all(
            [
                Membership(id="mbr_admin_a", org_id=org_a.id, user_id=admin_a.id, role_key="org_admin"),
                Membership(id="mbr_shared_a", org_id=org_a.id, user_id=shared.id, role_key="engineer"),
                Membership(id="mbr_shared_b", org_id=org_b.id, user_id=shared.id, role_key="viewer"),
            ]
        )
        session.add(
            ApiToken(
                id="tok_shared_b",
                org_id=org_b.id,
                user_id=shared.id,
                name="beta service token",
                token_prefix="dk_shared",
                token_hash=hash_token("api", "shared-cp26-secret"),
                scopes=["well.read"],
            )
        )
        session.add(
            UserSession(
                id="ses_shared_b",
                org_id=org_b.id,
                user_id=shared.id,
                session_token_prefix="ds_shared",
                session_token_hash=hash_token("session", "shared-cp26-session"),
                csrf_token_hash=hash_token("csrf", "shared-cp26-csrf"),
                expires_at=now + dt.timedelta(hours=12),
                last_seen_at=now,
            )
        )

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@alpha-cp26.example", "password": "Alpha-CP26-Admin-Password!2026"},
            )
            assert login.status_code == 200, login.text
            csrf = {"X-CSRF-Token": login.json()["csrf_token"]}

            for role_key in ("admin", "well_manager"):
                escalation = await client.post(
                    "/api/v1/auth/users",
                    headers=csrf,
                    json={
                        "email": f"escalate-{role_key}@alpha-cp26.example",
                        "display_name": "Escalation Attempt",
                        "password": "Valid-CP26-Password!2026",
                        "role_keys": [role_key],
                    },
                )
                assert escalation.status_code == 403, escalation.text

            reset = await client.post(
                "/api/v1/auth/password/reset-tokens",
                headers=csrf,
                json={"user_id": "usr_shared"},
            )
            assert reset.status_code == 403

            deactivate = await client.patch(
                "/api/v1/auth/users/usr_shared",
                headers=csrf,
                json={"is_active": False},
            )
            assert deactivate.status_code == 403

            async with app.state.database.session() as session:
                shared_after = await session.get(User, "usr_shared")
                token_after = await session.get(ApiToken, "tok_shared_b")
                session_after = await session.get(UserSession, "ses_shared_b")
                reset_rows = (
                    await session.execute(select(PasswordResetToken).where(PasswordResetToken.user_id == "usr_shared"))
                ).scalars().all()
                assert shared_after is not None and shared_after.is_active is True
                assert token_after is not None and token_after.revoked_at is None
                assert session_after is not None and session_after.revoked_at is None
                assert reset_rows == []
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


async def test_cp26_04_scoped_platform_admin_token_cannot_delegate_global_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A role-bearing token still needs the actual system scope to delegate platform authority."""
    app = _make_app(tmp_path, monkeypatch, "cp26_scoped_admin")
    await _create_schema(app)

    async with app.state.database.session() as session:
        org = Organization(id="org_admin_scope", slug="admin-scope", name="Admin Scope")
        admin = User(
            id="usr_platform_admin",
            email="admin@scope-cp26.example",
            display_name="Scoped Platform Administrator",
            password_hash=hash_password("Admin-Scope-CP26-Password!2026"),
        )
        session.add_all([org, admin])
        await session.flush()
        session.add(Membership(id="mbr_platform_admin", org_id=org.id, user_id=admin.id, role_key="admin"))

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as browser:
            login = await browser.post(
                "/api/v1/auth/login",
                json={"email": "admin@scope-cp26.example", "password": "Admin-Scope-CP26-Password!2026"},
            )
            assert login.status_code == 200, login.text
            csrf = {"X-CSRF-Token": login.json()["csrf_token"]}
            minted = await browser.post(
                "/api/v1/auth/tokens",
                headers=csrf,
                json={"name": "user-manager-only", "scopes": ["user.manage"]},
            )
            assert minted.status_code == 201, minted.text

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as token_client:
            for role_key in ("admin", "well_manager"):
                response = await token_client.post(
                    "/api/v1/auth/users",
                    headers={"Authorization": f"Bearer {minted.json()['token']}"},
                    json={
                        "email": f"scoped-{role_key}@scope-cp26.example",
                        "display_name": "Scoped Escalation Attempt",
                        "password": "Valid-CP26-Password!2026",
                        "role_keys": [role_key],
                    },
                )
                assert response.status_code == 403, response.text
    finally:
        await app.state.database.dispose()
        reset_settings_cache()
