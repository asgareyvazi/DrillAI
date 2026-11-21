#!/usr/bin/env python3
"""Generate/check the roadmap registry table from runtime source, not a hand-edited list.

The generated values are declarations, not validation. Test SHA is deliberately not inferred.
"""
from __future__ import annotations

import argparse
import hashlib
import inspect
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROADMAP = ROOT / 'docs/DRILLAI_MASTER_PRODUCT_ROADMAP.md'
BEGIN = '<!-- BEGIN GENERATED ENGINE REGISTRY -->'
END = '<!-- END GENERATED ENGINE REGISTRY -->'


def source_sha256(source: bytes) -> str:
    """Hash the source with checkout line endings normalized to LF, on Linux and Windows."""
    return hashlib.sha256(source.replace(b"\r\n", b"\n")).hexdigest()


def table() -> str:
    from drillai.engines.registry import load_default_engines

    lines = [BEGIN, '| Key | Version | Registry declaration | Source | Source SHA-256 (LF normalized) |',
             '|---|---|---|---|---|']
    for engine in sorted(load_default_engines().all(), key=lambda item: item.spec.key):
        source = Path(inspect.getsourcefile(engine._fn)).resolve()
        path = source.relative_to(ROOT).as_posix()
        digest = source_sha256(source.read_bytes())
        spec = engine.spec
        lines.append(f'| `{spec.key}` | {spec.version} | {spec.validation_status} | `{path}` | `{digest}` |')
    return '\n'.join([*lines, END])


def replace_table(document: str) -> str:
    if document.count(BEGIN) != 1 or document.count(END) != 1 or document.index(BEGIN) >= document.index(END):
        raise ValueError('roadmap must contain exactly one correctly ordered generated registry block')
    before, remainder = document.split(BEGIN)
    _, after = remainder.split(END)
    return before + table() + after


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='update only the marked roadmap table')
    args = parser.parse_args()
    current = ROADMAP.read_text(encoding='utf-8')
    expected = replace_table(current)
    if args.write:
        ROADMAP.write_text(expected, encoding='utf-8')
    elif current != expected:
        print('Engine registry roadmap drift: run python scripts/verify_engine_roadmap.py --write')
        return 1
    print('Engine roadmap source/version/status/hash declarations match the runtime registry.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
