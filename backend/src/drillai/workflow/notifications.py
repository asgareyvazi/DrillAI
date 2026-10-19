"""Outbound notification adapter boundary, dispatcher, idempotency guard, and delivery ledger.

Implements production-grade delivery for `OutboundMessage` rows queued by the `output.notify`
workflow node (or alerts):

* **Webhook / HTTP adapter (`channel="webhook"`)**:
  - Enforces the authoritative database-backed egress policy (`load_authoritative_egress_policy` +
    `validate_connector_endpoint_async`) so outbound webhooks cannot be abused for SSRF or private
    network scanning.
  - Resolves credentials exclusively from environment/vault secret references (`secret_ref`) without
    storing raw secrets in database JSON columns.
  - Signs payloads with `X-DrillAI-Signature-256: sha256=<hmac>` when a signing secret reference is
    provided, and attaches `Idempotency-Key` and `X-DrillAI-Message-Id` headers.
  - Enforces explicit request timeouts and classifies outcomes into `sent` (`2xx`),
    `failed_retryable` (`408`, `429`, `5xx`, network/timeout errors), `failed_terminal` (`4xx`
    client errors or egress policy blocks), and `dead_letter` (retry ceiling exhausted).

* **SMTP Email adapter (`channel="email"`)**:
  - Delivers RFC 5322 email messages over SMTP with explicit timeout, `Idempotency-Key` header,
    and transient (`4xx`) vs permanent (`5xx`) SMTP error classification.

* **In-App adapter (`channel="in_app"`)**:
  - Deterministic internal inbox delivery without external network I/O.

* **Evidence Boundary**:
  - Every delivery receipt records `external_provider_verified: False` unless a live third-party
    provider certification flag is explicitly configured, ensuring local test receivers are never
    conflated with live SaaS provider certification.
"""

from __future__ import annotations

import abc
import asyncio
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import smtplib
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.clock import UTC, utc_now
from drillai.core.errors import ValidationFailed
from drillai.db.models import OutboundMessage, SecretRef
from drillai.telemetry.connectors import (
    sync_egress_policy_from_db,
    validate_connector_endpoint_async,
)

__all__ = [
    "DELIVERY_STATUSES",
    "MAX_DELIVERY_LEDGER_ENTRIES",
    "DeliveryAttemptOutcome",
    "EmailSmtpNotificationAdapter",
    "InAppNotificationAdapter",
    "NotificationAdapter",
    "NotificationDispatcher",
    "WebhookNotificationAdapter",
    "dispatch_outbound_message",
    "dispatch_pending_notifications",
    "resolve_notification_secret",
    "resolve_notification_secret_async",
]

DELIVERY_STATUSES = (
    "queued",
    "sending",
    "sent",
    "failed_retryable",
    "failed_terminal",
    "dead_letter",
)
MAX_DELIVERY_LEDGER_ENTRIES = 20

_SECRET_REDACT_RE = re.compile(r"(Bearer\s+[A-Za-z0-9._\-+/=]+|password=[^\s&]+|secret=[^\s&]+)", re.IGNORECASE)


def _redact_error(text: str | None) -> str | None:
    if not text:
        return None
    return _SECRET_REDACT_RE.sub("[REDACTED]", str(text))[:500]


def resolve_notification_secret(secret_ref: str | None) -> str | None:
    """Resolve a secret reference from environment variables without storing raw secrets in DB rows."""
    if not secret_ref or not secret_ref.strip():
        return None
    key = secret_ref.strip()
    if key.startswith("env:"):
        env_name = key[4:].strip()
        return os.environ.get(env_name)
    env_prefixed = f"DRILLAI_NOTIFY_SECRET_{key.upper().replace('-', '_').replace('.', '_')}"
    return os.environ.get(env_prefixed) or os.environ.get(key)


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
    status: str  # "sent" | "failed_retryable" | "failed_terminal"
    retryable: bool
    provider_message_id: str | None = None
    status_code: int | None = None
    latency_ms: float = 0.0
    error_code: str | None = None
    error: str | None = None
    external_provider_verified: bool = False
    details: dict[str, Any] = field(default_factory=dict)


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

        # 1. Enforce authoritative egress policy & SSRF protection
        try:
            allow_loopback = os.environ.get("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "").strip().lower() in {
                "1",
                "true",
                "yes",
            }
            await sync_egress_policy_from_db(session)
            await validate_connector_endpoint_async(
                recipient_url,
                "witsml.1.4.1.1.soap_http",  # HTTP/HTTPS transport rules
                allow_loopback=allow_loopback,
            )
        except ValidationFailed as exc:
            details = exc.details or {}
            reason = str(details.get("reason") or "egress_policy_blocked")
            policy_state = str(details.get("policy_state") or "")
            is_retryable = reason == "dns_lookup_timeout" or policy_state == "transient_store_error"
            return DeliveryAttemptOutcome(
                status="failed_retryable" if is_retryable else "failed_terminal",
                retryable=is_retryable,
                error_code=reason,
                error=_redact_error(exc.message),
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        # 2. Construct canonical JSON body & headers (resolving secret_ref if present)
        cfg_payload = dict(message.payload or {})
        timeout_seconds = float(cfg_payload.get("timeout_seconds") or 10.0)
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
                error=f"secret_ref {secret_ref!r} could not be resolved",
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
            for hk, hv in extra_headers.items():
                if hk.lower() not in {"authorization", "x-drillai-signature-256"}:
                    headers[str(hk)] = str(hv)
        if secret_val:
            auth_scheme = str(cfg_payload.get("auth_scheme") or "hmac_sha256").lower()
            if auth_scheme == "bearer":
                headers["Authorization"] = f"Bearer {secret_val}"
            else:
                sig = hmac.new(secret_val.encode("utf-8"), encoded_body, hashlib.sha256).hexdigest()
                headers["X-DrillAI-Signature-256"] = f"sha256={sig}"

        # 3. Execute HTTP POST with explicit timeout and classify response
        try:
            async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
                response = await client.post(recipient_url, content=encoded_body, headers=headers)
        except httpx.TimeoutException as exc:
            return DeliveryAttemptOutcome(
                status="failed_retryable",
                retryable=True,
                error_code="webhook_timeout",
                error=_redact_error(f"webhook timed out after {timeout_seconds}s: {exc}"),
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except httpx.HTTPError as exc:
            return DeliveryAttemptOutcome(
                status="failed_retryable",
                retryable=True,
                error_code="webhook_network_error",
                error=_redact_error(f"webhook transport error: {exc}"),
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

        latency_ms = (time.perf_counter() - started) * 1000.0
        code = response.status_code
        if 200 <= code < 300:
            provider_msg_id = response.headers.get("X-Provider-Message-Id")
            if not provider_msg_id:
                try:
                    resp_json = response.json()
                    if isinstance(resp_json, dict) and resp_json.get("id"):
                        provider_msg_id = str(resp_json["id"])
                except Exception:
                    provider_msg_id = None
            return DeliveryAttemptOutcome(
                status="sent",
                retryable=False,
                provider_message_id=provider_msg_id or f"webhook:{message.id}:{code}",
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
                error=_redact_error(f"webhook returned transient HTTP {code}: {response.text[:200]}"),
                latency_ms=latency_ms,
            )

        return DeliveryAttemptOutcome(
            status="failed_terminal",
            retryable=False,
            status_code=code,
            error_code=f"http_{code}",
            error=_redact_error(f"webhook returned permanent HTTP {code}: {response.text[:200]}"),
            latency_ms=latency_ms,
        )


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
                error=f"invalid email recipient {recipient!r}",
            )
        cfg = dict(message.payload or {})
        smtp_url = str(cfg.get("smtp_url") or os.environ.get("DRILLAI_SMTP_URL") or "").strip()
        if not smtp_url:
            return DeliveryAttemptOutcome(
                status="failed_terminal",
                retryable=False,
                error_code="smtp_not_configured",
                error="no smtp_url or DRILLAI_SMTP_URL configured for email delivery",
            )
        parsed = urlparse(smtp_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 25
        timeout_seconds = float(cfg.get("timeout_seconds") or 10.0)
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
                error=f"secret_ref {secret_ref!r} could not be resolved",
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
            with smtplib.SMTP(host=host, port=port, timeout=timeout_seconds) as client:
                if parsed.username and password:
                    client.login(parsed.username, password)
                client.send_message(msg)
            return str(msg["Message-ID"])

        try:
            msg_id = await asyncio.to_thread(_send_sync)
            return DeliveryAttemptOutcome(
                status="sent",
                retryable=False,
                provider_message_id=msg_id,
                status_code=250,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                external_provider_verified=False,
            )
        except smtplib.SMTPResponseException as exc:
            code = int(exc.smtp_code)
            retryable = 400 <= code < 500
            return DeliveryAttemptOutcome(
                status="failed_retryable" if retryable else "failed_terminal",
                retryable=retryable,
                status_code=code,
                error_code=f"smtp_{code}",
                error=_redact_error(f"SMTP {code}: {exc.smtp_error}"),
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        except (TimeoutError, OSError, smtplib.SMTPException) as exc:
            return DeliveryAttemptOutcome(
                status="failed_retryable",
                retryable=True,
                error_code="smtp_transport_error",
                error=_redact_error(f"SMTP transport error: {exc}"),
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
            status="sent",
            retryable=False,
            provider_message_id=f"inapp:{message.id}",
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
        now = now or dt.datetime.now(tz=UTC)

        # 1. Idempotency check: if this message or a sibling with the same (org_id, idempotency_key)
        # has already been delivered (`status == "sent"`), never re-send over the wire.
        if message.status == "sent":
            return {
                "message_id": message.id,
                "status": "sent",
                "duplicate_suppressed": True,
                "provider_message_id": message.provider_message_id,
                "attempts": message.attempts,
            }
        if message.idempotency_key:
            existing_sent = (
                await session.execute(
                    select(OutboundMessage)
                    .where(
                        OutboundMessage.org_id == message.org_id,
                        OutboundMessage.idempotency_key == message.idempotency_key,
                        OutboundMessage.status == "sent",
                    )
                    .limit(1)
                )
            ).scalars().first()
            if existing_sent is not None:
                message.status = "sent"
                message.provider_message_id = existing_sent.provider_message_id
                message.sent_at = existing_sent.sent_at or now
                await session.flush()
                return {
                    "message_id": message.id,
                    "status": "sent",
                    "duplicate_suppressed": True,
                    "provider_message_id": message.provider_message_id,
                    "attempts": message.attempts,
                }

        adapter = self._adapters.get(message.channel)
        if adapter is None:
            message.attempts = int(message.attempts or 0) + 1
            message.status = "failed_terminal"
            message.error = f"no notification adapter registered for channel {message.channel!r}"
            self._append_ledger(
                message,
                now=now,
                outcome=DeliveryAttemptOutcome(
                    status="failed_terminal",
                    retryable=False,
                    error_code="unsupported_channel",
                    error=message.error,
                ),
            )
            await session.flush()
            return {
                "message_id": message.id,
                "status": message.status,
                "duplicate_suppressed": False,
                "provider_message_id": None,
                "attempts": message.attempts,
                "error": message.error,
            }

        message.status = "sending"
        message.attempts = int(message.attempts or 0) + 1
        await session.flush()

        outcome = await adapter.send(session, message)
        max_attempts = max(1, int(message.max_attempts or 3))

        if outcome.status == "sent":
            message.status = "sent"
            message.sent_at = now
            message.next_attempt_at = None
            message.error = None
            message.provider_message_id = outcome.provider_message_id
        elif outcome.retryable and message.attempts < max_attempts:
            message.status = "failed_retryable"
            backoff_seconds = min(300, (2 ** message.attempts) * 5)
            message.next_attempt_at = now + dt.timedelta(seconds=backoff_seconds)
            message.error = outcome.error
        elif outcome.retryable and message.attempts >= max_attempts:
            message.status = "dead_letter"
            message.next_attempt_at = None
            message.error = outcome.error
        else:
            message.status = "failed_terminal"
            message.next_attempt_at = None
            message.error = outcome.error

        self._append_ledger(message, now=now, outcome=outcome)
        await session.flush()
        return {
            "message_id": message.id,
            "status": message.status,
            "duplicate_suppressed": False,
            "provider_message_id": message.provider_message_id,
            "attempts": message.attempts,
            "max_attempts": max_attempts,
            "next_attempt_at": message.next_attempt_at.isoformat() if message.next_attempt_at else None,
            "error": message.error,
            "external_provider_verified": outcome.external_provider_verified,
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
                "provider_message_id": outcome.provider_message_id,
                "error_code": outcome.error_code,
                "error": outcome.error,
                "external_provider_verified": outcome.external_provider_verified,
            }
        )
        if len(ledger) > MAX_DELIVERY_LEDGER_ENTRIES:
            ledger = ledger[-MAX_DELIVERY_LEDGER_ENTRIES:]
        attrs["delivery_ledger"] = ledger
        attrs["external_provider_verified"] = outcome.external_provider_verified
        message.attributes = attrs


_DEFAULT_DISPATCHER = NotificationDispatcher()


async def dispatch_outbound_message(
    session: AsyncSession,
    message: OutboundMessage,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    return await _DEFAULT_DISPATCHER.dispatch_message(session, message, now=now)


async def dispatch_pending_notifications(
    session: AsyncSession,
    *,
    org_id: str | None = None,
    limit: int = 50,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or dt.datetime.now(tz=UTC)
    stmt = select(OutboundMessage).where(
        OutboundMessage.status.in_(("queued", "failed_retryable")),
    )
    if org_id:
        stmt = stmt.where(OutboundMessage.org_id == org_id)
    stmt = stmt.order_by(OutboundMessage.created_at.asc()).limit(limit)
    rows = list((await session.execute(stmt)).scalars().all())
    results: list[dict[str, Any]] = []
    for row in rows:
        if row.status == "failed_retryable" and row.next_attempt_at and row.next_attempt_at > now:
            continue
        results.append(await _DEFAULT_DISPATCHER.dispatch_message(session, row, now=now))
    return results
