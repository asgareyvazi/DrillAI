"""Production ETP 1.2 (Energistics Transfer Protocol v1.2) Binary Avro WebSocket client,
schema-driven Avro binary codec, local non-production test harness server, and adapter.

Implements the ``etp.1.2.binary_ws`` production profile:
- WebSocket subprotocol ``energistics-tp`` with strict binary WebSocket frames (`OPCODE_BINARY`)
- Connection-bound SSRF protection and socket IP pinning (`enforce_connect_time_ssrf`)
- Exact Energistics ETP v1.2 Avro binary datum encoding (`MessageHeader` + protocol message body
  concatenated in a single binary WebSocket frame, zigzag varints, IEEE 754 floats/doubles,
  fixed(16) `Uuid`, block-encoded arrays/maps, union branch indexes)
- Core Protocol 0:
  - ``RequestSession`` (msgType=1)
  - ``OpenSession`` (msgType=2)
  - ``CloseSession`` (msgType=5)
  - ``Ping`` (msgType=8) / ``Pong`` (msgType=9)
  - ``ProtocolException`` (msgType=1000, both ``error`` and ``errors`` map)
  - ``Acknowledge`` (msgType=1001)
- ChannelStreaming Protocol 1:
  - ``ChannelMetadata`` (msgType=1, including multipart flags `0x01`/`0x02` and mid-stream updates)
  - ``ChannelData`` (msgType=2, streaming `DataItem` records with `indexes`, `value`, `valueAttributes`)
  - ``StartStreaming`` (msgType=3)
  - ``StopStreaming`` (msgType=4)
  - ``TruncateChannels`` (msgType=5)
- Per-channel watermarks (`channel_cursors[channel_key]`) keyed by canonical channel key / URI so
  session-scoped numeric `channelId` reassignment across reconnects never corrupts channel state
- Explicit primary time index and optional secondary depth index semantics (`IndexMetadataRecord`)
- Complete per-item disposition accounting (`ProtocolPollDisposition`).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime as dt
import math
import ssl
import struct
import uuid
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
from drillai.telemetry.protocols.etp import (
    MAX_ETP_FRAME_BYTES,
    EtpChannelInfo,
    EtpIndexMetadata,
    EtpPollBatch,
    EtpSamplePoint,
    _parse_etp_index_timestamp,
    _parse_iso_utc,
    compute_etp_watermark_for_frames,
)

_DEFAULT_ENFORCE_SSRF = enforce_connect_time_ssrf

# ETP v1.2 MessageHeader flags
MSG_FLAG_NONE = 0x00
MSG_FLAG_MULTIPART = 0x01
MSG_FLAG_FINALPART = 0x02
MSG_FLAG_MULTIPART_AND_FINAL = 0x03
MSG_FLAG_NO_DATA = 0x04
MSG_FLAG_COMPRESSED = 0x08
MSG_FLAG_ACKNOWLEDGE = 0x10

# ETP v1.2 Enumerations (exact symbol ordering from Energistics.Etp.v12.Datatypes.ChannelData)
CHANNEL_INDEX_KINDS: tuple[str, ...] = (
    "DateTime",
    "ElapsedTime",
    "MeasuredDepth",
    "TrueVerticalDepth",
    "PassIndexedDepth",
    "Pressure",
    "Temperature",
    "Scalar",
)
INDEX_DIRECTIONS: tuple[str, ...] = ("Increasing", "Decreasing", "Unordered")
CHANNEL_DATA_KINDS: tuple[str, ...] = (
    "DateTime",
    "ElapsedTime",
    "MeasuredDepth",
    "PassIndexedDepth",
    "TrueVerticalDepth",
    "typeBoolean",
    "typeInt",
    "typeLong",
    "typeFloat",
    "typeDouble",
    "typeString",
    "typeBytes",
)
ACTIVE_STATUS_KINDS: tuple[str, ...] = ("Active", "Inactive")
PASS_DIRECTIONS: tuple[str, ...] = ("Up", "HoldingSteady", "Down")

MAX_AVRO_STRING_BYTES = 65_536
MAX_AVRO_ARRAY_ITEMS = 10_000


class AvroBinaryDecodeError(ValueError):
    """Raised when an ETP 1.2 binary Avro frame violates schema or wire bounds."""


class AvroBinaryWriter:
    """Deterministic Apache Avro binary datum encoder for ETP v1.2 records."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def to_bytes(self) -> bytes:
        return bytes(self._buf)

    def write_boolean(self, val: bool) -> None:
        self._buf.append(1 if val else 0)

    def write_int(self, val: int) -> None:
        if isinstance(val, bool) or not (-2_147_483_648 <= int(val) <= 2_147_483_647):
            raise ValueError(f"Avro int out of 32-bit signed range: {val!r}")
        self.write_long(int(val))

    def write_long(self, val: int) -> None:
        if isinstance(val, bool) or not (-9_223_372_036_854_775_808 <= int(val) <= 9_223_372_036_854_775_807):
            raise ValueError(f"Avro long out of 64-bit signed range: {val!r}")
        n = int(val)
        zz = (n << 1) ^ (n >> 63)
        while (zz & ~0x7F) != 0:
            self._buf.append((zz & 0x7F) | 0x80)
            zz >>= 7
        self._buf.append(zz & 0x7F)

    def write_float(self, val: float) -> None:
        self._buf.extend(struct.pack("<f", float(val)))

    def write_double(self, val: float) -> None:
        self._buf.extend(struct.pack("<d", float(val)))

    def write_bytes(self, val: bytes) -> None:
        self.write_long(len(val))
        self._buf.extend(val)

    def write_string(self, val: str) -> None:
        raw = str(val).encode("utf-8")
        self.write_bytes(raw)

    def write_fixed(self, val: bytes, size: int = 16) -> None:
        if len(val) != size:
            raise ValueError(f"Avro fixed({size}) requires {size} bytes, got {len(val)}")
        self._buf.extend(val)

    def write_uuid(self, val: uuid.UUID | str | bytes) -> None:
        if isinstance(val, uuid.UUID):
            self.write_fixed(val.bytes, 16)
        elif isinstance(val, bytes) and len(val) == 16:
            self.write_fixed(val, 16)
        else:
            try:
                self.write_fixed(uuid.UUID(str(val)).bytes, 16)
            except ValueError:
                digest = uuid.uuid5(uuid.NAMESPACE_OID, str(val)).bytes
                self.write_fixed(digest, 16)

    def write_enum(self, symbol: str | int, symbols: tuple[str, ...]) -> None:
        if isinstance(symbol, int) and not isinstance(symbol, bool):
            if 0 <= symbol < len(symbols):
                self.write_int(symbol)
                return
            raise ValueError(f"enum index {symbol} out of range for {symbols}")
        s_clean = str(symbol)
        for idx, candidate in enumerate(symbols):
            if candidate.lower() == s_clean.lower():
                self.write_int(idx)
                return
        raise ValueError(f"unknown enum symbol {symbol!r} for {symbols}")


class AvroBinaryReader:
    """Bounds-checked Apache Avro binary datum decoder for ETP v1.2 records."""

    def __init__(self, data: bytes) -> None:
        if len(data) > MAX_ETP_FRAME_BYTES:
            raise AvroBinaryDecodeError(
                f"ETP binary frame of {len(data)} bytes exceeds maximum {MAX_ETP_FRAME_BYTES} bytes"
            )
        self._data = memoryview(data)
        self._pos = 0
        self._len = len(data)

    @property
    def remaining(self) -> int:
        return self._len - self._pos

    def read_raw(self, count: int) -> bytes:
        if count < 0 or self._pos + count > self._len:
            raise AvroBinaryDecodeError(
                f"truncated ETP binary frame at offset {self._pos}: needed {count} bytes, {self.remaining} left"
            )
        chunk = bytes(self._data[self._pos : self._pos + count])
        self._pos += count
        return chunk

    def read_boolean(self) -> bool:
        b = self.read_raw(1)[0]
        if b not in (0, 1):
            raise AvroBinaryDecodeError(f"invalid Avro boolean byte 0x{b:02x} at offset {self._pos - 1}")
        return b == 1

    def read_long(self, *, max_bytes: int = 10) -> int:
        shift = 0
        zz = 0
        bytes_read = 0
        while True:
            if self._pos >= self._len:
                raise AvroBinaryDecodeError(f"truncated varint at offset {self._pos}")
            b = self._data[self._pos]
            self._pos += 1
            bytes_read += 1
            if bytes_read > max_bytes:
                raise AvroBinaryDecodeError(f"varint exceeds {max_bytes} bytes at offset {self._pos}")
            zz |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
        val = (zz >> 1) ^ -(zz & 1)
        if not (-9_223_372_036_854_775_808 <= val <= 9_223_372_036_854_775_807):
            raise AvroBinaryDecodeError(f"decoded varint {val} overflows 64-bit signed integer")
        return val

    def read_int(self) -> int:
        val = self.read_long(max_bytes=5)
        if not (-2_147_483_648 <= val <= 2_147_483_647):
            raise AvroBinaryDecodeError(f"decoded int {val} overflows 32-bit signed integer")
        return int(val)

    def read_float(self) -> float:
        return float(struct.unpack("<f", self.read_raw(4))[0])

    def read_double(self) -> float:
        return float(struct.unpack("<d", self.read_raw(8))[0])

    def read_bytes(self, *, max_len: int = MAX_ETP_FRAME_BYTES) -> bytes:
        length = self.read_long()
        if length < 0 or length > max_len:
            raise AvroBinaryDecodeError(f"invalid Avro bytes length {length} (max {max_len})")
        return self.read_raw(length)

    def read_string(self, *, max_len: int = MAX_AVRO_STRING_BYTES) -> str:
        raw = self.read_bytes(max_len=max_len)
        try:
            return raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise AvroBinaryDecodeError(f"invalid UTF-8 string in ETP binary frame: {exc}") from exc

    def read_fixed(self, size: int = 16) -> bytes:
        return self.read_raw(size)

    def read_uuid_str(self) -> str:
        raw = self.read_fixed(16)
        return str(uuid.UUID(bytes=raw))

    def read_enum(self, symbols: tuple[str, ...]) -> str:
        idx = self.read_int()
        if idx < 0 or idx >= len(symbols):
            raise AvroBinaryDecodeError(f"invalid Avro enum symbol index {idx} for {symbols}")
        return symbols[idx]

    def read_array(self, item_reader: Any) -> list[Any]:
        items: list[Any] = []
        while True:
            block_count = self.read_long()
            if block_count == 0:
                break
            if block_count < 0:
                block_count = -block_count
                block_byte_size = self.read_long()
                if block_byte_size < 0 or block_byte_size > self.remaining:
                    raise AvroBinaryDecodeError(f"invalid negative array block byte size {block_byte_size}")
            if len(items) + block_count > MAX_AVRO_ARRAY_ITEMS:
                raise AvroBinaryDecodeError(
                    f"Avro array item count {len(items) + block_count} exceeds max {MAX_AVRO_ARRAY_ITEMS}"
                )
            for _ in range(block_count):
                items.append(item_reader(self))
        return items

    def read_map(self, value_reader: Any) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while True:
            block_count = self.read_long()
            if block_count == 0:
                break
            if block_count < 0:
                block_count = -block_count
                block_byte_size = self.read_long()
                if block_byte_size < 0 or block_byte_size > self.remaining:
                    raise AvroBinaryDecodeError(f"invalid negative map block byte size {block_byte_size}")
            if len(out) + block_count > MAX_AVRO_ARRAY_ITEMS:
                raise AvroBinaryDecodeError(
                    f"Avro map entry count {len(out) + block_count} exceeds max {MAX_AVRO_ARRAY_ITEMS}"
                )
            for _ in range(block_count):
                k = self.read_string()
                out[k] = value_reader(self)
        return out


# ---------------------------------------------------------------------------
# ETP v1.2 Datatype Encoders & Decoders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EtpMessageHeader:
    """ETP v1.2 `Energistics.Etp.v12.Datatypes.MessageHeader`."""

    protocol: int
    message_type: int
    correlation_id: int = 0
    message_id: int = 1
    message_flags: int = 0

    def encode(self, w: AvroBinaryWriter) -> None:
        w.write_int(self.protocol)
        w.write_int(self.message_type)
        w.write_long(self.correlation_id)
        w.write_long(self.message_id)
        w.write_int(self.message_flags)

    @classmethod
    def decode(cls, r: AvroBinaryReader) -> EtpMessageHeader:
        return cls(
            protocol=r.read_int(),
            message_type=r.read_int(),
            correlation_id=r.read_long(),
            message_id=r.read_long(),
            message_flags=r.read_int(),
        )


def encode_data_value(w: AvroBinaryWriter, val: Any) -> None:
    """Encode an ETP v1.2 `DataValue` union (`[null, boolean, int, long, float, double, string, ... , bytes, AnySparseArray]`)."""
    if isinstance(val, dict) and "item" in val and len(val) == 1:
        val = val["item"]
    if val is None:
        w.write_long(0)
    elif isinstance(val, bool):
        w.write_long(1)
        w.write_boolean(val)
    elif isinstance(val, int):
        if -2_147_483_648 <= val <= 2_147_483_647:
            w.write_long(2)
            w.write_int(val)
        else:
            w.write_long(3)
            w.write_long(val)
    elif isinstance(val, float):
        w.write_long(5)
        w.write_double(val)
    elif isinstance(val, str):
        w.write_long(6)
        w.write_string(val)
    elif isinstance(val, bytes):
        w.write_long(17)
        w.write_bytes(val)
    elif isinstance(val, (list, tuple)):
        if all(isinstance(x, bool) for x in val):
            w.write_long(7)
            _write_array(w, val, lambda bw, x: bw.write_boolean(bool(x)))
        elif all(isinstance(x, int) and not isinstance(x, bool) for x in val):
            w.write_long(11)
            _write_array(w, val, lambda bw, x: bw.write_long(int(x)))
        elif all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in val):
            w.write_long(14)
            _write_array(w, val, lambda bw, x: bw.write_double(float(x)))
        else:
            w.write_long(15)
            _write_array(w, val, lambda bw, x: bw.write_string(str(x)))
    else:
        raise ValueError(f"unsupported ETP DataValue type: {type(val).__name__}")


def decode_data_value(r: AvroBinaryReader) -> Any:
    """Decode an ETP v1.2 `DataValue` union."""
    branch = r.read_long()
    if branch == 0:
        return None
    if branch == 1:
        return r.read_boolean()
    if branch == 2:
        return r.read_int()
    if branch == 3:
        return r.read_long()
    if branch == 4:
        return r.read_float()
    if branch == 5:
        return r.read_double()
    if branch == 6:
        return r.read_string()
    if branch == 7:
        return r.read_array(lambda br: br.read_boolean())
    if branch == 8:
        return r.read_array(lambda br: None if br.read_long() == 0 else br.read_boolean())
    if branch == 9:
        return r.read_array(lambda br: br.read_int())
    if branch == 10:
        return r.read_array(lambda br: None if br.read_long() == 0 else br.read_int())
    if branch == 11:
        return r.read_array(lambda br: br.read_long())
    if branch == 12:
        return r.read_array(lambda br: None if br.read_long() == 0 else br.read_long())
    if branch == 13:
        return r.read_array(lambda br: br.read_float())
    if branch == 14:
        return r.read_array(lambda br: br.read_double())
    if branch == 15:
        return r.read_array(lambda br: br.read_string())
    if branch == 16:
        return r.read_array(lambda br: br.read_bytes())
    if branch == 17:
        return r.read_bytes()
    raise AvroBinaryDecodeError(f"invalid or unsupported DataValue union branch {branch}")


def encode_index_value(w: AvroBinaryWriter, val: Any) -> None:
    """Encode an ETP v1.2 `IndexValue` union (`[null, long, double, PassIndexedDepth]`)."""
    if isinstance(val, dict) and "item" in val:
        val = val["item"]
    if val is None:
        w.write_long(0)
    elif isinstance(val, bool):
        raise ValueError("IndexValue cannot be boolean")
    elif isinstance(val, int):
        w.write_long(1)
        w.write_long(val)
    elif isinstance(val, float):
        w.write_long(2)
        w.write_double(val)
    elif isinstance(val, dict) and "depth" in val:
        w.write_long(3)
        w.write_long(int(val.get("pass", 0)))
        w.write_enum(str(val.get("direction", "HoldingSteady")), PASS_DIRECTIONS)
        w.write_double(float(val["depth"]))
    else:
        raise ValueError(f"unsupported ETP IndexValue: {val!r}")


def decode_index_value(r: AvroBinaryReader) -> Any:
    """Decode an ETP v1.2 `IndexValue` union (`[null, long, double, PassIndexedDepth]`)."""
    branch = r.read_long()
    if branch == 0:
        return None
    if branch == 1:
        return r.read_long()
    if branch == 2:
        return r.read_double()
    if branch == 3:
        pass_num = r.read_long()
        direction = r.read_enum(PASS_DIRECTIONS)
        depth = r.read_double()
        return {"pass": pass_num, "direction": direction, "depth": depth}
    raise AvroBinaryDecodeError(f"invalid IndexValue union branch {branch}")


def _write_array(w: AvroBinaryWriter, items: list[Any] | tuple[Any, ...], writer_fn: Any) -> None:
    if items:
        w.write_long(len(items))
        for item in items:
            writer_fn(w, item)
    w.write_long(0)


def _write_map(w: AvroBinaryWriter, mapping: dict[str, Any], writer_fn: Any) -> None:
    if mapping:
        w.write_long(len(mapping))
        for k, v in mapping.items():
            w.write_string(str(k))
            writer_fn(w, v)
    w.write_long(0)


def _encode_supported_protocol(w: AvroBinaryWriter, sp: dict[str, Any]) -> None:
    w.write_int(int(sp.get("protocol", 0)))
    ver = sp.get("protocolVersion") or {}
    w.write_int(int(ver.get("major", 1)))
    w.write_int(int(ver.get("minor", 2)))
    w.write_int(int(ver.get("revision", 0)))
    w.write_int(int(ver.get("patch", 0)))
    w.write_string(str(sp.get("role", "client")))
    _write_map(w, dict(sp.get("protocolCapabilities") or {}), encode_data_value)


def _decode_supported_protocol(r: AvroBinaryReader) -> dict[str, Any]:
    return {
        "protocol": r.read_int(),
        "protocolVersion": {
            "major": r.read_int(),
            "minor": r.read_int(),
            "revision": r.read_int(),
            "patch": r.read_int(),
        },
        "role": r.read_string(),
        "protocolCapabilities": r.read_map(decode_data_value),
    }


def _encode_supported_data_object(w: AvroBinaryWriter, sdo: dict[str, Any]) -> None:
    w.write_string(str(sdo.get("qualifiedType", "witsml20.Channel")))
    _write_map(w, dict(sdo.get("dataObjectCapabilities") or {}), encode_data_value)


def _decode_supported_data_object(r: AvroBinaryReader) -> dict[str, Any]:
    return {
        "qualifiedType": r.read_string(),
        "dataObjectCapabilities": r.read_map(decode_data_value),
    }


# ---------------------------------------------------------------------------
# ETP v1.2 Frame Builders & Parser (Protocol 0 & Protocol 1)
# ---------------------------------------------------------------------------


def build_request_session_frame(
    *,
    message_id: int = 1,
    application_name: str = "DrillAI Telemetry Worker",
    application_version: str = "1.2.0",
    client_instance_id: uuid.UUID | None = None,
    requested_protocols: list[dict[str, Any]] | None = None,
    supported_data_objects: list[dict[str, Any]] | None = None,
    current_date_time_us: int | None = None,
    message_flags: int = 0,
) -> bytes:
    """Encode ETP v1.2 `Core.RequestSession` (protocol=0, messageType=1)."""
    w = AvroBinaryWriter()
    EtpMessageHeader(
        protocol=0,
        message_type=1,
        correlation_id=0,
        message_id=message_id,
        message_flags=message_flags,
    ).encode(w)
    now_us = (
        current_date_time_us
        if current_date_time_us is not None
        else int(dt.datetime.now(dt.UTC).timestamp() * 1_000_000)
    )
    protos = requested_protocols or [
        {
            "protocol": 0,
            "protocolVersion": {"major": 1, "minor": 2, "revision": 0, "patch": 0},
            "role": "client",
            "protocolCapabilities": {},
        },
        {
            "protocol": 1,
            "protocolVersion": {"major": 1, "minor": 2, "revision": 0, "patch": 0},
            "role": "consumer",
            "protocolCapabilities": {},
        },
    ]
    data_objs = supported_data_objects or [
        {"qualifiedType": "witsml20.Channel", "dataObjectCapabilities": {}},
        {"qualifiedType": "witsml20.ChannelSet", "dataObjectCapabilities": {}},
    ]
    w.write_string(application_name)
    w.write_string(application_version)
    w.write_uuid(client_instance_id or uuid.uuid4())
    _write_array(w, protos, _encode_supported_protocol)
    _write_array(w, data_objs, _encode_supported_data_object)
    _write_array(w, [], lambda bw, s: bw.write_string(str(s)))  # supportedCompression
    _write_array(w, ["xml"], lambda bw, s: bw.write_string(str(s)))  # supportedFormats
    w.write_long(now_us)
    w.write_long(now_us)
    w.write_boolean(False)  # serverAuthorizationRequired
    _write_map(w, {}, encode_data_value)  # endpointCapabilities
    return w.to_bytes()


def build_open_session_frame(
    *,
    correlation_id: int = 1,
    message_id: int = 1,
    session_id: uuid.UUID | str | None = None,
    server_instance_id: uuid.UUID | str | None = None,
    application_name: str = "DrillAI ETP 1.2 Binary Store",
    application_version: str = "1.2.0",
    supported_protocols: list[dict[str, Any]] | None = None,
    supported_data_objects: list[dict[str, Any]] | None = None,
    current_date_time_us: int | None = None,
    message_flags: int = 0,
) -> bytes:
    """Encode ETP v1.2 `Core.OpenSession` (protocol=0, messageType=2)."""
    w = AvroBinaryWriter()
    EtpMessageHeader(
        protocol=0,
        message_type=2,
        correlation_id=correlation_id,
        message_id=message_id,
        message_flags=message_flags,
    ).encode(w)
    now_us = (
        current_date_time_us
        if current_date_time_us is not None
        else int(dt.datetime.now(dt.UTC).timestamp() * 1_000_000)
    )
    protos = (
        supported_protocols
        if supported_protocols is not None
        else [
            {
                "protocol": 0,
                "protocolVersion": {"major": 1, "minor": 2, "revision": 0, "patch": 0},
                "role": "server",
                "protocolCapabilities": {},
            },
            {
                "protocol": 1,
                "protocolVersion": {"major": 1, "minor": 2, "revision": 0, "patch": 0},
                "role": "producer",
                "protocolCapabilities": {},
            },
        ]
    )
    data_objs = (
        supported_data_objects
        if supported_data_objects is not None
        else [{"qualifiedType": "witsml20.Channel", "dataObjectCapabilities": {}}]
    )
    w.write_string(application_name)
    w.write_string(application_version)
    w.write_uuid(server_instance_id or uuid.uuid4())
    _write_array(w, protos, _encode_supported_protocol)
    _write_array(w, data_objs, _encode_supported_data_object)
    w.write_string("")  # supportedCompression
    _write_array(w, ["xml"], lambda bw, s: bw.write_string(str(s)))  # supportedFormats
    w.write_long(now_us)
    w.write_long(now_us)
    w.write_uuid(session_id or uuid.uuid4())
    _write_map(w, {}, encode_data_value)
    return w.to_bytes()


def build_close_session_frame(
    *,
    reason: str = "normal closure",
    message_id: int = 2,
    correlation_id: int = 0,
    message_flags: int = 0,
) -> bytes:
    """Encode ETP v1.2 `Core.CloseSession` (protocol=0, messageType=5)."""
    w = AvroBinaryWriter()
    EtpMessageHeader(
        protocol=0,
        message_type=5,
        correlation_id=correlation_id,
        message_id=message_id,
        message_flags=message_flags,
    ).encode(w)
    w.write_string(reason)
    return w.to_bytes()


def build_ping_frame(
    *,
    message_id: int = 2,
    current_date_time_us: int | None = None,
    message_flags: int = 0,
) -> bytes:
    """Encode ETP v1.2 `Core.Ping` (protocol=0, messageType=8)."""
    w = AvroBinaryWriter()
    EtpMessageHeader(
        protocol=0,
        message_type=8,
        correlation_id=0,
        message_id=message_id,
        message_flags=message_flags,
    ).encode(w)
    now_us = (
        current_date_time_us
        if current_date_time_us is not None
        else int(dt.datetime.now(dt.UTC).timestamp() * 1_000_000)
    )
    w.write_long(now_us)
    return w.to_bytes()


def build_pong_frame(
    *,
    correlation_id: int,
    message_id: int = 2,
    current_date_time_us: int | None = None,
    message_flags: int = 0,
) -> bytes:
    """Encode ETP v1.2 `Core.Pong` (protocol=0, messageType=9)."""
    w = AvroBinaryWriter()
    EtpMessageHeader(
        protocol=0,
        message_type=9,
        correlation_id=correlation_id,
        message_id=message_id,
        message_flags=message_flags,
    ).encode(w)
    now_us = (
        current_date_time_us
        if current_date_time_us is not None
        else int(dt.datetime.now(dt.UTC).timestamp() * 1_000_000)
    )
    w.write_long(now_us)
    return w.to_bytes()


def build_protocol_exception_frame(
    *,
    correlation_id: int = 0,
    message_id: int = 1,
    error: dict[str, Any] | None = None,
    errors: dict[str, dict[str, Any]] | None = None,
    message_flags: int = MSG_FLAG_FINALPART,
) -> bytes:
    """Encode ETP v1.2 `Core.ProtocolException` (protocol=0, messageType=1000)."""
    w = AvroBinaryWriter()
    EtpMessageHeader(
        protocol=0,
        message_type=1000,
        correlation_id=correlation_id,
        message_id=message_id,
        message_flags=message_flags,
    ).encode(w)
    if error is not None:
        w.write_long(1)
        w.write_string(str(error.get("message", "ProtocolException")))
        w.write_int(int(error.get("code", 1000)))
    else:
        w.write_long(0)

    def _write_err_info(bw: AvroBinaryWriter, info: dict[str, Any]) -> None:
        bw.write_string(str(info.get("message", "ProtocolException")))
        bw.write_int(int(info.get("code", 1000)))

    _write_map(w, dict(errors or {}), _write_err_info)
    return w.to_bytes()


def build_acknowledge_frame(
    *,
    protocol: int = 1,
    correlation_id: int,
    message_id: int,
    message_flags: int = 0,
) -> bytes:
    """Encode ETP v1.2 `Core.Acknowledge` (messageType=1001, empty body record)."""
    w = AvroBinaryWriter()
    EtpMessageHeader(
        protocol=protocol,
        message_type=1001,
        correlation_id=correlation_id,
        message_id=message_id,
        message_flags=message_flags,
    ).encode(w)
    return w.to_bytes()


def build_start_streaming_frame(
    *,
    message_id: int = 2,
    correlation_id: int = 0,
    message_flags: int = 0,
) -> bytes:
    """Encode ETP v1.2 `ChannelStreaming.StartStreaming` (protocol=1, messageType=3, empty body record)."""
    w = AvroBinaryWriter()
    EtpMessageHeader(
        protocol=1,
        message_type=3,
        correlation_id=correlation_id,
        message_id=message_id,
        message_flags=message_flags,
    ).encode(w)
    return w.to_bytes()


def build_stop_streaming_frame(
    *,
    message_id: int = 3,
    correlation_id: int = 0,
    message_flags: int = 0,
) -> bytes:
    """Encode ETP v1.2 `ChannelStreaming.StopStreaming` (protocol=1, messageType=4, empty body record)."""
    w = AvroBinaryWriter()
    EtpMessageHeader(
        protocol=1,
        message_type=4,
        correlation_id=correlation_id,
        message_id=message_id,
        message_flags=message_flags,
    ).encode(w)
    return w.to_bytes()


def _encode_index_metadata_record(w: AvroBinaryWriter, idx: dict[str, Any]) -> None:
    w.write_enum(str(idx.get("indexKind", "DateTime")), CHANNEL_INDEX_KINDS)
    interval = idx.get("interval") or {}
    encode_index_value(w, interval.get("startIndex"))
    encode_index_value(w, interval.get("endIndex"))
    w.write_string(str(interval.get("uom", idx.get("uom", "us"))))
    w.write_string(str(interval.get("depthDatum", idx.get("depthDatum", ""))))
    w.write_enum(str(idx.get("direction", "Increasing")), INDEX_DIRECTIONS)
    w.write_string(str(idx.get("name", idx.get("mnemonic", "TIME"))))
    w.write_string(str(idx.get("uom", "us")))
    w.write_string(str(idx.get("depthDatum", "")))
    w.write_string(str(idx.get("indexPropertyKindUri", "")))
    w.write_boolean(bool(idx.get("filterable", True)))


def _decode_index_metadata_record(r: AvroBinaryReader) -> dict[str, Any]:
    index_kind = r.read_enum(CHANNEL_INDEX_KINDS)
    start_idx = decode_index_value(r)
    end_idx = decode_index_value(r)
    interval_uom = r.read_string()
    interval_datum = r.read_string()
    direction = r.read_enum(INDEX_DIRECTIONS)
    name = r.read_string()
    uom = r.read_string()
    depth_datum = r.read_string()
    prop_uri = r.read_string()
    filterable = r.read_boolean()
    return {
        "indexKind": index_kind,
        "interval": {
            "startIndex": start_idx,
            "endIndex": end_idx,
            "uom": interval_uom,
            "depthDatum": interval_datum,
        },
        "direction": direction,
        "name": name,
        "mnemonic": name,
        "uom": uom,
        "depthDatum": depth_datum,
        "indexPropertyKindUri": prop_uri,
        "filterable": filterable,
    }


def _encode_attribute_metadata_record(w: AvroBinaryWriter, attr: dict[str, Any]) -> None:
    w.write_int(int(attr.get("attributeId", 0)))
    w.write_string(str(attr.get("attributeName", "quality")))
    w.write_enum(str(attr.get("dataKind", "typeString")), CHANNEL_DATA_KINDS)
    w.write_string(str(attr.get("uom", "Euc")))
    w.write_string(str(attr.get("depthDatum", "")))
    w.write_string(str(attr.get("attributePropertyKindUri", "")))
    _write_array(w, list(attr.get("axisVectorLengths") or []), lambda bw, x: bw.write_int(int(x)))


def _decode_attribute_metadata_record(r: AvroBinaryReader) -> dict[str, Any]:
    return {
        "attributeId": r.read_int(),
        "attributeName": r.read_string(),
        "dataKind": r.read_enum(CHANNEL_DATA_KINDS),
        "uom": r.read_string(),
        "depthDatum": r.read_string(),
        "attributePropertyKindUri": r.read_string(),
        "axisVectorLengths": r.read_array(lambda br: br.read_int()),
    }


def _encode_channel_metadata_record(w: AvroBinaryWriter, ch: dict[str, Any]) -> None:
    uri = str(ch.get("uri") or ch.get("channelUri") or "")
    cid = int(ch.get("id") if ch.get("id") is not None else ch.get("channelId", 0))
    if "indexes" in ch and ch["indexes"] is not None:
        indexes = list(ch["indexes"])
    else:
        indexes = [
            {
                "indexKind": "DateTime",
                "uom": "us",
                "direction": "Increasing",
                "name": "TIME",
            }
        ]
    ch_name = str(ch.get("channelName") or ch.get("mnemonic") or "")
    raw_kind = str(ch.get("dataKind") or ch.get("dataType") or "typeDouble")
    if raw_kind == "double":
        raw_kind = "typeDouble"
    elif raw_kind == "float":
        raw_kind = "typeFloat"
    elif raw_kind == "int":
        raw_kind = "typeInt"
    elif raw_kind == "long":
        raw_kind = "typeLong"
    elif raw_kind == "string":
        raw_kind = "typeString"
    elif raw_kind == "boolean":
        raw_kind = "typeBoolean"

    w.write_string(uri)
    w.write_long(cid)
    _write_array(w, indexes, _encode_index_metadata_record)
    w.write_string(ch_name)
    w.write_enum(raw_kind, CHANNEL_DATA_KINDS)
    w.write_string(str(ch.get("uom", "")))
    w.write_string(str(ch.get("depthDatum", "KB")))
    w.write_string(str(ch.get("channelClassUri", "")))
    w.write_enum(str(ch.get("status", "Active")), ACTIVE_STATUS_KINDS)
    w.write_string(str(ch.get("source", ch.get("sourceMnemonic", ch_name))))
    _write_array(w, list(ch.get("axisVectorLengths") or []), lambda bw, x: bw.write_int(int(x)))
    _write_array(w, list(ch.get("attributeMetadata") or []), _encode_attribute_metadata_record)
    _write_map(w, dict(ch.get("customData") or {}), encode_data_value)


def _decode_channel_metadata_record(r: AvroBinaryReader) -> dict[str, Any]:
    uri = r.read_string()
    cid = r.read_long()
    indexes = r.read_array(_decode_index_metadata_record)
    ch_name = r.read_string()
    data_kind = r.read_enum(CHANNEL_DATA_KINDS)
    uom = r.read_string()
    depth_datum = r.read_string()
    channel_class_uri = r.read_string()
    status = r.read_enum(ACTIVE_STATUS_KINDS)
    source = r.read_string()
    axis_lengths = r.read_array(lambda br: br.read_int())
    attr_meta = r.read_array(_decode_attribute_metadata_record)
    custom_data = r.read_map(decode_data_value)
    return {
        "uri": uri,
        "channelUri": uri,
        "id": cid,
        "channelId": cid,
        "indexes": indexes,
        "channelName": ch_name,
        "mnemonic": ch_name,
        "dataKind": data_kind,
        "dataType": data_kind,
        "uom": uom,
        "depthDatum": depth_datum,
        "channelClassUri": channel_class_uri,
        "status": status,
        "source": source,
        "sourceMnemonic": source or ch_name,
        "axisVectorLengths": axis_lengths,
        "attributeMetadata": attr_meta,
        "customData": custom_data,
    }


def build_channel_metadata_frame(
    *,
    channels: list[dict[str, Any]],
    correlation_id: int = 0,
    message_id: int = 2,
    message_flags: int = MSG_FLAG_FINALPART,
) -> bytes:
    """Encode ETP v1.2 `ChannelStreaming.ChannelMetadata` (protocol=1, messageType=1)."""
    w = AvroBinaryWriter()
    EtpMessageHeader(
        protocol=1,
        message_type=1,
        correlation_id=correlation_id,
        message_id=message_id,
        message_flags=message_flags,
    ).encode(w)
    _write_array(w, channels, _encode_channel_metadata_record)
    return w.to_bytes()


def _encode_data_attribute(w: AvroBinaryWriter, attr: dict[str, Any]) -> None:
    w.write_int(int(attr.get("attributeId", 0)))
    encode_data_value(w, attr.get("attributeValue"))


def _decode_data_attribute(r: AvroBinaryReader) -> dict[str, Any]:
    return {
        "attributeId": r.read_int(),
        "attributeValue": {"item": decode_data_value(r)},
    }


def _encode_data_item(w: AvroBinaryWriter, item: dict[str, Any]) -> None:
    w.write_long(int(item.get("channelId", 0)))
    indexes = list(item.get("indexes") or [])
    _write_array(w, indexes, encode_index_value)
    encode_data_value(w, item.get("value"))
    attrs = list(item.get("valueAttributes") or [])
    _write_array(w, attrs, _encode_data_attribute)


def _decode_data_item(r: AvroBinaryReader) -> dict[str, Any]:
    cid = r.read_long()
    indexes = r.read_array(lambda br: {"item": decode_index_value(br)})
    val = decode_data_value(r)
    attrs = r.read_array(_decode_data_attribute)
    return {
        "channelId": cid,
        "indexes": indexes,
        "value": {"item": val},
        "valueAttributes": attrs,
    }


def build_channel_data_frame(
    *,
    data_items: list[dict[str, Any]],
    correlation_id: int = 0,
    message_id: int = 3,
    message_flags: int = MSG_FLAG_FINALPART,
) -> bytes:
    """Encode ETP v1.2 `ChannelStreaming.ChannelData` (protocol=1, messageType=2)."""
    w = AvroBinaryWriter()
    EtpMessageHeader(
        protocol=1,
        message_type=2,
        correlation_id=correlation_id,
        message_id=message_id,
        message_flags=message_flags,
    ).encode(w)
    _write_array(w, data_items, _encode_data_item)
    return w.to_bytes()


def decode_etp_binary_frame(raw: bytes) -> dict[str, Any]:
    """Decode a full ETP v1.2 binary WebSocket frame (`MessageHeader` + protocol message body)."""
    if not isinstance(raw, (bytes, bytearray)):
        raise AvroBinaryDecodeError("ETP 1.2 binary decoder requires a bytes frame")
    if len(raw) == 0:
        raise AvroBinaryDecodeError("empty ETP 1.2 binary frame")
    r = AvroBinaryReader(bytes(raw))
    hdr = EtpMessageHeader.decode(r)
    if hdr.message_flags & MSG_FLAG_COMPRESSED:
        raise AvroBinaryDecodeError(
            "ETP 1.2 binary frame sets compressed flag (0x08), which was not negotiated (supportedCompression=[])"
        )
    proto = hdr.protocol
    mtype = hdr.message_type

    body: dict[str, Any]
    if mtype == 1000:
        err_branch = r.read_long()
        err_obj: dict[str, Any] | None = None
        if err_branch == 1:
            err_obj = {"message": r.read_string(), "code": r.read_int()}
        elif err_branch != 0:
            raise AvroBinaryDecodeError(f"invalid ProtocolException.error union branch {err_branch}")
        errs_map = r.read_map(lambda br: {"message": br.read_string(), "code": br.read_int()})
        body = {
            "type": "ProtocolException",
            "error": err_obj,
            "errors": errs_map,
        }
    elif mtype == 1001:
        body = {"type": "Acknowledge"}
    elif proto == 0 and mtype == 1:
        app_name = r.read_string()
        app_ver = r.read_string()
        client_id = r.read_uuid_str()
        req_protos = r.read_array(_decode_supported_protocol)
        supp_objs = r.read_array(_decode_supported_data_object)
        supp_comp = r.read_array(lambda br: br.read_string())
        supp_fmt = r.read_array(lambda br: br.read_string())
        curr_dt = r.read_long()
        earliest_dt = r.read_long()
        auth_req = r.read_boolean()
        caps = r.read_map(decode_data_value)
        body = {
            "type": "RequestSession",
            "applicationName": app_name,
            "applicationVersion": app_ver,
            "clientInstanceId": client_id,
            "requestedProtocols": req_protos,
            "supportedDataObjects": supp_objs,
            "supportedCompression": supp_comp,
            "supportedFormats": supp_fmt,
            "currentDateTime": curr_dt,
            "earliestRetainedChangeTime": earliest_dt,
            "serverAuthorizationRequired": auth_req,
            "endpointCapabilities": caps,
        }
    elif proto == 0 and mtype == 2:
        app_name = r.read_string()
        app_ver = r.read_string()
        srv_id = r.read_uuid_str()
        supp_protos = r.read_array(_decode_supported_protocol)
        supp_objs = r.read_array(_decode_supported_data_object)
        supp_comp = r.read_string()
        supp_fmt = r.read_array(lambda br: br.read_string())
        curr_dt = r.read_long()
        earliest_dt = r.read_long()
        sess_id = r.read_uuid_str()
        caps = r.read_map(decode_data_value)
        body = {
            "type": "OpenSession",
            "applicationName": app_name,
            "applicationVersion": app_ver,
            "serverInstanceId": srv_id,
            "supportedProtocols": supp_protos,
            "supportedDataObjects": supp_objs,
            "supportedCompression": supp_comp,
            "supportedFormats": supp_fmt,
            "currentDateTime": curr_dt,
            "earliestRetainedChangeTime": earliest_dt,
            "sessionId": sess_id,
            "endpointCapabilities": caps,
        }
    elif proto == 0 and mtype == 5:
        body = {"type": "CloseSession", "reason": r.read_string()}
    elif proto == 0 and mtype == 8:
        body = {"type": "Ping", "currentDateTime": r.read_long()}
    elif proto == 0 and mtype == 9:
        body = {"type": "Pong", "currentDateTime": r.read_long()}
    elif proto == 1 and mtype == 1:
        channels = r.read_array(_decode_channel_metadata_record)
        body = {"type": "ChannelMetadata", "channels": channels}
    elif proto == 1 and mtype == 2:
        data_items = r.read_array(_decode_data_item)
        body = {
            "type": "ChannelData",
            "data": data_items,
            "isFinalBatch": bool(hdr.message_flags & MSG_FLAG_FINALPART),
        }
    elif proto == 1 and mtype == 3:
        body = {"type": "StartStreaming"}
    elif proto == 1 and mtype == 4:
        body = {"type": "StopStreaming"}
    elif proto == 1 and mtype == 5:
        truncs = r.read_array(
            lambda br: {"channelId": br.read_long(), "newEndIndex": decode_index_value(br)}
        )
        body = {"type": "TruncateChannels", "channels": truncs}
    else:
        raise AvroBinaryDecodeError(
            f"unsupported ETP 1.2 binary (protocol={proto}, messageType={mtype}) pair"
        )

    if r.remaining != 0:
        raise AvroBinaryDecodeError(
            f"ETP 1.2 binary frame has {r.remaining} trailing unconsumed bytes after {body.get('type')}"
        )

    return {
        "header": {
            "protocol": hdr.protocol,
            "messageType": hdr.message_type,
            "correlationId": hdr.correlation_id,
            "messageId": hdr.message_id,
            "messageFlags": hdr.message_flags,
        },
        "body": body,
        "type": body["type"],
        "protocol": hdr.protocol,
        "messageType": hdr.message_type,
        "correlationId": hdr.correlation_id,
        "messageId": hdr.message_id,
        "messageFlags": hdr.message_flags,
        **body,
    }


def _extract_binary_exception_text(msg: dict[str, Any]) -> str:
    parts: list[str] = []
    err = msg.get("error")
    if isinstance(err, dict) and err.get("message"):
        parts.append(f"[{err.get('code', '')}] {err['message']}".strip())
    errs = msg.get("errors")
    if isinstance(errs, dict):
        for k, v in errs.items():
            if isinstance(v, dict) and v.get("message"):
                parts.append(f"{k}: [{v.get('code', '')}] {v['message']}".strip())
    return "; ".join(parts) if parts else "ETP ProtocolException"


# ---------------------------------------------------------------------------
# Production ETP 1.2 Binary Avro WebSocket Client
# ---------------------------------------------------------------------------


class EtpBinaryWebSocketClient:
    """Async ETP 1.2 Binary Avro WebSocket client for Protocol 0 (Core) + Protocol 1 (ChannelStreaming)."""

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

    async def _recv_binary(self, ws: Any, *, timeout: float | None = None) -> dict[str, Any]:
        wait_s = self._timeout_seconds if timeout is None else timeout
        raw = await asyncio.wait_for(ws.recv(), timeout=wait_s)
        if isinstance(raw, str):
            raise ConnectorTransportError(
                "protocol_error",
                "ETP 1.2 binary profile received unexpected text WebSocket frame (expected binary Avro)",
                retryable=False,
            )
        if len(raw) > MAX_ETP_FRAME_BYTES:
            raise ConnectorTransportError(
                "oversized_payload",
                f"ETP 1.2 binary frame exceeds {MAX_ETP_FRAME_BYTES} bytes",
                retryable=False,
            )
        try:
            return decode_etp_binary_frame(bytes(raw))
        except AvroBinaryDecodeError as exc:
            msg = str(exc)
            lower_msg = msg.lower()
            if "exceeds max" in lower_msg:
                category = "oversized_payload"
            elif "compressed flag" in lower_msg:
                category = "protocol_error"
            else:
                category = "malformed_payload"
            raise ConnectorTransportError(category, f"ETP 1.2 binary decode error: {msg}", retryable=False) from exc

    @staticmethod
    def _parse_channel_metadata_record(ch: dict[str, Any]) -> EtpChannelInfo:
        cid_raw = ch.get("id", ch.get("channelId", 0))
        if isinstance(cid_raw, bool) or not isinstance(cid_raw, int):
            raise ConnectorTransportError(
                "protocol_error",
                f"ETP ChannelMetadata has invalid integer channel id {cid_raw!r}",
                retryable=False,
            )
        uri = str(ch.get("uri") or ch.get("channelUri") or "").strip()
        ch_name = str(ch.get("channelName") or ch.get("mnemonic") or ch.get("source") or "").strip()
        mnemonic = str(ch.get("mnemonic") or ch.get("source") or ch_name).strip()
        uom = str(ch.get("uom") or "").strip()
        dtype = str(ch.get("dataKind") or ch.get("dataType") or "typeDouble").strip()

        raw_indexes = ch.get("indexes")
        parsed_indexes: list[EtpIndexMetadata] = []
        if isinstance(raw_indexes, list):
            for idx_entry in raw_indexes:
                if isinstance(idx_entry, dict):
                    raw_kind = str(idx_entry.get("indexKind") or "").strip()
                    raw_uom = str(idx_entry.get("uom") or "").strip()
                    if not raw_kind or not raw_uom:
                        continue
                    parsed_indexes.append(
                        EtpIndexMetadata(
                            index_kind=raw_kind,
                            uom=raw_uom,
                            direction=str(idx_entry.get("direction") or "Increasing").strip(),
                            mnemonic=str(
                                idx_entry.get("name") or idx_entry.get("mnemonic") or "TIME"
                            ).strip(),
                        )
                    )

        return EtpChannelInfo(
            channel_id=cid_raw,
            mnemonic=mnemonic,
            unit=uom,
            channel_name=ch_name,
            uri=uri,
            data_type=dtype,
            indexes=tuple(parsed_indexes),
        )

    @staticmethod
    def _validate_open_session(open_msg: dict[str, Any], request_msg_id: int) -> str:
        if open_msg.get("type") == "ProtocolException":
            err_text = _extract_binary_exception_text(open_msg)
            if "401" in err_text or "auth" in err_text.lower() or "403" in err_text:
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
        if open_corr_id != request_msg_id:
            raise ConnectorTransportError(
                "protocol_error",
                f"ETP OpenSession correlationId {open_corr_id} does not match RequestSession messageId {request_msg_id}",
                retryable=False,
            )
        supp_comp = str(open_msg.get("supportedCompression") or "").strip()
        if supp_comp.lower() not in ("", "none", "identity"):
            raise ConnectorTransportError(
                "protocol_error",
                f"ETP OpenSession selected unsupported compression {supp_comp!r} (requested none)",
                retryable=False,
            )
        supp_formats = [
            str(fmt).strip().lower()
            for fmt in (open_msg.get("supportedFormats") or [])
            if str(fmt).strip()
        ]
        if supp_formats and "xml" not in supp_formats:
            raise ConnectorTransportError(
                "protocol_error",
                f"ETP OpenSession did not negotiate required format 'xml' (supportedFormats={supp_formats})",
                retryable=False,
            )
        supp_protos = open_msg.get("supportedProtocols") or []
        proto_ids = {int(p.get("protocol", -1)) for p in supp_protos if isinstance(p, dict)}
        if 1 not in proto_ids:
            raise ConnectorTransportError(
                "protocol_error",
                f"ETP OpenSession did not negotiate required ChannelStreaming Protocol 1 (supportedProtocols={sorted(proto_ids)})",
                retryable=False,
            )
        return str(open_msg.get("sessionId") or "etp-binary-session")

    async def probe_session(self) -> tuple[str, list[EtpChannelInfo]]:
        """Perform binary ETP 1.2 `RequestSession` -> `OpenSession` and `StartStreaming` -> `ChannelMetadata` discovery."""
        if enforce_connect_time_ssrf is _DEFAULT_ENFORCE_SSRF:
            pinned = await enforce_connect_time_ssrf_async(
                self._endpoint_url,
                "etp.1.2.binary_ws",
                dns_timeout_seconds=min(self._timeout_seconds, 5.0),
            )
        else:
            pinned = enforce_connect_time_ssrf(self._endpoint_url, "etp.1.2.binary_ws")
        headers = self._build_headers()
        try:
            async with websockets.connect(
                self._endpoint_url,
                **self._ws_connect_kwargs(pinned, headers, min(self._timeout_seconds, 2.0)),
            ) as ws:
                if getattr(ws, "subprotocol", "energistics-tp") != "energistics-tp":
                    raise ConnectorTransportError(
                        "protocol_error",
                        f"ETP WebSocket server did not select 'energistics-tp' subprotocol (got {getattr(ws, 'subprotocol', None)!r})",
                        retryable=False,
                    )
                next_msg_id = 1
                await ws.send(build_request_session_frame(message_id=next_msg_id))
                open_msg = await self._recv_binary(ws)
                session_id = self._validate_open_session(open_msg, next_msg_id)
                if open_msg.get("messageFlags", 0) & MSG_FLAG_ACKNOWLEDGE:
                    next_msg_id += 1
                    await ws.send(
                        build_acknowledge_frame(
                            protocol=0,
                            correlation_id=int(open_msg.get("messageId", 1)),
                            message_id=next_msg_id,
                        )
                    )

                next_msg_id += 1
                await ws.send(build_start_streaming_frame(message_id=next_msg_id))

                discovered: list[EtpChannelInfo] = []
                with contextlib.suppress(TimeoutError, ConnectionClosedOK):
                    for _ in range(8):
                        msg = await self._recv_binary(ws, timeout=min(self._timeout_seconds, 1.5))
                        if msg.get("messageFlags", 0) & MSG_FLAG_ACKNOWLEDGE:
                            next_msg_id += 1
                            await ws.send(
                                build_acknowledge_frame(
                                    protocol=int(msg.get("protocol", 1)),
                                    correlation_id=int(msg.get("messageId", 1)),
                                    message_id=next_msg_id,
                                )
                            )
                        if msg.get("type") == "ChannelMetadata":
                            for ch in msg.get("channels") or []:
                                if isinstance(ch, dict):
                                    discovered.append(self._parse_channel_metadata_record(ch))
                            if msg.get("messageFlags", MSG_FLAG_FINALPART) & MSG_FLAG_FINALPART:
                                break
                        elif msg.get("type") == "ProtocolException":
                            raise ConnectorTransportError(
                                "protocol_error",
                                _extract_binary_exception_text(msg),
                                retryable=False,
                            )

                with contextlib.suppress(Exception):
                    next_msg_id += 1
                    await ws.send(build_stop_streaming_frame(message_id=next_msg_id))
                    next_msg_id += 1
                    await ws.send(build_close_session_frame(reason="probe complete", message_id=next_msg_id))
                return session_id, discovered
        except ConnectorTransportError:
            raise
        except InvalidStatus as exc:
            status = getattr(exc.response, "status_code", 0)
            if status in {401, 403}:
                raise ConnectorTransportError(
                    "auth_failure",
                    f"ETP binary server rejected handshake with HTTP {status}",
                    retryable=False,
                ) from exc
            raise ConnectorTransportError(
                "protocol_error",
                f"ETP binary server handshake failed with HTTP {status}",
                retryable=status >= 500,
            ) from exc
        except InvalidHandshake as exc:
            raise ConnectorTransportError(
                "protocol_error",
                redact_sensitive_text(f"ETP binary WebSocket handshake invalid: {exc}", self._secrets)
                or "ETP binary WebSocket handshake invalid",
                retryable=False,
            ) from exc
        except TimeoutError as exc:
            raise ConnectorTransportError(
                "timeout",
                "ETP binary WebSocket handshake or session open timed out",
                retryable=True,
            ) from exc
        except ssl.SSLError as exc:
            raise ConnectorTransportError(
                "tls_error",
                redact_sensitive_text(f"ETP binary TLS error: {exc}", self._secrets)
                or "ETP binary TLS error",
                retryable=False,
            ) from exc
        except (OSError, WebSocketException) as exc:
            msg = str(exc)
            if "SSL" in msg.upper() or "CERTIFICATE" in msg.upper():
                raise ConnectorTransportError(
                    "tls_error",
                    redact_sensitive_text(f"ETP binary TLS error: {exc}", self._secrets)
                    or "ETP binary TLS error",
                    retryable=False,
                ) from exc
            raise ConnectorTransportError(
                "connection_refused",
                redact_sensitive_text(f"ETP binary WebSocket connection error: {exc}", self._secrets)
                or "ETP binary WebSocket connection error",
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
        """Open an ETP 1.2 binary Avro WebSocket session, stream `ChannelMetadata` + `ChannelData`, and reconcile."""
        if enforce_connect_time_ssrf is _DEFAULT_ENFORCE_SSRF:
            pinned = await enforce_connect_time_ssrf_async(
                self._endpoint_url,
                "etp.1.2.binary_ws",
                dns_timeout_seconds=min(self._timeout_seconds, 5.0),
            )
        else:
            pinned = enforce_connect_time_ssrf(self._endpoint_url, "etp.1.2.binary_ws")
        mappings = parse_channel_mappings(config.get("channel_mappings"))
        mapping_by_mnemonic = {m.source_mnemonic.upper(): m for m in mappings}
        mapping_by_uri = {m.channel_uri.lower(): m for m in mappings if m.channel_uri}
        headers = self._build_headers()
        disposition = ProtocolPollDisposition()

        try:
            async with websockets.connect(
                self._endpoint_url,
                **self._ws_connect_kwargs(pinned, headers, min(self._timeout_seconds, 5.0)),
            ) as ws:
                if getattr(ws, "subprotocol", "energistics-tp") != "energistics-tp":
                    raise ConnectorTransportError(
                        "protocol_error",
                        f"ETP WebSocket server did not select 'energistics-tp' subprotocol (got {getattr(ws, 'subprotocol', None)!r})",
                        retryable=False,
                    )
                next_msg_id = 1
                await ws.send(build_request_session_frame(message_id=next_msg_id))
                open_msg = await self._recv_binary(ws)
                disposition.pages_or_messages_received += 1
                session_id = self._validate_open_session(open_msg, next_msg_id)
                if open_msg.get("messageFlags", 0) & MSG_FLAG_ACKNOWLEDGE:
                    next_msg_id += 1
                    await ws.send(
                        build_acknowledge_frame(
                            protocol=0,
                            correlation_id=int(open_msg.get("messageId", 1)),
                            message_id=next_msg_id,
                        )
                    )

                # Ping/Pong keepalive probe
                ping_verified = False
                next_msg_id += 1
                ping_msg_id = next_msg_id
                await ws.send(build_ping_frame(message_id=ping_msg_id))

                # StartStreaming (Protocol 1, messageType 3)
                next_msg_id += 1
                await ws.send(build_start_streaming_frame(message_id=next_msg_id))

                discovered: dict[int, EtpChannelInfo] = {}
                channel_id_to_mapping: dict[int, ChannelMappingSpec] = {}
                frames: list[SourceFrame] = []
                messages_received = 1
                duplicates_suppressed = 0

                prev_wm = dict(watermark or {})
                prev_global_dt: dt.datetime | None = None
                if prev_wm.get("cursor_timestamp"):
                    with contextlib.suppress(ValueError):
                        prev_global_dt = _parse_iso_utc(str(prev_wm["cursor_timestamp"]), require_tz=False)

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

                def _register_metadata_channels(raw_channels: list[Any]) -> None:
                    for ch_raw in raw_channels:
                        if not isinstance(ch_raw, dict):
                            continue
                        meta = self._parse_channel_metadata_record(ch_raw)
                        discovered[meta.channel_id] = meta
                        matched = (
                            mapping_by_uri.get(meta.channel_uri.lower())
                            or mapping_by_mnemonic.get(meta.source_mnemonic.upper())
                            or mapping_by_mnemonic.get(meta.channel_name.upper())
                        )
                        if matched is not None:
                            channel_id_to_mapping[meta.channel_id] = matched
                        else:
                            channel_id_to_mapping.pop(meta.channel_id, None)

                for _ in range(64):
                    try:
                        msg = await self._recv_binary(ws, timeout=min(self._timeout_seconds, 2.0))
                    except (TimeoutError, ConnectionClosedOK):
                        break

                    messages_received += 1
                    disposition.pages_or_messages_received += 1
                    mtype = msg.get("type")
                    mflags = int(msg.get("messageFlags", 0))

                    if mflags & MSG_FLAG_ACKNOWLEDGE:
                        next_msg_id += 1
                        await ws.send(
                            build_acknowledge_frame(
                                protocol=int(msg.get("protocol", 1)),
                                correlation_id=int(msg.get("messageId", 1)),
                                message_id=next_msg_id,
                            )
                        )

                    if mtype == "Pong":
                        if msg.get("correlationId") in (0, ping_msg_id):
                            ping_verified = True
                        continue
                    if mtype == "Ping":
                        next_msg_id += 1
                        await ws.send(
                            build_pong_frame(
                                correlation_id=int(msg.get("messageId", 0)),
                                message_id=next_msg_id,
                            )
                        )
                        continue
                    if mtype == "Acknowledge":
                        continue
                    if mtype == "ProtocolException":
                        raise ConnectorTransportError(
                            "protocol_error",
                            _extract_binary_exception_text(msg),
                            retryable=False,
                        )
                    if mtype == "ChannelMetadata":
                        _register_metadata_channels(msg.get("channels") or [])
                        continue
                    if mtype == "TruncateChannels":
                        for trunc in msg.get("channels") or []:
                            if not isinstance(trunc, dict):
                                continue
                            tcid = trunc.get("channelId")
                            if not isinstance(tcid, int) or isinstance(tcid, bool):
                                continue
                            tmeta = discovered.get(tcid)
                            tmapping = channel_id_to_mapping.get(tcid)
                            if tmapping is None or tmeta is None or not tmeta.indexes:
                                continue
                            raw_end = trunc.get("newEndIndex")
                            if isinstance(raw_end, dict) and "item" in raw_end:
                                raw_end = raw_end["item"]
                            end_dt, _ = _parse_etp_index_timestamp(
                                raw_end,
                                index_kind=tmeta.primary_index.index_kind,
                                index_uom=tmeta.primary_index.uom,
                            )
                            if end_dt is None:
                                continue
                            # Clamp pre-existing cursor watermark and prune recent_points if truncated back before it
                            prev_dt = prev_channel_dt.get(tmapping.channel_key)
                            if prev_dt is not None and prev_dt > end_dt:
                                prev_channel_dt[tmapping.channel_key] = end_dt
                            rec_map = prev_channel_recent.get(tmapping.channel_key)
                            if isinstance(rec_map, dict):
                                for r_ts in list(rec_map.keys()):
                                    try:
                                        parsed_r_dt = dt.datetime.fromisoformat(str(r_ts))
                                        if parsed_r_dt.tzinfo is None:
                                            parsed_r_dt = parsed_r_dt.replace(tzinfo=dt.UTC)
                                        if parsed_r_dt > end_dt:
                                            rec_map.pop(r_ts, None)
                                    except ValueError:
                                        pass
                            # Drop any already-buffered frames in the current batch after newEndIndex
                            kept_frames: list[SourceFrame] = []
                            dropped_count = 0
                            for ef in frames:
                                if ef.channel_key == tmapping.channel_key and ef.ts > end_dt:
                                    dropped_count += 1
                                    seen_in_batch.pop(
                                        f"etp_bin:{ef.channel_key}:{ef.ts.isoformat()}", None
                                    )
                                else:
                                    kept_frames.append(ef)
                            if dropped_count > 0:
                                frames[:] = kept_frames
                                disposition.mapped_frames_emitted = max(
                                    0, disposition.mapped_frames_emitted - dropped_count
                                )
                                disposition.watermark_suppressed += dropped_count
                                duplicates_suppressed += dropped_count
                                trunc_src_label = (
                                    tmeta.source_mnemonic
                                    if tmeta is not None and tmeta.source_mnemonic
                                    else (tmapping.source_mnemonic if tmapping is not None else f"CH_{tcid}")
                                )
                                ch_entry = disposition.per_source_channel.get(trunc_src_label)
                                if isinstance(ch_entry, dict):
                                    ch_entry["emitted"] = max(
                                        0, int(ch_entry.get("emitted", 0)) - dropped_count
                                    )
                                    ch_entry["watermark_suppressed"] = (
                                        int(ch_entry.get("watermark_suppressed", 0)) + dropped_count
                                    )
                        continue
                    if mtype == "CloseSession":
                        break
                    if mtype != "ChannelData":
                        continue

                    if mflags & MSG_FLAG_NO_DATA:
                        if mflags & MSG_FLAG_FINALPART:
                            break
                        continue

                    data_items = msg.get("data")
                    if not isinstance(data_items, list):
                        raise ConnectorTransportError(
                            "malformed_payload",
                            "ETP ChannelData payload field 'data' must be a list",
                            retryable=False,
                        )

                    for item in data_items:
                        if not isinstance(item, dict):
                            disposition.source_records_discovered += 1
                            disposition.rejected_invalid_value += 1
                            disposition.record_channel_outcome("<INVALID>", "rejected")
                            disposition.record_rejection(reason="invalid_data_item_object")
                            continue

                        raw_cid = item.get("channelId", -1)
                        cid = raw_cid if isinstance(raw_cid, int) and not isinstance(raw_cid, bool) else -1
                        meta = discovered.get(cid)
                        mapping = channel_id_to_mapping.get(cid)
                        source_label = (
                            meta.source_mnemonic
                            if meta is not None and meta.source_mnemonic
                            else (mapping.source_mnemonic if mapping is not None else f"CH_{cid}")
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

                        if meta is not None and not meta.indexes:
                            disposition.rejected_unsupported_index += 1
                            disposition.record_channel_outcome(source_label, "rejected")
                            disposition.record_rejection(
                                reason="missing_channel_index_metadata",
                                source_mnemonic=source_label,
                            )
                            continue

                        primary_idx_meta = meta.primary_index if meta is not None else EtpIndexMetadata()
                        raw_indexes_list = item.get("indexes")
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
                                    if isinstance(sec_raw, dict) and "depth" in sec_raw:
                                        sec_raw = sec_raw["depth"]
                                    if not isinstance(sec_raw, bool) and isinstance(sec_raw, int | float):
                                        sec_f = float(sec_raw)
                                        if math.isfinite(sec_f):
                                            depth_val = (
                                                sec_f * 0.3048
                                                if sec_uom in {"ft", "feet", "ftus"}
                                                else sec_f
                                            )

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

                        # Session-independent point_id keyed by canonical channel_key + timestamp
                        # so dynamic session channelId reassignment across reconnects preserves dedup/revision state
                        point_id = f"etp_bin:{mapping.channel_key}:{item_dt.isoformat()}"

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

                        quality = "good" if val is not None else "missing"
                        attrs = item.get("valueAttributes") or []
                        if isinstance(attrs, list):
                            for attr in attrs:
                                if isinstance(attr, dict):
                                    aval = attr.get("attributeValue")
                                    if isinstance(aval, dict) and "item" in aval:
                                        aval = aval["item"]
                                    q_cand = str(aval or "").strip().lower()
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

                    if mflags & MSG_FLAG_FINALPART:
                        break

                with contextlib.suppress(Exception):
                    next_msg_id += 1
                    await ws.send(build_stop_streaming_frame(message_id=next_msg_id))
                    next_msg_id += 1
                    await ws.send(build_close_session_frame(reason="poll batch complete", message_id=next_msg_id))

                next_wm = compute_etp_watermark_for_frames(
                    watermark,
                    frames,
                    session_id=session_id,
                    has_more=disposition.partial_more_available,
                )
                next_wm["protocol"] = "etp.1.2.binary_ws"
                return EtpPollBatch(
                    frames=tuple(frames),
                    discovered_channels=tuple(discovered.values()),
                    next_watermark=next_wm,
                    session_id=session_id,
                    messages_received=messages_received,
                    ping_pong_verified=ping_verified,
                    duplicates_suppressed=duplicates_suppressed,
                    disposition=disposition,
                )
        except ConnectorTransportError:
            raise
        except InvalidStatus as exc:
            status = getattr(exc.response, "status_code", 0)
            if status in {401, 403}:
                raise ConnectorTransportError(
                    "auth_failure",
                    f"ETP binary server rejected handshake with HTTP {status}",
                    retryable=False,
                ) from exc
            raise ConnectorTransportError(
                "protocol_error",
                f"ETP binary server handshake failed with HTTP {status}",
                retryable=status >= 500,
            ) from exc
        except InvalidHandshake as exc:
            raise ConnectorTransportError(
                "protocol_error",
                redact_sensitive_text(f"ETP binary WebSocket handshake invalid: {exc}", self._secrets)
                or "ETP binary WebSocket handshake invalid",
                retryable=False,
            ) from exc
        except ConnectionClosedError as exc:
            raise ConnectorTransportError(
                "stream_dropped",
                f"ETP binary WebSocket connection closed unexpectedly (code={exc.code})",
                retryable=True,
            ) from exc
        except TimeoutError as exc:
            raise ConnectorTransportError(
                "timeout",
                "ETP binary WebSocket session timed out",
                retryable=True,
            ) from exc
        except ssl.SSLError as exc:
            raise ConnectorTransportError(
                "tls_error",
                redact_sensitive_text(f"ETP binary TLS error: {exc}", self._secrets)
                or "ETP binary TLS error",
                retryable=False,
            ) from exc
        except (OSError, WebSocketException) as exc:
            msg = str(exc)
            if "SSL" in msg.upper() or "CERTIFICATE" in msg.upper():
                raise ConnectorTransportError(
                    "tls_error",
                    redact_sensitive_text(f"ETP binary TLS error: {exc}", self._secrets)
                    or "ETP binary TLS error",
                    retryable=False,
                ) from exc
            raise ConnectorTransportError(
                "connection_refused",
                redact_sensitive_text(f"ETP binary WebSocket connection error: {exc}", self._secrets)
                or "ETP binary WebSocket connection error",
                retryable=True,
            ) from exc


# ---------------------------------------------------------------------------
# Non-Production Local ETP 1.2 Binary Avro Test Server (`LocalEtpBinaryWebSocketServer`)
# ---------------------------------------------------------------------------


@dataclass
class LocalEtpBinaryWebSocketServer:
    """In-process TCP WebSocket server speaking real ETP 1.2 Binary Avro for local non-production testing."""

    host: str = "127.0.0.1"
    port: int = 0
    expected_username: str | None = None
    expected_password: str | None = None
    expected_bearer_token: str | None = None
    fault_mode: str | None = None
    channels: list[EtpChannelInfo] = field(
        default_factory=lambda: [
            EtpChannelInfo(
                channel_id=1,
                mnemonic="SPP",
                unit="psi",
                channel_name="SPP",
                uri="eml://witsml20/Channel(spp)",
                indexes=(
                    EtpIndexMetadata("DateTime", "us", "Increasing", "TIME"),
                    EtpIndexMetadata("MeasuredDepth", "m", "Increasing", "DEPTH"),
                ),
            ),
            EtpChannelInfo(
                channel_id=2,
                mnemonic="WOB",
                unit="klbf",
                channel_name="WOB",
                uri="eml://witsml20/Channel(wob)",
                indexes=(
                    EtpIndexMetadata("DateTime", "us", "Increasing", "TIME"),
                    EtpIndexMetadata("MeasuredDepth", "m", "Increasing", "DEPTH"),
                ),
            ),
            EtpChannelInfo(
                channel_id=3,
                mnemonic="HKLD",
                unit="klbf",
                channel_name="HKLD",
                uri="eml://witsml20/Channel(hkld)",
                indexes=(
                    EtpIndexMetadata("DateTime", "us", "Increasing", "TIME"),
                    EtpIndexMetadata("MeasuredDepth", "m", "Increasing", "DEPTH"),
                ),
            ),
            EtpChannelInfo(
                channel_id=4,
                mnemonic="RPM",
                unit="rpm",
                channel_name="RPM",
                uri="eml://witsml20/Channel(rpm)",
                indexes=(
                    EtpIndexMetadata("DateTime", "us", "Increasing", "TIME"),
                    EtpIndexMetadata("MeasuredDepth", "m", "Increasing", "DEPTH"),
                ),
            ),
            EtpChannelInfo(
                channel_id=5,
                mnemonic="FLOWIN",
                unit="gal/min",
                channel_name="FLOWIN",
                uri="eml://witsml20/Channel(flowin)",
                indexes=(
                    EtpIndexMetadata("DateTime", "us", "Increasing", "TIME"),
                    EtpIndexMetadata("MeasuredDepth", "m", "Increasing", "DEPTH"),
                ),
            ),
        ]
    )
    points: list[EtpSamplePoint] = field(default_factory=list)
    sessions_opened: int = 0
    acks_received: int = 0
    _server: Any = None

    @property
    def endpoint_url(self) -> str:
        return f"ws://{self.host}:{self.port}/etp-binary"

    def seed_default_points(
        self,
        *,
        start: dt.datetime | None = None,
        count: int = 6,
        step_seconds: float = 5.0,
    ) -> None:
        base = (start or (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=count * step_seconds))).astimezone(
            dt.UTC
        )
        seeded: list[EtpSamplePoint] = []
        for idx in range(count):
            ts_iso = (base + dt.timedelta(seconds=idx * step_seconds)).isoformat()
            depth = 2450.0 + idx * 0.5
            defaults = {
                "SPP": 2950.0 + idx * 25.0,
                "WOB": 22.0 + idx * 0.5,
                "HKLD": 185.0 + idx * 1.0,
                "RPM": 120.0 + (idx % 3) * 2.0,
                "FLOWIN": 650.0 + idx * 3.0,
            }
            for ch in self.channels:
                val = defaults.get(ch.mnemonic.upper(), 100.0 + idx)
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

    def append_point(
        self,
        *,
        timestamp: str,
        depth_md: float = 2500.0,
        values: dict[str, float | None],
        quality: str = "good",
    ) -> None:
        ch_by_mnem = {c.mnemonic.upper(): c for c in self.channels}
        for mnem, val in values.items():
            if val is None:
                continue
            ch = ch_by_mnem.get(mnem.upper())
            if ch is not None:
                self.points.append(
                    EtpSamplePoint(
                        channel_id=ch.channel_id,
                        mnemonic=ch.mnemonic,
                        timestamp=timestamp,
                        value=float(val),
                        depth_md=depth_md,
                        quality=quality,
                    )
                )

    async def start(self) -> str:
        if not self.points:
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
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> LocalEtpBinaryWebSocketServer:
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

    async def _handler(self, ws: Any) -> None:
        server_msg_id = 0
        try:
            async for raw in ws:
                if self.fault_mode in {"malformed_binary", "malformed_frame"}:
                    await ws.send(b"\xff\xff\xff\xff\x7ftruncated-avro")
                    return
                if self.fault_mode == "unexpected_text_frame":
                    await ws.send('{"unexpected": "json_text_on_binary_profile"}')
                    return
                if self.fault_mode == "drop_mid_handshake":
                    await ws.close(1011, "Simulated handshake drop")
                    return

                if not isinstance(raw, (bytes, bytearray)):
                    server_msg_id += 1
                    await ws.send(
                        build_protocol_exception_frame(
                            correlation_id=0,
                            message_id=server_msg_id,
                            error={"code": 4, "message": "Binary WebSocket frame required"},
                        )
                    )
                    return

                msg = decode_etp_binary_frame(bytes(raw))
                mtype = msg.get("type")
                client_msg_id = int(msg.get("messageId", 1))

                if mtype == "Acknowledge":
                    self.acks_received += 1
                    continue

                if mtype == "RequestSession":
                    if self.fault_mode == "protocol_exception":
                        server_msg_id += 1
                        await ws.send(
                            build_protocol_exception_frame(
                                correlation_id=client_msg_id,
                                message_id=server_msg_id,
                                error={"code": 3, "message": "Requested ETP binary session denied"},
                            )
                        )
                        return
                    self.sessions_opened += 1
                    server_msg_id += 1
                    await ws.send(
                        build_open_session_frame(
                            correlation_id=client_msg_id,
                            message_id=server_msg_id,
                        )
                    )
                elif mtype == "Ping":
                    server_msg_id += 1
                    await ws.send(
                        build_pong_frame(
                            correlation_id=client_msg_id,
                            message_id=server_msg_id,
                            current_date_time_us=int(msg.get("currentDateTime", 0)),
                        )
                    )
                elif mtype == "StartStreaming":
                    server_msg_id += 1
                    meta_records = [
                        {
                            "uri": ch.channel_uri,
                            "id": ch.channel_id,
                            "channelName": ch.mnemonic,
                            "dataKind": "typeDouble",
                            "uom": ch.uom,
                            "source": ch.source_mnemonic,
                            "indexes": [
                                {
                                    "indexKind": idx.index_kind,
                                    "uom": idx.uom,
                                    "direction": idx.direction,
                                    "name": idx.mnemonic,
                                }
                                for idx in ch.indexes
                            ],
                        }
                        for ch in self.channels
                    ]
                    await ws.send(
                        build_channel_metadata_frame(
                            channels=meta_records,
                            correlation_id=client_msg_id,
                            message_id=server_msg_id,
                            message_flags=MSG_FLAG_FINALPART | MSG_FLAG_ACKNOWLEDGE,
                        )
                    )

                    if self.fault_mode in {"drop_mid_stream", "disconnect_mid_stream"}:
                        await ws.close(1011, "Simulated mid-stream drop")
                        return

                    data_items: list[dict[str, Any]] = []
                    for pt in self.points:
                        try:
                            dt_parsed = _parse_iso_utc(pt.timestamp, require_tz=False)
                        except ValueError:
                            dt_parsed = dt.datetime.now(dt.UTC)
                        ts_us = int(dt_parsed.timestamp() * 1_000_000)
                        data_items.append(
                            {
                                "channelId": pt.channel_id,
                                "indexes": [ts_us, float(pt.depth_md or 0.0)],
                                "value": float(pt.value) if pt.value is not None else None,
                                "valueAttributes": [{"attributeId": 0, "attributeValue": pt.quality}],
                            }
                        )

                    server_msg_id += 1
                    await ws.send(
                        build_channel_data_frame(
                            data_items=data_items,
                            correlation_id=0,
                            message_id=server_msg_id,
                            message_flags=MSG_FLAG_FINALPART,
                        )
                    )
                elif mtype in {"StopStreaming", "CloseSession"}:
                    if mtype == "CloseSession":
                        break
        except ConnectionClosed:
            pass
