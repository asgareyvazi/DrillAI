"""Deterministic fault endpoints for the end-to-end suite.

Three of the failures the browser has to survive cannot be produced by a healthy server: a request
that never comes back, a response that is not the contract, and an unhandled server fault. The
end-to-end suite could fake them with a proxy that rewrites responses, but then the thing under test
would no longer be the real stack — and the properties being checked (that the UI says "timed out"
rather than "unreachable", that a broken body is a protocol failure rather than an outage) are
exactly the ones a rewriting proxy would paper over.

So the failures are produced inside the application instead, by routes that go through the real
middleware, the real exception handlers and the real HTTP stack:

* ``/__faults/slow`` sleeps for as long as it is told — a real deadline, really exceeded;
* ``/__faults/malformed`` answers 200 with a body that is not JSON;
* ``/__faults/wrong-shape`` answers 200 with valid JSON of the wrong shape;
* ``/__faults/unhandled`` raises — and is answered by the application's generic 500 handler, which
  is the boundary that has to be proven, not a hand-written error response.

**These routes cannot exist in production.** Registration requires the explicit
``DRILLAI_E2E_FAULTS=true`` setting *and* a non-production environment; the app factory refuses to
start when the two are combined (``create_app`` raises ``ConfigurationError``), so an operator who
sets the flag on a production deployment gets a failed boot rather than an open debug endpoint. The
router also declares no authentication dependency, which is precisely why that guard is mandatory
rather than a nicety.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Response

from drillai.core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/__faults", tags=["faults"], include_in_schema=False)

#: Upper bound on how long a fault route may sleep. A test that asks for more than this is a bug in
#: the test, and an unbounded sleep would hang the suite rather than fail it.
MAX_SLEEP_MS = 60_000


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
