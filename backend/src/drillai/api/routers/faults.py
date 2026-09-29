"""Deterministic fault endpoints for the end-to-end suite.

Three of the failures the browser has to survive cannot be produced by a healthy server: a request
that never comes back in time, a response that is not the contract, and an unhandled server fault.
The end-to-end suite could fake them with a proxy that rewrites responses, but then the thing under
test would no longer be the real stack — and the properties being checked (that the UI says "timed
out" rather than "unreachable", that an unreadable body is a protocol failure rather than an outage,
that a 500 carries a request id) are exactly the ones a rewriting proxy would paper over.

So the failures are produced inside the application. There are two mechanisms, and both go through
the real middleware, the real exception handlers and the real HTTP stack:

* **fixed routes** — ``/__faults/slow``, ``/__faults/malformed``, ``/__faults/wrong-shape`` and
  ``/__faults/unhandled``, used by the API-level tests (including the 500, which is answered by the
  application's own generic error handler rather than by a hand-written response);
* **arming** — ``POST /__faults/arm`` installs a rule that makes the *next* matching request fail:
  the browser journeys arm a rule against a path the UI really calls (an approval decision, a run
  read), then drive the interface the way an operator would. Nothing is intercepted in the browser:
  the request leaves the page, reaches the server, and the server fails it.

**These routes cannot exist in production.** Registration requires the explicit
``DRILLAI_E2E_FAULTS=true`` setting *and* a non-production environment; the app factory refuses to
start when the two are combined (``create_app`` raises ``ConfigurationError``). The control endpoints
and the fault middleware are unauthenticated by construction — they exist to make a *client* fail,
not to be authorised — which is precisely why that guard is a boot failure rather than a warning.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel, Field

from drillai.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/__faults", tags=["faults"], include_in_schema=False)

#: Upper bound on how long a fault may sleep. A test that asks for more than this is a bug in the
#: test, and an unbounded sleep would hang the suite rather than fail it.
MAX_SLEEP_MS = 60_000

FaultMode = Literal["unhandled", "slow", "malformed"]


class FaultRule(BaseModel):
    """One armed fault: requests matching `method` + `path` fail in `mode`."""

    method: str = Field(default="GET", description="HTTP method to match, or '*' for any")
    path: str = Field(description="Path to match exactly, or as a prefix when it ends with '*'")
    mode: FaultMode
    delay_ms: int = Field(default=4_000, ge=0, le=MAX_SLEEP_MS)


@dataclass
class _Armed:
    rule: FaultRule


#: Process-scoped, like the application's other state. The fault instance serves one test run.
_armed: list[_Armed] = []


def armed_rules() -> list[FaultRule]:
    return [entry.rule for entry in _armed]


def disarm_all() -> None:
    """Stop injecting faults."""
    _armed.clear()


def _matches(rule: FaultRule, method: str, path: str) -> bool:
    if rule.method != "*" and rule.method.upper() != method.upper():
        return False
    if rule.path.endswith("*"):
        return path.startswith(rule.path[:-1])
    return path == rule.path


def matching_rule(method: str, path: str) -> FaultRule | None:
    for entry in _armed:
        if _matches(entry.rule, method, path):
            return entry.rule
    return None


class FaultInjectorMiddleware:
    """Fail a matching request the way the injected fault describes.

    A plain ASGI middleware, registered as the innermost layer so the responses it produces still
    travel out through compression, CORS and the correlation middleware — without that ordering an
    injected response would be blocked by the browser and read as a network failure instead, which is
    the exact confusion this checkpoint exists to remove.

    Raising here is answered by the application's own generic error handler, with the same envelope,
    the same request id and the same retryable flag as any other unhandled failure.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/api/v1/__faults") or path.endswith("/__faults"):
            await self.app(scope, receive, send)
            return
        rule = matching_rule(scope.get("method", "GET"), path)
        if rule is None:
            await self.app(scope, receive, send)
            return

        logger.warning("e2e fault injection: %s %s -> %s", scope.get("method"), path, rule.mode)
        if rule.mode == "unhandled":
            raise RuntimeError(f"deliberate unhandled error injected for {path}")
        if rule.mode == "slow":
            # The request is held, then served for real: the client's deadline expires against a
            # server that is genuinely still working, and the response it abandoned is a real one.
            await asyncio.sleep(rule.delay_ms / 1000)
            await self.app(scope, receive, send)
            return
        response = Response(content='{"broken": ', media_type="application/json")
        await response(scope, receive, send)


@router.post("/arm")
async def arm(rule: FaultRule) -> dict[str, object]:
    _armed.append(_Armed(rule))
    return {"armed": [r.model_dump() for r in armed_rules()]}


@router.post("/disarm")
async def disarm() -> dict[str, object]:
    """Stop injecting faults."""
    disarm_all()
    return {"armed": []}


@router.get("/armed")
async def armed() -> dict[str, object]:
    return {"armed": [r.model_dump() for r in armed_rules()]}


@router.get("/slow")
async def slow(ms: Annotated[int, Query(ge=0, le=MAX_SLEEP_MS)] = 1_000) -> dict[str, object]:
    """Answer after a real delay, so a client deadline can be exceeded for real."""
    await asyncio.sleep(ms / 1000)
    return {"slept_ms": ms}


@router.get("/malformed")
async def malformed() -> Response:
    """Answer 200 with a body that is not JSON, with a JSON content type claiming otherwise."""
    return Response(content='{"broken": ', media_type="application/json")


@router.get("/wrong-shape")
async def wrong_shape() -> dict[str, object]:
    """Answer 200 with valid JSON that is not the shape the caller's guard expects."""
    return {"unexpected": [], "total": 0}


@router.get("/unhandled")
async def unhandled() -> dict[str, object]:
    """Raise, and let the application's own generic error handler answer."""
    logger.warning("e2e fault route: raising an unhandled error on purpose")
    raise RuntimeError("deliberate unhandled error from the e2e fault route")
