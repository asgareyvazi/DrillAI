#!/usr/bin/env python3
"""Fail closed when the reviewed pytest inventory or platform result changes.

The expected inventory lives in ci/test-inventory.json and is shared with the E2E gates.
Run from any working directory after installing backend test dependencies.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
INVENTORY_PATH = ROOT / "ci" / "test-inventory.json"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
NODEID_RE = re.compile(r"^tests[\\/].+\.py::[^\s]+$")
SUMMARY_LINE_RE = re.compile(
    r"^\s*\d+\s+(?:passed|failed|errors?|skipped|deselected|xfailed|xpassed)\b.*\bin\s+\S+"
)
STATUS_RE = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)\b")
SKIP_LINE_RE = re.compile(r"^\s*SKIPPED\s+\[(\d+)\]\s+(.+):(\d+):\s*(.*?)\s*$")


class InventoryError(RuntimeError):
    """The live test inventory differs from the reviewed manifest."""


def load_inventory() -> dict[str, Any]:
    data = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    backend = data.get("backend", {})
    postgres = backend.get("postgres", [])
    e2e = data.get("e2e", {})
    projects = e2e.get("projects", {})
    if data.get("version") != 1:
        raise InventoryError("Unsupported ci/test-inventory.json version")
    if not postgres or len(set(postgres)) != len(postgres):
        raise InventoryError("Manifest PostgreSQL node IDs must be nonempty and unique")
    if backend.get("windows", {}).get("deselected") != len(postgres):
        raise InventoryError("Windows deselection count must match the reviewed PostgreSQL node-ID list")
    if sum(projects.values()) != e2e.get("total"):
        raise InventoryError("E2E project totals do not sum to the manifest total")
    total = backend.get("collected")
    for platform in ("default", "linux", "windows"):
        result = backend.get(platform, {})
        if sum(result.get(key, 0) for key in ("passed", "skipped", "deselected")) != total:
            raise InventoryError(f"{platform} backend results do not account for the collected total")
    return data


def collect_postgres_nodeids() -> list[str]:
    """Collect PostgreSQL-marked node IDs without executing any test or opening a database."""
    try:
        import pytest
    except ImportError as exc:  # pragma: no cover - CI installs backend[dev]
        raise InventoryError("pytest is unavailable; install backend test dependencies") from exc

    class Collector:
        nodeids: list[str] = []

        def pytest_collection_finish(self, session: Any) -> None:
            self.nodeids = [item.nodeid.replace("\\", "/") for item in session.items]

    collector = Collector()
    capture_out = io.StringIO()
    capture_err = io.StringIO()
    previous = Path.cwd()
    os.chdir(BACKEND)
    try:
        with contextlib.redirect_stdout(capture_out), contextlib.redirect_stderr(capture_err):
            result = pytest.main(
                ["--collect-only", "-m", "postgres", "-o", "addopts="],
                plugins=[collector],
            )
    finally:
        os.chdir(previous)

    if int(result) != 0:
        details = (capture_out.getvalue() + capture_err.getvalue()).strip()
        raise InventoryError(f"pytest PostgreSQL collection failed (exit={int(result)}):\n{details}")
    return collector.nodeids


def verify_postgres_inventory(manifest: dict[str, Any]) -> int:
    expected = manifest["backend"]["postgres"]
    actual_list = collect_postgres_nodeids()
    actual = set(actual_list)
    expected_set = set(expected)
    duplicates = len(actual_list) - len(actual)
    missing = sorted(expected_set - actual)
    added = sorted(actual - expected_set)
    if duplicates or missing or added:
        details = [
            f"expected {len(expected_set)} reviewed PostgreSQL node IDs; collected {len(actual_list)}",
        ]
        if duplicates:
            details.append(f"duplicate node IDs: {duplicates}")
        if missing:
            details.append("missing: " + ", ".join(missing))
        if added:
            details.append("unreviewed additions: " + ", ".join(added))
        raise InventoryError("PostgreSQL test inventory changed:\n- " + "\n- ".join(details))

    file_count = len({nodeid.split("::", 1)[0] for nodeid in actual})
    expected_files = manifest["backend"]["postgres_files"]
    if file_count != expected_files:
        raise InventoryError(f"Expected PostgreSQL tests across {expected_files} files; collected {file_count}")
    return file_count


def parse_summary(log_text: str) -> tuple[str, dict[str, int]]:
    plain = ANSI_RE.sub("", log_text)
    lines = [line.strip() for line in plain.splitlines() if line.strip()]
    candidates = [line for line in lines if SUMMARY_LINE_RE.match(line)]
    if not candidates:
        raise InventoryError("pytest log has no final summary line with a duration")
    summary = candidates[-1]
    stats = summary.split(" in ", 1)[0]
    counts: dict[str, int] = {}
    for value, status in STATUS_RE.findall(stats):
        key = "errors" if status in {"error", "errors"} else status
        counts[key] = counts.get(key, 0) + int(value)
    if not counts:
        raise InventoryError(f"Could not parse pytest summary: {summary}")
    return summary, counts


def verify_windows_skip(log_text: str, manifest: dict[str, Any]) -> None:
    expected = manifest["backend"]["windows"]
    plain = ANSI_RE.sub("", log_text)
    skip_lines = [line.strip() for line in plain.splitlines() if re.search(r"\bSKIPPED\s+\[", line)]
    if len(skip_lines) != expected["skipped"]:
        raise InventoryError(
            f"Expected exactly {expected['skipped']} reported skip record(s); found {len(skip_lines)}: {skip_lines}"
        )
    if len(skip_lines) != 1:
        raise InventoryError("The Windows policy requires one specifically identified POSIX-only skip")
    match = SKIP_LINE_RE.match(skip_lines[0])
    if not match:
        raise InventoryError(f"Could not parse the single allowed skip record: {skip_lines[0]}")
    count, path, _line_number, reason = match.groups()
    normalized_path = path.replace("\\", "/").removeprefix("./")
    if (
        int(count) != expected["skipped"]
        or normalized_path != expected["expected_skip_path"]
        or reason != expected["expected_skip_reason"]
    ):
        raise InventoryError(
            "The only permitted Windows skip is "
            f"{expected['expected_skip_path']}: {expected['expected_skip_reason']}; got {skip_lines[0]}"
        )


def verify_platform_result(manifest: dict[str, Any], platform: str, log_text: str, pytest_exit_code: int) -> str:
    backend = manifest["backend"]
    expected = backend[platform]
    if pytest_exit_code != 0:
        raise InventoryError(f"pytest exited {pytest_exit_code}; a zero process exit is required")

    summary, counts = parse_summary(log_text)
    expected_counts = {
        "passed": expected["passed"],
        "skipped": expected["skipped"],
        "deselected": expected["deselected"],
    }
    actual_counts = {key: counts.get(key, 0) for key in expected_counts}
    unexpected = {
        key: value
        for key, value in counts.items()
        if key not in expected_counts and key not in {"warning", "warnings"} and value != 0
    }
    if actual_counts != expected_counts or unexpected:
        raise InventoryError(
            f"{platform} pytest totals differ from the reviewed inventory: "
            f"expected={expected_counts}, actual={actual_counts}, other={unexpected}; summary={summary}"
        )

    total = sum(actual_counts.values())
    if total != backend["collected"]:
        raise InventoryError(
            f"{platform} pytest accounting totals {total}, expected {backend['collected']} collected tests"
        )
    if platform == "windows":
        verify_windows_skip(log_text, manifest)
    elif platform == "linux" and actual_counts["skipped"] != 0:
        raise InventoryError("Linux PostgreSQL-enabled suite must not skip any tests")
    elif platform == "default" and actual_counts["skipped"] != len(backend["postgres"]):
        raise InventoryError("SQLite-only run must skip exactly the reviewed PostgreSQL-marked tests")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=("default", "linux", "windows"))
    parser.add_argument("--log-file", required=True, type=Path)
    parser.add_argument("--pytest-exit-code", required=True, type=int)
    args = parser.parse_args()

    try:
        manifest = load_inventory()
        file_count = verify_postgres_inventory(manifest)
        log_text = args.log_file.read_text(encoding="utf-8", errors="replace")
        summary = verify_platform_result(manifest, args.platform, log_text, args.pytest_exit_code)
    except (InventoryError, OSError, json.JSONDecodeError) as exc:
        print(f"::error::Backend CI test-inventory verification failed: {exc}", file=sys.stderr)
        return 1

    postgres_count = len(manifest["backend"]["postgres"])
    print(f"PostgreSQL inventory verified: {postgres_count} reviewed tests across {file_count} files.")
    print(f"{args.platform} pytest result verified: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
