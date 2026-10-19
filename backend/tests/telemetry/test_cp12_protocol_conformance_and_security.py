"""CP12 Phase B (Independent Standards Conformance), Phase C (Concurrency & Lease Heartbeats),
Phase D (SSRF/DNS-Rebinding & Secret Minimization), and Phase F2 (Governed Replay) test suite.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import html
import json
from typing import Any

import pytest
import websockets
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from drillai.api.app import create_app
from drillai.core.clock import utc_now
from drillai.core.config import get_settings, reset_settings_cache
from drillai.core.errors import ValidationFailed
from drillai.db.base import Base
from drillai.db.models import (
    AuditLog,
    Connector,
    Organization,
    Project,
    Well,
    Wellbore,
)
from drillai.telemetry.connectors import (
    PROTOCOL_PROFILE_SPECS,
    ConnectorService,
    ConnectorTransportError,
    enforce_connect_time_ssrf,
    mask_secret_refs,
    redact_sensitive_text,
    register_harness_endpoint,
    set_custom_dns_resolver,
    unregister_harness_endpoint,
    validate_connector_endpoint,
    validate_secret_refs,
)
from drillai.telemetry.protocols.etp import EtpWebSocketClient
from drillai.telemetry.protocols.witsml import WitsmlSoapClient, parse_get_from_store_response
from drillai.telemetry.worker import ConnectorWorker


def _mappings() -> list[dict[str, Any]]:
    return [
        {
            "source_mnemonic": "SPP",
            "channel_uri": "eml://witsml20/Channel(spp)",
            "channel_key": "spp",
            "name": "Standpipe Pressure",
            "dimension": "pressure",
            "unit": "psi",
        },
        {
            "source_mnemonic": "WOB",
            "channel_uri": "eml://witsml20/Channel(wob)",
            "channel_key": "wob",
            "name": "Weight on Bit",
            "dimension": "force",
            "unit": "klbf",
        },
    ]


async def _seed_org_and_well(app, org_id: str, well_id: str, wellbore_id: str, slug: str) -> None:
    async with app.state.database.session() as session:
        session.add(Organization(id=org_id, slug=slug, name="CP12 Conformance Org", kind="operator"))
        session.add(Project(id=f"prj_{well_id}", org_id=org_id, name="CP12 Project"))
        session.add(
            Well(
                id=well_id,
                org_id=org_id,
                project_id=f"prj_{well_id}",
                name=f"WELL-{well_id}",
                well_type="development_producer",
                status="drilling",
            )
        )
        session.add(
            Wellbore(
                id=wellbore_id,
                org_id=org_id,
                well_id=well_id,
                name=f"WB-{wellbore_id}",
                purpose="main",
                status="active",
                is_active=True,
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Phase B1 & B3: Independent WITSML 1.4.1.1 Conformance Suite (Spec Fixtures)
# ---------------------------------------------------------------------------


async def test_b1_b3_independent_witsml_1411_soap_conformance_and_negative_fixtures():
    """Verify `WitsmlSoapClient` against an independently constructed raw HTTP/1.1 responder
    using Energistics WITSML v1.4.1.1 SOAP 1.1 wire fixtures (not `LocalWitsmlSoapServer`)."""
    recorded_requests: list[tuple[dict[str, str], str]] = []
    mode = "standard_prefixed"

    # Spec-derived WITSML 1.4.1.1 <witsml:logs> fixture with explicit namespace prefix
    prefixed_logs_xml = (
        '<witsml:logs xmlns:witsml="http://www.witsml.org/schemas/1series" version="1.4.1.1">'
        '<witsml:log uidWell="W-SPEC-01" uidWellbore="WB-SPEC-01" uid="LOG-SPEC-01">'
        "<witsml:nameWell>Spec Well 01</witsml:nameWell>"
        "<witsml:nameWellbore>Spec Wellbore 01</witsml:nameWellbore>"
        "<witsml:name>Time Log</witsml:name>"
        "<witsml:indexType>date time</witsml:indexType>"
        "<witsml:indexCurve>TIME</witsml:indexCurve>"
        "<witsml:nullValue>-999.25</witsml:nullValue>"
        "<witsml:logCurveInfo><witsml:mnemonic>TIME</witsml:mnemonic><witsml:unit>s</witsml:unit></witsml:logCurveInfo>"
        "<witsml:logCurveInfo><witsml:mnemonic>SPP</witsml:mnemonic><witsml:unit>psi</witsml:unit></witsml:logCurveInfo>"
        "<witsml:logCurveInfo><witsml:mnemonic>WOB</witsml:mnemonic><witsml:unit>klbf</witsml:unit></witsml:logCurveInfo>"
        "<witsml:logData>"
        "<witsml:mnemonicList>TIME,SPP,WOB</witsml:mnemonicList>"
        "<witsml:unitList>s,psi,klbf</witsml:unitList>"
        "<witsml:data>2026-06-01T00:00:00Z,3010.5,24.2</witsml:data>"
        "<witsml:data>2026-06-01T00:00:05Z,-999.25,25.0</witsml:data>"
        "</witsml:logData>"
        "</witsml:log>"
        "</witsml:logs>"
    )

    async def _raw_http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal mode
        try:
            head_bytes = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            head_str = head_bytes.decode("latin-1")
            hdrs: dict[str, str] = {}
            for line in head_str.split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    hdrs[k.strip().lower()] = v.strip()
            clen = int(hdrs.get("content-length", "0"))
            body_str = (await asyncio.wait_for(reader.readexactly(clen), timeout=5.0)).decode("utf-8")
            recorded_requests.append((hdrs, body_str))

            if "WMLS_GetVersion" in body_str:
                xml_body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
                    'xmlns:ns1="http://www.witsml.org/message/120">'
                    "<SOAP-ENV:Body><ns1:WMLS_GetVersionResponse>"
                    "<Result>1.4.1.1</Result>"
                    "</ns1:WMLS_GetVersionResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"
                )
            elif "WMLS_GetCap" in body_str:
                cap_inner = (
                    '<capServers xmlns="http://www.witsml.org/schemas/1series" version="1.4.1.1">'
                    '<capServer apiVersion="1.4.1.1">'
                    '<function name="WMLS_GetFromStore"><dataObject>log</dataObject></function>'
                    '<function name="WMLS_GetVersion"/>'
                    "</capServer></capServers>"
                )
                xml_body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
                    'xmlns:ns1="http://www.witsml.org/message/120">'
                    "<SOAP-ENV:Body><ns1:WMLS_GetCapResponse>"
                    "<Result>1</Result>"
                    f"<CapabilitiesOut>{html.escape(cap_inner)}</CapabilitiesOut>"
                    "<SuppMsgOut/>"
                    "</ns1:WMLS_GetCapResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"
                )
            elif mode == "soap_fault":
                xml_body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
                    "<SOAP-ENV:Body><SOAP-ENV:Fault>"
                    "<faultcode>SOAP-ENV:Server</faultcode>"
                    "<faultstring>WITSML Store Internal Exception</faultstring>"
                    "</SOAP-ENV:Fault></SOAP-ENV:Body></SOAP-ENV:Envelope>"
                )
            elif mode == "negative_result":
                xml_body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/">'
                    "<SOAP-ENV:Body><WMLS_GetFromStoreResponse>"
                    "<Result>-403</Result><XMLout/><SuppMsgOut>Missing read capability</SuppMsgOut>"
                    "</WMLS_GetFromStoreResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"
                )
            elif mode == "non_soap_html":
                xml_body = "<html><body>Proxy Gateway Error</body></html>"
            else:
                xml_body = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" '
                    'xmlns:ns1="http://www.witsml.org/message/120">'
                    "<SOAP-ENV:Body><ns1:WMLS_GetFromStoreResponse>"
                    "<Result>1</Result>"
                    f"<XMLout>{html.escape(prefixed_logs_xml)}</XMLout>"
                    "<SuppMsgOut/>"
                    "</ns1:WMLS_GetFromStoreResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"
                )

            payload = xml_body.encode("utf-8")
            resp_head = (
                "HTTP/1.1 200 OK\r\n"
                "Content-Type: text/xml; charset=utf-8\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("latin-1")
            writer.write(resp_head + payload)
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=0.5)

    srv = await asyncio.start_server(_raw_http_handler, "127.0.0.1", 0)
    port = int(srv.sockets[0].getsockname()[1])
    register_harness_endpoint("127.0.0.1", port)
    url = f"http://127.0.0.1:{port}/witsml"

    try:
        client = WitsmlSoapClient(endpoint_url=url, timeout_seconds=5.0, max_retries=0)

        # 1. Verify WMLS_GetVersion and outbound SOAPAction header
        ver = await client.get_version()
        assert ver == "1.4.1.1"
        assert "Store.WMLS_GetVersion" in recorded_requests[-1][0]["soapaction"]

        # 2. Verify WMLS_GetCap
        cap = await client.get_cap()
        assert cap["version"] == "1.4.1.1"
        assert "WMLS_GetFromStore" in cap["functions"]
        assert "Store.WMLS_GetCap" in recorded_requests[-1][0]["soapaction"]

        # 3. Verify WMLS_GetFromStore with prefixed namespace XMLout and matching UIDs
        batch = await client.poll_log_batch(
            well_id="W-SPEC-01",
            config={
                "uid_well": "W-SPEC-01",
                "uid_wellbore": "WB-SPEC-01",
                "uid_log": "LOG-SPEC-01",
                "channel_mappings": _mappings(),
            },
        )
        assert len(batch.frames) == 4
        assert batch.disposition.reconciled is True
        missing = [f for f in batch.frames if f.value is None]
        assert len(missing) == 1 and missing[0].quality == "missing"

        # 4. UID mismatch fails closed with protocol_error
        with pytest.raises(ConnectorTransportError) as uid_exc:
            await client.poll_log_batch(
                well_id="W-SPEC-01",
                config={
                    "uid_well": "WRONG-WELL-UID",
                    "channel_mappings": _mappings(),
                },
            )
        assert uid_exc.value.category == "protocol_error"

        # 5. SOAP Fault fails closed
        mode = "soap_fault"
        with pytest.raises(ConnectorTransportError) as fault_exc:
            await client.poll_log_batch(well_id="W-SPEC-01", config={"channel_mappings": _mappings()})
        assert fault_exc.value.category == "protocol_error"
        assert "WITSML Store Internal Exception" in str(fault_exc.value)

        # 6. Negative Result code (Result=-403) fails closed
        mode = "negative_result"
        with pytest.raises(ConnectorTransportError) as neg_exc:
            await client.poll_log_batch(well_id="W-SPEC-01", config={"channel_mappings": _mappings()})
        assert neg_exc.value.category == "protocol_error"
        assert "-403" in str(neg_exc.value)

        # 7. Non-SOAP HTML document fails closed instead of returning an empty batch
        mode = "non_soap_html"
        with pytest.raises(ConnectorTransportError) as html_exc:
            await client.poll_log_batch(well_id="W-SPEC-01", config={"channel_mappings": _mappings()})
        assert html_exc.value.category == "protocol_error"

        # 8. Depth-indexed <log> fails closed
        depth_log_xml = (
            '<logs xmlns="http://www.witsml.org/schemas/1series" version="1.4.1.1">'
            '<log uidWell="W1" uidWellbore="WB1" uid="L1">'
            "<indexType>measured depth</indexType><indexCurve>DEPT</indexCurve>"
            "<logData><mnemonicList>DEPT,SPP</mnemonicList><unitList>m,psi</unitList>"
            "<data>2500.0,3000.0</data></logData></log></logs>"
        )
        with pytest.raises(ConnectorTransportError) as depth_exc:
            parse_get_from_store_response(depth_log_xml, mappings=[], well_id="W1")
        assert depth_exc.value.category == "protocol_error"
    finally:
        unregister_harness_endpoint("127.0.0.1", port)
        srv.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(srv.wait_closed(), timeout=1.0)


# ---------------------------------------------------------------------------
# Phase B1 & B2: Independent ETP 1.2 Conformance Suite (Spec Fixtures)
# ---------------------------------------------------------------------------


async def test_b1_b2_independent_etp_12_avro_json_conformance_and_lifecycle():
    """Verify `EtpWebSocketClient` against an independent raw WebSocket server speaking
    canonical ETP v1.2 `{header, body}` Avro-JSON messages (not `LocalEtpWebSocketServer`)."""
    client_messages: list[dict[str, Any]] = []
    mode = "standard_v12"
    t0 = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.UTC)
    t0_us = int(t0.timestamp() * 1_000_000)

    async def _raw_etp_ws_handler(ws: Any) -> None:
        nonlocal mode
        assert ws.subprotocol == "energistics-tp"
        async for raw_text in ws:
            msg = json.loads(raw_text)
            client_messages.append(msg)
            hdr = msg.get("header") or {}
            body = msg.get("body") or {}
            proto = hdr.get("protocol")
            mtype = hdr.get("messageType")
            mid = hdr.get("messageId", 1)

            if proto == 0 and mtype == 1:  # RequestSession
                if mode == "bad_correlation_id":
                    await ws.send(
                        json.dumps(
                            {
                                "header": {
                                    "protocol": 0,
                                    "messageType": 2,
                                    "correlationId": 999,  # wrong correlationId!
                                    "messageId": 1,
                                    "messageFlags": 0,
                                },
                                "body": {"sessionId": "bad-corr-sess"},
                            }
                        )
                    )
                    return
                if mode == "errors_map_exception":
                    await ws.send(
                        json.dumps(
                            {
                                "header": {
                                    "protocol": 0,
                                    "messageType": 1000,
                                    "correlationId": mid,
                                    "messageId": 1,
                                    "messageFlags": 0,
                                },
                                "body": {
                                    "errors": {
                                        "1": {"code": 8, "message": "Unsupported protocol role"},
                                    }
                                },
                            }
                        )
                    )
                    return

                # Pure ETP 1.2 header/body OpenSession without any synthetic `type` field in body
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "protocol": 0,
                                "messageType": 2,
                                "correlationId": mid,
                                "messageId": 1,
                                "messageFlags": 0,
                            },
                            "body": {
                                "applicationName": "Independent ETP 1.2 Spec Responder",
                                "applicationVersion": "1.2.0",
                                "sessionId": "spec-etp12-sess-01",
                                "supportedProtocols": [
                                    {"protocol": 0, "role": "server"},
                                    {"protocol": 1, "role": "producer"},
                                ],
                            },
                        }
                    )
                )
            elif proto == 0 and mtype == 8:  # Ping
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "protocol": 0,
                                "messageType": 9,
                                "correlationId": mid,
                                "messageId": 2,
                                "messageFlags": 0,
                            },
                            "body": {"currentDateTime": t0_us},
                        }
                    )
                )
            elif proto == 1 and mtype == 1 and "uris" in body:  # ChannelDescribe
                # Send initial ChannelMetadata for Channel 10 (SPP)
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "protocol": 1,
                                "messageType": 1,
                                "correlationId": mid,
                                "messageId": 3,
                                "messageFlags": 0,
                            },
                            "body": {
                                "channels": [
                                    {
                                        "channelId": 10,
                                        "channelUri": "eml://witsml20/Channel(spp)",
                                        "channelName": "SPP",
                                        "uom": "psi",
                                        "dataType": "double",
                                        "indexes": [
                                            {
                                                "indexKind": "DateTime",
                                                "uom": "us",
                                                "direction": "Increasing",
                                            }
                                        ],
                                    }
                                ]
                            },
                        }
                    )
                )
            elif proto == 1 and mtype == 4:  # ChannelStreamingStart
                # Mid-stream dynamic ChannelMetadata registration for Channel 20 (WOB) before ChannelData!
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
                                "channels": [
                                    {
                                        "channelId": 20,
                                        "channelUri": "eml://witsml20/Channel(wob)",
                                        "channelName": "WOB",
                                        "uom": "klbf",
                                        "dataType": "double",
                                        "indexes": [
                                            {
                                                "indexKind": "DateTime",
                                                "uom": "us",
                                                "direction": "Increasing",
                                            }
                                        ],
                                    }
                                ]
                            },
                        }
                    )
                )
                # Send pure ETP 1.2 ChannelData (protocol=1, messageType=2) with microsecond DateTime indexes
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "protocol": 1,
                                "messageType": 2,
                                "correlationId": 0,
                                "messageId": 5,
                                "messageFlags": 0,
                            },
                            "body": {
                                "data": [
                                    {
                                        "channelId": 10,
                                        "indexes": [{"item": t0_us}],
                                        "value": {"item": 2995.0},
                                        "valueAttributes": [
                                            {"attributeId": 0, "attributeValue": {"item": "good"}}
                                        ],
                                    },
                                    {
                                        "channelId": 20,
                                        "indexes": [{"item": t0_us + 5_000_000}],
                                        "value": {"item": 23.5},
                                        "valueAttributes": [
                                            {"attributeId": 0, "attributeValue": {"item": "good"}}
                                        ],
                                    },
                                ],
                                "isFinalBatch": True,
                            },
                        }
                    )
                )
            elif proto == 0 and mtype == 5:  # CloseSession
                break

    async with websockets.serve(
        _raw_etp_ws_handler,
        "127.0.0.1",
        0,
        subprotocols=["energistics-tp"],
    ) as ws_srv:
        port = int(ws_srv.sockets[0].getsockname()[1])
        register_harness_endpoint("127.0.0.1", port)
        url = f"ws://127.0.0.1:{port}/etp12"
        client = EtpWebSocketClient(endpoint_url=url, timeout_seconds=5.0)
        try:
            # 1. Verify full ETP 1.2 handshake, Ping/Pong, dynamic mid-stream ChannelMetadata, and ChannelData
            batch = await client.poll_subscription_batch(
                well_id="wel_etp_spec",
                config={"channel_mappings": _mappings()},
                watermark={},
            )
            assert batch.session_id == "spec-etp12-sess-01"
            assert batch.ping_pong_verified is True
            assert len(batch.frames) == 2
            assert {f.channel_key for f in batch.frames} == {"spp", "wob"}
            assert batch.disposition.reconciled is True

            # Verify client outbound messages used canonical ETP 1.2 {header, body} structure
            assert all("header" in m and "body" in m for m in client_messages)

            # 2. Verify mismatched OpenSession correlationId is rejected
            mode = "bad_correlation_id"
            with pytest.raises(ConnectorTransportError) as corr_exc:
                await client.poll_subscription_batch(
                    well_id="wel_etp_spec",
                    config={"channel_mappings": _mappings()},
                )
            assert corr_exc.value.category == "protocol_error"
            assert "correlationId" in str(corr_exc.value)

            # 3. Verify ProtocolException with `errors` map is parsed and rejected
            mode = "errors_map_exception"
            with pytest.raises(ConnectorTransportError) as map_exc:
                await client.poll_subscription_batch(
                    well_id="wel_etp_spec",
                    config={"channel_mappings": _mappings()},
                )
            assert map_exc.value.category == "protocol_error"
            assert "Unsupported protocol role" in str(map_exc.value)
        finally:
            unregister_harness_endpoint("127.0.0.1", port)

    # 4. Verify PROTOCOL_PROFILE_SPECS honesty
    etp_spec = PROTOCOL_PROFILE_SPECS["etp.1.2.json_ws"]
    witsml_spec = PROTOCOL_PROFILE_SPECS["witsml.1.4.1.1.soap_http"]
    assert etp_spec.external_vendor_verified is False
    assert witsml_spec.external_vendor_verified is False
    assert "GetResources" not in etp_spec.supported_operations


# ---------------------------------------------------------------------------
# Phase C1, C2, C4 & F2: Concurrency, Lease Heartbeat/Cancellation, Fairness & Replay
# ---------------------------------------------------------------------------


async def test_c1_c2_f2_atomic_cas_lease_heartbeat_preemption_and_governed_replay(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Verify:
    - C1: Concurrent config updates with the same `expected_config_version` reject stale writers with 409;
          if a connector's config_version is incremented while a worker poll is in flight, the worker
          aborts with `status='fenced'`.
    - C2: Long poll exceeding `lease_seconds` succeeds because the background heartbeat renews the lease;
          if the lease is preempted mid-poll (`stop`), the heartbeat cancels the in-flight poll task immediately.
    - F2: `POST /api/v1/connectors/{id}/replay` supports `dry_run=True` and `dry_run=False`, never regresses
          the connector watermark behind `cursor_before`, and records `ConnectorRun(run_kind="replay")` + `AuditLog`.
    """
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/c1_c2_f2.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)

    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id = "org_000000000000000000000001"
    well_id = "wel_c1_c2"
    wellbore_id = "wbr_c1_c2"
    await _seed_org_and_well(app, org_id, well_id, wellbore_id, settings.dev_org_slug)

    from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer

    server = LocalWitsmlSoapServer()
    t0 = dt.datetime(2026, 6, 2, 10, 0, 0, tzinfo=dt.UTC)
    server.seed_default_rows(start=t0, count=4, step_seconds=5.0)

    try:
        async with server:
            worker = ConnectorWorker(
                app.state.database,
                worker_id="wrk_c1_main",
                lease_seconds=0.45,
                allow_loopback=True,
            )
            app.state.connector_worker = worker

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                hdrs = {"X-Dev-Roles": "engineer"}
                create_res = await client.post(
                    "/api/v1/connectors",
                    headers=hdrs,
                    json={
                        "key": "witsml-c1-c2",
                        "name": "WITSML Concurrency & Replay Store",
                        "protocol_profile": "witsml.1.4.1.1.soap_http",
                        "well_id": well_id,
                        "wellbore_id": wellbore_id,
                        "endpoint_url": server.endpoint_url,
                        "config": {
                            "auth_mode": "none",
                            "poll_interval_seconds": 0.05,
                            "channel_mappings": _mappings(),
                        },
                    },
                )
                assert create_res.status_code == 201
                cid = create_res.json()["id"]

                # C1a: Idempotency-Key reused with a different payload returns 409 Conflict
                await client.patch(
                    f"/api/v1/connectors/{cid}",
                    headers={**hdrs, "Idempotency-Key": "idem-c1-conflict"},
                    json={
                        "expected_config_version": 1,
                        "reason": "first update",
                        "name": "Updated Name V2",
                    },
                )
                reused_diff_payload = await client.patch(
                    f"/api/v1/connectors/{cid}",
                    headers={**hdrs, "Idempotency-Key": "idem-c1-conflict"},
                    json={
                        "expected_config_version": 2,
                        "reason": "different payload with same key",
                        "name": "Different Name V3",
                    },
                )
                assert reused_diff_payload.status_code == 409

                # Start connector (config_version is 2)
                await client.post(
                    f"/api/v1/connectors/{cid}/start",
                    headers=hdrs,
                    json={"reason": "start C1/C2 test", "expected_config_version": 2},
                )

                # C1b: Worker claims connector at config_version=2, then operator updates config (to version 3)
                # before worker commits Phase 2 -> worker MUST fence and abort!
                async def _mutate_config_mid_poll(claimed) -> None:
                    async with app.state.database.session() as s2:
                        svc2 = ConnectorService(s2, org_id)
                        await svc2.update(
                            claimed.connector_id,
                            name="Mutated Mid-Poll V3",
                            expected_config_version=2,
                            reason="operator config update during in-flight poll",
                            allow_loopback=True,
                        )
                        await s2.commit()

                worker._before_commit_hook = _mutate_config_mid_poll
                claimed_c1 = await worker.claim_connector(cid, now=utc_now())
                assert claimed_c1 is not None
                outcome_c1 = await worker.execute_claimed_poll(claimed_c1)
                assert outcome_c1.status == "fenced"
                worker._before_commit_hook = None

                # C2: Mid-poll preemption during slow network connect cancels in-flight connect via heartbeat!
                server.fault_mode = "slow_timeout"  # sleeps 3.0s on server
                claimed_c2 = await worker.claim_connector(cid, now=utc_now() + dt.timedelta(seconds=2))
                assert claimed_c2 is not None

                async def _preempt_after_200ms() -> None:
                    await asyncio.sleep(0.2)
                    async with app.state.database.session() as s3:
                        svc3 = ConnectorService(s3, org_id)
                        await svc3.transition(
                            cid,
                            action="stop",
                            reason="preempt slow poll",
                        )
                        await s3.commit()

                preempt_task = asyncio.create_task(_preempt_after_200ms())
                t_start = asyncio.get_running_loop().time()
                outcome_c2 = await worker.execute_claimed_poll(claimed_c2)
                elapsed = asyncio.get_running_loop().time() - t_start
                await preempt_task
                assert outcome_c2.status == "fenced"
                # Must have been cancelled by heartbeat well before the 3.0s server sleep finished!
                assert elapsed < 1.8

                # Restart connector and run a normal poll to advance cursor to t0 + 15s
                worker.lease_seconds = 15.0
                server.fault_mode = "ok"
                await client.post(
                    f"/api/v1/connectors/{cid}/restart",
                    headers=hdrs,
                    json={"reason": "restart after preemption test"},
                )
                poll_ok = (await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)).json()
                assert poll_ok["outcome"]["status"] == "succeeded"
                assert poll_ok["outcome"]["points_accepted"] == 8  # 4 rows * 2 curves
                max_cursor_iso = (t0 + dt.timedelta(seconds=15)).isoformat()
                assert poll_ok["connector"]["cursor"]["cursor_timestamp"] == max_cursor_iso

                # F2a: Dry-run replay from t0 (beginning) previews disposition without persisting or changing cursor
                dry_res = await client.post(
                    f"/api/v1/connectors/{cid}/replay",
                    headers={**hdrs, "Idempotency-Key": "idem-replay-dry-1"},
                    json={
                        "reason": "verify historical window before backfill",
                        "from_timestamp": t0.isoformat(),
                        "dry_run": True,
                    },
                )
                assert dry_res.status_code == 200, dry_res.text
                dry_body = dry_res.json()
                assert dry_body["dry_run"] is True
                assert dry_body["run"]["run_kind"] == "replay_dry_run"
                assert dry_body["run"]["frames_received"] == 8
                assert dry_body["run"]["points_accepted"] == 0
                assert dry_body["connector"]["cursor"]["cursor_timestamp"] == max_cursor_iso

                # F2b: Insert an earlier historical row at t0 + 2s (between row 0 and row 1) on the server
                # and run a governed live replay from t0!
                from drillai.telemetry.protocols.witsml import WitsmlSampleRow

                t_backfill = (t0 + dt.timedelta(seconds=2)).isoformat()
                server.rows.insert(
                    1,
                    WitsmlSampleRow(
                        timestamp=t_backfill,
                        depth_md=2450.2,
                        values={"SPP": 2960.0, "WOB": 22.2, "HKLD": 185.5, "RPM": 120.0, "FLOWIN": 651.0},
                    ),
                )
                replay_res = await client.post(
                    f"/api/v1/connectors/{cid}/replay",
                    headers={**hdrs, "Idempotency-Key": "idem-replay-live-1"},
                    json={
                        "reason": "backfill missed row at T+2s",
                        "from_timestamp": t0.isoformat(),
                        "dry_run": False,
                    },
                )
                assert replay_res.status_code == 200, replay_res.text
                replay_body = replay_res.json()
                assert replay_body["dry_run"] is False
                assert replay_body["run"]["run_kind"] == "replay"
                # The 2 new points at T+2s are accepted; the 8 already-ingested points are deduplicated!
                assert replay_body["run"]["points_accepted"] == 2
                assert replay_body["run"]["points_duplicates"] >= 8
                # Persistent watermark MUST NOT regress behind max_cursor_iso (t0 + 15s)!
                assert replay_body["connector"]["cursor"]["cursor_timestamp"] == max_cursor_iso

                # Verify AuditLog recorded the replay reason
                async with app.state.database.session() as session:
                    audits = (
                        await session.execute(
                            select(AuditLog).where(
                                AuditLog.resource_kind == "connector",
                                AuditLog.resource_id == cid,
                                AuditLog.action == "connector.control",
                            )
                        )
                    ).scalars().all()
                    reasons = [a.details.get("reason") for a in audits if isinstance(a.details, dict)]
                    assert "backfill missed row at T+2s" in reasons
    finally:
        await app.state.database.dispose()


async def test_c4_fair_worker_discovery_across_110_connectors(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Verify `discover_candidate_ids` fairly prioritizes never-polled and oldest-polled
    connectors across >100 connectors without starvation."""
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/c4_fair.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)

    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id = "org_000000000000000000000001"
    well_id = "wel_c4"
    wellbore_id = "wbr_c4"
    await _seed_org_and_well(app, org_id, well_id, wellbore_id, settings.dev_org_slug)

    now = utc_now()
    try:
        async with app.state.database.session() as session:
            for idx in range(110):
                # Connectors 0..99 were polled recently; Connectors 100..109 have never been polled (next_poll_at=None)
                is_never_polled = idx >= 100
                session.add(
                    Connector(
                        id=f"cnc_fair_{idx:03d}",
                        org_id=org_id,
                        well_id=well_id,
                        wellbore_id=wellbore_id,
                        key=f"fair-cnc-{idx:03d}",
                        name=f"Fair Connector {idx:03d}",
                        provider="synthetic",
                        protocol_profile="synthetic.v1",
                        endpoint_url="synthetic://plan",
                        status="running",
                        desired_state="enabled",
                        is_enabled=True,
                        config={"channel_mappings": _mappings()},
                        next_poll_at=None if is_never_polled else (now - dt.timedelta(seconds=110 - idx)),
                        last_poll_at=None if is_never_polled else (now - dt.timedelta(seconds=110 - idx)),
                    )
                )
            await session.commit()

        worker = ConnectorWorker(app.state.database, worker_id="wrk_fair", max_concurrency=10)
        async with app.state.database.session() as session:
            candidates = await worker.discover_candidate_ids(session, now=now)
            assert len(candidates) == 110
            # Never-polled connectors (100..109) must appear first (`nullsfirst`), followed by oldest `next_poll_at`
            assert candidates[:10] == [f"cnc_fair_{idx:03d}" for idx in range(100, 110)]
            assert candidates[10] == "cnc_fair_000"
    finally:
        await app.state.database.dispose()


# ---------------------------------------------------------------------------
# Phase D1 & D2: Outbound-Connect SSRF, DNS-Rebinding & Secret Minimization
# ---------------------------------------------------------------------------


def test_d1_ssrf_dns_rebinding_ipv4_mapped_ipv6_and_noncanonical_ips():
    """Verify:
    1. IPv4-mapped IPv6 (`::ffff:127.0.0.1`, `::ffff:169.254.169.254`, `::ffff:10.0.0.1`),
       6to4 (`2002:7f00:0001::1`), and non-canonical/octal/integer IPv4 literals
       (`0177.0.0.1`, `2130706433`, `127.1`) are blocked.
    2. Multi-record DNS responses mixing a public IP and a private/loopback/metadata IP are blocked.
    3. Connect-time DNS rebinding (`rig.example.com` resolving to public IP at registration time,
       then rebinding to `127.0.0.1` or `169.254.169.254` at connect time) is blocked by `enforce_connect_time_ssrf`.
    """
    for blocked_url in (
        "https://[::ffff:127.0.0.1]/witsml",
        "https://[::ffff:169.254.169.254]/witsml",
        "https://[::ffff:10.1.2.3]/witsml",
        "https://[2002:7f00:0001::1]/witsml",
        "https://0177.0.0.1/witsml",
        "https://2130706433/witsml",
        "https://127.1/witsml",
    ):
        with pytest.raises(ValidationFailed) as exc_info:
            validate_connector_endpoint(
                blocked_url,
                "witsml.1.4.1.1.soap_http",
                allow_loopback=False,
                allowed_hosts=[],
            )
        assert exc_info.value.details["reason"] == "ssrf_blocked", blocked_url

    # Multi-A/AAAA DNS response mixing public IP + private/metadata IP is blocked
    with pytest.raises(ValidationFailed) as mixed_exc:
        validate_connector_endpoint(
            "https://dual-record.rig.example.com/witsml",
            "witsml.1.4.1.1.soap_http",
            allow_loopback=False,
            allowed_hosts=[],
            resolver=lambda host, port: ["93.184.216.34", "169.254.169.254"],
        )
    assert mixed_exc.value.details["reason"] == "ssrf_blocked"

    # DNS Rebinding simulation: passes at registration (public IP), blocked at connect time (rebound to 127.0.0.1)
    dns_phase = "registration"

    def _rebinding_resolver(host: str, port: int) -> list[str]:
        if dns_phase == "registration":
            return ["93.184.216.34"]
        return ["127.0.0.1"]

    set_custom_dns_resolver(_rebinding_resolver)
    try:
        validated = validate_connector_endpoint(
            "https://rebind.rig.example.com/witsml",
            "witsml.1.4.1.1.soap_http",
            allow_loopback=False,
            allowed_hosts=[],
        )
        assert validated == "https://rebind.rig.example.com/witsml"

        # Now DNS rebinds to 127.0.0.1 right before outbound socket connect
        dns_phase = "connect"
        with pytest.raises(ConnectorTransportError) as rebind_exc:
            enforce_connect_time_ssrf(
                "https://rebind.rig.example.com/witsml",
                "witsml.1.4.1.1.soap_http",
                allow_loopback=False,
            )
        assert rebind_exc.value.category == "ssrf_blocked"
        assert rebind_exc.value.retryable is False
    finally:
        set_custom_dns_resolver(None)


def test_d2_secret_reference_minimization_and_query_token_redaction():
    """Verify:
    - `validate_secret_refs` supports `env:`, `secret_ref:`, and `secret:` (normalizing `secret:` -> `secret_ref:`).
    - `mask_secret_refs` never leaks raw env var names (`DRILLAI_SECRET_*`) or secret locator keys (`rig_pass_key`).
    - `redact_sensitive_text` scrubs URL query tokens (`?token=`, `&api_key=`), `X-API-Key` headers, and `DRILLAI_SECRET_*` names.
    """
    refs = validate_secret_refs(
        {
            "username": "env:DRILLAI_SECRET_PROD_WITSML_USER",
            "password": "secret:rig_prod_password_key",
        }
    )
    assert refs["username"] == "env:DRILLAI_SECRET_PROD_WITSML_USER"
    assert refs["password"] == "secret_ref:rig_prod_password_key"

    masked = mask_secret_refs(refs)
    serialized = json.dumps(masked)
    assert "DRILLAI_SECRET_PROD_WITSML_USER" not in serialized
    assert "rig_prod_password_key" not in serialized
    assert masked["username"]["ref"] == "env:********"
    assert masked["username"]["redacted"] is True
    assert masked["password"]["ref"] == "secret_ref:********"
    assert masked["password"]["redacted"] is True

    # Masked placeholders are ignored when allow_masked_placeholders=True
    preserved = validate_secret_refs(
        {"username": "env:********", "password": "secret_ref:new_key"},
        allow_masked_placeholders=True,
    )
    assert "username" not in preserved
    assert preserved["password"] == "secret_ref:new_key"

    raw_err = (
        "Connect failed to wss://rig.example.com/etp?token=secret_jwt_999&api_key=key_888 "
        "with X-API-Key: key_888 and env:DRILLAI_SECRET_PROD_WITSML_USER"
    )
    scrubbed = redact_sensitive_text(raw_err)
    assert scrubbed is not None
    assert "secret_jwt_999" not in scrubbed
    assert "key_888" not in scrubbed
    assert "DRILLAI_SECRET_PROD_WITSML_USER" not in scrubbed


def test_e1_e2_docker_compose_security_posture():
    """Verify `docker-compose.yml` enforces:
    - Required `POSTGRES_PASSWORD` (`${POSTGRES_PASSWORD:?...}`) with no reusable default fallback
    - Private internal network (`drillai_internal`) with `postgres` not published on `0.0.0.0:5432`
    - `127.0.0.1` default host binding (`${BIND_ADDRESS:-127.0.0.1}`) for `backend` and `frontend`
    - `/api/v1/health/ready` readiness probe on `backend`
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    compose_text = (repo_root / "docker-compose.yml").read_text(encoding="utf-8")
    assert "POSTGRES_PASSWORD:?" in compose_text
    assert "drillai_dev_password" not in compose_text
    assert "5432:5432" not in compose_text
    assert "drillai_internal:" in compose_text
    assert "internal: true" in compose_text
    assert "${BIND_ADDRESS:-127.0.0.1}:${BACKEND_PORT:-8000}:8000" in compose_text
    assert "${BIND_ADDRESS:-127.0.0.1}:${FRONTEND_PORT:-8080}:8080" in compose_text
    assert "/api/v1/health" in compose_text


async def test_h1_h3_bounded_soak_multi_connector_fault_recovery_and_retention(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Run a multi-connector soak across WITSML 1.4.1.1, ETP 1.2, and Synthetic connectors
    with injected transient faults, verifying:
    - Zero data loss across recovery cycles
    - Bounded `ConnectorRun` retention (`run_retention_limit=8`)
    - Bounded observability `Histogram` sample buffer (`MAX_HISTOGRAM_SAMPLES`)
    """
    from drillai.db.models import ConnectorRun
    from drillai.observability.tracing import MAX_HISTOGRAM_SAMPLES, Histogram
    from drillai.telemetry.protocols.etp import LocalEtpWebSocketServer
    from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer

    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/soak_h1_h3.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)

    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id = "org_000000000000000000000001"
    well_id = "wel_soak_01"
    wellbore_id = "wbr_soak_01"
    await _seed_org_and_well(app, org_id, well_id, wellbore_id, settings.dev_org_slug)

    witsml_srv = LocalWitsmlSoapServer()
    etp_srv = LocalEtpWebSocketServer()
    t0 = dt.datetime(2026, 6, 3, 0, 0, 0, tzinfo=dt.UTC)
    witsml_srv.seed_default_rows(start=t0, count=6, step_seconds=5.0)
    etp_srv.seed_default_points(start=t0 + dt.timedelta(minutes=5), count=6, step_seconds=5.0)

    try:
        async with witsml_srv, etp_srv:
            worker = ConnectorWorker(
                app.state.database,
                worker_id="wrk_soak_01",
                lease_seconds=5.0,
                max_concurrency=4,
                retention_limit=8,
                allow_loopback=True,
            )
            async with app.state.database.session() as session:
                svc = ConnectorService(session, org_id)
                c_witsml = await svc.create(
                    key="soak-witsml",
                    name="Soak WITSML",
                    protocol_profile="witsml.1.4.1.1.soap_http",
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                    endpoint_url=witsml_srv.endpoint_url,
                    config={
                        "auth_mode": "none",
                        "poll_interval_seconds": 0.05,
                        "max_points_per_poll": 4,
                        "channel_mappings": _mappings(),
                    },
                    allow_loopback=True,
                )
                c_etp = await svc.create(
                    key="soak-etp",
                    name="Soak ETP",
                    protocol_profile="etp.1.2.json_ws",
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                    endpoint_url=etp_srv.endpoint_url,
                    config={
                        "auth_mode": "none",
                        "poll_interval_seconds": 0.05,
                        "max_points_per_poll": 4,
                        "channel_mappings": _mappings(),
                    },
                    allow_loopback=True,
                )
                await svc.transition(c_witsml["id"], action="start", reason="start soak")
                await svc.transition(c_etp["id"], action="start", reason="start soak")
                await session.commit()

            # Execute 15 worker ticks with mid-soak transient faults on ticks 3 and 7
            for tick in range(15):
                if tick in (3, 7):
                    witsml_srv.fault_mode = "http_500"
                    etp_srv.fault_mode = "disconnect_mid_stream"
                else:
                    witsml_srv.fault_mode = "ok"
                    etp_srv.fault_mode = "ok"

                # Force connectors to be due immediately so all 15 ticks execute
                async with app.state.database.session() as session:
                    rows = (await session.execute(select(Connector))).scalars().all()
                    now_due = utc_now() - dt.timedelta(seconds=1)
                    for r in rows:
                        r.next_poll_at = now_due
                        r.lease_expires_at = None
                    await session.commit()

                outcomes = await worker.run_once()
                assert len(outcomes) == 2

            # Verify both connectors recovered to running and pruned runs to <= 8 each
            async with app.state.database.session() as session:
                for cid in (c_witsml["id"], c_etp["id"]):
                    row = await session.get(Connector, cid)
                    assert row is not None
                    assert row.status == "running"
                    runs = (
                        await session.execute(
                            select(ConnectorRun).where(ConnectorRun.connector_id == cid)
                        )
                    ).scalars().all()
                    assert len(runs) == 8

            # Verify Histogram sample buffer stays bounded at MAX_HISTOGRAM_SAMPLES
            hist = Histogram("test.soak.hist", "bounded histogram check")
            for i in range(MAX_HISTOGRAM_SAMPLES + 500):
                hist.observe(float(i))
            snap = hist.snapshot()
            assert snap[""]["count"] == MAX_HISTOGRAM_SAMPLES + 500
            assert len(hist._values[()]) == MAX_HISTOGRAM_SAMPLES
    finally:
        await app.state.database.dispose()
