#!/usr/bin/env python3
"""DrillAI CP16 — Safe Opt-In External Vendor Interoperability & Conformance Runner.

Purpose
-------
1. Default offline mode (`--mode harness`):
   Executes deterministic protocol conformance checks against the in-repo reference harnesses
   (`LocalWitsmlSoapServer` for WITSML v1.4.1.1 SOAP and `LocalEtpBinaryWebSocketServer` for
   Energistics ETP v1.2 Binary Avro WebSocket) plus negative checks for unsupported ETP 1.2
   protocols, compression, and message types. Never makes outbound external network requests.

2. Opt-in external vendor mode (`--mode external`):
   Executes a read-only acceptance test against an operator-supplied external commercial
   WITSML 1.4.1.1 store (`DRILLAI_EXTERNAL_WITSML_URL`) and/or ETP 1.2 store
   (`DRILLAI_EXTERNAL_ETP_URL`). When those environment variables are not set, the runner
   makes zero network calls and reports `external_gate_status: "BLOCKED_BY_EXTERNAL_EVIDENCE"`
   with `external_vendor_verified: false`.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from drillai.telemetry.connectors import PROTOCOL_PROFILE_SPECS  # noqa: E402
from drillai.telemetry.protocols.etp_binary import (  # noqa: E402
    AvroBinaryWriter,
    EtpBinaryWebSocketClient,
    EtpMessageHeader,
    LocalEtpBinaryWebSocketServer,
    decode_etp_binary_frame,
)
from drillai.telemetry.protocols.witsml import (  # noqa: E402
    LocalWitsmlSoapServer,
    WitsmlSoapClient,
)


async def _run_harness_conformance() -> dict[str, Any]:
    """Run offline WITSML 1.4.1.1 SOAP + ETP 1.2 Binary Avro conformance and negative protocol checks."""
    os.environ.setdefault("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")

    # 1. WITSML 1.4.1.1 SOAP harness conformance
    async with LocalWitsmlSoapServer() as witsml_srv:
        witsml_client = WitsmlSoapClient(endpoint_url=witsml_srv.endpoint_url)
        version = await witsml_client.get_version()
        caps = await witsml_client.get_cap()
        batch = await witsml_client.poll_log_batch(
            well_id="well-01",
            config={
                "uid_well": "well-01",
                "uid_wellbore": "wb-01",
                "uid_log": "log-time-01",
                "channel_mappings": [
                    {
                        "source_mnemonic": "SPP",
                        "channel_key": "standpipe_pressure",
                        "dimension": "pressure",
                        "unit": "psi",
                    }
                ],
            },
        )
        witsml_ok = (
            version == "1.4.1.1"
            and "WMLS_GetFromStore" in (caps.get("functions") or [])
            and len(batch.frames) > 0
        )

    # 2. ETP 1.2 Binary Avro WebSocket harness conformance + unsupported feature rejection
    async with LocalEtpBinaryWebSocketServer() as etp_srv:
        etp_client = EtpBinaryWebSocketClient(endpoint_url=etp_srv.endpoint_url)
        session_id, channels = await etp_client.probe_session()

        import websockets

        # 2a. RequestSession asking for unsupported Protocol 3 (Discovery) & compression 'lz4'
        async with websockets.connect(
            etp_srv.endpoint_url,
            subprotocols=["energistics-tp"],
        ) as ws:
            w = AvroBinaryWriter()
            EtpMessageHeader(
                protocol=0,
                message_type=1,  # RequestSession
                correlation_id=0,
                message_id=1,
                message_flags=0x02,
            ).encode(w)
            w.write_string("DrillAI-Interop-Probe")
            w.write_string("1.0.0")
            w.write_fixed(b"\x11" * 16)
            w.write_long(2)  # 2 requested protocols: Protocol 0 + unsupported Protocol 3
            for proto_id, role in ((0, "server"), (3, "store")):
                w.write_int(proto_id)
                w.write_int(1)
                w.write_int(2)
                w.write_int(0)
                w.write_int(0)
                w.write_string(role)
                w.write_long(0)
            w.write_long(0)
            w.write_long(0)  # supportedDataObjects
            w.write_long(1)  # supportedCompression = ["lz4"]
            w.write_string("lz4")
            w.write_long(0)
            w.write_long(1)  # supportedFormats = ["xml"]
            w.write_string("xml")
            w.write_long(0)
            w.write_long(0)
            w.write_long(0)
            w.write_boolean(False)
            w.write_long(0)
            await ws.send(w.to_bytes())

            raw_reply = await asyncio.wait_for(ws.recv(), timeout=5.0)
            assert isinstance(raw_reply, bytes)
            decoded_exc = decode_etp_binary_frame(raw_reply)
            unsupported_proto_rejected = (
                decoded_exc.get("type") == "ProtocolException"
                and decoded_exc.get("protocol") == 0
                and decoded_exc.get("messageType") == 1000
            )

        # 2b. Unsupported messageType (protocol=99, messageType=99) rejected with ProtocolException
        async with websockets.connect(
            etp_srv.endpoint_url,
            subprotocols=["energistics-tp"],
        ) as ws:
            w2 = AvroBinaryWriter()
            EtpMessageHeader(
                protocol=99,
                message_type=99,
                correlation_id=0,
                message_id=2,
                message_flags=0x02,
            ).encode(w2)
            await ws.send(w2.to_bytes())
            raw_reply2 = await asyncio.wait_for(ws.recv(), timeout=5.0)
            assert isinstance(raw_reply2, bytes)
            decoded_exc2 = decode_etp_binary_frame(raw_reply2)
            unsupported_msg_rejected = (
                decoded_exc2.get("type") == "ProtocolException"
                and decoded_exc2.get("messageType") == 1000
            )

    witsml_spec = PROTOCOL_PROFILE_SPECS["witsml.1.4.1.1.soap_http"]
    etp_bin_spec = PROTOCOL_PROFILE_SPECS["etp.1.2.binary_ws"]
    etp_json_spec = PROTOCOL_PROFILE_SPECS["etp.1.2.json_ws"]

    return {
        "witsml_1411_harness_passed": witsml_ok,
        "etp_12_binary_harness_passed": bool(session_id and len(channels) > 0),
        "etp_12_unsupported_protocol_rejected_with_exception": unsupported_proto_rejected,
        "etp_12_unsupported_message_rejected_with_exception": unsupported_msg_rejected,
        "profiles": {
            "witsml.1.4.1.1.soap_http": {
                "transport": witsml_spec.transport,
                "wire_encoding": witsml_spec.wire_encoding,
                "conformance_verified": witsml_spec.conformance_verified,
                "local_harness_verified": witsml_spec.local_harness_verified,
                "external_vendor_verified": witsml_spec.external_vendor_verified,
            },
            "etp.1.2.binary_ws": {
                "transport": etp_bin_spec.transport,
                "wire_encoding": etp_bin_spec.wire_encoding,
                "conformance_verified": etp_bin_spec.conformance_verified,
                "local_harness_verified": etp_bin_spec.local_harness_verified,
                "external_vendor_verified": etp_bin_spec.external_vendor_verified,
            },
            "etp.1.2.json_ws": {
                "transport": etp_json_spec.transport,
                "wire_encoding": etp_json_spec.wire_encoding,
                "conformance_verified": etp_json_spec.conformance_verified,
                "local_harness_verified": etp_json_spec.local_harness_verified,
                "external_vendor_verified": etp_json_spec.external_vendor_verified,
            },
        },
    }


async def _run_external_vendor_probe() -> dict[str, Any]:
    """Run opt-in read-only acceptance probe against external vendor endpoints if configured."""
    witsml_url = os.environ.get("DRILLAI_EXTERNAL_WITSML_URL", "").strip()
    etp_url = os.environ.get("DRILLAI_EXTERNAL_ETP_URL", "").strip()
    if not witsml_url and not etp_url:
        return {
            "external_gate_status": "BLOCKED_BY_EXTERNAL_EVIDENCE",
            "external_vendor_verified": False,
            "network_calls_made": 0,
            "reason": (
                "No external commercial WITSML 1.4.1.1 or ETP 1.2 endpoint is configured in this "
                "environment. Set DRILLAI_EXTERNAL_WITSML_URL and/or DRILLAI_EXTERNAL_ETP_URL in a "
                "staging/rig environment to execute live vendor acceptance."
            ),
            "required_env_vars": [
                "DRILLAI_EXTERNAL_WITSML_URL",
                "DRILLAI_EXTERNAL_WITSML_USERNAME",
                "DRILLAI_EXTERNAL_WITSML_PASSWORD",
                "DRILLAI_EXTERNAL_ETP_URL",
                "DRILLAI_EXTERNAL_ETP_TOKEN",
            ],
        }

    results: dict[str, Any] = {"network_calls_made": 0}
    if witsml_url:
        secrets: dict[str, str] = {}
        if os.environ.get("DRILLAI_EXTERNAL_WITSML_USERNAME"):
            secrets["username"] = os.environ["DRILLAI_EXTERNAL_WITSML_USERNAME"]
        if os.environ.get("DRILLAI_EXTERNAL_WITSML_PASSWORD"):
            secrets["password"] = os.environ["DRILLAI_EXTERNAL_WITSML_PASSWORD"]
        client = WitsmlSoapClient(endpoint_url=witsml_url, secrets=secrets)
        ver = await client.get_version()
        cap = await client.get_cap()
        results["network_calls_made"] += 2
        results["witsml_external"] = {
            "version": ver,
            "functions": cap.get("functions", []),
            "transcript_sha256": hashlib.sha256(
                json.dumps({"version": ver, "cap": cap}, sort_keys=True).encode("utf-8")
            ).hexdigest(),
        }
    if etp_url:
        etp_secrets: dict[str, str] = {}
        if os.environ.get("DRILLAI_EXTERNAL_ETP_TOKEN"):
            etp_secrets["bearer_token"] = os.environ["DRILLAI_EXTERNAL_ETP_TOKEN"]
        client = EtpBinaryWebSocketClient(endpoint_url=etp_url, secrets=etp_secrets)
        session_id, channels = await client.probe_session()
        results["network_calls_made"] += 1
        results["etp_external"] = {
            "session_established": bool(session_id),
            "channels_discovered": len(channels),
            "transcript_sha256": hashlib.sha256(
                json.dumps(
                    {"session_id": session_id, "channels": [c.mnemonic for c in channels]},
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        }

    results["external_gate_status"] = "EXTERNAL_PROBE_COMPLETED"
    results["external_vendor_verified"] = bool(witsml_url and etp_url)
    return results


COMMISSIONING_CHECKLIST = [
    "1. Configure authoritative egress allowlist via PUT /api/v1/connectors/egress-policy with the target WITSML/ETP host.",
    "2. Export DRILLAI_EXTERNAL_WITSML_URL, DRILLAI_EXTERNAL_WITSML_USERNAME, DRILLAI_EXTERNAL_WITSML_PASSWORD and/or DRILLAI_EXTERNAL_ETP_URL, DRILLAI_EXTERNAL_ETP_TOKEN in the staging/rig environment.",
    "3. Run `python scripts/verify_external_vendor_interop.py --mode external --require-external` to execute read-only WMLS_GetVersion/WMLS_GetCap and ETP RequestSession/CloseSession probes.",
    "4. Archive the output JSON report (including transcript_sha256 digests with all credentials redacted) before flipping external_vendor_verified for that vendor profile.",
]


async def _async_main(mode: str, require_external: bool) -> int:
    report: dict[str, Any] = {
        "timestamp_utc": dt.datetime.now(dt.UTC).isoformat(),
        "mode": mode,
        "commissioning_checklist": COMMISSIONING_CHECKLIST,
    }
    harness_report = await _run_harness_conformance()
    report["harness_conformance"] = harness_report
    harness_passed = (
        harness_report["witsml_1411_harness_passed"]
        and harness_report["etp_12_binary_harness_passed"]
        and harness_report["etp_12_unsupported_protocol_rejected_with_exception"]
        and harness_report["etp_12_unsupported_message_rejected_with_exception"]
    )
    if mode == "external":
        ext = await _run_external_vendor_probe()
        report["external_vendor_probe"] = ext
        report["external_gate_status"] = ext["external_gate_status"]
        report["external_vendor_verified"] = ext["external_vendor_verified"]
        report["network_calls_made"] = ext["network_calls_made"]
    else:
        report["external_gate_status"] = "BLOCKED_BY_EXTERNAL_EVIDENCE"
        report["external_vendor_verified"] = False
        report["network_calls_made"] = 0

    print(json.dumps(report, indent=2, sort_keys=True))
    if not harness_passed:
        return 1
    if require_external and not report["external_vendor_verified"]:
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DrillAI CP16 WITSML 1.4.1.1 & ETP 1.2 Conformance and External Vendor Interop Runner"
    )
    parser.add_argument(
        "--mode",
        choices=("harness", "external"),
        default="harness",
        help="Execution mode: 'harness' (default offline conformance) or 'external' (opt-in live vendor probe)",
    )
    parser.add_argument(
        "--require-external",
        action="store_true",
        help="Exit non-zero (code 2) if live external vendor verification is not available",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_async_main(args.mode, args.require_external))


if __name__ == "__main__":
    raise SystemExit(main())
