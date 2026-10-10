"""Tests for Checkpoint B: Real WITSML 1.4.1.1 SOAP polling client, ETP 1.2 WebSocket subscription
client, and local protocol-compatible test servers."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from drillai.api.app import create_app
from drillai.core.config import get_settings, reset_settings_cache
from drillai.db.models import Base, Organization, Project, Well, Wellbore
from drillai.telemetry.adapters import ingest_frames
from drillai.telemetry.connectors import ConnectorTransportError
from drillai.telemetry.etp_client import EtpSubscriptionAdapter
from drillai.telemetry.protocols.etp import EtpWebSocketClient, LocalEtpWebSocketServer
from drillai.telemetry.protocols.witsml import (
    LocalWitsmlSoapServer,
    WitsmlSoapClient,
    parse_get_from_store_response,
)
from drillai.telemetry.service import TelemetryService
from drillai.telemetry.witsml_client import WitsmlPollingAdapter


def _mappings() -> list[dict[str, Any]]:
    return [
        {
            "source_mnemonic": "SPP",
            "channel_key": "spp",
            "name": "Standpipe Pressure",
            "dimension": "pressure",
            "unit": "psi",
        },
        {
            "source_mnemonic": "WOB",
            "channel_key": "wob",
            "name": "Weight on Bit",
            "dimension": "force",
            "unit": "klbf",
        },
        {
            "source_mnemonic": "RPM",
            "channel_key": "rpm",
            "name": "Rotary Speed",
            "dimension": "rotary_speed",
            "unit": "rpm",
        },
    ]


async def test_witsml_soap_polling_pagination_watermark_and_faults():
    start_dt = dt.datetime(2026, 4, 10, 12, 0, 0, tzinfo=dt.UTC)
    server = LocalWitsmlSoapServer(
        expected_username="rig_witsml",
        expected_password="witsml_password_123",
        page_size_override=2,
    )
    server.seed_default_rows(start=start_dt, count=5, step_seconds=5.0)
    # Set one row's SPP to None (-999.25 nullValue) to verify missing quality handling
    server.rows[1].values["SPP"] = None

    async with server:
        # 1. Wrong credentials fail immediately with auth_failure (retryable=False)
        bad_client = WitsmlSoapClient(
            endpoint_url=server.endpoint_url,
            secrets={"username": "rig_witsml", "password": "wrong_password"},
            timeout_seconds=3.0,
            max_retries=1,
        )
        with pytest.raises(ConnectorTransportError) as exc_info:
            await bad_client.poll_log_batch(
                well_id="wel_01",
                config={"channel_mappings": _mappings()},
            )
        assert exc_info.value.category == "auth_failure"
        assert exc_info.value.retryable is False

        # 2. Valid credentials with page_size_override=2 and max_pages=2 fetches 4 rows (12 mapped frames)
        good_client = WitsmlSoapClient(
            endpoint_url=server.endpoint_url,
            secrets={"username": "rig_witsml", "password": "witsml_password_123"},
            timeout_seconds=5.0,
            max_retries=1,
        )
        batch1 = await good_client.poll_log_batch(
            well_id="wel_01",
            config={"channel_mappings": _mappings()},
            watermark={},
            max_pages=2,
        )
        assert batch1.pages_fetched == 2
        assert batch1.raw_rows_parsed == 4
        assert batch1.partial_more_available is True
        assert len(batch1.frames) == 12  # 4 timestamps * 3 mapped curves (SPP, WOB, RPM)
        missing_frames = [f for f in batch1.frames if f.value is None]
        assert len(missing_frames) == 1
        assert missing_frames[0].quality == "missing"

        # 3. Subsequent poll starting from batch1.next_watermark fetches only the 5th row (3 frames)
        batch2 = await good_client.poll_log_batch(
            well_id="wel_01",
            config={"channel_mappings": _mappings()},
            watermark=batch1.next_watermark,
            max_pages=2,
        )
        assert batch2.raw_rows_parsed == 1
        assert batch2.partial_more_available is False
        assert len(batch2.frames) == 3

        # 4. Subsequent poll with no new rows returns 0 frames and preserves watermark
        batch3 = await good_client.poll_log_batch(
            well_id="wel_01",
            config={"channel_mappings": _mappings()},
            watermark=batch2.next_watermark,
        )
        assert len(batch3.frames) == 0
        assert batch3.next_watermark["cursor_timestamp"] == batch2.next_watermark["cursor_timestamp"]

        # 5. Fault modes: malformed_xml and http_500
        server.fault_mode = "malformed_xml"
        with pytest.raises(ConnectorTransportError) as xml_exc:
            await good_client.poll_log_batch(
                well_id="wel_01",
                config={"channel_mappings": _mappings()},
            )
        assert xml_exc.value.category == "malformed_payload"

        server.fault_mode = "http_500"
        with pytest.raises(ConnectorTransportError) as http_exc:
            await good_client.poll_log_batch(
                well_id="wel_01",
                config={"channel_mappings": _mappings()},
            )
        assert http_exc.value.category == "remote_http_5xx"
        assert http_exc.value.retryable is True


def test_witsml_rejects_doctype_entity_expansion():
    xxe_payload = """<?xml version="1.0"?>
    <!DOCTYPE lolz [<!ENTITY lol "lol">]>
    <logs version="1.4.1.1"></logs>
    """
    with pytest.raises(ConnectorTransportError) as exc_info:
        parse_get_from_store_response(
            xxe_payload,
            mappings=[],
            well_id="wel_01",
        )
    assert exc_info.value.category == "malformed_payload"


async def test_etp_websocket_handshake_streaming_resume_and_faults():
    start_dt = dt.datetime(2026, 4, 10, 13, 0, 0, tzinfo=dt.UTC)
    server = LocalEtpWebSocketServer(expected_bearer_token="etp-secret-token-99")
    server.seed_default_points(start=start_dt, count=4, step_seconds=5.0)

    async with server:
        # 1. Invalid bearer token fails with auth_failure
        bad_client = EtpWebSocketClient(
            endpoint_url=server.endpoint_url,
            secrets={"bearer_token": "wrong-token"},
            timeout_seconds=3.0,
        )
        with pytest.raises(ConnectorTransportError) as auth_exc:
            await bad_client.probe_session()
        assert auth_exc.value.category == "auth_failure"
        assert auth_exc.value.retryable is False

        # 2. Valid bearer token probes session and discovers 5 ETP channels
        good_client = EtpWebSocketClient(
            endpoint_url=server.endpoint_url,
            secrets={"bearer_token": "etp-secret-token-99"},
            timeout_seconds=5.0,
        )
        session_id, channels = await good_client.probe_session()
        assert session_id.startswith("etp-sess-")
        assert len(channels) == 5

        # 3. Poll subscription batch maps 3 configured channels * 4 timestamps = 12 frames
        batch1 = await good_client.poll_subscription_batch(
            well_id="wel_01",
            config={"channel_mappings": _mappings()},
            watermark={},
        )
        assert len(batch1.frames) == 12
        assert "cursor_timestamp" in batch1.next_watermark

        # 4. Resume from watermark returns 0 duplicate points; append 1 new timestamp and resume
        new_ts = (start_dt + dt.timedelta(seconds=25)).isoformat()
        from drillai.telemetry.protocols.etp import EtpSamplePoint

        server.points.append(EtpSamplePoint(1, "SPP", new_ts, 3120.0, 2453.0))
        batch2 = await good_client.poll_subscription_batch(
            well_id="wel_01",
            config={"channel_mappings": _mappings()},
            watermark=batch1.next_watermark,
        )
        assert len(batch2.frames) == 1
        assert batch2.frames[0].channel_key == "spp"
        assert batch2.frames[0].value == 3120.0

        # 5. Replay duplicate suppression within a batch
        server.fault_mode = "replay_duplicates"
        batch_dup = await good_client.poll_subscription_batch(
            well_id="wel_01",
            config={"channel_mappings": _mappings()},
            watermark={},
        )
        assert batch_dup.duplicates_suppressed >= 1

        # 6. Malformed frame fault
        server.fault_mode = "malformed_frame"
        with pytest.raises(ConnectorTransportError) as mal_exc:
            await good_client.poll_subscription_batch(
                well_id="wel_01",
                config={"channel_mappings": _mappings()},
                watermark={},
            )
        assert mal_exc.value.category == "malformed_payload"


async def test_witsml_and_etp_adapters_ingest_via_telemetry_service_and_api(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """End-to-end verification that WitsmlPollingAdapter and EtpSubscriptionAdapter ingest
    through TelemetryService with canonical unit conversion and work via /connectors/{id}/test and /preview."""
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/proto_e2e.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")
    monkeypatch.setenv("DRILLAI_SECRET_WITSML_PASS", "witsml_pass_77")
    monkeypatch.setenv("DRILLAI_SECRET_ETP_TOKEN", "etp_token_77")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)

    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id = "org_000000000000000000000001"
    well_id = "wel_proto_01"
    wellbore_id = "wbr_proto_01"
    async with app.state.database.session() as session:
        session.add(Organization(id=org_id, slug=settings.dev_org_slug, name="Demo Org", kind="operator"))
        session.add(Project(id="prj_proto_01", org_id=org_id, name="Protocol Project"))
        session.add(
            Well(
                id=well_id,
                org_id=org_id,
                project_id="prj_proto_01",
                name="PROTO-WELL-01",
                well_type="development_producer",
                status="drilling",
            )
        )
        session.add(
            Wellbore(
                id=wellbore_id,
                org_id=org_id,
                well_id=well_id,
                name="PROTO-WB-01",
                purpose="main",
                status="active",
                is_active=True,
            )
        )
        await session.commit()

    witsml_server = LocalWitsmlSoapServer(
        expected_username="witsml_user",
        expected_password="witsml_pass_77",
    )
    etp_server = LocalEtpWebSocketServer(expected_bearer_token="etp_token_77")

    try:
        async with witsml_server, etp_server:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                hdrs = {"X-Dev-Roles": "engineer"}

                # Create WITSML connector and run /test and /preview
                w_create = await client.post(
                    "/api/v1/connectors",
                    headers=hdrs,
                    json={
                        "key": "witsml-live-01",
                        "name": "WITSML SOAP Rig Store",
                        "protocol_profile": "witsml.1.4.1.1.soap_http",
                        "well_id": well_id,
                        "wellbore_id": wellbore_id,
                        "endpoint_url": witsml_server.endpoint_url,
                        "config": {
                            "auth_mode": "basic",
                            "channel_mappings": _mappings(),
                        },
                        "secret_refs": {
                            "username": "env:DRILLAI_SECRET_WITSML_USER",
                            "password": "env:DRILLAI_SECRET_WITSML_PASS",
                        },
                    },
                )
                monkeypatch.setenv("DRILLAI_SECRET_WITSML_USER", "witsml_user")
                assert w_create.status_code == 201, w_create.text
                w_id = w_create.json()["id"]

                w_test = await client.post(f"/api/v1/connectors/{w_id}/test", headers=hdrs)
                assert w_test.status_code == 200
                assert w_test.json()["ok"] is True
                assert len(w_test.json()["discovered_channels"]) == 3

                w_prev = await client.post(
                    f"/api/v1/connectors/{w_id}/preview",
                    headers=hdrs,
                    json={"sample_limit": 6},
                )
                assert w_prev.status_code == 200
                assert w_prev.json()["sample_count"] == 6

                # Create ETP connector and run /test and /preview
                e_create = await client.post(
                    "/api/v1/connectors",
                    headers=hdrs,
                    json={
                        "key": "etp-live-01",
                        "name": "ETP 1.2 Rig Stream",
                        "protocol_profile": "etp.1.2.json_ws",
                        "well_id": well_id,
                        "wellbore_id": wellbore_id,
                        "endpoint_url": etp_server.endpoint_url,
                        "config": {
                            "auth_mode": "bearer",
                            "channel_mappings": _mappings(),
                        },
                        "secret_refs": {
                            "bearer_token": "env:DRILLAI_SECRET_ETP_TOKEN",
                        },
                    },
                )
                assert e_create.status_code == 201, e_create.text
                e_id = e_create.json()["id"]

                e_test = await client.post(f"/api/v1/connectors/{e_id}/test", headers=hdrs)
                assert e_test.status_code == 200
                assert e_test.json()["ok"] is True

                e_prev = await client.post(
                    f"/api/v1/connectors/{e_id}/preview",
                    headers=hdrs,
                    json={"sample_limit": 6},
                )
                assert e_prev.status_code == 200
                assert e_prev.json()["sample_count"] == 6

            # Now ingest frames from both adapters through TelemetryService and verify unit conversion
            async with app.state.database.session() as session:
                svc = TelemetryService(session, org_id)
                w_adapter = WitsmlPollingAdapter(
                    endpoint_url=witsml_server.endpoint_url,
                    channel_mappings=_mappings(),
                    secrets={"username": "witsml_user", "password": "witsml_pass_77"},
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                )
                w_report = await ingest_frames(
                    svc,
                    w_adapter,
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                )
                assert w_report.frames > 0
                assert w_report.to_dict()["totals"]["accepted"] > 0
                assert "cursor_timestamp" in w_adapter.cursor

                e_adapter = EtpSubscriptionAdapter(
                    endpoint_url=etp_server.endpoint_url,
                    channel_mappings=_mappings(),
                    secrets={"bearer_token": "etp_token_77"},
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                )
                e_report = await ingest_frames(
                    svc,
                    e_adapter,
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                )
                assert e_report.frames > 0
                assert "cursor_timestamp" in e_adapter.cursor
                await session.commit()

            async with app.state.database.session() as session:
                svc = TelemetryService(session, org_id)
                latest = await svc.latest(well_id=well_id)
                by_key = {r.channel_key: r for r in latest}
                assert "spp" in by_key
                # 2950+ psi converted to canonical Pa (> 20,000,000 Pa)
                assert by_key["spp"].unit == "Pa"
                assert by_key["spp"].value is not None and by_key["spp"].value > 20_000_000.0
    finally:
        await app.state.database.dispose()
        reset_settings_cache()
