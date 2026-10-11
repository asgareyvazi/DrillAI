"""WITSML 1.4.1.1 SOAP/HTTP read-only polling client and local protocol-compatible test server.

Implements the ``witsml.1.4.1.1.soap_http`` profile:
- SOAP 1.1 ``WMLS_GetVersion``, ``WMLS_GetCap``, and ``WMLS_GetFromStore`` over HTTP/HTTPS POST
- Standards-based HTTP Basic or Bearer authentication
- Full-payload XML entity/DTD/depth/complexity protection on both SOAP envelope and ``XMLout``
- Namespace-aware parsing of ``<logs><log><logCurveInfo>`` and ``<logData>``
- Timezone-aware UTC index timestamp parsing and inclusive ``startDateTimeIndex`` watermark advancing
  with per-timestamp curve completion tracking so ``max_points_per_poll`` caps and multi-page boundaries
  never skip unprocessed measurements
- Exact source-record disposition accounting (`ProtocolPollDisposition`)
- ``LocalWitsmlSoapServer``: a real TCP/HTTP 1.1 SOAP server for protocol-level integration and E2E testing
  (explicitly distinct from external commercial vendor certification).
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import html
import io
import math
import re
import ssl
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

import httpx

from drillai.telemetry.adapters import SourceFrame
from drillai.telemetry.connectors import (
    ChannelMappingSpec,
    ConnectorTransportError,
    ProtocolPollDisposition,
    enforce_connect_time_ssrf,
    parse_channel_mappings,
    redact_sensitive_text,
    register_harness_endpoint,
    unregister_harness_endpoint,
)

MAX_WITSML_XML_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_XML_DEPTH = 32
MAX_XML_ELEMENTS = 50_000

_FORBIDDEN_XML_DECL_RE = re.compile(
    r"<!\s*(?:DOCTYPE|ENTITY|ELEMENT|ATTLIST|NOTATION)\b",
    re.IGNORECASE,
)
_CUSTOM_ENTITY_REF_RE = re.compile(
    r"&(?!(?:lt|gt|amp|apos|quot|#\d+|#x[0-9a-fA-F]+);)[A-Za-z_][A-Za-z0-9._-]*;"
)

_QUALITY_MAP: dict[str, str] = {
    "good": "good",
    "ok": "good",
    "valid": "good",
    "1": "good",
    "suspect": "suspect",
    "questionable": "queue_questionable",
    "doubtful": "suspect",
    "bad": "bad",
    "invalid": "bad",
    "error": "bad",
    "missing": "missing",
    "null": "missing",
}


def _normalize_quality(raw_q: str) -> str:
    val = _QUALITY_MAP.get(raw_q.strip().lower(), "suspect")
    return "suspect" if val == "queue_questionable" else val


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
    duplicates_suppressed: int = 0
    disposition: ProtocolPollDisposition = field(default_factory=ProtocolPollDisposition)


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.split("}", 1)[1]
    if ":" in tag:
        return tag.split(":", 1)[1]
    return tag


def _ensure_safe_xml(xml_text: str) -> None:
    """Validate the entire XML payload against DTD/XXE/entity declarations before parsing."""
    raw_bytes = xml_text.encode("utf-8")
    if len(raw_bytes) > MAX_WITSML_XML_BYTES:
        raise ConnectorTransportError(
            "oversized_payload",
            f"WITSML XML payload exceeds {MAX_WITSML_XML_BYTES} bytes",
            retryable=False,
        )
    if _FORBIDDEN_XML_DECL_RE.search(xml_text):
        raise ConnectorTransportError(
            "malformed_payload",
            "WITSML XML response contains forbidden DOCTYPE or ENTITY declarations",
            retryable=False,
        )
    if _CUSTOM_ENTITY_REF_RE.search(xml_text):
        raise ConnectorTransportError(
            "malformed_payload",
            "WITSML XML response contains forbidden custom entity references",
            retryable=False,
        )


def _parse_safe_xml(xml_text: str, *, context: str = "WITSML XML") -> ET.Element:
    """Parse XML after full-payload DTD/entity checks while enforcing depth and element bounds."""
    _ensure_safe_xml(xml_text)
    depth = 0
    elements = 0
    root: ET.Element | None = None
    try:
        for event, elem in ET.iterparse(io.StringIO(xml_text), events=("start", "end")):
            if event == "start":
                depth += 1
                elements += 1
                if depth > MAX_XML_DEPTH:
                    raise ConnectorTransportError(
                        "malformed_payload",
                        f"{context} exceeds maximum nesting depth of {MAX_XML_DEPTH}",
                        retryable=False,
                    )
                if elements > MAX_XML_ELEMENTS:
                    raise ConnectorTransportError(
                        "oversized_payload",
                        f"{context} exceeds maximum element count of {MAX_XML_ELEMENTS}",
                        retryable=False,
                    )
                if root is None:
                    root = elem
            else:
                depth -= 1
    except ConnectorTransportError:
        raise
    except ET.ParseError as exc:
        raise ConnectorTransportError(
            "malformed_payload",
            f"invalid {context}: {exc}",
            retryable=False,
        ) from exc
    if root is None:
        raise ConnectorTransportError(
            "malformed_payload",
            f"empty {context}",
            retryable=False,
        )
    return root


def _parse_iso_utc(raw: str) -> dt.datetime:
    if not isinstance(raw, str):
        raise ValueError("timestamp must be an ISO-8601 string")
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("empty timestamp")
    if cleaned.endswith("Z") or cleaned.endswith("z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    utc_dt = parsed.astimezone(dt.UTC)
    if utc_dt < dt.datetime(1970, 1, 2, tzinfo=dt.UTC) or utc_dt > dt.datetime(2100, 1, 1, tzinfo=dt.UTC):
        raise ValueError(f"timestamp {raw!r} is outside operational UTC bounds")
    return utc_dt


def build_get_version_soap_envelope() -> str:
    """Build a WITSML 1.4.1.1 ``WMLS_GetVersion`` SOAP 1.1 envelope."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:wmls="http://www.witsml.org/message/120">'
        "<soapenv:Header/>"
        "<soapenv:Body>"
        "<wmls:WMLS_GetVersion/>"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    )


def build_get_cap_soap_envelope(options_in: str = "dataVersion=1.4.1.1") -> str:
    """Build a WITSML 1.4.1.1 ``WMLS_GetCap`` SOAP 1.1 envelope."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:wmls="http://www.witsml.org/message/120">'
        "<soapenv:Header/>"
        "<soapenv:Body>"
        "<wmls:WMLS_GetCap>"
        f"<OptionsIn>{html.escape(options_in)}</OptionsIn>"
        "</wmls:WMLS_GetCap>"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    )


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
        "<nameWell/><nameWellbore/><name/><indexType/><indexCurve/>"
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


def compute_witsml_next_watermark(
    previous_watermark: dict[str, Any] | None,
    emitted_frames: tuple[SourceFrame, ...] | list[SourceFrame],
    *,
    has_more: bool = False,
) -> dict[str, Any]:
    """Compute a safe WITSML watermark exclusively from frames that were actually emitted."""
    prev = dict(previous_watermark or {})
    if not emitted_frames:
        if "protocol" not in prev and prev.get("cursor_timestamp"):
            prev["protocol"] = "witsml.1.4.1.1.soap_http"
        prev["has_more_deferred"] = bool(has_more)
        return prev

    prev_cursor_ts: dt.datetime | None = None
    if prev.get("cursor_timestamp"):
        try:
            prev_cursor_ts = _parse_iso_utc(str(prev["cursor_timestamp"]))
        except ValueError:
            prev_cursor_ts = None

    max_ts = max(f.ts for f in emitted_frames)
    if prev_cursor_ts is not None and prev_cursor_ts > max_ts:
        max_ts = prev_cursor_ts

    completed_ids: set[str] = set()
    completed_vals: dict[str, float | None] = {}
    if prev_cursor_ts is not None and max_ts == prev_cursor_ts:
        for pid in prev.get("completed_at_cursor") or []:
            completed_ids.add(str(pid))
        for pid, pval in (prev.get("completed_values_at_cursor") or {}).items():
            completed_vals[str(pid)] = float(pval) if pval is not None else None

    for frame in emitted_frames:
        if frame.ts == max_ts and frame.source_point_id:
            completed_ids.add(frame.source_point_id)
            completed_vals[frame.source_point_id] = frame.value

    return {
        "protocol": "witsml.1.4.1.1.soap_http",
        "cursor_timestamp": max_ts.isoformat(),
        "completed_at_cursor": sorted(completed_ids),
        "completed_values_at_cursor": completed_vals,
        "has_more_deferred": bool(has_more),
    }


def parse_get_from_store_response(
    soap_xml: str,
    *,
    mappings: list[ChannelMappingSpec],
    well_id: str,
    wellbore_id: str | None = None,
    operation_id: str | None = None,
    previous_watermark: dict[str, Any] | None = None,
    max_frames: int | None = None,
    subscribed_channels: set[str] | None = None,
    expected_uid_well: str | None = None,
    expected_uid_wellbore: str | None = None,
    expected_uid_log: str | None = None,
) -> WitsmlPollBatch:
    """Parse a WITSML 1.4.1.1 ``WMLS_GetFromStoreResponse`` or direct ``<logs>`` document.

    Enforces ``max_frames`` and ``subscribed_channels`` during parsing so that ``next_watermark``
    never advances past any frame that was not emitted in ``frames``.
    """
    outer = _parse_safe_xml(soap_xml, context="WITSML SOAP XML")
    disposition = ProtocolPollDisposition(pages_or_messages_received=1)

    fault_string: str | None = None
    result_code = 1
    found_result = False
    supp_msg_out = ""
    xml_out_text: str | None = None

    if _local_name(outer.tag) == "logs":
        logs_root = outer
    else:
        if _local_name(outer.tag) != "Envelope":
            raise ConnectorTransportError(
                "protocol_error",
                f"expected SOAP Envelope or WITSML <logs> root element, got {_local_name(outer.tag)!r}",
                retryable=False,
            )
        for elem in outer.iter():
            lname = _local_name(elem.tag)
            if lname == "faultstring" and elem.text:
                fault_string = elem.text.strip()
            elif lname == "Result" and elem.text:
                found_result = True
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
        if not found_result:
            raise ConnectorTransportError(
                "protocol_error",
                "WITSML SOAP response missing mandatory <Result> element",
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
                duplicates_suppressed=0,
                disposition=disposition,
            )
        logs_root = _parse_safe_xml(xml_out_text, context="WITSML XMLout payload")
        if _local_name(logs_root.tag) != "logs":
            raise ConnectorTransportError(
                "protocol_error",
                f"expected WITSML <logs> root in XMLout, got {_local_name(logs_root.tag)!r}",
                retryable=False,
            )

    mapping_by_mnemonic = {m.source_mnemonic.upper(): m for m in mappings}
    discovered: list[WitsmlCurveDescriptor] = []
    discovered_seen: set[str] = set()
    frames: list[SourceFrame] = []
    raw_rows_parsed = 0
    skipped_rows = 0
    duplicates_suppressed = 0

    prev_wm = dict(previous_watermark or {})
    prev_cursor_ts: dt.datetime | None = None
    if prev_wm.get("cursor_timestamp"):
        try:
            prev_cursor_ts = _parse_iso_utc(str(prev_wm["cursor_timestamp"]))
        except ValueError:
            prev_cursor_ts = None

    has_completed_set = "completed_at_cursor" in prev_wm
    completed_at_cursor: set[str] = {str(x) for x in (prev_wm.get("completed_at_cursor") or [])}
    completed_values: dict[str, float | None] = {
        str(k): (float(v) if v is not None else None)
        for k, v in (prev_wm.get("completed_values_at_cursor") or {}).items()
    }
    seen_in_batch: dict[str, tuple[bool, float | None]] = {}

    for log_elem in logs_root.iter():
        if _local_name(log_elem.tag) != "log":
            continue

        actual_uid_well = (log_elem.attrib.get("uidWell") or "").strip()
        actual_uid_wellbore = (log_elem.attrib.get("uidWellbore") or "").strip()
        actual_uid_log = (log_elem.attrib.get("uid") or "").strip()
        if expected_uid_well and actual_uid_well and actual_uid_well != expected_uid_well:
            raise ConnectorTransportError(
                "protocol_error",
                f"WITSML log uidWell {actual_uid_well!r} does not match expected {expected_uid_well!r}",
                retryable=False,
            )
        if expected_uid_wellbore and actual_uid_wellbore and actual_uid_wellbore != expected_uid_wellbore:
            raise ConnectorTransportError(
                "protocol_error",
                f"WITSML log uidWellbore {actual_uid_wellbore!r} does not match expected {expected_uid_wellbore!r}",
                retryable=False,
            )
        if expected_uid_log and actual_uid_log and actual_uid_log != expected_uid_log:
            raise ConnectorTransportError(
                "protocol_error",
                f"WITSML log uid {actual_uid_log!r} does not match expected {expected_uid_log!r}",
                retryable=False,
            )

        index_type = "date time"
        index_curve = "TIME"
        null_value = "-999.25"
        curve_units: dict[str, str] = {}

        for child in log_elem:
            cname = _local_name(child.tag)
            if cname == "indexType" and child.text and child.text.strip():
                index_type = child.text.strip().lower()
            elif cname == "indexCurve" and child.text and child.text.strip():
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

        if "depth" in index_type and "time" not in index_type:
            raise ConnectorTransportError(
                "protocol_error",
                f"unsupported WITSML log indexType {index_type!r}; profile requires date time index",
                retryable=False,
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
            if unit_list and len(unit_list) != len(mnemonic_list):
                raise ConnectorTransportError(
                    "malformed_payload",
                    f"WITSML <unitList> length ({len(unit_list)}) does not match <mnemonicList> length ({len(mnemonic_list)})",
                    retryable=False,
                )

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
            depth_unit = (
                unit_list[depth_pos].strip().lower()
                if (depth_pos is not None and depth_pos < len(unit_list))
                else "m"
            )

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
                    disposition.source_records_discovered += 1
                    disposition.rejected_malformed_timestamp += 1
                    disposition.record_channel_outcome(index_curve, "rejected")
                    disposition.record_rejection(
                        reason="malformed_timestamp",
                        source_mnemonic=index_curve,
                        raw_value=row_text[:80],
                    )
                    continue
                try:
                    row_ts = _parse_iso_utc(tokens[idx_pos])
                except ValueError:
                    skipped_rows += 1
                    disposition.source_records_discovered += 1
                    disposition.rejected_malformed_timestamp += 1
                    disposition.record_channel_outcome(index_curve, "rejected")
                    disposition.record_rejection(
                        reason="malformed_timestamp",
                        source_mnemonic=index_curve,
                        index_repr=tokens[idx_pos],
                    )
                    continue

                row_depth: float | None = None
                if depth_pos is not None and depth_pos < len(tokens):
                    raw_d = tokens[depth_pos]
                    if raw_d and raw_d != null_value:
                        try:
                            parsed_d = float(raw_d)
                            if math.isfinite(parsed_d):
                                if depth_unit in ("ft", "feet", "foot", "ft_us"):
                                    row_depth = parsed_d * 0.3048
                                else:
                                    row_depth = parsed_d
                        except ValueError:
                            row_depth = None

                row_quality = "good"
                if quality_pos is not None and quality_pos < len(tokens):
                    raw_q = tokens[quality_pos].strip().lower()
                    if raw_q and raw_q != null_value:
                        row_quality = _normalize_quality(raw_q)

                row_had_active_cells = False

                for col_idx, mnem_upper in enumerate(upper_mnemonics):
                    if col_idx in {idx_pos, quality_pos}:
                        continue
                    if col_idx == depth_pos and mnem_upper not in mapping_by_mnemonic:
                        continue

                    disposition.source_records_discovered += 1
                    mapping = mapping_by_mnemonic.get(mnem_upper)
                    if mapping is None:
                        disposition.unmapped_skipped += 1
                        disposition.record_channel_outcome(mnem_upper, "unmapped_skipped")
                        continue

                    if subscribed_channels and mapping.channel_key not in subscribed_channels:
                        disposition.unsubscribed_skipped += 1
                        disposition.record_channel_outcome(mnem_upper, "unsubscribed_skipped")
                        continue

                    if col_idx >= len(tokens):
                        skipped_rows += 1
                        disposition.rejected_invalid_value += 1
                        disposition.record_channel_outcome(mnem_upper, "rejected")
                        disposition.record_rejection(
                            reason="missing_column_token",
                            source_mnemonic=mnem_upper,
                            index_repr=row_ts.isoformat(),
                        )
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
                            disposition.rejected_invalid_value += 1
                            disposition.record_channel_outcome(mnem_upper, "rejected")
                            disposition.record_rejection(
                                reason="invalid_numeric_value",
                                source_mnemonic=mnem_upper,
                                index_repr=row_ts.isoformat(),
                                raw_value=raw_val,
                            )
                            continue
                        if not math.isfinite(val):
                            skipped_rows += 1
                            disposition.rejected_non_finite_value += 1
                            disposition.record_channel_outcome(mnem_upper, "rejected")
                            disposition.record_rejection(
                                reason="non_finite_value",
                                source_mnemonic=mnem_upper,
                                index_repr=row_ts.isoformat(),
                                raw_value=raw_val,
                            )
                            continue

                    point_id = f"witsml:{mapping.channel_key}:{row_ts.isoformat()}"

                    # Check inclusive watermark suppression
                    if prev_cursor_ts is not None:
                        if row_ts < prev_cursor_ts:
                            disposition.watermark_suppressed += 1
                            disposition.record_channel_outcome(mnem_upper, "watermark_suppressed")
                            continue
                        if row_ts == prev_cursor_ts:
                            if not has_completed_set:
                                disposition.watermark_suppressed += 1
                                disposition.record_channel_outcome(mnem_upper, "watermark_suppressed")
                                continue
                            if point_id in completed_at_cursor and (
                                point_id not in completed_values or completed_values.get(point_id) == val
                            ):
                                disposition.watermark_suppressed += 1
                                disposition.record_channel_outcome(mnem_upper, "watermark_suppressed")
                                continue

                    # Check in-batch exact duplicate (same point_id and same value)
                    prev_seen = seen_in_batch.get(point_id)
                    if prev_seen is not None and prev_seen == (True, val):
                        duplicates_suppressed += 1
                        disposition.in_batch_duplicates += 1
                        disposition.record_channel_outcome(mnem_upper, "in_batch_duplicates")
                        continue

                    # Enforce max_frames cap BEFORE emitting or advancing watermark
                    if max_frames is not None and len(frames) >= max_frames:
                        disposition.deferred_over_cap += 1
                        disposition.truncated_by_limit = True
                        disposition.record_channel_outcome(mnem_upper, "deferred_over_cap")
                        continue

                    row_had_active_cells = True
                    seen_in_batch[point_id] = (True, val)
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
                            source_point_id=point_id,
                            source_ref=f"witsml.log:{mnemonic_list[col_idx]}",
                            sequence=len(frames) + 1,
                            depth_md_si=row_depth,
                        )
                    )
                    disposition.mapped_frames_emitted += 1
                    disposition.record_channel_outcome(mnem_upper, "emitted")

                if row_had_active_cells:
                    raw_rows_parsed += 1

    partial_more = (result_code == 2) or disposition.truncated_by_limit
    disposition.partial_more_available = partial_more
    next_watermark = compute_witsml_next_watermark(
        previous_watermark,
        frames,
        has_more=partial_more,
    )

    return WitsmlPollBatch(
        frames=tuple(frames),
        discovered_curves=tuple(discovered),
        next_watermark=next_watermark,
        pages_fetched=1,
        raw_rows_parsed=raw_rows_parsed,
        skipped_rows=skipped_rows,
        partial_more_available=partial_more,
        duplicates_suppressed=duplicates_suppressed,
        disposition=disposition,
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
        ssl_context: ssl.SSLContext | bool = True,
    ) -> None:
        if tls_verify is False or ssl_context is False:
            raise ValueError("tls_verify=False is forbidden")
        self._endpoint_url = endpoint_url
        self._secrets = dict(secrets or {})
        self._timeout_seconds = max(1.0, min(timeout_seconds, 30.0))
        self._max_retries = max(0, min(max_retries, 5))
        self._tls_verify: ssl.SSLContext | bool = ssl_context if isinstance(ssl_context, ssl.SSLContext) else True

    def _build_headers(self, soap_action: str = "Store.WMLS_GetFromStore") -> dict[str, str]:
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "Accept": "text/xml, application/soap+xml, application/xml",
            "SOAPAction": f'"http://www.witsml.org/action/120/{soap_action}"',
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

    async def _post_soap(self, envelope: str, *, soap_action: str = "Store.WMLS_GetFromStore") -> str:
        auth = self._build_auth()
        last_exc: ConnectorTransportError | None = None

        for attempt in range(self._max_retries + 1):
            pinned = enforce_connect_time_ssrf(self._endpoint_url, "witsml.1.4.1.1.soap_http")
            headers = self._build_headers(soap_action=soap_action)
            headers["Host"] = pinned.host_header
            extensions: dict[str, Any] = {}
            if pinned.scheme == "https":
                extensions["sni_hostname"] = pinned.sni_hostname
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self._timeout_seconds),
                    follow_redirects=False,
                    verify=self._tls_verify,
                    trust_env=False,
                ) as client:
                    response = await client.post(
                        pinned.connect_url,
                        content=envelope.encode("utf-8"),
                        headers=headers,
                        auth=auth,
                        extensions=extensions,
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
                err_code = (
                    "tls_error"
                    if "SSL" in msg.upper() or "CERTIFICATE" in msg.upper()
                    else "connection_refused"
                )
                last_exc = ConnectorTransportError(
                    err_code,
                    redact_sensitive_text(f"WITSML connection error: {exc}", self._secrets)
                    or "WITSML connection error",
                    retryable=(err_code != "tls_error"),
                )
            except httpx.HTTPError as exc:
                last_exc = ConnectorTransportError(
                    "transport_error",
                    redact_sensitive_text(f"WITSML HTTP error: {exc}", self._secrets) or "WITSML HTTP error",
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

    async def get_version(self) -> str:
        """Execute ``WMLS_GetVersion`` and return the server's supported WITSML version string."""
        soap_text = await self._post_soap(
            build_get_version_soap_envelope(), soap_action="Store.WMLS_GetVersion"
        )
        outer = _parse_safe_xml(soap_text, context="WITSML GetVersion XML")
        for elem in outer.iter():
            if _local_name(elem.tag) == "Result" and elem.text and elem.text.strip():
                return elem.text.strip()
        raise ConnectorTransportError(
            "protocol_error",
            "WITSML WMLS_GetVersion response did not contain a Result element",
            retryable=False,
        )

    async def get_cap(self) -> dict[str, Any]:
        """Execute ``WMLS_GetCap`` and return parsed server capabilities."""
        soap_text = await self._post_soap(build_get_cap_soap_envelope(), soap_action="Store.WMLS_GetCap")
        outer = _parse_safe_xml(soap_text, context="WITSML GetCap XML")
        result_code = 1
        cap_xml = ""
        for elem in outer.iter():
            lname = _local_name(elem.tag)
            if lname == "Result" and elem.text:
                try:
                    result_code = int(elem.text.strip())
                except ValueError:
                    result_code = -1
            elif lname == "CapabilitiesOut" and elem.text:
                cap_xml = elem.text
        if result_code <= 0:
            raise ConnectorTransportError(
                "protocol_error",
                f"WITSML WMLS_GetCap failed with Result={result_code}",
                retryable=False,
            )
        functions: list[str] = []
        schema_version = "1.4.1.1"
        if cap_xml.strip():
            cap_root = _parse_safe_xml(cap_xml, context="WITSML CapabilitiesOut XML")
            schema_version = cap_root.attrib.get("version") or schema_version
            for elem in cap_root.iter():
                if _local_name(elem.tag) == "function" and elem.attrib.get("name"):
                    functions.append(elem.attrib["name"])
        return {"version": schema_version, "functions": functions}

    async def poll_log_batch(
        self,
        *,
        well_id: str,
        wellbore_id: str | None = None,
        operation_id: str | None = None,
        config: dict[str, Any],
        watermark: dict[str, Any] | None = None,
        max_pages: int = 3,
        max_frames: int | None = None,
        subscribed_channels: set[str] | None = None,
    ) -> WitsmlPollBatch:
        """Poll one or more pages of WITSML 1.4.1.1 log data starting at ``watermark``.

        Guarantees that ``next_watermark`` never advances past ``max_frames`` and that all
        remaining frames are either deferred to the next poll or fetched on subsequent pages.
        """
        mappings = parse_channel_mappings(config.get("channel_mappings"))
        uid_well = str(config.get("uid_well") or config.get("well_uid") or well_id)
        uid_wellbore = str(config.get("uid_wellbore") or config.get("wellbore_uid") or (wellbore_id or ""))
        uid_log = str(config.get("uid_log") or config.get("log_uid") or "")
        expected_uid_well = str(config.get("uid_well") or config.get("well_uid") or "").strip() or None
        expected_uid_wellbore = (
            str(config.get("uid_wellbore") or config.get("wellbore_uid") or "").strip() or None
        )
        expected_uid_log = str(config.get("uid_log") or config.get("log_uid") or "").strip() or None
        max_return_nodes = int(config.get("max_return_nodes") or 500)
        effective_max_frames = (
            int(max_frames)
            if max_frames is not None
            else (int(config["max_points_per_poll"]) if "max_points_per_poll" in config else None)
        )

        current_watermark = dict(watermark or {})
        all_frames: list[SourceFrame] = []
        discovered_by_mnem: dict[str, WitsmlCurveDescriptor] = {}
        pages_fetched = 0
        total_raw_rows = 0
        total_skipped = 0
        total_dups = 0
        partial_remaining = False
        combined_disp = ProtocolPollDisposition()

        for _ in range(max(1, min(max_pages, 10))):
            remaining_frame_budget = (
                max(0, effective_max_frames - len(all_frames)) if effective_max_frames is not None else None
            )
            if remaining_frame_budget is not None and remaining_frame_budget == 0:
                partial_remaining = True
                break

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
            page_batch = parse_get_from_store_response(
                soap_text,
                mappings=mappings,
                well_id=well_id,
                wellbore_id=wellbore_id,
                operation_id=operation_id,
                previous_watermark=current_watermark,
                max_frames=remaining_frame_budget,
                subscribed_channels=subscribed_channels,
                expected_uid_well=expected_uid_well,
                expected_uid_wellbore=expected_uid_wellbore,
                expected_uid_log=expected_uid_log,
            )
            pages_fetched += 1
            total_raw_rows += page_batch.raw_rows_parsed
            total_skipped += page_batch.skipped_rows
            total_dups += page_batch.duplicates_suppressed

            pdisp = page_batch.disposition
            combined_disp.pages_or_messages_received += pdisp.pages_or_messages_received
            combined_disp.source_records_discovered += pdisp.source_records_discovered
            combined_disp.mapped_frames_emitted += pdisp.mapped_frames_emitted
            combined_disp.unmapped_skipped += pdisp.unmapped_skipped
            combined_disp.unsubscribed_skipped += pdisp.unsubscribed_skipped
            combined_disp.watermark_suppressed += pdisp.watermark_suppressed
            combined_disp.in_batch_duplicates += pdisp.in_batch_duplicates
            combined_disp.deferred_over_cap += pdisp.deferred_over_cap
            combined_disp.rejected_malformed_timestamp += pdisp.rejected_malformed_timestamp
            combined_disp.rejected_unsupported_index += pdisp.rejected_unsupported_index
            combined_disp.rejected_invalid_value += pdisp.rejected_invalid_value
            combined_disp.rejected_non_finite_value += pdisp.rejected_non_finite_value
            combined_disp.rejected_unknown_channel += pdisp.rejected_unknown_channel
            combined_disp.truncated_by_limit = combined_disp.truncated_by_limit or pdisp.truncated_by_limit
            for rej in pdisp.protocol_rejections:
                if len(combined_disp.protocol_rejections) < 50:
                    combined_disp.protocol_rejections.append(rej)
            for ch_key, ch_counts in pdisp.per_source_channel.items():
                dest = combined_disp.per_source_channel.setdefault(
                    ch_key,
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
                for bucket_k, bucket_v in ch_counts.items():
                    dest[bucket_k] = dest.get(bucket_k, 0) + bucket_v

            for curve in page_batch.discovered_curves:
                discovered_by_mnem.setdefault(curve.mnemonic.upper(), curve)
            for frame in page_batch.frames:
                all_frames.append(frame)

            prev_cursor = current_watermark.get("cursor_timestamp")
            prev_completed = list(current_watermark.get("completed_at_cursor") or [])
            new_cursor = page_batch.next_watermark.get("cursor_timestamp")
            new_completed = list(page_batch.next_watermark.get("completed_at_cursor") or [])
            current_watermark = dict(page_batch.next_watermark)
            partial_remaining = page_batch.partial_more_available

            if pdisp.truncated_by_limit:
                break
            if not page_batch.partial_more_available:
                break
            if not new_cursor or (new_cursor == prev_cursor and new_completed == prev_completed):
                break

        combined_disp.partial_more_available = partial_remaining
        current_watermark["has_more_deferred"] = bool(partial_remaining)

        return WitsmlPollBatch(
            frames=tuple(all_frames),
            discovered_curves=tuple(discovered_by_mnem.values()),
            next_watermark=current_watermark,
            pages_fetched=pages_fetched,
            raw_rows_parsed=total_raw_rows,
            skipped_rows=total_skipped,
            partial_more_available=partial_remaining,
            duplicates_suppressed=total_dups,
            disposition=combined_disp,
        )


# ---------------------------------------------------------------------------
# Local protocol-compatible WITSML 1.4.1.1 SOAP server for harness & E2E tests
# ---------------------------------------------------------------------------


@dataclass
class WitsmlSampleRow:
    timestamp: str
    depth_md: float
    values: dict[str, float | int | str | None]
    quality: str = "good"


@dataclass
class LocalWitsmlSoapServer:
    """In-process local TCP HTTP/1.1 SOAP server speaking ``WMLS_GetVersion``, ``WMLS_GetCap``,
    and ``WMLS_GetFromStore`` for ``log`` objects.

    Supports authentication verification (Basic or Bearer), inclusive ``startDateTimeIndex`` filtering,
    ``maxReturnNodes`` pagination (returning ``Result=2`` when more rows remain and ``Result=1`` when
    complete), and fault injection modes (``ok``, ``auth_failure``, ``http_500``, ``malformed_xml``,
    ``xxe_entity``, ``slow_timeout``) for deterministic transport testing.
    """

    host: str = "127.0.0.1"
    port: int = 0
    expected_username: str | None = None
    expected_password: str | None = None
    expected_bearer_token: str | None = None
    uid_well: str = "well-01"
    uid_wellbore: str = "wb-01"
    uid_log: str = "log-time-01"
    index_type: str = "date time"
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
        base = (start or (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=count * step_seconds))).astimezone(
            dt.UTC
        )
        seeded: list[WitsmlSampleRow] = []
        for idx in range(count):
            ts = (base + dt.timedelta(seconds=idx * step_seconds)).isoformat()
            seeded.append(
                WitsmlSampleRow(
                    timestamp=ts,
                    depth_md=2450.0 + idx * 0.5,
                    values={
                        "SPP": 2950.0 + idx * 25.0,
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
        register_harness_endpoint(self.host, self.port)
        return self.endpoint_url

    async def stop(self) -> None:
        if self._server is not None:
            unregister_harness_endpoint(self.host, self.port)
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
        """Render WITSML 1.4.1.1 ``<logs>`` with standard inclusive ``startDateTimeIndex`` semantics."""
        filtered: list[WitsmlSampleRow] = []
        boundary_count = 0
        for row in self.rows:
            try:
                row_dt = _parse_iso_utc(row.timestamp)
            except ValueError:
                # Include intentionally malformed timestamp rows so parser rejection tests can observe them
                filtered.append(row)
                continue
            if start_dt is not None and row_dt < start_dt:
                continue
            if start_dt is not None and row_dt == start_dt:
                boundary_count += 1
            filtered.append(row)

        limit = self.page_size_override or max_nodes
        # When resuming at start_dt (inclusive), include the boundary row(s) at start_dt plus up to `limit`
        # subsequent rows so a completed boundary row does not consume the new-row page budget while any
        # unfinished curves or duplicate-timestamp rows at start_dt remain present in the response.
        effective_limit = limit + (1 if boundary_count > 0 else 0)
        has_more = len(filtered) > effective_limit
        page = filtered[:effective_limit]
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
            f"<indexType>{html.escape(self.index_type)}</indexType>"
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

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
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

            if self.fault_mode == "xxe_entity":
                padding = "<!-- " + ("A" * 4500) + " -->"
                xxe_doc = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    f"{padding}"
                    '<!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
                    "<soapenv:Body>&xxe;</soapenv:Body></soapenv:Envelope>"
                )
                await self._write_response(writer, 200, xxe_doc, "text/xml; charset=utf-8")
                return

            if "WMLS_GetVersion" in body_text:
                soap_resp = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
                    'xmlns:wmls="http://www.witsml.org/message/120">'
                    "<soapenv:Body>"
                    "<wmls:WMLS_GetVersionResponse>"
                    "<Result>1.4.1.1</Result>"
                    "</wmls:WMLS_GetVersionResponse>"
                    "</soapenv:Body>"
                    "</soapenv:Envelope>"
                )
                await self._write_response(writer, 200, soap_resp, "text/xml; charset=utf-8")
                return

            if "WMLS_GetCap" in body_text:
                cap_xml = (
                    '<capServers xmlns="http://www.witsml.org/schemas/1series" version="1.4.1.1">'
                    '<capServer apiVersion="1.4.1.1">'
                    "<name>DrillAI Local WITSML Store</name>"
                    '<function name="WMLS_GetFromStore"><dataObject>log</dataObject></function>'
                    "</capServer>"
                    "</capServers>"
                )
                soap_resp = (
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
                    'xmlns:wmls="http://www.witsml.org/message/120">'
                    "<soapenv:Body>"
                    "<wmls:WMLS_GetCapResponse>"
                    "<Result>1</Result>"
                    f"<CapabilitiesOut>{html.escape(cap_xml)}</CapabilitiesOut>"
                    "<SuppMsgOut/>"
                    "</wmls:WMLS_GetCapResponse>"
                    "</soapenv:Body>"
                    "</soapenv:Envelope>"
                )
                await self._write_response(writer, 200, soap_resp, "text/xml; charset=utf-8")
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
        reason = {200: "OK", 401: "Unauthorized", 500: "Internal Server Error"}.get(status, "Error")
        payload = body.encode("utf-8")
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + payload)
        await writer.drain()
