#!/usr/bin/env python3
"""Local synthetic CP135 diagnosis using a tesserocr-backed CLI protocol emulator.

The adapter/ingestion path is exercised, but the executable is NOT the deployed Tesseract CLI.
Only the checked-in synthetic corpus is accepted; no input-document argument exists. Install
`tesserocr` separately, and supply local `eng.traineddata` and `fas.traineddata`. The output
contains metrics and pass/PSM identifiers only, never recognized or reference text or images.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shlex
import sys
import tempfile
from collections import Counter
from importlib.metadata import version
from pathlib import Path
from typing import Any


def _shim(trace: Path, args: list[str]) -> int:
    import tesserocr
    from PIL import Image

    if args == ["--version"]:
        print(tesserocr.tesseract_version().splitlines()[0] + "-emulated-cli")
        return 0
    if args == ["--list-langs"]:
        print("List of installed languages (synthetic emulator):\neng\nfas")
        return 0
    if (
        len(args) != 7
        or args[:2] != ["stdin", "stdout"]
        or args[2] != "--psm"
        or args[4] != "-l"
        or args[6] != "tsv"
        or args[3] not in {"6", "11"}
        or args[5] not in {"eng+fas", "fas"}
    ):
        return 2
    with trace.open("a", encoding="utf-8") as output:
        output.write(json.dumps({"languages": args[5], "psm": int(args[3])}) + "\n")
    with (
        Image.open(io.BytesIO(sys.stdin.buffer.read())) as image,
        tesserocr.PyTessBaseAPI(path=os.environ["TESSDATA_PREFIX"], lang=args[5], psm=int(args[3])) as api,
    ):
        api.SetImage(image)
        # GetTSVText provides rows only; the production parser requires the CLI's TSV header.
        sys.stdout.write(
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            + api.GetTSVText(0)
        )
    return 0


def _group_passes(events: list[dict[str, Any]], cases: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Only align complete sequential runs with exactly one first pass per corpus case."""
    groups: list[list[dict[str, Any]]] = []
    for event in events:
        if set(event) != {"languages", "psm"} or event["psm"] not in {6, 11}:
            raise ValueError("invalid synthetic pass event")
        if event["languages"] == "eng+fas":
            groups.append([event])
        elif event["languages"] == "fas" and groups and len(groups[-1]) == 1 and event["psm"] == groups[-1][0]["psm"]:
            groups[-1].append(event)
        else:
            raise ValueError("unexpected synthetic recognition pass sequence")
    if len(groups) != len(cases) or any(case.get("status") != "evaluated" for case in cases):
        raise ValueError("pass sequences cannot be attributed to incomplete synthetic cases")
    return groups


def _attribute_fields(cases: list[dict[str, Any]], records: list[dict[str, Any]]) -> bool:
    """Attribute field-only diagnostics only when they agree with the unchanged aggregate score."""
    if len(records) != len(cases) or not cases:
        return False
    for case, record in zip(cases, records, strict=True):
        matches = record.get("field_matches")
        if (
            case.get("status") != "evaluated"
            or record.get("language") != case.get("language")
            or not isinstance(matches, list)
            or len(matches) != case.get("critical_fields_expected")
            or any(
                not isinstance(field, dict)
                or set(field) != {"key", "matched"}
                or not isinstance(field["key"], str)
                or not isinstance(field["matched"], bool)
                for field in matches
            )
            or sum(field["matched"] for field in matches) != case.get("critical_fields_matched")
        ):
            return False
    return True


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--shim":
        return _shim(Path(sys.argv[2]), sys.argv[3:])

    from drillai.core.config import Settings
    from drillai.ingestion import ocr_synthetic_quality as quality

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("this diagnostic's temporary executable wrapper currently requires POSIX")
    lang_dir = args.language_dir.resolve(strict=True)
    hashes = {name: hashlib.sha256((lang_dir / f"{name}.traineddata").read_bytes()).hexdigest() for name in ("eng", "fas")}
    if args.output.exists():
        parser.error("output already exists; choose a fresh path")

    with tempfile.TemporaryDirectory(prefix="drillai-synthetic-tesserocr-") as temporary:
        directory = Path(temporary)
        trace = directory / "passes.ndjson"
        shim = directory / "tesseract-emulator"
        shim.write_text(
            "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(Path(__file__).resolve()))
            + " --shim " + shlex.quote(str(trace)) + ' "$@"\n', encoding="utf-8",
        )
        shim.chmod(0o700)
        previous = os.environ.get("TESSDATA_PREFIX")
        os.environ["TESSDATA_PREFIX"] = str(lang_dir)
        # This isolated process measures the already-pinned synthetic fixtures only. It calls the
        # existing scorer unchanged, additionally retaining only per-field booleans, never text.
        original_scorer = quality._critical_field_matches
        field_records: list[dict[str, Any]] = []

        def instrument_scorer(actual: str, fields: list[dict[str, str]], *, language: str) -> int:
            total = original_scorer(actual, fields, language=language)
            field_records.append({
                "language": language,
                "field_matches": [
                    {"key": field["key"], "matched": original_scorer(actual, [field], language=language) == 1}
                    for field in fields
                ],
            })
            return total

        quality._critical_field_matches = instrument_scorer
        try:
            report = quality.run_synthetic_quality_benchmark(
                Settings(ocr_enabled=True, ocr_executable=str(shim), ocr_languages="eng+fas", ocr_dpi=200)
            )
        finally:
            quality._critical_field_matches = original_scorer
            if previous is None:
                del os.environ["TESSDATA_PREFIX"]
            else:
                os.environ["TESSDATA_PREFIX"] = previous
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()] if trace.exists() else []

    cases = report.get("cases", [])
    try:
        groups = _group_passes(events, cases)
    except ValueError:
        groups = []
    field_attribution = _attribute_fields(cases, field_records)
    route_counts: Counter[tuple[str, int, int]] = Counter()
    if groups:
        for case, passes in zip(cases, groups, strict=True):
            case["synthetic_passes"] = passes
            route_counts[(case["language"], passes[0]["psm"], len(passes))] += 1
    if field_attribution:
        for case, record in zip(cases, field_records, strict=True):
            case["synthetic_field_matches"] = record["field_matches"]
    report["diagnostic"] = {
        "classification": "synthetic development only; tesserocr CLI protocol emulator, not deployment CLI or holdout",
        "tesserocr_version": version("tesserocr"),
        "model_sha256": hashes,
        "pass_events": len(events),
        "pass_attribution_complete": bool(groups),
        "field_attribution_complete": field_attribution,
        "routes": [
            {"fixture_language": language, "psm": psm, "passes": passes, "cases": count}
            for (language, psm, passes), count in sorted(route_counts.items())
        ],
    }
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        output.write("\n")
    metrics = report.get("metrics") or {}
    print(f"diagnostic status={report['status']} evaluated={metrics.get('evaluated_cases')} passes={len(events)} attributed={bool(groups)} fields={field_attribution}")
    # A completed but failing diagnostic remains a failing process, never a passing quality gate.
    return 0 if groups and field_attribution and report.get("quality_evaluated") and report.get("status") == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
