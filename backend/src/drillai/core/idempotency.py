"""Replay protection for mutating API calls.

``IdempotencyKey`` was in the schema and unused. Without it, the ordinary failure of a client — a
request that succeeds on the server and times out on the wire, retried by the browser — creates a
second well, a second wellbore or a second life-cycle transition. None of those are recoverable by the
user, because the duplicate is indistinguishable from the thing they meant to create.

The contract is the conventional one:

* a caller that sends ``Idempotency-Key`` gets the **stored response** for a repeat of the same
  request, so a retry is answered rather than re-executed;
* the same key with a *different* payload is a conflict, not a replay — otherwise a key reused by
  accident would silently return an unrelated resource;
* a key that is still in flight is a conflict too, because two concurrent requests cannot both be the
  first one.

Only the *completed* response is stored, and the entry is written inside the same transaction as the
mutation it protects, so a rolled-back mutation leaves no key claiming that something happened.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.errors import Conflict
from drillai.core.ids import new_id
from drillai.db.models import IdempotencyKey

__all__ = ["complete", "fingerprint", "replay_or_reserve"]

_IN_PROGRESS = "in_progress"
_COMPLETED = "completed"


def fingerprint(payload: Any) -> str:
    """A stable hash of a request body, used to tell a retry from a reused key.

    ``sort_keys`` makes the hash independent of key order in the JSON object, so a client that
    serialises the same body differently still gets a replay rather than a spurious conflict.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _load(session: AsyncSession, org_id: str, key: str, scope: str) -> IdempotencyKey | None:
    return (
        await session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.org_id == org_id, IdempotencyKey.key == key, IdempotencyKey.scope == scope
            )
        )
    ).scalar_one_or_none()


async def replay_or_reserve(
    session: AsyncSession,
    *,
    org_id: str,
    key: str | None,
    scope: str,
    payload: Any,
) -> dict[str, Any] | None:
    """Return the stored response for a repeat call, ``None`` when the caller should proceed.

    Reserving is deliberate: the row is inserted *before* the mutation runs, so a concurrent duplicate
    fails on the unique constraint instead of racing the first request to completion.
    """
    if not key:
        return None
    request_hash = fingerprint(payload)
    existing = await _load(session, org_id, key, scope)
    if existing is not None:
        return _replay(existing, request_hash)

    record = IdempotencyKey(
        id=new_id("idk"),
        org_id=org_id,
        key=key,
        scope=scope,
        request_hash=request_hash,
        status=_IN_PROGRESS,
        response_payload={},
    )
    session.add(record)
    try:
        await session.flush()
    except IntegrityError:
        # Two callers arrived with the same key at the same time. One of them loses the insert; it
        # must not proceed, and it must not report a server error either — reload and replay.
        await session.rollback()
        race = await _load(session, org_id, key, scope)
        if race is None:  # pragma: no cover - the insert failed for a reason that is not this key
            raise
        return _replay(race, request_hash)
    return None


def _replay(record: IdempotencyKey, request_hash: str) -> dict[str, Any] | None:
    if record.request_hash and record.request_hash != request_hash:
        raise Conflict(
            "this Idempotency-Key was used with a different request body",
            details={"idempotency_key": record.key, "stored_request_hash": record.request_hash},
        )
    if record.status == _IN_PROGRESS:
        raise Conflict(
            "a request with this Idempotency-Key is still in progress",
            details={"idempotency_key": record.key},
        )
    return dict(record.response_payload or {})


async def complete(
    session: AsyncSession,
    *,
    org_id: str,
    key: str | None,
    scope: str,
    response: dict[str, Any],
) -> None:
    """Store the response so a later retry of the same request is answered from it."""
    if not key:
        return
    record = await _load(session, org_id, key, scope)
    if record is None:  # pragma: no cover - reserve always runs first
        return
    record.status = _COMPLETED
    record.response_payload = dict(response)
    record.response_ref = str(response.get("id") or "")[:120] or None
    await session.flush()
