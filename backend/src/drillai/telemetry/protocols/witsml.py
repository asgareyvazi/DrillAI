"""WITSML 1.4.1.1 SOAP/HTTP read-only polling client and local protocol-compatible test server.

Implements the ``witsml.1.4.1.1.soap_http`` profile:
- SOAP 1.1 ``WMLS_GetFromStore`` over HTTP/HTTPS POST
- Standards-based HTTP Basic or Bearer authentication
- XML entity/DTD rejection, namespace-aware parsing of ``<logs><log><logCurveInfo>`` and ``<logData>``
- Timezone-aware UTC index timestamp parsing and incremental ``startDateTimeIndex`` watermark advancing
- Pagination support when ``WMLS_GetFromStore`` returns ``Result=2`` (partial response, more rows available)
- Bounded retries for transient transport/5xx errors; immediate fail-fast on authentication or schema errors
- ``LocalWitsmlSoapServer``: a real TCP/HTTP 1.1 SOAP server for protocol-level integration and E2E testing
  (explicitly distinct from external vendor certification).
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import html
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from drillai.telemetry.adapters import SourceFrame
from drillai.telemetry.connectors import (
    ChannelMappingSpec,
    ConnectorTransportError,
    parse_channel_mappings,
    redact_sensitive_text,
)

MAX_WITSML_XML_BYTES = 5 * 1024 * 1024  # 5 MB

_QUALITY_MAP: dict[str, str] = {
    "good": "good",
    "ok": "good",
    "valid": "good",
    "1": "good",
    "suspect": "suspect",
    "questionable": "suspect",
    "doubtful": "suspect",
    "bad": "bad",
    "invalid": "bad",
    "error": "bad",
    "missing": "missing",
    "null": "missing",
}


@dataclass(frozen=True)
class WitsmlCurveDescriptor:
    mnemonic: str
    unit: str
    curve_description: str | None = None


@dataclass(frozen=True)
class WitsmlPollBatch:
    frames: tuple[SourceFrame, ...]
    discovered_curves: tuple[WitsmlCurveDescriptor, ...]
    next_watermark: dict[str, Any]
    pages_fetched: int
    raw_rows_parsed: int
    skipped_rows: int
    partial_more_available: bool = False


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    if ":" in tag:
        return tag.split(":", 1)[1]
    return tag


def _ensure_safe_xml(xml_text: str) -> None:
    upper = xml_text[:4096].upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ConnectorTransportError(
            "malformed_payload",
            "WITSML XML response contains forbidden DOCTYPE or ENTITY declarations",
            retryable=False,
        )


def _parse_iso_utc(raw: str) -> dt.datetime:
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("empty timestamp")
    if cleaned.endswith("Z") or cleaned.endswith("z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def build_get_from_store_soap_envelope(
    *,
    uid_well: str = "",
    uid_wellbore: str = "",
    uid_log: str = "",
    start_date_time_index: str | None = None,
    return_elements: str = "all",
    max_return_nodes: int | None = None,
) -> str:
    """Build a standards-compliant WITSML 1.4.1.1 ``WMLS_GetFromStore`` SOAP 1.1 envelope."""
    start_clause = (
        f"<startDateTimeIndex>{html.escape(start_date_time_index)}</startDateTimeIndex>"
        if start_date_time_index
        else ""
    )
    query_in = (
        '<logs xmlns="http://www.witsml.org/schemas/1series" version="1.4.1.1">'
        f'<log uidWell="{html.escape(uid_well)}" uidWellbore="{html.escape(uid_wellbore)}" uid="{html.escape(uid_log)}">'
        "<nameWell/><nameWellbore/><name/><indexCurve/>"
        f"{start_clause}"
        "<logCurveInfo><mnemonic/><unit/><curveDescription/></logCurveInfo>"
        "<logData><mnemonicList/><unitList/><data/></logData>"
        "</log>"
        "</logs>"
    )
    options_parts = [f"returnElements={return_elements}"]
    if max_return_nodes is not None and max_return_nodes > 0:
        options_parts.append(f"maxReturnNodes={int(max_return_nodes)}")
    options_in = ";".join(options_parts)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:wmls="http://www.witsml.org/message/120">'
        "<soapenv:Header/>"
        "<soapenv:Body>"
        "<wmls:WMLS_GetFromStore>"
        "<WMLtypeIn>log</WMLtypeIn>"
        f"<QueryIn>{html.escape(query_in)}</QueryIn>"
        f"<OptionsIn>{html.escape(options_in)}</OptionsIn>"
        "<CapabilitiesIn/>"
        "</wmls:WMLS_GetFromStore>"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    )


def parse_get_from_store_response(
    soap_xml: str,
    *,
    mappings: list[ChannelMappingSpec],
    well_id: str,
    wellbore_id: str | None = None,
    operation_id: str | None = None,
    previous_watermark: dict[str, Any] | None = None,
) -> WitsmlPollBatch:
    """Parse a WITSML 1.4.1.1 ``WMLS_GetFromStoreResponse`` or direct ``<logs>`` document."""
    if len(soap_xml.encode("utf-8")) > MAX_WITSML_XML_BYTES:
        raise ConnectorTransportError(
            "oversized_payload",
            f"WITSML SOAP response exceeds {MAX_WITSML_XML_BYTES} bytes",
            retryable=False,
        )
    _ensure_safe_xml(soap_xml)
    try:
        outer = ET.fromstring(soap_xml)
    except ET.ParseError as exc:
        raise ConnectorTransportError(
            "malformed_payload",
            f"invalid WITSML SOAP XML: {exc}",
            retryable=False,
        ) from exc

    fault_string: str | None = None
    result_code = 1
    supp_msg_out = ""
    xml_out_text: str | None = None

    if _local_name(outer.tag) == "logs":
        logs_root = outer
    else:
        for elem in outer.iter():
            lname = _local_name(elem.tag)
            if lname == "faultstring" and elem.text:
                fault_string = elem.text.strip()
            elif lname == "Result" and elem.text:
                try:
                    result_code = int(elem.text.strip())
                except ValueError:
                    result_code = -1
            elif lname == "SuppMsgOut" and elem.text:
                supp_msg_out = elem.text.strip()
            elif lname == "XMLout" and elem.text:
                xml_out_text = elem.text

        if fault_string:
            raise ConnectorTransportError(
                "protocol_error",
                f"WITSML SOAP Fault: {fault_string}",
                retryable=False,
            )
        if result_code <= 0:
            raise ConnectorTransportError(
                "protocol_error",
                f"WITSML WMLS_GetFromStore failed with Result={result_code}: {supp_msg_out or 'error'}",
                retryable=False,
            )
        if not xml_out_text or not xml_out_text.strip():
            return WitsmlPollBatch(
                frames=(),
                discovered_curves=(),
                next_watermark=dict(previous_watermark or {}),
                pages_fetched=1,
                raw_rows_parsed=0,
                skipped_rows=0,
                partial_more_available=False,
            )
        _ensure_safe_xml(xml_out_text)
        try:
            logs_root = ET.fromstring(xml_out_text)
        except ET.ParseError as exc:
            raise ConnectorTransportError(
                "malformed_payload",
                f"invalid WITSML XMLout payload: {exc}",
                retryable=False,
            ) from exc

    mapping_by_mnemonic = {m.source_mnemonic.upper(): m for m in mappings}
    discovered: list[WitsmlCurveDescriptor] = []
    discovered_seen: set[str] = set()
    frames: list[SourceFrame] = []
    raw_rows_parsed = 0
    skipped_rows = 0

    prev_cursor_ts: dt.datetime | None = None
    if previous_watermark and previous_watermark.get("cursor_timestamp"):
        try:
            prev_cursor_ts = _parse_iso_utc(str(previous_watermark["cursor_timestamp"]))
        except ValueError:
            prev_cursor_ts = None
    max_ts: dt.datetime | None = prev_cursor_ts

    for log_elem in logs_root.iter():
        if _local_name(log_elem.tag) != "log":
            continue

        index_curve = "TIME"
        null_value = "-999.25"
        curve_units: dict[str, str] = {}

        for child in log_elem:
            cname = _local_name(child.tag)
            if cname == "indexCurve" and child.text and child.text.strip():
                index_curve = child.text.strip().upper()
            elif cname == "nullValue" and child.text and child.text.strip():
                null_value = child.text.strip()
            elif cname == "logCurveInfo":
                mnem = ""
                unit = ""
                desc: str | None = None
                for sub in child:
                    sname = _local_name(sub.tag)
                    if sname == "mnemonic" and sub.text:
                        mnem = sub.text.strip()
                    elif sname == "unit" and sub.text:
                        unit = sub.text.strip()
                    elif sname == "curveDescription" and sub.text:
                        desc = sub.text.strip()
                if mnem:
                    curve_units[mnem.upper()] = unit
                    if mnem.upper() not in discovered_seen:
                        discovered_seen.add(mnem.upper())
                        discovered.append(
                            WitsmlCurveDescriptor(
                                mnemonic=mnem,
                                unit=unit,
                                curve_description=desc,
                            )
                        )

        for child in log_elem:
            if _local_name(child.tag) != "logData":
                continue
            mnemonic_list: list[str] = []
            unit_list: list[str] = []
            data_rows: list[str] = []
            for sub in child:
                sname = _local_name(sub.tag)
                if sname == "mnemonicList" and sub.text:
                    mnemonic_list = [tok.strip() for tok in sub.text.split(",")]
                elif sname == "unitList" and sub.text:
                    unit_list = [tok.strip() for tok in sub.text.split(",")]
                elif sname == "data" and sub.text:
                    data_rows.append(sub.text.strip())

            if not mnemonic_list:
                continue

            upper_mnemonics = [m.upper() for m in mnemonic_list]
            try:
                idx_pos = upper_mnemonics.index(index_curve)
            except ValueError:
                idx_pos = 0

            depth_pos: int | None = None
            for candidate_depth in ("DEPTH", "MD", "BITDEPTH", "DEPT"):
                if candidate_depth in upper_mnemonics and upper_mnemonics.index(candidate_depth) != idx_pos:
                    depth_pos = upper_mnemonics.index(candidate_depth)
                    break

            quality_pos: int | None = None
            for candidate_q in ("QUALITY", "QUAL", "QFLAG"):
                if candidate_q in upper_mnemonics:
                    quality_pos = upper_mnemonics.index(candidate_q)
                    break

            for row_text in data_rows:
                if not row_text:
                    continue
                tokens = [tok.strip() for tok in row_text.split(",")]
                if len(tokens) <= idx_pos:
                    skipped_rows += 1
                    continue
                try:
                    row_ts = _parse_iso_utc(tokens[idx_pos])
                except ValueError:
                    skipped_rows += 1
                    continue

                raw_rows_parsed += 1
                if max_ts is None or row_ts > max_ts:
                    max_ts = row_ts

                row_depth: float | None = None
                if depth_pos is not None and depth_pos < len(tokens):
                    raw_d = tokens[depth_pos]
                    if raw_d and raw_d != null_value:
                        try:
                            row_depth = float(raw_d)
                        except ValueError:
                            row_depth = None

                row_quality = "good"
                if quality_pos is not None and quality_pos < len(tokens):
                    raw_q = tokens[quality_pos].strip().lower()
                    row_quality = _QUALITY_MAP.get(raw_q, "suspect")

                for col_idx, mnem_upper in enumerate(upper_mnemonics):
                    if col_idx in {idx_pos, quality_pos}:
                        continue
                    mapping = mapping_by_mnemonic.get(mnem_upper)
                    if mapping is None or col_idx >= len(tokens):
                        continue
                    raw_val = tokens[col_idx]
                    val: float | None
                    col_quality = row_quality
                    if raw_val == "" or raw_val == null_value:
                        val = None
                        col_quality = "missing"
                    else:
                        try:
                            val = float(raw_val)
                        except ValueError:
                            skipped_rows += 1
                            continue

                    source_unit = (
                        unit_list[col_idx].strip()
                        if col_idx < len(unit_list) and unit_list[col_idx].strip()
                        else curve_units.get(mnem_upper) or mapping.unit
                    )
                    frames.append(
                        SourceFrame(
                            channel_key=mapping.channel_key,
                            ts=row_ts,
                            value=val,
                            unit=source_unit or mapping.unit,
                            quality=col_quality,
                            source_point_id=f"witsml:{mapping.channel_key}:{row_ts.isoformat()}",
                            source_ref=f"witsml.log:{mnemonic_list[col_idx]}",
                            sequence=raw_rows_parsed,
                            depth_md_si=row_depth,
                        )
                    )

    next_watermark = dict(previous_watermark or {})
    if max_ts is not None:
        next_watermark["cursor_timestamp"] = max_ts.isoformat()
        next_watermark["protocol"] = "witsml.1.4.1.1.soap_http"

    return WitsmlPollBatch(
        frames=tuple(frames),
        discovered_curves=tuple(discovered),
        next_watermark=next_watermark,
        pages_fetched=1,
        raw_rows_parsed=raw_rows_parsed,
        skipped_rows=skipped_rows,
        partial_more_available=(result_code == 2),
    )


class WitsmlSoapClient:
    """Async WITSML 1.4.1.1 SOAP/HTTP polling client with bounded retries and watermark progression."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        secrets: dict[str, str] | None = None,
        timeout_seconds: float = 10.0,
        max_retries: int = 2,
        tls_verify: bool = True,
    ) -> None:
        if tls_verify is False:
            raise ValueError("tls_verify=False is forbidden")
        self._endpoint_url = endpoint_url
        self._secrets = dict(secrets or {})
        self._timeout_seconds = max(1.0, min(timeout_seconds, 30.0))
        self._max_retries = max(0, min(max_retries, 5))
        self._tls_verify = True

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "Accept": "text/xml, application/soap+xml, application/xml",
            "SOAPAction": '"http://www.witsml.org/action/120/Store.WMLS_GetFromStore"',
            "User-Agent": "DrillAI-WITSML-Connector/1.4.1.1",
        }
        if "bearer_token" in self._secrets:
            headers["Authorization"] = f"Bearer {self._secrets['bearer_token']}"
        elif "api_key" in self._secrets:
            headers["X-API-Key"] = self._secrets["api_key"]
        return headers

    def _build_auth(self) -> httpx.BasicAuth | None:
        if "username" in self._secrets and "password" in self._secrets:
            return httpx.BasicAuth(self._secrets["username"], self._secrets["password"])
        return None

    async def _post_soap(self, envelope: str) -> str:
        headers = self._build_headers()
        auth = self._build_auth()
        last_exc: ConnectorTransportError | None = None

        for attempt in range(self._max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self._timeout_seconds),
                    follow_redirects=False,
                    verify=self._tls_verify,
                ) as client:
                    response = await client.post(
                        self._endpoint_url,
                        content=envelope.encode("utf-8"),
                        headers=headers,
                        auth=auth,
                    )
            except httpx.TimeoutException as exc:
                last_exc = ConnectorTransportError(
                    "timeout",
                    redact_sensitive_text(f"WITSML request timed out: {exc}", self._secrets)
                    or "WITSML request timed out",
                    retryable=True,
                )
            except httpx.ConnectError as exc:
                msg = str(exc)
                err_code = "tls_error" if "SSL" in msg.upper() or "CERTIFICATE" in msg.upper() else "connection_refused"
                last_exc = ConnectorTransportError(
                    err_code,
                    redact_sensitive_text(f"WITSML connection error: {exc}", self._secrets)
                    or "WITSML connection error",
                    retryable=(err_code != "tls_error"),
                )
            except httpx.HTTPError as exc:
                last_exc = ConnectorTransportError(
                    "transport_error",
                    redact_sensitive_text(f"WITSML HTTP error: {exc}", self._secrets)
                    or "WITSML HTTP error",
                    retryable=True,
                )
            else:
                if response.status_code in {401, 403}:
                    raise ConnectorTransportError(
                        "auth_failure",
                        f"WITSML server rejected credentials with HTTP {response.status_code}",
                        retryable=False,
                    )
                if 300 <= response.status_code < 400:
                    raise ConnectorTransportError(
                        "redirect_blocked",
                        f"WITSML server returned HTTP {response.status_code} redirect; redirects are blocked by SSRF policy",
                        retryable=False,
                    )
                if response.status_code in {429, 500, 502, 503, 504}:
                    last_exc = ConnectorTransportError(
                        "remote_http_5xx" if response.status_code >= 500 else "rate_limited",
                        f"WITSML server returned HTTP {response.status_code}",
                        retryable=True,
                    )
                elif response.status_code >= 400:
                    raise ConnectorTransportError(
                        "protocol_error",
                        f"WITSML server returned HTTP {response.status_code}",
                        retryable=False,
                    )
                else:
                    if len(response.content) > MAX_WITSML_XML_BYTES:
                        raise ConnectorTransportError(
                            "oversized_payload",
                            f"WITSML SOAP response exceeds {MAX_WITSML_XML_BYTES} bytes",
                            retryable=False,
                        )
                    return response.text

            if last_exc is not None and not last_exc.retryable:
                raise last_exc
            if attempt < self._max_retries:
                await asyncio.sleep(min(0.15 * (2**attempt), 1.0))

        assert last_exc is not None
        raise last_exc

    async def poll_log_batch(
        self,
        *,
        well_id: str,
        wellbore_id: str | None = None,
        operation_id: str | None = None,
        config: dict[str, Any],
        watermark: dict[str, Any] | None = None,
        max_pages: int = 3,
    ) -> WitsmlPollBatch:
        """Poll one or more pages of WITSML 1.4.1.1 log data starting at ``watermark``."""
        mappings = parse_channel_mappings(config.get("channel_mappings"))
        uid_well = str(config.get("uid_well") or well_id)
        uid_wellbore = str(config.get("uid_wellbore") or (wellbore_id or ""))
        uid_log = str(config.get("uid_log") or "")
        max_return_nodes = int(config.get("max_return_nodes") or 500)

        current_watermark = dict(watermark or {})
        all_frames: list[SourceFrame] = []
        discovered_by_mnem: dict[str, WitsmlCurveDescriptor] = {}
        seen_keys: set[tuple[str, str]] = set()
        pages_fetched = 0
        total_raw_rows = 0
        total_skipped = 0
        partial_remaining = False

        for _ in range(max(1, min(max_pages, 10))):
            cursor_ts = current_watermark.get("cursor_timestamp")
            envelope = build_get_from_store_soap_envelope(
                uid_well=uid_well,
                uid_wellbore=uid_wellbore,
                uid_log=uid_log,
                start_date_time_index=str(cursor_ts) if cursor_ts else None,
                return_elements="all",
                max_return_nodes=max_return_nodes,
            )
            soap_text = await self._post_soap(envelope)
            batch = parse_get_from_store_response(
                soap_text,
                mappings=mappings,
                well_id=well_id,
                wellbore_id=wellbore_id,
                operation_id=operation_id,
                previous_watermark=current_watermark,
            )
            pages_fetched += 1
            total_raw_rows += batch.raw_rows_parsed
            total_skipped += batch.skipped_rows
            for curve in batch.discovered_curves:
                discovered_by_mnem.setdefault(curve.mnemonic.upper(), curve)
            for frame in batch.frames:
                dedup_key = (frame.channel_key, frame.ts.isoformat())
                if dedup_key not in seen_keys:
                    seen_keys.add(dedup_key)
                    all_frames.append(frame)

            prev_cursor = current_watermark.get("cursor_timestamp")
            new_cursor = batch.next_watermark.get("cursor_timestamp")
            current_watermark = dict(batch.next_watermark)
            partial_remaining = batch.partial_more_available
            if not batch.partial_more_available or not new_cursor or new_cursor == prev_cursor:
                break

        return WitsmlPollBatch(
            frames=tuple(all_frames),
            discovered_curves=tuple(discovered_by_mnem.values()),
            next_watermark=current_watermark,
            pages_fetched=pages_fetched,
            raw_rows_parsed=total_raw_rows,
            skipped_rows=total_skipped,
            partial_more_available=partial_remaining,
        )


# ---------------------------------------------------------------------------
# Local protocol-compatible WITSML 1.4.1.1 SOAP server for harness & E2E tests
# ---------------------------------------------------------------------------


@dataclass
class WitsmlSampleRow:
    timestamp: str
    depth_md: float
    values: dict[str, float | None]
    quality: str = "good"


@dataclass
class LocalWitsmlSoapServer:
    """In-process local TCP HTTP/1.1 SOAP server speaking ``WMLS_GetFromStore`` for ``log`` objects.

    Supports authentication verification (Basic or Bearer), ``startDateTimeIndex`` filtering,
    ``maxReturnNodes`` pagination (returning ``Result=2`` when more rows remain and ``Result=1`` when
    complete), and fault injection modes (``ok``, ``auth_failure``, ``http_500``, ``malformed_xml``,
    ``slow_timeout``) for deterministic transport testing.
    """

    host: str = "127.0.0.1"
    port: int = 0
    expected_username: str | None = None
    expected_password: str | None = None
    expected_bearer_token: str | None = None
    uid_well: str = "well-01"
    uid_wellbore: str = "wb-01"
    uid_log: str = "log-time-01"
    curves: list[tuple[str, str, str]] = field(
        default_factory=lambda: [
            ("TIME", "s", "Time Index"),
            ("DEPTH", "m", "Measured Depth"),
            ("SPP", "psi", "Standpipe Pressure"),
            ("WOB", "klbf", "Weight on Bit"),
            ("HKLD", "klbf", "Hookload"),
            ("RPM", "rpm", "Surface Rotary Speed"),
            ("FLOWIN", "gpm", "Mud Flow In"),
            ("QUALITY", "unitless", "Quality Flag"),
        ]
    )
    rows: list[WitsmlSampleRow] = field(default_factory=list)
    fault_mode: str = "ok"
    page_size_override: int | None = None
    requests_received: int = 0
    last_start_index: str | None = None
    _server: asyncio.AbstractServer | None = field(default=None, init=False, repr=False)

    @property
    def endpoint_url(self) -> str:
        return f"http://{self.host}:{self.port}/witsml/store"

    def seed_default_rows(
        self,
        *,
        start: dt.datetime | None = None,
        count: int = 6,
        step_seconds: float = 5.0,
    ) -> None:
        base = (start or (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=count * step_seconds))).astimezone(dt.UTC)
        seeded: list[WitsmlSampleRow] = []
        for idx in range(count):
            ts = (base + dt.timedelta(seconds=idx * step_seconds)).isoformat()
            seeded.append(
                WitsmlSampleRow(
                    timestamp=ts,
                    depth_md=2450.0 + idx * 0.5,
                    values={
                        "SPP": 2950.0 + idx * 25.0,  # psi (will normalize to kPa via platform unit engine)
                        "WOB": 22.0 + idx * 0.5,
                        "HKLD": 185.0 + idx * 1.0,
                        "RPM": 120.0 + (idx % 3) * 2.0,
                        "FLOWIN": 650.0 + idx * 3.0,
                    },
                    quality="good",
                )
            )
        self.rows = seeded

    async def start(self) -> str:
        if not self.rows:
            self.seed_default_rows()
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        sockets = self._server.sockets or []
        if sockets:
            self.port = int(sockets[0].getsockname()[1])
        return self.endpoint_url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> LocalWitsmlSoapServer:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()

    def _check_auth(self, headers: dict[str, str]) -> bool:
        if self.fault_mode == "auth_failure":
            return False
        auth_header = headers.get("authorization", "")
        if self.expected_bearer_token:
            return auth_header == f"Bearer {self.expected_bearer_token}"
        if self.expected_username is not None and self.expected_password is not None:
            if not auth_header.startswith("Basic "):
                return False
            try:
                decoded = base64.b64decode(auth_header[6:].strip()).decode("utf-8")
            except Exception:
                return False
            return decoded == f"{self.expected_username}:{self.expected_password}"
        return True

    def _render_logs_xml(self, start_dt: dt.datetime | None, max_nodes: int) -> tuple[str, int]:
        filtered: list[WitsmlSampleRow] = []
        for row in self.rows:
            row_dt = _parse_iso_utc(row.timestamp)
            if start_dt is not None and row_dt <= start_dt:
                continue
            filtered.append(row)

        limit = self.page_size_override or max_nodes
        has_more = len(filtered) > limit
        page = filtered[:limit]
        result_code = 2 if has_more else 1

        curve_infos = []
        mnemonics = []
        units = []
        for mnem, unit, desc in self.curves:
            mnemonics.append(mnem)
            units.append(unit)
            curve_infos.append(
                f"<logCurveInfo><mnemonic>{html.escape(mnem)}</mnemonic>"
                f"<unit>{html.escape(unit)}</unit>"
                f"<curveDescription>{html.escape(desc)}</curveDescription></logCurveInfo>"
            )

        data_elements = []
        for row in page:
            cells: list[str] = []
            for mnem, _, _ in self.curves:
                if mnem == "TIME":
                    cells.append(row.timestamp)
                elif mnem == "DEPTH":
                    cells.append(f"{row.depth_md:.3f}")
                elif mnem == "QUALITY":
                    cells.append(row.quality)
                else:
                    val = row.values.get(mnem)
                    cells.append("-999.25" if val is None else f"{val}")
            data_elements.append(f"<data>{','.join(cells)}</data>")

        logs_xml = (
            '<logs xmlns="http://www.witsml.org/schemas/1series" version="1.4.1.1">'
            f'<log uidWell="{html.escape(self.uid_well)}" uidWellbore="{html.escape(self.uid_wellbore)}" uid="{html.escape(self.uid_log)}">'
            "<nameWell>Harness Well</nameWell>"
            "<nameWellbore>Main Wellbore</nameWellbore>"
            "<name>Time Drilling Log</name>"
            "<indexType>date time</indexType>"
            "<indexCurve>TIME</indexCurve>"
            "<nullValue>-999.25</nullValue>"
            f"{''.join(curve_infos)}"
            "<logData>"
            f"<mnemonicList>{','.join(mnemonics)}</mnemonicList>"
            f"<unitList>{','.join(units)}</unitList>"
            f"{''.join(data_elements)}"
            "</logData>"
            "</log>"
            "</logs>"
        )
        return logs_xml, result_code

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            head_text = raw_head.decode("latin-1")
            lines = head_text.split("\r\n")
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            content_length = int(headers.get("content-length", "0"))
            body_bytes = b""
            if content_length > 0:
                body_bytes = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0)
            body_text = body_bytes.decode("utf-8", errors="replace")

            self.requests_received += 1

            if self.fault_mode == "slow_timeout":
                await asyncio.sleep(3.0)

            if not self._check_auth(headers):
                await self._write_response(writer, 401, "Unauthorized", "text/plain")
                return

            if self.fault_mode == "http_500":
                await self._write_response(writer, 500, "Internal WITSML Store Error", "text/plain")
                return

            if self.fault_mode == "malformed_xml":
                await self._write_response(
                    writer,
                    200,
                    "<soapenv:Envelope><broken><unclosed>",
                    "text/xml; charset=utf-8",
                )
                return

            start_dt: dt.datetime | None = None
            max_nodes = 500
            try:
                env = ET.fromstring(body_text)
                for elem in env.iter():
                    lname = _local_name(elem.tag)
                    if lname == "QueryIn" and elem.text:
                        qroot = ET.fromstring(elem.text)
                        for qelem in qroot.iter():
                            if _local_name(qelem.tag) == "startDateTimeIndex" and qelem.text:
                                self.last_start_index = qelem.text.strip()
                                start_dt = _parse_iso_utc(self.last_start_index)
                    elif lname == "OptionsIn" and elem.text:
                        for part in elem.text.split(";"):
                            if part.strip().startswith("maxReturnNodes="):
                                max_nodes = int(part.split("=", 1)[1].strip())
            except Exception:
                start_dt = None

            logs_xml, result_code = self._render_logs_xml(start_dt, max_nodes)
            soap_resp = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
                'xmlns:wmls="http://www.witsml.org/message/120">'
                "<soapenv:Body>"
                "<wmls:WMLS_GetFromStoreResponse>"
                f"<Result>{result_code}</Result>"
                f"<XMLout>{html.escape(logs_xml)}</XMLout>"
                "<SuppMsgOut/>"
                "</wmls:WMLS_GetFromStoreResponse>"
                "</soapenv:Body>"
                "</soapenv:Envelope>"
            )
            await self._write_response(writer, 200, soap_resp, "text/xml; charset=utf-8")
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _write_response(
        writer: asyncio.StreamWriter, status: int, body: str, content_type: str
    ) -> None:
        reason = {200: "OK", 401: "Unauthorized", 500: "Internal Server Error"}.get(
            status, "Error"
        )
        payload = body.encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + payload)
        await writer.drain()
