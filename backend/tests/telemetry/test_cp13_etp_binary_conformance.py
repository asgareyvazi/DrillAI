"""CP13 Phase 3 & Phase 4 (Checkpoint B): Production ETP 1.2 Binary Avro Profile (`etp.1.2.binary_ws`)
and 25-Scenario Independent Interoperability Conformance Suite.

The reference encoder/decoder in this test module (`_IndependentEtp12BinaryCodec`) is implemented
independently from `drillai.telemetry.protocols.etp_binary` so that client wire framing is verified
against an independent implementation of the Energistics ETP v1.2 Avro binary specification.
"""

from __future__ import annotations

import datetime as dt
import math
import struct
import uuid
from typing import Any

import pytest
import websockets
from httpx import ASGITransport, AsyncClient

from drillai.api.app import create_app
from drillai.core.config import get_settings, reset_settings_cache
from drillai.db.base import Base
from drillai.db.models import Organization, Project, Well, Wellbore
from drillai.telemetry.connectors import (
    ConnectorTransportError,
    register_harness_endpoint,
    unregister_harness_endpoint,
)
from drillai.telemetry.protocols.etp_binary import EtpBinaryWebSocketClient
from drillai.telemetry.worker import ConnectorWorker

# ===========================================================================
# Independent Reference ETP 1.2 Avro Binary Codec (Zero Shared Framing Code)
# ===========================================================================


class _IndependentEtp12BinaryCodec:
    """Standalone reference encoder/decoder for ETP v1.2 Avro binary wire frames."""

    @staticmethod
    def pack_varint(n: int) -> bytes:
        zz = (n << 1) ^ (n >> 63)
        out = bytearray()
        while (zz & ~0x7F) != 0:
            out.append((zz & 0x7F) | 0x80)
            zz >>= 7
        out.append(zz & 0x7F)
        return bytes(out)

    @staticmethod
    def unpack_varint(buf: bytes, offset: int = 0) -> tuple[int, int]:
        shift = 0
        zz = 0
        pos = offset
        while True:
            b = buf[pos]
            pos += 1
            zz |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
        val = (zz >> 1) ^ -(zz & 1)
        return val, pos

    @classmethod
    def pack_str(cls, s: str) -> bytes:
        raw = s.encode("utf-8")
        return cls.pack_varint(len(raw)) + raw

    @classmethod
    def unpack_str(cls, buf: bytes, pos: int) -> tuple[str, int]:
        length, pos = cls.unpack_varint(buf, pos)
        return buf[pos : pos + length].decode("utf-8"), pos + length

    @classmethod
    def pack_header(
        cls,
        protocol: int,
        message_type: int,
        correlation_id: int,
        message_id: int,
        message_flags: int = 0,
    ) -> bytes:
        return (
            cls.pack_varint(protocol)
            + cls.pack_varint(message_type)
            + cls.pack_varint(correlation_id)
            + cls.pack_varint(message_id)
            + cls.pack_varint(message_flags)
        )

    @classmethod
    def unpack_header(cls, buf: bytes) -> tuple[dict[str, int], int]:
        proto, pos = cls.unpack_varint(buf, 0)
        mtype, pos = cls.unpack_varint(buf, pos)
        corr_id, pos = cls.unpack_varint(buf, pos)
        msg_id, pos = cls.unpack_varint(buf, pos)
        flags, pos = cls.unpack_varint(buf, pos)
        return (
            {
                "protocol": proto,
                "messageType": mtype,
                "correlationId": corr_id,
                "messageId": msg_id,
                "messageFlags": flags,
            },
            pos,
        )

    @classmethod
    def validate_and_unpack_client_frame(cls, raw: bytes) -> dict[str, Any]:
        """Validate raw client bytes against ETP v1.2 Avro schemas."""
        assert isinstance(raw, bytes), "Client frame must be binary WebSocket bytes"
        hdr, pos = cls.unpack_header(raw)
        proto = hdr["protocol"]
        mtype = hdr["messageType"]

        if proto == 0 and mtype == 1:  # RequestSession
            app_name, pos = cls.unpack_str(raw, pos)
            app_ver, pos = cls.unpack_str(raw, pos)
            client_uuid = uuid.UUID(bytes=raw[pos : pos + 16])
            pos += 16
            # requestedProtocols array
            req_protos: list[dict[str, Any]] = []
            while True:
                cnt, pos = cls.unpack_varint(raw, pos)
                if cnt == 0:
                    break
                for _ in range(cnt):
                    p_id, pos = cls.unpack_varint(raw, pos)
                    maj, pos = cls.unpack_varint(raw, pos)
                    mino, pos = cls.unpack_varint(raw, pos)
                    rev, pos = cls.unpack_varint(raw, pos)
                    pat, pos = cls.unpack_varint(raw, pos)
                    role, pos = cls.unpack_str(raw, pos)
                    cap_cnt, pos = cls.unpack_varint(raw, pos)
                    assert cap_cnt == 0
                    req_protos.append(
                        {
                            "protocol": p_id,
                            "version": (maj, mino, rev, pat),
                            "role": role,
                        }
                    )
            # supportedDataObjects array
            while True:
                cnt, pos = cls.unpack_varint(raw, pos)
                if cnt == 0:
                    break
                for _ in range(cnt):
                    _qtype, pos = cls.unpack_str(raw, pos)
                    cap_cnt, pos = cls.unpack_varint(raw, pos)
                    assert cap_cnt == 0
            # supportedCompression array
            while True:
                cnt, pos = cls.unpack_varint(raw, pos)
                if cnt == 0:
                    break
                for _ in range(cnt):
                    _, pos = cls.unpack_str(raw, pos)
            # supportedFormats array
            while True:
                cnt, pos = cls.unpack_varint(raw, pos)
                if cnt == 0:
                    break
                for _ in range(cnt):
                    _, pos = cls.unpack_str(raw, pos)
            curr_dt, pos = cls.unpack_varint(raw, pos)
            earliest_dt, pos = cls.unpack_varint(raw, pos)
            auth_req = raw[pos] == 1
            pos += 1
            ep_cap_cnt, pos = cls.unpack_varint(raw, pos)
            assert ep_cap_cnt == 0
            assert pos == len(raw), "RequestSession must have zero trailing bytes"
            return {
                "header": hdr,
                "type": "RequestSession",
                "applicationName": app_name,
                "applicationVersion": app_ver,
                "clientInstanceId": str(client_uuid),
                "requestedProtocols": req_protos,
                "currentDateTime": curr_dt,
                "earliestRetainedChangeTime": earliest_dt,
                "serverAuthorizationRequired": auth_req,
            }

        if proto == 0 and mtype == 8:  # Ping
            curr_dt, pos = cls.unpack_varint(raw, pos)
            assert pos == len(raw)
            return {"header": hdr, "type": "Ping", "currentDateTime": curr_dt}

        if proto == 0 and mtype == 9:  # Pong
            curr_dt, pos = cls.unpack_varint(raw, pos)
            assert pos == len(raw)
            return {"header": hdr, "type": "Pong", "currentDateTime": curr_dt}

        if proto == 0 and mtype == 5:  # CloseSession
            reason, pos = cls.unpack_str(raw, pos)
            assert pos == len(raw)
            return {"header": hdr, "type": "CloseSession", "reason": reason}

        if mtype == 1001:  # Acknowledge
            assert pos == len(raw)
            return {"header": hdr, "type": "Acknowledge"}

        if proto == 1 and mtype == 3:  # StartStreaming
            assert pos == len(raw)
            return {"header": hdr, "type": "StartStreaming"}

        if proto == 1 and mtype == 4:  # StopStreaming
            assert pos == len(raw)
            return {"header": hdr, "type": "StopStreaming"}

        raise AssertionError(f"Unexpected client message ({proto}, {mtype})")

    @classmethod
    def encode_open_session(
        cls,
        *,
        correlation_id: int,
        message_id: int = 1,
        session_uuid: uuid.UUID | None = None,
        protocols: tuple[int, ...] | list[int] = (0, 1),
        message_flags: int = 0,
    ) -> bytes:
        sess = session_uuid or uuid.UUID("11111111-2222-3333-4444-555555555555")
        srv_uuid = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        out = bytearray(cls.pack_header(0, 2, correlation_id, message_id, message_flags))
        out += cls.pack_str("Independent Reference ETP 1.2 Server")
        out += cls.pack_str("1.2.0")
        out += srv_uuid.bytes
        if protocols:
            out += cls.pack_varint(len(protocols))
            for pid in protocols:
                out += cls.pack_varint(pid)
                out += cls.pack_varint(1) + cls.pack_varint(2) + cls.pack_varint(0) + cls.pack_varint(0)
                out += cls.pack_str("server" if pid == 0 else "producer")
                out += cls.pack_varint(0)  # empty protocolCapabilities map
        out += cls.pack_varint(0)  # end of supportedProtocols array
        # supportedDataObjects array (1 item)
        out += cls.pack_varint(1)
        out += cls.pack_str("witsml20.Channel")
        out += cls.pack_varint(0)
        out += cls.pack_varint(0)
        # supportedCompression string
        out += cls.pack_str("")
        # supportedFormats array
        out += cls.pack_varint(1) + cls.pack_str("xml") + cls.pack_varint(0)
        now_us = 1_780_000_000_000_000
        out += cls.pack_varint(now_us) + cls.pack_varint(now_us)
        out += sess.bytes
        out += cls.pack_varint(0)  # endpointCapabilities map
        return bytes(out)

    @classmethod
    def encode_pong(cls, *, correlation_id: int, message_id: int, ts_us: int) -> bytes:
        return cls.pack_header(0, 9, correlation_id, message_id, 0) + cls.pack_varint(ts_us)

    @classmethod
    def encode_ping(cls, *, message_id: int, ts_us: int) -> bytes:
        return cls.pack_header(0, 8, 0, message_id, 0) + cls.pack_varint(ts_us)

    @classmethod
    def encode_protocol_exception(
        cls,
        *,
        correlation_id: int,
        message_id: int,
        single_error: tuple[int, str] | None = None,
        errors_map: dict[str, tuple[int, str]] | None = None,
    ) -> bytes:
        out = bytearray(cls.pack_header(0, 1000, correlation_id, message_id, 0x02))
        if single_error is not None:
            code, msg = single_error
            out += cls.pack_varint(1) + cls.pack_str(msg) + cls.pack_varint(code)
        else:
            out += cls.pack_varint(0)
        if errors_map:
            out += cls.pack_varint(len(errors_map))
            for k, (code, msg) in errors_map.items():
                out += cls.pack_str(k) + cls.pack_str(msg) + cls.pack_varint(code)
        out += cls.pack_varint(0)
        return bytes(out)

    @classmethod
    def encode_channel_metadata(
        cls,
        *,
        channels: list[dict[str, Any]],
        correlation_id: int = 0,
        message_id: int = 2,
        message_flags: int = 0x02,
    ) -> bytes:
        index_kind_map = {
            "DateTime": 0,
            "ElapsedTime": 1,
            "MeasuredDepth": 2,
            "TrueVerticalDepth": 3,
            "PassIndexedDepth": 4,
            "Pressure": 5,
            "Temperature": 6,
            "Scalar": 7,
        }
        out = bytearray(cls.pack_header(1, 1, correlation_id, message_id, message_flags))
        if channels:
            out += cls.pack_varint(len(channels))
            for ch in channels:
                out += cls.pack_str(ch["uri"])
                out += cls.pack_varint(int(ch["id"]))
                indexes = ch.get("indexes") or [{"indexKind": "DateTime", "uom": "us", "name": "TIME"}]
                out += cls.pack_varint(len(indexes))
                for idx in indexes:
                    out += cls.pack_varint(index_kind_map[idx.get("indexKind", "DateTime")])
                    # interval: startIndex=null(0), endIndex=null(0), uom="", depthDatum=""
                    out += cls.pack_varint(0) + cls.pack_varint(0) + cls.pack_str("") + cls.pack_str("")
                    out += cls.pack_varint(0)  # Increasing
                    out += cls.pack_str(idx.get("name", "TIME"))
                    out += cls.pack_str(idx.get("uom", "us"))
                    out += cls.pack_str("")
                    out += cls.pack_str("")
                    out += b"\x01"  # filterable=True
                out += cls.pack_varint(0)  # end indexes array
                out += cls.pack_str(ch["channelName"])
                out += cls.pack_varint(9)  # typeDouble
                out += cls.pack_str(ch.get("uom", "psi"))
                out += cls.pack_str("KB")
                out += cls.pack_str("")
                out += cls.pack_varint(0)  # Active
                out += cls.pack_str(ch.get("source", ch["channelName"]))
                out += cls.pack_varint(0)  # axisVectorLengths
                out += cls.pack_varint(0)  # attributeMetadata
                out += cls.pack_varint(0)  # customData
        out += cls.pack_varint(0)
        return bytes(out)

    @classmethod
    def encode_channel_data(
        cls,
        *,
        items: list[dict[str, Any]],
        correlation_id: int = 0,
        message_id: int = 3,
        message_flags: int = 0x02,
    ) -> bytes:
        out = bytearray(cls.pack_header(1, 2, correlation_id, message_id, message_flags))
        if items:
            out += cls.pack_varint(len(items))
            for item in items:
                out += cls.pack_varint(int(item["channelId"]))
                idxs = item.get("indexes") or []
                if idxs:
                    out += cls.pack_varint(len(idxs))
                    for iv in idxs:
                        if iv is None:
                            out += cls.pack_varint(0)
                        elif isinstance(iv, int) and not isinstance(iv, bool):
                            out += cls.pack_varint(1) + cls.pack_varint(iv)
                        elif isinstance(iv, float):
                            out += cls.pack_varint(2) + struct.pack("<d", iv)
                out += cls.pack_varint(0)  # end indexes

                val = item.get("value")
                if val is None:
                    out += cls.pack_varint(0)
                elif isinstance(val, bool):
                    out += cls.pack_varint(1) + (b"\x01" if val else b"\x00")
                elif isinstance(val, int):
                    out += cls.pack_varint(3) + cls.pack_varint(val)
                elif isinstance(val, float):
                    out += cls.pack_varint(5) + struct.pack("<d", val)
                elif isinstance(val, str):
                    out += cls.pack_varint(6) + cls.pack_str(val)

                quality = item.get("quality")
                if quality:
                    out += cls.pack_varint(1)
                    out += cls.pack_varint(0)  # attributeId=0
                    out += cls.pack_varint(6) + cls.pack_str(str(quality))
                out += cls.pack_varint(0)  # end valueAttributes
        out += cls.pack_varint(0)
        return bytes(out)


def _mappings() -> list[dict[str, Any]]:
    return [
        {
            "source_mnemonic": "SPP",
            "channel_key": "spp",
            "name": "Standpipe Pressure",
            "dimension": "pressure",
            "unit": "psi",
            "channel_uri": "eml://witsml20/Channel(spp)",
        },
        {
            "source_mnemonic": "WOB",
            "channel_key": "wob",
            "name": "Weight on Bit",
            "dimension": "force",
            "unit": "klbf",
            "channel_uri": "eml://witsml20/Channel(wob)",
        },
    ]


# ===========================================================================
# Scenarios 1-25: Independent Binary Conformance & Interoperability Tests
# ===========================================================================


async def test_etp12_binary_scenarios_1_to_11_handshake_exceptions_ack_and_channel_id_remapping():
    """Verify Scenarios 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, and 11 against the independent codec."""
    t0 = dt.datetime(2026, 7, 1, 12, 0, 0, tzinfo=dt.UTC)
    t0_us = int(t0.timestamp() * 1_000_000)

    # Scenario 2: Server that does NOT negotiate 'energistics-tp' subprotocol is rejected
    async def _no_subproto_handler(ws: Any) -> None:
        await ws.recv()

    async with websockets.serve(_no_subproto_handler, "127.0.0.1", 0) as bad_sub_srv:
        bad_port = int(bad_sub_srv.sockets[0].getsockname()[1])
        register_harness_endpoint("127.0.0.1", bad_port)
        try:
            bad_client = EtpBinaryWebSocketClient(
                endpoint_url=f"ws://127.0.0.1:{bad_port}/etp", timeout_seconds=5.0
            )
            with pytest.raises(ConnectorTransportError) as sub_exc:
                await bad_client.probe_session()
            assert sub_exc.value.category == "protocol_error"
            assert "energistics-tp" in str(sub_exc.value)
        finally:
            unregister_harness_endpoint("127.0.0.1", bad_port)

    # Scenarios 1, 3, 4, 5, 6, 7, 8, 9, 10, 11
    mode = "session_1_initial_and_midstream"
    client_frames_decoded: list[dict[str, Any]] = []
    acks_seen = 0

    async def _ref_binary_ws_handler(ws: Any) -> None:
        nonlocal acks_seen
        assert ws.subprotocol == "energistics-tp"
        async for raw in ws:
            msg = _IndependentEtp12BinaryCodec.validate_and_unpack_client_frame(raw)
            client_frames_decoded.append(msg)
            mtype = msg["type"]
            cid = msg["header"]["messageId"]

            if mtype == "Acknowledge":
                acks_seen += 1
                continue

            if mtype == "RequestSession":
                if mode == "bad_correlation":
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_open_session(correlation_id=999, message_id=1)
                    )
                    return
                if mode == "missing_protocol_1":
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_open_session(
                            correlation_id=cid, message_id=1, protocols=[0]
                        )
                    )
                    return
                if mode == "single_error_exception":
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_protocol_exception(
                            correlation_id=cid,
                            message_id=1,
                            single_error=(401, "Invalid bearer token for ETP binary store"),
                        )
                    )
                    return
                if mode == "errors_map_exception":
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_protocol_exception(
                            correlation_id=cid,
                            message_id=1,
                            errors_map={"1": (5, "Unsupported ETP capability requested")},
                        )
                    )
                    return
                # Request Acknowledge (0x10) on OpenSession to test Scenario 8
                await ws.send(
                    _IndependentEtp12BinaryCodec.encode_open_session(
                        correlation_id=cid,
                        message_id=10,
                        message_flags=0x10,
                    )
                )
            elif mtype == "Ping":
                await ws.send(
                    _IndependentEtp12BinaryCodec.encode_pong(
                        correlation_id=cid,
                        message_id=11,
                        ts_us=msg["currentDateTime"],
                    )
                )
                # Also send a server-initiated Ping to verify client replies with binary Pong
                await ws.send(_IndependentEtp12BinaryCodec.encode_ping(message_id=12, ts_us=t0_us))
            elif mtype == "StartStreaming":
                if mode == "session_1_initial_and_midstream":
                    # Scenario 9: Initial ChannelMetadata for SPP with channelId=10 (multipart part 1, flag=0x01)
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_channel_metadata(
                            channels=[
                                {
                                    "uri": "eml://witsml20/Channel(spp)",
                                    "id": 10,
                                    "channelName": "SPP",
                                    "uom": "psi",
                                    "indexes": [{"indexKind": "DateTime", "uom": "us", "name": "TIME"}],
                                }
                            ],
                            correlation_id=cid,
                            message_id=20,
                            message_flags=0x01 | 0x10,  # multipart + acknowledge
                        )
                    )
                    # Scenario 10: Mid-stream dynamic ChannelMetadata addition for WOB (channelId=20)
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_channel_metadata(
                            channels=[
                                {
                                    "uri": "eml://witsml20/Channel(wob)",
                                    "id": 20,
                                    "channelName": "WOB",
                                    "uom": "klbf",
                                    "indexes": [{"indexKind": "DateTime", "uom": "us", "name": "TIME"}],
                                }
                            ],
                            correlation_id=0,
                            message_id=21,
                            message_flags=0x02,
                        )
                    )
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_channel_data(
                            items=[
                                {"channelId": 10, "indexes": [t0_us], "value": 3010.0, "quality": "good"},
                                {"channelId": 20, "indexes": [t0_us], "value": 24.5, "quality": "good"},
                            ],
                            message_id=22,
                            message_flags=0x02,
                        )
                    )
                elif mode == "session_2_reassigned_channel_ids":
                    # Scenario 11: Reconnect where server reassigns SPP->77 and WOB->88!
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_channel_metadata(
                            channels=[
                                {
                                    "uri": "eml://witsml20/Channel(spp)",
                                    "id": 77,
                                    "channelName": "SPP",
                                    "uom": "psi",
                                    "indexes": [{"indexKind": "DateTime", "uom": "us", "name": "TIME"}],
                                },
                                {
                                    "uri": "eml://witsml20/Channel(wob)",
                                    "id": 88,
                                    "channelName": "WOB",
                                    "uom": "klbf",
                                    "indexes": [{"indexKind": "DateTime", "uom": "us", "name": "TIME"}],
                                },
                            ],
                            correlation_id=cid,
                            message_id=30,
                            message_flags=0x02,
                        )
                    )
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_channel_data(
                            items=[
                                # Duplicate of t0_us under new channelId=77 -> must be watermark_suppressed!
                                {"channelId": 77, "indexes": [t0_us], "value": 3010.0, "quality": "good"},
                                # New timestamp t0_us + 5s under new channelId=77 -> must be emitted!
                                {
                                    "channelId": 77,
                                    "indexes": [t0_us + 5_000_000],
                                    "value": 3025.0,
                                    "quality": "good",
                                },
                            ],
                            message_id=31,
                            message_flags=0x02,
                        )
                    )
            elif mtype == "CloseSession":
                break

    async with websockets.serve(
        _ref_binary_ws_handler,
        "127.0.0.1",
        0,
        subprotocols=["energistics-tp"],
    ) as srv:
        port = int(srv.sockets[0].getsockname()[1])
        register_harness_endpoint("127.0.0.1", port)
        url = f"ws://127.0.0.1:{port}/etp12-bin"
        client = EtpBinaryWebSocketClient(endpoint_url=url, timeout_seconds=5.0)
        try:
            # Session 1: Handshake, Acknowledge, Ping/Pong, initial + mid-stream ChannelMetadata, ChannelData
            batch1 = await client.poll_subscription_batch(
                well_id="wel_bin_1",
                config={"channel_mappings": _mappings()},
                watermark={},
            )
            assert batch1.session_id == "11111111-2222-3333-4444-555555555555"
            assert batch1.ping_pong_verified is True
            assert len(batch1.frames) == 2
            assert {f.channel_key for f in batch1.frames} == {"spp", "wob"}
            assert batch1.disposition.reconciled is True
            assert acks_seen >= 2
            sent_types = [m["type"] for m in client_frames_decoded]
            assert "RequestSession" in sent_types
            assert "Acknowledge" in sent_types
            assert "Pong" in sent_types
            assert "StartStreaming" in sent_types
            assert "StopStreaming" in sent_types
            assert "CloseSession" in sent_types

            # Scenario 11: Session 2 reconnect with reassigned numeric channelId (10->77, 20->88)
            mode = "session_2_reassigned_channel_ids"
            batch2 = await client.poll_subscription_batch(
                well_id="wel_bin_1",
                config={"channel_mappings": _mappings()},
                watermark=batch1.next_watermark,
            )
            assert len(batch2.frames) == 1
            assert batch2.frames[0].channel_key == "spp"
            assert batch2.frames[0].value == 3025.0
            assert batch2.disposition.watermark_suppressed == 1
            assert batch2.disposition.reconciled is True

            # Scenario 4: OpenSession with mismatched correlationId
            mode = "bad_correlation"
            with pytest.raises(ConnectorTransportError) as corr_exc:
                await client.poll_subscription_batch(
                    well_id="wel_bin_1", config={"channel_mappings": _mappings()}
                )
            assert corr_exc.value.category == "protocol_error"
            assert "correlationId" in str(corr_exc.value)

            # Scenario 5: OpenSession missing Protocol 1 support
            mode = "missing_protocol_1"
            with pytest.raises(ConnectorTransportError) as proto1_exc:
                await client.poll_subscription_batch(
                    well_id="wel_bin_1", config={"channel_mappings": _mappings()}
                )
            assert proto1_exc.value.category == "protocol_error"
            assert "Protocol 1" in str(proto1_exc.value)

            # Scenario 6: ProtocolException with single `error` (401 auth failure)
            mode = "single_error_exception"
            with pytest.raises(ConnectorTransportError) as err1_exc:
                await client.poll_subscription_batch(
                    well_id="wel_bin_1", config={"channel_mappings": _mappings()}
                )
            assert err1_exc.value.category == "auth_failure"

            # Scenario 7: ProtocolException with `errors` map
            mode = "errors_map_exception"
            with pytest.raises(ConnectorTransportError) as err2_exc:
                await client.poll_subscription_batch(
                    well_id="wel_bin_1", config={"channel_mappings": _mappings()}
                )
            assert err2_exc.value.category == "protocol_error"
            assert "Unsupported ETP capability" in str(err2_exc.value)
        finally:
            unregister_harness_endpoint("127.0.0.1", port)


async def test_etp12_binary_scenarios_12_to_22_indexes_depths_dispositions_revisions_and_caps():
    """Verify Scenarios 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, and 22."""
    t0 = dt.datetime(2026, 7, 2, 8, 0, 0, tzinfo=dt.UTC)
    t0_s = int(t0.timestamp())
    t0_ms = t0_s * 1_000
    t0_us = t0_s * 1_000_000
    t0_ns = t0_s * 1_000_000_000

    scenario_mode = "multi_index_and_dispositions"

    async def _handler(ws: Any) -> None:
        async for raw in ws:
            msg = _IndependentEtp12BinaryCodec.validate_and_unpack_client_frame(raw)
            mtype = msg["type"]
            cid = msg["header"]["messageId"]
            if mtype == "RequestSession":
                await ws.send(_IndependentEtp12BinaryCodec.encode_open_session(correlation_id=cid))
            elif mtype == "Ping":
                await ws.send(
                    _IndependentEtp12BinaryCodec.encode_pong(
                        correlation_id=cid, message_id=2, ts_us=msg["currentDateTime"]
                    )
                )
            elif mtype == "StartStreaming":
                if scenario_mode == "depth_only_primary_index":
                    # Scenario 15: Unsupported MeasuredDepth primary index
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_channel_metadata(
                            channels=[
                                {
                                    "uri": "eml://witsml20/Channel(spp)",
                                    "id": 1,
                                    "channelName": "SPP",
                                    "uom": "psi",
                                    "indexes": [{"indexKind": "MeasuredDepth", "uom": "m", "name": "MD"}],
                                }
                            ]
                        )
                    )
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_channel_data(
                            items=[{"channelId": 1, "indexes": [2500.0], "value": 3000.0}]
                        )
                    )
                elif scenario_mode == "multi_index_and_dispositions":
                    # Scenarios 12, 13, 14, 16, 17, 18:
                    # - ch 1 (SPP): ms DateTime primary index + secondary MeasuredDepth in ft
                    # - ch 2 (WOB): ns ElapsedTime primary index + secondary MeasuredDepth in m
                    # - ch 3 (UNMAPPED): unmapped channel
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_channel_metadata(
                            channels=[
                                {
                                    "uri": "eml://witsml20/Channel(spp)",
                                    "id": 1,
                                    "channelName": "SPP",
                                    "uom": "psi",
                                    "indexes": [
                                        {"indexKind": "DateTime", "uom": "ms", "name": "TIME"},
                                        {"indexKind": "MeasuredDepth", "uom": "ft", "name": "MD"},
                                    ],
                                },
                                {
                                    "uri": "eml://witsml20/Channel(wob)",
                                    "id": 2,
                                    "channelName": "WOB",
                                    "uom": "klbf",
                                    "indexes": [
                                        {"indexKind": "ElapsedTime", "uom": "ns", "name": "ELAPSED"},
                                        {"indexKind": "MeasuredDepth", "uom": "m", "name": "MD"},
                                    ],
                                },
                                {
                                    "uri": "eml://witsml20/Channel(unmapped)",
                                    "id": 3,
                                    "channelName": "UNMAPPED_CH",
                                    "uom": "rpm",
                                    "indexes": [{"indexKind": "DateTime", "uom": "us", "name": "TIME"}],
                                },
                            ]
                        )
                    )
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_channel_data(
                            items=[
                                # Valid SPP with ms timestamp + 1000 ft secondary depth (-> 304.8 m)
                                {"channelId": 1, "indexes": [t0_ms, 1000.0], "value": 2950.0, "quality": "good"},
                                # Valid WOB with ns timestamp + 500 m secondary depth
                                {"channelId": 2, "indexes": [t0_ns, 500.0], "value": 22.0, "quality": "good"},
                                # Scenario 17: Null value on SPP at t0 + 5s
                                {"channelId": 1, "indexes": [t0_ms + 5000, 1005.0], "value": None},
                                # Scenario 16: Boolean value rejected
                                {"channelId": 1, "indexes": [t0_ms + 10000, 1010.0], "value": True},
                                # Scenario 16: NaN value rejected
                                {"channelId": 1, "indexes": [t0_ms + 15000, 1015.0], "value": float("nan")},
                                # Scenario 16: Inf value rejected
                                {"channelId": 1, "indexes": [t0_ms + 20000, 1020.0], "value": float("inf")},
                                # Scenario 16: String value rejected
                                {"channelId": 1, "indexes": [t0_ms + 25000, 1025.0], "value": "bad_num"},
                                # Scenario 18: Unmapped channel (id=3)
                                {"channelId": 3, "indexes": [t0_us], "value": 120.0},
                                # Scenario 18: Unknown channelId (id=999)
                                {"channelId": 999, "indexes": [t0_us], "value": 120.0},
                            ]
                        )
                    )
                elif scenario_mode == "fast_slow_revision_and_cap":
                    # Scenarios 19, 20, 21, 22:
                    # SPP (fast channel at t0 + 60s) already ahead of WOB (slow channel at t0).
                    # Send:
                    # 1. Exact duplicate of SPP at t0 -> watermark_suppressed
                    # 2. Revised value of SPP at t0 (2950.0 -> 3100.0) -> emitted!
                    # 3. Slow channel WOB at t0 + 10s (behind SPP's t0 + 60s cursor, ahead of WOB's t0 cursor) -> emitted!
                    # 4. Two more points when max_frames=2 -> deferred_over_cap!
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_channel_metadata(
                            channels=[
                                {
                                    "uri": "eml://witsml20/Channel(spp)",
                                    "id": 1,
                                    "channelName": "SPP",
                                    "uom": "psi",
                                    "indexes": [{"indexKind": "DateTime", "uom": "us", "name": "TIME"}],
                                },
                                {
                                    "uri": "eml://witsml20/Channel(wob)",
                                    "id": 2,
                                    "channelName": "WOB",
                                    "uom": "klbf",
                                    "indexes": [{"indexKind": "DateTime", "uom": "us", "name": "TIME"}],
                                },
                            ]
                        )
                    )
                    await ws.send(
                        _IndependentEtp12BinaryCodec.encode_channel_data(
                            items=[
                                # 1. Duplicate of SPP at t0 -> suppressed
                                {"channelId": 1, "indexes": [t0_us], "value": 2950.0},
                                # 2. Revised value of SPP at t0 -> emitted (frame 1 of 2)
                                {"channelId": 1, "indexes": [t0_us], "value": 3100.0},
                                # 3. Slow channel WOB at t0 + 10s -> emitted (frame 2 of 2)
                                {"channelId": 2, "indexes": [t0_us + 10_000_000], "value": 25.0},
                                # 4. Over-cap item -> deferred_over_cap
                                {"channelId": 1, "indexes": [t0_us + 70_000_000], "value": 3200.0},
                            ]
                        )
                    )
            elif mtype == "CloseSession":
                break

    async with websockets.serve(
        _handler, "127.0.0.1", 0, subprotocols=["energistics-tp"]
    ) as srv:
        port = int(srv.sockets[0].getsockname()[1])
        register_harness_endpoint("127.0.0.1", port)
        url = f"ws://127.0.0.1:{port}/etp12-bin"
        client = EtpBinaryWebSocketClient(endpoint_url=url, timeout_seconds=5.0)
        try:
            # Test Scenarios 12, 13, 14, 16, 17, 18
            batch = await client.poll_subscription_batch(
                well_id="wel_bin_2",
                config={"channel_mappings": _mappings()},
                watermark={},
            )
            assert len(batch.frames) == 3  # SPP valid, WOB valid, SPP null
            spp_frame = batch.frames[0]
            wob_frame = batch.frames[1]
            null_frame = batch.frames[2]
            assert math.isclose(spp_frame.depth_md_si or 0.0, 304.8, rel_tol=1e-5)
            assert math.isclose(wob_frame.depth_md_si or 0.0, 500.0, rel_tol=1e-5)
            assert null_frame.value is None and null_frame.quality == "missing"

            disp = batch.disposition
            assert disp.source_records_discovered == 9
            assert disp.mapped_frames_emitted == 3
            assert disp.rejected_invalid_value == 2  # bool + string
            assert disp.rejected_non_finite_value == 2  # NaN + Inf
            assert disp.unmapped_skipped == 1
            assert disp.rejected_unknown_channel == 1
            assert disp.reconciled is True

            # Test Scenario 15: Depth-only primary index rejected in disposition
            scenario_mode = "depth_only_primary_index"
            depth_batch = await client.poll_subscription_batch(
                well_id="wel_bin_2",
                config={"channel_mappings": _mappings()},
                watermark={},
            )
            assert len(depth_batch.frames) == 0
            assert depth_batch.disposition.rejected_unsupported_index == 1
            assert depth_batch.disposition.reconciled is True

            # Test Scenarios 19, 20, 21, 22: Fast/slow channel watermarks, revisions, and max_frames cap
            scenario_mode = "fast_slow_revision_and_cap"
            seed_wm = {
                "protocol": "etp.1.2.binary_ws",
                "cursor_timestamp": (t0 + dt.timedelta(seconds=60)).isoformat(),
                "channel_cursors": {
                    "spp": {
                        "last_timestamp": (t0 + dt.timedelta(seconds=60)).isoformat(),
                        "recent_points": {f"etp_bin:spp:{t0.isoformat()}": 2950.0},
                    },
                    "wob": {
                        "last_timestamp": t0.isoformat(),
                        "recent_points": {f"etp_bin:wob:{t0.isoformat()}": 22.0},
                    },
                },
            }
            cap_batch = await client.poll_subscription_batch(
                well_id="wel_bin_2",
                config={"channel_mappings": _mappings()},
                watermark=seed_wm,
                max_frames=2,
            )
            assert len(cap_batch.frames) == 2
            assert cap_batch.frames[0].channel_key == "spp"
            assert cap_batch.frames[0].value == 3100.0  # revised historical value emitted!
            assert cap_batch.frames[1].channel_key == "wob"
            assert cap_batch.frames[1].value == 25.0  # slow channel not suppressed by fast channel!
            assert cap_batch.disposition.watermark_suppressed == 1
            assert cap_batch.disposition.deferred_over_cap == 1
            assert cap_batch.disposition.truncated_by_limit is True
            assert cap_batch.disposition.reconciled is True
            # Ensure SPP cursor did NOT advance to the deferred t0+70s item!
            assert cap_batch.next_watermark["channel_cursors"]["spp"]["last_timestamp"] == (
                t0 + dt.timedelta(seconds=60)
            ).isoformat()
        finally:
            unregister_harness_endpoint("127.0.0.1", port)


async def test_etp12_binary_scenarios_23_24_25_adversarial_frames_and_end_to_end_worker_ingestion(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify Scenarios 23, 24, and 25:
    - Scenario 23: Truncated binary header, truncated binary body, and trailing garbage bytes fail closed.
    - Scenario 24: Oversized frame / malicious varint length and unexpected text WebSocket frame fail closed.
    - Scenario 25: Full `etp.1.2.binary_ws` connector registration, connection test milestones,
      worker poll ingestion through `TelemetryService`, and profile capability honesty.
    """
    adv_payload: bytes | str = b""

    async def _adv_ws_handler(ws: Any) -> None:
        await ws.recv()
        await ws.send(adv_payload)

    async with websockets.serve(
        _adv_ws_handler, "127.0.0.1", 0, subprotocols=["energistics-tp"]
    ) as adv_srv:
        adv_port = int(adv_srv.sockets[0].getsockname()[1])
        register_harness_endpoint("127.0.0.1", adv_port)
        client = EtpBinaryWebSocketClient(
            endpoint_url=f"ws://127.0.0.1:{adv_port}/etp12-adv", timeout_seconds=5.0
        )
        try:
            # 23a: Truncated binary header (only 2 bytes)
            adv_payload = b"\x00\x04"
            with pytest.raises(ConnectorTransportError) as exc_trunc_hdr:
                await client.probe_session()
            assert exc_trunc_hdr.value.category == "malformed_payload"

            # 23b: Truncated binary body (header says OpenSession, body truncated mid-UUID)
            adv_payload = _IndependentEtp12BinaryCodec.pack_header(0, 2, 1, 1, 0) + b"\x04test\x041.2"
            with pytest.raises(ConnectorTransportError) as exc_trunc_body:
                await client.probe_session()
            assert exc_trunc_body.value.category == "malformed_payload"

            # 23c: Trailing garbage bytes after valid OpenSession
            adv_payload = (
                _IndependentEtp12BinaryCodec.encode_open_session(correlation_id=1) + b"\xde\xad\xbe\xef"
            )
            with pytest.raises(ConnectorTransportError) as exc_trailing:
                await client.probe_session()
            assert exc_trailing.value.category == "malformed_payload"
            assert "trailing" in str(exc_trailing.value).lower()

            # 24a: Malicious varint claiming 100 MB string length inside a tiny frame
            adv_payload = _IndependentEtp12BinaryCodec.pack_header(0, 2, 1, 1, 0) + (
                _IndependentEtp12BinaryCodec.pack_varint(100_000_000)
            )
            with pytest.raises(ConnectorTransportError) as exc_oversize_str:
                await client.probe_session()
            assert exc_oversize_str.value.category in {"malformed_payload", "oversized_payload"}

            # 24b: Unexpected text WebSocket frame on binary profile
            adv_payload = '{"header":{"protocol":0,"messageType":2},"body":{"type":"OpenSession"}}'
            with pytest.raises(ConnectorTransportError) as exc_text:
                await client.probe_session()
            assert exc_text.value.category == "protocol_error"
            assert "text WebSocket frame" in str(exc_text.value)
        finally:
            unregister_harness_endpoint("127.0.0.1", adv_port)

    # Scenario 25: Full end-to-end API + Worker + TelemetryService ingestion for `etp.1.2.binary_ws`
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/etp_bin_e2e.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)
    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id = "org_etp_bin"
    well_id = "wel_etp_bin"
    wellbore_id = "wbr_etp_bin"
    async with app.state.database.session() as session:
        session.add(Organization(id=org_id, slug=settings.dev_org_slug, name="ETP Binary Org"))
        session.add(Project(id="prj_etp_bin", org_id=org_id, name="ETP Binary Project"))
        session.add(Well(id=well_id, org_id=org_id, project_id="prj_etp_bin", name="ETP Binary Well"))
        session.add(Wellbore(id=wellbore_id, org_id=org_id, well_id=well_id, name="Main Wellbore"))
        await session.commit()

    worker = ConnectorWorker(app.state.database, worker_id="wrk_etp_bin")
    app.state.connector_worker = worker

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            hdrs = {"X-Dev-Roles": "engineer,admin"}

            # 1. Inspect profiles: verify etp.1.2.binary_ws vs etp.1.2.json_ws honesty
            prof_res = await http.get("/api/v1/connectors/profiles", headers=hdrs)
            assert prof_res.status_code == 200
            profiles_by_id = {p["profile"]: p for p in prof_res.json()["profiles"]}
            bin_spec = profiles_by_id["etp.1.2.binary_ws"]
            json_spec = profiles_by_id["etp.1.2.json_ws"]
            assert bin_spec["wire_encoding"] == "avro_binary"
            assert bin_spec["production_supported"] is True
            assert bin_spec["is_test_only_encoding"] is False
            assert bin_spec["conformance_verified"] is True
            assert bin_spec["external_vendor_verified"] is False
            assert json_spec["is_test_only_encoding"] is True
            assert json_spec["production_supported"] is False

            # 2. Ensure local harness starts `etp_binary` server
            harness_res = await http.post("/api/v1/connectors/harness/ensure", headers=hdrs)
            assert harness_res.status_code == 200
            harness_data = harness_res.json()
            assert harness_data["etp_binary"]["running"] is True
            etp_bin_url = harness_data["etp_binary"]["endpoint_url"]
            etp_bin_secrets = harness_data["etp_binary"]["secret_refs"]

            # 3. Register `etp.1.2.binary_ws` connector
            create_res = await http.post(
                "/api/v1/connectors",
                headers=hdrs,
                json={
                    "key": "etp-12-binary-prod",
                    "name": "ETP 1.2 Binary Avro Stream",
                    "protocol_profile": "etp.1.2.binary_ws",
                    "well_id": well_id,
                    "wellbore_id": wellbore_id,
                    "endpoint_url": etp_bin_url,
                    "secret_refs": etp_bin_secrets,
                    "config": {
                        "auth_mode": "bearer",
                        "poll_interval_seconds": 5.0,
                        "max_points_per_poll": 250,
                        "channel_mappings": _mappings(),
                    },
                },
            )
            assert create_res.status_code == 201
            cid = create_res.json()["id"]
            assert create_res.json()["wire_encoding"] == "avro_binary"
            assert create_res.json()["is_test_only_encoding"] is False

            # 4. Run connection test and verify all 10 milestones are reported
            test_res = await http.post(f"/api/v1/connectors/{cid}/test", headers=hdrs)
            assert test_res.status_code == 200
            test_body = test_res.json()
            assert test_body["ok"] is True
            assert test_body["wire_encoding"] == "avro_binary"
            stages = [m["stage"] for m in test_body["milestones"]]
            assert stages == [
                "endpoint_policy_validated",
                "socket_connected",
                "tls_verified",
                "protocol_encoding_negotiated",
                "session_established",
                "channels_discovered",
                "mapping_verified",
                "data_frame_decoded",
                "disposition_validated",
                "transaction_committed",
            ]

            # 5. Start and poll connector -> verify points land in TelemetryService with SI conversion
            await http.post(f"/api/v1/connectors/{cid}/start", headers=hdrs, json={"reason": "start binary"})
            poll_res = await http.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)
            assert poll_res.status_code == 200
            polled = poll_res.json()
            assert polled["connector"]["cursor"]["protocol"] == "etp.1.2.binary_ws"
            assert polled["connector"]["health"]["is_live"] is True
            assert polled["outcome"]["points_accepted"] == 10  # 5 timestamps x 2 mapped channels (SPP, WOB)
            runs_res = await http.get(f"/api/v1/connectors/{cid}/runs", headers=hdrs)
            assert runs_res.status_code == 200
            latest_run = runs_res.json()["items"][0]
            assert latest_run["points_accepted"] == 10
            assert latest_run["manifest"]["reconciled"] is True
            assert latest_run["checkpoint_status"] == "complete"
    finally:
        if getattr(app.state, "protocol_harness", None) is not None:
            await app.state.protocol_harness.stop()
        await app.state.database.dispose()
        reset_settings_cache()


async def test_etp12_production_profile_honesty_blocks_json_ws_and_allows_binary_ws(
    monkeypatch: pytest.MonkeyPatch,
):
    """In production (`DRILLAI_ENVIRONMENT=production`), `etp.1.2.json_ws` is rejected by
    endpoint policy because it is an internal-test/debug JSON profile, whereas
    `etp.1.2.binary_ws` with `wss://` and a public IP is accepted.
    """
    from drillai.core.errors import ValidationFailed
    from drillai.telemetry.connectors import validate_connector_endpoint

    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "production")
    reset_settings_cache()
    try:
        with pytest.raises(ValidationFailed) as json_exc:
            validate_connector_endpoint(
                "wss://93.184.216.34:8443/etp12",
                "etp.1.2.json_ws",
            )
        assert "internal/test-only profile" in str(json_exc.value)
        assert "etp.1.2.binary_ws" in str(json_exc.value)

        # Production binary ETP over wss:// to public IP is accepted
        cleaned = validate_connector_endpoint(
            "wss://93.184.216.34:8443/etp12",
            "etp.1.2.binary_ws",
        )
        assert cleaned == "wss://93.184.216.34:8443/etp12"
    finally:
        reset_settings_cache()

