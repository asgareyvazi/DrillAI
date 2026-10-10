"""ETP 1.2 WebSocket/JSON session & subscription client and local protocol-compatible server.

Implements the ``etp.1.2.json_ws`` profile:
- WebSocket subprotocol ``energistics-tp`` with JSON envelope framing:
  - Protocol ``0`` (Core): ``RequestSession``, ``OpenSession``, ``Ping``, ``Pong``, ``CloseSession``, ``ProtocolException``
  - Protocol ``1`` (ChannelStreaming): ``Start``, ``ChannelMetadata``, ``ChannelData``, ``Stop``
- Standards-based Bearer or Basic authentication on WebSocket handshake
- Channel metadata resolution (`channelId` -> mnemonic, unit, description) and mapped `TelemetryFrame` normalization
- Cursor-based subscription resume (`start_timestamp` in `Start` message) and duplicate suppression on replay
- Bounded frame size (`1 MB`) and bounded batch buffer (`max_frames`) for flow control
- ``LocalEtpWebSocketServer``: a real TCP WebSocket server speaking ETP 1.2 JSON framing for integration
  and E2E tests (explicitly distinct from external vendor certification).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import Server, ServerConnection
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosedOK, InvalidStatus, WebSocketException

from drillai.telemetry.adapters import SourceFrame
from drillai.telemetry.connectors import (
    ConnectorTransportError,
    parse_channel_mappings,
    redact_sensitive_text,
)

MAX_ETP_FRAME_BYTES = 1024 * 1024  # 1 MB


@dataclass(frozen=True)
class EtpChannelInfo:
    channel_id: int
    mnemonic: str
    unit: str
    channel_name: str
    uri: str = ""


@dataclass(frozen=True)
class EtpPollBatch:
    frames: tuple[SourceFrame, ...]
    discovered_channels: tuple[EtpChannelInfo, ...]
    next_watermark: dict[str, Any]
    session_id: str
    messages_received: int
    duplicates_suppressed: int


def _parse_iso_utc(raw: str | int | float) -> dt.datetime:
    if isinstance(raw, int | float):
        # ETP timestamps in microseconds or seconds since epoch
        val = float(raw)
        if val > 1e12:
            val = val / 1_000_000.0
        return dt.datetime.fromtimestamp(val, tz=dt.UTC)
    cleaned = str(raw).strip()
    if cleaned.endswith("Z") or cleaned.endswith("z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


class EtpWebSocketClient:
    """Async ETP 1.2 JSON-framed WebSocket client supporting handshake, channel discovery,
    bounded streaming batch collection, heartbeat ping/pong, and cursor-based resume."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        secrets: dict[str, str] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._secrets = dict(secrets or {})
        self._timeout_seconds = max(1.0, min(timeout_seconds, 30.0))

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if "bearer_token" in self._secrets:
            headers["Authorization"] = f"Bearer {self._secrets['bearer_token']}"
        elif "username" in self._secrets and "password" in self._secrets:
            token = base64.b64encode(
                f"{self._secrets['username']}:{self._secrets['password']}".encode()
            ).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        elif "api_key" in self._secrets:
            headers["X-API-Key"] = self._secrets["api_key"]
        return headers

    @staticmethod
    def _decode_message(raw_msg: str | bytes) -> dict[str, Any]:
        raw_bytes = raw_msg.encode("utf-8") if isinstance(raw_msg, str) else raw_msg
        if len(raw_bytes) > MAX_ETP_FRAME_BYTES:
            raise ConnectorTransportError(
                "oversized_payload",
                f"ETP WebSocket frame exceeds {MAX_ETP_FRAME_BYTES} bytes",
                retryable=False,
            )
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except Exception as exc:
            raise ConnectorTransportError(
                "malformed_payload",
                f"ETP WebSocket frame is not valid JSON: {exc}",
                retryable=False,
            ) from exc
        if not isinstance(data, dict):
            raise ConnectorTransportError(
                "malformed_payload",
                "ETP WebSocket frame must be a JSON object",
                retryable=False,
            )
        return data

    async def probe_session(self) -> tuple[str, list[EtpChannelInfo]]:
        """Perform ETP ``RequestSession`` -> ``OpenSession`` and ``Start`` -> ``ChannelMetadata`` discovery."""
        headers = self._build_headers()
        try:
            async with ws_connect(
                self._endpoint_url,
                subprotocols=["energistics-tp"],
                additional_headers=headers,
                open_timeout=self._timeout_seconds,
                close_timeout=2.0,
                max_size=MAX_ETP_FRAME_BYTES,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "protocol": 0,
                            "messageType": "RequestSession",
                            "messageId": 1,
                            "body": {
                                "applicationName": "DrillAI Connector Probe",
                                "applicationVersion": "1.0",
                                "requestedProtocols": [
                                    {"protocol": 0, "protocolVersion": {"major": 1, "minor": 2}, "role": "client"},
                                    {"protocol": 1, "protocolVersion": {"major": 1, "minor": 2}, "role": "consumer"},
                                ],
                            },
                        }
                    )
                )
                raw_open = await asyncio.wait_for(ws.recv(), timeout=self._timeout_seconds)
                open_msg = self._decode_message(raw_open)
                if open_msg.get("messageType") == "ProtocolException":
                    err_text = str((open_msg.get("body") or {}).get("message") or "ETP ProtocolException")
                    raise ConnectorTransportError("auth_failure", err_text, retryable=False)
                if open_msg.get("messageType") != "OpenSession":
                    raise ConnectorTransportError(
                        "protocol_error",
                        f"expected ETP OpenSession, got {open_msg.get('messageType')!r}",
                        retryable=False,
                    )
                session_id = str((open_msg.get("body") or {}).get("sessionId") or "etp-session")

                await ws.send(
                    json.dumps(
                        {
                            "protocol": 1,
                            "messageType": "Start",
                            "messageId": 2,
                            "body": {"maxMessageRate": 100, "maxDataItems": 200, "discoverOnly": True},
                        }
                    )
                )
                discovered: list[EtpChannelInfo] = []
                try:
                    raw_meta = await asyncio.wait_for(ws.recv(), timeout=min(self._timeout_seconds, 3.0))
                    meta_msg = self._decode_message(raw_meta)
                    if meta_msg.get("messageType") == "ChannelMetadata":
                        for ch in (meta_msg.get("body") or {}).get("channels", []):
                            discovered.append(
                                EtpChannelInfo(
                                    channel_id=int(ch.get("channelId", len(discovered) + 1)),
                                    mnemonic=str(ch.get("mnemonic") or ch.get("channelName") or ""),
                                    unit=str(ch.get("uom") or ch.get("unit") or ""),
                                    channel_name=str(ch.get("channelName") or ch.get("mnemonic") or ""),
                                    uri=str(ch.get("channelUri") or ""),
                                )
                            )
                except TimeoutError:
                    pass

                with contextlib.suppress(Exception):
                    await ws.send(
                        json.dumps(
                            {
                                "protocol": 0,
                                "messageType": "CloseSession",
                                "messageId": 3,
                                "body": {"reason": "probe complete"},
                            }
                        )
                    )
                return session_id, discovered
        except ConnectorTransportError:
            raise
        except InvalidStatus as exc:
            status = getattr(exc.response, "status_code", 0)
            if status in {401, 403}:
                raise ConnectorTransportError(
                    "auth_failure",
                    f"ETP server rejected handshake with HTTP {status}",
                    retryable=False,
                ) from exc
            raise ConnectorTransportError(
                "protocol_error",
                f"ETP server handshake failed with HTTP {status}",
                retryable=status >= 500,
            ) from exc
        except TimeoutError as exc:
            raise ConnectorTransportError(
                "timeout",
                "ETP WebSocket handshake or session open timed out",
                retryable=True,
            ) from exc
        except (OSError, WebSocketException) as exc:
            raise ConnectorTransportError(
                "connection_refused",
                redact_sensitive_text(f"ETP WebSocket connection error: {exc}", self._secrets)
                or "ETP WebSocket connection error",
                retryable=True,
            ) from exc

    async def poll_subscription_batch(
        self,
        *,
        well_id: str,
        wellbore_id: str | None = None,
        operation_id: str | None = None,
        config: dict[str, Any],
        watermark: dict[str, Any] | None = None,
        max_frames: int = 200,
        idle_timeout_seconds: float = 0.4,
    ) -> EtpPollBatch:
        """Open/resume an ETP 1.2 streaming session, collect up to ``max_frames`` mapped frames,
        send ``Ping``/``Pong`` if needed, and close cleanly."""
        mappings = parse_channel_mappings(config.get("channel_mappings"))
        mapping_by_mnemonic = {m.source_mnemonic.upper(): m for m in mappings}

        current_watermark = dict(watermark or {})
        cursor_ts_raw = current_watermark.get("cursor_timestamp")
        prev_cursor_dt: dt.datetime | None = None
        if cursor_ts_raw:
            try:
                prev_cursor_dt = _parse_iso_utc(str(cursor_ts_raw))
            except ValueError:
                prev_cursor_dt = None
        max_dt: dt.datetime | None = prev_cursor_dt

        headers = self._build_headers()
        try:
            async with ws_connect(
                self._endpoint_url,
                subprotocols=["energistics-tp"],
                additional_headers=headers,
                open_timeout=self._timeout_seconds,
                close_timeout=2.0,
                max_size=MAX_ETP_FRAME_BYTES,
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "protocol": 0,
                            "messageType": "RequestSession",
                            "messageId": 1,
                            "body": {
                                "applicationName": "DrillAI Connector Worker",
                                "applicationVersion": "1.0",
                                "requestedProtocols": [
                                    {"protocol": 0, "protocolVersion": {"major": 1, "minor": 2}, "role": "client"},
                                    {"protocol": 1, "protocolVersion": {"major": 1, "minor": 2}, "role": "consumer"},
                                ],
                            },
                        }
                    )
                )
                raw_open = await asyncio.wait_for(ws.recv(), timeout=self._timeout_seconds)
                open_msg = self._decode_message(raw_open)
                if open_msg.get("messageType") == "ProtocolException":
                    err_text = str((open_msg.get("body") or {}).get("message") or "ETP ProtocolException")
                    raise ConnectorTransportError("auth_failure", err_text, retryable=False)
                if open_msg.get("messageType") != "OpenSession":
                    raise ConnectorTransportError(
                        "protocol_error",
                        f"expected ETP OpenSession, got {open_msg.get('messageType')!r}",
                        retryable=False,
                    )
                session_id = str((open_msg.get("body") or {}).get("sessionId") or "etp-session")

                await ws.send(
                    json.dumps(
                        {
                            "protocol": 1,
                            "messageType": "Start",
                            "messageId": 2,
                            "body": {
                                "maxMessageRate": 100,
                                "maxDataItems": max_frames,
                                "channels": [m.source_mnemonic for m in mappings],
                                "startTimestamp": str(cursor_ts_raw) if cursor_ts_raw else None,
                            },
                        }
                    )
                )

                channel_by_id: dict[int, EtpChannelInfo] = {}
                discovered: list[EtpChannelInfo] = []
                frames: list[SourceFrame] = []
                seen_keys: set[tuple[str, str]] = set()
                messages_received = 1
                duplicates_suppressed = 0

                while len(frames) < max_frames:
                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=idle_timeout_seconds)
                    except (TimeoutError, ConnectionClosedOK):
                        break
                    messages_received += 1
                    msg = self._decode_message(raw_msg)
                    mtype = msg.get("messageType")
                    body = msg.get("body") or {}

                    if mtype == "Ping":
                        await ws.send(
                            json.dumps(
                                {
                                    "protocol": 0,
                                    "messageType": "Pong",
                                    "messageId": messages_received + 10,
                                    "body": {"currentDateTime": dt.datetime.now(dt.UTC).isoformat()},
                                }
                            )
                        )
                        continue

                    if mtype == "ProtocolException":
                        raise ConnectorTransportError(
                            "protocol_error",
                            str(body.get("message") or "ETP ProtocolException"),
                            retryable=False,
                        )

                    if mtype == "ChannelMetadata":
                        for ch in body.get("channels", []):
                            info = EtpChannelInfo(
                                channel_id=int(ch.get("channelId", len(discovered) + 1)),
                                mnemonic=str(ch.get("mnemonic") or ch.get("channelName") or ""),
                                unit=str(ch.get("uom") or ch.get("unit") or ""),
                                channel_name=str(ch.get("channelName") or ch.get("mnemonic") or ""),
                                uri=str(ch.get("channelUri") or ""),
                            )
                            channel_by_id[info.channel_id] = info
                            discovered.append(info)
                        continue

                    if mtype == "ChannelData":
                        for item in body.get("data", []):
                            if not isinstance(item, dict):
                                continue
                            ch_id = item.get("channelId")
                            ch_info = channel_by_id.get(int(ch_id)) if isinstance(ch_id, int) else None
                            mnem = str(
                                item.get("mnemonic")
                                or (ch_info.mnemonic if ch_info else "")
                            ).strip()
                            if not mnem:
                                continue
                            mapping = mapping_by_mnemonic.get(mnem.upper())
                            if mapping is None:
                                continue

                            raw_ts = (
                                item.get("timestamp")
                                or (
                                    (item.get("indexes") or [None])[0]
                                    if isinstance(item.get("indexes"), list)
                                    else None
                                )
                            )
                            if raw_ts is None:
                                continue
                            try:
                                item_dt = _parse_iso_utc(raw_ts)
                            except Exception:
                                continue

                            if prev_cursor_dt is not None and item_dt <= prev_cursor_dt:
                                duplicates_suppressed += 1
                                continue

                            dedup_key = (mapping.channel_key, item_dt.isoformat())
                            if dedup_key in seen_keys:
                                duplicates_suppressed += 1
                                continue
                            seen_keys.add(dedup_key)

                            raw_val = item.get("value")
                            if isinstance(raw_val, dict) and "item" in raw_val:
                                raw_val = raw_val["item"]
                            val = None if raw_val is None else float(raw_val)
                            depth_md = item.get("depth_md")
                            quality = str(item.get("quality") or ("missing" if val is None else "good"))
                            unit = (ch_info.unit if ch_info and ch_info.unit else mapping.unit)
                            frames.append(
                                SourceFrame(
                                    channel_key=mapping.channel_key,
                                    ts=item_dt,
                                    value=val,
                                    unit=unit,
                                    quality=quality,
                                    source_point_id=f"etp:{mapping.channel_key}:{item_dt.isoformat()}",
                                    source_ref=f"etp.session:{session_id}:{mnem}",
                                    sequence=len(frames) + 1,
                                    depth_md_si=float(depth_md) if depth_md is not None else None,
                                )
                            )
                            if max_dt is None or item_dt > max_dt:
                                max_dt = item_dt
                            if len(frames) >= max_frames:
                                break
                        if body.get("endOfBatch") is True:
                            break

                    if mtype == "CloseSession":
                        break

                with contextlib.suppress(Exception):
                    await ws.send(
                        json.dumps(
                            {
                                "protocol": 0,
                                "messageType": "CloseSession",
                                "messageId": messages_received + 20,
                                "body": {"reason": "batch complete"},
                            }
                        )
                    )

                next_watermark = dict(current_watermark)
                if max_dt is not None:
                    next_watermark["cursor_timestamp"] = max_dt.isoformat()
                    next_watermark["protocol"] = "etp.1.2.json_ws"
                    next_watermark["last_session_id"] = session_id

                return EtpPollBatch(
                    frames=tuple(frames),
                    discovered_channels=tuple(discovered),
                    next_watermark=next_watermark,
                    session_id=session_id,
                    messages_received=messages_received,
                    duplicates_suppressed=duplicates_suppressed,
                )
        except ConnectorTransportError:
            raise
        except InvalidStatus as exc:
            status = getattr(exc.response, "status_code", 0)
            if status in {401, 403}:
                raise ConnectorTransportError(
                    "auth_failure",
                    f"ETP server rejected handshake with HTTP {status}",
                    retryable=False,
                ) from exc
            raise ConnectorTransportError(
                "protocol_error",
                f"ETP server handshake failed with HTTP {status}",
                retryable=status >= 500,
            ) from exc
        except TimeoutError as exc:
            raise ConnectorTransportError(
                "timeout",
                "ETP WebSocket session timed out",
                retryable=True,
            ) from exc
        except (OSError, WebSocketException) as exc:
            raise ConnectorTransportError(
                "stream_disconnected",
                redact_sensitive_text(f"ETP WebSocket stream disconnected: {exc}", self._secrets)
                or "ETP WebSocket stream disconnected",
                retryable=True,
            ) from exc


# ---------------------------------------------------------------------------
# Local protocol-compatible ETP 1.2 WebSocket server for harness & E2E tests
# ---------------------------------------------------------------------------


@dataclass
class EtpSamplePoint:
    channel_id: int
    mnemonic: str
    timestamp: str
    value: float | None
    depth_md: float | None = None
    quality: str = "good"


@dataclass
class LocalEtpWebSocketServer:
    """In-process local TCP WebSocket server speaking ETP 1.2 JSON framing (Core + ChannelStreaming).

    Supports Bearer/Basic auth checks, ``ChannelMetadata`` advertisement, cursor-filtered
    ``ChannelData`` streaming, ``Ping``/``Pong``, and fault modes (``ok``, ``auth_failure``,
    ``disconnect_mid_stream``, ``malformed_frame``, ``replay_duplicates``).
    """

    host: str = "127.0.0.1"
    port: int = 0
    expected_bearer_token: str | None = None
    expected_username: str | None = None
    expected_password: str | None = None
    channels: list[EtpChannelInfo] = field(
        default_factory=lambda: [
            EtpChannelInfo(1, "SPP", "psi", "Standpipe Pressure", "eml:///witsml20/Channel(spp)"),
            EtpChannelInfo(2, "WOB", "klbf", "Weight on Bit", "eml:///witsml20/Channel(wob)"),
            EtpChannelInfo(3, "HKLD", "klbf", "Hookload", "eml:///witsml20/Channel(hkld)"),
            EtpChannelInfo(4, "RPM", "rpm", "Rotary Speed", "eml:///witsml20/Channel(rpm)"),
            EtpChannelInfo(5, "FLOWIN", "gpm", "Mud Flow In", "eml:///witsml20/Channel(flowin)"),
        ]
    )
    points: list[EtpSamplePoint] = field(default_factory=list)
    fault_mode: str = "ok"
    sessions_opened: int = 0
    last_start_timestamp: str | None = None
    _server: Server | None = field(default=None, init=False, repr=False)

    @property
    def endpoint_url(self) -> str:
        return f"ws://{self.host}:{self.port}/etp"

    def seed_default_points(
        self,
        *,
        start: dt.datetime | None = None,
        count: int = 5,
        step_seconds: float = 5.0,
    ) -> None:
        base = (start or (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=count * step_seconds))).astimezone(dt.UTC)
        seeded: list[EtpSamplePoint] = []
        for idx in range(count):
            ts = (base + dt.timedelta(seconds=idx * step_seconds)).isoformat()
            depth = 2450.0 + idx * 0.5
            seeded.extend(
                [
                    EtpSamplePoint(1, "SPP", ts, 2950.0 + idx * 20.0, depth),
                    EtpSamplePoint(2, "WOB", ts, 22.5 + idx * 0.4, depth),
                    EtpSamplePoint(3, "HKLD", ts, 186.0 + idx * 1.0, depth),
                    EtpSamplePoint(4, "RPM", ts, 122.0 + (idx % 3) * 2.0, depth),
                    EtpSamplePoint(5, "FLOWIN", ts, 655.0 + idx * 4.0, depth),
                ]
            )
        self.points = seeded

    async def start(self) -> str:
        if not self.points:
            self.seed_default_points()
        self._server = await ws_serve(
            self._handle_connection,
            self.host,
            self.port,
            subprotocols=["energistics-tp"],
        )
        sockets = self._server.sockets or []
        if sockets:
            self.port = int(sockets[0].getsockname()[1])
        return self.endpoint_url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> LocalEtpWebSocketServer:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    def _check_auth(self, ws: ServerConnection) -> bool:
        if self.fault_mode == "auth_failure":
            return False
        req_headers = ws.request.headers if ws.request else {}
        auth_header = req_headers.get("Authorization", "")
        if self.expected_bearer_token:
            return auth_header == f"Bearer {self.expected_bearer_token}"
        if self.expected_username is not None and self.expected_password is not None:
            if not auth_header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
            except Exception:
                return False
            return decoded == f"{self.expected_username}:{self.expected_password}"
        return True

    async def _handle_connection(self, ws: ServerConnection) -> None:
        try:
            raw_req = await asyncio.wait_for(ws.recv(), timeout=5.0)
            req = json.loads(raw_req if isinstance(raw_req, str) else raw_req.decode("utf-8"))
            if req.get("messageType") != "RequestSession":
                return

            if not self._check_auth(ws):
                await ws.send(
                    json.dumps(
                        {
                            "protocol": 0,
                            "messageType": "ProtocolException",
                            "messageId": 1,
                            "body": {"code": 401, "message": "ETP authentication failed"},
                        }
                    )
                )
                await ws.close()
                return

            self.sessions_opened += 1
            session_id = f"etp-sess-{self.sessions_opened}"
            await ws.send(
                json.dumps(
                    {
                        "protocol": 0,
                        "messageType": "OpenSession",
                        "messageId": 1,
                        "correlationId": req.get("messageId", 1),
                        "body": {
                            "applicationName": "LocalEtpWebSocketServer",
                            "applicationVersion": "1.2",
                            "sessionId": session_id,
                            "supportedProtocols": [
                                {"protocol": 0, "protocolVersion": {"major": 1, "minor": 2}, "role": "server"},
                                {"protocol": 1, "protocolVersion": {"major": 1, "minor": 2}, "role": "producer"},
                            ],
                        },
                    }
                )
            )

            raw_start = await asyncio.wait_for(ws.recv(), timeout=5.0)
            start_msg = json.loads(raw_start if isinstance(raw_start, str) else raw_start.decode("utf-8"))
            if start_msg.get("messageType") == "CloseSession":
                return
            if start_msg.get("messageType") != "Start":
                return

            body = start_msg.get("body") or {}
            discover_only = bool(body.get("discoverOnly"))
            start_ts_str = body.get("startTimestamp")
            self.last_start_timestamp = str(start_ts_str) if start_ts_str else None
            start_dt = _parse_iso_utc(start_ts_str) if start_ts_str else None

            await ws.send(
                json.dumps(
                    {
                        "protocol": 1,
                        "messageType": "ChannelMetadata",
                        "messageId": 2,
                        "body": {
                            "channels": [
                                {
                                    "channelId": ch.channel_id,
                                    "mnemonic": ch.mnemonic,
                                    "uom": ch.unit,
                                    "channelName": ch.channel_name,
                                    "channelUri": ch.uri,
                                }
                                for ch in self.channels
                            ]
                        },
                    }
                )
            )
            if discover_only:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(ws.recv(), timeout=1.0)
                return

            if self.fault_mode == "malformed_frame":
                await ws.send("{broken-json-frame")
                return

            filtered: list[EtpSamplePoint] = []
            for pt in self.points:
                pt_dt = _parse_iso_utc(pt.timestamp)
                if start_dt is not None and pt_dt <= start_dt and self.fault_mode != "replay_duplicates":
                    continue
                filtered.append(pt)

            if self.fault_mode == "replay_duplicates" and filtered:
                filtered = [filtered[0], *filtered]

            if self.fault_mode == "disconnect_mid_stream":
                half = max(1, len(filtered) // 2)
                await ws.send(
                    json.dumps(
                        {
                            "protocol": 1,
                            "messageType": "ChannelData",
                            "messageId": 3,
                            "body": {
                                "data": [
                                    {
                                        "channelId": pt.channel_id,
                                        "mnemonic": pt.mnemonic,
                                        "timestamp": pt.timestamp,
                                        "value": pt.value,
                                        "depth_md": pt.depth_md,
                                        "quality": pt.quality,
                                    }
                                    for pt in filtered[:half]
                                ],
                                "endOfBatch": False,
                            },
                        }
                    )
                )
                await ws.close(code=1011, reason="simulated transport drop")
                return

            await ws.send(
                json.dumps(
                    {
                        "protocol": 0,
                        "messageType": "Ping",
                        "messageId": 3,
                        "body": {"currentDateTime": dt.datetime.now(dt.UTC).isoformat()},
                    }
                )
            )
            await ws.send(
                json.dumps(
                    {
                        "protocol": 1,
                        "messageType": "ChannelData",
                        "messageId": 4,
                        "body": {
                            "data": [
                                {
                                    "channelId": pt.channel_id,
                                    "mnemonic": pt.mnemonic,
                                    "timestamp": pt.timestamp,
                                    "value": pt.value,
                                    "depth_md": pt.depth_md,
                                    "quality": pt.quality,
                                }
                                for pt in filtered
                            ],
                            "endOfBatch": True,
                        },
                    }
                )
            )
            with contextlib.suppress(Exception):
                for _ in range(2):
                    raw_tail = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    tail_msg = json.loads(
                        raw_tail if isinstance(raw_tail, str) else raw_tail.decode("utf-8")
                    )
                    if tail_msg.get("messageType") == "CloseSession":
                        break
        except Exception:
            pass
