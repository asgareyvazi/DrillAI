"""Deterministic extractors: parsed regions in, structured records out.

The platform's rule is that a *number* must never come from a language model. Extractors are
therefore ordinary, reviewable code with an explicit ``method`` and ``method_version`` recorded
on every extracted record. An LLM may be used later to *propose* an extractor or to assist with
classification, but its output is a candidate record that must pass validation before it is
promoted — see ``ai.guardrails``.

Each extractor declares which document types it applies to and returns records with:

* a canonical SI value where a quantity is involved, plus the source unit context;
* the page and region it came from (provenance);
* a confidence between 0 and 1 (pattern quality, not model confidence);
* ``validation_state``: ``unvalidated`` for anything freshly extracted. Records are promoted to
  ``validated`` only by an explicit validation step (engine cross-check or human confirmation).
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from drillai.core.clock import utc_now
from drillai.core.logging import get_logger
from drillai.ingestion.parsers import ParsedDocument, ParsedRegion

logger = get_logger(__name__)

__all__ = [
    "EXTRACTOR_REGISTRY",
    "ExtractedRecordDraft",
    "Extractor",
    "extract",
    "extractor_catalogue",
    "register_extractor",
]

_MAX_RECORDS_PER_DOCUMENT = 2000


@dataclass
class ExtractedRecordDraft:
    """A record as extracted, before it is written to the database."""

    record_type: str
    payload: dict[str, Any]
    page_number: int
    region_index: int | None = None
    confidence: float = 0.8
    method: str = "regex"
    method_version: str = "1.0.0"
    observed_at: dt.datetime | None = None
    depth_md_si: float | None = None
    depth_tvd_si: float | None = None
    unit_context: dict[str, str] = field(default_factory=dict)
    quality_flags: list[str] = field(default_factory=list)
    excerpt: str | None = None
    source_locator: dict[str, Any] = field(default_factory=dict)
    field_locations: dict[str, dict[str, Any]] = field(default_factory=dict)
    payload_schema_key: str = "extracted.record"
    payload_schema_version: int = 1


class Extractor:
    """Base extractor. Subclasses set ``key``/``doc_types`` and implement :meth:`run`."""

    key: str = "base"
    name: str = "Base extractor"
    version: str = "1.0.0"
    doc_types: tuple[str, ...] = ()
    priority: int = 100

    def applies_to(self, doc_type: str, document: ParsedDocument) -> bool:
        return not self.doc_types or doc_type in self.doc_types

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:  # pragma: no cover
        raise NotImplementedError


EXTRACTOR_REGISTRY: dict[str, Extractor] = {}


def register_extractor(extractor: Extractor, *, replace: bool = False) -> None:
    if extractor.key in EXTRACTOR_REGISTRY and not replace:
        raise ValueError(f"extractor {extractor.key!r} already registered")
    EXTRACTOR_REGISTRY[extractor.key] = extractor


def extractor_catalogue() -> list[dict[str, Any]]:
    return [
        {"key": extractor.key, "name": extractor.name, "version": extractor.version, "doc_types": list(extractor.doc_types)}
        for extractor in sorted(EXTRACTOR_REGISTRY.values(), key=lambda item: (item.priority, item.key))
    ]


def extract(document: ParsedDocument, *, doc_type: str) -> tuple[list[ExtractedRecordDraft], list[str]]:
    """Run every applicable extractor, collecting warnings rather than failing the document."""
    drafts: list[ExtractedRecordDraft] = []
    warnings: list[str] = []
    for extractor in sorted(EXTRACTOR_REGISTRY.values(), key=lambda item: (item.priority, item.key)):
        if not extractor.applies_to(doc_type, document):
            continue
        try:
            produced = list(extractor.run(document, doc_type=doc_type))
        except Exception as exc:
            warnings.append(f"extractor {extractor.key} failed: {exc}")
            logger.warning(
                "extractor failed", extra={"extra_fields": {"extractor": extractor.key, "doc_type": doc_type, "error": str(exc)}}
            )
            continue
        if not produced:
            continue
        if len(produced) > _MAX_RECORDS_PER_DOCUMENT:
            warnings.append(
                f"extractor {extractor.key} produced {len(produced)} records; truncated to {_MAX_RECORDS_PER_DOCUMENT}"
            )
            produced = produced[:_MAX_RECORDS_PER_DOCUMENT]
        drafts.extend(produced)
    return drafts, warnings


# --------------------------------------------------------------------------- patterns

#: Rows that introduce a *new* block inside the same sheet. Spreadsheet DDR exports stack several
#: logical tables in one worksheet (operations, then drilling parameters, then mud, then surveys),
#: so an extractor must know where its own table ends. Recognising the heading is what stops
#: "WOB (klbf) | 18.5" from being read as an operation row.
SECTION_HEADING_RE = re.compile(
    r"^\s*(?P<name>operations?|activities|activity|drilling\s+parameters?|drilling\s+data|"
    r"mud\s+(properties|parameters|checks?)|survey(s)?|surveys?\s+(data|stations?)|depth\s+readings?|"
    r"casing(\s*[/&]\s*cement)?|cement(ing)?|bit\s+record|bha|nozzles?|pumps?|"
    r"personnel|hse|safety|bulk|inventory|costs?|time\s+breakdown|npt|problems?|"
    r"remarks?|notes?|summary)\s*[:\-]?\s*$",
    re.IGNORECASE,
)


def is_section_heading(values: list[str]) -> bool:
    """True when a table row introduces a new block rather than carrying data."""
    cells = [value.strip() for value in values if value and value.strip()]
    if not cells:
        return True
    if len(cells) > 2:
        return False
    return bool(SECTION_HEADING_RE.match(cells[0]))


_DATE_PATTERNS = (
    re.compile(r"(?P<y>20\d{2})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})"),
    re.compile(r"(?P<d>\d{1,2})[-/](?P<m>\d{1,2})[-/](?P<y>20\d{2})"),
    re.compile(r"(?P<d>\d{1,2})[ -](?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ -](?P<y>20\d{2})", re.I),
)
_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
)}

_DEPTH_RE = re.compile(
    r"(?<![\d.,])(?P<value>\d{1,6}(?:[.,]\d{1,2})?)\s*"
    r"(?P<unit>m\b|ft\b|feet\b|'|″|in\b)(?![\w])",
    re.I,
)
_MUD_WEIGHT_RE = re.compile(
    r"(?:\bmw\b|mud weight|mud\s*weight|\bemd\b|\bewm\b)[^0-9\-]{0,24}(?P<value>\d{1,3}(?:[.,]\d{1,3})?)\s*(?P<unit>ppg|kg/m3|kg/m³|g/cm3|sg|psi|barg)?",
    re.I,
)


def _to_float(raw: Any) -> float | None:
    """Parse an unambiguous finite decimal; reject locale-ambiguous comma grouping.

    A single comma followed by exactly three digits (for example ``1,200``) can mean either 1.2 or
    1200. Guessing would silently change engineering values, so that form is rejected unless the
    integer part is zero or has more than three digits. Well-formed grouped thousands are accepted
    only when groups are exactly three digits.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if math.isfinite(value) else None

    text = str(raw).strip()
    if not text:
        return None
    sign = ""
    if text[0] in "+-":
        sign, text = text[0], text[1:]
    if not text or not text.replace(",", "").replace(".", "").isdigit():
        return None

    if "," in text and "." in text:
        decimal_separator = "," if text.rfind(",") > text.rfind(".") else "."
        grouping_separator = "." if decimal_separator == "," else ","
        integer, fraction = text.rsplit(decimal_separator, 1)
        if not fraction.isdigit():
            return None
        groups = integer.split(grouping_separator)
        if not groups[0].isdigit() or not 1 <= len(groups[0]) <= 3:
            return None
        if groups[0].startswith("0") and len(groups[0]) > 1:
            return None
        if any(not group.isdigit() or len(group) != 3 for group in groups[1:]):
            return None
        normalized = "".join(groups) + "." + fraction
    elif "," in text:
        groups = text.split(",")
        if len(groups) > 2:
            if not groups[0].isdigit() or not 1 <= len(groups[0]) <= 3 or groups[0].startswith("0"):
                return None
            if any(not group.isdigit() or len(group) != 3 for group in groups[1:]):
                return None
            normalized = "".join(groups)
        else:
            integer, fraction = groups
            if not fraction.isdigit() or (integer and not integer.isdigit()):
                return None
            if len(fraction) == 3 and 1 <= len(integer) <= 3 and not integer.startswith("0"):
                return None
            normalized = (integer or "0") + "." + fraction
    elif "." in text:
        groups = text.split(".")
        if len(groups) > 2:
            if not groups[0].isdigit() or not 1 <= len(groups[0]) <= 3 or groups[0].startswith("0"):
                return None
            if any(not group.isdigit() or len(group) != 3 for group in groups[1:]):
                return None
            normalized = "".join(groups)
        else:
            integer, fraction = groups
            if not fraction.isdigit() or (integer and not integer.isdigit()):
                return None
            normalized = (integer or "0") + "." + fraction
    else:
        if not text.isdigit():
            return None
        normalized = text

    try:
        value = float(sign + normalized)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _parse_date_match(text: str) -> tuple[dt.datetime, re.Match[str]] | None:
    """Return the parsed date and the exact match that supplied it.

    Keep the locator tied to the same valid date as the payload. Searching the patterns a second time
    can otherwise point at an earlier invalid date while the parsed value came from a later one.
    """
    for pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groupdict()
            try:
                if groups.get("mon"):
                    month = _MONTHS[groups["mon"][:3].lower()]
                else:
                    month = int(groups["m"])
                parsed = dt.datetime(int(groups["y"]), month, int(groups["d"]), tzinfo=dt.UTC)
            except (KeyError, ValueError):
                continue
            return parsed, match
    return None


def _parse_date(text: str) -> dt.datetime | None:
    result = _parse_date_match(text)
    return result[0] if result is not None else None


def _depth_unit_factor(unit: str) -> float:
    lowered = unit.strip().casefold().rstrip(".")
    factors = {
        "m": 1.0,
        "meter": 1.0,
        "meters": 1.0,
        "metre": 1.0,
        "metres": 1.0,
        "ft": 0.3048,
        "foot": 0.3048,
        "feet": 0.3048,
        "'": 0.3048,
        "″": 0.0254,
        '"': 0.0254,
        "in": 0.0254,
        "inch": 0.0254,
        "inches": 0.0254,
    }
    try:
        return factors[lowered]
    except KeyError as exc:
        raise ValueError(f"unsupported measured-depth unit {unit!r}") from exc


def _depth_to_si(value: float, unit: str) -> float:
    return value * _depth_unit_factor(unit)


def _angle_to_degrees(value: float, unit: str) -> float:
    lowered = unit.strip().casefold().rstrip(".")
    if lowered in {"deg", "degree", "degrees", "°"}:
        return value
    if lowered in {"rad", "radian", "radians"}:
        return math.degrees(value)
    raise ValueError(f"unsupported survey angle unit {unit!r}")


def _unit_from_header(label: str) -> str:
    """Read an explicit unit from a column label without guessing from its numeric values."""
    match = re.search(r"\(([^)]+)\)|\[([^]]+)\]", label)
    if match:
        return (match.group(1) or match.group(2)).strip()
    match = re.search(r"\b(ft|feet|m|in|inch|rad|radian|deg|degree|degrees)\b", label, re.I)
    return match.group(1) if match else ""


def _text_region_spans(page) -> list[tuple[ParsedRegion, int, int]]:
    """Map real text regions into the parser's page-text stream, in source order."""
    spans: list[tuple[ParsedRegion, int, int]] = []
    cursor = 0
    for region in page.regions:
        if region.kind != "text" or not region.text:
            continue
        locator = region.source_locator or {}
        explicit_start = locator.get("char_start")
        explicit_end = locator.get("char_end")
        if (
            isinstance(explicit_start, int)
            and isinstance(explicit_end, int)
            and 0 <= explicit_start <= explicit_end <= len(page.text)
            and page.text[explicit_start:explicit_end] == region.text
        ):
            start, end = explicit_start, explicit_end
        else:
            start = page.text.find(region.text, cursor)
            if start < 0:
                continue
            end = start + len(region.text)
        spans.append((region, start, end))
        cursor = max(cursor, end)
    return spans


def _region_for_text_range(page, start: int, end: int) -> int | None:
    for region, region_start, region_end in _text_region_spans(page):
        if region_start <= start and end <= region_end:
            return region.order_index
    return None


def _single_region_index(field_locations: dict[str, dict[str, Any]]) -> int | None:
    if not field_locations:
        return None
    region_orders = [location.get("region_order") for location in field_locations.values()]
    if not all(isinstance(order, int) for order in region_orders):
        return None
    unique_orders = set(region_orders)
    return next(iter(unique_orders)) if len(unique_orders) == 1 else None


def _text_locator(page, start: int, end: int | None = None) -> dict[str, Any]:
    """Locate a text match without pretending a parser exposed page coordinates."""
    end = start if end is None else end
    prefix = page.text[:start]
    line = prefix.count("\n") + 1
    line_start = prefix.rfind("\n") + 1
    locator = {
        "format": page.layout.get("source", "text"),
        "kind": "text_line",
        "page": page.page_number,
        "line": line,
        "char_start": start,
        "char_end": end,
        "line_char_start": start - line_start,
    }
    region_order = _region_for_text_range(page, start, end)
    if region_order is not None:
        locator["region_order"] = region_order
    return locator


def _cell_locator(region: ParsedRegion, cell: dict[str, Any]) -> dict[str, Any] | None:
    locator = cell.get("source_locator")
    if not isinstance(locator, dict):
        return None
    return {**locator, "region_order": region.order_index}


def _header_sources(page):
    """Yield exact text fragments or original table cells where a report header can live.

    Delimited reports are represented as a cell matrix rather than a text region. Searching only
    ``page.text`` still finds the characters but loses their source cell; searching only text regions
    silently drops CSV/XLSX headers. This keeps each candidate bound to its original parser source.
    """
    text_starts = {
        region.order_index: start
        for region, start, _end in _text_region_spans(page)
    }
    label_aliases = {
        "well": "Well",
        "well name": "Well name",
        "wellbore": "Wellbore",
        "rig": "Rig",
        "rig name": "Rig name",
        "report no": "Report no",
        "report number": "Report number",
        "ddr no": "DDR no",
        "date": "Report date",
        "report date": "Report date",
    }
    for region in page.regions:
        if region.kind == "text" and region.text:
            yield region, region.text, text_starts.get(region.order_index), None, False
            continue
        table = region.table
        if region.kind != "table" or not table or table.get("hidden"):
            continue
        for row in table.get("cells", [])[:20]:
            visible = [
                cell
                for cell in row[:40]
                if not cell.get("hidden_row")
                and not cell.get("hidden_sheet")
                and str(cell.get("text", "")).strip()
            ]
            for cell in visible:
                text = str(cell.get("text", "")).strip()
                locator = _cell_locator(region, cell)
                if text:
                    yield region, text, None, locator, False
            if len(visible) == 2:
                label = re.sub(r"[^a-z0-9]+", " ", str(visible[0].get("text", "")).casefold()).strip()
                canonical = label_aliases.get(label)
                value = str(visible[1].get("text", "")).strip()
                value_locator = _cell_locator(region, visible[1])
                label_locator = _cell_locator(region, visible[0])
                if canonical and value and value_locator is not None:
                    paired_locator = dict(value_locator)
                    if label_locator is not None:
                        paired_locator["label_cell"] = label_locator
                    yield region, f"{canonical}: {value}", None, paired_locator, True


def _table_row_locator(region, row: list[dict[str, Any]]) -> dict[str, Any]:
    table = region.table or {}
    locators = [
        {**cell["source_locator"], "region_order": region.order_index}
        for cell in row
        if isinstance(cell.get("source_locator"), dict)
    ]
    result: dict[str, Any] = {
        "format": table.get("source_format", "table"),
        "kind": "table_row",
        "region_order": region.order_index,
        "source_cells": locators,
    }
    if locators:
        for key in ("sheet", "table"):
            if key in locators[0]:
                result[key] = locators[0][key]
        result["row"] = locators[0].get("row")
        result["end_line"] = locators[0].get("end_line")
    return result


# --------------------------------------------------------------------------- extractors


class ReportHeaderExtractor(Extractor):
    """Header fields of a daily report: well name, report date, rig, report number."""

    key = "report_header"
    name = "Report header fields"
    version = "1.1.0"
    doc_types = ("ddr", "drilling_document", "program")
    priority = 10

    _WELL_RE = re.compile(r"(?:well|well name|wellbore)\s*[:\-]\s*(?P<name>[A-Za-z0-9][\w\-/ ]{1,60})", re.I)
    _RIG_RE = re.compile(r"(?:rig|rig name)\s*[:\-]\s*(?P<name>[A-Za-z0-9][\w\-/ ]{1,60})", re.I)
    _REPORT_NO_RE = re.compile(r"(?:report|ddr)\s*(?:no|number|#)\s*[:\-]?\s*(?P<number>\d{1,6})", re.I)

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages[:2]:
            payload: dict[str, Any] = {}
            field_locations: dict[str, dict[str, Any]] = {}
            excerpts: list[str] = []
            report_date: dt.datetime | None = None
            for region, text, region_start, cell_locator, is_cell_pair in _header_sources(page):
                bounded_text = text[:2000]
                if not bounded_text.strip():
                    continue
                if len(excerpts) < 40:
                    excerpts.append(bounded_text)

                def location(
                    match: re.Match[str],
                    *,
                    cell_locator=cell_locator,
                    is_cell_pair=is_cell_pair,
                    region_start=region_start,
                    page=page,
                    region=region,
                ) -> dict[str, Any]:
                    if cell_locator is not None:
                        if is_cell_pair:
                            # The label and value are distinct source cells; the value cell carries
                            # the field and `label_cell` preserves the relationship without fake offsets.
                            return dict(cell_locator)
                        return {
                            **cell_locator,
                            "cell_char_start": match.start(),
                            "cell_char_end": match.end(),
                        }
                    if region_start is not None:
                        return _text_locator(page, region_start + match.start(), region_start + match.end())
                    # Some parsers expose a real text region that is not part of their rendered page
                    # string. Preserve the region identity without inventing page coordinates.
                    return {
                        "format": page.layout.get("source", "text"),
                        "kind": "text_region",
                        "page": page.page_number,
                        "region_order": region.order_index,
                    }

                if "well_name_text" not in payload and (match := self._WELL_RE.search(bounded_text)):
                    payload["well_name_text"] = match.group("name").strip()
                    field_locations["well_name_text"] = location(match)
                if "rig_name_text" not in payload and (match := self._RIG_RE.search(bounded_text)):
                    payload["rig_name_text"] = match.group("name").strip()
                    field_locations["rig_name_text"] = location(match)
                if "report_number" not in payload and (match := self._REPORT_NO_RE.search(bounded_text)):
                    payload["report_number"] = match.group("number")
                    field_locations["report_number"] = location(match)
                if report_date is None:
                    date_result = _parse_date_match(bounded_text)
                    if date_result is not None:
                        report_date, date_match = date_result
                        payload["report_date"] = report_date.date().isoformat()
                        field_locations["report_date"] = location(date_match)
            if not payload:
                continue
            payload["doc_type"] = doc_type
            region_index = _single_region_index(field_locations)
            yield ExtractedRecordDraft(
                record_type="report_header",
                payload=payload,
                page_number=page.page_number,
                region_index=region_index,
                confidence=0.75,
                method="regex_header",
                method_version=self.version,
                observed_at=report_date,
                excerpt="\n".join(excerpts)[:400],
                source_locator={
                    "format": page.layout.get("source", "text"),
                    "kind": "page_text",
                    "page": page.page_number,
                    "region_order": region_index,
                },
                field_locations=field_locations,
                payload_schema_key="extracted.report_header",
            )


def _contains_table_header_hint(label: str, hint: str) -> bool:
    """Match a whole header word or phrase, not a misleading substring such as ``code`` in ``decoder``."""
    normalised = re.sub(r"[^a-z0-9]+", " ", label.casefold()).strip()
    return f" {hint} " in f" {normalised} "


class OperationTableExtractor(Extractor):
    """Operations rows: sequence, start/end time, operation code, duration, depth."""

    key = "operations_table"
    name = "Operations / activities table"
    version = "1.1.0"
    doc_types = ("ddr", "drilling_document")
    priority = 20

    _HEADER_HINTS = (
        "operation", "operations", "activity", "activities", "description", "code", "duration"
    )
    _DEPTH_HEADER_ALIASES = {
        "md",
        "measured depth",
        "md depth",
        "depth md",
        "bit depth",
    }
    _DEPTH_CELL = re.compile(
        r"^(?P<value>\d{1,6}(?:[.,]\d{1,2})?)\s*(?P<unit>m|ft|feet)?$",
        re.I,
    )

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages:
            for region in page.regions:
                if region.kind != "table" or not region.table or region.table.get("hidden"):
                    continue
                header = [str(cell).lower().strip() for cell in (region.table.get("header") or [])]
                if not any(
                    _contains_table_header_hint(column, hint)
                    for column in header
                    for hint in self._HEADER_HINTS
                ):
                    continue
                counts = {name: header.count(name) for name in set(header) if name}
                ambiguous = [
                    name
                    for name, count in counts.items()
                    if count > 1
                    and any(_contains_table_header_hint(name, hint) for hint in self._HEADER_HINTS)
                ]
                if ambiguous:
                    raise ValueError(f"ambiguous duplicate operation header(s): {sorted(ambiguous)}")
                columns = {name: index for index, name in enumerate(header) if name}
                depth_columns = [
                    index
                    for index, label in enumerate(region.table.get("header") or [])
                    if re.sub(r"[^a-z0-9]+", " ", re.sub(r"\([^)]*\)|\[[^]]*\]", " ", str(label).casefold())).strip()
                    in self._DEPTH_HEADER_ALIASES
                ]
                depth_column = depth_columns[0] if len(depth_columns) == 1 else None
                depth_header_unit = _unit_from_header(str((region.table.get("header") or [])[depth_column])) if depth_column is not None else ""
                # The parser detects where the header actually is; a title/metadata block above
                # the table must not be parsed as data rows.
                header_row = int(region.table.get("header_row") or 0)
                for row_index, row in enumerate(region.table["cells"][header_row + 1:], start=header_row + 1):
                    if not row or any(cell.get("hidden_row") or cell.get("hidden_sheet") for cell in row):
                        continue
                    values = [str(cell.get("text", "")) for cell in row]
                    joined = " | ".join(values)
                    if not joined.strip():
                        continue
                    if is_section_heading(values):
                        # End of this logical table: later blocks belong to other extractors.
                        break
                    source_row = next((cell.get("source_row") for cell in row if cell.get("source_row") is not None), row_index + 1)
                    payload: dict[str, Any] = {"row_index": source_row}
                    field_locations: dict[str, dict[str, Any]] = {}
                    for key, index in columns.items():
                        if index < len(values) and values[index].strip():
                            payload[key] = values[index].strip()
                            if index < len(row) and (locator := _cell_locator(region, row[index])):
                                field_locations[key] = locator
                    row_locator = _table_row_locator(region, row)
                    unit_context: dict[str, str] = {}
                    quality_flags: list[str] = []
                    duration = self._duration_hours(payload)
                    if duration is not None:
                        payload["duration_hours"] = duration
                        duration_key = next(
                            (
                                key
                                for key in payload
                                if "duration" in key or re.search(r"\bdur\b", key, re.I)
                            ),
                            None,
                        )
                        if duration_key:
                            unit_context["duration_hours"] = _unit_from_header(duration_key) or "h"
                            if duration_key in field_locations:
                                field_locations["duration_hours"] = field_locations[duration_key]
                    if len(depth_columns) > 1:
                        quality_flags.append("ambiguous_depth_columns")
                    elif depth_column is not None and depth_column < len(row):
                        raw_depth = values[depth_column].strip()
                        depth_match = self._DEPTH_CELL.fullmatch(raw_depth)
                        if depth_match is not None:
                            depth_unit = depth_match.group("unit") or depth_header_unit
                            depth_value = _to_float(depth_match.group("value"))
                            if depth_value is None:
                                quality_flags.append("ambiguous_or_invalid_depth_value")
                            elif depth_unit:
                                try:
                                    depth_si = _depth_to_si(depth_value, depth_unit)
                                except ValueError:
                                    quality_flags.append("unsupported_depth_unit")
                                else:
                                    payload["depth_md_si"] = depth_si
                                    unit_context["depth_md_si"] = depth_unit
                                    if locator := _cell_locator(region, row[depth_column]):
                                        field_locations["depth_md_si"] = locator
                    yield ExtractedRecordDraft(
                        record_type="operation_row",
                        payload=payload,
                        page_number=page.page_number,
                        region_index=region.order_index,
                        confidence=0.7,
                        method="table_row",
                        method_version=self.version,
                        excerpt=joined[:400],
                        unit_context=unit_context,
                        quality_flags=quality_flags,
                        source_locator=row_locator,
                        field_locations=field_locations,
                        payload_schema_key="extracted.operation_row",
                    )

    @staticmethod
    def _duration_hours(payload: dict[str, Any]) -> float | None:
        for key, value in payload.items():
            if "duration" not in key.lower() and not re.search(r"\bdur\b", key, re.I):
                continue
            text = str(value).strip()
            match = re.fullmatch(r"(?P<h>\d{1,3})\s*:\s*(?P<m>\d{1,2})", text)
            if match:
                minutes = int(match.group("m"))
                if minutes >= 60:
                    return None
                hours = int(match.group("h")) + minutes / 60.0
                return hours if 0 <= hours < 100 else None
            plain = _to_float(text)
            if plain is None or plain < 0:
                continue
            unit = _unit_from_header(key).lower().replace(".", "").strip()
            if unit in {"min", "mins", "minute", "minutes"}:
                hours = plain / 60.0
            elif unit in {"s", "sec", "secs", "second", "seconds"}:
                hours = plain / 3600.0
            elif unit in {"d", "day", "days"}:
                hours = plain * 24.0
            elif not unit or unit in {"h", "hr", "hrs", "hour", "hours"}:
                hours = plain
            else:
                continue
            # Apply the processing bound to the canonical value, not to the raw number: 120 minutes
            # is two hours, while five days is 120 hours and must be rejected.
            if math.isfinite(hours) and hours < 100:
                return hours
        return None


class MudPropertiesExtractor(Extractor):
    """Mud weight and rheology readings (label/value pairs and mud-check tables)."""

    key = "mud_properties"
    name = "Mud properties"
    version = "1.1.0"
    doc_types = ("ddr", "drilling_document", "mud_report", "other")
    priority = 30

    _LABELS = {
        "mud_weight": ("mud weight", "mw", "emd", "ewm", "density"),
        "funnel_viscosity": ("funnel viscosity", "funnel vis", "fv"),
        "plastic_viscosity": ("plastic viscosity", "pv"),
        "yield_point": ("yield point", "yp"),
        "gel_10s": ("gel 10", "10 sec gel", "10s gel"),
        "gel_10m": ("gel 10 min", "10 min gel", "10m gel"),
        "ph": ("ph",),
        "chlorides": ("chloride", "cl"),
        "solids": ("solids",),
    }

    @staticmethod
    def _canonical_field(label: str) -> str | None:
        normalized = re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()
        matches = [
            field_name
            for field_name, aliases in MudPropertiesExtractor._LABELS.items()
            if any(re.search(rf"\b{re.escape(alias)}\b", normalized) for alias in aliases)
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _numeric_value(raw: str) -> tuple[float, str] | None:
        match = re.fullmatch(
            r"\s*(?P<value>-?\d{1,4}(?:[.,]\d{1,3})?)\s*"
            r"(?P<unit>ppg|kg/m3|kg/m³|g/cm3|sg|cp|s|ml|psi)?\s*",
            raw,
            re.IGNORECASE,
        )
        if match is None:
            return None
        value = _to_float(match.group("value"))
        return (value, (match.group("unit") or "")) if value is not None else None

    @staticmethod
    def _header_unit(label: str) -> str:
        return _unit_from_header(label)

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages:
            readings: dict[str, Any] = {}
            units: dict[str, str] = {}
            field_locations: dict[str, dict[str, Any]] = {}
            source_locator: dict[str, Any] = {
                "format": page.layout.get("source", "text"),
                "kind": "page_text",
                "page": page.page_number,
            }
            excerpt: str | None = None

            for region in page.regions:
                table = region.table
                if region.kind != "table" or not table or table.get("hidden"):
                    continue
                header_row = int(table.get("header_row") or 0)
                header = [str(value) for value in (table.get("header") or [])]
                header_matches: dict[str, list[int]] = {}
                for column_index, label in enumerate(header):
                    field_name = self._canonical_field(label)
                    if field_name is not None:
                        header_matches.setdefault(field_name, []).append(column_index)
                header_fields = {
                    column_index: field_name
                    for field_name, column_indices in header_matches.items()
                    if len(column_indices) == 1
                    for column_index in column_indices
                }

                for row in table.get("cells", [])[header_row + 1:]:
                    if not row or any(cell.get("hidden_row") or cell.get("hidden_sheet") for cell in row):
                        continue
                    for column_index, field_name in header_fields.items():
                        if field_name in readings or column_index >= len(row):
                            continue
                        cell = row[column_index]
                        parsed = self._numeric_value(str(cell.get("text", "")))
                        if parsed is None:
                            continue
                        value, unit = parsed
                        readings[field_name] = value
                        locator = _cell_locator(region, cell)
                        if locator is not None:
                            field_locations[field_name] = locator
                        unit = unit or self._header_unit(header[column_index])
                        if unit:
                            units[field_name] = unit
                        if excerpt is None:
                            excerpt = " | ".join(str(item.get("text", "")) for item in row)[:400]
                        if len(field_locations) == 1:
                            source_locator = _table_row_locator(region, row)

                    populated = [cell for cell in row if str(cell.get("text", "")).strip()]
                    if len(populated) != 2:
                        continue
                    field_name = self._canonical_field(str(populated[0].get("text", "")))
                    if field_name is None or field_name in readings:
                        continue
                    parsed = self._numeric_value(str(populated[1].get("text", "")))
                    if parsed is None:
                        continue
                    value, unit = parsed
                    readings[field_name] = value
                    locator = _cell_locator(region, populated[1])
                    if locator is not None:
                        field_locations[field_name] = locator
                    unit = unit or self._header_unit(str(populated[0].get("text", "")))
                    if unit:
                        units[field_name] = unit
                    if excerpt is None:
                        excerpt = " | ".join(str(item.get("text", "")) for item in row)[:400]
                    if len(field_locations) == 1:
                        source_locator = _table_row_locator(region, row)

            for region, region_start, _region_end in _text_region_spans(page):
                text = region.text or ""
                if not text:
                    continue
                for field_name, labels in self._LABELS.items():
                    if field_name in readings:
                        continue
                    for label in labels:
                        match = re.search(
                            rf"\b{re.escape(label)}\b[^0-9\-]{{0,16}}(?P<value>\d{{1,4}}(?:[.,]\d{{1,3}})?)\s*(?P<unit>ppg|kg/m3|kg/m³|g/cm3|sg|cp|s|ml|psi)?",
                            text,
                            re.IGNORECASE,
                        )
                        if not match:
                            continue
                        suffix = text[match.end():]
                        if re.match(r"\s*(?:[-\u2012-\u2015]\s*\d|\bto\b\s*\d)", suffix, re.I):
                            # A range is not a point reading. Do not turn its lower bound into a fact.
                            continue
                        value = _to_float(match.group("value"))
                        if value is None:
                            continue
                        readings[field_name] = value
                        field_locations[field_name] = _text_locator(
                            page,
                            region_start + match.start(),
                            region_start + match.end(),
                        )
                        unit = match.group("unit") or ""
                        if unit:
                            units[field_name] = unit
                        if excerpt is None:
                            excerpt = text[max(0, match.start() - 60):match.end() + 60][:400]
                        if len(field_locations) == 1:
                            source_locator = {
                                "format": page.layout.get("source", "text"),
                                "kind": "page_text",
                                "page": page.page_number,
                                "region_order": region.order_index,
                            }
                        break

            if readings:
                region_index = _single_region_index(field_locations)
                yield ExtractedRecordDraft(
                    record_type="mud_properties",
                    payload=readings,
                    page_number=page.page_number,
                    region_index=region_index,
                    confidence=0.6,
                    method="label_value",
                    method_version=self.version,
                    excerpt=excerpt,
                    unit_context=units,
                    quality_flags=["unit_inferred"] if not units else [],
                    source_locator=source_locator,
                    field_locations=field_locations,
                    payload_schema_key="extracted.mud_properties",
                )


class DepthReadingExtractor(Extractor):
    """Depth readings (bit depth, hole depth, casing shoe depth) from labels."""

    key = "depth_readings"
    name = "Depth readings"
    version = "1.1.0"
    doc_types = ("ddr", "drilling_document", "other")
    priority = 40

    _LABELS = ("bit depth", "hole depth", "casing depth", "shoe depth", "td depth", "driller depth")

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages:
            found: dict[str, Any] = {}
            units: dict[str, str] = {}
            field_locations: dict[str, dict[str, Any]] = {}
            excerpt: str | None = None
            for region, region_start, _region_end in _text_region_spans(page):
                text = region.text or ""
                for label in self._LABELS:
                    if label.replace(" ", "_") + "_si" in found:
                        continue
                    match = re.search(
                        rf"{re.escape(label)}\s*[:\-]?\s*{_DEPTH_RE.pattern}", text, re.I
                    )
                    if not match:
                        continue
                    value = _to_float(match.group("value"))
                    if value is None:
                        continue
                    key = label.replace(" ", "_") + "_si"
                    found[key] = _depth_to_si(value, match.group("unit"))
                    units[key] = match.group("unit")
                    field_locations[key] = _text_locator(
                        page,
                        region_start + match.start(),
                        region_start + match.end(),
                    )
                    if excerpt is None:
                        excerpt = text[max(0, match.start() - 60):match.end() + 60][:400]
            if found:
                region_index = _single_region_index(field_locations)
                yield ExtractedRecordDraft(
                    record_type="depth_reading",
                    payload=found,
                    page_number=page.page_number,
                    region_index=region_index,
                    confidence=0.7,
                    method="label_value",
                    method_version=self.version,
                    excerpt=excerpt,
                    unit_context=units,
                    source_locator={
                        "format": page.layout.get("source", "text"),
                        "kind": "page_text",
                        "page": page.page_number,
                        "region_order": region_index,
                    },
                    field_locations=field_locations,
                    payload_schema_key="extracted.depth_reading",
                )


class SurveyTableExtractor(Extractor):
    """Survey stations (MD, inclination, azimuth) from survey tables."""

    key = "survey_table"
    name = "Survey stations"
    version = "1.1.0"
    doc_types = ("ddr", "drilling_document", "program", "survey_report", "other")
    priority = 50

    _HEADER_ALIASES = {
        "md": {"md", "measured depth", "md depth", "depth md", "measured depth md", "md measured depth"},
        "inclination": {"inc", "incl", "inclination", "inclination angle"},
        "azimuth": {"azi", "azim", "azimuth"},
    }

    @staticmethod
    def _header_field(label: str) -> str | None:
        """Match a controlled header alias; never infer measured depth from generic 'depth'."""
        normalized = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", label.casefold())
        unit = _unit_from_header(label)
        if unit:
            normalized = re.sub(rf"(?<![a-z0-9]){re.escape(unit.casefold())}(?![a-z0-9])", " ", normalized)
        normalized = re.sub(r"[^a-z0-9]+", " ", normalized).strip()
        matches = [field_name for field_name, aliases in SurveyTableExtractor._HEADER_ALIASES.items() if normalized in aliases]
        return matches[0] if len(matches) == 1 else None

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages:
            for region in page.regions:
                if region.kind != "table" or not region.table or region.table.get("hidden"):
                    continue
                header = [str(cell).lower().strip() for cell in (region.table.get("header") or [])]
                if not header:
                    continue
                column_of: dict[str, int] = {}
                for index, column in enumerate(header):
                    field_name = self._header_field(column)
                    if field_name is None:
                        continue
                    if field_name in column_of:
                        raise ValueError(f"ambiguous duplicate survey column for {field_name!r}")
                    column_of[field_name] = index
                if not {"md", "inclination"} <= set(column_of):
                    continue
                md_header_unit = _unit_from_header(header[column_of["md"]])
                inclination_header_unit = _unit_from_header(header[column_of["inclination"]])
                azimuth_header_unit = (
                    _unit_from_header(header[column_of["azimuth"]]) if "azimuth" in column_of else ""
                )
                md_unit = md_header_unit or "m"
                inclination_unit = inclination_header_unit or "deg"
                azimuth_unit = azimuth_header_unit or "deg"
                # Unknown explicit units must fail closed; silently treating e.g. kilometres as
                # metres or mils as degrees would produce plausible but materially wrong stations.
                _depth_unit_factor(md_unit)
                _angle_to_degrees(0.0, inclination_unit)
                if "azimuth" in column_of:
                    _angle_to_degrees(0.0, azimuth_unit)
                inferred_units = not md_header_unit or not inclination_header_unit or (
                    "azimuth" in column_of and not azimuth_header_unit
                )
                header_row = int(region.table.get("header_row") or 0)
                for row in region.table["cells"][header_row + 1:]:
                    if not row or any(cell.get("hidden_row") or cell.get("hidden_sheet") for cell in row):
                        continue
                    values = [str(cell.get("text", "")) for cell in row]

                    def cell_value(field_name: str, columns=column_of, texts=values) -> str | None:
                        """Bind the row explicitly: the lookup must not read a later row's values."""
                        index = columns.get(field_name)
                        if index is None or index >= len(texts):
                            return None
                        return texts[index].strip() or None

                    raw_md = cell_value("md")
                    md = _to_float(raw_md) if raw_md else None
                    if md is None or md <= 0:
                        continue
                    md_si = _depth_to_si(md, md_unit)
                    payload: dict[str, Any] = {"md_si": md_si}
                    field_locations: dict[str, dict[str, Any]] = {}
                    if column_of["md"] < len(row) and (locator := _cell_locator(region, row[column_of["md"]])):
                        field_locations["md_si"] = locator
                    quality_flags: list[str] = ["unit_inferred"] if inferred_units else []
                    if inclination := cell_value("inclination"):
                        parsed_inclination = _to_float(inclination)
                        if parsed_inclination is None:
                            quality_flags.append("ambiguous_or_invalid_inclination")
                        else:
                            parsed_inclination = _angle_to_degrees(parsed_inclination, inclination_unit)
                            if not 0 <= parsed_inclination <= 180:
                                quality_flags.append("inclination_out_of_range")
                            else:
                                payload["inclination_deg"] = parsed_inclination
                                if locator := _cell_locator(region, row[column_of["inclination"]]):
                                    field_locations["inclination_deg"] = locator
                    if azimuth := cell_value("azimuth"):
                        parsed_azimuth = _to_float(azimuth)
                        if parsed_azimuth is None:
                            quality_flags.append("ambiguous_or_invalid_azimuth")
                        else:
                            parsed_azimuth = _angle_to_degrees(parsed_azimuth, azimuth_unit)
                            if not 0 <= parsed_azimuth <= 360:
                                quality_flags.append("azimuth_out_of_range")
                            else:
                                payload["azimuth_deg"] = parsed_azimuth
                                if locator := _cell_locator(region, row[column_of["azimuth"]]):
                                    field_locations["azimuth_deg"] = locator
                    yield ExtractedRecordDraft(
                        record_type="survey_station",
                        payload=payload,
                        page_number=page.page_number,
                        region_index=region.order_index,
                        confidence=0.65,
                        method="table_row",
                        method_version=self.version,
                        depth_md_si=md_si,
                        excerpt=" | ".join(values)[:300],
                        unit_context={
                            "md_si": md_unit,
                            "inclination_deg": inclination_unit,
                            **({"azimuth_deg": azimuth_unit} if "azimuth" in column_of else {}),
                        },
                        quality_flags=quality_flags,
                        source_locator=_table_row_locator(region, row),
                        field_locations=field_locations,
                        payload_schema_key="extracted.survey_station",
                    )


class CasingCementExtractor(Extractor):
    """Casing/cement records: size, weight, grade, top, shoe."""

    key = "casing_cement"
    name = "Casing and cement records"
    version = "1.1.0"
    doc_types = ("ddr", "drilling_document", "program")
    priority = 60

    _SIZE_RE = re.compile(r"(?P<size>\d{1,2}(?:[\-/]\d{1,2})?(?:\.\d{1,3})?)\s*(?:\"|in\b|inch)", re.I)
    _GRADE_RE = re.compile(r"\b(?P<grade>[A-Z]-\d{2,3}|K55|J55|L80|N80|P110|Q125|C90|C95|T95)\b")
    _LINE_RE = re.compile(r"casing|liner|cement", re.I)

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages:
            for region, region_start, _region_end in _text_region_spans(page):
                text = region.text or ""
                cursor = 0
                for raw_line in text.splitlines(keepends=True):
                    line_start = cursor
                    cursor += len(raw_line)
                    line = raw_line.rstrip("\r\n")
                    if not self._LINE_RE.search(line):
                        continue
                    size = self._SIZE_RE.search(line)
                    grade = self._GRADE_RE.search(line)
                    depth = _DEPTH_RE.search(line)
                    if not (size or grade):
                        continue
                    payload: dict[str, Any] = {"text": line.strip()[:400]}
                    field_locations: dict[str, dict[str, Any]] = {}
                    unit_context: dict[str, str] = {}
                    if size:
                        payload["size_nominal"] = size.group("size")
                        field_locations["size_nominal"] = _text_locator(
                            page,
                            region_start + line_start + size.start(),
                            region_start + line_start + size.end(),
                        )
                    if grade:
                        payload["grade"] = grade.group("grade").upper()
                        field_locations["grade"] = _text_locator(
                            page,
                            region_start + line_start + grade.start(),
                            region_start + line_start + grade.end(),
                        )
                    quality_flags = ["low_confidence_pattern"]
                    if depth:
                        depth_value = _to_float(depth.group("value"))
                        if depth_value is None:
                            quality_flags.append("ambiguous_or_invalid_depth_value")
                        else:
                            payload["depth_md_si"] = _depth_to_si(depth_value, depth.group("unit"))
                            unit_context["depth_md_si"] = depth.group("unit")
                            field_locations["depth_md_si"] = _text_locator(
                                page,
                                region_start + line_start + depth.start(),
                                region_start + line_start + depth.end(),
                            )
                    region_index = _single_region_index(field_locations)
                    yield ExtractedRecordDraft(
                        record_type="casing_cement_line",
                        payload=payload,
                        page_number=page.page_number,
                        region_index=region_index,
                        confidence=0.5,
                        method="line_pattern",
                        method_version=self.version,
                        depth_md_si=payload.get("depth_md_si"),
                        excerpt=line.strip()[:400],
                        unit_context=unit_context,
                        quality_flags=quality_flags,
                        source_locator={
                            "format": page.layout.get("source", "text"),
                            "kind": "page_text",
                            "page": page.page_number,
                            "region_order": region_index,
                        },
                        field_locations=field_locations,
                        payload_schema_key="extracted.casing_cement",
                    )


class NumberedParameterExtractor(Extractor):
    """Catch-all label/value pairs: ``label: value unit`` lines not covered above."""

    key = "label_values"
    name = "Generic label/value parameters"
    version = "1.1.0"
    doc_types = ("ddr", "drilling_document", "program", "other")
    priority = 90

    _LINE_RE = re.compile(
        r"^(?P<label>[A-Za-z][A-Za-z0-9 /%\.\-]{2,40}?)\s*[:=]\s*(?P<value>-?\d{1,6}(?:[.,]\d{1,4})?)\s*(?P<unit>%|m|ft|ppg|kg/m3|g/cm3|sg|psi|bar|barg|kpa|cp|s|min|h|hr|kn|klbf|rpm|l/min|m3/min|deg|°|[A-Za-z]{0,4})?\s*$"
    )
    _SKIP_LABELS = ("report no", "page", "well", "rig", "date", "time")

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages:
            values: dict[str, Any] = {}
            units: dict[str, str] = {}
            field_locations: dict[str, dict[str, Any]] = {}
            ambiguous_keys: set[str] = set()
            source_locator: dict[str, Any] | None = None

            # Key/value tables carry direct cell addresses; use them instead of trying to infer
            # cells from the parser's rendered page text.
            for region in page.regions:
                table = region.table
                if region.kind != "table" or not table or table.get("hidden"):
                    continue
                header_row = int(table.get("header_row") or 0)
                for row in table.get("cells", [])[header_row + 1:]:
                    if not row or any(cell.get("hidden_row") or cell.get("hidden_sheet") for cell in row):
                        continue
                    populated = [cell for cell in row if str(cell.get("text", "")).strip()]
                    if len(populated) != 2:
                        continue
                    label_cell, value_cell = populated
                    label = str(label_cell.get("text", "")).strip()
                    raw_value = str(value_cell.get("text", "")).strip()
                    if SECTION_HEADING_RE.match(label):
                        continue
                    inline_unit = None
                    unit_match = re.match(
                        r"^(?P<label>.*?)\s*[([]\s*(?P<unit>[^)\]]+?)\s*[)\]]\s*$",
                        label,
                    )
                    if unit_match:
                        label = unit_match.group("label").strip()
                        inline_unit = unit_match.group("unit").strip()
                    key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
                    if not key or key in ambiguous_keys:
                        continue
                    match = re.fullmatch(
                        r"(?P<value>-?\d{1,7}(?:[.,]\d{1,4})?)\s*"
                        r"(?P<unit>%|m|ft|ppg|kg/m3|g/cm3|sg|psi|bar|barg|kpa|cp|s|min|h|hr|kn|klbf|"
                        r"rpm|l/min|m3/min|deg|°|bbl|gpm|ft/h|m/h)?",
                        raw_value,
                        re.IGNORECASE,
                    )
                    if match is None:
                        continue
                    value = _to_float(match.group("value"))
                    if value is None:
                        continue
                    if key in values:
                        values.pop(key, None)
                        units.pop(key, None)
                        field_locations.pop(key, None)
                        ambiguous_keys.add(key)
                        continue
                    values[key] = value
                    unit = match.group("unit") or inline_unit
                    if unit:
                        units[key] = unit
                    if locator := _cell_locator(region, value_cell):
                        field_locations[key] = locator
                    if source_locator is None:
                        source_locator = _table_row_locator(region, row)

            for region, region_start, _region_end in _text_region_spans(page):
                text = region.text or ""
                cursor = 0
                for raw_line in text.splitlines(keepends=True):
                    line_start = cursor
                    cursor += len(raw_line)
                    line = raw_line.rstrip("\r\n")
                    stripped = line.strip()
                    leading = len(line) - len(line.lstrip())
                    match = self._LINE_RE.match(stripped)
                    if not match:
                        continue
                    label = match.group("label").strip().lower()
                    if any(skip in label for skip in self._SKIP_LABELS):
                        continue
                    key = re.sub(r"[^a-z0-9]+", "_", label).strip("_")
                    if not key or key in ambiguous_keys:
                        continue
                    value = _to_float(match.group("value"))
                    if value is None:
                        continue
                    if key in values:
                        values.pop(key, None)
                        units.pop(key, None)
                        field_locations.pop(key, None)
                        ambiguous_keys.add(key)
                        continue
                    values[key] = value
                    if match.group("unit"):
                        units[key] = match.group("unit")
                    absolute_start = region_start + line_start + leading + match.start()
                    absolute_end = region_start + line_start + leading + match.end()
                    field_locations[key] = _text_locator(page, absolute_start, absolute_end)
                    if source_locator is None:
                        source_locator = {
                            "format": page.layout.get("source", "text"),
                            "kind": "page_text",
                            "page": page.page_number,
                            "region_order": region.order_index,
                        }

            if values:
                flags = ["unclassified_labels"]
                flags.extend(f"ambiguous_duplicate:{key}" for key in sorted(ambiguous_keys))
                region_index = _single_region_index(field_locations)
                yield ExtractedRecordDraft(
                    record_type="parameter_set",
                    payload=values,
                    page_number=page.page_number,
                    region_index=region_index,
                    confidence=0.55,
                    method="label_value",
                    method_version=self.version,
                    excerpt="; ".join(f"{key}={value}" for key, value in sorted(values.items()))[:400],
                    unit_context=units,
                    quality_flags=flags,
                    source_locator=source_locator
                    or {
                        "format": page.layout.get("source", "text"),
                        "kind": "page_text",
                        "page": page.page_number,
                    },
                    field_locations=field_locations,
                    payload_schema_key="extracted.parameter_set",
                )



def install_default_extractors() -> None:
    for extractor in (
        ReportHeaderExtractor(),
        OperationTableExtractor(),
        MudPropertiesExtractor(),
        DepthReadingExtractor(),
        SurveyTableExtractor(),
        CasingCementExtractor(),
        NumberedParameterExtractor(),
    ):
        register_extractor(extractor, replace=True)


install_default_extractors()


def extracted_at() -> dt.datetime:
    return utc_now()


def dedupe(drafts: Sequence[ExtractedRecordDraft]) -> list[ExtractedRecordDraft]:
    """Drop overlapping-extractor duplicates only when they name the same source location."""
    import json

    seen: set[str] = set()
    unique: list[ExtractedRecordDraft] = []
    for draft in drafts:
        fingerprint = json.dumps(
            {
                "record_type": draft.record_type,
                "page_number": draft.page_number,
                "payload": draft.payload,
                "region_index": draft.region_index,
                "method": draft.method,
                "method_version": draft.method_version,
                "observed_at": draft.observed_at,
                "depth_md_si": draft.depth_md_si,
                "depth_tvd_si": draft.depth_tvd_si,
                "unit_context": draft.unit_context,
                "quality_flags": draft.quality_flags,
                "excerpt": draft.excerpt,
                "payload_schema_key": draft.payload_schema_key,
                "payload_schema_version": draft.payload_schema_version,
                "source_locator": draft.source_locator,
                "field_locations": draft.field_locations,
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(draft)
    return unique
