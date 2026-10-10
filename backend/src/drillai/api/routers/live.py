"""The live feed: the event backbone over a WebSocket, with a well-scoped cursor.

A client connects, is told where the well's stream currently ends, receives a bounded snapshot of the
well it asked about, and then follows the well's contiguous sequence (`well_sequence`). Everything about
the design follows from one decision: **the socket is a view of the outbox table, not a queue**. The
database is the buffer, the per-well sequence number (paired with a scope-validated cursor token) is the
cursor, and the socket is a tail. That is what makes the failure modes honest:

* a dropped connection loses the *stream*, never the history — reconnecting with the same
  ``cursor`` (or ``after_seq``) returns exactly the events missed on that well, and asking twice returns
  the same page;
* events on other wells in the same organisation advance their own well sequences without inflating
  this well's backlog, so high-rate telemetry on Well B never causes a false ``backlog_exceeded`` gap
  or drops an alert on Well A;
* a cursor belonging to another well or organisation is refused with an explicit ``gap`` frame
  (`reason: "cursor_scope_mismatch"`) rather than silently skipping events;
* a slow client cannot make the server hold an unbounded queue: each tick reads at most
  ``live_stream_max_events_per_tick`` rows, and a client that has fallen further behind on this well than
  ``live_stream_max_backlog`` is told so explicitly (a ``gap`` frame naming the range it lost) rather
  than being quietly skipped forward;
* telemetry is **coalesced** — a hundred measurements a second would otherwise be a hundred frames a
  second, and a screen cannot render that. The coalesced frame carries how many events it folded and a
  ``resync`` flag, so a client knows to re-read the series instead of believing it saw every value.
  Alerts and operation changes are never coalesced: a missed acknowledgement is not a rendering detail.

Heartbeats exist for a different reason than most implementations: the client uses the ``position`` they
carry to notice that it has fallen behind without a gap frame — an idle socket that has actually missed
events is indistinguishable from an idle one that has not, unless the server keeps saying where it is.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, current_auth, get_db, require
from drillai.api.serializers import alert_out
from drillai.core.clock import utc_now
from drillai.core.errors import NotFound, ValidationFailed
from drillai.db.models import Operation, Well
from drillai.telemetry.alerts import AlertService
from drillai.telemetry.connectors import ConnectorService
from drillai.telemetry.outbox import (
    ENVELOPE_VERSION,
    STREAM_CURSOR_VERSION,
    decode_stream_cursor,
    encode_stream_cursor,
    envelope_of,
    events_since,
    stream_position,
    well_stream_sequence,
)
from drillai.telemetry.service import TelemetryService
from drillai.telemetry.vocabulary import COALESCIBLE_EVENT_TYPES

router = APIRouter(tags=["live"])

#: Frames a client can receive, documented in one place so the frontend and this file cannot drift:
#:
#: * ``stream_opened`` — hello: the stream position at connect, the scoped cursor, the schema version, the poll interval.
#: * ``snapshot`` — bounded current state (latest readings, open alerts, freshness counts, temporal as-of stamps).
#: * ``event`` — one envelope that was not coalesced (alerts, operation changes, events).
#: * ``telemetry`` — one frame folding N telemetry events, with ``omitted`` and ``resync``.
#: * ``gap`` — the client asked for a range the server will not replay (or sent an invalid/foreign cursor); names it and tells it to resync.
#: * ``heartbeat`` — the server's current position, so a client can notice a missed range.
#: * ``stream_idle`` — long quiet period; the client may reconnect from ``last_seq`` / ``cursor``.
#: * ``stream_error`` — a server-side failure; the socket closes afterwards.
FRAME_TYPES = (
    "stream_opened",
    "snapshot",
    "event",
    "telemetry",
    "gap",
    "heartbeat",
    "stream_idle",
    "stream_error",
)


def _live_filters() -> tuple[float, float, int, int, int, int]:
    from drillai.core.config import get_settings

    settings = get_settings()
    return (
        settings.live_stream_poll_seconds,
        settings.live_stream_heartbeat_seconds,
        settings.live_stream_max_events_per_tick,
        settings.live_stream_max_backlog,
        settings.live_stream_snapshot_alerts,
        settings.live_stream_snapshot_channels,
    )


class _HeaderShim:
    """The HTTP auth dependency, reused for a handshake that cannot carry headers from a browser."""

    def __init__(self, headers: dict[str, str], query: dict[str, str] | None = None) -> None:
        self.headers = headers
        self.query_params = query or {}

    @property
    def url(self) -> Any:  # pragma: no cover - only used by error handlers
        return None


async def _watch_client_disconnect(websocket: WebSocket) -> None:
    """Return as soon as the client goes away, so an abandoned socket stops reading the stream."""

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except Exception:  # pragma: no cover - a failed receive is a dead client
        return


async def _snapshot(
    session: AsyncSession, auth: AuthContext, well_id: str, *, position: int
) -> dict[str, Any]:
    """A bounded picture of the well at connect: readings, open alerts and freshness counts."""

    _, _, _, _, alert_bound, channel_bound = _live_filters()
    org_id = auth.org_id or ""
    service = TelemetryService(session, org_id)
    readings = await service.latest(well_id=well_id, limit=channel_bound)
    alerts = await AlertService(session, org_id).open_alerts(well_id=well_id, limit=alert_bound)
    freshness: dict[str, int] = {}
    latest_ts = None
    for reading in readings:
        freshness[reading.freshness] = freshness.get(reading.freshness, 0) + 1
        if reading.observed_at is not None and (latest_ts is None or reading.observed_at > latest_ts):
            latest_ts = reading.observed_at
    alerts_ts = None
    for row in alerts:
        candidate = row.updated_at or row.raised_at
        if candidate is not None and (alerts_ts is None or candidate > alerts_ts):
            alerts_ts = candidate
    operation_ts = (
        await session.execute(
            select(func.max(Operation.updated_at)).where(
                Operation.org_id == org_id,
                Operation.well_id == well_id,
                Operation.operation_class == "actual",
            )
        )
    ).scalar_one_or_none()
    trends = await service.trends(
        well_id=well_id, channel_ids=[reading.channel_id for reading in readings]
    )
    connectors_list, _ = await ConnectorService(
        session, org_id, principal=auth.principal
    ).list(well_id=well_id, limit=25)
    now_iso = utc_now().isoformat()
    return {
        "well_id": well_id,
        "generated_at": now_iso,
        "telemetry_as_of": latest_ts.isoformat() if latest_ts is not None else None,
        "operation_as_of": operation_ts.isoformat() if operation_ts is not None else None,
        "alerts_as_of": alerts_ts.isoformat() if alerts_ts is not None else None,
        "stream_position": position,
        "cursor": encode_stream_cursor(org_id=org_id, well_id=well_id, sequence=position),
        "latest": [reading.to_dict() for reading in readings],
        "trends": {key: value.to_dict() for key, value in sorted(trends.items())},
        "alerts": [alert_out(row) for row in alerts],
        "connectors": connectors_list,
        "freshness": dict(sorted(freshness.items())),
    }


@router.get("/wells/{well_id}/live/snapshot", summary="Bounded operational snapshot of a well")
async def get_well_live_snapshot(
    well_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[AuthContext, Depends(require("live.read"))],
) -> dict[str, Any]:
    """Assembled operational view (latest readings, non-predictive trends, open alerts, stream cursor)
    with explicit per-domain ``as_of`` timestamps for resync and polling fallback."""

    org_id = auth.org_id or ""
    owned = (
        await session.execute(
            select(Well.id).where(Well.id == well_id, Well.org_id == org_id)
        )
    ).scalar_one_or_none()
    if owned is None:
        raise NotFound(f"well {well_id!r} not found")
    position = await stream_position(session, org_id, well_id=well_id)
    return await _snapshot(session, auth, well_id, position=position)


@router.websocket("/wells/{well_id}/live/stream")
async def stream_well_live(
    websocket: WebSocket,
    well_id: str,
    after_seq: int = 0,
    cursor: str | None = None,
) -> None:
    """Follow one well's events, resuming from ``cursor`` (or legacy ``after_seq``).

    Close codes: 4401 unauthenticated, 4403 ``live.read`` missing, 4404 the well is not in this
    organisation (or does not exist — the two are the same answer on purpose: an identifier must not be
    a tenant capability).
    """

    await websocket.accept()
    database = websocket.app.state.database
    token = websocket.query_params.get("token")
    dev_roles = websocket.headers.get("x-dev-roles") or websocket.query_params.get("dev_roles")
    initial_gap: dict[str, Any] | None = None
    effective_after_seq = after_seq
    try:
        async with database.session() as session:
            headers = {}
            if token:
                headers["authorization"] = f"Bearer {token}"
            if dev_roles:
                headers["x-dev-roles"] = dev_roles
            request = _HeaderShim(headers, dict(websocket.query_params))
            auth = await current_auth(request, session)  # type: ignore[arg-type]
            if not auth.principal.has_permission("live.read"):
                await websocket.close(code=4403, reason="live.read is required")
                return
            owned = (
                await session.execute(
                    select(Well.id).where(Well.id == well_id, Well.org_id == auth.org_id)
                )
            ).scalar_one_or_none()
            if owned is None:
                await websocket.close(code=4404, reason="well not found")
                return
            org_id = auth.org_id or ""
            position = await stream_position(session, org_id, well_id=well_id)
            head_cursor = encode_stream_cursor(org_id=org_id, well_id=well_id, sequence=position)
            poll_seconds, heartbeat_seconds, per_tick, backlog_bound, _, _ = _live_filters()

            if cursor is not None:
                try:
                    decoded = decode_stream_cursor(
                        cursor,
                        expected_org_id=org_id,
                        expected_well_id=well_id,
                        require_well_scope=True,
                    )
                    effective_after_seq = decoded.sequence
                except ValidationFailed as err:
                    reason = str(err.details.get("reason") or "invalid_cursor")
                    initial_gap = {
                        "type": "gap",
                        "reason": reason,
                        "requested_cursor": cursor,
                        "well_id": well_id,
                        "position": position,
                        "cursor": head_cursor,
                        "resync": True,
                        "details": err.details,
                    }
                    effective_after_seq = position
            elif after_seq < 0:
                initial_gap = {
                    "type": "gap",
                    "reason": "invalid_cursor",
                    "requested_after": after_seq,
                    "well_id": well_id,
                    "position": position,
                    "cursor": head_cursor,
                    "resync": True,
                }
                effective_after_seq = position

            await websocket.send_json(
                {
                    "type": "stream_opened",
                    "well_id": well_id,
                    "org_id": auth.org_id,
                    "after_seq": effective_after_seq,
                    "position": position,
                    "cursor": head_cursor,
                    "cursor_version": STREAM_CURSOR_VERSION,
                    "cursor_scope": {
                        "kind": "well",
                        "org_id": auth.org_id,
                        "well_id": well_id,
                        "sequence_space": "well",
                    },
                    "schema_version": ENVELOPE_VERSION,
                    "poll_seconds": poll_seconds,
                    "heartbeat_seconds": heartbeat_seconds,
                    "server_time": utc_now().isoformat(),
                    "frame_types": list(FRAME_TYPES),
                    "coalesced_types": sorted(COALESCIBLE_EVENT_TYPES),
                }
            )
            await websocket.send_json(
                {
                    "type": "snapshot",
                    **_snapshot_payload(
                        await _snapshot(session, auth, well_id, position=position)
                    ),
                }
            )
            if initial_gap is not None:
                await websocket.send_json(initial_gap)
    except WebSocketDisconnect:
        return
    except Exception as exc:  # authentication/authorization failures close the socket
        with contextlib.suppress(RuntimeError):
            await websocket.close(code=4401, reason=f"not authorized: {exc}")
        return

    org_id = auth.org_id or ""
    sequence = max(0, effective_after_seq)
    idle_ticks = 0
    since_heartbeat = 0.0
    disconnect = asyncio.create_task(_watch_client_disconnect(websocket))
    try:
        while True:
            done, _pending = await asyncio.wait({disconnect}, timeout=poll_seconds)
            if disconnect in done:
                return
            since_heartbeat += poll_seconds
            async with database.session() as session:
                position = await stream_position(session, org_id, well_id=well_id)
                head_cursor = encode_stream_cursor(
                    org_id=org_id, well_id=well_id, sequence=position
                )

                # A client that claims a cursor beyond the stream end is confused (a restored backup, a
                # number from another well). Saying so is the only honest answer: silently sending
                # nothing would look exactly like a quiet well.
                if sequence > position:
                    await websocket.send_json(
                        {
                            "type": "gap",
                            "reason": "cursor_ahead_of_stream",
                            "requested_after": sequence,
                            "well_id": well_id,
                            "position": position,
                            "cursor": head_cursor,
                            "resync": True,
                        }
                    )
                    sequence = position

                # A client further behind than the backlog we agreed to replay on this well is told the
                # range it lost, by name, and continues from the end. Because `position` and `sequence`
                # are in this well's contiguous `well_sequence` space, events on other wells never
                # inflate `position - sequence`.
                if position - sequence > backlog_bound:
                    await websocket.send_json(
                        {
                            "type": "gap",
                            "reason": "backlog_exceeded",
                            "from": sequence + 1,
                            "to": position,
                            "omitted": position - sequence,
                            "well_id": well_id,
                            "position": position,
                            "cursor": head_cursor,
                            "resync": True,
                        }
                    )
                    sequence = position

                rows = await events_since(
                    session,
                    org_id,
                    after_sequence=sequence,
                    well_id=well_id,
                    limit=per_tick,
                )
                if rows:
                    idle_ticks = 0
                    frames, sequence = _frames_for(
                        rows, sequence, org_id=org_id, well_id=well_id
                    )
                    for frame in frames:
                        await websocket.send_json(frame)
                else:
                    idle_ticks += 1

            if since_heartbeat >= heartbeat_seconds:
                since_heartbeat = 0.0
                await websocket.send_json(
                    {
                        "type": "heartbeat",
                        "well_id": well_id,
                        "position": sequence,
                        "stream_position": position,
                        "cursor": encode_stream_cursor(
                            org_id=org_id, well_id=well_id, sequence=sequence
                        ),
                        "server_time": utc_now().isoformat(),
                    }
                )
            if idle_ticks > 300:  # ~5 minutes without activity: let the client reconnect
                await websocket.send_json(
                    {
                        "type": "stream_idle",
                        "well_id": well_id,
                        "last_seq": sequence,
                        "cursor": encode_stream_cursor(
                            org_id=org_id, well_id=well_id, sequence=sequence
                        ),
                    }
                )
                idle_ticks = 0
    except WebSocketDisconnect:
        return
    except Exception as exc:  # pragma: no cover - transport level
        payload = json.dumps({"type": "stream_error", "message": str(exc)})
        with contextlib.suppress(Exception):
            await websocket.send_text(payload)
            await websocket.close(code=1011)
    finally:
        disconnect.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait({disconnect})


def _snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return snapshot


def _frames_for(
    rows: list[Any],
    sequence: int,
    *,
    org_id: str,
    well_id: str,
) -> tuple[list[dict[str, Any]], int]:
    """Turn a page of stored events into frames, coalescing telemetry and nothing else."""

    frames: list[dict[str, Any]] = []
    telemetry_run: list[Any] = []

    def flush_telemetry() -> None:
        if not telemetry_run:
            return
        payload = [envelope_of(row, stream_well_id=well_id) for row in telemetry_run]
        end_seq = int(payload[-1]["sequence"])
        frames.append(
            {
                "type": "telemetry",
                "well_id": well_id,
                "from": payload[0]["sequence"],
                "to": end_seq,
                "cursor": encode_stream_cursor(
                    org_id=org_id, well_id=well_id, sequence=end_seq
                ),
                "omitted": max(0, len(payload) - 1),
                "count": len(payload),
                "resync": len(payload) > 1,
                "channels": [
                    {
                        "sequence": item["sequence"],
                        "org_sequence": item.get("org_sequence"),
                        "channel_id": item["payload"].get("channel_id"),
                        "channel_key": item["payload"].get("channel_key"),
                        "accepted": item["payload"].get("accepted"),
                        "last_ts": item["payload"].get("last_ts"),
                        "unit": item["payload"].get("unit"),
                    }
                    for item in payload
                ],
                "events": [item["id"] for item in payload],
            }
        )
        telemetry_run.clear()

    for row in rows:
        row_seq = well_stream_sequence(row)
        sequence = max(sequence, row_seq)
        if row.event_type in COALESCIBLE_EVENT_TYPES:
            telemetry_run.append(row)
            continue
        flush_telemetry()
        event_env = envelope_of(row, stream_well_id=well_id)
        frames.append(
            {
                "type": "event",
                "well_id": well_id,
                "cursor": event_env["cursor"],
                "event": event_env,
            }
        )
    flush_telemetry()
    return frames, sequence
