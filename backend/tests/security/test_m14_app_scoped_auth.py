"""Regression: settings belonging to one application must never authorize another."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from tests.api.ws import WebSocketRefused, WebSocketSession

from drillai.api.app import create_app
from drillai.core.config import Settings, get_settings, reset_settings_cache
from drillai.core.errors import ConfigurationError


def _settings(tmp_path: Path, name: str, *, auth: bool) -> Settings:
    return Settings(
        environment="test", auth_enabled=auth, blob_backend="memory",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / (name + '.db')).as_posix()}",
        session_cookie_name=f"{name}_cookie", session_csrf_secret=f"{name}-secret-not-the-global-cache",
        dev_org_slug=f"{name}-org", run_migrations_on_start=False,
    )


async def _app(settings: Settings):
    app = create_app(settings)
    await app.state.database.create_all()
    return app


async def test_explicit_auth_beats_disabled_cache_and_dev_roles(tmp_path, monkeypatch):
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    reset_settings_cache()
    assert not get_settings().auth_enabled
    app = await _app(_settings(tmp_path, "secure", auth=True))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for headers in ({}, {"X-Dev-Roles": "admin"}, {"Authorization": "Bearer invalid"}, {"Cookie": "secure_cookie=bad"}):
                assert (await client.get("/api/v1/wells", headers=headers)).status_code == 401
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


async def test_explicit_development_beats_enabled_cache_and_interleaves(tmp_path, monkeypatch):
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    reset_settings_cache()
    assert get_settings().auth_enabled
    secured = await _app(_settings(tmp_path, "secured", auth=True))
    dev = await _app(_settings(tmp_path, "development", auth=False))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=secured), base_url="http://test") as a, httpx.AsyncClient(transport=httpx.ASGITransport(app=dev), base_url="http://test") as b:
            # Initialize the developer org before concurrent read requests.
            assert (await b.get("/api/v1/wells")).status_code == 200
            for _ in range(3):
                assert (await a.get("/api/v1/wells", headers={"X-Dev-Roles": "admin"})).status_code == 401
                assert (await b.get("/api/v1/wells")).status_code == 200
            results = await asyncio.gather(*[c.get("/api/v1/wells") for c in [a, b] * 5])
            assert [r.status_code for r in results] == [401, 200] * 5
            me = await b.get("/api/v1/auth/me")
            assert me.status_code == 200 and me.json()["auth_enabled"] is False
            identity = await b.get("/api/v1/platform/identity", headers={"Accept-Language": "fa-IR"})
            assert identity.status_code == 200
            assert identity.json()["auth_enabled"] is False
            assert identity.json()["locale"] == "fa"
            assert identity.json()["development_presets"]
    finally:
        await secured.state.database.dispose()
        await dev.state.database.dispose()
        reset_settings_cache()


async def test_two_apps_cookie_csrf_database_and_revocation_isolation(tmp_path, monkeypatch):
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    reset_settings_cache()
    get_settings()
    apps = [await _app(_settings(tmp_path, n, auth=True)) for n in ("alpha", "beta")]
    clients = [httpx.AsyncClient(transport=httpx.ASGITransport(app=a), base_url="http://test") for a in apps]
    try:
        csrf = []
        for name, client in zip(("alpha", "beta"), clients, strict=True):
            r = await client.post("/api/v1/auth/bootstrap", json={"org_slug": f"{name}-org", "org_name": f"{name} organization", "email": f"{name}@example.test", "display_name": name, "password": "VeryStrongTestPass!2026", "role_keys": ["admin"]})
            assert r.status_code == 201, r.text
            assert f"{name}_cookie=" in r.headers["set-cookie"]
            csrf.append(r.json()["csrf_token"])
            assert (await client.get("/api/v1/auth/me")).json()["user"]["email"] == f"{name}@example.test"
        assert csrf[0] != csrf[1]
        assert (await clients[0].post("/api/v1/projects", json={"name": "wrong"}, headers={"X-CSRF-Token": csrf[1]})).status_code == 403
        assert (await clients[0].post("/api/v1/projects", json={"name": "correct"}, headers={"X-CSRF-Token": csrf[0]})).status_code == 201
        # A shared database and cookie name must not make a session valid under a different CSRF key.
        alternate = await _app(apps[0].state.settings.model_copy(update={"session_csrf_secret": "different-app-csrf-secret"}))
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=alternate), base_url="http://test") as alternate_client:
                assert (await alternate_client.get("/api/v1/wells", cookies={"alpha_cookie": clients[0].cookies.get("alpha_cookie")})).status_code == 401
        finally:
            await alternate.state.database.dispose()
        old = clients[0].cookies.get("alpha_cookie")
        assert (await clients[0].post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf[0]})).status_code == 200
        assert (await clients[0].get("/api/v1/wells", cookies={"alpha_cookie": old})).status_code == 401
        assert (await clients[1].get("/api/v1/wells")).status_code == 200
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[1]), base_url="http://test") as other:
            assert (await other.get("/api/v1/wells", cookies={"beta_cookie": old})).status_code == 401
    finally:
        for c in clients:
            await c.aclose()
        for app in apps:
            await app.state.database.dispose()
        reset_settings_cache()


async def test_websocket_uses_own_app_auth_not_global_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    reset_settings_cache()
    get_settings()
    app = await _app(_settings(tmp_path, "socket", auth=True))
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            r = await client.post("/api/v1/auth/bootstrap", json={"org_slug": "socket-org", "org_name": "Socket Organization", "email": "socket@example.test", "display_name": "Socket Admin", "password": "VeryStrongTestPass!2026", "role_keys": ["admin"]})
            assert r.status_code == 201, r.text
            cookie = client.cookies.get("socket_cookie")
        for path in ("/api/v1/wells/not-a-well/live/stream", "/api/v1/runs/not-a-run/events/stream"):
            async with WebSocketSession(app, path, {"dev_roles": "admin"}) as socket:
                with pytest.raises(WebSocketRefused) as exc:
                    await socket.receive()
                assert exc.value.code == 4401
            async with WebSocketSession(app, path, cookies={"socket_cookie": cookie}) as socket:
                with pytest.raises(WebSocketRefused) as exc:
                    await socket.receive()
                # The cookie was authenticated; the deliberately absent resource is then refused.
                assert exc.value.code == 4404
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


def test_production_cannot_disable_auth():
    with pytest.raises(ConfigurationError):
        create_app(Settings(environment="production", auth_enabled=False))
