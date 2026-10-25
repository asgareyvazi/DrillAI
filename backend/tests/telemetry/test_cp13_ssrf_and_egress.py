"""CP13 Phase 2 (Checkpoint A): Connection-bound SSRF protection, socket IP pinning,
explicit private rig-network CIDR policy, and strict non-production harness port isolation.

Covers all 16 required adversarial SSRF and egress governance scenarios at the socket level.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import subprocess
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from drillai.api.app import create_app
from drillai.core.config import get_settings, reset_settings_cache
from drillai.core.errors import ValidationFailed
from drillai.db.base import Base
from drillai.db.models import AuditLog, Organization
from drillai.telemetry.connectors import (
    ConnectorTransportError,
    clear_harness_endpoints,
    enforce_connect_time_ssrf,
    is_harness_endpoint,
    register_harness_endpoint,
    reset_egress_policy,
    resolve_and_pin_endpoint,
    set_custom_dns_resolver,
    set_egress_policy,
    unregister_harness_endpoint,
    validate_connector_endpoint,
)
from drillai.telemetry.protocols.etp import EtpWebSocketClient, LocalEtpWebSocketServer
from drillai.telemetry.protocols.witsml import LocalWitsmlSoapServer, WitsmlSoapClient


@pytest.fixture(autouse=True)
def _clean_resolvers_and_policy():
    reset_settings_cache()
    set_custom_dns_resolver(None)
    reset_egress_policy()
    clear_harness_endpoints()
    yield
    set_custom_dns_resolver(None)
    reset_egress_policy()
    clear_harness_endpoints()
    reset_settings_cache()


def test_the_certificate_fixture_never_treats_a_slow_machine_as_a_tls_failure() -> None:
    """The certificate is real; the patience must be generous.

    `openssl req -newkey rsa:2048` exceeded a fixed fifteen-second bound on the Windows runner — a
    first invocation of a freshly installed binary, no cached entropy, an antivirus scanning it — and
    the backend suite went red for a reason that has nothing to do with the TLS hostname verification
    this fixture supports. Both certificate fixtures in the telemetry tests therefore share one
    generous bound, and this asserts neither has drifted back to a stopwatch.
    """
    from pathlib import Path

    assert _OPENSSL_TIMEOUT_SECONDS >= 60, "key generation needs room on a slow machine"
    here = Path(__file__).resolve().parent
    for module in (Path(__file__).resolve(), here / "test_cp14_dns_and_transport_ssrf.py"):
        text = module.read_text(encoding="utf-8")
        assert "timeout=_OPENSSL_TIMEOUT_SECONDS" in text, (
            f"{module.name} must bound certificate generation with the shared generous timeout"
        )
        # Assembled rather than written out, so this scan does not match its own source.
        stopwatch = "timeout=" + "15,"
        assert stopwatch not in text, (
            f"{module.name} must not time certificate generation with a fixed fifteen seconds"
        )


#: How long the certificate fixture may take to generate a key. Generous on purpose: this bounds a
#: hung tool, it does not assert how fast a machine can create an RSA key.
_OPENSSL_TIMEOUT_SECONDS = 120


def _resolve_openssl_bin() -> str:
    import shutil
    from pathlib import Path

    found = shutil.which("openssl")
    if found:
        return found
    for candidate in (
        r"C:\Program Files\Git\usr\bin\openssl.exe",
        r"C:\Program Files\OpenSSL\bin\openssl.exe",
        r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return "openssl"


def _generate_self_signed_tls_contexts(
    san_dns_name: str,
    tmp_path: Any,
) -> tuple[ssl.SSLContext, ssl.SSLContext]:
    """Create a real X.509 certificate for `san_dns_name` via `openssl` and return (server_ssl_ctx, client_ssl_ctx)."""
    import os

    cert_path = tmp_path / f"{san_dns_name}.crt.pem"
    key_path = tmp_path / f"{san_dns_name}.key.pem"
    subprocess.run(
        [
            _resolve_openssl_bin(),
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(key_path),
            "-out",
            str(cert_path),
            "-days",
            "2",
            "-subj",
            f"/CN={san_dns_name}",
            "-addext",
            f"subjectAltName=DNS:{san_dns_name}",
        ],
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
        stdin=subprocess.DEVNULL,
        # Generating the key is the slowest part of this fixture, and it is the machine that is slow,
        # not the check: on the Windows runner `openssl req -newkey rsa:2048` has been observed to
        # take longer than fifteen seconds (first invocation, antivirus scanning a fresh executable,
        # no cached entropy). A timeout here fails the *test*, which is about TLS hostname
        # verification against an approved pinned IP — so a fixed 15 s turned a slow key generation
        # into a red backend suite. The certificate is real either way; only the patience is larger.
        timeout=_OPENSSL_TIMEOUT_SECONDS,
        check=True,
        capture_output=True,
    )

    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))

    client_ctx = ssl.create_default_context(cafile=str(cert_path))
    client_ctx.check_hostname = True
    client_ctx.verify_mode = ssl.CERT_REQUIRED
    return server_ctx, client_ctx


# ---------------------------------------------------------------------------
# Scenarios 1, 2, 3, 4, 5: TOCTOU DNS Rebinding & Socket Pinning (HTTPX & WS)
# ---------------------------------------------------------------------------


async def test_s1_s2_s3_toctou_dns_rebinding_and_socket_pinning_prevent_unvalidated_connect(
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify:
    - Scenario 1a: Even if OS DNS (`socket.getaddrinfo`) changes between `enforce_connect_time_ssrf`
      and the underlying HTTPX / WebSockets connect call, the client connects to the pinned IP
      (`127.0.0.1`) rather than re-resolving the hostname to an unvalidated IP (`127.0.0.2`),
      while preserving `Host: <original_hostname>:<port>`.
    - Scenario 1b & 2: Hostname resolving to public IP (`93.184.216.34`) at validation time and
      rebinding to `127.0.0.1` or `169.254.169.254` before connect is blocked at connect time.
    - Scenario 3: Mixed A/AAAA records (`["93.184.216.34", "127.0.0.1"]` or
      `["93.184.216.34", "169.254.169.254"]`) are rejected even when the first record is public.
    """
    # Scenario 1b & 2: Public IP at validation time, then rebinds to 127.0.0.1 or 169.254.169.254
    target_ip = "93.184.216.34"
    set_custom_dns_resolver(lambda host, port: [target_ip])
    url = "http://rebind.witsml.example:18080/witsml/store"
    assert validate_connector_endpoint(url, "witsml.1.4.1.1.soap_http") == url

    # Rebind to 127.0.0.1 (non-harness port 18080) -> blocked at connect time
    target_ip = "127.0.0.1"
    with pytest.raises(ConnectorTransportError) as rebind_loopback_exc:
        enforce_connect_time_ssrf(url, "witsml.1.4.1.1.soap_http", allow_loopback=False)
    assert rebind_loopback_exc.value.category == "ssrf_blocked"
    assert rebind_loopback_exc.value.retryable is False

    # Rebind to 169.254.169.254 -> blocked at connect time
    target_ip = "169.254.169.254"
    with pytest.raises(ConnectorTransportError) as rebind_meta_exc:
        enforce_connect_time_ssrf(url, "witsml.1.4.1.1.soap_http", allow_loopback=True)
    assert rebind_meta_exc.value.category == "ssrf_blocked"

    # Scenario 3: Mixed A/AAAA records containing both public and loopback/metadata IP
    for mixed_records in (
        ["93.184.216.34", "127.0.0.1"],
        ["93.184.216.34", "169.254.169.254"],
        ["93.184.216.34", "::ffff:10.0.0.1"],
        ["2606:2800:220:1:248:1893:25c8:1946", "::1"],
    ):
        set_custom_dns_resolver(lambda host, port, recs=mixed_records: recs)
        with pytest.raises(ValidationFailed) as mix_exc:
            validate_connector_endpoint(
                "https://mixed.witsml.example/witsml/store",
                "witsml.1.4.1.1.soap_http",
                allow_loopback=False,
            )
        assert mix_exc.value.details["reason"] == "ssrf_blocked"

    # Scenario 1a: True socket-level pinning proof for both WitsmlSoapClient (HTTPX) and EtpWebSocketClient.
    # Start a legitimate harness server on 127.0.0.1 and a trap listener on 127.0.0.2 (same port if possible,
    # or force OS socket.getaddrinfo for the hostname to point to the trap IP 127.0.0.2).
    observed_host_headers: list[str] = []

    async def _legit_http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.readuntil(b"\r\n\r\n")
        for line in raw.decode("latin-1").split("\r\n")[1:]:
            if line.lower().startswith("host:"):
                observed_host_headers.append(line.split(":", 1)[1].strip())
        content_len = 0
        for line in raw.decode("latin-1").split("\r\n")[1:]:
            if line.lower().startswith("content-length:"):
                content_len = int(line.split(":", 1)[1].strip())
        if content_len:
            await reader.readexactly(content_len)
        soap = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            b'xmlns:wmls="http://www.witsml.org/message/120">'
            b"<soapenv:Body><wmls:WMLS_GetVersionResponse><Result>1.4.1.1</Result>"
            b"</wmls:WMLS_GetVersionResponse></soapenv:Body></soapenv:Envelope>"
        )
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\nContent-Length: {len(soap)}\r\nConnection: close\r\n\r\n".encode(
                "latin-1"
            )
            + soap
        )
        await writer.drain()
        writer.close()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)

    trap_hits = 0

    async def _trap_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal trap_hits
        trap_hits += 1
        writer.close()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)

    legit_srv = await asyncio.start_server(_legit_http_handler, "127.0.0.1", 0)
    port = int(legit_srv.sockets[0].getsockname()[1])
    register_harness_endpoint("127.0.0.1", port)
    trap_srv = await asyncio.start_server(_trap_handler, "127.0.0.2", port)

    real_getaddrinfo = socket.getaddrinfo

    def _poisoned_os_getaddrinfo(host: Any, service: Any, *args: Any, **kwargs: Any) -> Any:
        # If HTTPX or WebSockets tries to resolve "pinned-rig.example" via OS getaddrinfo,
        # redirect it to the trap IP 127.0.0.2!
        if str(host).lower() == "pinned-rig.example":
            return real_getaddrinfo("127.0.0.2", service, *args, **kwargs)
        return real_getaddrinfo(host, service, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", _poisoned_os_getaddrinfo)
    # Our validated connector resolver resolves "pinned-rig.example" to the approved 127.0.0.1
    set_custom_dns_resolver(lambda host, p: ["127.0.0.1"])

    try:
        witsml_client = WitsmlSoapClient(
            endpoint_url=f"http://pinned-rig.example:{port}/witsml/store",
            timeout_seconds=5.0,
        )
        version = await witsml_client.get_version()
        assert version == "1.4.1.1"
        assert trap_hits == 0, "HTTPX must connect to pinned IP 127.0.0.1, never re-resolving via OS DNS"
        assert observed_host_headers == [f"pinned-rig.example:{port}"]
    finally:
        unregister_harness_endpoint("127.0.0.1", port)
        legit_srv.close()
        trap_srv.close()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(legit_srv.wait_closed(), timeout=1.0)
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(trap_srv.wait_closed(), timeout=1.0)


async def test_s4_s5_dns_rebinding_across_consecutive_polls_and_websocket_reconnects():
    """Verify:
    - Scenario 4: Hostname rebinding across two consecutive WITSML polls fails closed on poll 2.
    - Scenario 5: Hostname rebinding across ETP WebSocket reconnects fails closed on reconnect 2,
      and ETP WebSocket connects strictly to the pinned IP.
    """
    async with LocalWitsmlSoapServer() as witsml_srv, LocalEtpWebSocketServer() as etp_srv:
        resolved_ip = "127.0.0.1"
        set_custom_dns_resolver(lambda host, port: [resolved_ip])

        witsml_url = f"http://dynamic-witsml.example:{witsml_srv.port}/witsml/store"
        witsml_client = WitsmlSoapClient(endpoint_url=witsml_url, timeout_seconds=5.0)
        assert await witsml_client.get_version() == "1.4.1.1"

        # Poll 2: DNS rebinds to metadata IP 169.254.169.254 -> fails closed
        resolved_ip = "169.254.169.254"
        with pytest.raises(ConnectorTransportError) as poll2_exc:
            await witsml_client.get_version()
        assert poll2_exc.value.category == "ssrf_blocked"

        # Scenario 5: ETP WebSocket reconnects
        resolved_ip = "127.0.0.1"
        etp_url = f"ws://dynamic-etp.example:{etp_srv.port}/etp"
        etp_client = EtpWebSocketClient(endpoint_url=etp_url, timeout_seconds=5.0)
        session_id, channels = await etp_client.probe_session()
        assert session_id
        assert len(channels) >= 1

        # Reconnect 2: DNS rebinds to unapproved private IP 10.99.88.77 -> fails closed
        resolved_ip = "10.99.88.77"
        with pytest.raises(ConnectorTransportError) as ws_rebind_exc:
            await etp_client.probe_session()
        assert ws_rebind_exc.value.category == "ssrf_blocked"


# ---------------------------------------------------------------------------
# Scenario 6: HTTP 301/302/307/308 Redirect Blocking
# ---------------------------------------------------------------------------


async def test_s6_http_301_302_307_308_redirects_to_loopback_or_metadata_are_blocked():
    """Verify HTTP 301, 302, 307, and 308 redirects are never followed and fail with `redirect_blocked`."""
    redirect_status = 302
    redirect_target = "http://169.254.169.254/latest/meta-data/"
    trap_hits = 0

    async def _redirect_srv_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        head = (
            f"HTTP/1.1 {redirect_status} Redirect\r\n"
            f"Location: {redirect_target}\r\n"
            "Content-Length: 0\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head)
        await writer.drain()
        writer.close()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)

    srv = await asyncio.start_server(_redirect_srv_handler, "127.0.0.1", 0)
    port = int(srv.sockets[0].getsockname()[1])
    register_harness_endpoint("127.0.0.1", port)

    try:
        client = WitsmlSoapClient(
            endpoint_url=f"http://127.0.0.1:{port}/witsml/store",
            timeout_seconds=5.0,
            max_retries=0,
        )
        for code, loc in (
            (301, "http://169.254.169.254/latest/meta-data/"),
            (302, "http://127.0.0.1:59999/internal-admin"),
            (307, "http://[::1]:59999/internal-admin"),
            (308, "http://metadata.google.internal/computeMetadata/v1/"),
        ):
            redirect_status = code
            redirect_target = loc
            with pytest.raises(ConnectorTransportError) as redir_exc:
                await client.get_version()
            assert redir_exc.value.category == "redirect_blocked"
            assert redir_exc.value.retryable is False
        assert trap_hits == 0
    finally:
        unregister_harness_endpoint("127.0.0.1", port)
        srv.close()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(srv.wait_closed(), timeout=1.0)


# ---------------------------------------------------------------------------
# Scenarios 7, 8, 9, 10, 13, 15, 16: Loopback Isolation, IPv6 Mapped, Metadata,
# Non-Canonical IPs, Embedded Credentials, Resolver Timeouts, Production Rules
# ---------------------------------------------------------------------------


async def test_s7_ordinary_connector_cannot_use_loopback_when_harness_runs_on_another_port():
    """Scenario 7: While the local harness is active on its own registered ports, an ordinary
    connector targeting any other local port (`127.0.0.1`, `::1`, `localhost`) is blocked."""
    unrelated_hits = 0

    async def _unrelated_local_service(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal unrelated_hits
        unrelated_hits += 1
        writer.close()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)

    unrelated_srv = await asyncio.start_server(_unrelated_local_service, "127.0.0.1", 0)
    unrelated_port = int(unrelated_srv.sockets[0].getsockname()[1])

    try:
        async with LocalWitsmlSoapServer() as witsml_harness:
            # Harness port is allowed in non-production
            assert is_harness_endpoint("127.0.0.1", witsml_harness.port, is_production=False) is True
            assert is_harness_endpoint("127.0.0.1", unrelated_port, is_production=False) is False

            harness_client = WitsmlSoapClient(endpoint_url=witsml_harness.endpoint_url)
            assert await harness_client.get_version() == "1.4.1.1"

            # Ordinary connector targeting unrelated local service on 127.0.0.1, ::1, or localhost is blocked
            for forbidden_local_url in (
                f"http://127.0.0.1:{unrelated_port}/witsml/store",
                f"http://[::1]:{unrelated_port}/witsml/store",
                f"http://localhost:{unrelated_port}/witsml/store",
            ):
                with pytest.raises(ValidationFailed) as val_exc:
                    validate_connector_endpoint(
                        forbidden_local_url,
                        "witsml.1.4.1.1.soap_http",
                        allow_loopback=False,
                        is_production=False,
                    )
                assert val_exc.value.details["reason"] == "ssrf_blocked"

                bad_client = WitsmlSoapClient(endpoint_url=forbidden_local_url)
                with pytest.raises(ConnectorTransportError) as conn_exc:
                    await bad_client.get_version()
                assert conn_exc.value.category == "ssrf_blocked"

            assert unrelated_hits == 0
    finally:
        unrelated_srv.close()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(unrelated_srv.wait_closed(), timeout=1.0)


def test_s8_s9_s10_s13_s15_s16_ipv6_mapped_noncanonical_metadata_credentials_timeouts_and_prod():
    """Verify Scenarios 8, 9, 10, 13, 15, and 16."""
    # Scenario 8: IPv4-mapped IPv6 literals and resolved records
    for v6_url in (
        "https://[::ffff:127.0.0.1]:8443/witsml/store",
        "https://[::ffff:169.254.169.254]/latest/meta-data/",
        "https://[0:0:0:0:0:ffff:7f00:1]/witsml/store",
        "https://[64:ff9b::127.0.0.1]/witsml/store",
    ):
        with pytest.raises(ValidationFailed) as exc8:
            validate_connector_endpoint(v6_url, "witsml.1.4.1.1.soap_http", allow_loopback=False)
        assert exc8.value.details["reason"] == "ssrf_blocked"

    # Scenario 9: Non-canonical numeric/octal/hex/shorthand IPv4 representations
    for non_canonical in (
        "https://2130706433/witsml/store",
        "https://0177.0.0.1/witsml/store",
        "https://0x7f.0.0.1/witsml/store",
        "https://127.1/witsml/store",
        "https://0x7f000001/witsml/store",
    ):
        with pytest.raises(ValidationFailed) as exc9:
            validate_connector_endpoint(non_canonical, "witsml.1.4.1.1.soap_http", allow_loopback=True)
        assert exc9.value.details["reason"] == "ssrf_blocked"

    # Scenario 10: Link-local and cloud metadata addresses
    for meta_url in (
        "https://169.254.169.254/latest/meta-data/",
        "https://169.254.169.250/metadata",
        "https://[fe80::1]/witsml",
        "https://100.100.100.200/latest/meta-data/",
        "https://[fd00:ec2::254]/latest/meta-data/",
        "https://metadata.google.internal/computeMetadata/v1/",
    ):
        with pytest.raises(ValidationFailed) as exc10:
            validate_connector_endpoint(
                meta_url,
                "witsml.1.4.1.1.soap_http",
                allow_loopback=True,
                allowed_hosts=["169.254.169.254", "metadata.google.internal"],
            )
        assert exc10.value.details["reason"] == "ssrf_blocked"

    # Scenario 13: Embedded credentials in URL
    for cred_url in (
        "https://rig_user:secret_pass@witsml.rig.example/store",
        "wss://token_user@etp.rig.example/etp",
    ):
        profile = "etp.1.2.json_ws" if cred_url.startswith("wss://") else "witsml.1.4.1.1.soap_http"
        with pytest.raises(ValidationFailed) as exc13:
            validate_connector_endpoint(cred_url, profile)
        assert exc13.value.details["reason"] == "plaintext_secret_forbidden"

    # Scenario 15: Resolver timeout and NXDOMAIN classification
    def _timeout_resolver(host: str, port: int) -> list[str]:
        raise TimeoutError("DNS query timed out after 5.0s")

    with pytest.raises(ConnectorTransportError) as exc_timeout:
        enforce_connect_time_ssrf(
            "https://slow-dns.rig.example/witsml/store",
            "witsml.1.4.1.1.soap_http",
            resolver=_timeout_resolver,
        )
    assert exc_timeout.value.category == "timeout"
    assert exc_timeout.value.retryable is True

    with pytest.raises(ConnectorTransportError) as exc_nx:
        enforce_connect_time_ssrf(
            "https://nxdomain.rig.example/witsml/store",
            "witsml.1.4.1.1.soap_http",
            resolver=lambda h, p: [],
        )
    assert exc_nx.value.category == "dns_failure"
    assert exc_nx.value.retryable is True

    # Scenario 16: Production rejection of local harness ports, loopback flags, and test-only JSON ETP profile
    register_harness_endpoint("127.0.0.1", 19090)
    assert is_harness_endpoint("127.0.0.1", 19090, is_production=True) is False
    with pytest.raises(ValidationFailed) as exc_prod_loop:
        validate_connector_endpoint(
            "https://127.0.0.1:19090/witsml/store",
            "witsml.1.4.1.1.soap_http",
            allow_loopback=True,
            is_production=True,
        )
    assert exc_prod_loop.value.details["reason"] == "ssrf_blocked"


# ---------------------------------------------------------------------------
# Scenarios 11 & 12: Private Rig-Network CIDR Policy & Admin Governance
# ---------------------------------------------------------------------------


async def test_s11_s12_private_rig_network_cidr_policy_and_admin_governance_api(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify:
    - Scenario 11: Private network addresses (`10/8`, `172.16/12`, `192.168/16`, `fc00::/7`) are blocked
      when not explicitly in an approved private CIDR — even if the hostname is in `allowed_hosts`!
    - Scenario 12: When an admin approves `allowed_private_cidrs=["10.24.0.0/16"]` and
      `allowed_hosts=["historian.rig24.internal.corp"]`, resolving to `10.24.12.8` is permitted and
      pinned (`is_private_rig=True`), whereas resolving that same allowlisted hostname to an unapproved
      subnet (`192.168.1.10`) or loopback (`127.0.0.1`) is still blocked.
    - Non-admin roles (`engineer`, `viewer`) receive `403 Forbidden` on `PUT /api/v1/connectors/egress-policy`,
      and admin updates record an `AuditLog`.
    """
    # Scenario 11: Even if hostname is in allowed_hosts, resolving to private IP without approved CIDR fails
    for priv_ip in ("10.24.12.8", "172.20.4.15", "192.168.10.25", "fd12:3456:789a::1"):
        with pytest.raises(ValidationFailed) as exc11:
            resolve_and_pin_endpoint(
                "https://historian.rig24.corp/witsml/store",
                "witsml.1.4.1.1.soap_http",
                allowed_hosts=["historian.rig24.corp"],
                allowed_private_cidrs=[],
                resolver=lambda h, p, ip=priv_ip: [ip],
            )
        assert exc11.value.details["reason"] == "ssrf_blocked"

    # Scenario 12: Scoped private CIDR + allowlisted hostname succeeds for 10.24.0.0/16 only
    pinned = resolve_and_pin_endpoint(
        "https://historian.rig24.corp:8443/witsml/store",
        "witsml.1.4.1.1.soap_http",
        allowed_hosts=["historian.rig24.corp"],
        allowed_private_cidrs=["10.24.0.0/16"],
        resolver=lambda h, p: ["10.24.12.8"],
    )
    assert pinned.pinned_ip == "10.24.12.8"
    assert pinned.is_private_rig is True
    assert pinned.connect_url == "https://10.24.12.8:8443/witsml/store"
    assert pinned.host_header == "historian.rig24.corp:8443"
    assert pinned.sni_hostname == "historian.rig24.corp"

    # Same allowlisted hostname resolving outside 10.24.0.0/16 (e.g. 192.168.1.50 or 127.0.0.1) is blocked
    for unapproved_ip in ("192.168.1.50", "10.99.0.1", "127.0.0.1", "169.254.169.254"):
        with pytest.raises(ValidationFailed) as exc_outside:
            resolve_and_pin_endpoint(
                "https://historian.rig24.corp:8443/witsml/store",
                "witsml.1.4.1.1.soap_http",
                allowed_hosts=["historian.rig24.corp"],
                allowed_private_cidrs=["10.24.0.0/16"],
                resolver=lambda h, p, ip=unapproved_ip: [ip],
            )
        assert exc_outside.value.details["reason"] == "ssrf_blocked"

    # Wildcard / public / loopback CIDRs in allowed_private_cidrs are rejected
    for invalid_cidr in ("0.0.0.0/0", "127.0.0.0/8", "169.254.0.0/16", "8.8.8.0/24"):
        with pytest.raises(ValidationFailed) as cidr_exc:
            set_egress_policy(allowed_private_cidrs=[invalid_cidr])
        assert cidr_exc.value.details["reason"] == "invalid_cidr"

    # Verify API RBAC & AuditLog on GET/PUT /api/v1/connectors/egress-policy
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/egress_api.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)
    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with app.state.database.session() as session:
        session.add(Organization(id="org_egress", slug=settings.dev_org_slug, name="Egress Org"))
        await session.commit()

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            # Engineer cannot modify egress policy (403 Forbidden)
            forbidden_res = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "engineer"},
                json={
                    "reason": "attempt self-authorization",
                    "allowed_hosts": ["historian.rig24.corp"],
                    "allowed_private_cidrs": ["10.24.0.0/16"],
                },
            )
            assert forbidden_res.status_code == 403

            # Admin can update egress policy and the change is audited
            admin_res = await client.put(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "admin"},
                json={
                    "reason": "Approve Rig 24 OT network segment",
                    "allowed_hosts": ["historian.rig24.corp"],
                    "allowed_private_cidrs": ["10.24.0.0/16"],
                },
            )
            assert admin_res.status_code == 200
            body = admin_res.json()
            assert body["allowed_hosts"] == ["historian.rig24.corp"]
            assert body["allowed_private_cidrs"] == ["10.24.0.0/16"]

            get_res = await client.get(
                "/api/v1/connectors/egress-policy",
                headers={"X-Dev-Roles": "viewer"},
            )
            assert get_res.status_code == 200
            assert get_res.json()["allowed_private_cidrs"] == ["10.24.0.0/16"]

        from sqlalchemy import select

        async with app.state.database.session() as session:
            audits = (
                (
                    await session.execute(
                        select(AuditLog).where(AuditLog.action == "connector.egress_policy.update")
                    )
                )
                .scalars()
                .all()
            )
            assert len(audits) == 1
            assert audits[0].details.get("reason") == "Approve Rig 24 OT network segment"
    finally:
        await app.state.database.dispose()
        reset_settings_cache()


# ---------------------------------------------------------------------------
# Scenario 14: TLS Hostname Verification on an Approved Pinned IP
# ---------------------------------------------------------------------------


async def test_s14_tls_hostname_verification_on_approved_pinned_ip(tmp_path: Any):
    """Scenario 14: When connecting to an HTTPS WITSML endpoint via a pinned IP (`127.0.0.1`),
    TLS SNI and X.509 SAN hostname verification are preserved:
    - Matching hostname (`witsml-tls.drillai.test`) succeeds over TLS 1.3 / 1.2;
    - Mismatched hostname (`wrong-host.drillai.test`) resolving to the same pinned IP fails
      with `ConnectorTransportError(category="tls_error", retryable=False)`.
    """
    server_ssl_ctx, client_ssl_ctx = _generate_self_signed_tls_contexts(
        "witsml-tls.drillai.test", tmp_path
    )

    async def _tls_witsml_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            content_len = 0
            for line in raw.decode("latin-1").split("\r\n")[1:]:
                if line.lower().startswith("content-length:"):
                    content_len = int(line.split(":", 1)[1].strip())
            if content_len:
                await reader.readexactly(content_len)
            soap = (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
                b'xmlns:wmls="http://www.witsml.org/message/120">'
                b"<soapenv:Body><wmls:WMLS_GetVersionResponse><Result>1.4.1.1</Result>"
                b"</wmls:WMLS_GetVersionResponse></soapenv:Body></soapenv:Envelope>"
            )
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\nContent-Length: {len(soap)}\r\nConnection: close\r\n\r\n".encode(
                    "latin-1"
                )
                + soap
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            with pytest.raises(Exception) if False else _null_ctx():
                writer.close()

    class _null_ctx:
        def __enter__(self):
            return None

        def __exit__(self, *args):
            return True

    srv = await asyncio.start_server(_tls_witsml_handler, "127.0.0.1", 0, ssl=server_ssl_ctx)
    port = int(srv.sockets[0].getsockname()[1])
    register_harness_endpoint("127.0.0.1", port)
    set_custom_dns_resolver(lambda host, p: ["127.0.0.1"])

    try:
        # 1. Matching certificate SAN hostname succeeds on pinned IP 127.0.0.1
        ok_client = WitsmlSoapClient(
            endpoint_url=f"https://witsml-tls.drillai.test:{port}/witsml/store",
            ssl_context=client_ssl_ctx,
            timeout_seconds=5.0,
            max_retries=0,
        )
        assert await ok_client.get_version() == "1.4.1.1"

        # 2. Mismatched hostname resolving to the exact same pinned IP fails TLS hostname verification
        bad_client = WitsmlSoapClient(
            endpoint_url=f"https://wrong-host.drillai.test:{port}/witsml/store",
            ssl_context=client_ssl_ctx,
            timeout_seconds=5.0,
            max_retries=0,
        )
        with pytest.raises(ConnectorTransportError) as tls_exc:
            await bad_client.get_version()
        assert tls_exc.value.category == "tls_error"
        assert tls_exc.value.retryable is False
    finally:
        unregister_harness_endpoint("127.0.0.1", port)
        srv.close()
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(srv.wait_closed(), timeout=1.0)
