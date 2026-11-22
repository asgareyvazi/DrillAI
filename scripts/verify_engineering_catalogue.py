#!/usr/bin/env python3
"""Generate/check a bounded bilingual engineering inventory, not an independent assurance audit."""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = ROOT / 'docs/ENGINEERING_CAPABILITY_CATALOGUE.json'
EN_PATH = ROOT / 'docs/ENGINEERING_CAPABILITY_CATALOGUE.en.md'
FA_PATH = ROOT / 'docs/ENGINEERING_CAPABILITY_CATALOGUE.fa.md'
TESTS = {
    'drilling.mse': 'backend/tests/engines/test_mechanics.py',
    'hydraulics.laminar': 'backend/tests/engines/test_fluids.py',
    'integrity.barrier_envelope': 'backend/tests/engines/test_safety_engines.py',
    'offsets.similarity': 'backend/tests/engines/test_planning_engines.py',
    'optimization.pareto': 'backend/tests/engines/test_twin_optimization_engines.py',
    'optimization.sweep': 'backend/tests/engines/test_twin_optimization_engines.py',
    'readiness.assessment': 'backend/tests/engines/test_planning_engines.py',
    'schematic.well_model': 'backend/tests/engines/test_planning_engines.py',
    'torque_drag.soft_string': 'backend/tests/engines/test_mechanics.py',
    'trajectory.geometric_proximity': 'backend/tests/engines/test_proximity.py',
    'trajectory.minimum_curvature': 'backend/tests/engines/test_trajectory.py',
    'tubulars.api_5c3': 'backend/tests/engines/test_safety_engines.py',
    'twin.reconciliation': 'backend/tests/engines/test_twin_optimization_engines.py',
    'well_control.kill_sheet': 'backend/tests/engines/test_safety_engines.py',
    'wellbore.capacity': 'backend/tests/engines/test_fluids.py',
}
PLANNED = ['turbulent hydraulics', 'Power Law rheology', 'Herschel-Bulkley rheology', 'hole cleaning',
           'cuttings transport', 'uncertainty-based anti-collision', 'advanced torque and drag', 'surge/swab',
           'casing design', 'tubular connection capacity', 'cementing', 'wellbore stability', 'kick tolerance',
           'MAASP', 'managed pressure drilling', 'drillstring design', 'casing-seat planning', 'pressure management',
           'well integrity', 'well economics', 'lifecycle performance', 'advanced optimization']
HISTORY = [
    {'revision': 'bfa066b', 'event': 'Initial repository commit; not current product capability evidence.'},
    {'revision': '09bef03', 'event': 'Early engine pack, including trajectory and fluid calculations.'},
    {'revision': '80aee9f', 'event': 'CP19 analytical software vectors; not field validation.'},
    {'revision': '658c4f0', 'event': 'Survey datum and station-time provenance.'},
    {'revision': 'd7526a1', 'event': 'Governed engineering products and OCR evidence.'},
    {'revision': '11e6cd2', 'event': 'OCR TSV quote and synthetic renderer correction; OCR acceptance remained open.'},
    {'revision': '85396bc', 'event': 'Hydraulics bracketing and bottoms-up geometry safeguards.'},
    {'revision': 'e212fa1', 'event': 'Capacity full-path validation and explicit TVD; incomplete design workflows.'},
    {'revision': '0735fc1', 'event': 'Evidence pagination test setup matches API ordering.'},
]


def build() -> tuple[str, str, str]:
    from drillai.engines.registry import load_default_engines

    engines = []
    for engine in sorted(load_default_engines().all(), key=lambda item: item.spec.key):
        spec = engine.spec.describe()
        source = Path(inspect.getsourcefile(engine._fn)).resolve()
        path = source.relative_to(ROOT).as_posix()
        sha = hashlib.sha256(source.read_bytes().replace(b'\r\n', b'\n')).hexdigest()
        key = spec['key']
        if key not in TESTS or not (ROOT / TESTS[key]).is_file():
            raise ValueError(f'missing reviewed primary test association for {key}')
        engines.append({
            'id': key, 'version': spec['version'], 'source': path, 'source_function': engine._fn.__name__,
            'source_sha256_lf': sha, 'status': 'NEEDS_FIELD_VALIDATION' if key in {'well_control.kill_sheet', 'tubulars.api_5c3'} else 'IMPLEMENTED_NOT_INDEPENDENTLY_VALIDATED',
            'registry_self_declaration': spec['validation_status'], 'summary': spec['summary'],
            'api': '/api/v1/registry/engines/{engine_key}/run', 'persistence': 'backend/src/drillai/db/models/engines.py:EngineRun',
            'ui': 'frontend/src/pages/engineering/CirculationWorkspace.tsx (partial)' if key in {'wellbore.capacity', 'hydraulics.laminar'} else 'frontend/src/pages/engineering/EngineeringWorkspace.tsx (generic runner)',
            'primary_tests': [TESTS[key]], 'input_schema': spec['input_schema'], 'output_schema': spec['output_schema'],
            'units': spec['unit_contract'], 'assumptions': spec['assumptions'], 'limitations': spec['limitations'],
            'references_as_declared': spec['references'], 'dependencies': spec['consumes'], 'produces': spec['produces'],
            'latest_exact_sha_assurance': 'NOT_MEASURED_FOR_CURRENT_TREE',
            'field_vendor_customer_validation': 'NOT_MEASURED',
        })
    data = {'schema': 'drillai.capability-catalogue.v1', 'scope': 'initial source-linked inventory; independent per-engine audit, full history and product catalogue remain incomplete',
            'history_selected_events': HISTORY, 'engines': engines,
            'product_workflows': [
                {'id': 'wellbore.capacity.workspace', 'status': 'PARTIAL', 'ui': 'frontend/src/pages/engineering/CirculationWorkspace.tsx', 'missing': ['design revisions', 'scoped declarations UI', 'review', 'comparison', 'report', 'browser journey']},
                {'id': 'hydraulics.ecd.workspace', 'status': 'PARTIAL', 'ui': 'frontend/src/pages/engineering/CirculationWorkspace.tsx', 'missing': ['verified survey linkage', 'profile by TVD', 'scenario comparison', 'review', 'report', 'browser journey']},
                {'id': 'anti_collision', 'status': 'PARTIAL', 'missing': ['uncertainty model', 'survey database integration', 'dedicated UI', 'review', 'report']},
                {'id': 'ocr.persian', 'status': 'PARTIAL', 'missing': ['passing deployed CP135 gate', 'independent holdout']},
            ],
            'future': [{'id': item.replace(' ', '_'), 'name': item, 'status': 'PLANNED', 'required_reference': 'NOT_SELECTED', 'validation': 'NOT_MEASURED'} for item in PLANNED],
            'notes': ['Status is deliberately conservative and does not imply independent, field, vendor or customer validation.',
                      'Registry metadata is self-declared. The source-linked historical examples are not a complete history audit.',
                      'Some planned topics have existing partial implementations; planned entries identify missing advanced scope, not an assertion that all related code is absent.']}
    json_text = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + '\n'
    rows = [f"| `{e['id']}` | {e['version']} | `{e['status']}` | `{e['source']}` |" for e in engines]
    en = '\n'.join(['# Engineering capability inventory — preliminary', '',
        'This source-linked inventory is not the requested comprehensive historical/product catalogue or an independent engineering audit. No field, vendor or customer acceptance is implied.', '',
        '| Engine | Version | Status | Source |', '|---|---|---|---|', *rows, '',
        '## Historical milestones (selected, not exhaustive)', *[f"- `{h['revision']}` — {h['event']}" for h in HISTORY], '',
        '## Product workflow status', '- Capacity/circulation: PARTIAL — structured input and persisted runs; no approved revisions, comparisons or reports.',
        '- Hydraulics/ECD: PARTIAL — structured input and declared references; no verified survey linkage or ECD depth profile.',
        '- Anti-collision: PARTIAL — `GEOMETRIC_PROXIMITY_ONLY`; no uncertainty model, integrated survey selection or dedicated workflow.',
        '- Persian OCR: NOT ACCEPTED on the unchanged deployed gate; independent holdout absent.', '',
        '## Future / incomplete advanced scopes', ', '.join(PLANNED) + '.', '',
        'See the JSON catalogue for input/output schemas, units, assumptions, limitations and self-declared references. The source SHA normalizes CRLF to LF.', ''])
    fa = '\n'.join(['# فهرست اولیه قابلیت‌های مهندسی', '',
        'این فهرست متصل به منبع، ممیزی مستقل یا فهرست جامع تاریخی و محصول نیست. اعتبارسنجی میدانی، پذیرش مشتری و تأیید سازنده اثبات نشده‌اند.', '',
        '| موتور | نسخه | وضعیت | منبع |', '|---|---|---|---|', *rows, '',
        '## نقاط تاریخی منتخب', '- `bfa066b`: آغاز مخزن؛ نشانه توانایی فعلی نیست.', '- `09bef03`: بسته اولیه محاسبات مسیر و سیالات.',
        '- `80aee9f`: آزمون‌های تحلیلی نرم‌افزاری؛ نه اعتبارسنجی میدانی.', '- `658c4f0`: منشأ مبنا و زمان برداشت مسیر.',
        '- `d7526a1`: محصولات مهندسی دارای حاکمیت و شواهد OCR.', '- `11e6cd2`: اصلاح OCR بدون احراز معیار پذیرش.',
        '- `85396bc` و `e212fa1`: محافظت عددی و هندسی ظرفیت و هیدرولیک.', '- `0735fc1`: ترتیب قطعی داده آزمون شواهد.', '',
        '## وضعیت گردش‌کار', '- ظرفیت و گردش: ناقص؛ ورود ساخت‌یافته و ذخیره اجرا وجود دارد، نسخه تأییدشده و گزارش کامل وجود ندارد.',
        '- هیدرولیک و ECD: ناقص؛ اتصال معتبر برداشت، مقایسه و گزارش کامل وجود ندارد.',
        '- ضدبرخورد: فقط `GEOMETRIC_PROXIMITY_ONLY`؛ بدون عدم‌قطعیت و ضریب جدایش.',
        '- OCR فارسی: پذیرفته نشده؛ آزمون مستقل holdout موجود نیست.', '',
        '## حوزه‌های آینده', 'هیدرولیک آشفته، رئولوژی پیشرفته، پاک‌سازی چاه، عدم‌قطعیت ضدبرخورد، گشتاور و درگ، surge/swab، جداری، سیمان‌کاری، پایداری چاه، کنترل فشار، MPD، اقتصاد چاه و بهینه‌سازی پیشرفته.', '',
        'جزئیات ورودی، خروجی، واحد، فرض‌ها و منابع ادعاشده در JSON آمده است. هش منبع با نرمال‌سازی CRLF به LF محاسبه می‌شود.', ''])
    return json_text, en, fa


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    for path, value in zip((JSON_PATH, EN_PATH, FA_PATH), build(), strict=True):
        if args.write:
            path.write_text(value, encoding='utf-8', newline='\n')
        elif not path.exists() or path.read_text(encoding='utf-8') != value:
            print(f'Catalogue drift: {path.relative_to(ROOT)}')
            return 1
    print('Bilingual preliminary catalogue matches runtime declarations and source hashes; this is not engineering validation.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
