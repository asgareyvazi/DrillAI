"""Production connector registry, configuration validation, secret redaction, SSRF guards,
and governed lifecycle transitions.

This module separates four things that must never be conflated:

1. **Operator intent** (``desired_state``: ``enabled``, ``stopped``, ``disabled``; ``is_enabled``) —
   whether the operator wants a collector to run;
2. **Observed runtime state** (``status``: ``created``, ``configured``, ``stopped``, ``starting``,
   ``running``, ``backing_off``, ``degraded``, ``failed``, ``disabled``) — what the worker last
   reported;
3. **Lease ownership & fencing** (``worker_id``, ``lease_expires_at``, ``last_heartbeat_at``,
   ``fencing_token``) — which worker currently owns the connector and is permitted to advance its
   checkpoint;
4. **Data freshness & quality** (``last_poll_at``, ``last_successful_poll_at``, ``last_frame_at``,
   ``last_ingest_at``, plus the target well's canonical telemetry freshness) — whether trustworthy
   measurements are actually arriving.

A stopped, disabled, starting, backing-off, lease-expired, or failed connector never reports
``is_live = True``. Credentials are referenced exclusively through ``secret_refs`` (environment
variables ``env:DRILLAI_SECRET_*`` or tenant-scoped :class:`SecretRef` rows) and are never stored
or returned in plaintext.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import os
import re
import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.core.audit import record_audit
from drillai.core.clock import utc_now
from drillai.core.config import get_settings
from drillai.core.errors import Conflict, NotFound, PermissionDenied, ValidationFailed
from drillai.core.ids import new_id
from drillai.db.models import (
    CONNECTOR_DESIRED_STATES,
    CONNECTOR_PROFILES,
    CONNECTOR_RUNTIME_STATUSES,
    Connector,
    ConnectorRun,
    Operation,
    SecretRef,
    TimeSeries,
    Well,
    Wellbore,
)
from drillai.security.actions import Principal
from drillai.security.rbac import permission_granted
from drillai.telemetry.adapters import (
    ChannelDescriptor,
    SourceAdapter,
    SyntheticAdapter,
    validate_descriptor,
)
from drillai.telemetry.outbox import emit
from drillai.telemetry.units import convert
from drillai.telemetry.vocabulary import CHANNEL_DIMENSIONS, TRUSTWORTHY_QUALITY

__all__ = [
    "ALLOWED_SECRET_SLOTS",
    "CONNECTOR_DESIRED_STATES",
    "CONNECTOR_PROFILES",
    "CONNECTOR_RUNTIME_STATUSES",
    "PROTOCOL_PROFILE_SPECS",
    "ChannelMappingSpec",
    "ConnectorService",
    "ConnectorTransportError",
    "PinnedEndpoint",
    "ProtocolPollDisposition",
    "ProtocolProfileSpec",
    "classify_connector_error",
    "clear_harness_endpoints",
    "connector_out",
    "connector_run_out",
    "enforce_connect_time_ssrf",
    "get_egress_policy",
    "is_harness_endpoint",
    "mask_secret_refs",
    "parse_channel_mappings",
    "parse_private_cidr_list",
    "redact_sensitive_text",
    "register_harness_endpoint",
    "reset_egress_policy",
    "resolve_and_pin_endpoint",
    "resolve_secret_refs",
    "set_custom_dns_resolver",
    "set_egress_policy",
    "unregister_harness_endpoint",
    "validate_connector_config",
    "validate_connector_endpoint",
    "validate_no_plaintext_secrets",
    "validate_secret_refs",
]

ALLOWED_SECRET_SLOTS: tuple[str, ...] = ("username", "password", "bearer_token")
ENV_SECRET_PATTERN = re.compile(r"^env:(DRILLAI_SECRET_[A-Z0-9_]{2,120})$")
SECRET_REF_PATTERN = re.compile(r"^(?:secret_ref|secret):([a-zA-Z0-9._:-]{2,160})$")
MASKED_SECRET_PLACEHOLDERS: frozenset[str] = frozenset(
    {"********", "env:********", "secret_ref:********", "secret:********"}
)
CONNECTOR_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{1,118}[a-z0-9]$")

_custom_dns_resolver: Callable[[str, int], Sequence[str]] | None = None
_HARNESS_OWNED_ENDPOINTS: set[tuple[str, int]] = set()
_runtime_allowed_hosts_override: list[str] | None = None
_runtime_allowed_private_cidrs_override: list[str] | None = None


def set_custom_dns_resolver(
    resolver: Callable[[str, int], Sequence[str]] | None,
) -> None:
    """Install or clear a custom DNS resolver function ``(hostname, port) -> list[ip_str]`` for testing."""
    global _custom_dns_resolver
    _custom_dns_resolver = resolver


def register_harness_endpoint(host: str, port: int) -> None:
    """Register a local non-production test harness (host, port) endpoint."""
    clean_host = (host or "127.0.0.1").strip().lower().strip("[]")
    if clean_host in {"localhost", "127.0.0.1", "::1"} and 1 <= int(port) <= 65535:
        _HARNESS_OWNED_ENDPOINTS.add(("127.0.0.1", int(port)))
        _HARNESS_OWNED_ENDPOINTS.add(("::1", int(port)))


def unregister_harness_endpoint(host: str, port: int) -> None:
    """Remove a previously registered local test harness endpoint."""
    _HARNESS_OWNED_ENDPOINTS.discard(("127.0.0.1", int(port)))
    _HARNESS_OWNED_ENDPOINTS.discard(("::1", int(port)))


def clear_harness_endpoints() -> None:
    """Clear all in-memory harness endpoints."""
    _HARNESS_OWNED_ENDPOINTS.clear()


def is_harness_endpoint(host_or_ip: str, port: int | None, *, is_production: bool | None = None) -> bool:
    """Return True only if (host_or_ip, port) is an active non-production local harness endpoint."""
    settings = get_settings()
    prod = settings.is_production if is_production is None else is_production
    if prod or port is None:
        return False
    clean = (host_or_ip or "").strip().lower().strip("[]")
    if clean == "localhost":
        clean = "127.0.0.1"
    if (clean, int(port)) in _HARNESS_OWNED_ENDPOINTS:
        return True
    return clean in {"127.0.0.1", "::1"} and int(port) in settings.connector_harness_port_list


def parse_private_cidr_list(
    raw_cidrs: Sequence[str] | None,
) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse and validate approved private rig network CIDRs, rejecting loopback/link-local/public/wildcard ranges."""
    if not raw_cidrs:
        return []
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw_cidrs:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            net = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ValidationFailed(
                f"invalid private network CIDR {text!r}",
                details={"field": "allowed_private_cidrs", "cidr": text, "reason": "invalid_cidr"},
            ) from exc
        if (
            net.prefixlen == 0
            or net.is_loopback
            or net.is_link_local
            or net.is_multicast
            or net.is_unspecified
            or not net.is_private
        ):
            raise ValidationFailed(
                f"CIDR {text!r} is not a scoped private network range (loopback, link-local, wildcard, and public ranges are forbidden)",
                details={"field": "allowed_private_cidrs", "cidr": text, "reason": "invalid_cidr"},
            )
        networks.append(net)
    return networks


def get_egress_policy() -> dict[str, Any]:
    """Return the active connector outbound network egress policy."""
    settings = get_settings()
    allowed_hosts = (
        list(_runtime_allowed_hosts_override)
        if _runtime_allowed_hosts_override is not None
        else list(settings.connector_allowed_host_list)
    )
    allowed_cidrs = (
        list(_runtime_allowed_private_cidrs_override)
        if _runtime_allowed_private_cidrs_override is not None
        else list(settings.connector_allowed_private_cidr_list)
    )
    harness_ports = sorted(
        {port for _, port in _HARNESS_OWNED_ENDPOINTS} | set(settings.connector_harness_port_list)
    )
    return {
        "is_production": settings.is_production,
        "allow_loopback": bool(settings.connector_allow_loopback and not settings.is_production),
        "allowed_hosts": allowed_hosts,
        "allowed_private_cidrs": allowed_cidrs,
        "harness_ports": [] if settings.is_production else harness_ports,
    }


def set_egress_policy(
    *,
    allowed_hosts: Sequence[str] | None = None,
    allowed_private_cidrs: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Update the runtime connector outbound egress policy after validating all hosts and CIDRs."""
    global _runtime_allowed_hosts_override, _runtime_allowed_private_cidrs_override
    if allowed_private_cidrs is not None:
        nets = parse_private_cidr_list(allowed_private_cidrs)
        _runtime_allowed_private_cidrs_override = [str(n) for n in nets]
    if allowed_hosts is not None:
        cleaned_hosts: list[str] = []
        for raw_h in allowed_hosts:
            h = str(raw_h or "").strip().lower().rstrip(".")
            if not h:
                continue
            if h in PROHIBITED_METADATA_HOSTS or h.endswith(".internal") or h in {"localhost", "127.0.0.1", "::1"}:
                raise ValidationFailed(
                    f"host {h!r} cannot be added to allowed_hosts",
                    details={"field": "allowed_hosts", "host": h, "reason": "ssrf_blocked"},
                )
            cleaned_hosts.append(h)
        _runtime_allowed_hosts_override = cleaned_hosts
    return get_egress_policy()


def reset_egress_policy() -> None:
    """Reset runtime egress policy overrides back to environment settings."""
    global _runtime_allowed_hosts_override, _runtime_allowed_private_cidrs_override
    _runtime_allowed_hosts_override = None
    _runtime_allowed_private_cidrs_override = None

FORBIDDEN_SECRET_KEY_PATTERNS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "access_token",
    "bearer_token",
    "private_key",
    "client_secret",
    "authorization",
)

PROHIBITED_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "169.254.169.254",
        "169.254.170.2",
        "100.100.100.200",
        "fd00:ec2::254",
        "metadata.google.internal",
        "metadata",
        "instance-data",
    }
)


@dataclass(frozen=True)
class ChannelMappingSpec:
    """Typed mapping between a source protocol mnemonic/URI and a platform channel."""

    source_mnemonic: str
    channel_key: str
    name: str
    dimension: str
    unit: str
    channel_uri: str = ""
    description: str | None = None
    is_realtime: bool = True


class ConnectorTransportError(Exception):
    """Structured protocol/transport failure with explicit retryability and category."""

    def __init__(self, category: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.retryable = retryable


@dataclass
class ProtocolPollDisposition:
    """Bounded, audit-safe accounting of every source record encountered during a protocol poll.

    Satisfies the exact invariant:
    ``source_records_discovered == mapped_frames_emitted + unmapped_skipped + unsubscribed_skipped
    + watermark_suppressed + in_batch_duplicates + deferred_over_cap + total_protocol_rejected``.
    """

    pages_or_messages_received: int = 0
    source_records_discovered: int = 0
    mapped_frames_emitted: int = 0
    unmapped_skipped: int = 0
    unsubscribed_skipped: int = 0
    watermark_suppressed: int = 0
    in_batch_duplicates: int = 0
    deferred_over_cap: int = 0
    rejected_malformed_timestamp: int = 0
    rejected_unsupported_index: int = 0
    rejected_invalid_value: int = 0
    rejected_non_finite_value: int = 0
    rejected_unknown_channel: int = 0
    truncated_by_limit: bool = False
    partial_more_available: bool = False
    protocol_rejections: list[dict[str, Any]] = field(default_factory=list)
    per_source_channel: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total_protocol_rejected(self) -> int:
        return (
            self.rejected_malformed_timestamp
            + self.rejected_unsupported_index
            + self.rejected_invalid_value
            + self.rejected_non_finite_value
            + self.rejected_unknown_channel
        )

    @property
    def total_filtered(self) -> int:
        return self.unmapped_skipped + self.unsubscribed_skipped

    @property
    def reconciled(self) -> bool:
        return self.source_records_discovered == (
            self.mapped_frames_emitted
            + self.unmapped_skipped
            + self.unsubscribed_skipped
            + self.watermark_suppressed
            + self.in_batch_duplicates
            + self.deferred_over_cap
            + self.total_protocol_rejected
        )

    def record_channel_outcome(self, source_mnemonic: str, bucket: str, count: int = 1) -> None:
        key = (source_mnemonic or "<unknown>").strip().upper() or "<UNKNOWN>"
        if len(self.per_source_channel) >= 100 and key not in self.per_source_channel:
            key = "<OVERFLOW>"
        entry = self.per_source_channel.setdefault(
            key,
            {
                "discovered": 0,
                "emitted": 0,
                "unmapped_skipped": 0,
                "unsubscribed_skipped": 0,
                "watermark_suppressed": 0,
                "in_batch_duplicates": 0,
                "deferred_over_cap": 0,
                "rejected": 0,
            },
        )
        entry["discovered"] = entry.get("discovered", 0) + count
        entry[bucket] = entry.get(bucket, 0) + count

    def record_rejection(
        self,
        *,
        reason: str,
        source_mnemonic: str = "",
        index_repr: str | None = None,
        raw_value: str | None = None,
    ) -> None:
        if len(self.protocol_rejections) < 50:
            self.protocol_rejections.append(
                {
                    "reason": reason,
                    "source_mnemonic": source_mnemonic[:80] if source_mnemonic else None,
                    "index": str(index_repr)[:80] if index_repr is not None else None,
                    "raw_value": str(raw_value)[:80] if raw_value is not None else None,
                }
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pages_or_messages_received": self.pages_or_messages_received,
            "source_records_discovered": self.source_records_discovered,
            "mapped_frames_emitted": self.mapped_frames_emitted,
            "unmapped_skipped": self.unmapped_skipped,
            "unsubscribed_skipped": self.unsubscribed_skipped,
            "watermark_suppressed": self.watermark_suppressed,
            "in_batch_duplicates": self.in_batch_duplicates,
            "deferred_over_cap": self.deferred_over_cap,
            "rejected_malformed_timestamp": self.rejected_malformed_timestamp,
            "rejected_unsupported_index": self.rejected_unsupported_index,
            "rejected_invalid_value": self.rejected_invalid_value,
            "rejected_non_finite_value": self.rejected_non_finite_value,
            "rejected_unknown_channel": self.rejected_unknown_channel,
            "total_protocol_rejected": self.total_protocol_rejected,
            "total_filtered": self.total_filtered,
            "truncated_by_limit": self.truncated_by_limit,
            "partial_more_available": self.partial_more_available,
            "reconciled": self.reconciled,
            "protocol_rejections": list(self.protocol_rejections[:50]),
            "per_source_channel": dict(sorted(self.per_source_channel.items())),
        }


def parse_channel_mappings(raw_mappings: Any) -> list[ChannelMappingSpec]:
    """Fail-closed parser and validator for channel mapping definitions.

    Never silently drops non-object, incomplete, duplicate, or invalid mapping entries.
    """
    if raw_mappings is None:
        return []
    if not isinstance(raw_mappings, list | tuple):
        raise ValidationFailed(
            "channel_mappings must be a list of channel mapping objects",
            details={"field": "config.channel_mappings", "reason": "invalid_channel_mappings"},
        )
    if len(raw_mappings) > 100:
        raise ValidationFailed(
            "channel_mappings cannot exceed 100 channels per connector",
            details={
                "field": "config.channel_mappings",
                "count": len(raw_mappings),
                "reason": "invalid_channel_mappings",
            },
        )
    specs: list[ChannelMappingSpec] = []
    seen_source: set[str] = set()
    seen_target: set[str] = set()
    for idx, item in enumerate(raw_mappings):
        if not isinstance(item, dict):
            raise ValidationFailed(
                f"channel_mappings[{idx}] must be an object",
                details={
                    "field": f"config.channel_mappings[{idx}]",
                    "reason": "invalid_channel_mappings",
                },
            )
        src = str(item.get("source_mnemonic") or item.get("channel_key") or "").strip()
        key = str(item.get("channel_key") or src).strip().lower()
        name = str(item.get("name") or key.upper()).strip()
        dim = str(item.get("dimension") or "").strip().lower()
        unit = str(item.get("unit") or "").strip()
        if not src or not key or not dim or not unit:
            raise ValidationFailed(
                f"channel_mappings[{idx}] requires non-empty source_mnemonic, channel_key, dimension, and unit",
                details={
                    "field": f"config.channel_mappings[{idx}]",
                    "reason": "invalid_channel_mappings",
                },
            )
        if len(src) > 120 or len(key) > 120 or len(name) > 200:
            raise ValidationFailed(
                f"channel_mappings[{idx}] identifier exceeds maximum length",
                details={
                    "field": f"config.channel_mappings[{idx}]",
                    "reason": "invalid_channel_mappings",
                },
            )
        if dim not in CHANNEL_DIMENSIONS:
            raise ValidationFailed(
                f"channel_mappings[{idx}] dimension {dim!r} is not in CHANNEL_DIMENSIONS",
                details={
                    "field": f"config.channel_mappings[{idx}].dimension",
                    "value": dim,
                    "allowed": list(CHANNEL_DIMENSIONS),
                    "reason": "invalid_channel_mappings",
                },
            )
        convert(0.0, unit, dim)
        descriptor = ChannelDescriptor(
            channel_key=key,
            name=name,
            dimension=dim,
            unit=unit,
            description=item.get("description"),
            is_realtime=bool(item.get("is_realtime", True)),
        )
        validate_descriptor(descriptor)
        src_norm = src.lower()
        if src_norm in seen_source:
            raise ValidationFailed(
                f"duplicate source_mnemonic {src!r} in channel_mappings",
                details={
                    "field": f"config.channel_mappings[{idx}].source_mnemonic",
                    "reason": "duplicate_channel_mapping",
                },
            )
        if key in seen_target:
            raise ValidationFailed(
                f"duplicate target channel_key {key!r} in channel_mappings",
                details={
                    "field": f"config.channel_mappings[{idx}].channel_key",
                    "reason": "duplicate_channel_mapping",
                },
            )
        seen_source.add(src_norm)
        seen_target.add(key)
        raw_uri = str(item.get("channel_uri") or f"eml://witsml/logChannel/{src}").strip()
        specs.append(
            ChannelMappingSpec(
                source_mnemonic=src,
                channel_key=key,
                name=name,
                dimension=dim,
                unit=unit,
                channel_uri=raw_uri[:300],
                description=item.get("description"),
                is_realtime=bool(item.get("is_realtime", True)),
            )
        )
    return specs


@dataclass(frozen=True)
class ProtocolProfileSpec:
    """Explicit capability and verification declaration for a supported connector profile."""

    profile: str
    provider: str
    standard_version: str
    transport: str
    auth_modes: tuple[str, ...]
    supported_operations: tuple[str, ...]
    supported_objects: tuple[str, ...]
    resume_mechanism: str
    is_synthetic: bool
    local_harness_verified: bool
    external_vendor_verified: bool
    limitations: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "provider": self.provider,
            "standard_version": self.standard_version,
            "transport": self.transport,
            "auth_modes": list(self.auth_modes),
            "supported_operations": list(self.supported_operations),
            "supported_objects": list(self.supported_objects),
            "resume_mechanism": self.resume_mechanism,
            "is_synthetic": self.is_synthetic,
            "local_harness_verified": self.local_harness_verified,
            "external_vendor_verified": self.external_vendor_verified,
            "limitations": self.limitations,
        }


PROTOCOL_PROFILE_SPECS: dict[str, ProtocolProfileSpec] = {
    "synthetic.v1": ProtocolProfileSpec(
        profile="synthetic.v1",
        provider="synthetic",
        standard_version="1.0",
        transport="in_process",
        auth_modes=("none",),
        supported_operations=("describe_channels", "subscribe", "poll"),
        supported_objects=("plan_series",),
        resume_mechanism="plan_offset_index",
        is_synthetic=True,
        local_harness_verified=True,
        external_vendor_verified=False,
        limitations=(
            "Deterministic synthetic plan source for commissioning and automated tests; "
            "never connects to an external rig or vendor system."
        ),
    ),
    "witsml.1.4.1.1.soap_http": ProtocolProfileSpec(
        profile="witsml.1.4.1.1.soap_http",
        provider="witsml",
        standard_version="1.4.1.1",
        transport="soap_http",
        auth_modes=("none", "basic", "bearer"),
        supported_operations=("WMLS_GetVersion", "WMLS_GetCap", "WMLS_GetFromStore"),
        supported_objects=("log",),
        resume_mechanism="startDateTimeIndex_inclusive_watermark",
        is_synthetic=False,
        local_harness_verified=True,
        external_vendor_verified=False,
        limitations=(
            "Read-only SOAP 1.1 WMLS_GetFromStore time-indexed log polling subset (plus WMLS_GetVersion and "
            "WMLS_GetCap). Inclusive startDateTimeIndex watermark with per-curve cursor completion tracking. "
            "Depth-indexed logs, WMLS_AddToStore/UpdateInStore, WITSML 2.1 ETP Store objects, and live external "
            "commercial vendor endpoints are not certified in CI."
        ),
    ),
    "etp.1.2.json_ws": ProtocolProfileSpec(
        profile="etp.1.2.json_ws",
        provider="etp",
        standard_version="1.2",
        transport="websocket_json",
        auth_modes=("none", "basic", "bearer"),
        supported_operations=(
            "RequestSession",
            "OpenSession",
            "StartStreaming",
            "Start",
            "ChannelDescribe",
            "ChannelMetadata",
            "ChannelStreamingStart",
            "ChannelData",
            "StopStreaming",
            "ChannelStreamingStop",
            "Ping",
            "Pong",
            "ProtocolException",
            "Acknowledge",
            "CloseSession",
        ),
        supported_objects=("channel_streaming",),
        resume_mechanism="per_channel_index_watermark",
        is_synthetic=False,
        local_harness_verified=True,
        external_vendor_verified=False,
        limitations=(
            "ETP v1.2 / v1.1 JSON-framed WebSocket ChannelStreaming subset (Protocol 0 Core + Protocol 1 "
            "ChannelStreaming, supporting both header/body Avro-JSON envelopes and flat JSON envelopes). "
            "Per-channel index watermarks with explicit DateTime unit semantics. Binary Avro encoding, "
            "Protocol 3 Discovery, Protocol 4 Store, Protocol 21 ChannelSubscribe, and live external "
            "commercial vendor endpoints are not certified in CI."
        ),
    ),
}


def redact_sensitive_text(text: str | None, secrets: dict[str, str] | None = None) -> str | None:
    """Scrub resolved secrets, Authorization/API-Key headers, URL query tokens, and internal env names."""
    if text is None:
        return None
    cleaned = str(text)
    if secrets:
        for secret_val in secrets.values():
            if secret_val and len(secret_val) >= 3:
                cleaned = cleaned.replace(secret_val, "********")
    cleaned = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s,;\"']+",
        r"\1********",
        cleaned,
    )
    cleaned = re.sub(
        r"(?i)(x-api-key\s*[:=]\s*)[^\s,;\"']+",
        r"\1********",
        cleaned,
    )
    cleaned = re.sub(
        r"(?i)(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}",
        "********",
        cleaned,
    )
    cleaned = re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:)([^@\s/]+)(@)",
        r"\1********\3",
        cleaned,
    )
    cleaned = re.sub(
        r"(?i)([?&](?:token|access_token|auth_token|api_key|apikey|password|passwd|secret|sig|signature)=)[^&\s#\"']+",
        r"\1********",
        cleaned,
    )
    cleaned = re.sub(
        r"\b(?:env:)?DRILLAI_SECRET_[A-Z0-9_]+\b",
        "env:********",
        cleaned,
    )
    return cleaned[:1000]


def classify_connector_error(exc: Exception) -> str:
    """Map an exception into a bounded operational error category."""
    if isinstance(exc, ConnectorTransportError):
        return exc.category
    if isinstance(exc, ValidationFailed):
        return str(exc.details.get("reason") or "validation_error")
    if isinstance(exc, PermissionDenied):
        return "permission_denied"
    msg = str(exc).lower()
    if "401" in msg or "403" in msg or "unauthorized" in msg or "authentication" in msg or "auth" in msg:
        return "authentication_failed"
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return "rate_limited"
    if "certificate" in msg or "ssl" in msg or "tls" in msg:
        return "tls_verification_failed"
    if "timeout" in msg or "timed out" in msg or isinstance(exc, TimeoutError):
        return "timeout"
    if "name or service not known" in msg or "nodename nor servname" in msg or "dns" in msg:
        return "dns_failure"
    if "connection refused" in msg or "connecterror" in msg or "cannot connect" in msg:
        return "connection_refused"
    if "version" in msg and ("mismatch" in msg or "unsupported" in msg):
        return "protocol_version_mismatch"
    if "xml" in msg or "soap" in msg or "etp" in msg or "malformed" in msg or "protocol" in msg:
        return "protocol_error"
    if "oversized" in msg or "too large" in msg or "max_response_bytes" in msg:
        return "response_too_large"
    return "transport_error"


def validate_no_plaintext_secrets(payload: Any, *, path: str = "config") -> None:
    """Reject any configuration dictionary or value containing plaintext credentials."""
    if isinstance(payload, dict):
        for raw_key, value in payload.items():
            key_lower = str(raw_key).strip().lower()
            field_path = f"{path}.{raw_key}"
            if any(token in key_lower for token in FORBIDDEN_SECRET_KEY_PATTERNS):
                raise ValidationFailed(
                    f"plaintext credential field {field_path!r} is forbidden; use secret_refs",
                    details={"field": field_path, "reason": "plaintext_secret_forbidden"},
                )
            validate_no_plaintext_secrets(value, path=field_path)
    elif isinstance(payload, list | tuple):
        for idx, item in enumerate(payload):
            validate_no_plaintext_secrets(item, path=f"{path}[{idx}]")
    elif isinstance(payload, str) and re.search(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^/\s:@]+:[^@\s/]+@", payload):
        raise ValidationFailed(
            f"embedded URL credentials in {path!r} are forbidden; use secret_refs",
            details={"field": path, "reason": "plaintext_secret_forbidden"},
        )


def validate_secret_refs(
    secret_refs: dict[str, Any] | None,
    *,
    allow_masked_placeholders: bool = False,
) -> dict[str, str]:
    """Validate that secret_refs only maps allowed credential slots to env:, secret_ref:, or secret: locators."""
    if not secret_refs:
        return {}
    if not isinstance(secret_refs, dict):
        raise ValidationFailed(
            "secret_refs must be an object mapping credential slots to secret references",
            details={"field": "secret_refs", "reason": "invalid_secret_ref"},
        )
    normalized: dict[str, str] = {}
    for slot, locator in secret_refs.items():
        slot_name = str(slot).strip().lower()
        if slot_name not in ALLOWED_SECRET_SLOTS:
            raise ValidationFailed(
                f"unsupported credential slot {slot!r}",
                details={
                    "field": f"secret_refs.{slot}",
                    "allowed_slots": list(ALLOWED_SECRET_SLOTS),
                    "reason": "invalid_secret_ref",
                },
            )
        if locator is None or locator == "":
            continue
        if not isinstance(locator, str):
            raise ValidationFailed(
                f"secret reference for {slot_name!r} must be a locator string",
                details={"field": f"secret_refs.{slot_name}", "reason": "invalid_secret_ref"},
            )
        ref = locator.strip()
        if allow_masked_placeholders and ref in MASKED_SECRET_PLACEHOLDERS:
            continue
        env_match = ENV_SECRET_PATTERN.match(ref)
        ref_match = SECRET_REF_PATTERN.match(ref)
        if not (env_match or ref_match):
            raise ValidationFailed(
                f"secret reference for {slot_name!r} must use 'env:DRILLAI_SECRET_<NAME>', 'secret_ref:<key>', or 'secret:<key>'",
                details={
                    "field": f"secret_refs.{slot_name}",
                    "reason": "invalid_secret_ref",
                },
            )
        if ref_match and ref.startswith("secret:"):
            ref = f"secret_ref:{ref_match.group(1)}"
        normalized[slot_name] = ref
    return normalized


def mask_secret_refs(secret_refs: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Return a minimized, masked representation of configured secret references.

    Never exposes raw internal environment variable names or secret locator keys in API responses,
    UI payloads, or audit records.
    """
    if not secret_refs or not isinstance(secret_refs, dict):
        return {}
    masked: dict[str, dict[str, Any]] = {}
    for slot, locator in sorted(secret_refs.items()):
        if not locator:
            continue
        ref_str = str(locator)
        backend = "env" if ref_str.startswith("env:") else "secret_ref"
        masked[str(slot)] = {
            "slot": str(slot),
            "ref": f"{backend}:********",
            "backend": backend,
            "configured": True,
            "redacted": True,
            "masked_value": "********",
        }
    return masked


async def resolve_secret_refs(
    session: AsyncSession,
    org_id: str,
    secret_refs: dict[str, str] | None,
) -> dict[str, str]:
    """Resolve configured secret references at runtime without ever persisting or logging values or env names."""
    if not secret_refs:
        return {}
    resolved: dict[str, str] = {}
    for slot, ref in secret_refs.items():
        env_match = ENV_SECRET_PATTERN.match(ref)
        if env_match:
            env_name = env_match.group(1)
            val = os.environ.get(env_name, "")
            if not val:
                raise ValidationFailed(
                    f"configured environment secret for slot {slot!r} is not set",
                    details={
                        "field": f"secret_refs.{slot}",
                        "slot": slot,
                        "backend": "env",
                        "reason": "secret_not_found",
                    },
                )
            resolved[slot] = val
            continue

        ref_match = SECRET_REF_PATTERN.match(ref)
        if ref_match:
            key = ref_match.group(1)
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
                raise ValidationFailed(
                    f"configured secret_ref for slot {slot!r} was not found or is inactive in this organization",
                    details={
                        "field": f"secret_refs.{slot}",
                        "slot": slot,
                        "backend": "secret_ref",
                        "reason": "secret_not_found",
                    },
                )
            row.last_used_at = utc_now()
            if row.backend == "env":
                env_name = row.locator.removeprefix("env:").strip()
                val = os.environ.get(env_name, "")
                if not val:
                    raise ValidationFailed(
                        f"backing environment secret for slot {slot!r} is not set",
                        details={
                            "field": f"secret_refs.{slot}",
                            "slot": slot,
                            "backend": "secret_ref",
                            "reason": "secret_not_found",
                        },
                    )
                resolved[slot] = val
            else:
                raise ValidationFailed(
                    f"SecretRef backend {row.backend!r} is not supported",
                    details={"field": f"secret_refs.{slot}", "reason": "secret_backend_unsupported"},
                )
            continue

        raise ValidationFailed(
            f"invalid secret reference for slot {slot!r}",
            details={"field": f"secret_refs.{slot}", "reason": "invalid_secret_ref"},
        )
    return resolved


_AMBIGUOUS_NUMERIC_HOST_RE = re.compile(
    r"^(?:0x[0-9a-fA-F]+|\d+)(?:\.(?:0x[0-9a-fA-F]+|\d+))*$"
)


def _unpack_embedded_ip(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return the IP and any embedded IPv4 addresses (IPv4-mapped IPv6, 6to4, Teredo)."""
    out: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            out.append(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            out.append(ip.sixtofour)
        if ip.teredo is not None:
            out.extend(ip.teredo)
    return out


@dataclass(frozen=True)
class PinnedEndpoint:
    """Validated, DNS-resolved, socket-pinned outbound endpoint descriptor."""

    original_url: str
    scheme: str
    hostname: str
    port: int
    pinned_ip: str
    all_resolved_ips: tuple[str, ...]
    host_header: str
    sni_hostname: str
    connect_url: str
    is_harness: bool = False
    is_private_rig: bool = False


def resolve_and_pin_endpoint(
    endpoint_url: str | None,
    protocol_profile: str,
    *,
    allow_loopback: bool | None = None,
    allowed_hosts: Sequence[str] | None = None,
    allowed_private_cidrs: Sequence[str] | None = None,
    is_production: bool | None = None,
    resolver: Callable[[str, int], Sequence[str]] | None = None,
    require_dns_resolution: bool = True,
) -> PinnedEndpoint:
    """Validate URL, resolve all A/AAAA records, enforce egress/loopback/private-CIDR policy, and pin socket IP."""
    if protocol_profile not in PROTOCOL_PROFILE_SPECS:
        raise ValidationFailed(
            f"unsupported protocol profile {protocol_profile!r}",
            details={
                "field": "protocol_profile",
                "value": protocol_profile,
                "allowed": list(PROTOCOL_PROFILE_SPECS),
            },
        )
    settings = get_settings()
    policy = get_egress_policy()
    if is_production is None:
        is_production = settings.is_production
    if allow_loopback is None:
        allow_loopback = settings.connector_allow_loopback
    if allowed_hosts is None:
        allowed_hosts = policy["allowed_hosts"]
    if allowed_private_cidrs is None:
        allowed_private_cidrs = policy["allowed_private_cidrs"]

    spec = PROTOCOL_PROFILE_SPECS[protocol_profile]
    if is_production and not getattr(spec, "production_supported", True):
        raise ValidationFailed(
            f"protocol profile {protocol_profile!r} is an internal/test-only profile and is prohibited in production; use 'etp.1.2.binary_ws'",
            details={
                "field": "protocol_profile",
                "value": protocol_profile,
                "reason": "profile_not_production_approved",
            },
        )

    if protocol_profile == "synthetic.v1":
        if endpoint_url is None or endpoint_url.strip() in ("", "synthetic://plan"):
            return PinnedEndpoint(
                original_url="synthetic://plan",
                scheme="synthetic",
                hostname="plan",
                port=0,
                pinned_ip="0.0.0.0",
                all_resolved_ips=(),
                host_header="plan",
                sni_hostname="plan",
                connect_url="synthetic://plan",
                is_harness=True,
            )
        raise ValidationFailed(
            "synthetic.v1 connectors do not accept an external network endpoint_url",
            details={"field": "endpoint_url", "value": endpoint_url, "reason": "invalid_endpoint"},
        )

    if not endpoint_url or not isinstance(endpoint_url, str) or not endpoint_url.strip():
        raise ValidationFailed(
            f"protocol profile {protocol_profile!r} requires a non-empty endpoint_url",
            details={"field": "endpoint_url", "reason": "endpoint_required"},
        )
    cleaned = endpoint_url.strip()
    if len(cleaned) > 500:
        raise ValidationFailed(
            "endpoint_url exceeds maximum length of 500 characters",
            details={"field": "endpoint_url", "reason": "endpoint_too_long"},
        )
    parsed = urlparse(cleaned)
    if parsed.username or parsed.password:
        raise ValidationFailed(
            "endpoint_url must not contain embedded credentials; use secret_refs",
            details={"field": "endpoint_url", "reason": "plaintext_secret_forbidden"},
        )

    scheme = (parsed.scheme or "").lower()
    if protocol_profile == "witsml.1.4.1.1.soap_http":
        allowed_schemes = ("https",) if is_production else ("https", "http")
    elif protocol_profile in ("etp.1.2.json_ws", "etp.1.2.binary_ws"):
        allowed_schemes = ("wss",) if is_production else ("wss", "ws")
    else:
        allowed_schemes = ()

    if scheme not in allowed_schemes:
        raise ValidationFailed(
            f"endpoint_url scheme {scheme!r} is not permitted for {protocol_profile!r}",
            details={
                "field": "endpoint_url",
                "scheme": scheme,
                "allowed_schemes": list(allowed_schemes),
                "reason": "invalid_scheme",
            },
        )

    hostname = (parsed.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise ValidationFailed(
            "endpoint_url has no valid hostname",
            details={"field": "endpoint_url", "reason": "invalid_hostname"},
        )

    if hostname in PROHIBITED_METADATA_HOSTS or hostname.endswith(".internal"):
        raise ValidationFailed(
            f"endpoint_url host {hostname!r} is a prohibited metadata or internal destination",
            details={"field": "endpoint_url", "host": hostname, "reason": "ssrf_blocked"},
        )

    if _AMBIGUOUS_NUMERIC_HOST_RE.match(hostname):
        try:
            parsed_lit = ipaddress.ip_address(hostname)
            if str(parsed_lit).lower() != hostname:
                raise ValidationFailed(
                    f"non-canonical IP address literal {hostname!r} is prohibited by SSRF policy",
                    details={"field": "endpoint_url", "host": hostname, "reason": "ssrf_blocked"},
                )
        except ValueError as exc:
            raise ValidationFailed(
                f"ambiguous numeric IP literal {hostname!r} is prohibited by SSRF policy",
                details={"field": "endpoint_url", "host": hostname, "reason": "ssrf_blocked"},
            ) from exc

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationFailed(
            "endpoint_url has an invalid port",
            details={"field": "endpoint_url", "reason": "invalid_port"},
        ) from exc
    if port is not None and (port < 1 or port > 65535):
        raise ValidationFailed(
            "endpoint_url port must be between 1 and 65535",
            details={"field": "endpoint_url", "port": port, "reason": "invalid_port"},
        )
    default_port = 443 if scheme in ("https", "wss") else 80
    effective_port = int(port or default_port)

    normalized_allowlist = {h.strip().lower().rstrip(".") for h in allowed_hosts if h and h.strip()}
    private_networks = parse_private_cidr_list(allowed_private_cidrs)
    active_resolver = resolver or _custom_dns_resolver

    is_ip_literal = False
    primary_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    candidate_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    try:
        lit_ip = ipaddress.ip_address(hostname)
        is_ip_literal = True
        primary_ips.append(lit_ip)
        candidate_ips.extend(_unpack_embedded_ip(lit_ip))
    except ValueError:
        if hostname in {"localhost", "localhost.localdomain"} and (
            is_production
            or not (
                allow_loopback
                or is_harness_endpoint("127.0.0.1", effective_port, is_production=is_production)
            )
        ):
            raise ValidationFailed(
                "loopback endpoint host 'localhost' is prohibited by SSRF policy",
                details={"field": "endpoint_url", "host": hostname, "reason": "ssrf_blocked"},
            ) from None
        if active_resolver is not None:
            try:
                resolved_strs = active_resolver(hostname, effective_port)
            except TimeoutError as exc:
                raise ValidationFailed(
                    f"DNS resolution timed out for {hostname!r}",
                    details={"field": "endpoint_url", "host": hostname, "reason": "timeout"},
                ) from exc
            except Exception as exc:
                raise ValidationFailed(
                    f"endpoint_url host {hostname!r} could not be resolved: {exc}",
                    details={"field": "endpoint_url", "host": hostname, "reason": "dns_failure"},
                ) from exc
            if not resolved_strs:
                raise ValidationFailed(
                    f"endpoint_url host {hostname!r} could not be resolved",
                    details={"field": "endpoint_url", "host": hostname, "reason": "dns_failure"},
                ) from None
            for ip_s in resolved_strs:
                try:
                    r_ip = ipaddress.ip_address(str(ip_s).strip())
                except ValueError as exc:
                    raise ValidationFailed(
                        f"DNS resolver returned invalid IP {ip_s!r} for {hostname!r}",
                        details={"field": "endpoint_url", "host": hostname, "reason": "ssrf_blocked"},
                    ) from exc
                primary_ips.append(r_ip)
                candidate_ips.extend(_unpack_embedded_ip(r_ip))
        else:
            try:
                infos = socket.getaddrinfo(hostname, effective_port, type=socket.SOCK_STREAM)
                for info in infos:
                    sockaddr = info[4]
                    if sockaddr and sockaddr[0]:
                        raw_addr = str(sockaddr[0]).split("%", 1)[0]
                        r_ip = ipaddress.ip_address(raw_addr)
                        if r_ip not in primary_ips:
                            primary_ips.append(r_ip)
                        candidate_ips.extend(_unpack_embedded_ip(r_ip))
            except socket.gaierror as exc:
                if require_dns_resolution:
                    raise ValidationFailed(
                        f"endpoint_url host {hostname!r} could not be resolved",
                        details={"field": "endpoint_url", "host": hostname, "reason": "dns_failure"},
                    ) from exc

    is_harness = False
    is_private_rig = False
    for ip in candidate_ips:
        ip_text = str(ip).lower()
        if ip_text in PROHIBITED_METADATA_HOSTS or (ip.is_link_local and not ip.is_loopback):
            raise ValidationFailed(
                f"endpoint_url destination {ip_text!r} is a prohibited link-local/metadata address",
                details={"field": "endpoint_url", "ip": ip_text, "reason": "ssrf_blocked"},
            )
        if ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise ValidationFailed(
                f"endpoint_url destination {ip_text!r} is not a routable unicast address",
                details={"field": "endpoint_url", "ip": ip_text, "reason": "ssrf_blocked"},
            )
        if ip.is_loopback:
            if is_production:
                raise ValidationFailed(
                    f"loopback destination {ip_text!r} is prohibited in production",
                    details={"field": "endpoint_url", "ip": ip_text, "reason": "ssrf_blocked"},
                )
            harness_ok = is_harness_endpoint(ip_text, effective_port, is_production=is_production)
            if harness_ok:
                is_harness = True
            elif not allow_loopback:
                raise ValidationFailed(
                    f"loopback destination {ip_text}:{effective_port} is prohibited by SSRF policy (not an approved harness endpoint)",
                    details={
                        "field": "endpoint_url",
                        "ip": ip_text,
                        "port": effective_port,
                        "reason": "ssrf_blocked",
                    },
                )
        elif ip.is_private:
            in_approved_cidr = any(ip in net for net in private_networks)
            host_or_ip_allowlisted = (
                hostname in normalized_allowlist
                or f"{hostname}:{effective_port}" in normalized_allowlist
                or ip_text in normalized_allowlist
                or f"{ip_text}:{effective_port}" in normalized_allowlist
            )
            if in_approved_cidr and (host_or_ip_allowlisted or is_ip_literal):
                is_private_rig = True
            else:
                raise ValidationFailed(
                    f"private network destination {ip_text!r} is prohibited unless within an approved private rig network CIDR and allowlist",
                    details={
                        "field": "endpoint_url",
                        "host": hostname,
                        "ip": ip_text,
                        "reason": "ssrf_blocked",
                    },
                )

    pinned_ip = str(primary_ips[0]) if primary_ips else hostname
    ip_host = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    netloc = f"{ip_host}:{port}" if port is not None else ip_host
    connect_url = parsed._replace(netloc=netloc).geturl()
    host_header = (
        f"[{hostname}]:{port}"
        if (":" in hostname and port is not None)
        else (f"{hostname}:{port}" if port is not None else hostname)
    )

    return PinnedEndpoint(
        original_url=cleaned,
        scheme=scheme,
        hostname=hostname,
        port=effective_port,
        pinned_ip=pinned_ip,
        all_resolved_ips=tuple(str(ip) for ip in primary_ips),
        host_header=host_header,
        sni_hostname=hostname,
        connect_url=connect_url,
        is_harness=is_harness,
        is_private_rig=is_private_rig,
    )


def validate_connector_endpoint(
    endpoint_url: str | None,
    protocol_profile: str,
    *,
    allow_loopback: bool | None = None,
    allowed_hosts: list[str] | tuple[str, ...] | None = None,
    allowed_private_cidrs: Sequence[str] | None = None,
    is_production: bool | None = None,
    resolver: Callable[[str, int], Sequence[str]] | None = None,
) -> str | None:
    """Validate protocol, scheme, port, hostname, and SSRF/DNS-rebinding constraints."""
    pinned = resolve_and_pin_endpoint(
        endpoint_url,
        protocol_profile,
        allow_loopback=allow_loopback,
        allowed_hosts=allowed_hosts,
        allowed_private_cidrs=allowed_private_cidrs,
        is_production=is_production,
        resolver=resolver,
        require_dns_resolution=False,
    )
    return pinned.original_url


def enforce_connect_time_ssrf(
    endpoint_url: str,
    protocol_profile: str,
    *,
    allow_loopback: bool | None = None,
    allowed_hosts: Sequence[str] | None = None,
    allowed_private_cidrs: Sequence[str] | None = None,
    is_production: bool | None = None,
    resolver: Callable[[str, int], Sequence[str]] | None = None,
) -> PinnedEndpoint:
    """Enforce connection-bound SSRF/DNS-rebinding checks and return the pinned socket destination."""
    try:
        return resolve_and_pin_endpoint(
            endpoint_url,
            protocol_profile,
            allow_loopback=allow_loopback,
            allowed_hosts=allowed_hosts,
            allowed_private_cidrs=allowed_private_cidrs,
            is_production=is_production,
            resolver=resolver,
            require_dns_resolution=True,
        )
    except ValidationFailed as exc:
        reason = str(exc.details.get("reason") or "ssrf_blocked")
        retryable = reason in {"dns_failure", "timeout"}
        raise ConnectorTransportError(
            reason,
            str(exc),
            retryable=retryable,
        ) from exc


def validate_connector_config(
    config: dict[str, Any] | None,
    *,
    protocol_profile: str,
) -> dict[str, Any]:
    """Validate and normalize connector configuration, bounds, and channel mappings."""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValidationFailed(
            "config must be a JSON object",
            details={"field": "config", "reason": "invalid_config"},
        )
    validate_no_plaintext_secrets(config, path="config")
    spec = PROTOCOL_PROFILE_SPECS.get(protocol_profile)
    if spec is None:
        raise ValidationFailed(
            f"unsupported protocol profile {protocol_profile!r}",
            details={"field": "protocol_profile", "allowed": list(PROTOCOL_PROFILE_SPECS)},
        )

    auth_mode = str(config.get("auth_mode") or "none").strip().lower()
    if auth_mode not in spec.auth_modes:
        raise ValidationFailed(
            f"auth_mode {auth_mode!r} is not supported by {protocol_profile!r}",
            details={
                "field": "config.auth_mode",
                "value": auth_mode,
                "allowed": list(spec.auth_modes),
            },
        )

    if config.get("tls_verify") is False:
        raise ValidationFailed(
            "disabling TLS certificate verification (tls_verify=false) is forbidden",
            details={"field": "config.tls_verify", "reason": "tls_verify_required"},
        )

    poll_interval = float(config.get("poll_interval_seconds", 2.0))
    if poll_interval < 0.05 or poll_interval > 3600.0:
        raise ValidationFailed(
            "poll_interval_seconds must be between 0.05 and 3600 seconds",
            details={"field": "config.poll_interval_seconds", "value": poll_interval},
        )

    timeout_seconds = float(config.get("timeout_seconds", 10.0))
    if timeout_seconds < 0.2 or timeout_seconds > 60.0:
        raise ValidationFailed(
            "timeout_seconds must be between 0.2 and 60 seconds",
            details={"field": "config.timeout_seconds", "value": timeout_seconds},
        )

    max_points_per_poll = int(config.get("max_points_per_poll", 250))
    if max_points_per_poll < 1 or max_points_per_poll > 500:
        raise ValidationFailed(
            "max_points_per_poll must be between 1 and 500",
            details={"field": "config.max_points_per_poll", "value": max_points_per_poll},
        )

    max_consecutive_failures = int(config.get("max_consecutive_failures", 5))
    if max_consecutive_failures < 1 or max_consecutive_failures > 50:
        raise ValidationFailed(
            "max_consecutive_failures must be between 1 and 50",
            details={"field": "config.max_consecutive_failures", "value": max_consecutive_failures},
        )

    base_backoff_seconds = float(config.get("base_backoff_seconds", 1.0))
    max_backoff_seconds = float(config.get("max_backoff_seconds", 30.0))
    if (
        base_backoff_seconds < 0.01
        or max_backoff_seconds < base_backoff_seconds
        or max_backoff_seconds > 3600.0
    ):
        raise ValidationFailed(
            "backoff bounds must satisfy 0.01 <= base_backoff_seconds <= max_backoff_seconds <= 3600",
            details={
                "field": "config.backoff_seconds",
                "base_backoff_seconds": base_backoff_seconds,
                "max_backoff_seconds": max_backoff_seconds,
            },
        )

    parsed_specs = parse_channel_mappings(config.get("channel_mappings"))
    normalized_mappings: list[dict[str, Any]] = [
        {
            "source_mnemonic": spec_item.source_mnemonic,
            "channel_uri": spec_item.channel_uri,
            "channel_key": spec_item.channel_key,
            "name": spec_item.name,
            "dimension": spec_item.dimension,
            "unit": spec_item.unit,
            "description": spec_item.description,
            "is_realtime": spec_item.is_realtime,
        }
        for spec_item in parsed_specs
    ]

    normalized: dict[str, Any] = {
        "auth_mode": auth_mode,
        "tls_verify": True,
        "poll_interval_seconds": poll_interval,
        "timeout_seconds": timeout_seconds,
        "max_points_per_poll": max_points_per_poll,
        "max_consecutive_failures": max_consecutive_failures,
        "base_backoff_seconds": base_backoff_seconds,
        "max_backoff_seconds": max_backoff_seconds,
        "channel_mappings": normalized_mappings,
    }
    for extra_key in (
        "well_uid",
        "wellbore_uid",
        "log_uid",
        "uid_well",
        "uid_wellbore",
        "uid_log",
        "max_return_nodes",
        "max_pages_per_poll",
        "plan",
        "start_iso",
        "quality",
        "max_data_items",
        "max_message_rate",
    ):
        if extra_key in config and config[extra_key] is not None:
            normalized[extra_key] = config[extra_key]
    return normalized


def _ensure_aware(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


def compute_connector_health(
    row: Connector,
    *,
    now: dt.datetime | None = None,
    telemetry_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute honest, non-conflated operational health for a connector.

    Separates:
    - intent (`desired_state`, `is_enabled`)
    - runtime state (`status`)
    - worker lease state (`lease_active`, `worker_id`, `fencing_token`)
    - data freshness & quality (`data_freshness`, `has_low_quality`, `seconds_since_last_ingest`)
    - overall `health_state` and `is_live`
    """
    current = now or utc_now()
    settings = get_settings()
    lease_expires = _ensure_aware(row.lease_expires_at)
    last_ingest = _ensure_aware(row.last_ingest_at or row.last_sync_at)
    last_poll = _ensure_aware(row.last_successful_poll_at)
    lease_active = bool(row.worker_id and lease_expires and lease_expires > current)

    seconds_since_ingest = (
        max(0.0, round((current - last_ingest).total_seconds(), 2)) if last_ingest is not None else None
    )
    seconds_since_poll = (
        max(0.0, round((current - last_poll).total_seconds(), 2)) if last_poll is not None else None
    )

    if telemetry_summary is not None:
        data_freshness = str(telemetry_summary.get("freshness") or "missing")
        has_low_quality = bool(
            telemetry_summary.get("has_low_quality", False)
            or (row.cursor or {}).get("last_batch_low_quality", False)
        )
        trustworthy_channels = int(telemetry_summary.get("trustworthy_channels", 0))
    else:
        has_low_quality = bool((row.cursor or {}).get("last_batch_low_quality", False))
        trustworthy_channels = 0
        if last_ingest is None:
            data_freshness = "missing"
        elif (current - last_ingest).total_seconds() <= settings.telemetry_fresh_seconds:
            data_freshness = "fresh"
            trustworthy_channels = 0 if has_low_quality else 1
        else:
            data_freshness = "stale"

    if row.desired_state == "disabled" or row.status == "disabled":
        health_state = "disabled"
    elif row.desired_state == "stopped" or row.status == "stopped" or not row.is_enabled:
        health_state = "stopped"
    elif row.status == "failed":
        health_state = "failed"
    elif row.status == "backing_off":
        health_state = "backing_off"
    elif row.status == "degraded":
        health_state = "degraded"
    elif row.status in ("created", "configured"):
        health_state = "configured"
    elif row.status == "starting":
        health_state = "starting"
    elif row.status == "running":
        if row.worker_id and lease_expires and lease_expires <= current:
            health_state = "lease_expired"
        elif last_ingest is None:
            health_state = "no_data_yet"
        elif data_freshness == "stale":
            health_state = "stale_data"
        elif has_low_quality:
            health_state = "low_quality_data"
        else:
            health_state = "live"
    else:
        health_state = "configured"

    is_live = bool(
        row.is_enabled
        and row.desired_state == "enabled"
        and row.status == "running"
        and (not row.worker_id or lease_active)
        and row.error_count == 0
        and data_freshness == "fresh"
        and not has_low_quality
    )

    return {
        "health_state": health_state,
        "is_live": is_live,
        "lease_active": lease_active,
        "data_freshness": data_freshness,
        "has_low_quality": has_low_quality,
        "trustworthy_channels": trustworthy_channels,
        "seconds_since_last_poll": seconds_since_poll,
        "seconds_since_last_ingest": seconds_since_ingest,
    }


def allowed_connector_actions(row: Connector, principal: Principal | None = None) -> list[str]:
    """Compute governed lifecycle actions permitted for this connector and caller."""
    can_control = principal is None or (
        permission_granted(principal, "connector.control") and principal.max_action_level.rank >= 2
    )
    can_manage = principal is None or (
        permission_granted(principal, "connector.manage") and principal.max_action_level.rank >= 2
    )
    can_test = principal is None or (
        permission_granted(principal, "connector.test") and principal.max_action_level.rank >= 2
    )
    actions: list[str] = []
    if can_test:
        actions.extend(["test", "preview"])
    if can_manage:
        actions.append("update")
    if can_control:
        if (
            not row.is_enabled
            or row.desired_state in ("disabled", "stopped")
            or row.status
            in (
                "created",
                "configured",
                "stopped",
                "disabled",
                "failed",
            )
        ):
            actions.append("start")
        if row.is_enabled or row.status in ("starting", "running", "backing_off", "degraded"):
            actions.append("stop")
        actions.append("restart")
        if row.desired_state != "disabled" or row.status != "disabled":
            actions.append("disable")
    return actions


def connector_out(
    row: Connector,
    *,
    principal: Principal | None = None,
    now: dt.datetime | None = None,
    telemetry_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize a Connector row with masked secrets and explicit health/freshness semantics."""
    spec = PROTOCOL_PROFILE_SPECS.get(row.protocol_profile)
    health = compute_connector_health(row, now=now, telemetry_summary=telemetry_summary)
    mappings = list((row.config or {}).get("channel_mappings") or [])
    safe_config = {k: v for k, v in (row.config or {}).items() if k != "plan"}
    if "plan" in (row.config or {}):
        safe_config["plan_channels"] = sorted((row.config or {})["plan"].keys())

    return {
        "id": row.id,
        "org_id": row.org_id,
        "key": row.key,
        "name": row.name,
        "provider": row.provider,
        "protocol_profile": row.protocol_profile,
        "profile_spec": spec.to_dict() if spec else None,
        "is_synthetic": bool(spec.is_synthetic) if spec else (row.provider == "synthetic"),
        "source_classification": (
            "synthetic_test_only"
            if (spec and spec.is_synthetic) or row.provider == "synthetic"
            else "external_protocol"
        ),
        "direction": row.direction,
        "well_id": row.well_id,
        "wellbore_id": row.wellbore_id,
        "operation_id": row.operation_id,
        "project_id": row.project_id,
        "endpoint_url": row.endpoint_url,
        "description": row.description,
        "is_enabled": bool(row.is_enabled),
        "desired_state": row.desired_state,
        "status": row.status,
        "config_version": int(row.config_version or 1),
        "version": row.updated_at.isoformat() if row.updated_at else None,
        "config": safe_config,
        "channel_mappings": mappings,
        "mapped_channel_count": len(mappings),
        "secret_refs": mask_secret_refs(row.secret_refs),
        "cursor": dict(row.cursor or {}),
        "worker": {
            "worker_id": row.worker_id,
            "lease_expires_at": row.lease_expires_at.isoformat() if row.lease_expires_at else None,
            "last_heartbeat_at": (row.last_heartbeat_at.isoformat() if row.last_heartbeat_at else None),
            "fencing_token": int(row.fencing_token or 0),
            "lease_active": health["lease_active"],
        },
        "health": {
            **health,
            "consecutive_failures": int(row.error_count or 0),
            "reconnect_count": int(row.reconnect_count or 0),
            "backoff_seconds": round(float(row.backoff_seconds or 0.0), 3),
            "next_poll_at": row.next_poll_at.isoformat() if row.next_poll_at else None,
            "last_transition_at": (row.last_transition_at.isoformat() if row.last_transition_at else None),
            "last_connected_at": (row.last_connected_at.isoformat() if row.last_connected_at else None),
            "last_poll_at": row.last_poll_at.isoformat() if row.last_poll_at else None,
            "last_successful_poll_at": (
                row.last_successful_poll_at.isoformat() if row.last_successful_poll_at else None
            ),
            "last_frame_at": row.last_frame_at.isoformat() if row.last_frame_at else None,
            "last_ingest_at": (
                (row.last_ingest_at or row.last_sync_at).isoformat()
                if (row.last_ingest_at or row.last_sync_at)
                else None
            ),
            "last_error": redact_sensitive_text(row.last_error),
            "last_error_category": row.last_error_category,
            "last_error_at": row.last_error_at.isoformat() if row.last_error_at else None,
            "last_trace_id": row.last_trace_id,
        },
        "allowed_actions": allowed_connector_actions(row, principal),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def connector_run_out(row: ConnectorRun) -> dict[str, Any]:
    return {
        "id": row.id,
        "org_id": row.org_id,
        "connector_id": row.connector_id,
        "well_id": row.well_id,
        "worker_id": row.worker_id,
        "fencing_token": int(row.fencing_token or 0),
        "config_version": int(row.config_version or 1),
        "run_kind": row.run_kind,
        "status": row.status,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
        "duration_ms": round(float(row.duration_ms), 2) if row.duration_ms is not None else None,
        "frames_received": int(row.frames_received or 0),
        "points_accepted": int(row.points_accepted or 0),
        "points_duplicates": int(row.points_duplicates or 0),
        "points_revised": int(row.points_revised or 0),
        "points_rejected": int(row.points_rejected or 0),
        "alerts_raised": int(row.alerts_raised or 0),
        "alerts_cleared": int(row.alerts_cleared or 0),
        "cursor_before": dict(row.cursor_before or {}),
        "cursor_after": dict(row.cursor_after or {}),
        "error_category": row.error_category,
        "error_message": redact_sensitive_text(row.error_message),
        "trace_id": row.trace_id,
        "details": dict(row.details or {}),
    }


class ConnectorSyntheticAdapter(SyntheticAdapter):
    """SyntheticAdapter wrapper that tracks connector watermark cursor state across worker polls."""

    def __init__(
        self,
        channels: list[ChannelDescriptor],
        plan: dict[str, list[tuple[float, float | None]]],
        *,
        start: dt.datetime,
        quality: str = "good",
        initial_cursor: dict[str, Any] | None = None,
        connector_id: str = "synthetic",
    ) -> None:
        super().__init__(channels, plan, start=start, quality=quality)
        self._connector_cursor: dict[str, Any] = dict(initial_cursor or {})
        self._connector_id = connector_id

    @property
    def cursor(self) -> dict[str, Any]:
        return dict(self._connector_cursor)

    async def poll(self) -> list[Any]:
        frames = list(await super().poll())
        if frames:
            step_index = int(self._connector_cursor.get("step_index", 0)) + 1
            max_ts = max(f.ts for f in frames)
            # Ensure unique source_point_id per connector poll step
            rewritten = [
                type(f)(
                    channel_key=f.channel_key,
                    ts=f.ts,
                    value=f.value,
                    unit=f.unit,
                    quality=f.quality,
                    source_point_id=f"synthetic:{self._connector_id}:{f.channel_key}:{step_index}",
                    source_ref=f.source_ref,
                    sequence=step_index,
                    depth_md_si=f.depth_md_si,
                )
                for f in frames
            ]
            self._connector_cursor = {
                "protocol": "synthetic.v1",
                "step_index": step_index,
                "cursor_timestamp": max_ts.isoformat(),
            }
            return rewritten
        return frames


class ConnectorService:
    """Tenant-scoped connector registry, configuration lifecycle, and safe connection testing."""

    def __init__(
        self,
        session: AsyncSession,
        org_id: str,
        *,
        principal: Principal | None = None,
    ) -> None:
        self.session = session
        self.org_id = org_id
        self._principal = principal

    async def _verify_scope(
        self,
        *,
        well_id: str | None,
        wellbore_id: str | None = None,
        operation_id: str | None = None,
    ) -> Well:
        if not well_id:
            raise ValidationFailed(
                "connector requires a target well_id",
                details={"field": "well_id", "reason": "well_required"},
            )
        well = (
            await self.session.execute(select(Well).where(Well.id == well_id, Well.org_id == self.org_id))
        ).scalar_one_or_none()
        if well is None:
            raise NotFound(f"well {well_id!r} not found")
        if wellbore_id is not None:
            wb = (
                await self.session.execute(
                    select(Wellbore).where(
                        Wellbore.id == wellbore_id,
                        Wellbore.well_id == well_id,
                        Wellbore.org_id == self.org_id,
                    )
                )
            ).scalar_one_or_none()
            if wb is None:
                raise NotFound(f"wellbore {wellbore_id!r} not found on well {well_id!r}")
        if operation_id is not None:
            op_row = (
                await self.session.execute(
                    select(Operation).where(
                        Operation.id == operation_id,
                        Operation.well_id == well_id,
                        Operation.org_id == self.org_id,
                    )
                )
            ).scalar_one_or_none()
            if op_row is None:
                raise NotFound(f"operation {operation_id!r} not found on well {well_id!r}")
        return well

    async def get(self, connector_id: str) -> Connector:
        row = (
            await self.session.execute(
                select(Connector).where(Connector.id == connector_id, Connector.org_id == self.org_id)
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFound(f"connector {connector_id!r} not found")
        return row

    async def _telemetry_summaries_by_well(self, well_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Batch-load channel freshness/quality summaries per well in one query (zero N+1)."""
        if not well_ids:
            return {}
        now = utc_now()
        settings = get_settings()
        rows = (
            (
                await self.session.execute(
                    select(TimeSeries).where(
                        TimeSeries.org_id == self.org_id,
                        TimeSeries.well_id.in_(well_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        by_well: dict[str, list[TimeSeries]] = {}
        for ch in rows:
            if ch.well_id:
                by_well.setdefault(ch.well_id, []).append(ch)
        summaries: dict[str, dict[str, Any]] = {}
        for wid in well_ids:
            channels = by_well.get(wid, [])
            if not channels:
                summaries[wid] = {
                    "freshness": "missing",
                    "has_low_quality": False,
                    "trustworthy_channels": 0,
                    "channel_count": 0,
                }
                continue
            any_points = any(ch.last_ts is not None for ch in channels)
            if not any_points:
                summaries[wid] = {
                    "freshness": "missing",
                    "has_low_quality": False,
                    "trustworthy_channels": 0,
                    "channel_count": len(channels),
                }
                continue
            latest_ts = max(
                (_ensure_aware(ch.last_ts) for ch in channels if ch.last_ts is not None),
                default=None,
            )
            is_fresh = bool(
                latest_ts is not None
                and (now - latest_ts).total_seconds() <= settings.telemetry_fresh_seconds
            )
            has_low_quality = False
            trustworthy = 0
            for ch in channels:
                last_q = str((ch.attributes or {}).get("last_quality") or "good").lower()
                if ch.last_ts is not None:
                    if last_q in TRUSTWORTHY_QUALITY:
                        trustworthy += 1
                    else:
                        has_low_quality = True
            summaries[wid] = {
                "freshness": "fresh" if is_fresh else "stale",
                "has_low_quality": has_low_quality,
                "trustworthy_channels": trustworthy,
                "channel_count": len(channels),
            }
        return summaries

    async def list(
        self,
        *,
        well_id: str | None = None,
        provider: str | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        clauses: list[Any] = [Connector.org_id == self.org_id]
        if well_id is not None:
            clauses.append(Connector.well_id == well_id)
        if provider is not None:
            clauses.append(Connector.provider == provider.strip().lower())
        if status is not None:
            clauses.append(Connector.status == status.strip().lower())

        total = int(
            (
                await self.session.execute(
                    select(func.count()).select_from(select(Connector.id).where(*clauses).subquery())
                )
            ).scalar_one()
        )
        rows = (
            (
                await self.session.execute(
                    select(Connector)
                    .where(*clauses)
                    .order_by(Connector.created_at.desc(), Connector.id.desc())
                    .limit(min(limit, 200))
                    .offset(offset)
                )
            )
            .scalars()
            .all()
        )
        well_ids = sorted({row.well_id for row in rows if row.well_id})
        summaries = await self._telemetry_summaries_by_well(well_ids)
        now = utc_now()
        items = [
            connector_out(
                row,
                principal=self._principal,
                now=now,
                telemetry_summary=summaries.get(row.well_id) if row.well_id else None,
            )
            for row in rows
        ]
        return items, total

    async def inspect(self, connector_id: str) -> dict[str, Any]:
        row = await self.get(connector_id)
        summaries = await self._telemetry_summaries_by_well([row.well_id]) if row.well_id else {}
        return connector_out(
            row,
            principal=self._principal,
            telemetry_summary=summaries.get(row.well_id) if row.well_id else None,
        )

    async def create(
        self,
        *,
        key: str,
        name: str,
        protocol_profile: str,
        well_id: str,
        wellbore_id: str | None = None,
        operation_id: str | None = None,
        endpoint_url: str | None = None,
        description: str | None = None,
        config: dict[str, Any] | None = None,
        secret_refs: dict[str, Any] | None = None,
        allow_loopback: bool | None = None,
    ) -> dict[str, Any]:
        clean_key = key.strip().lower()
        if not CONNECTOR_KEY_PATTERN.match(clean_key):
            raise ValidationFailed(
                "connector key must be 3..120 lowercase alphanumeric, dot, underscore or hyphen characters",
                details={"field": "key", "value": key},
            )
        if not name or not name.strip():
            raise ValidationFailed("connector name is required", details={"field": "name"})

        spec = PROTOCOL_PROFILE_SPECS.get(protocol_profile)
        if spec is None:
            raise ValidationFailed(
                f"unsupported protocol_profile {protocol_profile!r}",
                details={"field": "protocol_profile", "allowed": list(PROTOCOL_PROFILE_SPECS)},
            )

        well = await self._verify_scope(well_id=well_id, wellbore_id=wellbore_id, operation_id=operation_id)
        existing = (
            await self.session.execute(
                select(Connector.id).where(Connector.org_id == self.org_id, Connector.key == clean_key)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise Conflict(
                f"connector with key {clean_key!r} already exists in this organization",
                details={"field": "key", "key": clean_key, "existing_id": existing},
            )

        validated_endpoint = validate_connector_endpoint(
            endpoint_url, protocol_profile, allow_loopback=allow_loopback
        )
        validated_secrets = validate_secret_refs(secret_refs)
        validated_config = validate_connector_config(config, protocol_profile=protocol_profile)

        auth_mode = validated_config.get("auth_mode", "none")
        if auth_mode == "basic" and (
            "username" not in validated_secrets or "password" not in validated_secrets
        ):
            raise ValidationFailed(
                "auth_mode='basic' requires both 'username' and 'password' in secret_refs",
                details={"field": "secret_refs", "auth_mode": "basic"},
            )
        if auth_mode == "bearer" and "bearer_token" not in validated_secrets:
            raise ValidationFailed(
                "auth_mode='bearer' requires 'bearer_token' in secret_refs",
                details={"field": "secret_refs", "auth_mode": "bearer"},
            )

        now = utc_now()
        row = Connector(
            id=new_id("cnc"),
            org_id=self.org_id,
            key=clean_key,
            name=name.strip(),
            provider=spec.provider,
            protocol_profile=protocol_profile,
            direction="inbound",
            status="configured",
            desired_state="disabled",
            config_version=1,
            project_id=well.project_id,
            well_id=well_id,
            wellbore_id=wellbore_id,
            operation_id=operation_id,
            endpoint_url=validated_endpoint,
            description=description.strip() if description else None,
            config=validated_config,
            secret_refs=validated_secrets,
            capabilities=list(spec.supported_operations),
            cursor={},
            fencing_token=0,
            last_transition_at=now,
            error_count=0,
            reconnect_count=0,
            backoff_seconds=0.0,
            is_enabled=False,
            created_by=self._principal.id if self._principal else None,
            attributes={},
        )
        self.session.add(row)
        await self.session.flush()

        out = await self.inspect(row.id)
        await emit(
            self.session,
            org_id=self.org_id,
            type="connector.changed",
            subject_kind="connector",
            subject_id=row.id,
            well_id=row.well_id,
            wellbore_id=row.wellbore_id,
            payload={
                "connector_id": row.id,
                "key": row.key,
                "protocol_profile": row.protocol_profile,
                "status": row.status,
                "desired_state": row.desired_state,
                "config_version": row.config_version,
                "action": "created",
            },
        )
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal,
            action="connector.manage",
            resource_kind="connector",
            resource_id=row.id,
            well_id=row.well_id,
            before={},
            after={
                "key": row.key,
                "name": row.name,
                "protocol_profile": row.protocol_profile,
                "endpoint_url": row.endpoint_url,
                "config_version": row.config_version,
                "secret_refs": mask_secret_refs(row.secret_refs),
            },
            details={"operation": "create"},
        )
        return out

    async def update(
        self,
        connector_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        endpoint_url: str | None = None,
        config: dict[str, Any] | None = None,
        secret_refs: dict[str, Any] | None = None,
        clear_secret_slots: list[str] | None = None,
        expected_config_version: int | None = None,
        reason: str | None = None,
        allow_loopback: bool | None = None,
    ) -> dict[str, Any]:
        row = await self.get(connector_id)
        current_cfg_version = int(row.config_version or 1)
        if expected_config_version is not None and current_cfg_version != int(expected_config_version):
            raise Conflict(
                "connector configuration was modified by another request",
                details={
                    "connector_id": row.id,
                    "expected_config_version": expected_config_version,
                    "stored_config_version": current_cfg_version,
                },
            )

        before = {
            "name": row.name,
            "endpoint_url": row.endpoint_url,
            "config_version": current_cfg_version,
            "config": dict(row.config or {}),
            "secret_refs": mask_secret_refs(row.secret_refs),
        }

        new_name = row.name
        if name is not None:
            if not name.strip():
                raise ValidationFailed("connector name cannot be blank", details={"field": "name"})
            new_name = name.strip()
        new_description = row.description
        if description is not None:
            new_description = description.strip() or None
        new_endpoint_url = row.endpoint_url
        if endpoint_url is not None:
            new_endpoint_url = validate_connector_endpoint(
                endpoint_url, row.protocol_profile, allow_loopback=allow_loopback
            )

        merged_secrets = dict(row.secret_refs or {})
        if clear_secret_slots:
            for slot in clear_secret_slots:
                merged_secrets.pop(str(slot).strip().lower(), None)
        if secret_refs is not None:
            new_secrets = validate_secret_refs(secret_refs, allow_masked_placeholders=True)
            merged_secrets.update(new_secrets)

        new_config = dict(row.config or {})
        if config is not None:
            merged_config = {**new_config, **config}
            new_config = validate_connector_config(merged_config, protocol_profile=row.protocol_profile)

        auth_mode = new_config.get("auth_mode", "none")
        if auth_mode == "basic" and ("username" not in merged_secrets or "password" not in merged_secrets):
            raise ValidationFailed(
                "auth_mode='basic' requires both 'username' and 'password' in secret_refs",
                details={"field": "secret_refs", "auth_mode": "basic"},
            )
        if auth_mode == "bearer" and "bearer_token" not in merged_secrets:
            raise ValidationFailed(
                "auth_mode='bearer' requires 'bearer_token' in secret_refs",
                details={"field": "secret_refs", "auth_mode": "bearer"},
            )

        next_cfg_version = current_cfg_version + 1
        next_fencing_token = int(row.fencing_token or 0) + 1
        now = utc_now()

        cas_result = await self.session.execute(
            update(Connector)
            .where(
                Connector.id == row.id,
                Connector.org_id == self.org_id,
                Connector.config_version == current_cfg_version,
            )
            .values(
                name=new_name,
                description=new_description,
                endpoint_url=new_endpoint_url,
                secret_refs=merged_secrets,
                config=new_config,
                config_version=next_cfg_version,
                fencing_token=next_fencing_token,
                updated_at=now,
            )
        )
        if (cas_result.rowcount or 0) != 1:
            raise Conflict(
                "connector configuration was modified concurrently by another transaction",
                details={
                    "connector_id": row.id,
                    "expected_config_version": current_cfg_version,
                },
            )
        await self.session.refresh(row)

        await emit(
            self.session,
            org_id=self.org_id,
            type="connector.changed",
            subject_kind="connector",
            subject_id=row.id,
            well_id=row.well_id,
            wellbore_id=row.wellbore_id,
            payload={
                "connector_id": row.id,
                "key": row.key,
                "status": row.status,
                "desired_state": row.desired_state,
                "config_version": row.config_version,
                "fencing_token": row.fencing_token,
                "action": "updated",
            },
        )
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal,
            action="connector.manage",
            resource_kind="connector",
            resource_id=row.id,
            well_id=row.well_id,
            before=before,
            after={
                "name": row.name,
                "endpoint_url": row.endpoint_url,
                "config_version": int(row.config_version),
                "config": dict(row.config or {}),
                "secret_refs": mask_secret_refs(row.secret_refs),
            },
            details={"operation": "update", "reason": reason or "configuration updated"},
        )
        return await self.inspect(row.id)

    async def transition(
        self,
        connector_id: str,
        *,
        action: str,
        reason: str | None = None,
        expected_config_version: int | None = None,
    ) -> dict[str, Any]:
        """Execute a governed lifecycle transition (`start`, `stop`, `restart`, `disable`)."""
        row = await self.get(connector_id)
        verb = action.strip().lower()
        if verb not in ("start", "stop", "restart", "disable"):
            raise ValidationFailed(
                f"unsupported connector transition {action!r}",
                details={"field": "action", "allowed": ["start", "stop", "restart", "disable"]},
            )
        if verb in ("stop", "restart", "disable") and (not reason or not reason.strip()):
            raise ValidationFailed(
                f"connector {verb!r} requires a non-empty operator reason",
                details={"field": "reason", "action": verb},
            )
        current_cfg_version = int(row.config_version or 1)
        if expected_config_version is not None and current_cfg_version != int(expected_config_version):
            raise Conflict(
                "connector configuration version changed since it was read",
                details={
                    "connector_id": row.id,
                    "expected_config_version": expected_config_version,
                    "stored_config_version": current_cfg_version,
                },
            )

        now = utc_now()
        current_fencing = int(row.fencing_token or 0)
        before = {
            "status": row.status,
            "desired_state": row.desired_state,
            "is_enabled": bool(row.is_enabled),
            "worker_id": row.worker_id,
            "fencing_token": current_fencing,
        }

        update_vals: dict[str, Any] = {
            "updated_at": now,
            "last_transition_at": now,
        }
        if verb == "start":
            # Note: starting sets desired_state='enabled' and status='starting' (never optimistically 'running' or 'live').
            update_vals["desired_state"] = "enabled"
            update_vals["is_enabled"] = True
            update_vals["status"] = row.status if row.status in ("running", "starting") else "starting"
            update_vals["error_count"] = 0
            update_vals["backoff_seconds"] = 0.0
            update_vals["next_poll_at"] = now
        elif verb == "stop":
            update_vals["desired_state"] = "stopped"
            update_vals["is_enabled"] = False
            update_vals["status"] = "stopped"
            update_vals["worker_id"] = None
            update_vals["lease_expires_at"] = None
            update_vals["backoff_seconds"] = 0.0
            update_vals["next_poll_at"] = None
            update_vals["fencing_token"] = current_fencing + 1
        elif verb == "disable":
            update_vals["desired_state"] = "disabled"
            update_vals["is_enabled"] = False
            update_vals["status"] = "disabled"
            update_vals["worker_id"] = None
            update_vals["lease_expires_at"] = None
            update_vals["backoff_seconds"] = 0.0
            update_vals["next_poll_at"] = None
            update_vals["fencing_token"] = current_fencing + 1
        elif verb == "restart":
            update_vals["desired_state"] = "enabled"
            update_vals["is_enabled"] = True
            update_vals["status"] = "starting"
            update_vals["worker_id"] = None
            update_vals["lease_expires_at"] = None
            update_vals["error_count"] = 0
            update_vals["backoff_seconds"] = 0.0
            update_vals["next_poll_at"] = now
            update_vals["reconnect_count"] = int(row.reconnect_count or 0) + 1
            update_vals["fencing_token"] = current_fencing + 1

        cas_clauses = [
            Connector.id == row.id,
            Connector.org_id == self.org_id,
            Connector.config_version == current_cfg_version,
        ]
        if expected_config_version is not None:
            cas_clauses.append(Connector.fencing_token == current_fencing)

        cas_res = await self.session.execute(
            update(Connector).where(*cas_clauses).values(**update_vals)
        )
        if (cas_res.rowcount or 0) != 1:
            raise Conflict(
                "connector state was modified concurrently by another operation",
                details={
                    "connector_id": row.id,
                    "expected_config_version": current_cfg_version,
                },
            )
        await self.session.refresh(row)

        await emit(
            self.session,
            org_id=self.org_id,
            type="connector.changed",
            subject_kind="connector",
            subject_id=row.id,
            well_id=row.well_id,
            wellbore_id=row.wellbore_id,
            payload={
                "connector_id": row.id,
                "key": row.key,
                "action": verb,
                "status": row.status,
                "desired_state": row.desired_state,
                "is_enabled": bool(row.is_enabled),
                "fencing_token": int(row.fencing_token or 0),
                "reason": reason.strip() if reason else None,
            },
        )
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal,
            action="connector.control",
            resource_kind="connector",
            resource_id=row.id,
            well_id=row.well_id,
            before=before,
            after={
                "status": row.status,
                "desired_state": row.desired_state,
                "is_enabled": bool(row.is_enabled),
                "worker_id": row.worker_id,
                "fencing_token": int(row.fencing_token or 0),
            },
            details={"transition": verb, "reason": reason.strip() if reason else "operator start"},
        )
        return await self.inspect(row.id)

    async def list_runs(self, connector_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        await self.get(connector_id)
        rows = (
            (
                await self.session.execute(
                    select(ConnectorRun)
                    .where(
                        ConnectorRun.org_id == self.org_id,
                        ConnectorRun.connector_id == connector_id,
                    )
                    .order_by(ConnectorRun.started_at.desc(), ConnectorRun.id.desc())
                    .limit(min(max(1, limit), 200))
                )
            )
            .scalars()
            .all()
        )
        return [connector_run_out(r) for r in rows]

    async def build_adapter(
        self,
        row: Connector,
        *,
        resolved_secrets: dict[str, str] | None = None,
    ) -> SourceAdapter:
        """Instantiate the concrete SourceAdapter for a connector's declared protocol profile."""
        cfg = dict(row.config or {})
        mappings = list(cfg.get("channel_mappings") or [])
        descriptors = [
            ChannelDescriptor(
                channel_key=str(m["channel_key"]),
                name=str(m["name"]),
                dimension=str(m["dimension"]),
                unit=str(m["unit"]),
                description=m.get("description"),
                is_realtime=bool(m.get("is_realtime", True)),
            )
            for m in mappings
        ]
        if row.protocol_profile == "synthetic.v1":
            plan_cfg = cfg.get("plan")
            if not isinstance(plan_cfg, dict) or not plan_cfg:
                plan_cfg = {d.channel_key: [[0.0, 100.0], [5.0, 105.0]] for d in descriptors}
            step_index = int((row.cursor or {}).get("step_index", 0))
            loop_plan = bool(cfg.get("loop", True))
            start_iso = cfg.get("start_iso")
            parsed_plan: dict[str, list[tuple[float, float | None]]] = {}
            for ch_key, pts in plan_cfg.items():
                key_norm = str(ch_key).strip().lower()
                entries = [(float(pair[0]), float(pair[1]) if pair[1] is not None else None) for pair in pts]
                if not entries:
                    continue
                if start_iso:
                    # Pinned start_iso: slice by step_index
                    parsed_plan[key_norm] = (
                        entries[step_index:] if not loop_plan else [entries[step_index % len(entries)]]
                    )
                else:
                    # Live commissioning synthetic plan: emit current step at utc_now()
                    chosen = (
                        entries[step_index % len(entries)]
                        if loop_plan
                        else (entries[step_index] if step_index < len(entries) else None)
                    )
                    parsed_plan[key_norm] = [(0.0, chosen[1])] if chosen is not None else []
            start_dt = (
                dt.datetime.fromisoformat(str(start_iso).replace("Z", "+00:00")) if start_iso else utc_now()
            )
            return ConnectorSyntheticAdapter(
                descriptors,
                parsed_plan,
                start=start_dt,
                quality=str(cfg.get("quality") or "good"),
                initial_cursor=dict(row.cursor or {}),
                connector_id=row.id,
            )

        if row.protocol_profile == "witsml.1.4.1.1.soap_http":
            from drillai.telemetry.witsml_client import WitsmlPollingAdapter

            return WitsmlPollingAdapter(
                endpoint_url=row.endpoint_url or "",
                channel_mappings=mappings,
                config=cfg,
                secrets=resolved_secrets or {},
                cursor=dict(row.cursor or {}),
                well_id=row.well_id or "",
                wellbore_id=row.wellbore_id,
                operation_id=row.operation_id,
            )

        if row.protocol_profile == "etp.1.2.json_ws":
            from drillai.telemetry.etp_client import EtpSubscriptionAdapter

            return EtpSubscriptionAdapter(
                endpoint_url=row.endpoint_url or "",
                channel_mappings=mappings,
                config=cfg,
                secrets=resolved_secrets or {},
                cursor=dict(row.cursor or {}),
                well_id=row.well_id or "",
                wellbore_id=row.wellbore_id,
                operation_id=row.operation_id,
            )

        raise ValidationFailed(
            f"unsupported protocol profile {row.protocol_profile!r}",
            details={"field": "protocol_profile", "value": row.protocol_profile},
        )

    async def test_connection(
        self,
        connector_id: str,
        *,
        allow_loopback: bool | None = None,
    ) -> dict[str, Any]:
        """Verify connectivity and channel descriptor discovery without persistent ingestion."""
        row = await self.get(connector_id)
        started = utc_now()
        t0 = time.perf_counter()
        trace_id = new_id("trc")
        resolved_secrets: dict[str, str] = {}
        try:
            validate_connector_endpoint(row.endpoint_url, row.protocol_profile, allow_loopback=allow_loopback)
            resolved_secrets = await resolve_secret_refs(
                self.session, self.org_id, dict(row.secret_refs or {})
            )
            adapter = await self.build_adapter(row, resolved_secrets=resolved_secrets)
            await adapter.connect()
            try:
                descriptors = list(await adapter.describe_channels())
                for d in descriptors:
                    validate_descriptor(d)
            finally:
                await adapter.close()

            finished = utc_now()
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            row.last_connected_at = finished
            row.last_trace_id = trace_id
            run_row = ConnectorRun(
                id=new_id("crn"),
                org_id=self.org_id,
                connector_id=row.id,
                well_id=row.well_id,
                config_version=int(row.config_version or 1),
                run_kind="test_connection",
                status="succeeded",
                started_at=started,
                finished_at=finished,
                duration_ms=duration_ms,
                cursor_before=dict(row.cursor or {}),
                cursor_after=dict(row.cursor or {}),
                trace_id=trace_id,
                details={
                    "discovered_channels": [
                        {
                            "channel_key": d.channel_key,
                            "name": d.name,
                            "dimension": d.dimension,
                            "unit": d.unit,
                        }
                        for d in descriptors
                    ]
                },
            )
            self.session.add(run_row)
            await record_audit(
                self.session,
                org_id=self.org_id,
                principal=self._principal,
                action="connector.test",
                resource_kind="connector",
                resource_id=row.id,
                well_id=row.well_id,
                details={
                    "run_kind": "test_connection",
                    "outcome": "succeeded",
                    "discovered_channel_count": len(descriptors),
                    "duration_ms": duration_ms,
                },
            )
            await self.session.flush()
            return {
                "ok": True,
                "connector_id": row.id,
                "protocol_profile": row.protocol_profile,
                "is_synthetic": row.protocol_profile == "synthetic.v1",
                "tested_at": finished.isoformat(),
                "duration_ms": duration_ms,
                "discovered_channels": [
                    {
                        "channel_key": d.channel_key,
                        "name": d.name,
                        "dimension": d.dimension,
                        "unit": d.unit,
                        "is_realtime": d.is_realtime,
                    }
                    for d in descriptors
                ],
                "error_category": None,
                "error_message": None,
                "trace_id": trace_id,
            }
        except Exception as exc:
            finished = utc_now()
            duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
            category = classify_connector_error(exc)
            safe_msg = redact_sensitive_text(str(exc), resolved_secrets) or "connection test failed"
            row.last_error = safe_msg
            row.last_error_category = category
            row.last_error_at = finished
            row.last_trace_id = trace_id
            run_row = ConnectorRun(
                id=new_id("crn"),
                org_id=self.org_id,
                connector_id=row.id,
                well_id=row.well_id,
                config_version=int(row.config_version or 1),
                run_kind="test_connection",
                status="failed",
                started_at=started,
                finished_at=finished,
                duration_ms=duration_ms,
                cursor_before=dict(row.cursor or {}),
                cursor_after=dict(row.cursor or {}),
                error_category=category,
                error_message=safe_msg,
                trace_id=trace_id,
                details={},
            )
            self.session.add(run_row)
            await record_audit(
                self.session,
                org_id=self.org_id,
                principal=self._principal,
                action="connector.test",
                resource_kind="connector",
                resource_id=row.id,
                well_id=row.well_id,
                outcome="failure",
                details={
                    "run_kind": "test_connection",
                    "outcome": "failed",
                    "error_category": category,
                    "error_message": safe_msg,
                    "duration_ms": duration_ms,
                },
            )
            await self.session.flush()
            return {
                "ok": False,
                "connector_id": row.id,
                "protocol_profile": row.protocol_profile,
                "is_synthetic": row.protocol_profile == "synthetic.v1",
                "tested_at": finished.isoformat(),
                "duration_ms": duration_ms,
                "discovered_channels": [],
                "error_category": category,
                "error_message": safe_msg,
                "trace_id": trace_id,
            }

    async def preview(
        self,
        connector_id: str,
        *,
        sample_limit: int = 20,
        allow_loopback: bool | None = None,
    ) -> dict[str, Any]:
        """Preview channel descriptors and a bounded sample of frames without persisting points."""
        row = await self.get(connector_id)
        bounded_limit = min(max(1, int(sample_limit)), 50)
        resolved_secrets = await resolve_secret_refs(self.session, self.org_id, dict(row.secret_refs or {}))
        validate_connector_endpoint(row.endpoint_url, row.protocol_profile, allow_loopback=allow_loopback)
        adapter = await self.build_adapter(row, resolved_secrets=resolved_secrets)
        await adapter.connect()
        try:
            descriptors = list(await adapter.describe_channels())
            for d in descriptors:
                validate_descriptor(d)
            frames = list(await adapter.poll())[:bounded_limit]
            samples: list[dict[str, Any]] = []
            for frame in frames:
                point = adapter.normalize(frame)
                samples.append(
                    {
                        "channel_key": frame.channel_key,
                        "ts": point.ts.isoformat(),
                        "value": point.value,
                        "unit": point.unit,
                        "quality": point.quality,
                        "source_point_id": point.source_point_id,
                    }
                )
            return {
                "connector_id": row.id,
                "protocol_profile": row.protocol_profile,
                "is_synthetic": row.protocol_profile == "synthetic.v1",
                "descriptors": [
                    {
                        "channel_key": d.channel_key,
                        "name": d.name,
                        "dimension": d.dimension,
                        "unit": d.unit,
                        "is_realtime": d.is_realtime,
                    }
                    for d in descriptors
                ],
                "samples": samples,
                "sample_count": len(samples),
            }
        finally:
            await adapter.close()

    async def replay(
        self,
        connector_id: str,
        *,
        reason: str,
        from_timestamp: str | None = None,
        channel_keys: list[str] | None = None,
        dry_run: bool = False,
        expected_config_version: int | None = None,
        allow_loopback: bool | None = None,
    ) -> dict[str, Any]:
        """Execute a governed, idempotent replay/backfill over a bounded cursor/time window.

        - Requires a non-empty operator ``reason``.
        - Enforces ``expected_config_version`` when supplied.
        - Never regresses the connector's persistent watermark behind ``cursor_before``.
        - Records an auditable ``ConnectorRun(run_kind="replay" | "replay_dry_run")`` and ``AuditLog``.
        """
        from drillai.telemetry.adapters import ingest_frames
        from drillai.telemetry.service import TelemetryService

        if not reason or not reason.strip():
            raise ValidationFailed(
                "connector replay requires a non-empty operator reason",
                details={"field": "reason", "action": "replay"},
            )
        row = await self.get(connector_id)
        current_cfg_version = int(row.config_version or 1)
        if expected_config_version is not None and current_cfg_version != int(expected_config_version):
            raise Conflict(
                "connector configuration version changed since it was read",
                details={
                    "connector_id": row.id,
                    "expected_config_version": expected_config_version,
                    "stored_config_version": current_cfg_version,
                },
            )

        cursor_before = dict(row.cursor or {})
        replay_cursor: dict[str, Any] = {}
        if from_timestamp is not None and from_timestamp.strip():
            raw_from = from_timestamp.strip()
            if raw_from.endswith("Z") or raw_from.endswith("z"):
                raw_from = raw_from[:-1] + "+00:00"
            try:
                parsed_from = dt.datetime.fromisoformat(raw_from)
            except ValueError as exc:
                raise ValidationFailed(
                    f"invalid from_timestamp {from_timestamp!r}; expected ISO-8601 UTC timestamp",
                    details={"field": "from_timestamp", "reason": "invalid_timestamp"},
                ) from exc
            if parsed_from.tzinfo is None:
                parsed_from = parsed_from.replace(tzinfo=dt.UTC)
            inclusive_from = (
                parsed_from.astimezone(dt.UTC) - dt.timedelta(microseconds=1)
            ).isoformat()
            replay_cursor = {"cursor_timestamp": inclusive_from}

        started = utc_now()
        t0 = time.perf_counter()
        trace_id = new_id("trc")

        validate_connector_endpoint(row.endpoint_url, row.protocol_profile, allow_loopback=allow_loopback)
        resolved_secrets = await resolve_secret_refs(
            self.session, self.org_id, dict(row.secret_refs or {})
        )

        # Build adapter with temporary replay_cursor
        saved_row_cursor = row.cursor
        row.cursor = replay_cursor
        try:
            adapter = await self.build_adapter(row, resolved_secrets=resolved_secrets)
        finally:
            row.cursor = saved_row_cursor

        selected_channels: list[str] | None = None
        if channel_keys:
            selected_channels = [str(k).strip().lower() for k in channel_keys if str(k).strip()]

        await adapter.connect()
        try:
            descriptors = list(await adapter.describe_channels())
            for d in descriptors:
                validate_descriptor(d)
            if selected_channels:
                await adapter.subscribe(selected_channels)

            if dry_run:
                frames = list(await adapter.poll())
                last_batch = getattr(adapter, "last_batch", None)
                protocol_disp = getattr(last_batch, "disposition", None)
                protocol_dups = int(getattr(last_batch, "duplicates_suppressed", 0) or 0)
                protocol_rejected = int(getattr(protocol_disp, "total_protocol_rejected", 0) or 0)
                manifest = (
                    protocol_disp.to_dict()
                    if protocol_disp is not None
                    else {
                        "pages_or_messages_received": 1 if frames else 0,
                        "source_records_discovered": len(frames) + protocol_dups,
                        "mapped_frames_emitted": len(frames),
                        "unmapped_skipped": 0,
                        "unsubscribed_skipped": 0,
                        "watermark_suppressed": protocol_dups,
                        "in_batch_duplicates": 0,
                        "deferred_over_cap": 0,
                        "total_protocol_rejected": 0,
                        "reconciled": True,
                    }
                )
                manifest["dry_run"] = True
                manifest["checkpoint_status"] = "unchanged"
                finished = utc_now()
                duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
                run_row = ConnectorRun(
                    id=new_id("crn"),
                    org_id=self.org_id,
                    connector_id=row.id,
                    well_id=row.well_id,
                    config_version=current_cfg_version,
                    run_kind="replay_dry_run",
                    status="succeeded",
                    started_at=started,
                    finished_at=finished,
                    duration_ms=duration_ms,
                    frames_received=len(frames),
                    points_accepted=0,
                    points_duplicates=protocol_dups,
                    points_revised=0,
                    points_rejected=protocol_rejected,
                    cursor_before=cursor_before,
                    cursor_after=cursor_before,
                    trace_id=trace_id,
                    details={
                        "checkpoint_status": "unchanged",
                        "replay": {
                            "reason": reason.strip(),
                            "from_timestamp": from_timestamp,
                            "channel_keys": selected_channels,
                            "dry_run": True,
                        },
                        "manifest": manifest,
                    },
                )
                self.session.add(run_row)
                await record_audit(
                    self.session,
                    org_id=self.org_id,
                    principal=self._principal,
                    action="connector.control",
                    resource_kind="connector",
                    resource_id=row.id,
                    well_id=row.well_id,
                    before={"cursor": cursor_before},
                    after={"cursor": cursor_before},
                    details={
                        "transition": "replay_dry_run",
                        "reason": reason.strip(),
                        "from_timestamp": from_timestamp,
                        "channel_keys": selected_channels,
                        "frames_discovered": len(frames),
                    },
                )
                await self.session.flush()
                return {
                    "ok": True,
                    "dry_run": True,
                    "connector_id": row.id,
                    "run": connector_run_out(run_row),
                    "connector": await self.inspect(row.id),
                }

            telemetry_svc = TelemetryService(self.session, self.org_id)
            ingest_report = await ingest_frames(
                telemetry_svc,
                adapter,
                well_id=row.well_id or "",
                wellbore_id=row.wellbore_id,
                operation_id=row.operation_id,
                subscribe=selected_channels,
            )
        finally:
            await adapter.close()

        replay_after = dict(getattr(adapter, "cursor", None) or {})
        # Never regress cursor_timestamp behind cursor_before!
        merged_cursor = dict(cursor_before)
        before_ts_str = cursor_before.get("cursor_timestamp")
        after_ts_str = replay_after.get("cursor_timestamp")
        if after_ts_str and (not before_ts_str or str(after_ts_str) >= str(before_ts_str)):
            merged_cursor.update(replay_after)
        elif not merged_cursor and replay_after:
            merged_cursor = dict(replay_after)

        # Merge per-channel cursors without regressing any channel's last_timestamp
        if isinstance(replay_after.get("channel_cursors"), dict):
            existing_ch = dict(merged_cursor.get("channel_cursors") or {})
            for ch_k, ch_v in replay_after["channel_cursors"].items():
                if not isinstance(ch_v, dict):
                    continue
                prev_ch = dict(existing_ch.get(ch_k) or {})
                prev_ts = prev_ch.get("last_timestamp")
                new_ts = ch_v.get("last_timestamp")
                merged_recent = {
                    **dict(prev_ch.get("recent_points") or {}),
                    **dict(ch_v.get("recent_points") or {}),
                }
                existing_ch[ch_k] = {
                    "last_timestamp": new_ts if (new_ts and (not prev_ts or str(new_ts) > str(prev_ts))) else prev_ts,
                    "recent_points": merged_recent,
                }
            merged_cursor["channel_cursors"] = existing_ch

        report_dict = ingest_report.to_dict()
        totals = report_dict["totals"]
        last_batch = getattr(adapter, "last_batch", None)
        protocol_disp = getattr(last_batch, "disposition", None)
        protocol_dups = int(getattr(last_batch, "duplicates_suppressed", 0) or 0)
        protocol_rejected = int(getattr(protocol_disp, "total_protocol_rejected", 0) or 0)

        frames_received = int(ingest_report.frames)
        accepted = int(totals.get("accepted", 0))
        duplicates = int(totals.get("duplicates", 0)) + protocol_dups
        revised = int(totals.get("revised", 0))
        service_rejected = int(totals.get("rejected", 0))
        rejected = service_rejected + protocol_rejected
        alerts_raised = int(report_dict.get("alerts_raised", 0))
        alerts_cleared = int(report_dict.get("alerts_cleared", 0))

        out_of_order_count = 0
        late_count = 0
        conflicts_count = 0
        for ch_rep in ingest_report.reports.values():
            out_of_order_count += int(getattr(ch_rep, "out_of_order", 0) or 0)
            late_count += int(getattr(ch_rep, "late", 0) or 0)
            conflicts_count += len(getattr(ch_rep, "conflicts", None) or [])

        checkpoint_status = "complete" if merged_cursor != cursor_before else "unchanged"
        base_manifest = (
            protocol_disp.to_dict()
            if protocol_disp is not None
            else {
                "pages_or_messages_received": 1 if frames_received > 0 else 0,
                "source_records_discovered": frames_received + protocol_dups,
                "mapped_frames_emitted": frames_received,
                "unmapped_skipped": 0,
                "unsubscribed_skipped": 0,
                "watermark_suppressed": protocol_dups,
                "in_batch_duplicates": 0,
                "deferred_over_cap": 0,
                "total_protocol_rejected": 0,
                "reconciled": True,
            }
        )
        run_manifest = {
            **base_manifest,
            "checkpoint_status": checkpoint_status,
            "points_accepted": accepted,
            "points_duplicates": duplicates,
            "points_revised": revised,
            "points_rejected": rejected,
            "service_rejected": service_rejected,
            "conflicts": conflicts_count,
            "out_of_order": out_of_order_count,
            "late": late_count,
        }

        finished = utc_now()
        duration_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        row.cursor = merged_cursor
        row.last_connected_at = finished
        row.last_poll_at = finished
        row.last_successful_poll_at = finished
        if accepted > 0:
            row.last_frame_at = finished
            row.last_ingest_at = finished
            row.last_sync_at = finished
        row.last_trace_id = trace_id
        row.updated_at = finished

        run_row = ConnectorRun(
            id=new_id("crn"),
            org_id=self.org_id,
            connector_id=row.id,
            well_id=row.well_id,
            config_version=current_cfg_version,
            run_kind="replay",
            status="succeeded",
            started_at=started,
            finished_at=finished,
            duration_ms=duration_ms,
            frames_received=frames_received,
            points_accepted=accepted,
            points_duplicates=duplicates,
            points_revised=revised,
            points_rejected=rejected,
            alerts_raised=alerts_raised,
            alerts_cleared=alerts_cleared,
            cursor_before=cursor_before,
            cursor_after=merged_cursor,
            trace_id=trace_id,
            details={
                "channels": report_dict.get("channels", []),
                "checkpoint_status": checkpoint_status,
                "replay": {
                    "reason": reason.strip(),
                    "from_timestamp": from_timestamp,
                    "channel_keys": selected_channels,
                    "dry_run": False,
                },
                "manifest": run_manifest,
            },
        )
        self.session.add(run_row)
        await emit(
            self.session,
            org_id=self.org_id,
            type="connector.changed",
            subject_kind="connector",
            subject_id=row.id,
            well_id=row.well_id,
            wellbore_id=row.wellbore_id,
            payload={
                "connector_id": row.id,
                "action": "replay_succeeded",
                "frames_received": frames_received,
                "points_accepted": accepted,
                "points_duplicates": duplicates,
            },
        )
        await record_audit(
            self.session,
            org_id=self.org_id,
            principal=self._principal,
            action="connector.control",
            resource_kind="connector",
            resource_id=row.id,
            well_id=row.well_id,
            before={"cursor": cursor_before},
            after={"cursor": merged_cursor},
            details={
                "transition": "replay",
                "reason": reason.strip(),
                "from_timestamp": from_timestamp,
                "channel_keys": selected_channels,
                "points_accepted": accepted,
                "points_duplicates": duplicates,
                "points_rejected": rejected,
            },
        )
        await self.session.flush()
        return {
            "ok": True,
            "dry_run": False,
            "connector_id": row.id,
            "run": connector_run_out(run_row),
            "connector": await self.inspect(row.id),
        }
