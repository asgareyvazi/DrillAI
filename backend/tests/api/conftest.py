"""API test harness.

Runs the real application over ASGI with a file-backed SQLite database:

* the same ``create_app()`` factory production uses (middleware, error handlers, routers);
* the real ``Database`` class, so session/commit semantics are exercised, not simulated;
* requests over HTTP through ``httpx.ASGITransport`` — no mocks, no borrowed service calls.

Authentication is disabled by default (``DRILLAI_AUTH_ENABLED=false``) so tests can act as a
catalogued role via ``X-Dev-Roles``. The tests that need bearer-token authentication build their own
app from the same factory with the flag on (see ``tests/api/test_error_contract.py`` and
``test_api.py``), because the settings are read at factory time and are process-wide.

WebSocket endpoints are driven by ``WebSocketSession`` (see the ``websocket`` fixture): the same
ASGI callable, in the same event loop as the test, speaking only what a browser can speak.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from drillai.core.config import get_settings, reset_settings_cache
from drillai.db.models import Base
from drillai.db.session import Database


@pytest.fixture
def api_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/api.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_RUN_EVENT_STREAM_POLL_SECONDS", "0.05")
    reset_settings_cache()
    settings = get_settings()
    yield settings
    reset_settings_cache()


@pytest_asyncio.fixture
async def app(api_settings):
    from drillai.api.app import _warm_registries, create_app

    application = create_app(api_settings)
    async with application.state.database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    application.state.catalogues = _warm_registries()
    yield application
    await application.state.database.dispose()


@pytest_asyncio.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest_asyncio.fixture
async def db(app) -> AsyncIterator[Database]:
    """Direct database access for arranging/extracting state the API contract does not expose."""
    yield app.state.database


def headers(role: str = "engineer,admin", **extra: str) -> dict[str, str]:
    """Development identity header.

    The default mirrors the server-side development default (an engineer who also administers the
    dev instance); tests that are about the authorization model pass a single explicit role.
    """
    return {"X-Dev-Roles": role, **extra}


@pytest_asyncio.fixture
def websocket(app):
    """Open the application's WebSocket endpoint from inside the test's event loop.

    Usage: ``async with websocket(path, dev_roles="drilling_supervisor") as socket: ...``
    """
    from tests.api.ws import WebSocketSession

    def _connect(path: str, **query: str) -> WebSocketSession:
        return WebSocketSession(app, path, query)

    return _connect
