"""ETP 1.2 (Energistics Transfer Protocol) JSON-framed WebSocket client and local test server.

Implements the ``etp.1.2.json_ws`` profile:
- WebSocket subprotocol ``energistics-tp`` with Basic or Bearer authorization
- Supports both canonical ETP v1.2 ``{"header": {...}, "body": {...}}`` Avro-JSON message envelopes
  and flat/hybrid JSON message envelopes across Protocol 0 (Core) and Protocol 1 (ChannelStreaming):
  - ``RequestSession`` / ``OpenSession`` / ``CloseSession``
  - ``StartStreaming`` / ``StopStreaming`` (ETP 1.2 Simple Streaming) and ``Start`` / ``ChannelDescribe`` /
    ``ChannelStreamingStart`` / ``ChannelStreamingStop`` (ETP 1.1 selective streaming compatibility)
  - ``ChannelMetadata`` / ``ChannelData``
  - ``Ping`` / ``Pong`` / ``ProtocolException`` (single ``error`` and ``errors`` map) / ``Acknowledge``
- Per-channel index watermarks (`channel_cursors[channel_key]`) so a fast channel never suppresses
  measurements on a slower channel
- Strict timestamp/index conversion from declared ``IndexMetadataRecord`` (`indexKind`, `uom`) or
  ISO-8601 UTC (rejecting `bool`, `NaN`, `Inf`, depth-only primary indexes, and out-of-range epochs)
- Complete per-item disposition accounting (`ProtocolPollDisposition`)
- ``LocalEtpWebSocketServer``: a real TCP WebSocket server for protocol-level integration and E2E testing
  (explicitly distinct from external commercial vendor certification).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import json
import math
import ssl
from dataclasses import dataclass, field
from typing import Any

import websockets
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedError,
    ConnectionClosedOK,
    InvalidHandshake,
    InvalidStatus,
    WebSocketException,
)

from drillai.telemetry.adapters import SourceFrame
from drillai.telemetry.connectors import (
    ChannelMappingSpec,
    ConnectorTransportError,
    ProtocolPollDisposition,
    enforce_connect_time_ssrf,
    enforce_connect_time_ssrf_async,
    parse_channel_mappings,
    redact_sensitive_text,
    register_harness_endpoint,
    unregister_harness_endpoint,
)

_DEFAULT_ENFORCE_SSRF = enforce_connect_time_ssrf

MAX_ETP_FRAME_BYTES = 1024 * 1024  # 1 MB per WebSocket frame
MIN_VALID_UTC = dt.datetime(1975, 1, 1, tzinfo=dt.UTC)
MAX_VALID_UTC = dt.datetime(2100, 1, 1, tzinfo=dt.UTC)
MAX_RECENT_POINTS_PER_CHANNEL = 500

_EPOCH_UOM_DIVISORS: dict[str, float] = {
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "ms": 1_000.0,
    "msec": 1_000.0,
    "millisecond": 1_000.0,
    "milliseconds": 1_000.0,
    "us": 1_000_000.0,
    "µs": 1_000_000.0,
    "μs": 1_000_000.0,
    "usec": 1_000_000.0,
    "microsecond": 1_000_000.0,
    "microseconds": 1_000_000.0,
    "ns": 1_000_000_000.0,
    "nsec": 1_000_000_000.0,
    "nanosecond": 1_000_000_000.0,
    "nanoseconds": 1_000_000_000.0,
}

_DEPTH_UOMS = {"m", "meter", "meters", "ft", "feet", "ftus"}


def _parse_iso_utc(raw: str, *, require_tz: bool = True) -> dt.datetime:
    if not isinstance(raw, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("empty timestamp")
    if cleaned.endswith("Z") or cleaned.endswith("z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        if require_tz:
            raise ValueError(f"naive timestamp {raw!r} missing explicit timezone offset")
        parsed = parsed.replace(tzinfo=dt.UTC)
    utc_dt = parsed.astimezone(dt.UTC)
    if utc_dt < MIN_VALID_UTC or utc_dt > MAX_VALID_UTC:
        raise ValueError(f"timestamp {raw!r} is outside operational UTC bounds")
    return utc_dt


def _parse_etp_index_timestamp(
    raw: Any,
    *,
    index_kind: str = "DateTime",
    index_uom: str = "us",
) -> tuple[dt.datetime | None, str | None]:
    """Parse an ETP primary index value into a UTC datetime using declared index metadata.

    Returns ``(datetime_utc, error_reason)`` where ``error_reason`` is one of:
    - ``"unsupported_primary_index_kind"``
    - ``"unsupported_numeric_index_uom"``
    - ``"boolean_timestamp_rejected"``
    - ``"non_finite_timestamp"``
    - ``"malformed_timestamp"``
    - ``"timestamp_out_of_range"``
    """
    kind_norm = (index_kind or "DateTime").strip().lower()
    uom_norm = (index_uom or "us").strip().lower()

    if "depth" in kind_norm or uom_norm in _DEPTH_UOMS:
        return None, "unsupported_primary_index_kind"
    if kind_norm not in {
        "datetime",
        "date time",
        "time",
        "timestamp",
        "elapsedtime",
    }:
        return None, "unsupported_primary_index_kind"

    if isinstance(raw, bool):
        return None, "boolean_timestamp_rejected"

    if isinstance(raw, int | float):
        val = float(raw)
        if not math.isfinite(val):
            return None, "non_finite_timestamp"
        divisor = _EPOCH_UOM_DIVISORS.get(uom_norm)
        if divisor is None:
            return None, "unsupported_numeric_index_uom"
        seconds = val / divisor
        try:
            utc_dt = dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
        except (OverflowError, OSError, ValueError):
            return None, "timestamp_out_of_range"
        if utc_dt < MIN_VALID_UTC or utc_dt > MAX_VALID_UTC:
            return None, "timestamp_out_of_range"
        return utc_dt, None

    if isinstance(raw, str):
        try:
            utc_dt = _parse_iso_utc(raw, require_tz=True)
        except ValueError as exc:
            msg = str(exc)
            if "outside operational UTC bounds" in msg:
                return None, "timestamp_out_of_range"
            return None, "malformed_timestamp"
        return utc_dt, None

    return None, "malformed_timestamp"


def _normalize_etp_envelope(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize canonical ETP 1.2 ``{header, body}``, hybrid ``{protocol, messageType, body}``, or flat JSON."""
    if not isinstance(raw, dict):
        raise ConnectorTransportError(
            "malformed_payload",
            "ETP WebSocket message must be a JSON object",
            retryable=False,
        )

    header = raw.get("header")
    body = raw.get("body")
    if isinstance(header, dict) and isinstance(body, dict):
        protocol = header.get("protocol")
        msg_type_num = header.get("messageType")
        corr_id = header.get("correlationId", 0)
        msg_id = header.get("messageId", 0)
        if not isinstance(protocol, int) or isinstance(protocol, bool):
            raise ConnectorTransportError(
                "protocol_error",
                "ETP message header.protocol must be an integer",
                retryable=False,
            )
        if not isinstance(msg_type_num, int) or isinstance(msg_type_num, bool):
            raise ConnectorTransportError(
                "protocol_error",
                "ETP message header.messageType must be an integer",
                retryable=False,
            )
        if (
            not isinstance(corr_id, int)
            or isinstance(corr_id, bool)
            or not isinstance(msg_id, int)
            or isinstance(msg_id, bool)
        ):
            raise ConnectorTransportError(
                "protocol_error",
                "ETP message header.correlationId and messageId must be integers",
                retryable=False,
            )

        inferred_type = str(body.get("type") or raw.get("type") or "").strip()
        if not inferred_type:
            if protocol == 0:
                inferred_type = {
                    1: "RequestSession",
                    2: "OpenSession",
                    5: "CloseSession",
                    6: "Authorize",
                    7: "AuthorizeResponse",
                    8: "Ping",
                    9: "Pong",
                    1000: "ProtocolException",
                    1001: "Acknowledge",
                }.get(msg_type_num, f"Core_{msg_type_num}")
            elif protocol == 1:
                if msg_type_num == 0:
                    inferred_type = "Start"
                elif msg_type_num == 1:
                    inferred_type = "ChannelDescribe" if "uris" in body else "ChannelMetadata"
                elif msg_type_num == 2:
                    inferred_type = "ChannelData" if "data" in body else "ChannelMetadata"
                elif msg_type_num == 3:
                    inferred_type = "ChannelData" if "data" in body else "StartStreaming"
                elif msg_type_num == 4:
                    inferred_type = "ChannelStreamingStart" if "channels" in body else "StopStreaming"
                elif msg_type_num == 5:
                    inferred_type = "ChannelStreamingStop"
                elif msg_type_num == 1000:
                    inferred_type = "ProtocolException"
                elif msg_type_num == 1001:
                    inferred_type = "Acknowledge"
                else:
                    inferred_type = f"ChannelStreaming_{msg_type_num}"
            else:
                if msg_type_num == 1000:
                    inferred_type = "ProtocolException"
                elif msg_type_num == 1001:
                    inferred_type = "Acknowledge"
                else:
                    inferred_type = f"Protocol{protocol}_{msg_type_num}"

        merged = dict(body)
        merged["type"] = inferred_type
        merged["protocol"] = protocol
        merged["messageType"] = msg_type_num
        merged["correlationId"] = corr_id
        merged["messageId"] = msg_id
        merged["_header"] = header
        return merged

    if isinstance(body, dict) and ("messageType" in raw or "type" in raw):
        mtype_val = raw.get("type") or raw.get("messageType")
        merged = dict(body)
        if isinstance(mtype_val, str):
            merged["type"] = mtype_val
        elif isinstance(mtype_val, int) and not isinstance(mtype_val, bool):
            proto = int(raw.get("protocol") or 0)
            return _normalize_etp_envelope(
                {
                    "header": {
                        "protocol": proto,
                        "messageType": mtype_val,
                        "correlationId": int(raw.get("correlationId") or 0),
                        "messageId": int(raw.get("messageId") or 0),
                    },
                    "body": body,
                }
            )
        for k in ("protocol", "messageId", "correlationId"):
            if k in raw:
                merged[k] = raw[k]
        return merged

    flat = dict(raw)
    if "type" not in flat and isinstance(flat.get("messageType"), str):
        flat["type"] = flat["messageType"]
    return flat


def _extract_protocol_exception_text(msg: dict[str, Any]) -> str:
    """Format an ETP ProtocolException supporting single `error`, `errors` map, or flat fields."""
    single_err = msg.get("error")
    if isinstance(single_err, dict):
        code = single_err.get("code", "unknown")
        err_msg = single_err.get("message", "ProtocolException")
        return f"[{code}] {err_msg}"

    errors_map = msg.get("errors")
    if isinstance(errors_map, dict) and errors_map:
        parts: list[str] = []
        for k, v in sorted(errors_map.items(), key=lambda item: str(item[0])):
            if isinstance(v, dict):
                code = v.get("code", "unknown")
                err_msg = v.get("message", "ProtocolException")
                parts.append(f"{k}: [{code}] {err_msg}")
            else:
                parts.append(f"{k}: {v}")
        return "; ".join(parts)

    err_code = msg.get("errorCode") or msg.get("code") or "unknown"
    err_msg = msg.get("errorMessage") or msg.get("message") or "ETP ProtocolException"
    return f"[{err_code}] {err_msg}"


@dataclass(frozen=True)
class EtpIndexMetadata:
    index_kind: str = "DateTime"
    uom: str = "us"
    direction: str = "Increasing"
    mnemonic: str | None = None


@dataclass(frozen=True)
class EtpChannelInfo:
    channel_id: int
    mnemonic: str
    unit: str
    channel_name: str
    uri: str = ""
    data_type: str = "double"
    indexes: tuple[EtpIndexMetadata, ...] = (EtpIndexMetadata(),)

    @property
    def channel_uri(self) -> str:
        return self.uri

    @property
    def uom(self) -> str:
        return self.unit

    @property
    def source_mnemonic(self) -> str:
        return self.mnemonic or self.channel_name or (self.uri.rsplit("/", 1)[-1] if "/" in self.uri else "")

    @property
    def primary_index(self) -> EtpIndexMetadata:
        return self.indexes[0] if self.indexes else EtpIndexMetadata()

    @property
    def secondary_depth_index_pos(self) -> tuple[int, str] | None:
        for idx, meta in enumerate(self.indexes):
            if idx == 0:
                continue
            if "depth" in meta.index_kind.lower() or meta.uom.lower() in _DEPTH_UOMS:
                return idx, meta.uom.lower()
        return None


EtpChannelMetadata = EtpChannelInfo


@dataclass(frozen=True)
class EtpSamplePoint:
    channel_id: int
    mnemonic: str
    timestamp: str
    value: float | None
    depth_md: float | None = 0.0
    quality: str = "good"


@dataclass(frozen=True)
class EtpPollBatch:
    frames: tuple[SourceFrame, ...]
    discovered_channels: tuple[EtpChannelInfo, ...]
    next_watermark: dict[str, Any]
    session_id: str
    messages_received: int
    ping_pong_verified: bool = False
    duplicates_suppressed: int = 0
    disposition: ProtocolPollDisposition = field(default_factory=ProtocolPollDisposition)


def compute_etp_watermark_for_frames(
    previous_watermark: dict[str, Any] | None,
    emitted_frames: tuple[SourceFrame, ...] | list[SourceFrame],
    *,
    session_id: str = "",
    has_more: bool = False,
) -> dict[str, Any]:
    """Advance per-channel ETP watermarks exclusively from frames actually emitted."""
    prev = dict(previous_watermark or {})
    prev_global_ts: dt.datetime | None = None
    if prev.get("cursor_timestamp"):
        try:
            prev_global_ts = _parse_iso_utc(str(prev["cursor_timestamp"]), require_tz=False)
        except ValueError:
            prev_global_ts = None

    raw_cursors = prev.get("channel_cursors")
    channel_cursors: dict[str, dict[str, Any]] = {}
    if isinstance(raw_cursors, dict):
        for ch_k, ch_v in raw_cursors.items():
            if isinstance(ch_v, dict):
                channel_cursors[str(ch_k)] = {
                    "last_timestamp": ch_v.get("last_timestamp"),
                    "recent_points": dict(ch_v.get("recent_points") or {}),
                }

    for frame in emitted_frames:
        ch_state = channel_cursors.setdefault(
            frame.channel_key,
            {
                "last_timestamp": prev_global_ts.isoformat() if prev_global_ts and not raw_cursors else None,
                "recent_points": {},
            },
        )
        cur_dt: dt.datetime | None = None
        if ch_state.get("last_timestamp"):
            try:
                cur_dt = _parse_iso_utc(str(ch_state["last_timestamp"]), require_tz=False)
            except ValueError:
                cur_dt = None
        if cur_dt is None or frame.ts > cur_dt:
            ch_state["last_timestamp"] = frame.ts.isoformat()

        if frame.source_point_id:
            recent_map: dict[str, float | None] = dict(ch_state.get("recent_points") or {})
            recent_map[frame.source_point_id] = frame.value
            if len(recent_map) > MAX_RECENT_POINTS_PER_CHANNEL:
                excess = len(recent_map) - MAX_RECENT_POINTS_PER_CHANNEL
                for old_k in list(recent_map.keys())[:excess]:
                    recent_map.pop(old_k, None)
            ch_state["recent_points"] = recent_map

    all_channel_dts: list[dt.datetime] = []
    for ch_state in channel_cursors.values():
        ts_str = ch_state.get("last_timestamp")
        if ts_str:
            with contextlib.suppress(ValueError):
                all_channel_dts.append(_parse_iso_utc(str(ts_str), require_tz=False))
    if prev_global_ts is not None and not all_channel_dts:
        all_channel_dts.append(prev_global_ts)

    max_global_dt = max(all_channel_dts) if all_channel_dts else None
    min_global_dt = min(all_channel_dts) if all_channel_dts else None

    result: dict[str, Any] = {
        "protocol": "etp.1.2.json_ws",
        "session_id": session_id or str(prev.get("session_id") or ""),
        "cursor_timestamp": max_global_dt.isoformat() if max_global_dt else prev.get("cursor_timestamp"),
        "min_cursor_timestamp": min_global_dt.isoformat()
        if min_global_dt
        else prev.get("min_cursor_timestamp"),
        "channel_cursors": channel_cursors,
        "has_more_deferred": bool(has_more),
    }
    return result


class EtpWebSocketClient:
    """Async ETP 1.2 JSON-framed WebSocket client for Protocol 0 (Core) + Protocol 1 (ChannelStreaming)."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        secrets: dict[str, str] | None = None,
        timeout_seconds: float = 10.0,
        ssl_context: ssl.SSLContext | bool = True,
    ) -> None:
        if ssl_context is False:
            raise ValueError("ssl_context=False is forbidden")
        self._endpoint_url = endpoint_url
        self._secrets = dict(secrets or {})
        self._timeout_seconds = max(1.0, min(timeout_seconds, 30.0))
        self._ssl_context: ssl.SSLContext | bool = (
            ssl_context if isinstance(ssl_context, ssl.SSLContext) else True
        )

    def _ws_connect_kwargs(self, pinned: Any, headers: dict[str, str], close_timeout: float) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "subprotocols": ["energistics-tp"],
            "additional_headers": headers,
            "open_timeout": self._timeout_seconds,
            "close_timeout": close_timeout,
            "max_size": MAX_ETP_FRAME_BYTES,
            "host": pinned.pinned_ip,
            "port": pinned.port,
            "proxy": None,
        }
        if pinned.scheme == "wss":
            kwargs["ssl"] = self._ssl_context
            kwargs["server_hostname"] = pinned.sni_hostname
        return kwargs

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if "bearer_token" in self._secrets:
            headers["Authorization"] = f"Bearer {self._secrets['bearer_token']}"
        elif "username" in self._secrets and "password" in self._secrets:
            raw = f"{self._secrets['username']}:{self._secrets['password']}".encode()
            b64 = base64.b64encode(raw).decode("ascii")
            headers["Authorization"] = f"Basic {b64}"
        elif "api_key" in self._secrets:
            headers["X-API-Key"] = self._secrets["api_key"]
        return headers

    async def _recv_json(self, ws: Any) -> dict[str, Any]:
        raw = await asyncio.wait_for(ws.recv(), timeout=self._timeout_seconds)
        if isinstance(raw, bytes):
            if len(raw) > MAX_ETP_FRAME_BYTES:
                raise ConnectorTransportError(
                    "oversized_payload",
                    f"ETP binary frame exceeds {MAX_ETP_FRAME_BYTES} bytes",
                    retryable=False,
                )
            text = raw.decode("utf-8", errors="strict")
        else:
            if len(raw.encode("utf-8")) > MAX_ETP_FRAME_BYTES:
                raise ConnectorTransportError(
                    "oversized_payload",
                    f"ETP text frame exceeds {MAX_ETP_FRAME_BYTES} bytes",
                    retryable=False,
                )
            text = raw
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConnectorTransportError(
                "malformed_payload",
                f"invalid ETP JSON frame: {exc}",
                retryable=False,
            ) from exc
        return _normalize_etp_envelope(parsed)

    @staticmethod
    def _parse_channel_metadata_record(ch: dict[str, Any]) -> EtpChannelInfo:
        cid_raw = ch.get("channelId", 0)
        if isinstance(cid_raw, bool) or not isinstance(cid_raw, int):
            cid = int(cid_raw)
        else:
            cid = cid_raw
        uri = str(ch.get("channelUri") or ch.get("uri") or "").strip()
        cname = str(ch.get("channelName") or ch.get("mnemonic") or "").strip()
        uom = str(ch.get("uom") or ch.get("unit") or "").strip()
        dtype = str(ch.get("dataType") or "double").strip()
        mnem = str(
            ch.get("mnemonic")
            or ch.get("sourceMnemonic")
            or cname
            or (uri.rsplit("/", 1)[-1] if "/" in uri else "")
        ).strip()

        raw_indexes = ch.get("indexes")
        parsed_indexes: list[EtpIndexMetadata] = []
        if isinstance(raw_indexes, list) and raw_indexes:
            for idx_item in raw_indexes:
                if isinstance(idx_item, dict):
                    ikind = str(idx_item.get("indexKind") or idx_item.get("indexType") or "DateTime").strip()
                    iuom = str(idx_item.get("uom") or "us").strip()
                    idir = str(idx_item.get("direction") or "Increasing").strip()
                    imnem = idx_item.get("mnemonic")
                    parsed_indexes.append(
                        EtpIndexMetadata(
                            index_kind=ikind,
                            uom=iuom,
                            direction=idir,
                            mnemonic=str(imnem).strip() if imnem else None,
                        )
                    )
        if not parsed_indexes:
            parsed_indexes.append(EtpIndexMetadata(index_kind="DateTime", uom="us"))

        return EtpChannelInfo(
            channel_id=cid,
            mnemonic=mnem,
            unit=uom,
            channel_name=cname,
            uri=uri,
            data_type=dtype,
            indexes=tuple(parsed_indexes),
        )

    async def probe_session(self) -> tuple[str, list[EtpChannelInfo]]:
        """Perform ETP ``RequestSession`` -> ``OpenSession`` and ``ChannelDescribe``/``Start`` -> ``ChannelMetadata`` discovery."""
        if enforce_connect_time_ssrf is _DEFAULT_ENFORCE_SSRF:
            pinned = await enforce_connect_time_ssrf_async(
                self._endpoint_url,
                "etp.1.2.json_ws",
                dns_timeout_seconds=min(self._timeout_seconds, 5.0),
            )
        else:
            pinned = enforce_connect_time_ssrf(self._endpoint_url, "etp.1.2.json_ws")
        headers = self._build_headers()
        try:
            async with websockets.connect(
                self._endpoint_url,
                **self._ws_connect_kwargs(pinned, headers, min(self._timeout_seconds, 2.0)),
            ) as ws:
                request_msg_id = 1
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "protocol": 0,
                                "messageType": 1,
                                "correlationId": 0,
                                "messageId": request_msg_id,
                                "messageFlags": 0,
                            },
                            "body": {
                                "type": "RequestSession",
                                "applicationName": "DrillAI Connector Probe",
                                "applicationVersion": "1.2.0",
                                "requestedProtocols": [
                                    {
                                        "protocol": 0,
                                        "protocolVersion": {"major": 1, "minor": 2},
                                        "role": "client",
                                    },
                                    {
                                        "protocol": 1,
                                        "protocolVersion": {"major": 1, "minor": 2},
                                        "role": "consumer",
                                    },
                                ],
                            },
                        }
                    )
                )
                open_msg = await self._recv_json(ws)
                if open_msg.get("type") == "ProtocolException":
                    err_text = _extract_protocol_exception_text(open_msg)
                    if "401" in err_text or "auth" in err_text.lower():
                        raise ConnectorTransportError("auth_failure", err_text, retryable=False)
                    raise ConnectorTransportError("protocol_error", err_text, retryable=False)
                if open_msg.get("type") != "OpenSession":
                    raise ConnectorTransportError(
                        "protocol_error",
                        f"expected ETP OpenSession, got {open_msg.get('type')!r}",
                        retryable=False,
                    )
                session_id = str(open_msg.get("sessionId") or "etp-sess-1")

                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "protocol": 1,
                                "messageType": 1,
                                "correlationId": 0,
                                "messageId": 2,
                                "messageFlags": 0,
                            },
                            "body": {
                                "type": "ChannelDescribe",
                                "uris": ["eml://"],
                                "discoverOnly": True,
                            },
                        }
                    )
                )
                discovered: list[EtpChannelInfo] = []
                with contextlib.suppress(TimeoutError, ConnectionClosedOK):
                    meta_msg = await self._recv_json(ws)
                    if meta_msg.get("type") == "ChannelMetadata":
                        for ch in meta_msg.get("channels") or []:
                            if isinstance(ch, dict):
                                discovered.append(self._parse_channel_metadata_record(ch))

                with contextlib.suppress(Exception):
                    await ws.send(
                        json.dumps(
                            {
                                "header": {
                                    "protocol": 0,
                                    "messageType": 5,
                                    "correlationId": 0,
                                    "messageId": 3,
                                    "messageFlags": 0,
                                },
                                "body": {"type": "CloseSession", "reason": "probe complete"},
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
        except InvalidHandshake as exc:
            raise ConnectorTransportError(
                "protocol_error",
                redact_sensitive_text(f"ETP WebSocket handshake invalid: {exc}", self._secrets)
                or "ETP WebSocket handshake invalid",
                retryable=False,
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
        max_frames: int = 250,
        subscribed_channels: set[str] | None = None,
    ) -> EtpPollBatch:
        """Open an ETP 1.2 WebSocket session, subscribe to mapped channels, and collect a batch."""
        if enforce_connect_time_ssrf is _DEFAULT_ENFORCE_SSRF:
            pinned = await enforce_connect_time_ssrf_async(
                self._endpoint_url,
                "etp.1.2.json_ws",
                dns_timeout_seconds=min(self._timeout_seconds, 5.0),
            )
        else:
            pinned = enforce_connect_time_ssrf(self._endpoint_url, "etp.1.2.json_ws")
        mappings = parse_channel_mappings(config.get("channel_mappings"))
        mapping_by_mnemonic = {m.source_mnemonic.upper(): m for m in mappings}
        mapping_by_uri = {m.channel_uri.lower(): m for m in mappings}
        headers = self._build_headers()
        disposition = ProtocolPollDisposition()

        try:
            async with websockets.connect(
                self._endpoint_url,
                **self._ws_connect_kwargs(pinned, headers, min(self._timeout_seconds, 5.0)),
            ) as ws:
                request_msg_id = 1
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "protocol": 0,
                                "messageType": 1,
                                "correlationId": 0,
                                "messageId": request_msg_id,
                                "messageFlags": 0,
                            },
                            "body": {
                                "type": "RequestSession",
                                "applicationName": "DrillAI Telemetry Worker",
                                "applicationVersion": "1.2.0",
                                "requestedProtocols": [
                                    {
                                        "protocol": 0,
                                        "protocolVersion": {
                                            "major": 1,
                                            "minor": 2,
                                            "revision": 0,
                                            "patch": 0,
                                        },
                                        "role": "client",
                                    },
                                    {
                                        "protocol": 1,
                                        "protocolVersion": {
                                            "major": 1,
                                            "minor": 2,
                                            "revision": 0,
                                            "patch": 0,
                                        },
                                        "role": "consumer",
                                    },
                                ],
                            },
                        }
                    )
                )

                open_msg = await self._recv_json(ws)
                disposition.pages_or_messages_received += 1
                if open_msg.get("type") == "ProtocolException":
                    err_text = _extract_protocol_exception_text(open_msg)
                    if "401" in err_text or "auth" in err_text.lower():
                        raise ConnectorTransportError(
                            "auth_failure",
                            f"ETP authentication failed: {err_text}",
                            retryable=False,
                        )
                    raise ConnectorTransportError(
                        "protocol_error",
                        f"ETP ProtocolException during RequestSession: {err_text}",
                        retryable=False,
                    )
                if open_msg.get("type") != "OpenSession":
                    raise ConnectorTransportError(
                        "protocol_error",
                        f"expected ETP OpenSession, received {open_msg.get('type')!r}",
                        retryable=False,
                    )
                open_corr_id = open_msg.get("correlationId")
                if isinstance(open_corr_id, int) and open_corr_id not in (0, request_msg_id):
                    raise ConnectorTransportError(
                        "protocol_error",
                        f"ETP OpenSession correlationId {open_corr_id} does not match RequestSession messageId {request_msg_id}",
                        retryable=False,
                    )
                session_id = str(open_msg.get("sessionId") or "etp-sess-1")

                # Ping / Pong health verification
                ping_msg_id = 2
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "protocol": 0,
                                "messageType": 8,
                                "correlationId": 0,
                                "messageId": ping_msg_id,
                                "messageFlags": 0,
                            },
                            "body": {
                                "type": "Ping",
                                "currentDateTime": dt.datetime.now(dt.UTC).isoformat(),
                            },
                        }
                    )
                )

                # Protocol 1 Start + ChannelDescribe
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "protocol": 1,
                                "messageType": 0,
                                "correlationId": 0,
                                "messageId": 3,
                                "messageFlags": 0,
                            },
                            "body": {
                                "type": "Start",
                                "maxMessageRate": int(config.get("max_message_rate", 500)),
                                "maxDataItems": int(config.get("max_data_items", max_frames)),
                            },
                        }
                    )
                )
                requested_uris = [m.channel_uri for m in mappings] or ["eml://"]
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "protocol": 1,
                                "messageType": 1,
                                "correlationId": 0,
                                "messageId": 4,
                                "messageFlags": 0,
                            },
                            "body": {
                                "type": "ChannelDescribe",
                                "uris": requested_uris,
                            },
                        }
                    )
                )

                discovered: dict[int, EtpChannelInfo] = {}
                channel_id_to_mapping: dict[int, ChannelMappingSpec] = {}
                frames: list[SourceFrame] = []
                messages_received = 1
                ping_pong_verified = False
                duplicates_suppressed = 0

                prev_wm = dict(watermark or {})
                prev_global_dt: dt.datetime | None = None
                if prev_wm.get("cursor_timestamp"):
                    try:
                        prev_global_dt = _parse_iso_utc(str(prev_wm["cursor_timestamp"]), require_tz=False)
                    except ValueError:
                        prev_global_dt = None

                raw_prev_cursors = prev_wm.get("channel_cursors")
                has_per_channel_cursors = isinstance(raw_prev_cursors, dict) and bool(raw_prev_cursors)
                prev_channel_dt: dict[str, dt.datetime] = {}
                prev_channel_recent: dict[str, dict[str, float | None]] = {}
                if isinstance(raw_prev_cursors, dict):
                    for ch_k, ch_v in raw_prev_cursors.items():
                        if isinstance(ch_v, dict):
                            if ch_v.get("last_timestamp"):
                                with contextlib.suppress(ValueError):
                                    prev_channel_dt[str(ch_k)] = _parse_iso_utc(
                                        str(ch_v["last_timestamp"]), require_tz=False
                                    )
                            if isinstance(ch_v.get("recent_points"), dict):
                                prev_channel_recent[str(ch_k)] = {
                                    str(pk): (float(pv) if pv is not None else None)
                                    for pk, pv in ch_v["recent_points"].items()
                                }

                seen_in_batch: dict[str, tuple[bool, float | None]] = {}
                streaming_started = False

                for _ in range(32):
                    try:
                        msg = await self._recv_json(ws)
                    except (TimeoutError, ConnectionClosedOK):
                        break
                    except ConnectionClosedError as exc:
                        raise ConnectorTransportError(
                            "transport_error",
                            f"ETP stream closed unexpectedly mid-session (code={exc.code}): {exc.reason}",
                            retryable=True,
                        ) from exc

                    messages_received += 1
                    disposition.pages_or_messages_received += 1
                    mtype = str(msg.get("type") or "")

                    if mtype == "Pong":
                        ping_pong_verified = True
                        continue

                    if mtype == "Ping":
                        await ws.send(
                            json.dumps(
                                {
                                    "header": {
                                        "protocol": 0,
                                        "messageType": 9,
                                        "correlationId": int(msg.get("messageId") or 0),
                                        "messageId": messages_received + 10,
                                        "messageFlags": 0,
                                    },
                                    "body": {
                                        "type": "Pong",
                                        "currentDateTime": dt.datetime.now(dt.UTC).isoformat(),
                                    },
                                }
                            )
                        )
                        continue

                    if mtype == "Acknowledge":
                        continue

                    if mtype == "ProtocolException":
                        raise ConnectorTransportError(
                            "protocol_error",
                            f"ETP ProtocolException: {_extract_protocol_exception_text(msg)}",
                            retryable=False,
                        )

                    if mtype == "CloseSession":
                        break

                    if mtype == "ChannelMetadata":
                        raw_channels = msg.get("channels") or []
                        new_cids: list[int] = []
                        for ch in raw_channels:
                            if not isinstance(ch, dict):
                                continue
                            meta = self._parse_channel_metadata_record(ch)
                            discovered[meta.channel_id] = meta
                            new_cids.append(meta.channel_id)
                            matched = (
                                mapping_by_mnemonic.get(meta.source_mnemonic.upper())
                                or mapping_by_mnemonic.get(meta.channel_name.upper())
                                or mapping_by_uri.get(meta.channel_uri.lower())
                            )
                            if matched is not None:
                                channel_id_to_mapping[meta.channel_id] = matched

                        if not streaming_started and new_cids:
                            streaming_started = True
                            channel_specs: list[dict[str, Any]] = []
                            for cid in sorted(discovered):
                                matched_map = channel_id_to_mapping.get(cid)
                                ch_cursor_iso: str | None = None
                                if matched_map is not None:
                                    ch_dt = prev_channel_dt.get(matched_map.channel_key) or (
                                        prev_global_dt if not has_per_channel_cursors else None
                                    )
                                    if ch_dt is not None:
                                        ch_cursor_iso = ch_dt.isoformat()
                                elif prev_global_dt is not None and not has_per_channel_cursors:
                                    ch_cursor_iso = prev_global_dt.isoformat()
                                channel_specs.append(
                                    {
                                        "channelId": cid,
                                        "startIndex": {"item": ch_cursor_iso},
                                    }
                                )
                            await ws.send(
                                json.dumps(
                                    {
                                        "header": {
                                            "protocol": 1,
                                            "messageType": 4,
                                            "correlationId": 0,
                                            "messageId": 5,
                                            "messageFlags": 0,
                                        },
                                        "body": {
                                            "type": "ChannelStreamingStart",
                                            "channels": channel_specs,
                                        },
                                    }
                                )
                            )
                        continue

                    if mtype == "ChannelData":
                        data_items = msg.get("data") or []
                        for item in data_items:
                            if not isinstance(item, dict):
                                disposition.source_records_discovered += 1
                                disposition.rejected_invalid_value += 1
                                disposition.record_channel_outcome("<INVALID>", "rejected")
                                disposition.record_rejection(reason="invalid_data_item_object")
                                continue

                            raw_cid = item.get("channelId", -1)
                            if isinstance(raw_cid, bool) or not isinstance(raw_cid, int):
                                try:
                                    cid = int(raw_cid)
                                except (TypeError, ValueError):
                                    cid = -1
                            else:
                                cid = raw_cid

                            meta = discovered.get(cid)
                            mapping = channel_id_to_mapping.get(cid)
                            if mapping is None and item.get("mnemonic"):
                                mapping = mapping_by_mnemonic.get(str(item["mnemonic"]).strip().upper())

                            source_label = (
                                meta.source_mnemonic
                                if meta is not None and meta.source_mnemonic
                                else (
                                    str(item.get("mnemonic") or "").strip()
                                    or (mapping.source_mnemonic if mapping is not None else f"CH_{cid}")
                                )
                            )
                            disposition.source_records_discovered += 1

                            if meta is None and mapping is None:
                                disposition.rejected_unknown_channel += 1
                                disposition.record_channel_outcome(source_label, "rejected")
                                disposition.record_rejection(
                                    reason="unknown_channel_id",
                                    source_mnemonic=source_label,
                                )
                                continue

                            if mapping is None:
                                disposition.unmapped_skipped += 1
                                disposition.record_channel_outcome(source_label, "unmapped_skipped")
                                continue

                            if subscribed_channels and mapping.channel_key not in subscribed_channels:
                                disposition.unsubscribed_skipped += 1
                                disposition.record_channel_outcome(source_label, "unsubscribed_skipped")
                                continue

                            primary_idx_meta = meta.primary_index if meta is not None else EtpIndexMetadata()
                            raw_indexes_list = item.get("indexes")
                            idx_block = item.get("index")
                            raw_ts: Any = None
                            depth_val: float | None = None

                            if isinstance(raw_indexes_list, list) and raw_indexes_list:
                                first_idx = raw_indexes_list[0]
                                raw_ts = (
                                    first_idx.get("item")
                                    if isinstance(first_idx, dict) and "item" in first_idx
                                    else first_idx
                                )
                                if meta is not None and meta.secondary_depth_index_pos is not None:
                                    sec_pos, sec_uom = meta.secondary_depth_index_pos
                                    if sec_pos < len(raw_indexes_list):
                                        sec_raw = raw_indexes_list[sec_pos]
                                        if isinstance(sec_raw, dict) and "item" in sec_raw:
                                            sec_raw = sec_raw["item"]
                                        if not isinstance(sec_raw, bool) and isinstance(sec_raw, int | float):
                                            sec_f = float(sec_raw)
                                            if math.isfinite(sec_f):
                                                depth_val = (
                                                    sec_f * 0.3048
                                                    if sec_uom in {"ft", "feet", "ftus"}
                                                    else sec_f
                                                )
                            elif isinstance(idx_block, dict):
                                raw_ts = (
                                    idx_block.get("item")
                                    if "item" in idx_block
                                    else idx_block.get("timestamp")
                                )
                                if "depth_md" in idx_block and idx_block["depth_md"] is not None:
                                    raw_d = idx_block["depth_md"]
                                    if not isinstance(raw_d, bool):
                                        try:
                                            d_f = float(raw_d)
                                            if math.isfinite(d_f):
                                                depth_val = d_f
                                        except (TypeError, ValueError):
                                            depth_val = None
                            else:
                                raw_ts = item.get("timestamp")

                            if depth_val is None and item.get("depth_md") is not None:
                                raw_d = item.get("depth_md")
                                if not isinstance(raw_d, bool):
                                    try:
                                        d_f = float(raw_d)
                                        if math.isfinite(d_f):
                                            depth_val = d_f
                                    except (TypeError, ValueError):
                                        depth_val = None

                            item_dt, idx_err = _parse_etp_index_timestamp(
                                raw_ts,
                                index_kind=primary_idx_meta.index_kind,
                                index_uom=primary_idx_meta.uom,
                            )
                            if item_dt is None:
                                if idx_err in {
                                    "unsupported_primary_index_kind",
                                    "unsupported_numeric_index_uom",
                                }:
                                    disposition.rejected_unsupported_index += 1
                                else:
                                    disposition.rejected_malformed_timestamp += 1
                                disposition.record_channel_outcome(source_label, "rejected")
                                disposition.record_rejection(
                                    reason=idx_err or "malformed_timestamp",
                                    source_mnemonic=source_label,
                                    index_repr=str(raw_ts),
                                )
                                continue

                            val_block = item.get("value")
                            raw_val = (
                                val_block.get("item")
                                if isinstance(val_block, dict) and "item" in val_block
                                else val_block
                            )
                            val: float | None
                            if raw_val is None:
                                val = None
                            elif isinstance(raw_val, bool):
                                disposition.rejected_invalid_value += 1
                                disposition.record_channel_outcome(source_label, "rejected")
                                disposition.record_rejection(
                                    reason="boolean_value_rejected",
                                    source_mnemonic=source_label,
                                    index_repr=item_dt.isoformat(),
                                    raw_value=str(raw_val),
                                )
                                continue
                            else:
                                try:
                                    val = float(raw_val)
                                except (TypeError, ValueError):
                                    disposition.rejected_invalid_value += 1
                                    disposition.record_channel_outcome(source_label, "rejected")
                                    disposition.record_rejection(
                                        reason="invalid_numeric_value",
                                        source_mnemonic=source_label,
                                        index_repr=item_dt.isoformat(),
                                        raw_value=str(raw_val),
                                    )
                                    continue
                                if not math.isfinite(val):
                                    disposition.rejected_non_finite_value += 1
                                    disposition.record_channel_outcome(source_label, "rejected")
                                    disposition.record_rejection(
                                        reason="non_finite_value",
                                        source_mnemonic=source_label,
                                        index_repr=item_dt.isoformat(),
                                        raw_value=str(raw_val),
                                    )
                                    continue

                            point_id = f"etp:{mapping.channel_key}:{cid}:{item_dt.isoformat()}"

                            ch_prev_dt = prev_channel_dt.get(mapping.channel_key) or (
                                prev_global_dt if not has_per_channel_cursors else None
                            )
                            ch_recent = prev_channel_recent.get(mapping.channel_key)
                            if ch_recent is not None and point_id in ch_recent:
                                prev_val = ch_recent[point_id]
                                if prev_val == val:
                                    duplicates_suppressed += 1
                                    disposition.watermark_suppressed += 1
                                    disposition.record_channel_outcome(source_label, "watermark_suppressed")
                                    continue
                            elif ch_recent is None and ch_prev_dt is not None and item_dt <= ch_prev_dt:
                                duplicates_suppressed += 1
                                disposition.watermark_suppressed += 1
                                disposition.record_channel_outcome(source_label, "watermark_suppressed")
                                continue

                            prev_in_batch = seen_in_batch.get(point_id)
                            if prev_in_batch is not None and prev_in_batch == (True, val):
                                duplicates_suppressed += 1
                                disposition.in_batch_duplicates += 1
                                disposition.record_channel_outcome(source_label, "in_batch_duplicates")
                                continue

                            if len(frames) >= max_frames:
                                disposition.deferred_over_cap += 1
                                disposition.truncated_by_limit = True
                                disposition.partial_more_available = True
                                disposition.record_channel_outcome(source_label, "deferred_over_cap")
                                continue

                            seen_in_batch[point_id] = (True, val)

                            quality = str(
                                item.get("quality") or ("good" if val is not None else "missing")
                            ).lower()
                            attrs = item.get("valueAttributes") or []
                            if isinstance(attrs, list):
                                for attr in attrs:
                                    if isinstance(attr, dict):
                                        aval = attr.get("attributeValue")
                                        if isinstance(aval, dict) and "item" in aval:
                                            aval = aval["item"]
                                        q_cand = str(aval or attr.get("quality") or "").strip().lower()
                                        if q_cand in {"good", "suspect", "bad", "missing"}:
                                            quality = q_cand

                            source_unit = (meta.uom if meta and meta.uom else "") or mapping.unit
                            frames.append(
                                SourceFrame(
                                    channel_key=mapping.channel_key,
                                    ts=item_dt,
                                    value=val,
                                    unit=source_unit,
                                    quality=quality,
                                    source_point_id=point_id,
                                    source_ref=meta.channel_uri
                                    if meta and meta.channel_uri
                                    else mapping.channel_uri,
                                    sequence=len(frames) + 1,
                                    depth_md_si=depth_val,
                                )
                            )
                            disposition.mapped_frames_emitted += 1
                            disposition.record_channel_outcome(source_label, "emitted")

                        if msg.get("isFinalBatch") is True or msg.get("endOfBatch") is True:
                            with contextlib.suppress(ConnectionClosed):
                                await ws.send(
                                    json.dumps(
                                        {
                                            "header": {
                                                "protocol": 0,
                                                "messageType": 5,
                                                "correlationId": 0,
                                                "messageId": messages_received + 20,
                                                "messageFlags": 0,
                                            },
                                            "body": {
                                                "type": "CloseSession",
                                                "reason": "Batch poll complete",
                                            },
                                        }
                                    )
                                )
                            break

                next_watermark = compute_etp_watermark_for_frames(
                    watermark,
                    frames,
                    session_id=session_id,
                    has_more=disposition.partial_more_available,
                )

                return EtpPollBatch(
                    frames=tuple(frames),
                    discovered_channels=tuple(discovered.values()),
                    next_watermark=next_watermark,
                    session_id=session_id,
                    messages_received=messages_received,
                    ping_pong_verified=ping_pong_verified,
                    duplicates_suppressed=duplicates_suppressed,
                    disposition=disposition,
                )

        except ConnectorTransportError:
            raise
        except InvalidStatus as exc:
            code = getattr(exc.response, "status_code", 0)
            if code in {401, 403}:
                raise ConnectorTransportError(
                    "auth_failure",
                    f"ETP server rejected WebSocket handshake with HTTP {code}",
                    retryable=False,
                ) from exc
            raise ConnectorTransportError(
                "transport_error",
                f"ETP WebSocket handshake failed with HTTP {code}",
                retryable=code >= 500,
            ) from exc
        except InvalidHandshake as exc:
            raise ConnectorTransportError(
                "protocol_error",
                redact_sensitive_text(f"ETP WebSocket handshake invalid: {exc}", self._secrets)
                or "ETP WebSocket handshake invalid",
                retryable=False,
            ) from exc
        except TimeoutError as exc:
            raise ConnectorTransportError(
                "timeout",
                f"ETP WebSocket session timed out: {exc}",
                retryable=True,
            ) from exc
        except (OSError, WebSocketException) as exc:
            raise ConnectorTransportError(
                "connection_refused",
                redact_sensitive_text(f"ETP WebSocket connection error: {exc}", self._secrets)
                or "ETP WebSocket connection error",
                retryable=True,
            ) from exc


# ---------------------------------------------------------------------------
# Local protocol-compatible ETP 1.2 WebSocket server for harness & E2E tests
# ---------------------------------------------------------------------------


@dataclass
class LocalEtpWebSocketServer:
    """In-process local TCP WebSocket server speaking ETP 1.2 JSON framing (`energistics-tp`)."""

    host: str = "127.0.0.1"
    port: int = 0
    expected_username: str | None = None
    expected_password: str | None = None
    expected_bearer_token: str | None = None
    use_header_body_envelope: bool = True
    channels: list[EtpChannelInfo] = field(
        default_factory=lambda: [
            EtpChannelInfo(
                channel_id=1,
                mnemonic="SPP",
                unit="psi",
                channel_name="SPP",
                uri="eml://witsml20/Channel(spp)",
                indexes=(EtpIndexMetadata(index_kind="DateTime", uom="us"),),
            ),
            EtpChannelInfo(
                channel_id=2,
                mnemonic="WOB",
                unit="klbf",
                channel_name="WOB",
                uri="eml://witsml20/Channel(wob)",
                indexes=(EtpIndexMetadata(index_kind="DateTime", uom="us"),),
            ),
            EtpChannelInfo(
                channel_id=3,
                mnemonic="HKLD",
                unit="klbf",
                channel_name="HKLD",
                uri="eml://witsml20/Channel(hkld)",
                indexes=(EtpIndexMetadata(index_kind="DateTime", uom="us"),),
            ),
            EtpChannelInfo(
                channel_id=4,
                mnemonic="RPM",
                unit="rpm",
                channel_name="RPM",
                uri="eml://witsml20/Channel(rpm)",
                indexes=(EtpIndexMetadata(index_kind="DateTime", uom="us"),),
            ),
            EtpChannelInfo(
                channel_id=5,
                mnemonic="FLOWIN",
                unit="gpm",
                channel_name="FLOWIN",
                uri="eml://witsml20/Channel(flowin)",
                indexes=(EtpIndexMetadata(index_kind="DateTime", uom="us"),),
            ),
        ]
    )
    points: list[EtpSamplePoint] = field(default_factory=list)
    data_items: list[dict[str, Any]] = field(default_factory=list)
    fault_mode: str = "ok"
    sessions_opened: int = 0
    last_start_timestamp: str | None = None
    last_start_timestamps_by_channel: dict[int, str] = field(default_factory=dict)
    _server: Any = field(default=None, init=False, repr=False)

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
        base = (start or (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=count * step_seconds))).astimezone(
            dt.UTC
        )
        seeded: list[EtpSamplePoint] = []
        base_values = {1: 2980.0, 2: 23.0, 3: 188.0, 4: 122.0, 5: 660.0}
        deltas = {1: 20.0, 2: 0.6, 3: 1.2, 4: 1.5, 5: 4.0}
        for step_idx in range(count):
            ts_dt = base + dt.timedelta(seconds=step_idx * step_seconds)
            ts_iso = ts_dt.isoformat()
            depth = 2460.0 + step_idx * 0.5
            for ch in self.channels:
                val = base_values.get(ch.channel_id, 100.0) + step_idx * deltas.get(ch.channel_id, 1.0)
                seeded.append(
                    EtpSamplePoint(
                        channel_id=ch.channel_id,
                        mnemonic=ch.mnemonic,
                        timestamp=ts_iso,
                        value=val,
                        depth_md=depth,
                        quality="good",
                    )
                )
        self.points = seeded

    def seed_default_data(
        self,
        *,
        start: dt.datetime | None = None,
        steps: int = 5,
        step_seconds: float = 5.0,
    ) -> None:
        self.seed_default_points(start=start, count=steps, step_seconds=step_seconds)

    async def start(self) -> str:
        if not self.points and not self.data_items:
            self.seed_default_points()
        self._server = await websockets.serve(
            self._handler,
            self.host,
            self.port,
            subprotocols=["energistics-tp"],
            process_request=self._process_request,
        )
        sockets = self._server.sockets or []
        if sockets:
            self.port = int(sockets[0].getsockname()[1])
        register_harness_endpoint(self.host, self.port)
        return self.endpoint_url

    async def stop(self) -> None:
        if self._server is not None:
            unregister_harness_endpoint(self.host, self.port)
            self._server.close()
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
            self._server = None

    async def __aenter__(self) -> LocalEtpWebSocketServer:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    def _process_request(self, connection: Any, request: Any) -> Any:
        if self.fault_mode == "auth_failure":
            return connection.respond(401, "Unauthorized\n")
        headers = request.headers
        auth = headers.get("Authorization", "")
        if self.expected_bearer_token:
            if auth != f"Bearer {self.expected_bearer_token}":
                return connection.respond(401, "Unauthorized\n")
        elif self.expected_username is not None and self.expected_password is not None:
            if not auth.startswith("Basic "):
                return connection.respond(401, "Unauthorized\n")
            try:
                decoded = base64.b64decode(auth[6:].strip()).decode("utf-8")
            except Exception:
                return connection.respond(401, "Unauthorized\n")
            if decoded != f"{self.expected_username}:{self.expected_password}":
                return connection.respond(401, "Unauthorized\n")
        return None

    def _wrap_out(
        self,
        *,
        protocol: int,
        message_type: int,
        correlation_id: int,
        message_id: int,
        body: dict[str, Any],
    ) -> str:
        if self.use_header_body_envelope:
            return json.dumps(
                {
                    "header": {
                        "protocol": protocol,
                        "messageType": message_type,
                        "correlationId": correlation_id,
                        "messageId": message_id,
                        "messageFlags": 0,
                    },
                    "body": body,
                }
            )
        flat = dict(body)
        flat.setdefault("protocol", protocol)
        flat.setdefault("messageType", message_type)
        flat.setdefault("correlationId", correlation_id)
        flat.setdefault("messageId", message_id)
        return json.dumps(flat)

    def _active_items(self) -> list[dict[str, Any]]:
        if self.data_items:
            return list(self.data_items)
        return [
            {
                "channelId": pt.channel_id,
                "mnemonic": pt.mnemonic,
                "indexes": [{"item": pt.timestamp}],
                "index": {"item": pt.timestamp, "depth_md": pt.depth_md},
                "value": {"item": pt.value},
                "valueAttributes": [{"attributeId": 0, "attributeValue": {"item": pt.quality}}],
            }
            for pt in self.points
        ]

    async def _handler(self, ws: Any) -> None:
        server_msg_id = 0
        try:
            async for raw in ws:
                if self.fault_mode in {"malformed_json", "malformed_frame"}:
                    await ws.send("{not-valid-json")
                    return
                if self.fault_mode == "drop_mid_handshake":
                    await ws.close(1011, "Simulated mid-handshake drop")
                    return
                raw_msg = json.loads(raw)
                msg = _normalize_etp_envelope(raw_msg)
                mtype = msg.get("type")
                client_msg_id = int(msg.get("messageId") or 0)

                if mtype == "RequestSession":
                    if self.fault_mode == "protocol_exception":
                        server_msg_id += 1
                        await ws.send(
                            self._wrap_out(
                                protocol=0,
                                message_type=1000,
                                correlation_id=client_msg_id,
                                message_id=server_msg_id,
                                body={
                                    "type": "ProtocolException",
                                    "error": {
                                        "code": 4,
                                        "message": "Simulated ETP session exception",
                                    },
                                },
                            )
                        )
                        return
                    if self.fault_mode == "protocol_exception_map":
                        server_msg_id += 1
                        await ws.send(
                            self._wrap_out(
                                protocol=0,
                                message_type=1000,
                                correlation_id=client_msg_id,
                                message_id=server_msg_id,
                                body={
                                    "type": "ProtocolException",
                                    "errors": {
                                        "0": {
                                            "code": 11,
                                            "message": "Simulated ETP map exception",
                                        }
                                    },
                                },
                            )
                        )
                        return
                    self.sessions_opened += 1
                    server_msg_id += 1
                    await ws.send(
                        self._wrap_out(
                            protocol=0,
                            message_type=2,
                            correlation_id=client_msg_id,
                            message_id=server_msg_id,
                            body={
                                "type": "OpenSession",
                                "applicationName": "DrillAI Local ETP 1.2 Server",
                                "applicationVersion": "1.2.0",
                                "sessionId": f"etp-sess-{self.sessions_opened}",
                                "supportedProtocols": [
                                    {"protocol": 0, "role": "server"},
                                    {"protocol": 1, "role": "producer"},
                                ],
                            },
                        )
                    )
                elif mtype == "Ping":
                    server_msg_id += 1
                    await ws.send(
                        self._wrap_out(
                            protocol=0,
                            message_type=9,
                            correlation_id=client_msg_id,
                            message_id=server_msg_id,
                            body={
                                "type": "Pong",
                                "currentDateTime": dt.datetime.now(dt.UTC).isoformat(),
                            },
                        )
                    )
                elif mtype == "ChannelDescribe":
                    server_msg_id += 1
                    await ws.send(
                        self._wrap_out(
                            protocol=1,
                            message_type=1,
                            correlation_id=client_msg_id,
                            message_id=server_msg_id,
                            body={
                                "type": "ChannelMetadata",
                                "channels": [
                                    {
                                        "channelId": ch.channel_id,
                                        "channelUri": ch.channel_uri,
                                        "channelName": ch.channel_name,
                                        "mnemonic": ch.mnemonic,
                                        "uom": ch.uom,
                                        "dataType": ch.data_type,
                                        "sourceMnemonic": ch.source_mnemonic,
                                        "indexes": [
                                            {
                                                "indexKind": idx.index_kind,
                                                "uom": idx.uom,
                                                "direction": idx.direction,
                                                "mnemonic": idx.mnemonic,
                                            }
                                            for idx in ch.indexes
                                        ],
                                    }
                                    for ch in self.channels
                                ],
                            },
                        )
                    )
                elif mtype in {"ChannelStreamingStart", "StartStreaming"}:
                    if mtype == "StartStreaming":
                        server_msg_id += 1
                        await ws.send(
                            self._wrap_out(
                                protocol=1,
                                message_type=1,
                                correlation_id=client_msg_id,
                                message_id=server_msg_id,
                                body={
                                    "type": "ChannelMetadata",
                                    "channels": [
                                        {
                                            "channelId": ch.channel_id,
                                            "channelUri": ch.channel_uri,
                                            "channelName": ch.channel_name,
                                            "mnemonic": ch.mnemonic,
                                            "uom": ch.uom,
                                            "dataType": ch.data_type,
                                            "sourceMnemonic": ch.source_mnemonic,
                                            "indexes": [
                                                {
                                                    "indexKind": idx.index_kind,
                                                    "uom": idx.uom,
                                                    "direction": idx.direction,
                                                    "mnemonic": idx.mnemonic,
                                                }
                                                for idx in ch.indexes
                                            ],
                                        }
                                        for ch in self.channels
                                    ],
                                },
                            )
                        )

                    if self.fault_mode in {"drop_mid_stream", "disconnect_mid_stream"}:
                        await ws.close(1011, "Simulated mid-stream drop")
                        return

                    start_cutoff_by_channel: dict[int, dt.datetime] = {}
                    for ch_spec in msg.get("channels", []):
                        if not isinstance(ch_spec, dict):
                            continue
                        cid_val = int(ch_spec.get("channelId", -1))
                        raw_idx = (
                            ch_spec.get("startIndex", {}).get("item")
                            if isinstance(ch_spec.get("startIndex"), dict)
                            else None
                        )
                        if raw_idx:
                            self.last_start_timestamp = str(raw_idx)
                            self.last_start_timestamps_by_channel[cid_val] = str(raw_idx)
                            with contextlib.suppress(ValueError):
                                start_cutoff_by_channel[cid_val] = _parse_iso_utc(
                                    str(raw_idx), require_tz=False
                                )

                    ch_by_id = {ch.channel_id: ch for ch in self.channels}
                    filtered_items: list[dict[str, Any]] = []
                    for item in self._active_items():
                        cid_val = int(item.get("channelId", -1))
                        cutoff = start_cutoff_by_channel.get(cid_val)
                        if cutoff is not None and self.fault_mode != "replay_duplicates":
                            ch_meta = ch_by_id.get(cid_val)
                            p_idx = ch_meta.primary_index if ch_meta is not None else EtpIndexMetadata()
                            raw_item_ts = None
                            if isinstance(item.get("indexes"), list) and item["indexes"]:
                                f0 = item["indexes"][0]
                                raw_item_ts = f0.get("item") if isinstance(f0, dict) and "item" in f0 else f0
                            elif isinstance(item.get("index"), dict):
                                raw_item_ts = item["index"].get("item")
                            else:
                                raw_item_ts = item.get("timestamp")
                            item_dt, _ = _parse_etp_index_timestamp(
                                raw_item_ts,
                                index_kind=p_idx.index_kind,
                                index_uom=p_idx.uom,
                            )
                            if item_dt is not None and item_dt <= cutoff:
                                continue
                        filtered_items.append(item)

                    if self.fault_mode == "replay_duplicates" and filtered_items:
                        filtered_items = [filtered_items[0], *filtered_items]

                    server_msg_id += 1
                    await ws.send(
                        self._wrap_out(
                            protocol=1,
                            message_type=2,
                            correlation_id=client_msg_id,
                            message_id=server_msg_id,
                            body={
                                "type": "ChannelData",
                                "data": filtered_items,
                                "isFinalBatch": True,
                            },
                        )
                    )
                elif mtype == "CloseSession":
                    break
        except ConnectionClosed:
            pass
