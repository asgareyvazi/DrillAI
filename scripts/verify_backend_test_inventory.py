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
import xml.etree.ElementTree as ET
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


def _junit_test_location(case: ET.Element) -> tuple[str, list[str]] | None:
    """Resolve a test path/class from JUnit fields without trusting arbitrary XML values."""
    raw_location = case.get("file", "").replace("\\", "/")
    if "tests/" in raw_location:
        location = raw_location[raw_location.index("tests/") :]
    else:
        location = raw_location.rsplit("/", 1)[-1]
    valid_location = (
        re.fullmatch(r"tests/[A-Za-z0-9_./-]+\.py", location)
        and ".." not in location.split("/")
    )

    classname = case.get("classname", "")
    class_segments = classname.split(".") if classname else []
    safe_classname = bool(class_segments) and all(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", segment) for segment in class_segments
    )
    if valid_location:
        # Pytest's JUnit family does not always include ``file``. When it does, retain
        # class names only if they follow the module path derived from that trusted file.
        module_name = location.removesuffix(".py").replace("/", ".")
        module_segments = module_name.split(".")
        class_names: list[str] = []
        if safe_classname and class_segments[: len(module_segments)] == module_segments:
            class_names = class_segments[len(module_segments) :]
        elif (
            safe_classname
            and module_segments[:1] == ["tests"]
            and class_segments[: len(module_segments) - 1] == module_segments[1:]
        ):
            class_names = class_segments[len(module_segments) - 1 :]
        elif safe_classname and class_segments[:1] == module_segments[-1:]:
            class_names = class_segments[1:]
        return location, class_names

    # Some pytest/xunit2 versions omit the optional ``file`` attribute. Resolve the
    # longest module prefix against the checked-out tests tree; remaining safe segments
    # are class names. Do not echo the raw JUnit classname into CI annotations.
    if not safe_classname:
        return None
    module_segments = class_segments[1:] if class_segments[0] == "tests" else class_segments
    tests_root = BACKEND / "tests"
    test_files = sorted(tests_root.rglob("test_*.py"))
    for module_end in range(len(module_segments), 0, -1):
        module_prefix = tuple(module_segments[:module_end])
        candidate = tests_root.joinpath(*module_prefix).with_suffix(".py")
        if candidate.is_file():
            return candidate.relative_to(BACKEND).as_posix(), module_segments[module_end:]

        # Some reporters use only the test module basename in `classname`. Accept a
        # suffix match only when it identifies exactly one checked-out test file.
        matching_files = []
        for test_file in test_files:
            file_module = test_file.relative_to(tests_root).with_suffix("").parts
            if len(file_module) >= module_end and file_module[-module_end:] == module_prefix:
                matching_files.append(test_file)
        if len(matching_files) == 1:
            return matching_files[0].relative_to(BACKEND).as_posix(), module_segments[module_end:]
    return None


def _junit_failure_line(failure: ET.Element, location: str) -> str | None:
    """Extract only the numeric source line for the resolved test file, never traceback text."""
    trace = (failure.text or "").replace("\\", "/")
    pattern = re.compile(r"(?m)^\s*.*?(tests/[A-Za-z0-9_./-]+\.py):([1-9][0-9]{0,5})(?::|,|\s|$)")
    lines = [match.group(2) for match in pattern.finditer(trace) if match.group(1) == location]
    return lines[-1] if lines else None


def _junit_failure_summaries(xml_path: Path, *, limit: int = 12) -> list[str]:
    """Extract stable test locations and exception classes, never assertion messages or values."""
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError):
        return []

    summaries: list[str] = []
    seen: set[str] = set()
    for case in root.iter("testcase"):
        failure = next((child for child in case if child.tag in {"failure", "error"}), None)
        if failure is None:
            continue
        resolved = _junit_test_location(case)
        test_name = case.get("name", "").split("[", 1)[0]
        if resolved is None or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", test_name):
            continue
        location, class_names = resolved
        node_id = "::".join([location, *class_names, test_name])
        raw_exception = (failure.get("type") or "").rsplit(".", 1)[-1]
        exception = raw_exception if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", raw_exception) else "failure"
        line_number = _junit_failure_line(failure, location)
        summary = f"{node_id} [{exception}]"
        if line_number:
            summary += f" at line {line_number}"
        if summary not in seen:
            summaries.append(summary)
            seen.add(summary)
        if len(summaries) >= limit:
            break
    return summaries


def failed_node_summaries(
    log_text: str,
    *,
    junit_xml_file: Path | None = None,
    limit: int = 12,
) -> list[str]:
    """Return failed pytest node IDs and exception classes without copying assertion values."""
    plain = ANSI_RE.sub("", log_text)
    summaries: list[str] = []
    seen: set[str] = set()
    for line in plain.splitlines():
        match = re.match(r"^\s*(?:FAILED|ERROR)\s+(\S+)", line)
        if not match:
            continue
        # Pytest parameter IDs can contain arbitrary values; keep only the stable test location.
        node_id = match.group(1).split("[", 1)[0]
        tail = line[match.end() :].strip()
        exception = re.match(r"-\s*([A-Za-z_][A-Za-z0-9_.]*)", tail)
        summary = f"{node_id} [{exception.group(1)}]" if exception else node_id
        if summary not in seen:
            summaries.append(summary)
            seen.add(summary)
        if len(summaries) >= limit:
            return summaries

    if junit_xml_file is not None:
        for summary in _junit_failure_summaries(junit_xml_file, limit=limit):
            if summary not in seen:
                summaries.append(summary)
                seen.add(summary)
            if len(summaries) >= limit:
                break
    return summaries


def verify_platform_result(
    manifest: dict[str, Any],
    platform: str,
    log_text: str,
    pytest_exit_code: int,
    *,
    junit_xml_file: Path | None = None,
) -> str:
    backend = manifest["backend"]
    expected = backend[platform]
    if pytest_exit_code != 0:
        details = [f"pytest exited {pytest_exit_code}; a zero process exit is required"]
        failed_nodes = failed_node_summaries(log_text, junit_xml_file=junit_xml_file)
        details.append(
            "failed node IDs and exception classes: " + ", ".join(failed_nodes)
            if failed_nodes
            else "no FAILED/ERROR node summary was found"
        )
        try:
            summary, _counts = parse_summary(log_text)
        except InventoryError:
            pass
        else:
            details.append("pytest summary: " + summary.split(" in ", 1)[0])
        raise InventoryError("; ".join(details))

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
    parser.add_argument(
        "--junit-xml-file",
        type=Path,
        help="optional pytest JUnit XML used only to expose sanitized failure node IDs",
    )
    args = parser.parse_args()

    try:
        manifest = load_inventory()
        file_count = verify_postgres_inventory(manifest)
        log_text = args.log_file.read_text(encoding="utf-8", errors="replace")
        summary = verify_platform_result(
            manifest,
            args.platform,
            log_text,
            args.pytest_exit_code,
            junit_xml_file=args.junit_xml_file,
        )
    except (InventoryError, OSError, json.JSONDecodeError) as exc:
        print(f"::error::Backend CI test-inventory verification failed: {exc}", file=sys.stderr)
        return 1

    postgres_count = len(manifest["backend"]["postgres"])
    print(f"PostgreSQL inventory verified: {postgres_count} reviewed tests across {file_count} files.")
    print(f"{args.platform} pytest result verified: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
