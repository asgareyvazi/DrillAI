"""CP14-E regression tests: ETP 1.2 Binary Avro normative correctness, golden vectors,
strict index metadata validation, compression fail-closed checks, and TruncateChannels handling.

Covers:
1. Versioned official ETP v1.2 Avro schema & byte-exact golden wire vector verification
   (`backend/tests/telemetry/fixtures/etp12_official_schemas_and_vectors.json`).
2. Strict ``ChannelMetadataRecord.indexes`` validation:
   - Never silently invents a ``DateTime`` (``us``) primary index when ``indexes`` is empty or
     missing ``indexKind``/``uom``.
   - Rejects ``DataItem`` frames on channels without declared index metadata as
     ``rejected_unsupported_index`` (``reason="missing_channel_index_metadata"``) while preserving
     manifest reconciliation.
3. Fail-closed rejection of ``MSG_FLAG_COMPRESSED (0x08)`` and unrequested ``OpenSession``
   ``supportedCompression`` / ``supportedFormats``.
4. Normative ``TruncateChannels`` handling during ``poll_subscription_batch()`` with exact
   disposition conservation law preservation.
5. Honest capability & provenance disclosure on ``PROTOCOL_PROFILE_SPECS["etp.1.2.binary_ws"]``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import struct
from typing import Any
import uuid

import pytest
import websockets

from drillai.core.config import reset_settings_cache
from drillai.telemetry.connectors import (
    PROTOCOL_PROFILE_SPECS,
    ConnectorTransportError,
    build_and_reconcile_poll_manifest,
    clear_harness_endpoints,
    register_harness_endpoint,
    reset_egress_policy,
)
from drillai.telemetry.protocols.etp_binary import (
    ACTIVE_STATUS_KINDS,
    CHANNEL_DATA_KINDS,
    CHANNEL_INDEX_KINDS,
    INDEX_DIRECTIONS,
    MSG_FLAG_COMPRESSED,
    MSG_FLAG_FINALPART,
    PASS_DIRECTIONS,
    AvroBinaryDecodeError,
    AvroBinaryWriter,
    EtpBinaryWebSocketClient,
    EtpMessageHeader,
    _write_array,
    build_acknowledge_frame,
    build_channel_data_frame,
    build_channel_metadata_frame,
    build_close_session_frame,
    build_open_session_frame,
    build_ping_frame,
    build_pong_frame,
    build_protocol_exception_frame,
    build_request_session_frame,
    build_start_streaming_frame,
    build_stop_streaming_frame,
    decode_etp_binary_frame,
    encode_index_value,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "etp12_official_schemas_and_vectors.json"


class _IndependentSchemaAvroVerifier:
    """Independent recursive schema-driven Avro datum decoder driven purely by official ETP 1.2 JSON schemas."""

    def __init__(self, artifact: dict[str, Any], raw: bytes) -> None:
        self._artifact = artifact
        self._buf = memoryview(raw)
        self.offset = 0

    def _read_byte(self) -> int:
        if self.offset >= len(self._buf):
            raise AssertionError("Unexpected EOF in Avro verifier")
        b = int(self._buf[self.offset])
        self.offset += 1
        return b

    def _read_varlong(self) -> int:
        shift = 0
        acc = 0
        while True:
            b = self._read_byte()
            acc |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
        return (acc >> 1) ^ -(acc & 1)

    def _read_bytes(self, n: int) -> bytes:
        if self.offset + n > len(self._buf):
            raise AssertionError(f"Unexpected EOF reading {n} bytes")
        out = bytes(self._buf[self.offset : self.offset + n])
        self.offset += n
        return out

    def decode_type(self, type_name: str) -> Any:
        if type_name == "null":
            return None
        if type_name == "boolean":
            return bool(self._read_byte())
        if type_name in ("int", "long"):
            return self._read_varlong()
        if type_name == "float":
            return struct.unpack("<f", self._read_bytes(4))[0]
        if type_name == "double":
            return struct.unpack("<d", self._read_bytes(8))[0]
        if type_name == "bytes":
            n = self._read_varlong()
            return self._read_bytes(n)
        if type_name == "string":
            n = self._read_varlong()
            return self._read_bytes(n).decode("utf-8")
        if type_name.startswith("array<") and type_name.endswith(">"):
            item_type = type_name[6:-1]
            items: list[Any] = []
            while True:
                count = self._read_varlong()
                if count == 0:
                    break
                if count < 0:
                    count = -count
                    _ = self._read_varlong()
                for _ in range(count):
                    items.append(self.decode_type(item_type))
            return items
        if type_name.startswith("map<") and type_name.endswith(">"):
            val_type = type_name[4:-1]
            out_map: dict[str, Any] = {}
            while True:
                count = self._read_varlong()
                if count == 0:
                    break
                if count < 0:
                    count = -count
                    _ = self._read_varlong()
                for _ in range(count):
                    k = self.decode_type("string")
                    out_map[k] = self.decode_type(val_type)
            return out_map
        if type_name in self._artifact.get("fixed", {}):
            size = int(self._artifact["fixed"][type_name])
            raw_fixed = self._read_bytes(size)
            if type_name.endswith(".Uuid") and size == 16:
                return str(uuid.UUID(bytes=raw_fixed))
            return raw_fixed
        if type_name in self._artifact.get("enums", {}):
            symbols = self._artifact["enums"][type_name]
            idx = self._read_varlong()
            return symbols[idx]
        if type_name in self._artifact.get("unions", {}):
            branches = self._artifact["unions"][type_name]
            branch_idx = self._read_varlong()
            return self.decode_type(branches[branch_idx])
        if type_name in self._artifact.get("records", {}):
            fields = self._artifact["records"][type_name]
            rec: dict[str, Any] = {}
            for f in fields:
                rec[f["name"]] = self.decode_type(f["type"])
            return rec
        raise AssertionError(f"Unknown Avro schema type in verifier: {type_name}")

    def decode_frame(self) -> tuple[dict[str, Any], str, dict[str, Any]]:
        header = self.decode_type("Energistics.Etp.v12.Datatypes.MessageHeader")
        dispatch_key = f"{header['protocol']}:{header['messageType']}"
        body_schema = self._artifact["message_dispatch"][dispatch_key]
        body = self.decode_type(body_schema)
        assert self.offset == len(self._buf), (
            f"Trailing unconsumed bytes in {body_schema}: {self.offset} != {len(self._buf)}"
        )
        return header, body_schema, body


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    reset_settings_cache()
    reset_egress_policy()
    clear_harness_endpoints()
    yield
    clear_harness_endpoints()
    reset_egress_policy()
    reset_settings_cache()


def test_official_etp12_schema_artifact_and_golden_wire_vectors() -> None:
    """Verify official ETP 1.2 Avro enum symbol tables, disclosure flags, and byte-exact golden vectors."""
    artifact = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert artifact["standard"] == "Energistics Transfer Protocol (ETP)"
    assert artifact["version"] == "1.2.0"

    disclosure = artifact["interoperability_disclosure"]
    assert disclosure["conformance_verified"] is True
    assert disclosure["independent_peer_verified"] is False
    assert disclosure["external_vendor_verified"] is False

    enums = artifact["enums"]
    assert list(CHANNEL_INDEX_KINDS) == enums["Energistics.Etp.v12.Datatypes.ChannelData.ChannelIndexKind"]
    assert list(INDEX_DIRECTIONS) == enums["Energistics.Etp.v12.Datatypes.ChannelData.IndexDirection"]
    assert list(CHANNEL_DATA_KINDS) == enums["Energistics.Etp.v12.Datatypes.ChannelData.ChannelDataKind"]
    assert list(ACTIVE_STATUS_KINDS) == enums["Energistics.Etp.v12.Datatypes.Object.ActiveStatusKind"]
    assert list(PASS_DIRECTIONS) == enums["Energistics.Etp.v12.Datatypes.PassDirection"]

    # Verify byte-exact encoding and decoding against golden wire vectors
    vec_by_name = {v["name"]: v for v in artifact["golden_vectors"]}

    ack_bytes = build_acknowledge_frame(protocol=1, correlation_id=7, message_id=8)
    assert ack_bytes.hex() == vec_by_name["Acknowledge_proto1_corr7_msg8"]["hex"]

    start_bytes = build_start_streaming_frame(message_id=2, correlation_id=0)
    assert start_bytes.hex() == vec_by_name["StartStreaming_proto1_msg2"]["hex"]

    stop_bytes = build_stop_streaming_frame(message_id=5, correlation_id=0)
    assert stop_bytes.hex() == vec_by_name["StopStreaming_proto1_msg5"]["hex"]

    close_bytes = build_close_session_frame(reason="Normal closure", message_id=9, correlation_id=0)
    assert close_bytes.hex() == vec_by_name["CloseSession_proto0_msg9_normal"]["hex"]

    for vec in artifact["golden_vectors"]:
        raw = bytes.fromhex(vec["hex"])
        decoded = decode_etp_binary_frame(raw)
        assert decoded["protocol"] == vec["protocol"]
        assert decoded["messageType"] == vec["messageType"]
        assert decoded["correlationId"] == vec["correlationId"]
        assert decoded["messageId"] == vec["messageId"]
        assert decoded["messageFlags"] == vec["messageFlags"]
        assert decoded["type"] == vec["expected_type"]
        if "expected_reason" in vec:
            assert decoded["reason"] == vec["expected_reason"]
        # Also verify via independent recursive schema-driven Avro decoder
        hdr_v, schema_v, body_v = _IndependentSchemaAvroVerifier(artifact, raw).decode_frame()
        assert hdr_v["protocol"] == vec["protocol"]
        assert hdr_v["messageType"] == vec["messageType"]
        assert schema_v.endswith(f".{vec['expected_type']}")
        if "expected_reason" in vec:
            assert body_v["reason"] == vec["expected_reason"]

    # Independently verify complex ETP 1.2 binary messages against official JSON record/union/enum/fixed schemas:
    req_frame = build_request_session_frame(message_id=1)
    _, req_schema, req_body = _IndependentSchemaAvroVerifier(artifact, req_frame).decode_frame()
    assert req_schema == "Energistics.Etp.v12.Protocol.Core.RequestSession"
    assert req_body["applicationName"] == "DrillAI Telemetry Worker"
    assert [p["protocol"] for p in req_body["requestedProtocols"]] == [0, 1]

    open_frame = build_open_session_frame(correlation_id=1, message_id=2)
    _, open_schema, open_body = _IndependentSchemaAvroVerifier(artifact, open_frame).decode_frame()
    assert open_schema == "Energistics.Etp.v12.Protocol.Core.OpenSession"
    assert [p["protocol"] for p in open_body["supportedProtocols"]] == [0, 1]

    ping_frame = build_ping_frame(message_id=3)
    _, ping_schema, ping_body = _IndependentSchemaAvroVerifier(artifact, ping_frame).decode_frame()
    assert ping_schema == "Energistics.Etp.v12.Protocol.Core.Ping"
    assert isinstance(ping_body["currentDateTime"], int)

    pong_frame = build_pong_frame(correlation_id=3, message_id=4)
    _, pong_schema, pong_body = _IndependentSchemaAvroVerifier(artifact, pong_frame).decode_frame()
    assert pong_schema == "Energistics.Etp.v12.Protocol.Core.Pong"
    assert isinstance(pong_body["currentDateTime"], int)

    exc_frame = build_protocol_exception_frame(
        error={"code": 4, "message": "Invalid argument"},
        correlation_id=1,
        message_id=5,
    )
    _, exc_schema, exc_body = _IndependentSchemaAvroVerifier(artifact, exc_frame).decode_frame()
    assert exc_schema == "Energistics.Etp.v12.Protocol.Core.ProtocolException"
    assert exc_body["error"]["code"] == 4
    assert exc_body["error"]["message"] == "Invalid argument"

    meta_frame = build_channel_metadata_frame(
        channels=[
            {
                "uri": "eml:///witsml20/Channel(spp)",
                "id": 20,
                "indexes": [
                    {
                        "indexKind": "DateTime",
                        "uom": "us",
                        "direction": "Increasing",
                        "name": "TIME",
                    }
                ],
                "channelName": "SPP",
                "dataKind": "typeDouble",
                "uom": "psi",
                "source": "SPP",
            }
        ],
        correlation_id=2,
        message_id=6,
    )
    _, meta_schema, meta_body = _IndependentSchemaAvroVerifier(artifact, meta_frame).decode_frame()
    assert meta_schema == "Energistics.Etp.v12.Protocol.ChannelStreaming.ChannelMetadata"
    assert len(meta_body["channels"]) == 1
    assert meta_body["channels"][0]["channelName"] == "SPP"
    assert meta_body["channels"][0]["indexes"][0]["indexKind"] == "DateTime"

    data_frame = build_channel_data_frame(
        data_items=[
            {"channelId": 20, "indexes": [1_767_225_600_000_000, 2500.5], "value": 2950.25},
        ],
        correlation_id=2,
        message_id=7,
    )
    _, data_schema, data_body = _IndependentSchemaAvroVerifier(artifact, data_frame).decode_frame()
    assert data_schema == "Energistics.Etp.v12.Protocol.ChannelStreaming.ChannelData"
    assert len(data_body["data"]) == 1
    assert data_body["data"][0]["channelId"] == 20
    assert data_body["data"][0]["indexes"][0]["item"] == 1_767_225_600_000_000
    assert data_body["data"][0]["value"]["item"] == pytest.approx(2950.25)

    # Verify honest capability disclosure on PROTOCOL_PROFILE_SPECS["etp.1.2.binary_ws"]
    spec = PROTOCOL_PROFILE_SPECS["etp.1.2.binary_ws"].to_dict()
    assert spec["production_supported"] is True
    assert spec["conformance_verified"] is True
    assert spec["independent_peer_verified"] is False
    assert spec["external_vendor_verified"] is False
    assert spec["evidence_tier"] == "conformance_and_golden_vectors_verified"
    assert "TruncateChannels" in spec["supported_operations"]


def test_compressed_flag_and_unrequested_compression_fail_closed() -> None:
    """Verify compressed frames (0x08) and unrequested OpenSession compression/formats fail closed."""
    # 1. Frame with MSG_FLAG_COMPRESSED (0x08) must be rejected by decode_etp_binary_frame
    w = AvroBinaryWriter()
    EtpMessageHeader(
        protocol=1,
        message_type=3,
        correlation_id=0,
        message_id=10,
        message_flags=MSG_FLAG_COMPRESSED,
    ).encode(w)
    with pytest.raises(AvroBinaryDecodeError, match="compressed flag"):
        decode_etp_binary_frame(w.to_bytes())

    # 2. OpenSession with unrequested compression ("gzip") must be rejected by _validate_open_session
    with pytest.raises(ConnectorTransportError) as exc_comp:
        EtpBinaryWebSocketClient._validate_open_session(
            {
                "type": "OpenSession",
                "correlationId": 1,
                "supportedCompression": "gzip",
                "supportedFormats": ["xml"],
                "supportedProtocols": [{"protocol": 0}, {"protocol": 1}],
                "sessionId": "00000000-0000-0000-0000-000000000001",
            },
            1,
        )
    assert exc_comp.value.category == "protocol_error"
    assert "unsupported compression" in str(exc_comp.value)

    # 3. OpenSession without 'xml' in non-empty supportedFormats must be rejected
    with pytest.raises(ConnectorTransportError) as exc_fmt:
        EtpBinaryWebSocketClient._validate_open_session(
            {
                "type": "OpenSession",
                "correlationId": 1,
                "supportedCompression": "",
                "supportedFormats": ["json"],
                "supportedProtocols": [{"protocol": 0}, {"protocol": 1}],
                "sessionId": "00000000-0000-0000-0000-000000000001",
            },
            1,
        )
    assert exc_fmt.value.category == "protocol_error"
    assert "supportedFormats" in str(exc_fmt.value)


@pytest.mark.asyncio
async def test_missing_index_metadata_never_invents_datetime_and_truncate_channels_reconciles() -> None:
    """Prove missing ChannelMetadataRecord.indexes rejects DataItems rather than inventing DateTime,
    and TruncateChannels truncates superseded in-flight frames while preserving reconciliation.
    """
    # 1. Unit check on _parse_channel_metadata_record with empty indexes or missing uom/indexKind
    ch_no_idx = EtpBinaryWebSocketClient._parse_channel_metadata_record(
        {
            "id": 10,
            "uri": "eml:///witsml20/Channel(no_idx)",
            "channelName": "HKLD",
            "dataKind": "typeDouble",
            "uom": "klbf",
            "indexes": [],
        }
    )
    assert ch_no_idx.indexes == ()

    ch_ambiguous_idx = EtpBinaryWebSocketClient._parse_channel_metadata_record(
        {
            "id": 11,
            "uri": "eml:///witsml20/Channel(ambiguous_idx)",
            "channelName": "SPP",
            "dataKind": "typeDouble",
            "uom": "psi",
            "indexes": [{"indexKind": "", "uom": ""}],
        }
    )
    assert ch_ambiguous_idx.indexes == ()

    # 2. Live WebSocket conversation testing both:
    #    - Channel 10 with empty indexes=[] (must reject DataItem with missing_channel_index_metadata)
    #    - Channel 20 with valid DateTime index + TruncateChannels mid-batch (must truncate t2 and accept replacement t2')
    t1_us = 1_767_225_600_000_000  # 2026-01-01T00:00:00Z
    t2_us = 1_767_225_610_000_000  # 2026-01-01T00:00:10Z
    t2_rev_us = 1_767_225_605_000_000  # 2026-01-01T00:00:05Z

    def _build_truncate_channels_frame(
        channels: list[tuple[int, int]],
        *,
        message_id: int,
        correlation_id: int = 0,
    ) -> bytes:
        w = AvroBinaryWriter()
        EtpMessageHeader(
            protocol=1,
            message_type=5,
            correlation_id=correlation_id,
            message_id=message_id,
            message_flags=0,
        ).encode(w)

        def _enc_trunc(bw: AvroBinaryWriter, item: tuple[int, int]) -> None:
            bw.write_long(item[0])
            encode_index_value(bw, item[1])

        _write_array(w, channels, _enc_trunc)
        return w.to_bytes()

    async def _peer_handler(ws: Any) -> None:
        # Receive RequestSession
        req_raw = await ws.recv()
        req_msg = decode_etp_binary_frame(req_raw)
        assert req_msg["type"] == "RequestSession"
        await ws.send(
            build_open_session_frame(
                correlation_id=req_msg["messageId"],
                message_id=1,
            )
        )
        # Receive Ping + StartStreaming
        start_msg: dict[str, Any] | None = None
        for _ in range(2):
            frame_raw = await ws.recv()
            decoded_frame = decode_etp_binary_frame(frame_raw)
            if decoded_frame["type"] == "Ping":
                await ws.send(
                    build_pong_frame(
                        correlation_id=decoded_frame["messageId"],
                        message_id=99,
                    )
                )
            elif decoded_frame["type"] == "StartStreaming":
                start_msg = decoded_frame
        assert start_msg is not None and start_msg["type"] == "StartStreaming"

        # Send ChannelMetadata:
        # - Channel 10 (HKLD) has empty indexes=[]
        # - Channel 20 (SPP) has explicit DateTime (us) index
        ch10 = {
            "uri": "eml:///witsml20/Channel(hkld)",
            "id": 10,
            "indexes": [],
            "channelName": "HKLD",
            "dataKind": "typeDouble",
            "uom": "klbf",
            "source": "HKLD",
        }
        ch20 = {
            "uri": "eml:///witsml20/Channel(spp)",
            "id": 20,
            "indexes": [
                {
                    "indexKind": "DateTime",
                    "uom": "us",
                    "direction": "Increasing",
                    "name": "TIME",
                }
            ],
            "channelName": "SPP",
            "dataKind": "typeDouble",
            "uom": "psi",
            "source": "SPP",
        }
        await ws.send(
            build_channel_metadata_frame(
                channels=[ch10, ch20],
                correlation_id=start_msg["messageId"],
                message_id=2,
            )
        )

        # Send first ChannelData (part 1):
        # - Item on channel 10 (missing index metadata -> must be rejected!)
        # - Item on channel 20 at t1_us (2800.0 psi)
        # - Item on channel 20 at t2_us (2999.0 psi -> will be truncated by TruncateChannels!)
        await ws.send(
            build_channel_data_frame(
                data_items=[
                    {"channelId": 10, "indexes": [t1_us], "value": 250.0},
                    {"channelId": 20, "indexes": [t1_us], "value": 2800.0},
                    {"channelId": 20, "indexes": [t2_us], "value": 2999.0},
                ],
                correlation_id=start_msg["messageId"],
                message_id=3,
                message_flags=0x01,  # multipart
            )
        )

        # Send TruncateChannels on channel 20 back to t1_us (drops t2_us!)
        await ws.send(
            _build_truncate_channels_frame(
                [(20, t1_us)],
                message_id=4,
                correlation_id=start_msg["messageId"],
            )
        )

        # Send final ChannelData (part 2, FIN) with replacement point at t2_rev_us (2850.0 psi)
        await ws.send(
            build_channel_data_frame(
                data_items=[
                    {"channelId": 20, "indexes": [t2_rev_us], "value": 2850.0},
                ],
                correlation_id=start_msg["messageId"],
                message_id=5,
                message_flags=MSG_FLAG_FINALPART,
            )
        )
        # Drain client teardown frames cleanly
        try:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=0.5)
        except Exception:
            pass

    server = await websockets.serve(
        _peer_handler,
        "127.0.0.1",
        0,
        subprotocols=["energistics-tp"],
    )
    port = server.sockets[0].getsockname()[1]
    register_harness_endpoint("127.0.0.1", port)

    try:
        client = EtpBinaryWebSocketClient(
            endpoint_url=f"ws://127.0.0.1:{port}/etp",
            timeout_seconds=5.0,
        )
        batch = await client.poll_subscription_batch(
            well_id="well-cp14e",
            config={
                "channel_mappings": [
                    {
                        "source_mnemonic": "HKLD",
                        "channel_key": "hookload",
                        "name": "Hookload",
                        "dimension": "force",
                        "unit": "klbf",
                    },
                    {
                        "source_mnemonic": "SPP",
                        "channel_key": "spp",
                        "name": "Standpipe Pressure",
                        "dimension": "pressure",
                        "unit": "psi",
                    },
                ]
            },
            watermark={},
            max_frames=100,
        )
        disp = batch.disposition.to_dict()
        # 4 DataItems discovered:
        # - 1 on CH 10 rejected (missing_channel_index_metadata)
        # - 1 on CH 20 at t2_us truncated by TruncateChannels (counted in watermark_suppressed)
        # - 2 on CH 20 emitted (t1_us=2800.0 and t2_rev_us=2850.0)
        assert disp["source_records_discovered"] == 4
        assert disp["rejected_unsupported_index"] == 1
        assert disp["watermark_suppressed"] == 1
        assert disp["mapped_frames_emitted"] == 2
        assert disp["reconciled"] is True
        reasons = [r["reason"] for r in disp["protocol_rejections"]]
        assert "missing_channel_index_metadata" in reasons

        # Verify full per-channel and cross-sum manifest reconciliation via build_and_reconcile_poll_manifest
        manifest = build_and_reconcile_poll_manifest(
            protocol_disp=batch.disposition,
            protocol_dups=batch.duplicates_suppressed,
            frames_received=len(batch.frames),
            accepted=len(batch.frames),
            service_duplicates=0,
            revised=0,
            service_rejected=0,
            has_more_deferred=False,
            checkpoint_status="complete",
        )
        assert manifest["protocol_reconciled"] is True
        assert manifest["ingestion_reconciled"] is True
        assert manifest["reconciled"] is True
        assert manifest["data_outcome"] == "partial_with_rejections"

        emitted_values = [f.value for f in batch.frames]
        assert emitted_values == [2800.0, 2850.0]
    finally:
        server.close()
        await server.wait_closed()
