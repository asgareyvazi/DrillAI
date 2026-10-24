"""CP32 end-to-end console-error contract.

The Windows certification job failed on six journeys in ``frontend/e2e/workflow-studio.spec.ts``
while the Linux job passed the same commit, and the failing assertion was the same in all six:
``expect(consoleErrors).toEqual([])``.

That assertion asks the *browser* whether anything went wrong, and the browser answers with its own
line for every resource that failed to load. The line is not written the same way everywhere: on the
Windows runner a request the application cancelled — the application shell cancels its background
reads whenever the reader navigates — arrived as a dropped connection and was logged, while on Linux
the same cancellation was logged as ``net::ERR_ABORTED`` and was not. The product behaved
identically on both; the *oracle* did not.

The suite already had the right definition. ``appConsoleErrors`` in ``frontend/e2e/fixtures.ts``
separates the application's own errors — a React error, an unhandled rejection, a ``console.error``
from the product — from the browser's network log, and seven journeys used it. The studio journeys
were the ones that did not, so they were the ones a platform could fail.

This module makes the class of defect impossible to ship again:

1. **No journey may use the browser's network log as its oracle.** A spec asserts on console errors
   only through ``appConsoleErrors`` or through the shared ``expectNoFailures`` contract.
2. **The product's own behaviour is asserted on something platform-independent.** The shared fixture
   records every response the page received with a failing status, and ``expectNoFailures`` requires
   that no call the journey did not provoke failed. The response is the same on every platform.
3. **The journeys are compiled before they run.** The E2E specs are type-checked by
   ``frontend/tsconfig.e2e.json``, which the root ``frontend/tsconfig.json`` references. Until this
   contract, a spec could not even name an import that did not exist, and the mistake was found by a
   CI job on a platform nobody had looked at.
4. **A failing journey says what it saw.** Both CI jobs copy the Playwright failure messages into
   the step annotation, so a red run names the assertion that failed rather than only a count.

The checks are static: they read the files the suite and the workflow actually use, so they fail
before the commit reaches CI rather than after it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = REPO_ROOT / "frontend" / "e2e"
FIXTURES_PATH = E2E_DIR / "fixtures.ts"
FRONTEND_TSCONFIG = REPO_ROOT / "frontend" / "tsconfig.json"
FRONTEND_TSCONFIG_E2E = REPO_ROOT / "frontend" / "tsconfig.e2e.json"
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# The browser's own line for a resource that failed to load. It is the noise the contract removes.
RESOURCE_LINE = "Failed to load resource"

# The shared contract a journey uses instead of the browser's network log.
SHARED_CONTRACT = "expectNoFailures"

# A journey that reads the raw console-error list and judges it itself. Every one of these is a
# platform-dependent verdict, which is what this contract forbids.
RAW_ORACLES = (
    re.compile(r"expect\(\s*consoleErrors\b"),
    re.compile(r"consoleErrors\.filter\("),
    re.compile(r"for\s*\(\s*const\s+\w+\s+of\s+consoleErrors\b"),
)


def _spec_files() -> list[Path]:
    return sorted(E2E_DIR.glob("*.spec.ts"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _load_jsonc(path: Path) -> dict:
    """Read a TypeScript configuration, which is JSON with comments."""
    source = _read(path)
    without_block_comments = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    without_line_comments = re.sub(r"^\s*//.*$", "", without_block_comments, flags=re.M)
    return json.loads(without_line_comments)


# ---------------------------------------------------------------------------------------------
# 1. The browser's network log is not an oracle
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("spec", _spec_files(), ids=lambda p: p.name)
def test_cp32_no_journey_judges_the_raw_console_error_list(spec: Path) -> None:
    """A journey may not decide "nothing went wrong" by reading the browser's network log."""
    source = _read(spec)
    offenders = [
        pattern.pattern
        for pattern in RAW_ORACLES
        if pattern.search(source)
    ]
    assert not offenders, (
        f"{spec.name} judges the raw console-error list ({offenders}). The browser writes that line "
        "differently on different platforms — a cancelled request is `net::ERR_ABORTED` on one "
        "runner and a dropped connection on the next — so the verdict would be about the runner "
        f"rather than about the product. Use `{SHARED_CONTRACT}` from './fixtures' instead."
    )


@pytest.mark.parametrize("spec", _spec_files(), ids=lambda p: p.name)
def test_cp32_every_journey_uses_the_shared_contract(spec: Path) -> None:
    """A journey that collects console errors must assert them through the shared contract."""
    source = _read(spec)
    if "consoleErrors" not in source:
        pytest.skip(f"{spec.name} does not collect console errors")
    if "const consoleErrors: string[] = []" in source:
        # The checkpoint journey collects the list itself rather than through the shared fixture.
        assert SHARED_CONTRACT in source or "appConsoleErrors" in source, (
            f"{spec.name} collects console errors but never asserts them through the shared "
            "definition of an application error."
        )
        return
    assert SHARED_CONTRACT in source or "appConsoleErrors" in source, (
        f"{spec.name} collects console errors but never asserts them through `appConsoleErrors` or "
        f"`{SHARED_CONTRACT}`."
    )


@pytest.mark.parametrize("spec", _spec_files(), ids=lambda p: p.name)
def test_cp32_a_collected_response_list_is_asserted(spec: Path) -> None:
    """Collecting the failing responses and never asserting them would be decoration."""
    source = _read(spec)
    if "failedResponses" not in source:
        return
    if "expectNoFailures" in source:
        return
    # The fixture file itself may mention the type without asserting on it.
    assert "expectNoFailures(consoleErrors, failedResponses" in source, (
        f"{spec.name} collects the failing responses but never asserts on them."
    )


# ---------------------------------------------------------------------------------------------
# 2. The shared fixture provides both oracles
# ---------------------------------------------------------------------------------------------


def test_cp32_the_fixture_separates_application_errors_from_the_network_log() -> None:
    """The repository's definition of an application error must keep excluding the network log."""
    source = _read(FIXTURES_PATH)
    assert "export function appConsoleErrors" in source, (
        "frontend/e2e/fixtures.ts must keep exporting `appConsoleErrors`: it is the definition of "
        "an application console error the whole suite shares."
    )
    match = re.search(r"export function appConsoleErrors\(errors: string\[\]\): string\[\] \{(.*?)\n\}", source, re.S)
    assert match, "could not find the body of `appConsoleErrors`"
    body = match.group(1)
    assert RESOURCE_LINE in body, (
        "`appConsoleErrors` must exclude the browser's own line for a resource that failed to load; "
        "that line is the platform-dependent noise this contract removes."
    )


def test_cp32_the_fixture_records_the_failing_responses() -> None:
    """The platform-independent oracle needs the responses themselves, with their status."""
    source = _read(FIXTURES_PATH)
    assert "export interface FailedResponse" in source, (
        "frontend/e2e/fixtures.ts must export the `FailedResponse` type: a journey asserts on the "
        "response the page received, which is the same on every platform."
    )
    for member in ("status", "method", "url"):
        assert re.search(rf"\b{member}\b", source), f"`FailedResponse` must carry the {member}"
    assert "page.on('response'" in source, (
        "the shared fixture must record the responses the page received with a failing status"
    )
    assert "response.status() >= 400" in source, (
        "the shared fixture must record every response with a failing status"
    )


def test_cp32_the_shared_contract_exists_and_is_used() -> None:
    """The contract itself must exist, and must assert on both oracles."""
    source = _read(FIXTURES_PATH)
    assert f"export function {SHARED_CONTRACT}" in source, (
        f"frontend/e2e/fixtures.ts must export `{SHARED_CONTRACT}`"
    )
    match = re.search(
        r"export function expectNoFailures\((.*?)\n\): void \{(.*?)\n\}", source, re.S
    )
    assert match, "could not find the body of `expectNoFailures`"
    body = match.group(2)
    assert "appConsoleErrors(consoleErrors)" in body, (
        f"`{SHARED_CONTRACT}` must assert that the application produced no error of its own"
    )
    assert "failedResponses" in body, (
        f"`{SHARED_CONTRACT}` must assert that no call the journey did not provoke failed"
    )
    assert "status >= 500" in body, (
        f"`{SHARED_CONTRACT}` must assert that the product never answered with a server error"
    )


# ---------------------------------------------------------------------------------------------
# 3. The journeys are compiled before they run
# ---------------------------------------------------------------------------------------------


def test_cp32_the_e2e_specs_are_type_checked() -> None:
    """A spec that cannot be compiled is a spec whose mistake is found by CI, on a runner."""
    assert FRONTEND_TSCONFIG_E2E.exists(), (
        "frontend/tsconfig.e2e.json must exist: the E2E specs run in CI on two platforms and must "
        "be compiled before they run."
    )
    project = _load_jsonc(FRONTEND_TSCONFIG_E2E)
    assert "e2e" in project.get("include", []), (
        "frontend/tsconfig.e2e.json must include the `e2e` directory"
    )
    assert project["compilerOptions"].get("strict") is True, (
        "the E2E project must be type-checked in strict mode"
    )

    root = _load_jsonc(FRONTEND_TSCONFIG)
    references = [entry["path"] for entry in root.get("references", [])]
    assert "./tsconfig.e2e.json" in references, (
        "frontend/tsconfig.json must reference the E2E project, or `npm run typecheck` — which CI "
        "runs on both platforms — will not compile the journeys."
    )


# ---------------------------------------------------------------------------------------------
# 4. A failing journey says what it saw
# ---------------------------------------------------------------------------------------------


def test_cp32_ci_reports_the_playwright_failure_message() -> None:
    """A red job must name the assertion that failed, not only a count of failures."""
    workflow = yaml.safe_load(_read(WORKFLOW_PATH))
    jobs = workflow["jobs"]
    assert "certification" in jobs and "windows-certification" in jobs

    linux_step = _step_text(jobs["certification"], "End-to-end suite (main, faults and auth stacks)")
    windows_step = _step_text(jobs["windows-certification"], "Playwright E2E certification")

    for name, step in (("linux", linux_step), ("windows", windows_step)):
        assert "Error" in step and "Received" in step, (
            f"the {name} end-to-end step must copy the Playwright failure messages into its "
            "annotation, so a red run says which assertion failed instead of only how many did."
        )


def _step_text(job: dict, step_name_prefix: str) -> str:
    for step in job.get("steps", []):
        name = str(step.get("name", ""))
        if name.startswith(step_name_prefix):
            return str(step.get("run", ""))
    raise AssertionError(f"no step named {step_name_prefix!r} in the job")
