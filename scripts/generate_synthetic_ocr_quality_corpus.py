#!/usr/bin/env python3
"""Regenerate the deterministic CP135 bilingual synthetic OCR corpus."""

from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = (
    ROOT
    / "backend"
    / "src"
    / "drillai"
    / "ingestion"
    / "data"
    / "synthetic_ddr_ocr_quality_v2.json"
)
LAYOUTS = ("single_column", "table", "two_column", "compact")
SCAN_CONDITIONS = ("clean", "low_contrast", "light_skew", "light_noise")
PERSIAN_LAYOUTS = ("single_column", "table", "compact", "single_column")
ENGLISH_LABELS = {
    "sample_id": "SAMPLE ID",
    "report_date": "REPORT DATE",
    "well": "WELL",
    "section": "SECTION",
    "measured_depth_m": "MEASURED DEPTH",
    "rop_m_per_h": "RATE OF PENETRATION",
    "mud_weight_ppg": "MUD WEIGHT",
    "npt_h": "NONPRODUCTIVE TIME",
    "activity": "ACTIVITY",
}
PERSIAN_LABELS = {
    "sample_id": "شناسه نمونه",
    "report_date": "تاریخ گزارش",
    "well": "چاه",
    "section": "بخش",
    "measured_depth_m": "عمق اندازه گیری",
    "rop_m_per_h": "سرعت حفاری",
    "mud_weight_ppg": "وزن گل",
    "npt_h": "زمان غیرمولد",
    "activity": "فعالیت",
}
ENGLISH_ACTIVITIES = (
    "DRILLING AHEAD",
    "SLIDE DRILLING",
    "CONNECTION",
    "CIRCULATING AND CONDITIONING",
    "TRIPPING OUT",
    "RUNNING CASING",
    "CEMENTING",
    "WELL CONTROL CHECK",
)
PERSIAN_ACTIVITIES = (
    "حفاری به جلو",
    "حفاری لغزشی",
    "اتصال لوله",
    "گردش گل و آماده سازی",
    "خروج لوله ها",
    "لوله گذاری",
    "سیمان کاری",
    "بررسی کنترل چاه",
)


def _case(index: int) -> dict[str, Any]:
    language = "eng" if index < 48 else "fas"
    language_index = index if language == "eng" else index - 48
    layout = LAYOUTS[index % len(LAYOUTS)] if language == "eng" else PERSIAN_LAYOUTS[language_index % 4]
    scan_condition = SCAN_CONDITIONS[index % len(SCAN_CONDITIONS)]
    labels = ENGLISH_LABELS if language == "eng" else PERSIAN_LABELS
    activity = (
        ENGLISH_ACTIVITIES[index % len(ENGLISH_ACTIVITIES)]
        if language == "eng"
        else PERSIAN_ACTIVITIES[language_index % len(PERSIAN_ACTIVITIES)]
    )
    case_id = f"CP135-DDR-{index + 1:03d}"
    report_date = (dt.date(2026, 6, 1) + dt.timedelta(days=index)).isoformat()
    well = f"CP135-SYNTHETIC-{1 + (index % 2):02d}"
    section = ("12 1/4", "8 1/2", "6 1/8", "17 1/2")[index % 4]
    depth = f"{900.0 + ((index * 17) % 500) + (index % 2) * 0.5:.1f}"
    rop = f"{7.25 + ((index * 9) % 30) * 0.75:.2f}"
    mud_weight = f"{9.8 + (index % 15) * 0.2:.1f}"
    npt = f"{(index % 12) * 0.25:.2f}"

    if language == "eng":
        section_value = f"{section} INCH"
        title = "SYNTHETIC DAILY DRILLING REPORT"
        depth_value = f"{depth} M"
        rop_value = f"{rop} M/H"
        mud_value = f"{mud_weight} PPG"
        npt_value = f"{npt} H"
    else:
        section_value = f"{section} اینچ"
        title = "گزارش روزانه حفاری مصنوعی"
        depth_value = f"{depth} M"
        rop_value = f"{rop} M/H"
        mud_value = f"{mud_weight} PPG"
        npt_value = f"{npt} H"

    fields = [
        ("sample_id", case_id),
        ("report_date", report_date),
        ("well", well),
        ("section", section_value),
        ("measured_depth_m", depth_value),
        ("rop_m_per_h", rop_value),
        ("mud_weight_ppg", mud_value),
        ("npt_h", npt_value),
        ("activity", activity),
    ]
    ground_truth = "\n".join([title, *(f"{labels[key]}: {value}" for key, value in fields)])
    critical = [
        {"key": key, "expected": value.split(maxsplit=1)[0] if key in {
            "measured_depth_m", "rop_m_per_h", "mud_weight_ppg", "npt_h"
        } else value}
        for key, value in fields
        if key in {"sample_id", "report_date", "measured_depth_m", "rop_m_per_h", "mud_weight_ppg", "npt_h"}
    ]
    return {
        "case_id": f"cp135-ddr-{index + 1:03d}",
        "language": language,
        "layout": layout,
        "scan_condition": scan_condition,
        "document_class": "daily_drilling_report",
        "ground_truth": ground_truth,
        "critical_fields": critical,
    }


def build_corpus() -> dict[str, Any]:
    cases = [_case(index) for index in range(64)]
    return {
        "corpus_id": "cp135-synthetic-ddr-ocr",
        "version": "2.0.0",
        "classification": "synthetic",
        "languages": ["eng", "fas"],
        "stratification_dimensions": ["language", "layout", "scan_condition"],
        "sampling_method": (
            "64 deterministic one-page image-only machine-generated DDR fixtures: 48 English and 16 Persian; "
            "four page layouts and four controlled scan conditions, crossed by a fixed schedule. "
            "No field or vendor documents are sampled."
        ),
        "ground_truth_protocol": (
            "Generated from the same explicit synthetic values used to render each page; not independently "
            "adjudicated and not a holdout. Persian labels are Unicode source text, shaped only for raster display."
        ),
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(build_corpus(), ensure_ascii=False, indent=2) + "\n"
    args.output.write_text(payload, encoding="utf-8", newline="\n")
    print(f"generated {len(build_corpus()['cases'])} synthetic cases at {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
