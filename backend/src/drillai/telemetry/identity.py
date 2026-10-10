"""Channel identity, and the answer to "is this point the one I already have?".

Two problems are solved here, and both were real defects in the model as it stood:

**A channel's identity is not its key.** ``time_series`` carried ``UniqueConstraint("org_id",
"channel_key")``, so a platform could hold exactly one channel called ``wob`` — for the whole
organization. The second well's weight on bit could not be recorded, and the workaround (prefixing the
key with a well name) would have been a string convention that no query could rely on. Identity is
whatever distinguishes two channels in reality, and in this domain that is:

    organization  →  well  →  wellbore (optional)  →  operation (optional)  →  channel key

A *wellbore-level* channel (``wob`` on the 12¼" hole) and a *well-level* channel (``depth_md`` for the
well as a whole) are both legitimate, so the scope is a token that starts with the well and narrows
from there. The token is what both the unique constraint and the lookup use, which is why the database
and the service cannot disagree about identity: there is one function that produces it.

**A point's identity is what the source says, or a documented fingerprint.** When the acquisition
system supplies ``source_point_id``, that identifier is authoritative — the same (series, source point)
is the same measurement, whatever else changed. When it does not, the platform derives a fingerprint
from ``(series, instant, value, source_ref)``: identical replays collapse, and a re-send with a
different value at the same instant is a **conflict** the caller must resolve rather than an overwrite
the platform performs silently.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass

from drillai.core.errors import ValidationFailed
from drillai.db.models import Operation, Well, Wellbore

#: The order in which a scope is narrowed. The token uses the *narrowest* identifier present, which is
#: what makes a wellbore channel and a well channel different rows and the same channel found again on
#: a re-send.
_SCOPE_PRECEDENCE: tuple[str, ...] = ("operation_id", "wellbore_id", "well_id")


def scope_token(
    *,
    well_id: str | None,
    wellbore_id: str | None = None,
    operation_id: str | None = None,
) -> str:
    """The canonical identity token for a channel scope.

    ``well_id`` is required: a channel that belongs to no well cannot be placed on a rig, and accepting
    one would create a row that no well-scoped query could ever find. The token is a plain,
    longest-scope-first string so that it is readable in a database dump and computable in SQL:

        ``wel_01H...``
        ``wel_01H.../wb_01H...``
        ``wel_01H.../wb_01H.../opr_01H...``
    """

    if not well_id:
        raise ValidationFailed(
            "a channel must name the well it belongs to",
            details={"field": "well_id", "value": well_id},
        )
    if operation_id and not wellbore_id:
        # An operation is drilled on a bore; naming one without the other would let the same operation
        # id be attached to two bores, which the operation table itself forbids.
        raise ValidationFailed(
            "a channel scoped to an operation must name the wellbore the operation runs in",
            details={"field": "wellbore_id", "value": None, "operation_id": operation_id},
        )
    parts = [well_id]
    if wellbore_id:
        parts.append(wellbore_id)
    if operation_id:
        parts.append(operation_id)
    return "/".join(parts)


def scope_of(token: str) -> dict[str, str | None]:
    """Inverse of :func:`scope_token`, for a caller that has only the stored token."""

    parts = token.split("/")
    return {
        "well_id": parts[0] if len(parts) > 0 and parts[0] else None,
        "wellbore_id": parts[1] if len(parts) > 1 and parts[1] else None,
        "operation_id": parts[2] if len(parts) > 2 and parts[2] else None,
    }


def channel_identity(
    *,
    well_id: str | None,
    channel_key: str,
    dimension: str,
    wellbore_id: str | None = None,
    operation_id: str | None = None,
) -> str:
    """The identity a unique constraint and a lookup both use: scope plus key plus dimension.

    The dimension is part of the identity because it is part of the meaning: ``flow_rate`` in ``m3/s``
    and the same key in a different dimension would be two different quantities stored under one name,
    and the conversion on read would be a guess. Two channels with the same key and different
    dimensions are refused at creation rather than reconciled later.
    """

    if not channel_key or not channel_key.strip():
        raise ValidationFailed(
            "a channel needs a key", details={"field": "channel_key", "value": channel_key}
        )
    token = scope_token(well_id=well_id, wellbore_id=wellbore_id, operation_id=operation_id)
    # Case-fold the key, keep the dimension and the scope verbatim: ``WOB`` and ``wob`` are one channel
    # (an acquisition system that shouts is not a second quantity), while the dimension and the scope
    # are identifiers the platform minted and must match exactly.
    return f"{token}|{channel_key.strip().lower()}|{dimension.strip()}"


def fingerprint(
    *,
    series_id: str,
    ts: dt.datetime,
    value: float | None,
    source_ref: str | None,
    sequence: int | None = None,
) -> str:
    """The deterministic identity of a point whose source supplies no identifier.

    Documented inputs, in this order, joined so that no two different inputs can produce the same
    string: series, instant (UTC, microsecond precision), value (``repr`` of the float, so ``0.1`` and
    ``0.10000000000000001`` are one measurement and ``1`` and ``True`` are not), source reference and
    the source's own sequence when it has one.

    The value is *part* of the fingerprint on purpose. A source that re-sends the same instant with a
    different reading is not replaying — it is correcting, or it is a different measurement that
    happens to share a timestamp — and collapsing the two would silently discard a reading. Treating it
    as a distinct point and letting the caller decide (reject, revise) is the honest behaviour; see
    :func:`drillai.telemetry.service.TelemetryService.append_points`.
    """

    stamp = ts.astimezone(dt.UTC).isoformat()
    payload = json.dumps(
        [series_id, stamp, repr(value), source_ref or "", sequence if sequence is not None else ""],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


async def resolve_channel_scope(
    session,  # AsyncSession; typed loosely to keep this module import-light
    *,
    org_id: str,
    well_id: str | None,
    wellbore_id: str | None = None,
    operation_id: str | None = None,
) -> dict[str, str | None]:
    """Validate a scope against the real asset tree, tenant-first.

    Every reference must exist, belong to ``org_id``, and nest correctly (the wellbore belongs to the
    well, the operation runs on the wellbore). A reference that fails any of those is a 404: reporting
    "that wellbore exists but is not yours" would be a tenant-existence oracle, and reporting a
    validation failure would invite the caller to retry with a corrected id it cannot know.
    """

    from sqlalchemy import select

    from drillai.core.errors import NotFound

    if not well_id:
        raise ValidationFailed(
            "a data scope must name a well", details={"field": "well_id", "value": well_id}
        )
    well = (
        await session.execute(select(Well).where(Well.id == well_id, Well.org_id == org_id))
    ).scalar_one_or_none()
    if well is None:
        raise NotFound("well not found", details={"well_id": well_id})

    if wellbore_id:
        bore = (
            await session.execute(
                select(Wellbore).where(Wellbore.id == wellbore_id, Wellbore.org_id == org_id)
            )
        ).scalar_one_or_none()
        if bore is None or bore.well_id != well_id:
            raise NotFound(
                "wellbore not found", details={"wellbore_id": wellbore_id, "well_id": well_id}
            )
    if operation_id:
        operation = (
            await session.execute(
                select(Operation).where(Operation.id == operation_id, Operation.org_id == org_id)
            )
        ).scalar_one_or_none()
        if operation is None or operation.well_id != well_id:
            raise NotFound("operation not found", details={"operation_id": operation_id})
        if operation.wellbore_id and wellbore_id and operation.wellbore_id != wellbore_id:
            raise ValidationFailed(
                "the operation runs on a different wellbore than the scope names",
                details={
                    "field": "wellbore_id",
                    "operation_wellbore_id": operation.wellbore_id,
                    "value": wellbore_id,
                },
            )
    return {"well_id": well_id, "wellbore_id": wellbore_id, "operation_id": operation_id}


@dataclass(frozen=True)
class PointIdentity:
    """How a point was identified, and by which of the two rules."""

    source_point_id: str | None
    fingerprint: str | None
    identified_by: str

    @property
    def dedup_key(self) -> str:
        return self.source_point_id or self.fingerprint or ""


def point_identity(
    *,
    series_id: str,
    source_point_id: str | None,
    ts: dt.datetime,
    value: float | None,
    source_ref: str | None,
    sequence: int | None = None,
) -> PointIdentity:
    """The identity of an arriving point: the source's own id when it has one, else the fingerprint."""

    if source_point_id and source_point_id.strip():
        return PointIdentity(
            source_point_id=source_point_id.strip(), fingerprint=None, identified_by="source_point_id"
        )
    return PointIdentity(
        source_point_id=None,
        fingerprint=fingerprint(
            series_id=series_id, ts=ts, value=value, source_ref=source_ref, sequence=sequence
        ),
        identified_by="fingerprint",
    )
