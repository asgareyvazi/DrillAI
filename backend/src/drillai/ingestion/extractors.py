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

_DATE_PATTERNS = (
    re.compile(r"(?P<y>20\d{2})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})"),
    re.compile(r"(?P<d>\d{1,2})[-/](?P<m>\d{1,2})[-/](?P<y>20\d{2})"),
    re.compile(r"(?P<d>\d{1,2})[ -](?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ -](?P<y>20\d{2})", re.I),
)
_MONTHS = {name: index for index, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
)}

_DEPTH_RE = re.compile(r"(?P<value>\d{1,6}(?:[.,]\d{1,2})?)\s*(?P<unit>m\b|ft\b|feet\b|'|″|in\b)", re.I)
_MUD_WEIGHT_RE = re.compile(
    r"(?:\bmw\b|mud weight|mud\s*weight|\bemd\b|\bewm\b)[^0-9\-]{0,24}(?P<value>\d{1,3}(?:[.,]\d{1,3})?)\s*(?P<unit>ppg|kg/m3|kg/m³|g/cm3|sg|psi|barg)?",
    re.I,
)


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def _parse_date(text: str) -> dt.datetime | None:
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = match.groupdict()
        try:
            if groups.get("mon"):
                month = _MONTHS[groups["mon"][:3].lower()]
            else:
                month = int(groups["m"])
            return dt.datetime(int(groups["y"]), month, int(groups["d"]), tzinfo=dt.UTC)
        except (KeyError, ValueError):
            continue
    return None


def _depth_to_si(value: float, unit: str) -> float:
    lowered = unit.strip().lower()
    if lowered in {"ft", "feet", "'", "″"}:
        return value * 0.3048
    if lowered in {"in"}:
        return value * 0.0254
    return value


# --------------------------------------------------------------------------- extractors


class ReportHeaderExtractor(Extractor):
    """Header fields of a daily report: well name, report date, rig, report number."""

    key = "report_header"
    name = "Report header fields"
    version = "1.0.0"
    doc_types = ("ddr", "drilling_document", "program")
    priority = 10

    _WELL_RE = re.compile(r"(?:well|well name|wellbore)\s*[:\-]\s*(?P<name>[A-Za-z0-9][\w\-/ ]{1,60})", re.I)
    _RIG_RE = re.compile(r"(?:rig|rig name)\s*[:\-]\s*(?P<name>[A-Za-z0-9][\w\-/ ]{1,60})", re.I)
    _REPORT_NO_RE = re.compile(r"(?:report|ddr)\s*(?:no|number|#)\s*[:\-]?\s*(?P<number>\d{1,6})", re.I)

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages[:2]:
            text = page.text
            if not text.strip():
                continue
            payload: dict[str, Any] = {}
            if match := self._WELL_RE.search(text):
                payload["well_name_text"] = match.group("name").strip()
            if match := self._RIG_RE.search(text):
                payload["rig_name_text"] = match.group("name").strip()
            if match := self._REPORT_NO_RE.search(text):
                payload["report_number"] = match.group("number")
            if report_date := _parse_date(text[:2000]):
                payload["report_date"] = report_date.date().isoformat()
            if not payload:
                continue
            payload["doc_type"] = doc_type
            yield ExtractedRecordDraft(
                record_type="report_header",
                payload=payload,
                page_number=page.page_number,
                region_index=0,
                confidence=0.75,
                method="regex_header",
                method_version=self.version,
                observed_at=report_date,
                excerpt=text[:400],
                payload_schema_key="extracted.report_header",
            )


class OperationTableExtractor(Extractor):
    """Operations rows: sequence, start/end time, operation code, duration, depth."""

    key = "operations_table"
    name = "Operations / activities table"
    version = "1.0.0"
    doc_types = ("ddr", "drilling_document")
    priority = 20

    _HEADER_HINTS = ("operation", "activity", "description", "code", "duration")
    _DEPTH_IN_ROW = re.compile(r"(?P<value>\d{3,6}(?:[.,]\d{1,2})?)\s*(?P<unit>m|ft)\b", re.I)

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages:
            for region in page.regions:
                if region.kind != "table" or not region.table:
                    continue
                header = [str(cell).lower() for cell in (region.table.get("header") or [])]
                if not any(any(hint in column for hint in self._HEADER_HINTS) for column in header):
                    continue
                columns = {name: index for index, name in enumerate(header)}
                for row_index, row in enumerate(region.table["cells"][1:], start=1):
                    values = [cell["text"] for cell in row]
                    joined = " | ".join(values)
                    if not joined.strip():
                        continue
                    payload: dict[str, Any] = {"row_index": row_index}
                    for key, index in columns.items():
                        if index < len(values) and values[index].strip():
                            payload[key] = values[index].strip()
                    if duration := self._duration_hours(payload):
                        payload["duration_hours"] = duration
                    if depth := self._DEPTH_IN_ROW.search(joined):
                        payload["depth_md_si"] = _depth_to_si(
                            float(depth.group("value").replace(",", ".")), depth.group("unit")
                        )
                    yield ExtractedRecordDraft(
                        record_type="operation_row",
                        payload=payload,
                        page_number=page.page_number,
                        region_index=region.order_index,
                        confidence=0.7,
                        method="table_row",
                        method_version=self.version,
                        excerpt=joined[:400],
                        unit_context={"duration_hours": "h", "depth_md_si": "m"},
                        payload_schema_key="extracted.operation_row",
                    )

    @staticmethod
    def _duration_hours(payload: dict[str, Any]) -> float | None:
        for key, value in payload.items():
            if "duration" not in key and "time" not in key:
                continue
            match = re.search(r"(?P<h>\d{1,2})[:h ](?P<m>\d{1,2})", str(value))
            if match:
                return int(match.group("h")) + int(match.group("m")) / 60.0
            plain = _to_float(str(value))
            if plain is not None and 0 < plain < 100:
                return plain
        return None


class MudPropertiesExtractor(Extractor):
    """Mud weight and rheology readings (label/value pairs and mud-check tables)."""

    key = "mud_properties"
    name = "Mud properties"
    version = "1.0.0"
    doc_types = ("ddr", "drilling_document", "other")
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

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages:
            readings: dict[str, Any] = {}
            units: dict[str, str] = {}
            for region in [*page.regions, ParsedRegion(kind="text", order_index=99, text=page.text)]:
                lowered = (region.text or "").lower()
                if not lowered:
                    continue
                for field_name, labels in self._LABELS.items():
                    if field_name in readings:
                        continue
                    for label in labels:
                        match = re.search(
                            rf"\b{re.escape(label)}\b[^0-9\-]{{0,16}}(?P<value>\d{{1,4}}(?:[.,]\d{{1,3}})?)\s*(?P<unit>ppg|kg/m3|kg/m³|g/cm3|sg|cp|s|ml|psi)?",
                            lowered,
                        )
                        if match:
                            value = _to_float(match.group("value"))
                            if value is None:
                                continue
                            unit = match.group("unit") or ""
                            readings[field_name] = value
                            if unit:
                                units[field_name] = unit
                            break
            if readings:
                yield ExtractedRecordDraft(
                    record_type="mud_properties",
                    payload=readings,
                    page_number=page.page_number,
                    confidence=0.6,
                    method="label_value",
                    method_version=self.version,
                    excerpt="; ".join(f"{key}={value}" for key, value in sorted(readings.items())),
                    unit_context=units,
                    quality_flags=["unit_inferred"] if not units else [],
                    payload_schema_key="extracted.mud_properties",
                )


class DepthReadingExtractor(Extractor):
    """Depth readings (bit depth, hole depth, casing shoe depth) from labels."""

    key = "depth_readings"
    name = "Depth readings"
    version = "1.0.0"
    doc_types = ("ddr", "drilling_document", "other")
    priority = 40

    _LABELS = ("bit depth", "hole depth", "casing depth", "shoe depth", "td depth", "driller depth")

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages:
            found: dict[str, Any] = {}
            units: dict[str, str] = {}
            lowered = page.text.lower()
            for label in self._LABELS:
                match = re.search(
                    rf"{re.escape(label)}\s*[:\-]?\s*{_DEPTH_RE.pattern}", lowered, re.I
                )
                if not match:
                    continue
                value = _to_float(match.group("value"))
                if value is None:
                    continue
                key = label.replace(" ", "_")
                found[key + "_si"] = _depth_to_si(value, match.group("unit"))
                units[key + "_si"] = "m"
            if found:
                yield ExtractedRecordDraft(
                    record_type="depth_reading",
                    payload=found,
                    page_number=page.page_number,
                    confidence=0.7,
                    method="label_value",
                    method_version=self.version,
                    excerpt=page.text[:300],
                    unit_context=units,
                    payload_schema_key="extracted.depth_reading",
                )


class SurveyTableExtractor(Extractor):
    """Survey stations (MD, inclination, azimuth) from survey tables."""

    key = "survey_table"
    name = "Survey stations"
    version = "1.0.0"
    doc_types = ("ddr", "drilling_document", "program", "other")
    priority = 50

    _HEADER_HINTS = {
        "md": ("md", "measured depth", "depth"),
        "inclination": ("incl", "inclination"),
        "azimuth": ("azi", "azimuth"),
    }

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages:
            for region in page.regions:
                if region.kind != "table" or not region.table:
                    continue
                header = [str(cell).lower().strip() for cell in (region.table.get("header") or [])]
                if not header:
                    continue
                column_of: dict[str, int] = {}
                for field_name, hints in self._HEADER_HINTS.items():
                    for index, column in enumerate(header):
                        if any(hint in column for hint in hints):
                            column_of[field_name] = index
                            break
                if not {"md", "inclination"} <= set(column_of):
                    continue
                for row in region.table["cells"][1:]:
                    values = [str(cell["text"]) for cell in row]

                    def cell_value(field_name: str, columns=column_of, texts=values) -> str | None:
                        """Bind the row explicitly: the lookup must not read a later row's values."""
                        index = columns.get(field_name)
                        if index is None or index >= len(texts):
                            return None
                        return texts[index].strip() or None

                    raw_md = cell_value("md")
                    md = _to_float(raw_md.replace(",", ".")) if raw_md else None
                    if md is None or md <= 0:
                        continue
                    payload: dict[str, Any] = {"md_si": md}
                    if inclination := cell_value("inclination"):
                        payload["inclination_deg"] = _to_float(inclination.replace(",", "."))
                    if azimuth := cell_value("azimuth"):
                        payload["azimuth_deg"] = _to_float(azimuth.replace(",", "."))
                    yield ExtractedRecordDraft(
                        record_type="survey_station",
                        payload=payload,
                        page_number=page.page_number,
                        region_index=region.order_index,
                        confidence=0.65,
                        method="table_row",
                        method_version=self.version,
                        depth_md_si=md,
                        excerpt=" | ".join(values)[:300],
                        unit_context={"md_si": "m", "inclination_deg": "deg", "azimuth_deg": "deg"},
                        payload_schema_key="extracted.survey_station",
                    )


class CasingCementExtractor(Extractor):
    """Casing/cement records: size, weight, grade, top, shoe."""

    key = "casing_cement"
    name = "Casing and cement records"
    version = "1.0.0"
    doc_types = ("ddr", "drilling_document", "program")
    priority = 60

    _SIZE_RE = re.compile(r"(?P<size>\d{1,2}(?:[\-/]\d{1,2})?(?:\.\d{1,3})?)\s*(?:\"|in\b|inch)", re.I)
    _GRADE_RE = re.compile(r"\b(?P<grade>[A-Z]-\d{2,3}|K55|J55|L80|N80|P110|Q125|C90|C95|T95)\b")
    _LINE_RE = re.compile(r"casing|liner|cement", re.I)

    def run(self, document: ParsedDocument, *, doc_type: str) -> Iterable[ExtractedRecordDraft]:
        for page in document.pages:
            for line in page.text.splitlines():
                if not self._LINE_RE.search(line):
                    continue
                size = self._SIZE_RE.search(line)
                grade = self._GRADE_RE.search(line)
                depth = _DEPTH_RE.search(line)
                if not (size or grade):
                    continue
                payload: dict[str, Any] = {"text": line.strip()[:400]}
                if size:
                    payload["size_nominal"] = size.group("size")
                if grade:
                    payload["grade"] = grade.group("grade").upper()
                if depth:
                    payload["depth_md_si"] = _depth_to_si(
                        float(depth.group("value").replace(",", ".")), depth.group("unit")
                    )
                yield ExtractedRecordDraft(
                    record_type="casing_cement_line",
                    payload=payload,
                    page_number=page.page_number,
                    confidence=0.5,
                    method="line_pattern",
                    method_version=self.version,
                    depth_md_si=payload.get("depth_md_si"),
                    excerpt=line.strip()[:400],
                    unit_context={"depth_md_si": "m"},
                    quality_flags=["low_confidence_pattern"],
                    payload_schema_key="extracted.casing_cement",
                )


class NumberedParameterExtractor(Extractor):
    """Catch-all label/value pairs: ``label: value unit`` lines not covered above."""

    key = "label_values"
    name = "Generic label/value parameters"
    version = "1.0.0"
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
            for line in page.text.splitlines():
                match = self._LINE_RE.match(line.strip())
                if not match:
                    continue
                label = match.group("label").strip().lower()
                if any(skip in label for skip in self._SKIP_LABELS):
                    continue
                key = re.sub(r"[^a-z0-9]+", "_", label).strip("_")
                if not key or key in values:
                    continue
                value = _to_float(match.group("value"))
                if value is None:
                    continue
                values[key] = value
                if match.group("unit"):
                    units[key] = match.group("unit")
            if values:
                yield ExtractedRecordDraft(
                    record_type="parameter_set",
                    payload=values,
                    page_number=page.page_number,
                    confidence=0.55,
                    method="label_value",
                    method_version=self.version,
                    excerpt="; ".join(f"{key}={value}" for key, value in sorted(values.items()))[:400],
                    unit_context=units,
                    quality_flags=["unclassified_labels"],
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
    """Drop exact duplicates produced by overlapping extractors (payload + page)."""
    seen: set[tuple[str, int, str]] = set()
    unique: list[ExtractedRecordDraft] = []
    for draft in drafts:
        fingerprint = (draft.record_type, draft.page_number, repr(sorted(draft.payload.items())))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(draft)
    return unique
