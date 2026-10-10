"""The live feed: the event backbone over a WebSocket, with a cursor instead of a handshake.

A client connects, is told where the stream currently ends, receives a bounded snapshot of the well it
asked about, and then follows the sequence. Everything about the design follows from one decision: **the
socket is a view of the outbox table, not a queue**. The database is the buffer, the sequence number is
the cursor, and the socket is a tail. That is what makes the failure modes honest:

* a dropped connection loses the *stream*, never the history — reconnecting with the same
  ``after_seq`` returns exactly the events missed, and asking twice returns the same page;
* a slow client cannot make the server hold an unbounded queue: each tick reads at most
  ``live_stream_max_events_per_tick`` rows, and a client that has fallen further behind than
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
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.api.deps import AuthContext, current_auth
from drillai.api.serializers import alert_out
from drillai.core.clock import utc_now
from drillai.db.models import Well
from drillai.telemetry.alerts import AlertService
from drillai.telemetry.outbox import ENVELOPE_VERSION, envelope_of, events_since, stream_position
from drillai.telemetry.service import TelemetryService
from drillai.telemetry.vocabulary import COALESCIBLE_EVENT_TYPES

router = APIRouter(tags=["live"])

#: Frames a client can receive, documented in one place so the frontend and this file cannot drift:
#:
#: * ``stream_opened`` — hello: the stream position at connect, the schema version, the poll interval.
#: * ``snapshot`` — bounded current state (latest readings, open alerts, freshness counts).
#: * ``event`` — one envelope that was not coalesced (alerts, operation changes, events).
#: * ``telemetry`` — one frame folding N telemetry events, with ``omitted`` and ``resync``.
#: * ``gap`` — the client asked for a range the server will not replay; names it and tells it to resync.
#: * ``heartbeat`` — the server's current position, so a client can notice a missed range.
#: * ``stream_idle`` — long quiet period; the client may reconnect from ``last_seq``.
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
    """Return as soon as the client goes away, so an abandoned socket stops reading the stream.

    A handler that only sends cannot notice a dropped browser until the next write succeeds-fails, which
    on a quiet well may be minutes. Reading the socket is what makes the disconnect observable.
    """

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                return
    except Exception:  # pragma: no cover - a failed receive is a dead client
        return


async def _snapshot(session: AsyncSession, auth: AuthContext, well_id: str) -> dict[str, Any]:
    """A bounded picture of the well at connect: readings, open alerts and freshness counts.

    The snapshot is a *head start*, not a substitute for the stream: it is taken at a position the hello
    frame names, so any event the client receives afterwards is either already reflected here or newer
    than this position. It is bounded on both sides (channels and alerts) because a monitor that opens a
    well with two hundred channels should still paint immediately.
    """

    _, _, _, _, alert_bound, channel_bound = _live_filters()
    service = TelemetryService(session, auth.org_id or "")
    readings = await service.latest(well_id=well_id, limit=channel_bound)
    alerts = await AlertService(session, auth.org_id or "").open_alerts(well_id=well_id, limit=alert_bound)
    freshness: dict[str, int] = {}
    for reading in readings:
        freshness[reading.freshness] = freshness.get(reading.freshness, 0) + 1
    # The direction of travel comes from the same stored points the readings do, computed in two bounded
    # statements. It is a description of measurements already taken — the frame says so explicitly
    # (``is_prediction: false``), because a monitor that showed a slope as a forecast would be lying about
    # what the platform knows.
    trends = await service.trends(
        well_id=well_id, channel_ids=[reading.channel_id for reading in readings]
    )
    return {
        "well_id": well_id,
        "generated_at": utc_now().isoformat(),
        "latest": [reading.to_dict() for reading in readings],
        "trends": {key: value.to_dict() for key, value in sorted(trends.items())},
        "alerts": [alert_out(row) for row in alerts],
        "freshness": dict(sorted(freshness.items())),
    }


@router.websocket("/wells/{well_id}/live/stream")
async def stream_well_live(websocket: WebSocket, well_id: str, after_seq: int = 0) -> None:
    """Follow one well's events, resuming from ``after_seq``.

    Close codes: 4401 unauthenticated, 4403 ``live.read`` missing, 4404 the well is not in this
    organisation (or does not exist — the two are the same answer on purpose: an identifier must not be
    a tenant capability).
    """

    await websocket.accept()
    database = websocket.app.state.database
    token = websocket.query_params.get("token")
    dev_roles = websocket.headers.get("x-dev-roles") or websocket.query_params.get("dev_roles")
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
            position = await stream_position(session, auth.org_id or "", well_id=well_id)
            poll_seconds, heartbeat_seconds, per_tick, backlog_bound, _, _ = _live_filters()
            await websocket.send_json(
                {
                    "type": "stream_opened",
                    "well_id": well_id,
                    "org_id": auth.org_id,
                    "after_seq": after_seq,
                    "position": position,
                    "schema_version": ENVELOPE_VERSION,
                    "poll_seconds": poll_seconds,
                    "heartbeat_seconds": heartbeat_seconds,
                    "server_time": utc_now().isoformat(),
                    "frame_types": list(FRAME_TYPES),
                    "coalesced_types": sorted(COALESCIBLE_EVENT_TYPES),
                }
            )
            await websocket.send_json({"type": "snapshot", **_snapshot_payload(await _snapshot(session, auth, well_id))})
    except Exception as exc:  # authentication/authorization failures close the socket
        await websocket.close(code=4401, reason=f"not authorized: {exc}")
        return

    sequence = max(0, after_seq)
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
                position = await stream_position(session, auth.org_id or "", well_id=well_id)

                # A client that claims a cursor beyond the stream end is confused (a restored backup, a
                # number from another well). Saying so is the only honest answer: silently sending
                # nothing would look exactly like a quiet well.
                if sequence > position:
                    await websocket.send_json(
                        {
                            "type": "gap",
                            "reason": "cursor_ahead_of_stream",
                            "requested_after": sequence,
                            "position": position,
                            "resync": True,
                        }
                    )
                    sequence = position

                # A client further behind than the backlog we agreed to replay is told the range it
                # lost, by name, and continues from the end. This is the only place events are not
                # delivered, and it is never silent.
                if position - sequence > backlog_bound:
                    await websocket.send_json(
                        {
                            "type": "gap",
                            "reason": "backlog_exceeded",
                            "from": sequence + 1,
                            "to": position,
                            "omitted": position - sequence,
                            "position": position,
                            "resync": True,
                        }
                    )
                    sequence = position

                rows = await events_since(
                    session,
                    auth.org_id or "",
                    after_sequence=sequence,
                    well_id=well_id,
                    limit=per_tick,
                )
                if rows:
                    idle_ticks = 0
                    frames, sequence = _frames_for(rows, sequence)
                    for frame in frames:
                        await websocket.send_json(frame)
                else:
                    idle_ticks += 1

            if since_heartbeat >= heartbeat_seconds:
                since_heartbeat = 0.0
                await websocket.send_json(
                    {
                        "type": "heartbeat",
                        "position": sequence,
                        "stream_position": position,
                        "server_time": utc_now().isoformat(),
                    }
                )
            if idle_ticks > 300:  # ~5 minutes without activity: let the client reconnect
                await websocket.send_json({"type": "stream_idle", "last_seq": sequence})
                idle_ticks = 0
    except WebSocketDisconnect:
        return
    except Exception as exc:  # pragma: no cover - transport level
        payload = json.dumps({"type": "stream_error", "message": str(exc)})
        with contextlib.suppress(Exception):
            await websocket.send_text(payload)
            await websocket.close(code=1011)
    finally:
        # `asyncio.wait` returns the task's state instead of raising its cancellation, so the only
        # cancellation that can leave this handler is a genuine cancellation of the handler itself.
        disconnect.cancel()
        with contextlib.suppress(Exception):
            await asyncio.wait({disconnect})


def _snapshot_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    return snapshot


def _frames_for(rows: list[Any], sequence: int) -> tuple[list[dict[str, Any]], int]:
    """Turn a page of stored events into frames, coalescing telemetry and nothing else.

    The order of the page is the sequence order, and the frames preserve it: a client that applies them
    in order sees the same history a reader of the table would. Telemetry rows are folded into one frame
    *per contiguous run*, so an alert that arrives between two bursts of measurements is never moved
    across it — which is what keeps \"the alert was raised after these 40 samples\" true.
    """

    frames: list[dict[str, Any]] = []
    telemetry_run: list[Any] = []

    def flush_telemetry() -> None:
        if not telemetry_run:
            return
        payload = [envelope_of(row) for row in telemetry_run]
        frames.append(
            {
                "type": "telemetry",
                "from": payload[0]["sequence"],
                "to": payload[-1]["sequence"],
                "omitted": max(0, len(payload) - 1),
                "count": len(payload),
                "resync": len(payload) > 1,
                "channels": [
                    {
                        "sequence": item["sequence"],
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
        sequence = max(sequence, int(row.sequence))
        if row.event_type in COALESCIBLE_EVENT_TYPES:
            telemetry_run.append(row)
            continue
        flush_telemetry()
        frames.append({"type": "event", "event": envelope_of(row)})
    flush_telemetry()
    return frames, sequence
