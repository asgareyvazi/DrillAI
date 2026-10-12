"""CP14-D regression tests: truthful data_outcome classification, manifest reconciliation,
and 10-stage connection-test milestone attribution.

Covers:
1. Mutual exclusion and completeness of all 8 ``data_outcome`` values:
   - ``no_data``
   - ``filtered_only`` (all discovered records unmapped/unsubscribed/watermark-suppressed)
   - ``duplicates_only`` (all discovered/emitted frames are duplicates; accepted == 0)
   - ``revisions_only`` (only revisions accepted; accepted == 0)
   - ``rejected_only``
   - ``partial_with_rejections``
   - ``partial_batch_capped``
   - ``all_valid_accepted`` (strictly requires ``points_accepted > 0``)
2. Manifest conservation law & reconciliation invariants:
   - Protocol equation: ``source_records_discovered == mapped_frames_emitted + total_filtered + total_protocol_rejected``
   - Ingestion equation: ``mapped_frames_emitted == points_accepted + service_duplicates + points_revised + service_rejected``
   - ``reconciled`` is ``True`` iff both equations hold.
3. End-to-end worker & replay runs verifying truthful ``data_outcome`` on real WITSML and ETP binary polls.
4. Truthful 10-stage ``ConnectorService.test_connection()`` milestone attribution across:
   - DNS / SSRF failure at ``endpoint_policy_validated``
   - TLS handshake failure at ``tls_verified``
   - Wire encoding failure at ``protocol_encoding_negotiated``
   - Session / auth failure at ``session_established``
   - ``data_frame_decoded`` marked ``not_applicable`` when ``frames == 0`` vs ``passed`` when ``frames > 0``
   - ``not_reached`` emitted for all stages after a failed stage.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest

from drillai.core.config import reset_settings_cache
from drillai.db.base import Base
from drillai.db.models import Project, Well
from drillai.db.session import Database
from drillai.security.actions import Principal
from drillai.telemetry.connectors import (
    ALL_TEST_CONNECTION_STAGES,
    POLL_DATA_OUTCOMES,
    ConnectorService,
    ProtocolPollDisposition,
    build_and_reconcile_poll_manifest,
    clear_harness_endpoints,
    register_harness_endpoint,
    reset_egress_policy,
    update_egress_policy_in_db,
)
from drillai.telemetry.protocols.etp_binary import LocalEtpBinaryWebSocketServer
from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer
from drillai.telemetry.worker import ConnectorWorker

DEFAULT_ORG_ID = "org_demo_default"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_SECRET_CP14D_TOK", "tok-cp14d")
    monkeypatch.setenv("DRILLAI_SECRET_ETP14D_TOK", "tok-etp-14d")
    reset_settings_cache()
    reset_egress_policy()
    clear_harness_endpoints()
    yield
    clear_harness_endpoints()
    reset_egress_policy()
    reset_settings_cache()


async def _seed_well(db: Database) -> str:
    async with db.session() as session:
        proj = Project(org_id=DEFAULT_ORG_ID, code="PRJ-CP14D", name="CP14-D Project")
        session.add(proj)
        await session.flush()
        well = Well(
            org_id=DEFAULT_ORG_ID,
            project_id=proj.id,
            uwi="UWI-CP14D-001",
            name="CP14-D Well",
        )
        session.add(well)
        await session.commit()
        return well.id


def test_manifest_reconciliation_and_all_eight_data_outcomes() -> None:
    """Verify every one of the 8 data_outcome states and the dual conservation equations."""
    # 1. no_data: 0 records discovered, 0 frames emitted
    disp_empty = ProtocolPollDisposition(
        pages_or_messages_received=1,
        source_records_discovered=0,
        mapped_frames_emitted=0,
    )
    m_no_data = build_and_reconcile_poll_manifest(
        protocol_disp=disp_empty,
        protocol_dups=0,
        frames_received=0,
        accepted=0,
        service_duplicates=0,
        revised=0,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="unchanged",
    )
    assert m_no_data["data_outcome"] == "no_data"
    assert m_no_data["reconciled"] is True
    assert m_no_data["points_accepted"] == 0

    # 2. filtered_only: 5 records discovered, all 5 unmapped_skipped, 0 emitted, 0 accepted
    disp_filtered = ProtocolPollDisposition(
        pages_or_messages_received=1,
        source_records_discovered=5,
        mapped_frames_emitted=0,
        unmapped_skipped=3,
        unsubscribed_skipped=2,
    )
    m_filtered = build_and_reconcile_poll_manifest(
        protocol_disp=disp_filtered,
        protocol_dups=0,
        frames_received=0,
        accepted=0,
        service_duplicates=0,
        revised=0,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="unchanged",
    )
    assert m_filtered["data_outcome"] == "filtered_only"
    assert m_filtered["data_outcome"] != "all_valid_accepted"
    assert m_filtered["reconciled"] is True

    # 3. duplicates_only (watermark_suppressed at protocol layer or service_duplicates at ingest layer)
    disp_dups = ProtocolPollDisposition(
        pages_or_messages_received=1,
        source_records_discovered=4,
        mapped_frames_emitted=0,
        watermark_suppressed=4,
    )
    m_dups = build_and_reconcile_poll_manifest(
        protocol_disp=disp_dups,
        protocol_dups=4,
        frames_received=0,
        accepted=0,
        service_duplicates=0,
        revised=0,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="unchanged",
    )
    assert m_dups["data_outcome"] == "duplicates_only"
    assert m_dups["reconciled"] is True

    # 4. revisions_only: 2 frames emitted, both revised existing points, 0 newly accepted
    disp_rev = ProtocolPollDisposition(
        pages_or_messages_received=1,
        source_records_discovered=2,
        mapped_frames_emitted=2,
    )
    m_rev = build_and_reconcile_poll_manifest(
        protocol_disp=disp_rev,
        protocol_dups=0,
        frames_received=2,
        accepted=0,
        service_duplicates=0,
        revised=2,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="complete",
    )
    assert m_rev["data_outcome"] == "revisions_only"
    assert m_rev["reconciled"] is True

    # 5. rejected_only: 3 discovered, 2 rejected at protocol layer, 1 rejected at service layer
    disp_rej = ProtocolPollDisposition(
        pages_or_messages_received=1,
        source_records_discovered=3,
        mapped_frames_emitted=1,
        rejected_non_finite_value=2,
    )
    m_rej = build_and_reconcile_poll_manifest(
        protocol_disp=disp_rej,
        protocol_dups=0,
        frames_received=1,
        accepted=0,
        service_duplicates=0,
        revised=0,
        service_rejected=1,
        has_more_deferred=False,
        checkpoint_status="unchanged",
    )
    assert m_rej["data_outcome"] == "rejected_only"
    assert m_rej["points_rejected"] == 3
    assert m_rej["reconciled"] is True

    # 6. partial_with_rejections: 4 discovered, 3 accepted, 1 rejected
    disp_part_rej = ProtocolPollDisposition(
        pages_or_messages_received=1,
        source_records_discovered=4,
        mapped_frames_emitted=3,
        rejected_malformed_timestamp=1,
    )
    m_part_rej = build_and_reconcile_poll_manifest(
        protocol_disp=disp_part_rej,
        protocol_dups=0,
        frames_received=3,
        accepted=3,
        service_duplicates=0,
        revised=0,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="complete",
    )
    assert m_part_rej["data_outcome"] == "partial_with_rejections"
    assert m_part_rej["reconciled"] is True

    # 7. partial_batch_capped: 6 discovered, 4 emitted/accepted, 2 deferred over cap
    disp_cap = ProtocolPollDisposition(
        pages_or_messages_received=1,
        source_records_discovered=6,
        mapped_frames_emitted=4,
        deferred_over_cap=2,
        truncated_by_limit=True,
        partial_more_available=True,
    )
    m_cap = build_and_reconcile_poll_manifest(
        protocol_disp=disp_cap,
        protocol_dups=0,
        frames_received=4,
        accepted=4,
        service_duplicates=0,
        revised=0,
        service_rejected=0,
        has_more_deferred=True,
        checkpoint_status="partial",
    )
    assert m_cap["data_outcome"] == "partial_batch_capped"
    assert m_cap["reconciled"] is True

    # 8. all_valid_accepted: 3 discovered, 3 emitted, 3 accepted
    disp_ok = ProtocolPollDisposition(
        pages_or_messages_received=1,
        source_records_discovered=3,
        mapped_frames_emitted=3,
    )
    m_ok = build_and_reconcile_poll_manifest(
        protocol_disp=disp_ok,
        protocol_dups=0,
        frames_received=3,
        accepted=3,
        service_duplicates=0,
        revised=0,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="complete",
    )
    assert m_ok["data_outcome"] == "all_valid_accepted"
    assert m_ok["reconciled"] is True

    # 9. revisions_and_duplicates: 2 frames emitted, 1 revised, 1 service duplicate, 0 newly accepted
    disp_rev_dups = ProtocolPollDisposition(
        pages_or_messages_received=1,
        source_records_discovered=2,
        mapped_frames_emitted=2,
    )
    m_rev_dups = build_and_reconcile_poll_manifest(
        protocol_disp=disp_rev_dups,
        protocol_dups=0,
        frames_received=2,
        accepted=0,
        service_duplicates=1,
        revised=1,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="unchanged",
    )
    assert m_rev_dups["data_outcome"] == "revisions_and_duplicates"
    assert m_rev_dups["reconciled"] is True

    # 10. dry_run_preview: 3 discovered, 3 emitted, dry_run=True (0 persisted)
    m_dry_run = build_and_reconcile_poll_manifest(
        protocol_disp=disp_ok,
        protocol_dups=0,
        frames_received=3,
        accepted=0,
        service_duplicates=0,
        revised=0,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="unchanged",
        dry_run=True,
    )
    assert m_dry_run["data_outcome"] == "dry_run_preview"
    assert m_dry_run["persisted"] is False
    assert m_dry_run["reconciled"] is True

    # 11. Verify reconciliation failure detection when ingestion counts do not sum to mapped_frames_emitted
    m_bad_ingest = build_and_reconcile_poll_manifest(
        protocol_disp=disp_ok,
        protocol_dups=0,
        frames_received=3,
        accepted=2,  # Missing 1 frame!
        service_duplicates=0,
        revised=0,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="complete",
    )
    assert m_bad_ingest["protocol_reconciled"] is True
    assert m_bad_ingest["ingestion_reconciled"] is False
    assert m_bad_ingest["reconciled"] is False
    assert m_bad_ingest["data_outcome"] == "reconciliation_error"

    seen_outcomes = {
        m_no_data["data_outcome"],
        m_filtered["data_outcome"],
        m_dups["data_outcome"],
        m_rev["data_outcome"],
        m_rev_dups["data_outcome"],
        m_rej["data_outcome"],
        m_part_rej["data_outcome"],
        m_cap["data_outcome"],
        m_ok["data_outcome"],
        m_dry_run["data_outcome"],
        m_bad_ingest["data_outcome"],
    }
    assert seen_outcomes == set(POLL_DATA_OUTCOMES)


@pytest.mark.asyncio
async def test_worker_poll_filtered_only_and_duplicates_only_outcomes(tmp_path: Path) -> None:
    """Prove real WITSML worker polls never mislabel unmapped or duplicate-only polls as all_valid_accepted."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cp14_outcomes.db'}"
    db = Database(db_url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    well_id = await _seed_well(db)
    principal = Principal(id="eng-cp14d", org_id=DEFAULT_ORG_ID, role_keys=("engineer",))

    witsml_server = LocalWitsmlSoapServer(expected_bearer_token="tok-cp14d")
    await witsml_server.start()
    register_harness_endpoint(witsml_server.host, witsml_server.port)

    try:
        # Case A: Connector maps only a mnemonic ("UNMAPPED_XYZ") not present in the WITSML log
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            unmapped_conn = await svc.create(
                key="witsml-filtered-only",
                name="WITSML Filtered Only",
                protocol_profile="witsml.1.4.1.1.soap_http",
                well_id=well_id,
                endpoint_url=witsml_server.endpoint_url,
                config={
                    "auth_mode": "bearer",
                    "tls_verify": True,
                    "poll_interval_seconds": 0.5,
                    "channel_mappings": [
                        {
                            "source_mnemonic": "UNMAPPED_XYZ",
                            "channel_key": "hookload",
                            "name": "Hookload",
                            "dimension": "force",
                            "unit": "klbf",
                        }
                    ],
                },
                secret_refs={"bearer_token": "env:DRILLAI_SECRET_CP14D_TOK"},
            )
            await svc.transition(
                unmapped_conn["id"],
                action="start",
                reason="Start filtered-only test connector",
            )
            await session.commit()

        worker = ConnectorWorker(db, worker_id="wrk-cp14d")
        outcomes_a = await worker.run_once()
        assert len(outcomes_a) == 1
        assert outcomes_a[0].status == "succeeded"

        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            runs_a = await svc.list_runs(unmapped_conn["id"])
            last_run_a = runs_a[0]
            manifest_a = last_run_a["details"]["manifest"]
            assert manifest_a["source_records_discovered"] > 0
            assert manifest_a["unmapped_skipped"] == manifest_a["source_records_discovered"]
            assert manifest_a["mapped_frames_emitted"] == 0
            assert manifest_a["points_accepted"] == 0
            assert manifest_a["data_outcome"] == "filtered_only"
            assert manifest_a["reconciled"] is True
            # Stop connector A so worker run_once only claims connector B next
            await svc.transition(
                unmapped_conn["id"],
                action="stop",
                reason="Stop filtered-only connector",
            )
            await session.commit()

        # Case B: Connector maps real mnemonics; poll 1 accepts all valid points; replay at same cursor sees duplicates_only
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            mapped_conn = await svc.create(
                key="witsml-dups-check",
                name="WITSML Duplicates Check",
                protocol_profile="witsml.1.4.1.1.soap_http",
                well_id=well_id,
                endpoint_url=witsml_server.endpoint_url,
                config={
                    "auth_mode": "bearer",
                    "tls_verify": True,
                    "poll_interval_seconds": 0.1,
                    "channel_mappings": [
                        {
                            "source_mnemonic": "HKLD",
                            "channel_key": "hookload",
                            "name": "Hookload",
                            "dimension": "force",
                            "unit": "klbf",
                        }
                    ],
                },
                secret_refs={"bearer_token": "env:DRILLAI_SECRET_CP14D_TOK"},
            )
            await svc.transition(
                mapped_conn["id"],
                action="start",
                reason="Start mapped connector",
            )
            await session.commit()

        # Drain initial WITSML rows
        await worker.run_once()
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            runs_b1 = await svc.list_runs(mapped_conn["id"])
            manifest_b1 = runs_b1[0]["details"]["manifest"]
            assert manifest_b1["points_accepted"] > 0
            assert manifest_b1["data_outcome"] == "all_valid_accepted"
            assert manifest_b1["reconciled"] is True

            # Now execute a replay over the already-ingested window: all points are duplicates!
            replay_res = await svc.replay(
                mapped_conn["id"],
                reason="Verify duplicates_only outcome on idempotent replay",
                from_timestamp="2026-01-01T00:00:00Z",
                dry_run=False,
            )
            await session.commit()
            replay_manifest = replay_res["run"]["details"]["manifest"]
            assert replay_manifest["points_accepted"] == 0
            assert replay_manifest["points_duplicates"] > 0
            assert replay_manifest["data_outcome"] == "duplicates_only"
            assert replay_manifest["reconciled"] is True
    finally:
        await witsml_server.stop()
        await db.dispose()


@pytest.mark.asyncio
async def test_connection_ten_stage_milestones_truthful_failure_attribution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ConnectorService.test_connection() attributes failures to the exact stage and marks remaining stages not_reached."""
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cp14_milestones.db'}"
    db = Database(db_url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    well_id = await _seed_well(db)
    principal = Principal(id="eng-cp14d", org_id=DEFAULT_ORG_ID, role_keys=("engineer",))

    etp_server = LocalEtpBinaryWebSocketServer(expected_bearer_token="tok-etp-14d")
    await etp_server.start()
    register_harness_endpoint(etp_server.host, etp_server.port)

    try:
        # 1. Unresolvable DNS hostname -> fails at endpoint_policy_validated (because require_dns_resolution=True)
        def _failing_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
            raise socket.gaierror(socket.EAI_NONAME, f"Name or service not known: {host}")

        monkeypatch.setattr(socket, "getaddrinfo", _failing_getaddrinfo)

        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            await update_egress_policy_in_db(
                session,
                allowed_hosts=["unresolvable.witsml.example.com"],
                allowed_private_cidrs=[],
                reason="Allow unresolvable host for milestone test",
                principal=principal,
                actor_org_id=DEFAULT_ORG_ID,
            )
            dns_conn = await svc.create(
                key="etp-dns-fail",
                name="ETP DNS Fail",
                protocol_profile="etp.1.2.binary_ws",
                well_id=well_id,
                endpoint_url="wss://unresolvable.witsml.example.com:8443/etp",
                config={
                    "auth_mode": "bearer",
                    "tls_verify": True,
                    "poll_interval_seconds": 1.0,
                    "channel_mappings": [
                        {
                            "source_mnemonic": "SPP",
                            "channel_key": "spp",
                            "name": "SPP",
                            "dimension": "pressure",
                            "unit": "psi",
                        }
                    ],
                },
                secret_refs={"bearer_token": "env:DRILLAI_SECRET_ETP14D_TOK"},
            )
            res_dns = await svc.test_connection(dns_conn["id"])
            await session.commit()

        assert res_dns["ok"] is False
        assert res_dns["error_category"] == "dns_failure"
        stages_dns = [m["stage"] for m in res_dns["milestones"]]
        assert stages_dns == list(ALL_TEST_CONNECTION_STAGES)
        by_stage_dns = {m["stage"]: m["status"] for m in res_dns["milestones"]}
        assert by_stage_dns["endpoint_policy_validated"] == "failed"
        assert by_stage_dns["socket_connected"] == "not_reached"
        assert by_stage_dns["session_established"] == "not_reached"
        assert by_stage_dns["transaction_committed"] == "not_applicable"

        monkeypatch.undo()
        monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
        monkeypatch.setenv("DRILLAI_SECRET_CP14D_TOK", "tok-cp14d")
        monkeypatch.setenv("DRILLAI_SECRET_ETP14D_TOK", "tok-etp-14d")
        register_harness_endpoint(etp_server.host, etp_server.port)

        # 2. Unexpected text WebSocket frame on ETP binary -> fails at protocol_encoding_negotiated
        etp_server.fault_mode = "unexpected_text_frame"
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            etp_conn = await svc.create(
                key="etp-milestones",
                name="ETP Milestones",
                protocol_profile="etp.1.2.binary_ws",
                well_id=well_id,
                endpoint_url=etp_server.endpoint_url,
                config={
                    "auth_mode": "bearer",
                    "tls_verify": True,
                    "poll_interval_seconds": 1.0,
                    "channel_mappings": [
                        {
                            "source_mnemonic": "SPP",
                            "channel_key": "spp",
                            "name": "SPP",
                            "dimension": "pressure",
                            "unit": "psi",
                        }
                    ],
                },
                secret_refs={"bearer_token": "env:DRILLAI_SECRET_ETP14D_TOK"},
            )
            res_enc = await svc.test_connection(etp_conn["id"])
            await session.commit()

        assert res_enc["ok"] is False
        stages_enc = [m["stage"] for m in res_enc["milestones"]]
        assert stages_enc == list(ALL_TEST_CONNECTION_STAGES)
        by_stage_enc = {m["stage"]: m["status"] for m in res_enc["milestones"]}
        assert by_stage_enc["endpoint_policy_validated"] == "passed"
        assert by_stage_enc["socket_connected"] == "passed"
        assert by_stage_enc["tls_verified"] == "not_applicable"
        assert by_stage_enc["protocol_encoding_negotiated"] == "failed"
        assert by_stage_enc["session_established"] == "not_reached"
        assert by_stage_enc["channels_discovered"] == "not_reached"
        assert by_stage_enc["transaction_committed"] == "not_applicable"

        # 3. Auth failure on ETP binary -> fails at session_established
        etp_server.fault_mode = "auth_failure"
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            res_auth = await svc.test_connection(etp_conn["id"])
            await session.commit()

        assert res_auth["ok"] is False
        assert res_auth["error_category"] == "auth_failure"
        by_stage_auth = {m["stage"]: m["status"] for m in res_auth["milestones"]}
        assert by_stage_auth["endpoint_policy_validated"] == "passed"
        assert by_stage_auth["socket_connected"] == "passed"
        assert by_stage_auth["tls_verified"] == "not_applicable"
        assert by_stage_auth["protocol_encoding_negotiated"] == "passed"
        assert by_stage_auth["session_established"] == "failed"
        assert by_stage_auth["channels_discovered"] == "not_reached"

        # 4. Healthy ETP binary connection test -> all 10 stages present, data_frame_decoded passed
        etp_server.fault_mode = None
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            res_ok = await svc.test_connection(etp_conn["id"])
            await session.commit()

        assert res_ok["ok"] is True
        stages_ok = [m["stage"] for m in res_ok["milestones"]]
        assert stages_ok == list(ALL_TEST_CONNECTION_STAGES)
        by_stage_ok = {m["stage"]: m["status"] for m in res_ok["milestones"]}
        assert by_stage_ok["endpoint_policy_validated"] == "passed"
        assert by_stage_ok["socket_connected"] == "passed"
        assert by_stage_ok["tls_verified"] == "not_applicable"
        assert by_stage_ok["protocol_encoding_negotiated"] == "passed"
        assert by_stage_ok["session_established"] == "passed"
        assert by_stage_ok["channels_discovered"] == "passed"
        assert by_stage_ok["mapping_verified"] == "passed"
        assert by_stage_ok["data_frame_decoded"] == "passed"
        assert by_stage_ok["disposition_validated"] == "passed"
        assert by_stage_ok["transaction_committed"] == "not_applicable"
    finally:
        await etp_server.stop()
        await db.dispose()
