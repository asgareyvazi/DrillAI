"""The browser's side of a WebSocket connection, for tests that drive the real ASGI app.

The driver lives in its own module rather than in ``conftest.py`` so there is exactly one
``WebSocketRefused`` class: pytest loads a conftest under two names, and a test that imported the
other one would find that ``pytest.raises`` does not match the exception it just caught.
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode


class WebSocketRefused(Exception):
    """The server closed the socket instead of streaming (a refusal, or the end of a terminal stream)."""

    def __init__(self, code: int | None, reason: str | None) -> None:
        super().__init__(f"websocket closed with {code}: {reason}")
        self.code = code
        self.reason = reason


class WebSocketSession:
    """The browser's side of a WebSocket connection, driven inside the test's own event loop.

    It speaks only what a browser can speak: a handshake carrying query parameters (a browser
    cannot set handshake headers), JSON frames received, and one disconnect sent. The ASGI callable
    is driven directly rather than through ``TestClient``, for two reasons. ``TestClient`` tears its
    portal down by cancelling the handler wherever it happens to be, and cancelling a task in the
    middle of SQLAlchemy's greenlet-based aiosqlite bridge deadlocks the connection — the suite hung
    instead of failing. And driving the callable directly lets a test assert the property that
    actually matters here: the handler ends **by itself** when the client goes away, instead of
    quietly polling the event log for a browser that no longer exists.
    """

    def __init__(self, app, path: str, query: dict[str, str] | None = None, *, timeout: float = 10.0) -> None:
        self.app = app
        self.path = path
        self.query = query or {}
        self.timeout = timeout
        self._inbound: asyncio.Queue[dict] = asyncio.Queue()
        self._outbound: asyncio.Queue[dict] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.closed_with: dict | None = None

    async def __aenter__(self) -> WebSocketSession:
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": urlencode(self.query).encode(),
            "root_path": "",
            "headers": [(b"host", b"testserver"), (b"connection", b"upgrade")],
            "client": ("127.0.0.1", 52345),
            "server": ("testserver", 80),
            "subprotocols": [],
            "state": {},
        }
        self._task = asyncio.create_task(self.app(scope, self._inbound.get, self._outbound.put))
        await self._inbound.put({"type": "websocket.connect"})
        accepted = await self._next()
        assert accepted["type"] == "websocket.accept", accepted
        return self

    @property
    def handler_state(self) -> str:
        """How the server-side handler ended: ``returned``, ``cancelled`` or ``raised``.

        A handler that stops because something cancelled it has not proved that it noticed the
        client leaving, so tests assert ``returned`` rather than merely "the task finished".
        """
        assert self._task is not None and self._task.done(), "the handler is still running"
        if self._task.cancelled():
            return "cancelled"
        if self._task.exception() is not None:
            return "raised"
        return "returned"

    async def __aexit__(self, *exc_info) -> None:
        if self._task is None:  # pragma: no cover - defensive
            return
        await self._inbound.put({"type": "websocket.disconnect", "code": 1000})
        done, _pending = await asyncio.wait({self._task}, timeout=self.timeout)
        if not done:  # pragma: no cover - a failure path, reported rather than hidden
            self._task.cancel()
            await asyncio.wait({self._task}, timeout=2)
            raise AssertionError("the stream handler did not stop after the client disconnected")

    async def _next(self) -> dict:
        return await asyncio.wait_for(self._outbound.get(), timeout=self.timeout)

    async def receive(self) -> dict:
        """The next frame the server sent, or a refusal/close raised as an exception."""
        if self.closed_with is not None:
            raise WebSocketRefused(self.closed_with.get("code"), self.closed_with.get("reason"))
        message = await self._next()
        if message["type"] == "websocket.close":
            self.closed_with = message
            raise WebSocketRefused(message.get("code"), message.get("reason"))
        assert message["type"] == "websocket.send", message
        return json.loads(message["text"])

    async def frames(self, count: int) -> list[dict]:
        """Read exactly ``count`` frames, failing (bounded) if the server stops sending."""
        assert self.closed_with is None
        return [await self.receive() for _ in range(count)]
