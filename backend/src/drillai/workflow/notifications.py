"""Outbound notification adapter boundary, dispatch state machine, and bounded attempt ledger.

The dispatcher records *acceptance* by the immediate transport, not receipt by a human. No built-in
adapter receives a delivery callback or independently verified provider receipt:

* **Webhook / HTTP adapter (`channel="webhook"`)**:
  - Enforces the authoritative database-backed egress policy, resolves and pins the destination IP,
    disables proxy-environment routing, and refuses redirects so webhooks cannot bypass SSRF checks.
  - Resolves credentials exclusively from environment/vault secret references (`secret_ref`) without
    storing raw secrets in database JSON columns.
  - Signs payloads with `X-DrillAI-Signature-256: sha256=<hmac>` when a signing secret reference is
    provided, and attaches `Idempotency-Key` and `X-DrillAI-Message-Id` headers.
  - Enforces explicit request timeouts and classifies outcomes into `accepted` (`2xx` HTTP response),
    `failed_retryable` (`408`, `429`, `5xx`, network/timeout errors), `failed_terminal` (`4xx`
    client errors or egress policy blocks), and `dead_letter` (retry ceiling exhausted).

* **SMTP Email adapter (`channel="email"`)**:
  - Submits RFC 5322 email messages to an SMTP server with explicit timeout and transient (`4xx`)
    vs permanent (`5xx`) response classification. Production requires the operator-configured SMTPS
    endpoint, checks the authoritative egress policy, and connects to a validated, DNS-pinned address
    while retaining hostname-based TLS verification. SMTP acceptance is not mailbox delivery.

* **In-App adapter (`channel="in_app"`)**:
  - Records acceptance by the local outbox only; there is no recipient inbox UI or read receipt.

* **Evidence Boundary**:
  - `accepted` means HTTP 2xx, SMTP server acceptance, or local outbox acceptance. It never means
    recipient delivery. A provider message id is stored only when the remote endpoint actually
    supplies one; generated local identifiers are not presented as provider receipts.
  - Every adapter keeps `external_provider_verified=false`. No delivery callback, read receipt, or
    live SaaS certification is implemented.
"""

from __future__ import annotations

import abc
import asyncio
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import utc_now
from drillai.core.errors import ValidationFailed
from drillai.db.models import OutboundMessage, SecretRef
from drillai.security.secrets import is_credential_header_name, is_valid_env_secret_name
from drillai.telemetry.connectors import (
    resolve_and_pin_http_endpoint_async,
    resolve_and_pin_smtp_endpoint_async,
    sync_egress_policy_from_db,
)

__all__ = [
    "DELIVERY_STATUSES",
    "MAX_DELIVERY_LEDGER_ENTRIES",
    "STALE_SENDING_AFTER",
    "DeliveryAttemptOutcome",
    "EmailSmtpNotificationAdapter",
    "InAppNotificationAdapter",
    "NotificationAdapter",
    "NotificationDispatcher",
    "WebhookNotificationAdapter",
    "claim_pending_notifications",
    "dispatch_outbound_message",
    "dispatch_pending_notifications",
    "resolve_notification_secret",
    "resolve_notification_secret_async",
    "safe_notification_error_code",
]

DELIVERY_STATUSES = (
    "queued",
    "sending",
    "accepted",
    "sent",  # legacy rows; not a recipient-delivery receipt
    "failed_retryable",
    "failed_terminal",
    "dead_letter",
    "reconciliation_required",  # stale send with an ambiguous remote outcome
)
MAX_DELIVERY_LEDGER_ENTRIES = 20
MAX_PENDING_DELIVERY_BATCH = 100
STALE_SENDING_AFTER = dt.timedelta(minutes=5)


def _bounded_timeout(value: Any, *, default: float = 10.0) -> float | None:
    if value is None:
        return default
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and 0.1 <= seconds <= 120.0 else None


def resolve_notification_secret(secret_ref: str | None) -> str | None:
    """Resolve an explicitly scoped notification secret, never an arbitrary process variable."""
    if not secret_ref or not secret_ref.strip():
        return None
    key = secret_ref.strip()
    if key.startswith("env:"):
        env_name = key[4:].strip()
        if not is_valid_env_secret_name(env_name):
            return None
        return os.environ.get(env_name) or None
    env_prefixed = f"DRILLAI_NOTIFY_SECRET_{key.upper().replace('-', '_').replace('.', '_')}"
    return os.environ.get(env_prefixed) or None


async def resolve_notification_secret_async(
    session: AsyncSession,
    org_id: str,
    secret_ref: str | None,
) -> str | None:
    """Resolve a notification secret from either ``secret_ref:<key>`` (DB SecretRef) or environment."""
    if not secret_ref or not secret_ref.strip():
        return None
    ref = secret_ref.strip()
    if ref.startswith("secret_ref:"):
        key = ref.removeprefix("secret_ref:").strip()
        row = (
            await session.execute(
                select(SecretRef).where(
                    SecretRef.org_id == org_id,
                    SecretRef.key == key,
                    SecretRef.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.last_used_at = utc_now()
        if row.backend == "env":
            env_name = row.locator.removeprefix("env:").strip()
            if not is_valid_env_secret_name(env_name):
                return None
            return os.environ.get(env_name) or None
        if row.backend == "db_encrypted":
            from drillai.security.secrets import decrypt_secret_value

            try:
                return decrypt_secret_value(row.locator, org_id=org_id, secret_key=row.key)
            except ValidationFailed:
                return None
        return None
    return resolve_notification_secret(ref)


@dataclass(frozen=True)
class DeliveryAttemptOutcome:
    status: str  # "accepted" | "failed_retryable" | "failed_terminal"
    retryable: bool
    acceptance_evidence: str | None = None
    provider_message_id: str | None = None  # only an identifier returned by the remote endpoint
    status_code: int | None = None
    latency_ms: float = 0.0
    error_code: str | None = None
    error: str | None = None
    external_provider_verified: bool = False
    details: dict[str, Any] = field(default_factory=dict)


_SAFE_NOTIFICATION_ERROR_CODES = frozenset(
    {
        "adapter_error",
        "missing_recipient_url",
        "egress_policy_blocked",
        "ssrf_blocked",
        "dns_lookup_timeout",
        "dns_failure",
        "timeout",
        "transient_store_error",
        "invalid_timeout",
        "secret_ref_unresolved",
        "plaintext_secret_header_forbidden",
        "invalid_email_recipient",
        "smtp_not_configured",
        "smtp_endpoint_policy_blocked",
        "invalid_smtp_url",
        "smtp_tls_required",
        "smtp_recipients_refused",
        "smtp_transport_error",
        "unsupported_channel",
        "stale_sending_ambiguous",
        "invalid_endpoint",
        "invalid_scheme",
        "invalid_hostname",
        "invalid_port",
        "endpoint_required",
        "endpoint_too_long",
        "profile_not_production_approved",
    }
)


def safe_notification_error_code(value: str | None) -> str | None:
    """Return an allowlisted error identifier, never adapter-supplied free text or secret refs."""
    if not value:
        return None
    normalized = "".join(
        char for char in str(value) if char.isascii() and (char.isalnum() or char in "_.-")
    )[:80]
    if normalized in _SAFE_NOTIFICATION_ERROR_CODES:
        return normalized
    prefix, separator, suffix = normalized.partition("_")
    if separator and prefix in {"http", "smtp", "provider"} and len(suffix) == 3 and suffix.isdigit():
        status_code = int(suffix)
        if 400 <= status_code <= 599:
            return normalized
    return "adapter_error"


def _safe_outcome_error(outcome: DeliveryAttemptOutcome) -> str:
    code = safe_notification_error_code(outcome.error_code)
    return f"notification attempt failed ({code})" if code else "notification attempt failed"


def _safe_provider_message_id(
    value: str | None,
    *,
    message: OutboundMessage | None = None,
) -> str | None:
    """Keep a bounded opaque receipt token, not URLs, recipients, or secret locators."""
    if not value:
        return None
    candidate = str(value).strip()
    if (
        len(candidate) > 128
        or not candidate
        or not candidate[0].isascii()
        or not candidate[0].isalnum()
        or any(
            not char.isascii() or not (char.isalnum() or char in "._-")
            for char in candidate
        )
    ):
        return None
    if candidate.upper().startswith(("DRILLAI_SECRET_", "SECRET_REF_")):
        return None
    if message is not None:
        payload = message.payload if isinstance(message.payload, dict) else {}
        explicit_sensitive_values = (message.recipient, payload.get("secret_ref"))
        if any(
            isinstance(sensitive, str) and candidate.casefold() == sensitive.strip().casefold()
            for sensitive in explicit_sensitive_values
        ):
            return None
    return candidate


class NotificationAdapter(abc.ABC):
    channel: str

    @abc.abstractmethod
    async def send(
        self,
        session: AsyncSession,
        message: OutboundMessage,
    ) -> DeliveryAttemptOutcome:
        ...


class WebhookNotificationAdapter(NotificationAdapter):
    """Outbound HTTP/Webhook notification adapter with egress policy, HMAC signing, and retry classification."""

    channel = "webhook"

    async def send(
        self,
        session: AsyncSession,
        message: OutboundMessage,
    ) -> DeliveryAttemptOutcome:
        started = time.perf_counter()
        recipient_url = (message.recipient or "").strip()
        if not recipient_url:
            return DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="missing_recipient_url",
                error="webhook notification requires a target URL in recipient",
            )

        # 1. Load the authoritative egress policy and pin the exact destination IP that is approved.
        try:
            allow_loopback = os.environ.get("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "").strip().lower() in {
                "1",
                "true",
                "yes",
            }
            await sync_egress_policy_from_db(session)
            pinned = await resolve_and_pin_http_endpoint_async(
                recipient_url,
                allow_loopback=allow_loopback,
                dns_timeout_seconds=5.0,
            )
        except ValidationFailed as exc:
            details = exc.details or {}
            reason = str(details.get("reason") or "egress_policy_blocked")
            policy_state = str(details.get("policy_state") or "")
            is_retryable = reason in {"dns_lookup_timeout", "timeout"} or policy_state == "transient_store_error"
            return DeliveryAttemptOutcome(
                status="failed_retryable" if is_retryable else "failed_terminal",
                retryable=is_retryable,
                error_code=reason,
                error="webhook destination was rejected by the egress policy",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        # 2. Construct canonical JSON body & headers (resolving secret_ref if present)
        cfg_payload = dict(message.payload or {})
        timeout_seconds = _bounded_timeout(cfg_payload.get("timeout_seconds"))
        if timeout_seconds is None:
            return DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="invalid_timeout",
                error="webhook timeout must be between 0.1 and 120 seconds",
            )
        secret_ref = cfg_payload.get("secret_ref")
        secret_val = (
            await resolve_notification_secret_async(session, message.org_id, secret_ref)
            if isinstance(secret_ref, str)
            else None
        )
        if secret_ref and secret_val is None:
            return DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="secret_ref_unresolved",
                error="configured webhook secret reference could not be resolved",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        wire_body = {
            "message_id": message.id,
            "org_id": message.org_id,
            "well_id": message.well_id,
            "workflow_run_id": message.workflow_run_id,
            "channel": message.channel,
            "subject": message.subject,
            "body": message.body,
            "idempotency_key": message.idempotency_key,
            "evidence_refs": list(message.evidence_refs or []),
        }
        encoded_body = json.dumps(wire_body, sort_keys=True, separators=(",", ":")).encode("utf-8")

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-DrillAI-Message-Id": message.id,
        }
        if message.idempotency_key:
            headers["Idempotency-Key"] = message.idempotency_key
        extra_headers = cfg_payload.get("headers")
        if isinstance(extra_headers, dict):
            if any(is_credential_header_name(str(name)) for name in extra_headers):
                return DeliveryAttemptOutcome(
                    status="failed_terminal",
                    retryable=False,
                    error_code="plaintext_secret_header_forbidden",
                    error="credential-bearing custom headers must use a managed secret reference",
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                )
            for hk, hv in extra_headers.items():
                if str(hk).lower() != "host":
                    headers[str(hk)] = str(hv)
        headers["Host"] = pinned.host_header
        extensions = {"sni_hostname": pinned.sni_hostname} if pinned.scheme == "https" else {}
        if secret_val:
            auth_scheme = str(cfg_payload.get("auth_scheme") or "hmac_sha256").lower()
            if auth_scheme == "bearer":
                headers["Authorization"] = f"Bearer {secret_val}"
            else:
                sig = hmac.new(secret_val.encode("utf-8"), encoded_body, hashlib.sha256).hexdigest()
                headers["X-DrillAI-Signature-256"] = f"sha256={sig}"

        # 3. Execute HTTP POST with explicit timeout and classify response
        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client, client.stream(
                "POST",
                pinned.connect_url,
                content=encoded_body,
                headers=headers,
                extensions=extensions,
            ) as response:
                code = response.status_code
                provider_msg_id = response.headers.get("X-Provider-Message-Id")
        except httpx.TimeoutException:
            return DeliveryAttemptOutcome(
                status="failed_retryable",
                retryable=True,
                error_code="webhook_timeout",
                error=f"webhook transport timed out after {timeout_seconds}s",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except httpx.HTTPError as exc:
            return DeliveryAttemptOutcome(
                status="failed_retryable",
                retryable=True,
                error_code="webhook_network_error",
                error=f"webhook transport failed ({type(exc).__name__})",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        latency_ms = (time.perf_counter() - started) * 1000.0
        if 200 <= code < 300:
            return DeliveryAttemptOutcome(
                status="accepted",
                retryable=False,
                acceptance_evidence="http_2xx",
                provider_message_id=provider_msg_id,
                status_code=code,
                latency_ms=latency_ms,
                external_provider_verified=False,
            )

        if code in {408, 429} or code >= 500:
            return DeliveryAttemptOutcome(
                status="failed_retryable",
                retryable=True,
                status_code=code,
                error_code=f"http_{code}",
                error=f"webhook returned transient HTTP {code}",
                latency_ms=latency_ms,
            )

        return DeliveryAttemptOutcome(
            status="failed_terminal",
            retryable=False,
            status_code=code,
            error_code=f"http_{code}",
            error=f"webhook returned permanent HTTP {code}",
            latency_ms=latency_ms,
        )


class _PinnedSMTPSSL(smtplib.SMTP_SSL):
    """TLS SMTP client that connects to the policy-approved IP but keeps the hostname for SNI."""

    def __init__(self, *, pinned_ip: str, **kwargs: Any) -> None:
        self._pinned_ip = pinned_ip
        super().__init__(**kwargs)

    def _get_socket(self, _host: str, port: int, timeout: float | None):
        # SMTP_SSL wraps this socket with server_hostname=self._host, preserving certificate
        # verification for the configured DNS name while preventing a second DNS lookup at connect.
        return super()._get_socket(self._pinned_ip, port, timeout)


class EmailSmtpNotificationAdapter(NotificationAdapter):
    """Outbound SMTP email adapter with timeout, SecretRef credentials, and 4xx/5xx error classification."""

    channel = "email"

    async def send(
        self,
        session: AsyncSession,
        message: OutboundMessage,
    ) -> DeliveryAttemptOutcome:
        started = time.perf_counter()
        recipient = (message.recipient or "").strip()
        if not recipient or "@" not in recipient:
            return DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="invalid_email_recipient",
                error="invalid email recipient",
            )
        cfg = dict(message.payload or {})
        from drillai.core.config import get_settings

        production = get_settings().is_production
        configured_smtp_url = str(os.environ.get("DRILLAI_SMTP_URL") or "").strip()
        payload_smtp_url = str(cfg.get("smtp_url") or "").strip()
        if production and payload_smtp_url and payload_smtp_url != configured_smtp_url:
            return DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="smtp_endpoint_policy_blocked",
                error="production email transport must use the operator-configured SMTP endpoint",
            )
        smtp_url = (
            configured_smtp_url
            if production
            else payload_smtp_url or configured_smtp_url
        )
        if not smtp_url:
            return DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="smtp_not_configured",
                error="no smtp_url or DRILLAI_SMTP_URL configured for email delivery",
            )
        try:
            parsed = urlparse(smtp_url)
            port = parsed.port
        except ValueError:
            return DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="invalid_smtp_url",
                error="configured SMTP URL is invalid",
            )
        if parsed.scheme not in {"smtp", "smtps"} or not parsed.hostname or parsed.password:
            return DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="invalid_smtp_url",
                error="SMTP URL must include a valid smtp/smtps host and no embedded password",
            )
        host = parsed.hostname
        port = port or (465 if parsed.scheme == "smtps" else 25)
        if production and parsed.scheme != "smtps":
            return DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="smtp_tls_required",
                error="production email transport requires an smtps URL",
            )
        timeout_seconds = _bounded_timeout(cfg.get("timeout_seconds"))
        if timeout_seconds is None:
            return DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="invalid_timeout",
                error="email timeout must be between 0.1 and 120 seconds",
            )

        pinned_smtp_endpoint = None
        if production:
            try:
                host_for_url = f"[{host}]" if ":" in host else host
                egress_url = f"smtps://{host_for_url}:{port}"
                allow_loopback = os.environ.get("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                }
                await sync_egress_policy_from_db(session)
                pinned_smtp_endpoint = await resolve_and_pin_smtp_endpoint_async(
                    egress_url,
                    allow_loopback=allow_loopback,
                    is_production=production,
                    dns_timeout_seconds=5.0,
                )
            except ValidationFailed as exc:
                details = exc.details or {}
                reason = str(details.get("reason") or "egress_policy_blocked")
                policy_state = str(details.get("policy_state") or "")
                is_retryable = (
                    reason in {"dns_lookup_timeout", "timeout"}
                    or policy_state == "transient_store_error"
                )
                return DeliveryAttemptOutcome(
                    status="failed_retryable" if is_retryable else "failed_terminal",
                    retryable=is_retryable,
                    error_code="smtp_endpoint_policy_blocked",
                    error="configured SMTP destination was rejected by the egress policy",
                    latency_ms=(time.perf_counter() - started) * 1000.0,
                )

        sender = str(cfg.get("from_address") or os.environ.get("DRILLAI_SMTP_FROM") or "noreply@drillai.local")
        secret_ref = cfg.get("secret_ref")
        password = (
            await resolve_notification_secret_async(session, message.org_id, secret_ref)
            if isinstance(secret_ref, str)
            else None
        )
        if secret_ref and password is None:
            return DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="secret_ref_unresolved",
                error="configured email secret reference could not be resolved",
            )

        msg = EmailMessage()
        msg["Subject"] = message.subject or f"DrillAI Notification ({message.id})"
        msg["From"] = sender
        msg["To"] = recipient
        msg["Message-ID"] = f"<{message.id}@drillai.local>"
        if message.idempotency_key:
            msg["X-DrillAI-Idempotency-Key"] = message.idempotency_key
        msg.set_content(message.body or "")

        def _send_sync() -> str:
            if pinned_smtp_endpoint is not None:
                client = _PinnedSMTPSSL(
                    pinned_ip=pinned_smtp_endpoint.pinned_ip,
                    host=pinned_smtp_endpoint.sni_hostname,
                    port=port,
                    timeout=timeout_seconds,
                    context=ssl.create_default_context(),
                )
            elif parsed.scheme == "smtps":
                client = smtplib.SMTP_SSL(
                    host=host,
                    port=port,
                    timeout=timeout_seconds,
                    context=ssl.create_default_context(),
                )
            else:
                client = smtplib.SMTP(host=host, port=port, timeout=timeout_seconds)
            with client as smtp_client:
                if parsed.username and password:
                    smtp_client.login(parsed.username, password)
                smtp_client.send_message(msg)
            return str(msg["Message-ID"])

        try:
            await asyncio.to_thread(_send_sync)
            return DeliveryAttemptOutcome(
                status="accepted",
                retryable=False,
                acceptance_evidence="smtp_server_accepted",
                status_code=250,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                external_provider_verified=False,
            )
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [int(response[0]) for response in exc.recipients.values() if response]
            retryable = bool(codes) and all(400 <= code < 500 for code in codes)
            status_code = max(codes) if codes else None
            return DeliveryAttemptOutcome(
                status="failed_retryable" if retryable else "failed_terminal",
                retryable=retryable,
                status_code=status_code,
                error_code="smtp_recipients_refused",
                error="SMTP server refused the notification recipient",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except smtplib.SMTPResponseException as exc:
            code = int(exc.smtp_code)
            retryable = 400 <= code < 500
            return DeliveryAttemptOutcome(
                status="failed_retryable" if retryable else "failed_terminal",
                retryable=retryable,
                status_code=code,
                error_code=f"smtp_{code}",
                error=f"SMTP server returned status {code}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except (TimeoutError, OSError, smtplib.SMTPException) as exc:
            return DeliveryAttemptOutcome(
                status="failed_retryable",
                retryable=True,
                error_code="smtp_transport_error",
                error=f"SMTP transport failed ({type(exc).__name__})",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )


class InAppNotificationAdapter(NotificationAdapter):
    """Internal in-app inbox notification adapter."""

    channel = "in_app"

    async def send(
        self,
        session: AsyncSession,
        message: OutboundMessage,
    ) -> DeliveryAttemptOutcome:
        return DeliveryAttemptOutcome(
            status="accepted",
            retryable=False,
            acceptance_evidence="local_in_app_outbox",
            status_code=200,
            latency_ms=0.1,
            external_provider_verified=False,
        )


class NotificationDispatcher:
    """Dispatches `OutboundMessage` rows with idempotency protection and bounded delivery ledger."""

    def __init__(self, adapters: dict[str, NotificationAdapter] | None = None) -> None:
        default_adapters: list[NotificationAdapter] = [
            WebhookNotificationAdapter(),
            EmailSmtpNotificationAdapter(),
            InAppNotificationAdapter(),
        ]
        self._adapters: dict[str, NotificationAdapter] = {a.channel: a for a in default_adapters}
        if adapters:
            self._adapters.update(adapters)

    def register_adapter(self, adapter: NotificationAdapter) -> None:
        self._adapters[adapter.channel] = adapter

    async def dispatch_message(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        *,
        now: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Deprecated inline API: caller-owned transactions may never perform notification I/O."""
        raise RuntimeError("inline notification dispatch is disabled; use NotificationWorker.run_once()")

    async def _dispatch_claimed_message(
        self,
        session: AsyncSession,
        message: OutboundMessage,
        *,
        now: dt.datetime | None = None,
        refresh: bool = True,
    ) -> dict[str, Any]:
        """Finish a durably claimed `sending` row. The worker commits the claim before calling this."""
        now = now or utc_now()
        if refresh and message.id is not None:
            await session.refresh(message, with_for_update=True)
        if message.status != "sending":
            return self._dispatch_result(message, duplicate_suppressed=True)

        suppressed = await self._suppress_accepted_sibling(session, message)
        if suppressed is not None:
            return suppressed

        adapter = self._adapters.get(message.channel)
        if adapter is None:
            outcome = DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="unsupported_channel",
                error=f"no notification adapter registered for channel {message.channel!r}",
            )
        else:
            outcome = await adapter.send(session, message)

        max_attempts = max(1, int(message.max_attempts or 3))
        if outcome.status == "accepted":
            if outcome.acceptance_evidence not in {"http_2xx", "smtp_server_accepted", "local_in_app_outbox"}:
                raise ValueError("notification adapter acceptance must include a recognized evidence kind")
            message.status = "accepted"
            message.accepted_at = now
            message.acceptance_evidence = outcome.acceptance_evidence
            message.next_attempt_at = None
            message.error = None
            message.provider_message_id = _safe_provider_message_id(outcome.provider_message_id, message=message)
        elif outcome.retryable and message.attempts < max_attempts:
            message.status = "failed_retryable"
            backoff_seconds = min(300, (2 ** message.attempts) * 5)
            message.next_attempt_at = now + dt.timedelta(seconds=backoff_seconds)
            message.error = _safe_outcome_error(outcome)
        elif outcome.retryable and message.attempts >= max_attempts:
            message.status = "dead_letter"
            message.next_attempt_at = None
            message.error = _safe_outcome_error(outcome)
        else:
            message.status = "failed_terminal"
            message.next_attempt_at = None
            message.error = _safe_outcome_error(outcome)

        self._append_ledger(message, now=now, outcome=outcome)
        await session.flush()
        result = self._dispatch_result(message, duplicate_suppressed=False)
        result.update(
            {
                "max_attempts": max_attempts,
                "next_attempt_at": message.next_attempt_at.isoformat() if message.next_attempt_at else None,
                "error": message.error,
                "external_provider_verified": False,
            }
        )
        return result

    async def _suppress_accepted_sibling(
        self,
        session: AsyncSession,
        message: OutboundMessage,
    ) -> dict[str, Any] | None:
        if not message.idempotency_key:
            return None
        existing = (
            await session.execute(
                select(OutboundMessage)
                .where(
                    OutboundMessage.org_id == message.org_id,
                    OutboundMessage.idempotency_key == message.idempotency_key,
                    OutboundMessage.id != message.id,
                    OutboundMessage.status.in_(("accepted", "sent")),
                )
                .order_by(OutboundMessage.created_at.asc(), OutboundMessage.id.asc())
                .limit(1)
            )
        ).scalars().first()
        if existing is None:
            return None
        message.status = existing.status
        message.provider_message_id = existing.provider_message_id
        message.accepted_at = existing.accepted_at
        message.acceptance_evidence = existing.acceptance_evidence
        message.sent_at = existing.sent_at
        await session.flush()
        return self._dispatch_result(message, duplicate_suppressed=True)

    @staticmethod
    def _dispatch_result(message: OutboundMessage, *, duplicate_suppressed: bool) -> dict[str, Any]:
        return {
            "message_id": message.id,
            "status": message.status,
            "duplicate_suppressed": duplicate_suppressed,
            "provider_message_id": message.provider_message_id,
            "accepted_at": message.accepted_at.isoformat() if message.accepted_at else None,
            "acceptance_evidence": message.acceptance_evidence,
            "delivery_confirmed": False,
            "external_provider_verified": False,
            "attempts": message.attempts,
        }

    def _append_ledger(
        self,
        message: OutboundMessage,
        *,
        now: dt.datetime,
        outcome: DeliveryAttemptOutcome,
    ) -> None:
        attrs = dict(message.attributes or {})
        ledger = list(attrs.get("delivery_ledger") or [])
        ledger.append(
            {
                "attempt": message.attempts,
                "attempted_at": now.isoformat(),
                "channel": message.channel,
                "status": message.status,
                "retryable": outcome.retryable,
                "status_code": outcome.status_code,
                "latency_ms": round(outcome.latency_ms, 3),
                "provider_message_id": _safe_provider_message_id(outcome.provider_message_id, message=message),
                "acceptance_evidence": outcome.acceptance_evidence,
                "delivery_confirmed": False,
                "error_code": safe_notification_error_code(outcome.error_code),
                "error": None if outcome.status == "accepted" else _safe_outcome_error(outcome),
                "external_provider_verified": False,
            }
        )
        if len(ledger) > MAX_DELIVERY_LEDGER_ENTRIES:
            ledger = ledger[-MAX_DELIVERY_LEDGER_ENTRIES:]
        attrs["delivery_ledger"] = ledger
        attrs["external_provider_verified"] = False
        attrs["delivery_confirmed"] = False
        attrs["acceptance_evidence"] = outcome.acceptance_evidence
        message.attributes = attrs


_DEFAULT_DISPATCHER = NotificationDispatcher()


async def dispatch_outbound_message(
    session: AsyncSession,
    message: OutboundMessage,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Deprecated compatibility guard: outbound messages dispatch only in the process worker."""
    raise RuntimeError("inline notification dispatch is disabled; use NotificationWorker.run_once()")


async def claim_pending_notifications(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    limit: int = 50,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Durably transition a bounded batch to `sending` before any worker performs network I/O."""
    from sqlalchemy import or_

    if not 1 <= limit <= MAX_PENDING_DELIVERY_BATCH:
        raise ValueError(f"limit must be between 1 and {MAX_PENDING_DELIVERY_BATCH}")
    now = now or utc_now()
    stale_before = now - STALE_SENDING_AFTER
    stale_stmt = select(OutboundMessage).where(
        OutboundMessage.status == "sending",
        OutboundMessage.updated_at <= stale_before,
    )
    if org_id:
        stale_stmt = stale_stmt.where(OutboundMessage.org_id == org_id)
    stale_rows = list(
        (
            await session.execute(
                stale_stmt.order_by(OutboundMessage.updated_at.asc(), OutboundMessage.id.asc())
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    reconciled: list[dict[str, Any]] = []
    for row in stale_rows:
        row.status = "reconciliation_required"
        row.next_attempt_at = None
        row.error = "dispatch outcome is unknown after an interrupted send; reconcile before retrying"
        attrs = dict(row.attributes or {})
        attrs["reconciliation_required_at"] = now.isoformat()
        attrs["reconciliation_reason"] = "stale_sending_status"
        row.attributes = attrs
        outcome = DeliveryAttemptOutcome(
            status="reconciliation_required",
            retryable=False,
            error_code="stale_sending_ambiguous",
            error=row.error,
            external_provider_verified=False,
        )
        _DEFAULT_DISPATCHER._append_ledger(row, now=now, outcome=outcome)
        reconciled.append(
            {
                "message_id": row.id,
                "status": row.status,
                "delivery_confirmed": False,
                "manual_reconciliation_required": True,
                "attempts": row.attempts,
            }
        )

    stmt = select(OutboundMessage).where(
        OutboundMessage.status.in_(("queued", "failed_retryable")),
        or_(OutboundMessage.next_attempt_at.is_(None), OutboundMessage.next_attempt_at <= now),
    )
    if org_id:
        stmt = stmt.where(OutboundMessage.org_id == org_id)
    candidates = list(
        (
            await session.execute(
                stmt.order_by(OutboundMessage.created_at.asc(), OutboundMessage.id.asc())
                .with_for_update(skip_locked=True)
                .limit(max(0, limit - len(stale_rows)))
            )
        )
        .scalars()
        .all()
    )
    claimed_ids: list[str] = []
    suppressed: list[dict[str, Any]] = []
    for row in candidates:
        duplicate = await _DEFAULT_DISPATCHER._suppress_accepted_sibling(session, row)
        if duplicate is not None:
            suppressed.append(duplicate)
            continue
        row.status = "sending"
        row.attempts = int(row.attempts or 0) + 1
        row.updated_at = now
        claimed_ids.append(row.id)
    await session.flush()
    return {"claimed_ids": claimed_ids, "reconciled": reconciled, "suppressed": suppressed}


async def _dispatch_claimed_notification(
    session: AsyncSession,
    message_id: str,
    *,
    now: dt.datetime | None = None,
    dispatcher: NotificationDispatcher | None = None,
) -> dict[str, Any]:
    """Perform transport I/O for a committed claim and persist its bounded outcome ledger."""
    row = (
        await session.execute(
            select(OutboundMessage).where(OutboundMessage.id == message_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        return {"message_id": message_id, "status": "missing", "delivery_confirmed": False}
    return await (dispatcher or _DEFAULT_DISPATCHER)._dispatch_claimed_message(session, row, now=now)


async def dispatch_pending_notifications(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    limit: int = 50,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Deprecated guard: dispatching inside a caller-owned transaction is unsafe.

    Use :class:`NotificationWorker` so the claim commits before transport I/O and each outcome is
    committed independently. This compatibility entry point deliberately performs no network I/O.
    """
    raise RuntimeError(
        "inline pending-notification dispatch is disabled; use NotificationWorker.run_once()"
    )
