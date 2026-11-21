#!/usr/bin/env python3
"""Snapshot runtime engine self-declarations at a clean tracked Git revision.

This is an inventory, not verification of calculations, units, bibliographic references or field use.
Run with the backend installed, and write a new output file; never overwrite an existing snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import platform
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; use a fresh filename")
    for command in (["git", "diff", "--quiet"], ["git", "diff", "--cached", "--quiet"]):
        if subprocess.run(command, cwd=ROOT, check=False).returncode:
            parser.error("tracked worktree and index must match the named revision")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()

    from drillai.engines.registry import load_default_engines

    engines = []
    for engine in load_default_engines().all():
        function = getattr(engine, "_fn", None)
        if function is None:
            raise RuntimeError(f"cannot identify the implementation function for {engine.spec.key}")
        source_file = inspect.getsourcefile(function)
        if source_file is None:
            raise RuntimeError(f"cannot identify the implementation source for {engine.spec.key}")
        source_path = Path(source_file).resolve()
        source = source_path.relative_to(ROOT).as_posix()
        engines.append({
            "key": engine.spec.key,
            "source": source,
            "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "function": function.__qualname__,
            "declared_spec": engine.spec.describe(),
        })
    snapshot = {
        "snapshot_revision": revision,
        "classification": "runtime self-declarations only; references and calculations not independently verified",
        "python_version": platform.python_version(),
        "engines": engines,
    }
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(snapshot, output, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        output.write("\n")
    print(f"revision={revision} engines={len(engines)} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
