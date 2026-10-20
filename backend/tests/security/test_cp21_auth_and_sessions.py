"""CP21 Multi-User Authentication, Session Security, CSRF, WebSocket Cookie Auth, Scoped Tokens & Rate Limiting Suite."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from tests.api.ws import WebSocketRefused, WebSocketSession

from drillai.api.app import create_app
from drillai.core.clock import UTC
from drillai.core.config import get_settings, reset_settings_cache
from drillai.core.ids import is_id
from drillai.db.models import (
    AuditLog,
    AuthRateLimitBucket,
    Base,
    Membership,
    Organization,
    Project,
    User,
    UserSession,
    Well,
)
from drillai.security.passwords import hash_password

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_cp21_01_production_bundle_exposure_eliminated_and_build_guard() -> None:
    """Verify VITE_API_TOKEN is removed from runtime client code and rejected by the Vite build guard."""
    client_ts = (REPO_ROOT / "frontend" / "src" / "api" / "client.ts").read_text(encoding="utf-8")
    app_shell_tsx = (REPO_ROOT / "frontend" / "src" / "components" / "layout" / "AppShell.tsx").read_text(
        encoding="utf-8"
    )
    env_security_ts = (REPO_ROOT / "frontend" / "src" / "lib" / "envSecurity.ts").read_text(
        encoding="utf-8"
    )
    vite_config_ts = (REPO_ROOT / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
    run_events_ts = (REPO_ROOT / "frontend" / "src" / "lib" / "runEvents.ts").read_text(encoding="utf-8")
    well_live_ts = (REPO_ROOT / "frontend" / "src" / "lib" / "wellLiveStream.ts").read_text(
        encoding="utf-8"
    )

    assert "VITE_API_TOKEN" not in client_ts
    assert "VITE_API_TOKEN" not in app_shell_tsx
    assert "assertSafeFrontendEnv" in env_security_ts
    assert "drillaiEnvSecurityPlugin" in vite_config_ts
    assert "url.searchParams.set('token'" not in run_events_ts
    assert "url.searchParams.set('token'" not in well_live_ts


async def test_cp21_02_per_user_login_logout_session_cookie_csrf_and_expiry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify bootstrap, per-user login, HttpOnly cookie attributes, CSRF enforcement, expiry, and logout."""
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/cp21_sessions.db")
    reset_settings_cache()
    app = create_app(get_settings())
    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. Anonymous request is refused with 401
            unauth = await client.get("/api/v1/wells")
            assert unauth.status_code == 401

            # 2. One-time bootstrap creates initial org + admin user + session cookie
            boot = await client.post(
                "/api/v1/auth/bootstrap",
                json={
                    "org_slug": "acme-drilling",
                    "org_name": "Acme Drilling Corp",
                    "email": "founder@acme.example.com",
                    "display_name": "Founder Admin",
                    "password": "StrongBootstrapPass!2026",
                    "role_keys": ["admin", "well_manager"],
                },
            )
            assert boot.status_code == 201, boot.text
            boot_data = boot.json()
            assert boot_data["csrf_token"]
            set_cookie_hdr = boot.headers.get("set-cookie", "")
            assert "drillai_session=" in set_cookie_hdr
            assert "HttpOnly" in set_cookie_hdr
            assert "SameSite=lax" in set_cookie_hdr

            # Second bootstrap attempt is refused with 409 Conflict
            boot_again = await client.post(
                "/api/v1/auth/bootstrap",
                json={
                    "org_slug": "second-org",
                    "org_name": "Second Org",
                    "email": "attacker@acme.example.com",
                    "display_name": "Attacker",
                    "password": "StrongBootstrapPass!2026",
                },
            )
            assert boot_again.status_code == 409
            assert boot_again.json()["error"]["details"]["reason"] == "bootstrap_already_completed"

            # 3. GET /api/v1/auth/me and GET /api/v1/platform/identity work via the HttpOnly cookie
            me_res = await client.get("/api/v1/auth/me")
            assert me_res.status_code == 200, me_res.text
            me_payload = me_res.json()
            assert me_payload["authenticated"] is True
            assert me_payload["identity_source"] == "session_cookie"
            assert me_payload["user"]["email"] == "founder@acme.example.com"
            csrf_token = me_payload["csrf_token"]

            id_res = await client.get("/api/v1/platform/identity")
            assert id_res.status_code == 200
            assert id_res.json()["identity_source"] == "session_cookie"
            assert id_res.json()["development_presets"] == []

            # 4. State-changing POST without X-CSRF-Token is refused with 403
            no_csrf = await client.post("/api/v1/projects", json={"name": "No CSRF Project"})
            assert no_csrf.status_code == 403
            assert no_csrf.json()["error"]["details"]["reason"] == "csrf_validation_failed"

            bad_csrf = await client.post(
                "/api/v1/projects",
                json={"name": "Bad CSRF Project"},
                headers={"X-CSRF-Token": "wrong_csrf_value"},
            )
            assert bad_csrf.status_code == 403

            # State-changing POST with valid X-CSRF-Token succeeds
            ok_proj = await client.post(
                "/api/v1/projects",
                json={"name": "CSRF Verified Project"},
                headers={"X-CSRF-Token": csrf_token},
            )
            assert ok_proj.status_code == 201, ok_proj.text

            # 5. Logout revokes the server-side session; replaying the old cookie fails with 401
            saved_cookie = client.cookies.get("drillai_session")
            assert saved_cookie
            logout_res = await client.post(
                "/api/v1/auth/logout",
                headers={"X-CSRF-Token": csrf_token},
            )
            assert logout_res.status_code == 200
            assert logout_res.json()["status"] == "logged_out"

            replay = await client.get(
                "/api/v1/wells",
                cookies={"drillai_session": saved_cookie},
            )
            assert replay.status_code == 401

            # 6. Interactive login creates a fresh session; expired sessions are rejected with 401
            login_res = await client.post(
                "/api/v1/auth/login",
                json={"email": "founder@acme.example.com", "password": "StrongBootstrapPass!2026"},
            )
            assert login_res.status_code == 200, login_res.text
            fresh_session_id = login_res.json()["session_id"]
            assert (await client.get("/api/v1/projects")).status_code == 200

            async with app.state.database.session() as db_session:
                s_row = (
                    await db_session.execute(
                        select(UserSession).where(UserSession.id == fresh_session_id)
                    )
                ).scalar_one()
                s_row.expires_at = dt.datetime.now(tz=UTC) - dt.timedelta(minutes=5)

            expired_check = await client.get("/api/v1/projects")
            assert expired_check.status_code == 401
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


async def test_cp21_03_websocket_cookie_auth_and_query_string_token_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify WebSockets authenticate via HttpOnly session cookie and reject query-string ?token=."""
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/cp21_ws.db")
    reset_settings_cache()
    app = create_app(get_settings())
    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with app.state.database.session() as session:
            org = Organization(id="org_ws", slug="ws-org", name="WS Org")
            user = User(
                id="usr_ws",
                email="ws@example.com",
                display_name="WS Supervisor",
                password_hash=hash_password("SupervisorWsPass!2026"),
            )
            session.add_all([org, user])
            await session.flush()
            session.add(
                Membership(
                    id="mbr_ws",
                    org_id=org.id,
                    user_id=user.id,
                    role_key="drilling_supervisor",
                    scope_kind="org",
                )
            )
            proj = Project(id="prj_ws", org_id=org.id, name="WS Project")
            session.add(proj)
            await session.flush()
            well = Well(
                id="wel_ws",
                org_id=org.id,
                project_id=proj.id,
                name="WS-WELL-1",
                well_type="development_producer",
                status="drilling",
            )
            session.add(well)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login_res = await client.post(
                "/api/v1/auth/login",
                json={"email": "ws@example.com", "password": "SupervisorWsPass!2026"},
            )
            assert login_res.status_code == 200
            session_cookie = client.cookies.get("drillai_session")
            assert session_cookie

        # 1. Passing ?token= in the WebSocket query string is rejected with 4401
        ws_query_token = WebSocketSession(
            app,
            "/api/v1/wells/wel_ws/live/stream",
            {"after_seq": "0", "token": session_cookie},
        )
        async with ws_query_token:
            with pytest.raises(WebSocketRefused) as refused:
                await ws_query_token.receive()
        assert refused.value.code == 4401
        assert "forbidden" in (refused.value.reason or "")

        # 2. Connecting with the HttpOnly session cookie (and no query-string token) succeeds
        ws_cookie = WebSocketSession(
            app,
            "/api/v1/wells/wel_ws/live/stream",
            {"after_seq": "0"},
            cookies={"drillai_session": session_cookie},
        )
        async with ws_cookie:
            opened = await ws_cookie.receive()
            assert opened["type"] == "stream_opened"
            assert opened["well_id"] == "wel_ws"
            snapshot = await ws_cookie.receive()
            assert snapshot["type"] == "snapshot"
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


async def test_cp21_04_tenant_user_provisioning_password_reset_and_scoped_api_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify tenant-scoped user provisioning, password change/reset, and scoped API token lifecycle."""
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/cp21_users_tokens.db")
    reset_settings_cache()
    app = create_app(get_settings())
    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with app.state.database.session() as session:
            org_a = Organization(id="org_a", slug="alpha", name="Alpha Operator")
            org_b = Organization(id="org_b", slug="beta", name="Beta Operator")
            admin_a = User(
                id="usr_admin_a",
                email="admin@alpha.example.com",
                display_name="Alpha Org Admin",
                password_hash=hash_password("AlphaOrgAdminPass!2026"),
            )
            user_b = User(
                id="usr_b",
                email="eng@beta.example.com",
                display_name="Beta Engineer",
                password_hash=hash_password("BetaEngineerPass!2026"),
            )
            session.add_all([org_a, org_b, admin_a, user_b])
            await session.flush()
            session.add_all(
                [
                    Membership(
                        id="mbr_a",
                        org_id=org_a.id,
                        user_id=admin_a.id,
                        role_key="org_admin",
                        scope_kind="org",
                    ),
                    Membership(
                        id="mbr_b",
                        org_id=org_b.id,
                        user_id=user_b.id,
                        role_key="engineer",
                        scope_kind="org",
                    ),
                ]
            )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            login_a = await client.post(
                "/api/v1/auth/login",
                json={"email": "admin@alpha.example.com", "password": "AlphaOrgAdminPass!2026"},
            )
            assert login_a.status_code == 200
            csrf_a = login_a.json()["csrf_token"]
            hdrs_a = {"X-CSRF-Token": csrf_a}

            # 1. Tenant org_admin cannot assign the platform-wide `admin` role
            forbid_admin = await client.post(
                "/api/v1/auth/users",
                headers=hdrs_a,
                json={
                    "email": "escalate@alpha.example.com",
                    "display_name": "Escalated",
                    "password": "ValidPassword!2026",
                    "role_keys": ["admin"],
                },
            )
            assert forbid_admin.status_code == 403

            # 2. Tenant org_admin can provision an engineer in Org A
            created_eng = await client.post(
                "/api/v1/auth/users",
                headers=hdrs_a,
                json={
                    "email": "engineer@alpha.example.com",
                    "display_name": "Alpha Engineer",
                    "password": "InitialEngineerPass!2026",
                    "role_keys": ["engineer"],
                },
            )
            assert created_eng.status_code == 201, created_eng.text
            eng_id = created_eng.json()["id"]

            # 3. Cross-tenant isolation: Org A admin cannot see or issue reset token for Org B user
            users_list = await client.get("/api/v1/auth/users")
            assert users_list.status_code == 200
            emails = [u["email"] for u in users_list.json()["items"]]
            assert "eng@beta.example.com" not in emails

            foreign_reset = await client.post(
                "/api/v1/auth/password/reset-tokens",
                headers=hdrs_a,
                json={"user_id": "usr_b"},
            )
            assert foreign_reset.status_code == 404

            # 4. Issue and redeem a single-use password reset token for Alpha Engineer
            issue_reset = await client.post(
                "/api/v1/auth/password/reset-tokens",
                headers=hdrs_a,
                json={"user_id": eng_id, "ttl_minutes": 30},
            )
            assert issue_reset.status_code == 201
            reset_token = issue_reset.json()["reset_token"]
            assert reset_token.startswith("prt_")

            redeem = await client.post(
                "/api/v1/auth/password/reset",
                json={"token": reset_token, "new_password": "ResetEngineerPass!2026"},
            )
            assert redeem.status_code == 200
            assert redeem.json()["status"] == "password_reset_complete"

            # Reusing the same reset token is rejected with 401
            redeem_again = await client.post(
                "/api/v1/auth/password/reset",
                json={"token": reset_token, "new_password": "AnotherPassword!2026"},
            )
            assert redeem_again.status_code == 401

            # 5. Scoped API tokens must be non-empty and tenant admins cannot widen a one-level
            # permission such as `user.*` into a recursive `user.**` token scope.
            empty_scope = await client.post(
                "/api/v1/auth/tokens",
                headers=hdrs_a,
                json={"name": "unscoped"},
            )
            assert empty_scope.status_code == 422

            recursive_scope = await client.post(
                "/api/v1/auth/tokens",
                headers=hdrs_a,
                json={"name": "recursive-user", "scopes": ["user.**"]},
            )
            assert recursive_scope.status_code == 403

            # org_admin cannot mint `system.egress_policy.manage` or `**`
            bad_scope = await client.post(
                "/api/v1/auth/tokens",
                headers=hdrs_a,
                json={"name": "overprivileged", "scopes": ["system.egress_policy.manage"]},
            )
            assert bad_scope.status_code == 403

            # Mint a read-only scoped token (`well.read` only) even though org_admin has `project.write`
            minted = await client.post(
                "/api/v1/auth/tokens",
                headers=hdrs_a,
                json={"name": "well-reader-only", "scopes": ["well.read"], "expires_in_days": 30},
            )
            assert minted.status_code == 201, minted.text
            clear_token = minted.json()["token"]
            token_id = minted.json()["id"]
            assert clear_token.startswith("dk_")

            # Listing tokens never returns the clear secret or hash
            listed = await client.get("/api/v1/auth/tokens")
            assert listed.status_code == 200
            assert listed.json()["total"] == 1
            assert "token" not in listed.json()["items"][0]
            assert "token_hash" not in listed.json()["items"][0]

            # Using the scoped token: `well.read` succeeds (200), `project.write` is denied (403)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as token_client:
                bearer = {"Authorization": f"Bearer {clear_token}"}
                wells_ok = await token_client.get("/api/v1/wells", headers=bearer)
                assert wells_ok.status_code == 200
                proj_denied = await token_client.post(
                    "/api/v1/projects",
                    headers=bearer,
                    json={"name": "Out of scope project"},
                )
                assert proj_denied.status_code == 403

                # Revoke the token and verify it is immediately rejected with 401
                revoked = await client.delete(f"/api/v1/auth/tokens/{token_id}", headers=hdrs_a)
                assert revoked.status_code == 200
                assert revoked.json()["is_active"] is False

                after_revoke = await token_client.get("/api/v1/wells", headers=bearer)
                assert after_revoke.status_code == 401
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


async def test_cp21_05_multi_worker_db_rate_limiting_and_lockout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify failed logins persist rate-limit counters in the DB and enforce 429 lockout with Retry-After."""
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_AUTH_RATE_LIMIT_MAX_FAILURES", "3")
    monkeypatch.setenv("DRILLAI_AUTH_RATE_LIMIT_LOCKOUT_SECONDS", "120")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/cp21_ratelimit.db")
    reset_settings_cache()
    app = create_app(get_settings())
    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    try:
        async with app.state.database.session() as session:
            org = Organization(id="org_rl", slug="rl-org", name="RL Org")
            user = User(
                id="usr_rl",
                email="target@rl.example.com",
                display_name="Rate Limit Target",
                password_hash=hash_password("CorrectHorseBatteryStaple!2026"),
            )
            session.add_all([org, user])
            await session.flush()
            session.add(
                Membership(
                    id="mbr_rl",
                    org_id=org.id,
                    user_id=user.id,
                    role_key="engineer",
                    scope_kind="org",
                )
            )

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            for attempt in range(3):
                fail_res = await client.post(
                    "/api/v1/auth/login",
                    json={"email": "target@rl.example.com", "password": f"WrongPass_{attempt}"},
                )
                assert fail_res.status_code == 401

            # 4th attempt (even with the CORRECT password!) must be refused with 429 RateLimited
            locked_res = await client.post(
                "/api/v1/auth/login",
                json={
                    "email": "target@rl.example.com",
                    "password": "CorrectHorseBatteryStaple!2026",
                },
            )
            assert locked_res.status_code == 429
            assert locked_res.json()["error"]["code"] == "platform.rate_limited"
            assert int(locked_res.headers.get("retry-after", "0")) > 0

        # Verify durable DB state in `auth_rate_limits` and `audit_logs`
        async with app.state.database.session() as session:
            buckets = (await session.execute(select(AuthRateLimitBucket))).scalars().all()
            assert len(buckets) >= 2
            assert all(is_id(bucket.id, "rlb") for bucket in buckets)
            assert any(b.failure_count >= 3 and b.blocked_until is not None for b in buckets)
            audits = (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "auth.login", AuditLog.outcome == "failure"
                    )
                )
            ).scalars().all()
            assert len(audits) == 3
    finally:
        await app.state.database.dispose()
        reset_settings_cache()
