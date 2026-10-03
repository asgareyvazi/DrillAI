"""The governance ledger: who did what, under which authority, and whether it was allowed.

``AuditLog`` has existed in the schema since the baseline migration and — until this module — **no
code ever wrote a row to it**. It was dead structure: declared, indexed, migrated, never populated. A
table that nothing writes is not an audit trail, and no amount of schema makes it one.

This module is the single writer. Every governed mutation funnels through :func:`record_audit`, so
two properties hold by construction rather than by discipline:

* a mutation cannot be recorded without the *decision* that permitted it — ``action``, the permission
  that was required and the action level all travel together with the row;
* ``before`` and ``after`` are stored as they were at the moment of the change, so the ledger answers
  "what did this look like before?" without reconstructing it from later state.

What the ledger is *not*: a domain change history. ``ChangeRecord`` (``db/models/twin.py``) is that,
it is what the well timeline reads, and the asset service writes one as well when a change is worth
showing to a person. The two answer different questions — "was this authorised?" versus "what
changed in the well?" — and merging them would lose one of the answers.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.ids import new_id
from drillai.db.base import utc_now
from drillai.db.models import AuditLog
from drillai.security.actions import ActionLevel, Principal, level_of

__all__ = ["audit_identity_of", "record_audit"]

#: Fields copied from a row into the ``before``/``after`` snapshot. Values outside this set are not
#: recorded, so the ledger cannot silently accumulate whole payloads (including document text) just
#: because a caller passed a row.
_SNAPSHOT_EXCLUDED = frozenset({"attributes"})


def audit_identity_of(principal: Principal | None) -> dict[str, Any]:
    """The actor columns for ``principal``, in the shape ``AuditLog`` stores them."""
    if principal is None:
        return {"actor_kind": "system", "actor_id": None, "actor_display": None}
    return {
        "actor_kind": getattr(principal, "actor_kind", "user") or "user",
        "actor_id": principal.id,
        "actor_display": getattr(principal, "display_name", None) or principal.id,
    }


def snapshot(row: Any, fields: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """A JSON-safe ``before``/``after`` picture of ``row`` restricted to ``fields``.

    Values are JSON-encoded through the same rules the API uses for datetimes, so the ledger reads the
    same way the API does. Anything that still cannot be serialised is recorded as a marker naming its
    type rather than crashing the mutation it was describing: a ledger that refuses to record an
    unusual value would make the edit impossible, and one that dropped it silently would be lying by
    omission.
    """
    out: dict[str, Any] = {}
    for name in fields:
        if name in _SNAPSHOT_EXCLUDED:
            continue
        if not hasattr(row, name):
            continue
        value = getattr(row, name)
        if isinstance(value, dt.datetime):
            value = value.isoformat()
        elif isinstance(value, (list, tuple)):
            value = list(value)
        elif isinstance(value, dict):
            value = dict(value)
        out[name] = _json_safe(value)
    return out


def _json_safe(value: Any) -> Any:
    """``value`` if it can be stored in a JSON column, otherwise a marker naming what it was."""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return f"<unserialisable {type(value).__name__}>"
    return value


async def record_audit(
    session: AsyncSession,
    *,
    org_id: str,
    action: str,
    resource_kind: str,
    resource_id: str | None,
    principal: Principal | None = None,
    outcome: str = "success",
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    details: dict[str, Any] | None = None,
    project_id: str | None = None,
    well_id: str | None = None,
    permission: str | None = None,
    request_id: str | None = None,
    trace_id: str | None = None,
    ip_address: str | None = None,
    user_agent: str | None = None,
    occurred_at: dt.datetime | None = None,
) -> AuditLog:
    """Append one row to the governance ledger and return it.

    ``action_level`` is resolved from the action catalogue rather than passed in, so a row cannot
    claim a lower level than the action it records actually carries. An unknown action resolves to the
    fail-closed default (``L4``), which is the same rule authorization applies.
    """
    # The catalogue is registered here rather than assumed: `level_of` fails closed to L4 for an
    # unknown action, and a ledger entry that claims a higher level than the act carried is worse than
    # no entry at all. Registration is idempotent.
    from drillai.security.catalog import ensure_platform_actions_registered

    ensure_platform_actions_registered()
    level: ActionLevel = level_of(action)
    row = AuditLog(
        id=new_id("aud"),
        org_id=org_id,
        # The actor columns come from the principal alone: a hard-coded kind here would disagree with
        # `audit_identity_of` for a system-initiated change (an engine or workflow actor is not a user).
        action=action,
        action_level=level.value,
        resource_kind=resource_kind,
        resource_id=resource_id,
        project_id=project_id,
        well_id=well_id,
        outcome=outcome,
        permission_decision={
            "required": permission or action,
            "granted": outcome == "success",
            "action_level": level.value,
        },
        details=dict(details or {}),
        before=dict(before or {}),
        after=dict(after or {}),
        request_id=request_id,
        trace_id=trace_id or request_id,
        ip_address=ip_address,
        user_agent=user_agent,
        occurred_at=occurred_at or utc_now(),
        **audit_identity_of(principal),
    )
    session.add(row)
    await session.flush()
    return row
