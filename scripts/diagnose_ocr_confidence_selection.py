#!/usr/bin/env python3
"""Evaluate line-level confidence selection on the pinned synthetic OCR corpus only.

This is an isolated development experiment, not a product OCR path, holdout, or deployed CLI.
It runs the unchanged synthetic generator and scorer using the tesserocr CLI emulator. It selects
whole recognized lines (no token substitution) from the mixed-language pass only when exactly one
line from each pass overlaps by at least 80% of the smaller box and mixed confidence exceeds the
Persian pass by more than 0.05. No fixture label or expected value is used for selection. Confidence
is not a probability of engineering correctness. The output contains aggregate/per-case synthetic
metrics and hashes, not page text or images. Failed/incomplete gates exit nonzero.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

from drillai.core.config import Settings
from drillai.ingestion import ocr
from drillai.ingestion import ocr_synthetic_quality as quality


def overlap_fraction(first: ocr.OcrLine, second: ocr.OcrLine) -> float:
    ax, ay, aw, ah = first.bbox
    bx, by, bw, bh = second.bbox
    area = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))
    return area / min(aw * ah, bw * bh) if min(aw * ah, bw * bh) > 0 else 0.0


def select_lines(combined: list[ocr.OcrLine], selected: list[ocr.OcrLine]) -> list[ocr.OcrLine]:
    result = []
    for line in selected:
        matches = [other for other in combined if overlap_fraction(line, other) >= 0.80]
        reverse = [other for other in selected if len(matches) == 1 and overlap_fraction(other, matches[0]) >= 0.80]
        result.append(matches[0] if len(matches) == len(reverse) == 1 and matches[0].confidence > line.confidence + 0.05 else line)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--language-dir', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('the local emulator launcher requires POSIX')
    models = args.language_dir.resolve(strict=True)
    hashes = {lang: hashlib.sha256((models / f'{lang}.traineddata').read_bytes()).hexdigest() for lang in ('eng', 'fas')}
    if args.output.exists():
        parser.error('output already exists')
    original = ocr.TesseractOcrProvider.recognize
    replacement_count = 0

    def recognize(self: ocr.TesseractOcrProvider, image: bytes) -> list[ocr.OcrLine]:
        nonlocal replacement_count
        previous = self._recognize_acquired
        passes: list[list[ocr.OcrLine]] = []

        def capture(*a, **kw):
            lines = previous(*a, **kw)
            passes.append(lines)
            return lines

        self._recognize_acquired = capture
        try:
            chosen = original(self, image)
        finally:
            self._recognize_acquired = previous
        if len(passes) != 2:
            return chosen
        result = select_lines(passes[0], chosen)
        replacement_count += sum(a is not b for a, b in zip(result, chosen, strict=True))
        return result

    previous_prefix = os.environ.get('TESSDATA_PREFIX')
    with tempfile.TemporaryDirectory(prefix='drillai-ocr-confidence-diagnostic-') as directory:
        shim = Path(directory) / 'shim'
        trace = Path(directory) / 'passes.ndjson'
        emulator = Path(__file__).with_name('diagnose_synthetic_ocr_tesserocr.py').resolve()
        shim.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' + shlex.quote(str(emulator))
                        + ' --shim ' + shlex.quote(str(trace)) + ' "$@"\n', encoding='utf-8')
        shim.chmod(0o700)
        os.environ['TESSDATA_PREFIX'] = str(models)
        ocr.TesseractOcrProvider.recognize = recognize
        try:
            report = quality.run_synthetic_quality_benchmark(
                Settings(ocr_enabled=True, ocr_executable=str(shim), ocr_languages='eng+fas', ocr_dpi=200)
            )
        finally:
            ocr.TesseractOcrProvider.recognize = original
            if previous_prefix is None:
                del os.environ['TESSDATA_PREFIX']
            else:
                os.environ['TESSDATA_PREFIX'] = previous_prefix
    report['diagnostic'] = {
        'classification': 'synthetic development experiment; native-library emulator, not deployed CLI or holdout',
        'method': 'one-to-one 80-percent smaller-box overlap; select whole mixed-script line when confidence exceeds Persian line by more than 0.05',
        'tesserocr_version': version('tesserocr'), 'model_sha256': hashes,
        'selected_mixed_pass_lines': replacement_count,
    }
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(report, output, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        output.write('\n')
    metrics = report.get('metrics') or {}
    print(f"status={report['status']} evaluated={metrics.get('evaluated_cases')} "
          f"critical={metrics.get('critical_fields_matched')}/{metrics.get('critical_fields_expected')} "
          f"mixed_lines={replacement_count}")
    return 0 if report.get('quality_evaluated') and report.get('status') == 'passed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
