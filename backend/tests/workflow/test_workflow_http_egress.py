"""Connection-time egress invariants for workflow HTTP nodes and webhook delivery."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from pydantic import ValidationError

from drillai.core.config import reset_settings_cache
from drillai.core.errors import ValidationFailed
from drillai.db.models import Organization, OutboundMessage
from drillai.security.actions import ActionLevel, Principal
from drillai.telemetry.connectors import (
    clear_harness_endpoints,
    register_harness_endpoint,
    reset_egress_policy,
    set_custom_dns_resolver,
)
from drillai.workflow.nodes import HttpFetchConfig, NodeContext, NotifyConfig, _http_fetch
from drillai.workflow.notifications import WebhookNotificationAdapter


@pytest.fixture(autouse=True)
def _reset_egress_state(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "development")
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "false")
    reset_settings_cache()
    set_custom_dns_resolver(None)
    clear_harness_endpoints()
    reset_egress_policy()
    yield
    set_custom_dns_resolver(None)
    clear_harness_endpoints()
    reset_egress_policy()
    reset_settings_cache()


class _HttpEndpoints:
    def __init__(self) -> None:
        self.origin_requests: list[dict[str, Any]] = []
        self.target_requests: list[bytes] = []
        self.origin: asyncio.AbstractServer | None = None
        self.target: asyncio.AbstractServer | None = None
        self.origin_port = 0
        self.target_port = 0

    async def __aenter__(self) -> _HttpEndpoints:
        self.target = await asyncio.start_server(self._target, "127.0.0.1", 0)
        self.target_port = int(self.target.sockets[0].getsockname()[1])
        self.origin = await asyncio.start_server(self._origin, "127.0.0.1", 0)
        self.origin_port = int(self.origin.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *_: Any) -> None:
        for server in (self.origin, self.target):
            if server is not None:
                server.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(server.wait_closed(), timeout=1.0)

    @property
    def host_url(self) -> str:
        return f"http://workflow-egress-test.invalid:{self.origin_port}"

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[bytes, dict[str, str], bytes]:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3.0)
        lines = head.decode("latin-1").split("\r\n")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        body = await asyncio.wait_for(reader.readexactly(length), timeout=3.0) if length else b""
        return lines[0].encode("latin-1"), headers, body

    async def _origin(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line, headers, body = await self._read_request(reader)
            self.origin_requests.append({"line": request_line, "headers": headers, "body": body})
            path = request_line.decode("latin-1").split(" ", 2)[1]
            if path.startswith("/redirect"):
                status = b"302 Found"
                location = f"http://127.0.0.1:{self.target_port}/metadata"
                response_headers = f"Location: {location}\r\n".encode("latin-1")
                response_body = b""
            else:
                status = b"200 OK"
                response_headers = b"Content-Type: text/plain\r\n"
                response_body = b"pinned-origin"
            writer.write(
                b"HTTP/1.1 "
                + status
                + b"\r\n"
                + response_headers
                + f"Content-Length: {len(response_body)}\r\nConnection: close\r\n\r\n".encode("latin-1")
                + response_body
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=0.5)

    async def _target(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line, _headers, _body = await self._read_request(reader)
            self.target_requests.append(request_line)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=0.5)


async def _start_proxy_trap() -> tuple[asyncio.AbstractServer, int, list[bytes]]:
    requests: list[bytes] = []

    async def _trap(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            requests.append(await asyncio.wait_for(reader.read(4096), timeout=1.0))
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout=0.5)

    server = await asyncio.start_server(_trap, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1]), requests


def _install_test_resolver() -> list[tuple[str, int]]:
    calls: list[tuple[str, int]] = []

    def _resolve(host: str, port: int) -> list[str]:
        calls.append((host, port))
        return ["127.0.0.1"]

    set_custom_dns_resolver(_resolve)
    return calls


def _poison_proxy_environment(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    proxy = f"http://127.0.0.1:{port}"
    for variable in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(variable, proxy)
    for variable in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(variable, "")


def test_workflow_http_nodes_reject_inline_credential_headers():
    with pytest.raises(ValidationError):
        HttpFetchConfig(
            url="https://public.example.invalid/data",
            headers={"Authorization": "Bearer do-not-store-this"},
        )
    with pytest.raises(ValidationError):
        HttpFetchConfig(
            url="https://public.example.invalid/data",
            headers={"X-API-Key": "do-not-store-this"},
        )
    with pytest.raises(ValidationError):
        NotifyConfig(
            channel="webhook",
            recipient="https://public.example.invalid/hook",
            body="safe body",
            headers={"Cookie": "session=do-not-store-this"},
        )


@pytest.mark.asyncio
async def test_http_fetch_pins_dns_disables_environment_proxy_and_refuses_redirects(session, monkeypatch):
    async with _HttpEndpoints() as endpoints:
        register_harness_endpoint("127.0.0.1", endpoints.origin_port)
        dns_calls = _install_test_resolver()
        proxy, proxy_port, proxy_requests = await _start_proxy_trap()
        _poison_proxy_environment(monkeypatch, proxy_port)
        try:
            context = NodeContext(
                session=session,
                org_id="org_http_egress",
                principal=Principal(
                    id="usr_http_egress",
                    max_action_level=ActionLevel.EXECUTE_WITH_APPROVAL,
                ),
                run_id="run_http_egress",
                node_id="fetch",
                node_name="Pinned HTTP fetch",
                inputs={},
                variables={},
                node_outputs={},
            )
            ok = await _http_fetch(
                context,
                HttpFetchConfig(url=f"{endpoints.host_url}/ok?token=do-not-log"),
            )
            redirect = await _http_fetch(
                context,
                HttpFetchConfig(url=f"{endpoints.host_url}/redirect"),
            )
        finally:
            proxy.close()
            await proxy.wait_closed()

    assert ok.outputs["status_code"] == 200
    assert ok.outputs["body_preview"] == "pinned-origin"
    assert "do-not-log" not in " ".join(ok.notes)
    assert redirect.outputs["status_code"] == 302
    assert redirect.outputs["ok"] is False
    assert len(endpoints.origin_requests) == 2
    assert endpoints.origin_requests[0]["line"] == b"GET /ok?token=do-not-log HTTP/1.1"
    assert endpoints.origin_requests[0]["headers"]["host"] == f"workflow-egress-test.invalid:{endpoints.origin_port}"
    assert endpoints.origin_requests[1]["line"] == b"GET /redirect HTTP/1.1"
    assert endpoints.target_requests == []
    assert proxy_requests == []
    assert len(dns_calls) == 2
    assert all(host == "workflow-egress-test.invalid" for host, _port in dns_calls)


@pytest.mark.asyncio
async def test_webhook_refuses_legacy_plaintext_credential_headers(session):
    org = Organization(slug="webhook-secret-headers", name="Webhook Secret Headers")
    session.add(org)
    await session.flush()

    async with _HttpEndpoints() as endpoints:
        register_harness_endpoint("127.0.0.1", endpoints.origin_port)
        _install_test_resolver()
        message = OutboundMessage(
            id="obm_legacy_secret_header",
            org_id=org.id,
            channel="webhook",
            recipient=endpoints.host_url,
            subject="Legacy unsafe header",
            body="This must not be delivered.",
            payload={"headers": {"X-API-Key": "do-not-send"}},
        )
        outcome = await WebhookNotificationAdapter().send(session, message)

    assert outcome.status == "failed_terminal"
    assert outcome.error_code == "plaintext_secret_header_forbidden"
    assert "do-not-send" not in str(outcome)
    assert endpoints.origin_requests == []


@pytest.mark.asyncio
async def test_webhook_pins_dns_disables_environment_proxy_and_refuses_redirects(session, monkeypatch):
    org = Organization(slug="webhook-egress", name="Webhook Egress")
    session.add(org)
    await session.flush()

    async with _HttpEndpoints() as endpoints:
        register_harness_endpoint("127.0.0.1", endpoints.origin_port)
        dns_calls = _install_test_resolver()
        proxy, proxy_port, proxy_requests = await _start_proxy_trap()
        _poison_proxy_environment(monkeypatch, proxy_port)
        try:
            message = OutboundMessage(
                id="obm_workflow_egress",
                org_id=org.id,
                channel="webhook",
                recipient=f"{endpoints.host_url}/redirect",
                subject="Approved workflow notification",
                body="The outbound request is test-scoped.",
                payload={"headers": {"Host": "attacker.invalid"}},
            )
            outcome = await WebhookNotificationAdapter().send(session, message)
        finally:
            proxy.close()
            await proxy.wait_closed()

    assert outcome.status == "failed_terminal"
    assert outcome.status_code == 302
    assert len(endpoints.origin_requests) == 1
    assert endpoints.origin_requests[0]["line"] == b"POST /redirect HTTP/1.1"
    assert endpoints.origin_requests[0]["headers"]["host"] == f"workflow-egress-test.invalid:{endpoints.origin_port}"
    assert endpoints.target_requests == []
    assert proxy_requests == []
    assert len(dns_calls) == 1
    assert dns_calls[0][0] == "workflow-egress-test.invalid"


@pytest.mark.asyncio
async def test_http_endpoint_rejects_unapproved_private_dns_answer():
    from drillai.telemetry.connectors import resolve_and_pin_http_endpoint_async

    set_custom_dns_resolver(lambda _host, _port: ["10.20.30.40"])
    with pytest.raises(ValidationFailed) as caught:
        await resolve_and_pin_http_endpoint_async("https://private-target.invalid/metadata")
    assert caught.value.details["reason"] == "ssrf_blocked"
