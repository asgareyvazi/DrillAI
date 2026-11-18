#!/usr/bin/env python3
"""Offline exploratory Tesseract.js comparison on the pinned synthetic CP135 corpus only.

Requires local tesseract.js/core and eng/fas traineddata; never installs or downloads them.
This evaluates raw PSM 6 text, NOT the DrillAI OCR adapter or deployment-image gate.
It writes metrics and model hashes, never recognized text, to --output. Not a holdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from importlib.metadata import version
from pathlib import Path

import pypdfium2

from drillai.ingestion.ocr_benchmark import error_rates
from drillai.ingestion.ocr_synthetic_quality import (
    _critical_field_matches,
    _load_corpus,
    _raster_pdf,
    _stratified_metrics,
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--module-prefix', type=Path, required=True)
    parser.add_argument('--language-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prefix = args.module_prefix.resolve(strict=True)
    languages = args.language_dir.resolve(strict=True)
    model_hashes = {lang: sha256(languages / f'{lang}.traineddata.gz') for lang in ('eng', 'fas')}
    package_versions = {
        name: json.loads((prefix / 'node_modules' / name / 'package.json').read_text())['version']
        for name in ('tesseract.js', 'tesseract.js-core')
    }
    corpus, corpus_bytes = _load_corpus()
    rows = []
    with tempfile.TemporaryDirectory(prefix='drillai-synthetic-ocr-compare-') as tmp:
        page_dir = Path(tmp)
        for case in corpus['cases']:
            pdf = pypdfium2.PdfDocument(_raster_pdf(case))
            page = pdf[0]
            try:
                page.render(scale=200 / 72).to_pil().save(page_dir / f"{case['case_id']}.png")
            finally:
                page.close()
                pdf.close()
        result = subprocess.run(
            ['node', str(Path(__file__).with_name('compare_ocr_tesseractjs.cjs')), str(prefix), str(languages), str(page_dir)],
            check=True, capture_output=True, text=True, timeout=600,
        )
        transcripts = [json.loads(line) for line in result.stdout.splitlines()]
        by_id = {item['id']: item['text'] for item in transcripts}
        expected = {case['case_id'] for case in corpus['cases']}
        if len(transcripts) != len(expected) or set(by_id) != expected:
            raise ValueError('comparison must return exactly one result per pinned synthetic case')
        for case in corpus['cases']:
            actual = by_id[case['case_id']]
            rows.append({
                'id': case['case_id'], 'status': 'evaluated',
                **error_rates(case['ground_truth'], actual),
                'critical_fields_expected': len(case['critical_fields']),
                'critical_fields_matched': _critical_field_matches(actual, case['critical_fields'], language=case['language']),
            })
    keys = ('character_errors', 'expected_characters', 'word_errors', 'expected_words', 'critical_fields_matched', 'critical_fields_expected')
    totals = {key: sum(row[key] for row in rows) for key in keys}
    report = {
        'classification': 'synthetic exploratory comparison; not deployment, product adapter, or holdout',
        'dataset_sha256': hashlib.sha256(corpus_bytes).hexdigest(),
        'package_versions': package_versions,
        'model_gzip_sha256': model_hashes,
        'pdfium_version': version('pypdfium2'),
        'dpi': 200, 'language_configuration': 'eng+fas', 'page_segmentation_mode': 6,
        'evaluated_cases': len(rows), 'totals': totals,
        'cer': totals['character_errors'] / totals['expected_characters'],
        'wer': totals['word_errors'] / totals['expected_words'],
        'critical_exact': totals['critical_fields_matched'] / totals['critical_fields_expected'],
        'strata': _stratified_metrics(corpus['cases'], rows), 'cases': rows,
    }
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(report, output, indent=2, ensure_ascii=False)
        output.write('\n')
    print(f"exploratory synthetic results: {len(rows)} evaluated; CER={report['cer']:.6f}; WER={report['wer']:.6f}; critical={report['critical_exact']:.6f}")


if __name__ == '__main__':
    main()
