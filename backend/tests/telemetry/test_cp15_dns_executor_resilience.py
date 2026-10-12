"""CP15 Phase 5 (CP15-E): DNS Executor Timeout, Cancellation & Saturation Resilience Tests.

Covers all 16 required scenarios:
1. Single fast DNS lookup succeeds and returns semaphore permit to initial count.
2. Single failing DNS lookup (`gaierror`) fails cleanly and returns permit.
3. Single slow DNS lookup exceeding `dns_timeout_seconds` fails with `reason="timeout"` and returns permit.
4. 4 concurrent slow lookups occupying all worker threads + 4 queued lookups.
5. Queued lookups cancelled before execution do not leak permits.
6. Running lookups cancelled while blocked in resolver do not double-release permits when they later exit.
7. Repeated bursts of slow/cancelled lookups (30 requests across multiple waves) do not reduce available permits to zero.
8. After a slow/timeout wave, subsequent healthy DNS lookups succeed within bounded latency even while prior threads remain blocked.
9. Async `resolve_and_pin_endpoint_async` caller cancellation (`asyncio.CancelledError`) releases permits cleanly.
10. Sync `resolve_and_pin_endpoint` timeout path behaves identically and does not leak permits.
11. `enforce_connect_time_ssrf` / `enforce_connect_time_ssrf_async` convert DNS timeout to `ConnectorTransportError(category="timeout", retryable=True)`.
12. `enforce_connect_time_ssrf` / `enforce_connect_time_ssrf_async` convert DNS failure to `ConnectorTransportError(category="dns_failure", retryable=True)`.
13. All IPv4/IPv6/NAT64/6to4/Teredo/IPv4-mapped unpacking rules still block private/loopback/link-local/metadata targets across multi-record responses.
14. WITSML SOAP client, ETP JSON WS client, and ETP Binary WS client all use the resilient async DNS path and recover after DNS timeout bursts.
15. `test_connection()` attributes DNS timeout/failure to `endpoint_policy_validated` with `socket_connected="not_reached"`.
16. Event-loop responsiveness check: concurrent async tasks remain responsive while DNS lookups time out.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from drillai.core.config import reset_settings_cache
from drillai.core.errors import ValidationFailed
from drillai.db.base import Base
from drillai.db.models import Organization, Project, Well
from drillai.db.session import Database
from drillai.security.actions import Principal
from drillai.telemetry.connectors import (
    ConnectorService,
    ConnectorTransportError,
    clear_harness_endpoints,
    enforce_connect_time_ssrf,
    enforce_connect_time_ssrf_async,
    get_dns_executor_stats,
    register_harness_endpoint,
    reset_dns_executor_state,
    reset_egress_policy,
    resolve_and_pin_endpoint,
    resolve_and_pin_endpoint_async,
    set_custom_dns_resolver,
    set_egress_policy,
)
from drillai.telemetry.protocols.etp import EtpWebSocketClient, LocalEtpWebSocketServer
from drillai.telemetry.protocols.etp_binary import (
    EtpBinaryWebSocketClient,
    LocalEtpBinaryWebSocketServer,
)
from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer, WitsmlSoapClient

DEFAULT_ORG_ID = "org_cp15_dns_test"


@pytest.fixture(autouse=True)
def _clean_dns_and_policy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DRILLAI_ENV", "development")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_SECRET_CP15_DNS_TOK", "tok-cp15-dns")
    reset_settings_cache()
    clear_harness_endpoints()
    reset_egress_policy()
    set_custom_dns_resolver(None)
    reset_dns_executor_state()
    yield
    set_custom_dns_resolver(None)
    clear_harness_endpoints()
    reset_egress_policy()
    reset_dns_executor_state()
    reset_settings_cache()


async def _seed_well(db: Database, well_id: str = "well_cp15_dns") -> str:
    async with db.session() as session:
        session.add(Organization(id=DEFAULT_ORG_ID, slug="cp15-dns-org", name="CP15 DNS Org"))
        proj = Project(
            id="prj_cp15_dns",
            org_id=DEFAULT_ORG_ID,
            code="CP15-DNS",
            name="CP15 DNS Project",
        )
        session.add(proj)
        well = Well(
            id=well_id,
            org_id=DEFAULT_ORG_ID,
            project_id=proj.id,
            uwi="UWI-CP15-DNS",
            name="Well CP15 DNS",
        )
        session.add(well)
        await session.commit()
    return well_id


@pytest.mark.asyncio
async def test_dns_permit_lifecycle_fast_gaierror_timeout_and_cancellation() -> None:
    """Scenarios 1, 2, 3, 6, 9, 10, 11, 12:
    Verify exact-once permit lifecycle across fast lookup, gaierror, sync timeout, async timeout,
    caller cancellation while running, and late background thread exit (no double-release).
    """
    initial_stats = get_dns_executor_stats()
    max_permits = initial_stats["max_concurrency"]
    assert initial_stats["available_permits"] == max_permits

    set_egress_policy(
        allowed_hosts=["rig-fast.example.com", "rig-slow.example.com", "rig-fail.example.com"],
        allowed_private_cidrs=["10.20.0.0/16"],
    )

    release_gate = threading.Event()

    def _controlled_resolver(host: str, port: int) -> Sequence[str]:
        if host == "rig-fast.example.com":
            return ["10.20.1.10"]
        if host == "rig-fail.example.com":
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        if host == "rig-slow.example.com":
            release_gate.wait(timeout=2.0)
            return ["10.20.1.11"]
        return ["10.20.1.12"]

    # 1. Fast lookup (sync + async)
    pinned_sync = resolve_and_pin_endpoint(
        "https://rig-fast.example.com:8443/witsml",
        "witsml.1.4.1.1.soap_http",
        resolver=_controlled_resolver,
    )
    assert pinned_sync.pinned_ip == "10.20.1.10"
    assert get_dns_executor_stats()["available_permits"] == max_permits

    pinned_async = await resolve_and_pin_endpoint_async(
        "https://rig-fast.example.com:8443/witsml",
        "witsml.1.4.1.1.soap_http",
        resolver=_controlled_resolver,
    )
    assert pinned_async.pinned_ip == "10.20.1.10"
    assert get_dns_executor_stats()["available_permits"] == max_permits

    # 2. Failing lookup (gaierror) -> ConnectorTransportError(category="dns_failure", retryable=True)
    with pytest.raises(ConnectorTransportError) as exc_fail_sync:
        enforce_connect_time_ssrf(
            "https://rig-fail.example.com:8443/witsml",
            "witsml.1.4.1.1.soap_http",
            resolver=_controlled_resolver,
        )
    assert exc_fail_sync.value.category == "dns_failure"
    assert exc_fail_sync.value.retryable is True
    assert get_dns_executor_stats()["available_permits"] == max_permits

    with pytest.raises(ConnectorTransportError) as exc_fail_async:
        await enforce_connect_time_ssrf_async(
            "https://rig-fail.example.com:8443/witsml",
            "witsml.1.4.1.1.soap_http",
            resolver=_controlled_resolver,
        )
    assert exc_fail_async.value.category == "dns_failure"
    assert exc_fail_async.value.retryable is True
    assert get_dns_executor_stats()["available_permits"] == max_permits

    # 3 & 10 & 11. Slow lookup exceeding dns_timeout_seconds (sync + async) -> category="timeout", retryable=True
    with pytest.raises(ConnectorTransportError) as exc_to_sync:
        enforce_connect_time_ssrf(
            "https://rig-slow.example.com:8443/witsml",
            "witsml.1.4.1.1.soap_http",
            resolver=_controlled_resolver,
            dns_timeout_seconds=0.06,
        )
    assert exc_to_sync.value.category == "timeout"
    assert exc_to_sync.value.retryable is True
    assert get_dns_executor_stats()["available_permits"] == max_permits

    with pytest.raises(ConnectorTransportError) as exc_to_async:
        await enforce_connect_time_ssrf_async(
            "https://rig-slow.example.com:8443/witsml",
            "witsml.1.4.1.1.soap_http",
            resolver=_controlled_resolver,
            dns_timeout_seconds=0.06,
        )
    assert exc_to_async.value.category == "timeout"
    assert exc_to_async.value.retryable is True
    assert get_dns_executor_stats()["available_permits"] == max_permits

    # 9. Caller cancellation while running (`asyncio.CancelledError`)
    cancel_task = asyncio.create_task(
        resolve_and_pin_endpoint_async(
            "https://rig-slow.example.com:8443/witsml",
            "witsml.1.4.1.1.soap_http",
            resolver=_controlled_resolver,
            dns_timeout_seconds=5.0,
        )
    )
    await asyncio.sleep(0.03)
    cancel_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancel_task
    assert get_dns_executor_stats()["available_permits"] == max_permits

    # 6. Unblock background threads and verify they do NOT double-releaseBoundedSemaphore (which would raise ValueError)
    release_gate.set()
    await asyncio.sleep(0.08)
    stats_after_unblock = get_dns_executor_stats()
    assert stats_after_unblock["available_permits"] == max_permits
    assert stats_after_unblock["quarantined_threads"] == 0


@pytest.mark.asyncio
async def test_dns_saturation_queued_cancellation_and_subsequent_recovery() -> None:
    """Scenarios 4, 5, 7, 8, 16:
    - 4 concurrent slow lookups occupying all worker threads + 4 queued lookups (total 8 permits).
    - Queued lookups cancelled/timed out before execution do NOT leak permits (the exact bug in F-CP15-04).
    - Repeated bursts of 30 slow/cancelled lookups across waves do NOT reduce available permits to zero.
    - While prior slow threads are STILL blocked on `hang_gate`, subsequent healthy DNS lookups succeed within < 200ms
      via generation-scoped executor replacement.
    - Concurrent event-loop heartbeat tasks remain responsive throughout the DNS timeout waves.
    """
    set_egress_policy(
        allowed_hosts=["rig-hang.example.com", "rig-healthy.example.com"],
        allowed_private_cidrs=["10.30.0.0/16"],
    )

    hang_gate = threading.Event()
    started_threads = 0
    started_lock = threading.Lock()

    def _saturation_resolver(host: str, port: int) -> Sequence[str]:
        nonlocal started_threads
        if host == "rig-hang.example.com":
            with started_lock:
                started_threads += 1
            hang_gate.wait(timeout=3.0)
            return ["10.30.1.99"]
        return ["10.30.1.1"]

    max_permits = get_dns_executor_stats()["max_concurrency"]
    assert max_permits == 8

    # Event-loop responsiveness ticker (Scenario 16)
    heartbeat_ticks = 0
    stop_heartbeat = False

    async def _heartbeat() -> None:
        nonlocal heartbeat_ticks
        while not stop_heartbeat:
            await asyncio.sleep(0.01)
            heartbeat_ticks += 1

    hb_task = asyncio.create_task(_heartbeat())
    try:
        # Scenarios 4 & 5: Launch 8 concurrent lookups against rig-hang.example.com (4 run on worker threads, 4 queue)
        wave1_tasks = [
            asyncio.create_task(
                resolve_and_pin_endpoint_async(
                    "https://rig-hang.example.com:8443/witsml",
                    "witsml.1.4.1.1.soap_http",
                    resolver=_saturation_resolver,
                    dns_timeout_seconds=0.08,
                )
            )
            for _ in range(8)
        ]
        wave1_results = await asyncio.gather(*wave1_tasks, return_exceptions=True)
        for res in wave1_results:
            assert isinstance(res, ValidationFailed)
            assert res.details.get("reason") == "timeout"

        # Immediately after wave 1 timeout (while the 4 worker threads are STILL blocked on hang_gate!):
        # All 8 permits MUST be returned (0 leaked from the 4 queued futures that were cancelled before starting!)
        stats_after_wave1 = get_dns_executor_stats()
        assert stats_after_wave1["available_permits"] == max_permits
        assert stats_after_wave1["in_flight_permits"] == 0
        assert stats_after_wave1["executor_generation"] >= 1

        # Scenario 8: While the 4 hung threads from wave 1 are STILL blocked on hang_gate,
        # a healthy lookup to rig-healthy.example.com MUST succeed within bounded latency (< 200ms)!
        t0 = time.perf_counter()
        healthy_pinned = await resolve_and_pin_endpoint_async(
            "https://rig-healthy.example.com:8443/witsml",
            "witsml.1.4.1.1.soap_http",
            resolver=_saturation_resolver,
            dns_timeout_seconds=1.0,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert healthy_pinned.pinned_ip == "10.30.1.1"
        assert elapsed_ms < 200.0
        assert get_dns_executor_stats()["available_permits"] == max_permits

        # Scenario 7: Repeated bursts (30 requests across 3 waves of 10) with mixed timeout and explicit task cancellation
        for wave_idx in range(3):
            # Unblock previous wave's hung threads so quarantine cap recovers between waves
            hang_gate.set()
            await asyncio.sleep(0.03)
            hang_gate.clear()

            burst_tasks = [
                asyncio.create_task(
                    resolve_and_pin_endpoint_async(
                        "https://rig-hang.example.com:8443/witsml",
                        "witsml.1.4.1.1.soap_http",
                        resolver=_saturation_resolver,
                        dns_timeout_seconds=0.07,
                    )
                )
                for _ in range(10)
            ]
            # Cancel half of the burst early (some running, some queued) and let the rest hit timeout
            await asyncio.sleep(0.02)
            for t in burst_tasks[:5]:
                t.cancel()
            burst_outcomes = await asyncio.gather(*burst_tasks, return_exceptions=True)
            for out in burst_outcomes:
                assert isinstance(out, (ValidationFailed, asyncio.CancelledError))

            stats_wave = get_dns_executor_stats()
            assert stats_wave["available_permits"] == max_permits, (
                f"Permit leak detected on wave {wave_idx}: {stats_wave}"
            )

        # Verify healthy lookup still succeeds immediately after all 30 burst requests
        healthy_after_bursts = await resolve_and_pin_endpoint_async(
            "https://rig-healthy.example.com:8443/witsml",
            "witsml.1.4.1.1.soap_http",
            resolver=_saturation_resolver,
            dns_timeout_seconds=1.0,
        )
        assert healthy_after_bursts.pinned_ip == "10.30.1.1"
        assert heartbeat_ticks >= 5  # Event loop remained responsive throughout!
    finally:
        stop_heartbeat = True
        hang_gate.set()
        await hb_task
        await asyncio.sleep(0.05)

    final_stats = get_dns_executor_stats()
    assert final_stats["available_permits"] == max_permits
    assert final_stats["quarantined_threads"] == 0


@pytest.mark.asyncio
async def test_multi_record_embedded_ip_unpacking_and_transport_recovery(tmp_path: Path) -> None:
    """Scenarios 13, 14, 15:
    - All 6 IPv4/IPv6/NAT64/6to4/Teredo/IPv4-mapped unpacking rules still block private/loopback/link-local/metadata
      targets across multi-record responses.
    - WITSML SOAP client, ETP JSON WS client, and ETP Binary WS client all use the resilient async DNS path and
      recover cleanly after DNS timeout bursts.
    - `test_connection()` attributes DNS timeout/failure to `endpoint_policy_validated` with `socket_connected="not_reached"`.
    """
    # Scenario 13: Multi-record responses where first IP is public (93.184.216.34) and second IP embeds a forbidden target
    embedded_attack_vectors = [
        ["93.184.216.34", "::ffff:169.254.169.254"],  # IPv4-mapped IPv6 cloud metadata
        ["93.184.216.34", "64:ff9b::a9fe:a9fe"],  # NAT64 cloud metadata (169.254.169.254)
        ["93.184.216.34", "2002:7f00:0001::1"],  # 6to4 loopback (127.0.0.1)
        ["93.184.216.34", "2001:0000:4136:e378:8000:63bf:3fff:fdd2"],  # Teredo embedded IP
        ["93.184.216.34", "fe80::1"],  # IPv6 link-local
        ["93.184.216.34", "10.99.1.5"],  # Unapproved RFC1918 private IP
    ]
    for vec in embedded_attack_vectors:
        with pytest.raises(ValidationFailed) as exc_ssrf:
            await resolve_and_pin_endpoint_async(
                "https://rebinding.example.com:8443/witsml",
                "witsml.1.4.1.1.soap_http",
                is_production=True,
                allow_loopback=False,
                resolver=lambda _h, _p, _v=vec: _v,
            )
        assert exc_ssrf.value.details.get("reason") == "ssrf_blocked"
    assert get_dns_executor_stats()["available_permits"] == 8

    # Scenarios 14 & 15: Start real local WITSML SOAP, ETP JSON WS, and ETP Binary WS servers
    witsml_srv = LocalWitsmlSoapServer(expected_bearer_token="tok-cp15-dns")
    await witsml_srv.start()
    register_harness_endpoint("127.0.0.1", witsml_srv.port)

    etp_json_srv = LocalEtpWebSocketServer(expected_bearer_token="tok-cp15-dns")
    await etp_json_srv.start()
    register_harness_endpoint("127.0.0.1", etp_json_srv.port)

    etp_bin_srv = LocalEtpBinaryWebSocketServer(expected_bearer_token="tok-cp15-dns")
    await etp_bin_srv.start()
    register_harness_endpoint("127.0.0.1", etp_bin_srv.port)

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cp15_dns_transports.db'}"
    db = Database(db_url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    well_id = await _seed_well(db)
    principal = Principal(id="eng-cp15-dns", org_id=DEFAULT_ORG_ID, role_keys=("admin", "engineer"))

    dns_fail_mode = "healthy"
    hang_evt = threading.Event()

    def _dynamic_resolver(host: str, port: int) -> Sequence[str]:
        if dns_fail_mode == "timeout":
            hang_evt.wait(timeout=0.15)
            raise TimeoutError("simulated slow DNS server")
        if dns_fail_mode == "gaierror":
            raise socket.gaierror(socket.EAI_NONAME, "Simulated NXDOMAIN")
        return ["127.0.0.1"]

    set_custom_dns_resolver(_dynamic_resolver)
    set_egress_policy(
        allowed_hosts=["witsml-rig.local", "etp-json-rig.local", "etp-bin-rig.local"],
        allowed_private_cidrs=[],
    )

    try:
        witsml_url = f"http://witsml-rig.local:{witsml_srv.port}/witsml/servlet/rpc"
        etp_json_url = f"ws://etp-json-rig.local:{etp_json_srv.port}/etp"
        etp_bin_url = f"ws://etp-bin-rig.local:{etp_bin_srv.port}/etp"

        # Create connector for test_connection milestone verification (Scenario 15)
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            created = await svc.create(
                key="etp-bin-dns-test",
                name="ETP Binary DNS Test",
                protocol_profile="etp.1.2.binary_ws",
                well_id=well_id,
                endpoint_url=etp_bin_url,
                config={
                    "auth_mode": "bearer",
                    "tls_verify": True,
                    "poll_interval_seconds": 1.0,
                    "channel_mappings": [
                        {
                            "source_mnemonic": "SPP",
                            "channel_key": "spp",
                            "name": "Standpipe Pressure",
                            "dimension": "pressure",
                            "unit": "psi",
                        }
                    ],
                },
                secret_refs={"bearer_token": "env:DRILLAI_SECRET_CP15_DNS_TOK"},
            )
            cid = created["id"]
            await session.commit()

            # DNS timeout during test_connection -> fails at endpoint_policy_validated, socket_connected == not_reached
            dns_fail_mode = "timeout"
            tc_timeout = await svc.test_connection(cid)
            await session.commit()
            assert tc_timeout["ok"] is False
            assert tc_timeout["error_category"] == "timeout"
            stages_to = {m["stage"]: m["status"] for m in tc_timeout["milestones"]}
            assert stages_to["endpoint_policy_validated"] == "failed"
            assert stages_to["socket_connected"] == "not_reached"

            # DNS gaierror during test_connection -> fails at endpoint_policy_validated, error_category == dns_failure
            dns_fail_mode = "gaierror"
            tc_gai = await svc.test_connection(cid)
            await session.commit()
            assert tc_gai["ok"] is False
            assert tc_gai["error_category"] == "dns_failure"
            stages_gai = {m["stage"]: m["status"] for m in tc_gai["milestones"]}
            assert stages_gai["endpoint_policy_validated"] == "failed"
            assert stages_gai["socket_connected"] == "not_reached"

        # Scenario 14: Restore healthy DNS and verify all 3 clients connect and exchange data cleanly!
        dns_fail_mode = "healthy"
        hang_evt.set()

        w_client = WitsmlSoapClient(
            endpoint_url=witsml_url,
            secrets={"bearer_token": "tok-cp15-dns"},
        )
        ver = await w_client.get_version()
        assert ver == "1.4.1.1"

        ej_client = EtpWebSocketClient(
            endpoint_url=etp_json_url,
            secrets={"bearer_token": "tok-cp15-dns"},
        )
        _sid_j, chs = await ej_client.probe_session()
        assert len(chs) > 0

        eb_client = EtpBinaryWebSocketClient(
            endpoint_url=etp_bin_url,
            secrets={"bearer_token": "tok-cp15-dns"},
        )
        _sid_b, b_chs = await eb_client.probe_session()
        assert len(b_chs) > 0

        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            tc_ok = await svc.test_connection(cid)
            await session.commit()
            assert tc_ok["ok"] is True

        assert get_dns_executor_stats()["available_permits"] == 8
    finally:
        hang_evt.set()
        await witsml_srv.stop()
        await etp_json_srv.stop()
        await etp_bin_srv.stop()
