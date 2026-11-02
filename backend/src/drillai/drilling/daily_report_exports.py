"""Secure document renderers for immutable daily-report revisions.

The API performs tenant authorization, immutable-state checks, fingerprint verification and auditing.
This module owns only deterministic presentation: it never queries or changes domain state.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import threading
from importlib.resources import as_file, files
from io import BytesIO
from typing import Any
from xml.sax.saxutils import escape

import arabic_reshaper
from bidi.algorithm import get_display
from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

EXPORT_MEDIA_TYPES = {
    "json": "application/json; charset=utf-8",
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
EXPORT_EXTENSIONS = {key: key for key in EXPORT_MEDIA_TYPES}
MAX_EXPORT_BYTES = 20 * 1024 * 1024
_FONT_LOCK = threading.Lock()
_FONT_REGULAR = "DrillAI-DejaVu"
_FONT_BOLD = "DrillAI-DejaVu-Bold"
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")


class DailyReportExportError(ValueError):
    """Raised when an export cannot safely represent the supplied revision data."""


_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "title": "Daily Drilling Report",
        "report": "Report",
        "revision": "Revision",
        "edit_version": "Edit version",
        "status": "Status",
        "report_number": "Report number",
        "well_id": "Well ID",
        "wellbore_id": "Wellbore ID",
        "report_date": "Report date",
        "timezone": "Report timezone",
        "shift": "Shift",
        "source_kind": "Source kind",
        "period": "Reporting period",
        "created_by": "Created by",
        "created_at": "Created at",
        "submitted_by": "Submitted by",
        "submitted_at": "Submitted at",
        "decided_by": "Decision by",
        "decided_at": "Decision at",
        "decision_reason": "Decision reason",
        "content_hash": "Revision content SHA-256",
        "fingerprint_verified": "Fingerprint verified",
        "summary": "Summary",
        "operations_summary": "Operations summary",
        "safety_notes": "Safety notes",
        "next_period_plan": "Next-period plan",
        "environmental_notes": "Environmental notes",
        "activities": "Activities",
        "activity": "Activity",
        "activity_kind": "Activity type",
        "operation_kind": "Operation type",
        "operation_id": "Linked operation ID",
        "event_id": "Linked event ID",
        "npt_category": "NPT category",
        "title_field": "Title",
        "description": "Description",
        "start_at": "Start time",
        "start_time_basis": "Start-time basis",
        "end_at": "End time",
        "end_time_basis": "End-time basis",
        "duration": "Duration",
        "duration_basis": "Duration basis",
        "depth_from": "Entered depth from",
        "depth_to": "Entered depth to",
        "canonical_depth_from": "Canonical depth from (m)",
        "canonical_depth_to": "Canonical depth to (m)",
        "depth_from_si": "Canonical depth from (m)",
        "depth_to_si": "Canonical depth to (m)",
        "canonical_depth": "Canonical associated depth (m)",
        "depth_si": "Canonical associated depth (m)",
        "depth_reference": "Depth reference",
        "depth_basis": "Depth basis",
        "cause_basis": "Cause basis",
        "measurements": "Measurements",
        "parameter": "Parameter",
        "dimension": "Dimension",
        "source_value": "Source value",
        "source_unit": "Source unit",
        "accepted_value": "Accepted input value",
        "accepted_unit": "Accepted input unit",
        "canonical_value": "Canonical value",
        "canonical_unit": "Canonical unit",
        "value_basis": "Value basis",
        "observed_at": "Observation time",
        "time_basis": "Time basis",
        "depth_value": "Associated depth",
        "source_evidence": "Source evidence",
        "source_document_id": "Source document ID",
        "source_record_id": "Source record ID",
        "source_region_id": "Source region ID",
        "source_page_number": "Source page",
        "reconciliation": "Source reconciliation",
        "field_key": "Field / target",
        "target_kind": "Target kind",
        "source_path": "Source path",
        "source_review_version": "Source review version",
        "source_fingerprint": "Source fingerprint",
        "source_value_hash": "Source-value SHA-256",
        "proposal_hash": "Proposal SHA-256",
        "proposal_matches": "Proposal matches current value",
        "resolution": "Disposition",
        "reason": "Review reason",
        "decision_actor": "Decision actor",
        "no_entries": "No entries recorded",
        "not_recorded": "Not recorded",
        "yes": "Yes",
        "no": "No",
        "unknown": "Unknown",
        "manual": "Manual entry",
        "import": "Document import",
        "connector": "Connector",
        "draft": "Draft",
        "submitted": "Submitted — awaiting independent review",
        "approved": "Approved",
        "rejected": "Rejected — not approved",
        "withdrawn": "Withdrawn",
        "unapproved_notice": "UNAPPROVED — this revision has not been approved.",
        "rejected_notice": "REJECTED — this revision must not be treated as approved.",
        "withdrawn_notice": "WITHDRAWN — this revision is retained for audit only.",
        "submitted_notice": "SUBMITTED — pending independent review; not approved.",
        "export_format": "Export format",
        "export_locale": "Export language",
        "english": "English",
        "persian": "Persian",
        "generated_at": "Export generated at",
        "export_note_label": "Export and engineering-review notice",
        "page": "Page",
        "export_note": "This export represents the exact immutable revision identified above. Source evidence and decision provenance are preserved; no additional engineering validation is implied.",
        "day": "Day",
        "night": "Night",
        "24h": "24-hour",
        "custom": "Custom",
        "operation": "Operation",
        "npt": "Non-productive time",
        "event": "Event",
        "safety": "Safety",
        "milestone": "Milestone",
        "other": "Other",
        "accepted": "Accepted as extracted",
        "corrected": "Corrected by reviewer",
        "rejected_resolution": "Rejected",
        "unresolved": "Unresolved",
        "proposed": "Proposed — not yet reviewed",
        "reported": "Reported",
        "observed": "Observed",
        "calculated": "Calculated",
        "planned": "Planned",
        "inferred": "Inferred",
        "recorded": "Recorded",
    },
    "fa": {
        "title": "گزارش روزانه حفاری",
        "report": "گزارش",
        "revision": "نسخه",
        "edit_version": "نسخه ویرایش",
        "status": "وضعیت",
        "report_number": "شماره گزارش",
        "well_id": "شناسه چاه",
        "wellbore_id": "شناسه چاه‌پیمایی",
        "report_date": "تاریخ گزارش",
        "timezone": "منطقه زمانی گزارش",
        "shift": "شیفت",
        "source_kind": "نوع منبع",
        "period": "بازه گزارش",
        "created_by": "ایجادکننده",
        "created_at": "زمان ایجاد",
        "submitted_by": "ارسال‌کننده برای بررسی",
        "submitted_at": "زمان ارسال",
        "decided_by": "تصمیم‌گیرنده",
        "decided_at": "زمان تصمیم",
        "decision_reason": "دلیل تصمیم",
        "content_hash": "اثر انگشت محتوای نسخه (SHA-256)",
        "fingerprint_verified": "تأیید اثر انگشت",
        "summary": "خلاصه",
        "operations_summary": "خلاصه عملیات",
        "safety_notes": "یادداشت‌های ایمنی",
        "next_period_plan": "برنامه بازه بعدی",
        "environmental_notes": "یادداشت‌های زیست‌محیطی",
        "activities": "فعالیت‌ها",
        "activity": "فعالیت",
        "activity_kind": "نوع فعالیت",
        "operation_kind": "نوع عملیات",
        "operation_id": "شناسه عملیات پیوندشده",
        "event_id": "شناسه رویداد پیوندشده",
        "npt_category": "دسته زمان غیرمولد",
        "title_field": "عنوان",
        "description": "شرح",
        "start_at": "زمان شروع",
        "start_time_basis": "مبنای زمان شروع",
        "end_at": "زمان پایان",
        "end_time_basis": "مبنای زمان پایان",
        "duration": "مدت‌زمان",
        "duration_basis": "مبنای مدت‌زمان",
        "depth_from": "عمق ورودی از",
        "depth_to": "عمق ورودی تا",
        "canonical_depth_from": "عمق استاندارد از (متر)",
        "canonical_depth_to": "عمق استاندارد تا (متر)",
        "depth_from_si": "عمق استاندارد از (متر)",
        "depth_to_si": "عمق استاندارد تا (متر)",
        "canonical_depth": "عمق وابسته استاندارد (متر)",
        "depth_si": "عمق وابسته استاندارد (متر)",
        "depth_reference": "مرجع عمق",
        "depth_basis": "مبنای عمق",
        "cause_basis": "مبنای علت",
        "measurements": "اندازه‌گیری‌ها",
        "parameter": "پارامتر",
        "dimension": "بُعد",
        "source_value": "مقدار منبع",
        "source_unit": "واحد منبع",
        "accepted_value": "مقدار ورودی پذیرفته‌شده",
        "accepted_unit": "واحد ورودی پذیرفته‌شده",
        "canonical_value": "مقدار استاندارد",
        "canonical_unit": "واحد استاندارد",
        "value_basis": "مبنای مقدار",
        "observed_at": "زمان مشاهده",
        "time_basis": "مبنای زمان",
        "depth_value": "عمق وابسته",
        "source_evidence": "شواهد منبع",
        "source_document_id": "شناسه سند منبع",
        "source_record_id": "شناسه رکورد منبع",
        "source_region_id": "شناسه ناحیه منبع",
        "source_page_number": "صفحه منبع",
        "reconciliation": "تطبیق منبع",
        "field_key": "فیلد / هدف",
        "target_kind": "نوع هدف",
        "source_path": "مسیر منبع",
        "source_review_version": "نسخه بازبینی منبع",
        "source_fingerprint": "اثر انگشت منبع",
        "source_value_hash": "اثر انگشت مقدار منبع",
        "proposal_hash": "اثر انگشت پیشنهاد",
        "proposal_matches": "انطباق پیشنهاد با مقدار فعلی",
        "resolution": "نتیجه بررسی",
        "reason": "دلیل بررسی",
        "decision_actor": "کاربر تصمیم‌گیرنده",
        "no_entries": "رکوردی ثبت نشده است",
        "not_recorded": "ثبت نشده",
        "yes": "بله",
        "no": "خیر",
        "unknown": "نامشخص",
        "manual": "ورود دستی",
        "import": "ورود از سند",
        "connector": "اتصال‌دهنده",
        "draft": "پیش‌نویس",
        "submitted": "ارسال‌شده — در انتظار بررسی مستقل",
        "approved": "تأییدشده",
        "rejected": "ردشده — تأییدنشده",
        "withdrawn": "پس‌گرفته‌شده",
        "unapproved_notice": "تأییدنشده — این نسخه هنوز تأیید نشده است.",
        "rejected_notice": "ردشده — این نسخه نباید تأییدشده تلقی شود.",
        "withdrawn_notice": "پس‌گرفته‌شده — این نسخه فقط برای ممیزی نگهداری می‌شود.",
        "submitted_notice": "ارسال‌شده — در انتظار بررسی مستقل؛ تأیید نشده است.",
        "export_format": "قالب خروجی",
        "export_locale": "زبان خروجی",
        "english": "انگلیسی",
        "persian": "فارسی",
        "generated_at": "زمان تولید خروجی",
        "export_note_label": "یادداشت خروجی و بازبینی مهندسی",
        "page": "صفحه",
        "export_note": "این خروجی دقیقاً نمایانگر نسخه تغییرناپذیر مشخص‌شده در بالا است. شواهد منبع و سوابق تصمیم حفظ شده‌اند؛ این خروجی به‌تنهایی دلالت بر تأیید مهندسی بیشتر ندارد.",
        "day": "روز",
        "night": "شب",
        "24h": "۲۴ ساعته",
        "custom": "سفارشی",
        "operation": "عملیات",
        "npt": "زمان غیرمولد",
        "event": "رویداد",
        "safety": "ایمنی",
        "milestone": "نقطه عطف",
        "other": "سایر",
        "accepted": "پذیرفته‌شده به‌عنوان مقدار استخراج‌شده",
        "corrected": "اصلاح‌شده توسط بازبین",
        "rejected_resolution": "ردشده",
        "unresolved": "حل‌نشده",
        "proposed": "پیشنهادشده — هنوز بررسی نشده",
        "reported": "گزارش‌شده",
        "observed": "مشاهده‌شده",
        "calculated": "محاسبه‌شده",
        "planned": "برنامه‌ریزی‌شده",
        "inferred": "استنباط‌شده",
        "recorded": "ثبت‌شده",
    },
}


_STATUS_LABELS = {
    "draft": "draft",
    "submitted": "submitted",
    "approved": "approved",
    "rejected": "rejected",
    "withdrawn": "withdrawn",
}
_WARNING_LABELS = {
    "submitted": "submitted_notice",
    "rejected": "rejected_notice",
    "withdrawn": "withdrawn_notice",
}
_ENUM_LABELS: dict[str, dict[str, dict[str, str]]] = {
    "operation_kind": {
        "en": {
            "spud": "Spud", "drilling": "Drilling", "slide_drilling": "Slide drilling",
            "rotary_drilling": "Rotary drilling", "reaming": "Reaming", "back_reaming": "Back reaming",
            "connection": "Connection", "trip_in": "Trip in", "trip_out": "Trip out",
            "wiper_trip": "Wiper trip", "casing": "Casing", "cementing": "Cementing",
            "curing_woc": "Waiting on cement", "nipple_up": "Nipple up", "pressure_test": "Pressure test",
            "mud_conditioning": "Mud conditioning", "circulating": "Circulating", "logging": "Logging",
            "coring": "Coring", "fishing": "Fishing", "milling": "Milling", "jarring": "Jarring",
            "blow_out_preventer_test": "BOP test", "well_control": "Well control", "completion": "Completion",
            "perforating": "Perforating", "stimulation": "Stimulation", "well_testing": "Well testing",
            "well_cleanup": "Well cleanup", "suspension": "Suspension", "abandonment": "Abandonment",
            "p_and_a": "Plug and abandon", "rig_up": "Rig up", "rig_down": "Rig down", "move": "Move",
            "maintenance": "Maintenance", "waiting": "Waiting", "other": "Other operation",
        },
        "fa": {
            "spud": "آغاز حفاری", "drilling": "حفاری", "slide_drilling": "حفاری لغزشی",
            "rotary_drilling": "حفاری چرخشی", "reaming": "گشادکاری", "back_reaming": "گشادکاری برگشتی",
            "connection": "اتصال لوله", "trip_in": "پایین‌فرستادن رشته", "trip_out": "بیرون‌کشیدن رشته",
            "wiper_trip": "مانور پاک‌سازی", "casing": "جداری‌رانی", "cementing": "سیمان‌کاری",
            "curing_woc": "انتظار گیرش سیمان", "nipple_up": "نصب تجهیزات سرچاهی", "pressure_test": "آزمون فشار",
            "mud_conditioning": "آماده‌سازی گل", "circulating": "گردش سیال", "logging": "چاه‌پیمایی",
            "coring": "مغزه‌گیری", "fishing": "ماهیگیری ابزار", "milling": "فرزکاری", "jarring": "ضربه‌زنی",
            "blow_out_preventer_test": "آزمون فوران‌گیر", "well_control": "کنترل چاه", "completion": "تکمیل چاه",
            "perforating": "مشبک‌کاری", "stimulation": "تحریک چاه", "well_testing": "آزمون چاه",
            "well_cleanup": "تمیزکاری چاه", "suspension": "تعلیق عملیات", "abandonment": "متروکه‌سازی",
            "p_and_a": "مسدودسازی و متروکه‌سازی", "rig_up": "برپایی دکل", "rig_down": "جمع‌آوری دکل",
            "move": "جابجایی", "maintenance": "نگهداری", "waiting": "انتظار", "other": "سایر عملیات",
        },
    },
    "npt_category": {
        "en": {
            "equipment_failure": "Equipment failure", "surface_equipment": "Surface equipment",
            "downhole_tools": "Downhole tools", "hole_problems": "Hole problems",
            "lost_circulation": "Lost circulation", "stuck_pipe": "Stuck pipe", "well_control": "Well control",
            "third_party": "Third party", "weather": "Weather", "waiting": "Waiting",
            "unclassified": "Unclassified", "not_npt": "Not NPT",
        },
        "fa": {
            "equipment_failure": "خرابی تجهیزات", "surface_equipment": "تجهیزات سطحی",
            "downhole_tools": "ابزارهای درون‌چاهی", "hole_problems": "مشکلات چاه",
            "lost_circulation": "هرزروی سیال", "stuck_pipe": "گیرکردن رشته", "well_control": "کنترل چاه",
            "third_party": "شخص ثالث", "weather": "آب‌وهوا", "waiting": "انتظار",
            "unclassified": "دسته‌بندی‌نشده", "not_npt": "زمان غیرمولد نیست",
        },
    },
    "parameter_key": {
        "en": {
            "depth_md": "Measured depth", "depth_tvd": "True vertical depth", "depth_tvdss": "TVD subsea",
            "bit_depth_md": "Bit depth", "hole_depth_md": "Hole depth", "daily_progress_md": "Daily progress",
            "rop": "Rate of penetration", "wob": "Weight on bit", "hookload": "Hook load",
            "rotary_speed": "Rotary speed", "flow_rate": "Flow rate", "standpipe_pressure": "Standpipe pressure",
            "torque": "Torque", "mud_weight": "Mud weight", "mud_weight_in": "Inlet mud weight",
            "mud_weight_out": "Outlet mud weight", "mud_temperature": "Mud temperature",
            "funnel_viscosity": "Funnel viscosity",
        },
        "fa": {
            "depth_md": "عمق اندازه‌گیری‌شده", "depth_tvd": "عمق قائم حقیقی", "depth_tvdss": "عمق قائم زیر سطح دریا",
            "bit_depth_md": "عمق مته", "hole_depth_md": "عمق چاه", "daily_progress_md": "پیشروی روزانه",
            "rop": "سرعت حفاری", "wob": "وزن روی مته", "hookload": "بار قلاب", "rotary_speed": "سرعت چرخش",
            "flow_rate": "دبی", "standpipe_pressure": "فشار لوله ایستاده", "torque": "گشتاور",
            "mud_weight": "وزن گل", "mud_weight_in": "وزن گل ورودی", "mud_weight_out": "وزن گل خروجی",
            "mud_temperature": "دمای گل", "funnel_viscosity": "گرانروی قیفی",
        },
    },
    "target_kind": {
        "en": {"revision_field": "Report field", "activity": "Activity row", "measurement": "Measurement row"},
        "fa": {"revision_field": "فیلد گزارش", "activity": "ردیف فعالیت", "measurement": "ردیف اندازه‌گیری"},
    },
}


def _label(locale: str, key: str) -> str:
    return _LABELS[locale].get(key, key.replace("_", " ").capitalize())


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise DailyReportExportError("non-finite numeric value cannot be exported")
        value = format(value, ".12g")
    elif isinstance(value, bool):
        value = "true" if value else "false"
    return _CONTROL_CHARS.sub("�", str(value))


def _display_text(value: Any, locale: str) -> str:
    text = _clean_text(value)
    if not text:
        return _label(locale, "not_recorded")
    if locale == "fa" or _ARABIC_SCRIPT.search(text):
        try:
            return get_display(arabic_reshaper.reshape(text), base_dir="R")
        except (ValueError, TypeError):
            return text
    return text


def _enum_text(value: Any, locale: str) -> str:
    raw = _clean_text(value)
    if not raw:
        return _label(locale, "not_recorded")
    return _label(locale, raw) if raw in _LABELS[locale] else raw


def _domain_enum_text(value: Any, category: str, locale: str) -> str:
    raw = _clean_text(value)
    if not raw:
        return _label(locale, "not_recorded")
    return _ENUM_LABELS.get(category, {}).get(locale, {}).get(raw, _enum_text(raw, locale))


def _source_quantity(value: Any, unit: Any, locale: str) -> str:
    if value is None:
        return _label(locale, "not_recorded")
    rendered = _clean_text(value)
    unit_text = _clean_text(unit)
    return f"{rendered} {unit_text}" if unit_text else rendered


def _source_rows(item: dict[str, Any], locale: str) -> list[tuple[str, str]]:
    fields = (
        ("source_document_id", "source_document_id"),
        ("source_record_id", "source_record_id"),
        ("source_region_id", "source_region_id"),
        ("source_page_number", "source_page_number"),
    )
    return [(_label(locale, label), _clean_text(item.get(key))) for key, label in fields if item.get(key) is not None]


def _metadata_rows(report: dict[str, Any], revision: dict[str, Any], manifest: dict[str, Any], locale: str) -> list[tuple[str, str]]:
    status = _clean_text(revision.get("status"))
    shift = _clean_text(report.get("shift_code"))
    period_start = _clean_text(revision.get("period_start_at"))
    period_end = _clean_text(revision.get("period_end_at"))
    period = " — ".join(value for value in (period_start, period_end) if value) or _label(locale, "not_recorded")
    return [
        (_label(locale, "report_number"), _clean_text(report.get("report_number"))),
        (_label(locale, "well_id"), _clean_text(report.get("well_id"))),
        (_label(locale, "wellbore_id"), _clean_text(report.get("wellbore_id"))),
        (_label(locale, "report_date"), _clean_text(report.get("report_date"))),
        (_label(locale, "timezone"), _clean_text(report.get("report_timezone"))),
        (_label(locale, "shift"), _enum_text(shift, locale)),
        (_label(locale, "revision"), f"{revision.get('revision_number')} · {_clean_text(revision.get('id'))}"),
        (_label(locale, "edit_version"), _clean_text(revision.get("edit_version"))),
        (_label(locale, "status"), _label(locale, _STATUS_LABELS.get(status, status))),
        (_label(locale, "source_kind"), _enum_text(revision.get("source_kind"), locale)),
        (_label(locale, "period"), period),
        (_label(locale, "created_by"), _clean_text(revision.get("created_by"))),
        (_label(locale, "created_at"), _clean_text(revision.get("created_at"))),
        (_label(locale, "submitted_by"), _clean_text(revision.get("submitted_by"))),
        (_label(locale, "submitted_at"), _clean_text(revision.get("submitted_at"))),
        (_label(locale, "decided_by"), _clean_text(revision.get("decided_by"))),
        (_label(locale, "decided_at"), _clean_text(revision.get("decided_at"))),
        (_label(locale, "decision_reason"), _clean_text(revision.get("decision_reason"))),
        (_label(locale, "content_hash"), _clean_text(revision.get("content_hash"))),
        (_label(locale, "fingerprint_verified"), _label(locale, "yes") if revision.get("content_hash_verified") else _label(locale, "no")),
        (_label(locale, "export_format"), _clean_text(manifest.get("format")).upper()),
        (_label(locale, "export_locale"), _label(locale, "english" if locale == "en" else "persian")),
        (_label(locale, "generated_at"), _clean_text(manifest.get("generated_at"))),
    ]


def _narrative_rows(revision: dict[str, Any], locale: str) -> list[tuple[str, str]]:
    return [
        (_label(locale, key), _clean_text(revision.get(key)) or _label(locale, "not_recorded"))
        for key in ("summary", "operations_summary", "safety_notes", "next_period_plan", "environmental_notes")
    ]


def _activity_rows(activity: dict[str, Any], index: int, locale: str) -> list[tuple[str, str]]:
    duration = activity.get("duration_source_value")
    duration_text = _label(locale, "not_recorded")
    if duration is not None:
        duration_text = f"{_clean_text(duration)} {_clean_text(activity.get('duration_source_unit'))}; { _clean_text(activity.get('duration_seconds')) } s canonical"
    rows = [
        (_label(locale, "activity"), str(index + 1)),
        (_label(locale, "activity_kind"), _enum_text(activity.get("activity_kind"), locale)),
        (_label(locale, "operation_kind"), _domain_enum_text(activity.get("operation_kind"), "operation_kind", locale)),
        (_label(locale, "npt_category"), _domain_enum_text(activity.get("npt_category"), "npt_category", locale)),
        (_label(locale, "title_field"), _clean_text(activity.get("title"))),
        (_label(locale, "description"), _clean_text(activity.get("description"))),
        (_label(locale, "start_at"), _clean_text(activity.get("start_at"))),
        (_label(locale, "start_time_basis"), _enum_text(activity.get("start_time_basis"), locale)),
        (_label(locale, "end_at"), _clean_text(activity.get("end_at"))),
        (_label(locale, "end_time_basis"), _enum_text(activity.get("end_time_basis"), locale)),
        (_label(locale, "duration"), duration_text),
        (_label(locale, "duration_basis"), _enum_text(activity.get("duration_basis"), locale)),
        (_label(locale, "depth_from"), _source_quantity(activity.get("depth_from_source_value"), activity.get("depth_from_source_unit"), locale)),
        (_label(locale, "depth_to"), _source_quantity(activity.get("depth_to_source_value"), activity.get("depth_to_source_unit"), locale)),
        (_label(locale, "canonical_depth_from"), _source_quantity(activity.get("depth_from_si"), "m", locale)),
        (_label(locale, "canonical_depth_to"), _source_quantity(activity.get("depth_to_si"), "m", locale)),
        (_label(locale, "depth_reference"), _enum_text(activity.get("depth_reference"), locale)),
        (_label(locale, "depth_basis"), _enum_text(activity.get("depth_basis"), locale)),
        (_label(locale, "cause_basis"), _enum_text(activity.get("cause_basis"), locale)),
        (_label(locale, "operation_id"), _clean_text(activity.get("operation_id"))),
        (_label(locale, "event_id"), _clean_text(activity.get("event_id"))),
        (_label(locale, "resolution"), _enum_text(activity.get("resolution_state"), locale)),
        *_source_rows(activity, locale),
    ]
    return [(label, value or _label(locale, "not_recorded")) for label, value in rows]


def _measurement_rows(item: dict[str, Any], index: int, locale: str) -> list[tuple[str, str]]:
    rows = [
        (_label(locale, "activity"), str(index + 1)),
        (_label(locale, "parameter"), _domain_enum_text(item.get("parameter_key"), "parameter_key", locale)),
        (_label(locale, "dimension"), _clean_text(item.get("dimension"))),
        (_label(locale, "source_value"), _clean_text(item.get("source_value"))),
        (_label(locale, "source_unit"), _clean_text(item.get("source_unit"))),
        (_label(locale, "accepted_value"), _clean_text(item.get("accepted_input_value"))),
        (_label(locale, "accepted_unit"), _clean_text(item.get("accepted_input_unit"))),
        (_label(locale, "canonical_value"), _clean_text(item.get("value_si"))),
        (_label(locale, "canonical_unit"), _clean_text(item.get("canonical_unit"))),
        (_label(locale, "value_basis"), _enum_text(item.get("value_basis"), locale)),
        (_label(locale, "observed_at"), _clean_text(item.get("observed_at"))),
        (_label(locale, "time_basis"), _enum_text(item.get("time_basis"), locale)),
        (_label(locale, "depth_value"), " · ".join(value for value in (
            _source_quantity(item.get("depth_source_value"), item.get("depth_source_unit"), locale)
            if item.get("depth_source_value") is not None else "",
            _enum_text(item.get("depth_reference"), locale) if item.get("depth_source_value") is not None else "",
            _enum_text(item.get("depth_basis"), locale) if item.get("depth_source_value") is not None else "",
        ) if value) or _label(locale, "not_recorded")),
        (_label(locale, "canonical_depth"), _source_quantity(item.get("depth_si"), "m", locale)),
        (_label(locale, "resolution"), _enum_text(item.get("resolution_state"), locale)),
        *_source_rows(item, locale),
    ]
    return [(label, value or _label(locale, "not_recorded")) for label, value in rows]


def _reconciliation_rows(item: dict[str, Any], index: int, locale: str) -> list[tuple[str, str]]:
    rows = [
        (_label(locale, "activity"), str(index + 1)),
        (_label(locale, "target_kind"), _domain_enum_text(item.get("target_kind"), "target_kind", locale)),
        (_label(locale, "field_key"), _clean_text(item.get("field_key"))),
        (_label(locale, "source_path"), _clean_text(item.get("source_path"))),
        (_label(locale, "source_review_version"), _clean_text(item.get("source_review_version"))),
        (_label(locale, "source_fingerprint"), _clean_text(item.get("source_fingerprint"))),
        (_label(locale, "source_value_hash"), _clean_text(item.get("source_value_hash"))),
        (_label(locale, "proposal_hash"), _clean_text(item.get("proposed_value_hash"))),
        (_label(locale, "proposal_matches"), _label(locale, "yes") if item.get("proposal_matches_current") else _label(locale, "no")),
        (_label(locale, "resolution"), _enum_text(item.get("resolution_state"), locale)),
        (_label(locale, "reason"), _clean_text(item.get("reason"))),
        (_label(locale, "decision_actor"), _clean_text(item.get("decided_by"))),
        (_label(locale, "decided_at"), _clean_text(item.get("decided_at"))),
        *_source_rows(item, locale),
    ]
    return [(label, value or _label(locale, "not_recorded")) for label, value in rows]


def _warning(locale: str, status: str) -> str | None:
    if status == "approved":
        return None
    key = _WARNING_LABELS.get(status)
    if key:
        return _label(locale, key)
    return _label(locale, "unapproved_notice")


def _require_pdf_fonts() -> None:
    if _FONT_REGULAR in pdfmetrics.getRegisteredFontNames():
        return
    with _FONT_LOCK:
        if _FONT_REGULAR in pdfmetrics.getRegisteredFontNames():
            return
        regular = files("drillai").joinpath("assets", "fonts", "DejaVuSans.ttf")
        bold = files("drillai").joinpath("assets", "fonts", "DejaVuSans-Bold.ttf")
        with as_file(regular) as regular_path, as_file(bold) as bold_path:
            pdfmetrics.registerFont(TTFont(_FONT_REGULAR, str(regular_path)))
            pdfmetrics.registerFont(TTFont(_FONT_BOLD, str(bold_path)))


def _pdf_paragraph(text: Any, style: ParagraphStyle, locale: str) -> Paragraph:
    value = _display_text(text, locale).replace("\n", "<br/>")
    return Paragraph(escape(value), style)


def _render_pdf(report: dict[str, Any], revision: dict[str, Any], manifest: dict[str, Any], locale: str) -> bytes:
    _require_pdf_fonts()
    buffer = BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"Daily report {report.get('id')} revision {revision.get('revision_number')}",
        author="DrillAI",
        subject=f"Immutable daily report revision {revision.get('content_hash')}",
        allowSplitting=1,
    )
    align = TA_RIGHT if locale == "fa" else TA_LEFT
    base = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "DDRTitle", parent=base["Title"], fontName=_FONT_BOLD, fontSize=17, leading=22,
        alignment=align, textColor=colors.HexColor("#153448"), spaceAfter=7,
    )
    h1 = ParagraphStyle(
        "DDRSection", parent=base["Heading2"], fontName=_FONT_BOLD, fontSize=11.5, leading=15,
        alignment=align, textColor=colors.HexColor("#153448"), spaceBefore=9, spaceAfter=5,
        keepWithNext=True,
    )
    body = ParagraphStyle(
        "DDRBody", parent=base["BodyText"], fontName=_FONT_REGULAR, fontSize=8.2, leading=11,
        alignment=align, wordWrap="CJK", allowWidows=0, allowOrphans=0,
    )
    label_style = ParagraphStyle(
        "DDRLabel", parent=body, fontName=_FONT_BOLD, textColor=colors.HexColor("#334e5c"),
    )
    warning_style = ParagraphStyle(
        "DDRWarning", parent=body, fontName=_FONT_BOLD, textColor=colors.HexColor("#9a3412"),
        borderColor=colors.HexColor("#fdba74"), borderWidth=0.5, borderPadding=5,
        backColor=colors.HexColor("#fff7ed"), spaceAfter=6,
    )

    def kv_table(rows: list[tuple[str, str]], *, compact: bool = False) -> Table:
        data = []
        for key, value in rows:
            left = _pdf_paragraph(value if locale == "fa" else key, body, locale)
            right = _pdf_paragraph(key if locale == "fa" else value, label_style if locale == "fa" else body, locale)
            data.append([left, right] if locale == "fa" else [right, left])
        widths = [document.width * 0.31, document.width * 0.69]
        if locale == "fa":
            widths.reverse()
        table = Table(data, colWidths=widths, hAlign="RIGHT" if locale == "fa" else "LEFT", repeatRows=0)
        padding = 2 if compact else 3
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), padding),
            ("BOTTOMPADDING", (0, 0), (-1, -1), padding),
            ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#d8e0e4")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f6f7")),
        ]))
        return table

    story: list[Any] = [
        _pdf_paragraph(_label(locale, "title"), title_style, locale),
        _pdf_paragraph(f"{_label(locale, 'report')}: {report.get('id')} · {_label(locale, 'revision')} {revision.get('revision_number')}", body, locale),
        Spacer(1, 4),
    ]
    status = _clean_text(revision.get("status"))
    notice = _warning(locale, status)
    if notice:
        story.extend([_pdf_paragraph(notice, warning_style, locale), Spacer(1, 3)])
    story.append(_pdf_paragraph(_label(locale, "report"), h1, locale))
    story.append(kv_table(_metadata_rows(report, revision, manifest, locale)))
    story.append(_pdf_paragraph(_label(locale, "summary"), h1, locale))
    story.append(kv_table(_narrative_rows(revision, locale)))

    for section_key, getter in (
        ("activities", _activity_rows),
        ("measurements", _measurement_rows),
        ("reconciliation", _reconciliation_rows),
    ):
        items = revision.get(section_key) or []
        story.append(_pdf_paragraph(_label(locale, section_key), h1, locale))
        if not items:
            story.append(_pdf_paragraph(_label(locale, "no_entries"), body, locale))
            continue
        for index, item in enumerate(items):
            story.append(kv_table(getter(item, index, locale), compact=True))
            story.append(Spacer(1, 4))

    story.extend([Spacer(1, 7), _pdf_paragraph(_label(locale, "export_note"), body, locale)])

    def page_footer(canvas, page_document) -> None:
        canvas.saveState()
        canvas.setFont(_FONT_REGULAR, 7)
        footer = f"{report.get('id')} · r{revision.get('revision_number')} · {_label(locale, 'page')} {page_document.page}"
        canvas.drawRightString(A4[0] - 16 * mm, 10 * mm, _display_text(footer, locale))
        canvas.restoreState()

    document.build(story, onFirstPage=page_footer, onLaterPages=page_footer)
    return buffer.getvalue()


def _set_docx_rtl(paragraph, locale: str) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT if locale == "fa" else WD_ALIGN_PARAGRAPH.LEFT
    if locale != "fa":
        return
    p_pr = paragraph._p.get_or_add_pPr()
    bidi = p_pr.find(qn("w:bidi"))
    if bidi is None:
        p_pr.append(OxmlElement("w:bidi"))


def _docx_add_text(paragraph, value: Any, locale: str, *, bold: bool = False, color: str | None = None) -> None:
    run = paragraph.add_run(_clean_text(value) or _label(locale, "not_recorded"))
    run.bold = bold
    run.font.name = "Arial"
    run.font.size = Pt(9)
    if color:
        run.font.color.rgb = RGBColor.from_string(color)
    if locale == "fa":
        r_pr = run._element.get_or_add_rPr()
        r_fonts = r_pr.rFonts
        if r_fonts is None:
            r_fonts = OxmlElement("w:rFonts")
            r_pr.insert(0, r_fonts)
        r_fonts.set(qn("w:cs"), "Arial")
        if r_pr.find(qn("w:rtl")) is None:
            r_pr.append(OxmlElement("w:rtl"))


def _docx_add_heading(document: Document, value: str, level: int, locale: str) -> None:
    paragraph = document.add_heading(level=level)
    _docx_add_text(paragraph, value, locale, bold=True, color="153448")
    _set_docx_rtl(paragraph, locale)


def _docx_table(document: Document, rows: list[tuple[str, str]], locale: str) -> None:
    table = document.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    if locale == "fa":
        table._tbl.tblPr.append(OxmlElement("w:bidiVisual"))
    for label, value in rows:
        cells = table.add_row().cells
        cells[0].text = ""
        cells[1].text = ""
        cells[0].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
        cells[1].vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
        for cell, text, bold in ((cells[0], label, True), (cells[1], value or _label(locale, "not_recorded"), False)):
            paragraph = cell.paragraphs[0]
            _docx_add_text(paragraph, text, locale, bold=bold)
            _set_docx_rtl(paragraph, locale)
    document.add_paragraph()


def _render_docx(report: dict[str, Any], revision: dict[str, Any], manifest: dict[str, Any], locale: str) -> bytes:
    document = Document()
    section = document.sections[0]
    section.top_margin = Inches(0.65)
    section.bottom_margin = Inches(0.65)
    section.left_margin = Inches(0.7)
    section.right_margin = Inches(0.7)
    normal = document.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(9)
    normal.paragraph_format.space_after = Pt(4)
    document.core_properties.title = f"Daily report {report.get('id')} revision {revision.get('revision_number')}"
    document.core_properties.subject = f"Immutable revision content hash {revision.get('content_hash')}"
    document.core_properties.author = "DrillAI"
    _docx_add_heading(document, _label(locale, "title"), 0, locale)
    p = document.add_paragraph()
    _docx_add_text(p, f"{_label(locale, 'report')}: {report.get('id')} · {_label(locale, 'revision')} {revision.get('revision_number')}", locale)
    _set_docx_rtl(p, locale)
    notice = _warning(locale, _clean_text(revision.get("status")))
    if notice:
        warning = document.add_paragraph()
        _docx_add_text(warning, notice, locale, bold=True, color="9A3412")
        _set_docx_rtl(warning, locale)
    _docx_add_heading(document, _label(locale, "report"), 1, locale)
    _docx_table(document, _metadata_rows(report, revision, manifest, locale), locale)
    _docx_add_heading(document, _label(locale, "summary"), 1, locale)
    _docx_table(document, _narrative_rows(revision, locale), locale)
    for section_key, getter in (
        ("activities", _activity_rows),
        ("measurements", _measurement_rows),
        ("reconciliation", _reconciliation_rows),
    ):
        _docx_add_heading(document, _label(locale, section_key), 1, locale)
        items = revision.get(section_key) or []
        if not items:
            p = document.add_paragraph()
            _docx_add_text(p, _label(locale, "no_entries"), locale)
            _set_docx_rtl(p, locale)
        for index, item in enumerate(items):
            _docx_table(document, getter(item, index, locale), locale)
    note = document.add_paragraph()
    _docx_add_text(note, _label(locale, "export_note"), locale)
    _set_docx_rtl(note, locale)
    footer = section.footer.paragraphs[0]
    _docx_add_text(footer, f"{report.get('id')} · r{revision.get('revision_number')} · SHA-256 {revision.get('content_hash')}", locale)
    _set_docx_rtl(footer, locale)
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def _excel_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise DailyReportExportError("non-finite numeric value cannot be exported")
        return value
    return _clean_text(value)


def _xlsx_sheet(workbook: Workbook, title: str, headers: list[str], rows: list[list[Any]], locale: str) -> None:
    worksheet = workbook.create_sheet(title=title)
    worksheet.sheet_view.rightToLeft = locale == "fa"
    worksheet.freeze_panes = "A2"
    worksheet.sheet_properties.pageSetUpPr.fitToPage = True
    worksheet.page_setup.orientation = "landscape"
    worksheet.page_setup.fitToWidth = 1
    worksheet.page_setup.fitToHeight = 0
    worksheet.sheet_view.showGridLines = False
    worksheet.append(headers)
    for row in rows:
        worksheet.append([_excel_value(value) for value in row])
    header_fill = PatternFill("solid", fgColor="153448")
    thin = Side(style="thin", color="D8E0E4")
    for cell in worksheet[1]:
        cell.font = Font(name="Arial", size=10, bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="right" if locale == "fa" else "left", vertical="top", wrap_text=True)
        cell.border = Border(bottom=thin)
    for row in worksheet.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, str):
                # The cell is explicitly serialized as a string even if user text starts with '='.
                cell.data_type = "s"
            cell.font = Font(name="Arial", size=9)
            cell.alignment = Alignment(horizontal="right" if locale == "fa" else "left", vertical="top", wrap_text=True)
            cell.border = Border(bottom=thin)
    for column in worksheet.columns:
        letter = get_column_letter(column[0].column)
        width = min(54, max(14, max((len(_clean_text(cell.value)) for cell in column), default=0) + 2))
        worksheet.column_dimensions[letter].width = width
    worksheet.auto_filter.ref = worksheet.dimensions if worksheet.max_row > 1 else None


def _render_xlsx(report: dict[str, Any], revision: dict[str, Any], manifest: dict[str, Any], locale: str) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    main_rows = _metadata_rows(report, revision, manifest, locale) + _narrative_rows(revision, locale)
    main_rows.append((_label(locale, "export_note_label"), _clean_text(manifest.get("export_notice"))))
    main_rows.extend(_source_rows({
        "source_document_id": revision.get("source_document_id"),
        "source_record_id": revision.get("source_record_id"),
        "source_region_id": revision.get("source_region_id"),
        "source_page_number": revision.get("source_page_number"),
    }, locale))
    _xlsx_sheet(
        workbook,
        _label(locale, "report")[:31],
        [_label(locale, "field_key"), _label(locale, "source_value")],
        [[label, value] for label, value in main_rows],
        locale,
    )
    activity_headers = [
        "id", "sequence", "activity_kind", "operation_kind", "npt_category", "title", "description",
        "start_at", "start_time_basis", "end_at", "end_time_basis", "duration_source_value",
        "duration_source_unit", "duration_seconds", "duration_basis", "depth_from_source_value",
        "depth_from_source_unit", "depth_to_source_value", "depth_to_source_unit", "depth_from_si",
        "depth_to_si", "depth_reference", "depth_basis", "cause_basis", "operation_id", "event_id",
        "resolution_state", "source_document_id", "source_record_id", "source_region_id", "source_page_number",
    ]
    activities = revision.get("activities") or []
    _xlsx_sheet(
        workbook,
        _label(locale, "activities")[:31],
        [_label(locale, key) for key in activity_headers],
        [[item.get(key) for key in activity_headers] for item in activities],
        locale,
    )
    measurement_headers = [
        "id", "parameter_key", "ordinal", "dimension", "source_value", "source_unit",
        "accepted_input_value", "accepted_input_unit", "value_si", "canonical_unit", "value_basis",
        "observed_at", "time_basis", "depth_source_value", "depth_source_unit", "depth_si", "depth_reference",
        "depth_basis", "resolution_state", "source_document_id", "source_record_id", "source_region_id",
        "source_page_number",
    ]
    measurements = revision.get("measurements") or []
    _xlsx_sheet(
        workbook,
        _label(locale, "measurements")[:31],
        [_label(locale, key) for key in measurement_headers],
        [[item.get(key) for key in measurement_headers] for item in measurements],
        locale,
    )
    reconciliation_headers = [
        "target_key", "target_kind", "field_key", "source_path", "source_review_version",
        "source_fingerprint", "source_value_hash", "proposed_value_hash", "proposal_matches_current",
        "resolution_state", "reason", "decided_by", "decided_at", "source_document_id", "source_record_id",
        "source_region_id", "source_page_number",
    ]
    reconciliation = revision.get("reconciliation") or []
    _xlsx_sheet(
        workbook,
        _label(locale, "reconciliation")[:31],
        [_label(locale, key) for key in reconciliation_headers],
        [[item.get(key) for key in reconciliation_headers] for item in reconciliation],
        locale,
    )
    workbook.properties.title = f"Daily report {report.get('id')} revision {revision.get('revision_number')}"
    workbook.properties.subject = f"Revision content SHA-256 {revision.get('content_hash')}"
    workbook.properties.creator = "DrillAI"
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def render_daily_report_export(
    report: dict[str, Any],
    revision: dict[str, Any],
    *,
    export_format: str,
    locale: str,
    generated_at: dt.datetime | None = None,
) -> bytes:
    """Render a tenant-authorized, fingerprint-verified, immutable report revision."""
    if export_format not in EXPORT_MEDIA_TYPES:
        raise DailyReportExportError(f"unsupported daily-report export format: {export_format}")
    if locale not in _LABELS:
        raise DailyReportExportError(f"unsupported export locale: {locale}")
    if revision.get("status") not in {"submitted", "approved", "rejected", "withdrawn"}:
        raise DailyReportExportError("only immutable, non-draft revisions can be exported")
    if revision.get("content_hash_verified") is not True:
        raise DailyReportExportError("revision content fingerprint is missing or does not match")
    try:
        generated = (generated_at or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
        manifest = {
            "schema_version": "1.0.0",
            "format": export_format,
            "locale": locale,
            "export_notice": _label(locale, "export_note"),
            "generated_at": generated.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "report_id": report.get("id"),
            "revision_id": revision.get("id"),
            "revision_number": revision.get("revision_number"),
            "revision_status": revision.get("status"),
            "revision_content_hash": revision.get("content_hash"),
            "revision_content_hash_verified": revision.get("content_hash_verified"),
        }
        payload = {"export_manifest": manifest, "report": report, "revision": revision}
        if export_format == "json":
            content = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8")
        elif export_format == "pdf":
            content = _render_pdf(report, revision, manifest, locale)
        elif export_format == "docx":
            content = _render_docx(report, revision, manifest, locale)
        else:
            content = _render_xlsx(report, revision, manifest, locale)
    except (TypeError, ValueError, RecursionError) as exc:
        if isinstance(exc, DailyReportExportError):
            raise
        raise DailyReportExportError("daily report contains a value that cannot be represented in this export") from exc
    if len(content) > MAX_EXPORT_BYTES:
        raise DailyReportExportError(
            f"daily-report export exceeds the configured size limit of {MAX_EXPORT_BYTES} bytes"
        )
    return content
