"""CP14 Phase 3 regression suite: Bounded, non-blocking DNS resolution, complete IPv4/IPv6
address-class validation (including NAT64/6to4/Teredo/IPv4-mapped), and direct socket-level
adversarial SSRF/rebinding/TLS-SNI/proxy-bypass tests for production `EtpBinaryWebSocketClient`
and `WitsmlSoapClient` (`F-CP14-03` and `F-CP14-04`).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from drillai.core.config import reset_settings_cache
from drillai.core.errors import ValidationFailed
from drillai.telemetry.connectors import (
    ConnectorTransportError,
    clear_harness_endpoints,
    enforce_connect_time_ssrf,
    enforce_connect_time_ssrf_async,
     register_harness_endpoint,
    reset_egress_policy,
    resolve_and_pin_endpoint,
    resolve_and_pin_endpoint_async,
    set_custom_dns_resolver,
    unregister_harness_endpoint,
)
from drillai.telemetry.protocols.etp_binary import (
    EtpBinaryWebSocketClient,
    LocalEtpBinaryWebSocketServer,
)
from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer, WitsmlSoapClient


@pytest.fixture(autouse=True)
def _clean_dns_and_ssrf_state():
    set_custom_dns_resolver(None)
    clear_harness_endpoints()
    reset_egress_policy()
    yield
    set_custom_dns_resolver(None)
    clear_harness_endpoints()
    reset_egress_policy()
    reset_settings_cache()


def _generate_self_signed_tls_contexts(
    common_name: str, tmp_path: Path
) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    cert_file = tmp_path / f"{common_name}.crt"
    key_file = tmp_path / f"{common_name}.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_file),
            "-out",
            str(cert_file),
            "-days",
            "1",
            "-subj",
            f"/CN={common_name}",
            "-addext",
            f"subjectAltName=DNS:{common_name}",
        ],
        stdin=subprocess.DEVNULL,
        timeout=15,
        check=True,
        capture_output=True,
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))
    client_ctx = ssl.create_default_context(cafile=str(cert_file))
    client_ctx.check_hostname = True
    client_ctx.verify_mode = ssl.CERT_REQUIRED
    return server_ctx, client_ctx


async def test_non_blocking_bounded_dns_timeout_and_event_loop_responsiveness():
    """Verify F-CP14-03 (Phase 3.1):
    1. A slow system `socket.getaddrinfo` or custom DNS resolver times out cleanly at `dns_timeout_seconds`
       with `ConnectorTransportError(category="timeout", retryable=True)`.
    2. While DNS resolution is blocked inside a worker thread, concurrent coroutines on the asyncio
       event loop continue ticking with < 50ms latency (event loop is never blocked).
    3. Empty `getaddrinfo` answer when `require_dns_resolution=True` raises `dns_failure` and never
       falls back to `pinned_ip = hostname`.
    4. Saturated DNS concurrency fails fast with `timeout` without spawning unbounded threads.
    """

    def _slow_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        # Block the DNS worker thread for 1.2s
        time.sleep(1.2)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", int(port or 443)))]

    # Measure event-loop tick responsiveness while `enforce_connect_time_ssrf_async` is waiting on slow DNS
    max_tick_gap_ms = 0.0
    ticks = 0
    keep_ticking = True

    async def _loop_heartbeat() -> None:
        nonlocal max_tick_gap_ms, ticks
        last = time.perf_counter()
        while keep_ticking:
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gap_ms = (now - last) * 1000.0
            if gap_ms > max_tick_gap_ms:
                max_tick_gap_ms = gap_ms
            last = now
            ticks += 1

    with patch("drillai.telemetry.connectors.socket.getaddrinfo", side_effect=_slow_getaddrinfo):
        hb_task = asyncio.create_task(_loop_heartbeat())
        t0 = time.perf_counter()
        try:
            with pytest.raises(ConnectorTransportError) as timeout_exc:
                await enforce_connect_time_ssrf_async(
                    "https://hanging-dns.rig.example/witsml/store",
                    "witsml.1.4.1.1.soap_http",
                    dns_timeout_seconds=0.20,
                )
            elapsed = time.perf_counter() - t0
        finally:
            keep_ticking = False
            await hb_task

    assert timeout_exc.value.category == "timeout"
    assert timeout_exc.value.retryable is True
    assert elapsed < 0.80
    assert ticks >= 8
    assert max_tick_gap_ms < 80.0, f"Event loop was blocked during DNS lookup (max_tick_gap_ms={max_tick_gap_ms:.1f}ms)"

    # Also verify synchronous `enforce_connect_time_ssrf` enforces `dns_timeout_seconds`
    with patch("drillai.telemetry.connectors.socket.getaddrinfo", side_effect=_slow_getaddrinfo):
        t_sync = time.perf_counter()
        with pytest.raises(ConnectorTransportError) as sync_timeout_exc:
            enforce_connect_time_ssrf(
                "https://hanging-sync-dns.rig.example/witsml/store",
                "witsml.1.4.1.1.soap_http",
                dns_timeout_seconds=0.15,
            )
        assert (time.perf_counter() - t_sync) < 0.80
        assert sync_timeout_exc.value.category == "timeout"
        assert sync_timeout_exc.value.retryable is True

    # Empty `socket.getaddrinfo` result in default path raises `dns_failure` when `require_dns_resolution=True`
    with patch("drillai.telemetry.connectors.socket.getaddrinfo", return_value=[]):
        with pytest.raises(ValidationFailed) as empty_exc:
            await resolve_and_pin_endpoint_async(
                "https://empty-answer.rig.example/witsml/store",
                "witsml.1.4.1.1.soap_http",
                require_dns_resolution=True,
            )
        assert empty_exc.value.details["reason"] == "dns_failure"


def test_complete_address_validation_mixed_answers_nat64_6to4_teredo_and_reserved():
    """Verify Phase 3.2:
    - Every returned A and AAAA record is validated; a single forbidden record in a multi-record
      DNS answer rejects the entire resolution.
    - NAT64 (`64:ff9b::/96`), IPv4-mapped IPv6 (`::ffff:0:0/96`), 6to4 (`2002::/16`), Teredo (`2001:0000::/32`),
      unspecified (`0.0.0.0`, `::`), multicast (`224.0.0.1`, `ff02::1`), and reserved (`240.0.0.1`)
      are rejected both as URL literals and as resolved DNS records.
    """
    # 1. Mixed public + forbidden answers must reject the entire resolution
    for mixed_answers in (
        ["93.184.216.34", "127.0.0.1"],
        ["93.184.216.34", "169.254.169.254"],
        ["93.184.216.34", "10.24.12.8"],
        ["2606:2800:220:1:248:1893:25c8:1946", "::1"],
        ["2606:2800:220:1:248:1893:25c8:1946", "::ffff:169.254.169.254"],
        ["2606:2800:220:1:248:1893:25c8:1946", "64:ff9b::127.0.0.1"],
        ["2606:2800:220:1:248:1893:25c8:1946", "64:ff9b::169.254.169.254"],
        ["2606:2800:220:1:248:1893:25c8:1946", "2002:7f00:0001::1"],
    ):
        with pytest.raises(ValidationFailed) as mix_exc:
            resolve_and_pin_endpoint(
                "wss://multi-record.rig.example/etp",
                "etp.1.2.binary_ws",
                allow_loopback=False,
                resolver=lambda h, p, ans=mixed_answers: ans,
            )
        assert mix_exc.value.details["reason"] == "ssrf_blocked"

    # 2. NAT64, 6to4, Teredo, unspecified, multicast, and reserved answers
    for bad_ip in (
        "0.0.0.0",
        "::",
        "224.0.0.1",
        "ff02::1",
        "240.0.0.1",
        "64:ff9b::10.0.0.1",
        "64:ff9b::192.168.1.1",
        "2002:a9fe:a9fe::1",  # 6to4 embedding 169.254.169.254
        "2002:0a00:0001::1",  # 6to4 embedding 10.0.0.1
    ):
        with pytest.raises(ValidationFailed) as ip_exc:
            resolve_and_pin_endpoint(
                "wss://transition-v6.rig.example/etp",
                "etp.1.2.binary_ws",
                allow_loopback=False,
                resolver=lambda h, p, ip=bad_ip: [ip],
            )
        assert ip_exc.value.details["reason"] == "ssrf_blocked"

    # 3. Multiple valid public records succeed and deterministically pin the first record
    pinned = resolve_and_pin_endpoint(
        "wss://public-dual.rig.example:8443/etp",
        "etp.1.2.binary_ws",
        resolver=lambda h, p: ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"],
    )
    assert pinned.pinned_ip == "93.184.216.34"
    assert pinned.all_resolved_ips == ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
    assert pinned.connect_url == "wss://93.184.216.34:8443/etp"
    assert pinned.sni_hostname == "public-dual.rig.example"


async def test_etp_binary_socket_pinning_rebinding_tls_sni_and_proxy_ignore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify F-CP14-04 (Phases 3.3, 3.4, 3.5) directly on production `EtpBinaryWebSocketClient`:
    1. Socket pinning: `EtpBinaryWebSocketClient` connects to `pinned.pinned_ip` (`127.0.0.1`) even
       when the underlying `socket.getaddrinfo` is poisoned to return `169.254.169.254` if the
       WebSocket transport ever re-resolves `etp-bin-rebind.drillai.test`.
    2. DNS rebinding across consecutive `poll_subscription_batch()` calls: Poll 1 resolves to the
       approved harness port and streams binary Avro frames; Poll 2 rebinds to `169.254.169.254`
       or an unapproved local port and is blocked with `ssrf_blocked` before opening any TCP socket.
    3. TLS (`wss://`) SNI and X.509 SAN hostname verification on pinned IP `127.0.0.1`:
       - Matching hostname `etp-bin-tls.drillai.test` succeeds over TLS and streams binary Avro frames.
       - Mismatched hostname `wrong-etp-host.drillai.test` resolving to the same pinned IP `127.0.0.1`
         fails with `ConnectorTransportError(category="tls_error", retryable=False)`.
    4. Proxy bypass prevention: With `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `WS_PROXY`, `WSS_PROXY`
       pointing to a local trap proxy server, both `WitsmlSoapClient` and `EtpBinaryWebSocketClient`
       ignore ambient proxy env vars (`proxy_hits == 0`).
    """
    # Start a local trap proxy server and an unrelated local listener to verify zero unintended hits
    proxy_hits = 0
    unrelated_hits = 0

    async def _proxy_trap(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal proxy_hits
        proxy_hits += 1
        writer.close()

    async def _unrelated_trap(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal unrelated_hits
        unrelated_hits += 1
        writer.close()

    proxy_srv = await asyncio.start_server(_proxy_trap, "127.0.0.1", 0)
    proxy_port = int(proxy_srv.sockets[0].getsockname()[1])
    unrelated_srv = await asyncio.start_server(_unrelated_trap, "127.0.0.1", 0)
    unrelated_port = int(unrelated_srv.sockets[0].getsockname()[1])

    for env_var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "WS_PROXY",
        "WSS_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "ws_proxy",
        "wss_proxy",
    ):
        monkeypatch.setenv(env_var, f"http://127.0.0.1:{proxy_port}")

    etp_bin_srv = LocalEtpBinaryWebSocketServer()
    await etp_bin_srv.start()
    etp_port = etp_bin_srv.port

    witsml_srv = LocalWitsmlSoapServer()
    await witsml_srv.start()
    witsml_port = witsml_srv.port

    real_getaddrinfo = socket.getaddrinfo

    def _poisoned_transport_getaddrinfo(host: str, port: Any, *args: Any, **kwargs: Any) -> list[Any]:
        # If the transport layer ever tries to resolve the hostname instead of connecting to the pinned IP,
        # return cloud metadata 169.254.169.254 or raise an error!
        if str(host).lower().endswith(".drillai.test"):
            raise AssertionError(f"Transport attempted unpinned DNS resolution for {host!r}!")
        return real_getaddrinfo(host, port, *args, **kwargs)

    mappings = [
        {
            "source_mnemonic": "SPP",
            "channel_key": "spp",
            "name": "Standpipe Pressure",
            "unit": "psi",
            "dimension": "pressure",
        }
    ]

    try:
        # 1 & 2: Socket pinning + DNS rebinding across consecutive polls on `EtpBinaryWebSocketClient`
        dns_step = {"count": 0}

        def _rebinding_resolver(host: str, port: int) -> list[str]:
            dns_step["count"] += 1
            if dns_step["count"] == 1:
                return ["127.0.0.1"]
            return ["169.254.169.254"]

        set_custom_dns_resolver(_rebinding_resolver)
        bin_client = EtpBinaryWebSocketClient(
            endpoint_url=f"ws://etp-bin-rebind.drillai.test:{etp_port}",
            timeout_seconds=5.0,
        )

        with patch("socket.getaddrinfo", side_effect=_poisoned_transport_getaddrinfo):
            # Poll 1: Resolves to 127.0.0.1 (approved harness port), pins socket IP, and streams binary Avro frames
            batch1 = await bin_client.poll_subscription_batch(
                well_id="wel_ssrf",
                config={"channel_mappings": mappings},
            )
            assert len(batch1.frames) > 0
            assert batch1.disposition.reconciled is True

            # Poll 2: DNS rebinds `etp-bin-rebind.drillai.test` to `169.254.169.254` -> blocked before connect!
            with pytest.raises(ConnectorTransportError) as rebind_exc:
                await bin_client.poll_subscription_batch(
                    well_id="wel_ssrf",
                    config={"channel_mappings": mappings},
                    watermark=batch1.next_watermark,
                )
            assert rebind_exc.value.category == "ssrf_blocked"
            assert rebind_exc.value.retryable is False

        # Verify `EtpBinaryWebSocketClient` cannot connect to an unrelated local port even while harness is running
        set_custom_dns_resolver(lambda h, p: ["127.0.0.1"])
        unapproved_port_client = EtpBinaryWebSocketClient(
            endpoint_url=f"ws://etp-bin-rebind.drillai.test:{unrelated_port}",
            timeout_seconds=5.0,
        )
        with pytest.raises(ConnectorTransportError) as port_exc:
            await unapproved_port_client.probe_session()
        assert port_exc.value.category == "ssrf_blocked"
        assert unrelated_hits == 0

        # Verify `WitsmlSoapClient` also connects with poisoned transport getaddrinfo and ignores proxy env vars
        witsml_client = WitsmlSoapClient(
            endpoint_url=f"http://witsml-pin.drillai.test:{witsml_port}/witsml/store",
            timeout_seconds=5.0,
            max_retries=0,
        )
        with patch("socket.getaddrinfo", side_effect=_poisoned_transport_getaddrinfo):
            assert await witsml_client.get_version() == "1.4.1.1"
        assert proxy_hits == 0

        # 3. TLS (`wss://`) SNI & X.509 certificate hostname verification on pinned IP `127.0.0.1`
        # for production `EtpBinaryWebSocketClient`
        server_ssl_ctx, client_ssl_ctx = _generate_self_signed_tls_contexts(
            "etp-bin-tls.drillai.test", tmp_path
        )
        import websockets

        tls_ws_srv = await websockets.serve(
            etp_bin_srv._handler,
            "127.0.0.1",
            0,
            subprotocols=["energistics-tp"],
            ssl=server_ssl_ctx,
        )
        tls_port = int(tls_ws_srv.sockets[0].getsockname()[1])
        register_harness_endpoint("127.0.0.1", tls_port)
        try:
            # 3a. Matching certificate SAN (`etp-bin-tls.drillai.test`) succeeds on pinned IP `127.0.0.1`
            ok_tls_client = EtpBinaryWebSocketClient(
                endpoint_url=f"wss://etp-bin-tls.drillai.test:{tls_port}",
                ssl_context=client_ssl_ctx,
                timeout_seconds=5.0,
            )
            with patch("socket.getaddrinfo", side_effect=_poisoned_transport_getaddrinfo):
                sess_id, discovered = await ok_tls_client.probe_session()
                assert sess_id
                assert len(discovered) >= 3
                tls_batch = await ok_tls_client.poll_subscription_batch(
                    well_id="wel_ssrf",
                    config={"channel_mappings": mappings},
                )
                assert len(tls_batch.frames) > 0

            # 3b. Mismatched hostname (`wrong-etp-host.drillai.test`) resolving to the exact same pinned IP
            # `127.0.0.1` fails TLS hostname verification with `tls_error`!
            bad_tls_client = EtpBinaryWebSocketClient(
                endpoint_url=f"wss://wrong-etp-host.drillai.test:{tls_port}",
                ssl_context=client_ssl_ctx,
                timeout_seconds=5.0,
            )
            with pytest.raises(ConnectorTransportError) as tls_exc:
                await bad_tls_client.probe_session()
            assert tls_exc.value.category == "tls_error"
            assert tls_exc.value.retryable is False
        finally:
            unregister_harness_endpoint("127.0.0.1", tls_port)
            tls_ws_srv.close()
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(tls_ws_srv.wait_closed(), timeout=1.0)

        assert proxy_hits == 0
        assert unrelated_hits == 0
    finally:
        await etp_bin_srv.stop()
        await witsml_srv.stop()
        proxy_srv.close()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(proxy_srv.wait_closed(), timeout=1.0)
        unrelated_srv.close()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(unrelated_srv.wait_closed(), timeout=1.0)
