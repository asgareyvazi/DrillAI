"""CP33/CP35 structured request-failure oracle contract.

The E2E suite records every failing HTTP response and transport failure at browser-context scope.
This guard follows the current implementation: typed structured allowances are validated at runtime,
matched one-to-one, and exercised by mutation tests. It intentionally does not preserve the earlier
substring-URL contract, which was too broad to be safe.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = REPO_ROOT / "frontend" / "e2e"
FIXTURES_PATH = E2E_DIR / "fixtures.ts"
ORACLE_PATH = E2E_DIR / "failure-oracle.ts"
MUTATION_TESTS_PATH = REPO_ROOT / "frontend" / "src" / "lib" / "failureOracle.test.ts"

SHARED_CONTRACT = "expectNoFailures"
CONVERTED_SPECS = (
    "checkpoint5.spec.ts",
    "connectors-platform.spec.ts",
    "context-deeplinks.spec.ts",
    "error-auth.spec.ts",
    "error-faults.spec.ts",
    "error-matrix.spec.ts",
    "identity-permissions.spec.ts",
)


def _spec_files() -> list[Path]:
    return sorted(E2E_DIR.glob("*.spec.ts"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _array_body(source: str, start: int) -> str:
    """Return an array body while respecting strings and nested values."""
    depth = 0
    quote: str | None = None
    index = start
    while index < len(source):
        char = source[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return source[start + 1 : index]
        index += 1
    raise AssertionError("unterminated allowance array")


def _array_entries(body: str) -> list[str]:
    """Split array elements on top-level commas, not commas in objects or helper calls."""
    entries: list[str] = []
    start = 0
    stack: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(body):
        char = body[index]
        if quote is not None:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "'\"`":
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}" and stack:
            stack.pop()
        elif char == "," and not stack:
            entry = body[start:index].strip()
            if entry:
                entries.append(entry)
            start = index + 1
        index += 1
    tail = body[start:].strip()
    if tail:
        entries.append(tail)
    return entries


def _allowance_arrays(source: str) -> list[str]:
    return [
        _array_body(source, match.end() - 1)
        for match in re.finditer(r"allowed:\s*\[", source)
    ]


def _object_field(entry: str, field: str) -> str | None:
    match = re.search(rf"\b{re.escape(field)}\s*:\s*([^,\n]+)", entry)
    return match.group(1).strip() if match else None


# ---------------------------------------------------------------------------------------------
# 1. Journeys use the common contract rather than judging only the browser console
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", CONVERTED_SPECS)
def test_cp33_the_converted_journeys_assert_on_the_responses(name: str) -> None:
    path = E2E_DIR / name
    assert path.exists(), f"{name} must exist"
    source = _read(path)
    assert SHARED_CONTRACT in source, (
        f"{name} must use `{SHARED_CONTRACT}` so its intentional refusals are separated from "
        "unprovoked HTTP or transport failures"
    )
    assert "expect(appConsoleErrors(consoleErrors)" not in source, (
        f"{name} has regressed to a console-only verdict"
    )


@pytest.mark.parametrize("spec", _spec_files(), ids=lambda path: path.name)
def test_cp33_no_spec_judges_only_the_console(spec: Path) -> None:
    source = _read(spec)
    if "consoleErrors" in source:
        assert SHARED_CONTRACT in source, (
            f"{spec.name} collects console errors but never checks the response/transport oracle"
        )


def test_all_e2e_specs_use_the_browser_context_failure_fixture() -> None:
    """Every project-spec imports the custom test fixture that owns context-wide observation."""
    for spec in _spec_files():
        source = _read(spec)
        assert "from './fixtures'" in source, (
            f"{spec.name} must import the custom fixture; a base Playwright test would evade its oracle"
        )


# ---------------------------------------------------------------------------------------------
# 2. Every tolerance names the exact request, failure, reason, and finite count
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("spec", _spec_files(), ids=lambda path: path.name)
def test_cp33_no_spec_passes_a_bare_string_as_a_tolerance(spec: Path) -> None:
    """URL strings, URL substrings, and string-valued catch-alls are forbidden."""
    for body in _allowance_arrays(_read(spec)):
        for entry in _array_entries(body):
            assert entry.startswith("{") or entry.startswith("anonymousGet("), (
                f"{spec.name} has an unstructured allowance expression: {entry[:120]!r}"
            )


def test_cp33_helper_allowances_are_typed_and_exact() -> None:
    """The only helper-based exception is the authentication journey's explicit 401 GET builder."""
    source = _read(E2E_DIR / "error-auth.spec.ts")
    match = re.search(
        r"function anonymousGet\(.*?\): FailureAllowance \{(.*?)\n\}", source, re.S
    )
    assert match, "anonymousGet must return the structured FailureAllowance type"
    body = match.group(1)
    for field in ("kind: 'http'", "method: 'GET'", "pathname", "query", "status: 401", "maxMatches", "reason:"):
        assert field in body, f"anonymousGet must specify {field}"


@pytest.mark.parametrize("spec", _spec_files(), ids=lambda path: path.name)
def test_cp33_every_tolerance_is_fully_scoped_and_bounded(spec: Path) -> None:
    for body in _allowance_arrays(_read(spec)):
        for entry in _array_entries(body):
            if entry.startswith("anonymousGet("):
                continue
            assert entry.startswith("{"), f"{spec.name} uses a non-object allowance: {entry[:120]}"
            for field in ("kind", "method", "pathname", "query", "maxMatches", "reason"):
                assert _object_field(entry, field) is not None, (
                    f"{spec.name} allowance lacks `{field}`: {entry.strip()[:160]}"
                )
            method = _object_field(entry, "method")
            assert method in {"'GET'", "'POST'", "'PUT'", "'PATCH'", "'DELETE'"}, (
                f"{spec.name} allowance must name one exact HTTP method"
            )
            pathname = _object_field(entry, "pathname")
            assert pathname is not None and re.fullmatch(r"'/api/v1/[A-Za-z0-9_./:-]+'", pathname), (
                f"{spec.name} allowance must name one canonical API route"
            )
            query = _object_field(entry, "query")
            assert query is not None and re.fullmatch(r"'(?:|\?[A-Za-z0-9_=&%.-]*)'", query), (
                f"{spec.name} allowance must name an exact query string, not a wildcard"
            )
            max_matches = _object_field(entry, "maxMatches")
            assert max_matches is not None and re.fullmatch(r"[1-9][0-9]*", max_matches), (
                f"{spec.name} allowance must have a positive finite count"
            )
            kind = _object_field(entry, "kind")
            if kind == "'http'":
                assert _object_field(entry, "status") is not None, (
                    f"{spec.name} HTTP allowance must name its exact response status"
                )
                assert _object_field(entry, "errorText") is None
            elif kind == "'network'":
                error_text = _object_field(entry, "errorText")
                assert error_text is not None and re.fullmatch(r"'net::ERR_[A-Z0-9_]+'", error_text), (
                    f"{spec.name} network allowance must name one exact Chromium error code"
                )
                assert _object_field(entry, "status") is None
                if error_text == "'net::ERR_ABORTED'":
                    assert _object_field(entry, "signalReason") == "'TimeoutError'", (
                        "ERR_ABORTED may be tolerated only for the exact fetch carrying the timeout signal"
                    )
            else:
                raise AssertionError(f"{spec.name} allowance must declare kind 'http' or 'network': {entry[:160]}")


def test_cp33_the_allowance_type_requires_a_reason_and_positive_count() -> None:
    source = _read(ORACLE_PATH)
    for interface in ("HttpFailureAllowance", "NetworkFailureAllowance"):
        match = re.search(rf"export interface {interface} extends FailureTarget \{{(.*?)\n\}}", source, re.S)
        assert match, f"failure-oracle.ts must export {interface}"
        body = match.group(1)
        for required in ("method: HttpMethod", "reason: string", "maxMatches: number"):
            assert required in body, f"{interface} must require `{required}`"
        assert "maxMatches?:" not in body, "a tolerance may not rely on an implicit or unbounded count"
    assert "needs a positive safe-integer maxMatches bound" in source


# ---------------------------------------------------------------------------------------------
# 3. The matcher is exact, one-to-one, and driven by mutation tests
# ---------------------------------------------------------------------------------------------


def test_cp33_the_tolerances_match_method_route_query_and_failure_code_exactly() -> None:
    source = _read(ORACLE_PATH)
    matcher = re.search(r"function allowanceMatches\(.*?\n\}", source, re.S)
    assert matcher, "failure-oracle.ts must define the matcher used by runtime evaluation"
    body = matcher.group(0)
    for invariant in (
        "entry.method !== failure.method",
        "parsedTarget(entry.pathname, entry.query)",
        "matchesSegments(target.pathSegments, actualSegments)",
        "matchesQuery(target.queryEntries, actualQuery)",
        "failure.status === entry.status",
        "failure.errorText === entry.errorText",
    ):
        assert invariant in body, f"the matcher must enforce {invariant}"
    assert "failure.url.includes(" not in body and "url.href.includes(" not in body, (
        "URL substring matching is broad and is forbidden"
    )


def test_cp33_overlapping_allowances_fail_closed_and_counts_are_one_to_one() -> None:
    source = _read(ORACLE_PATH)
    assert "matching.length > 1" in source, "ambiguous tolerances must not use first-match wins"
    assert "matchedCounts[index]" in source
    assert "usedMatches < maxMatches" in source
    assert "unprovoked ${safeFailureLabel(failure)}" in source
    assert "ambiguous tolerances" in source


def test_cp33_failure_oracle_mutation_tests_cover_the_forbidden_broad_matches() -> None:
    source = _read(MUTATION_TESTS_PATH)
    for expected in (
        "rejects mutations to method, route, query, or status",
        "rejects broad, wildcard, blank, malformed, and unexplained HTTP tolerances",
        "enforces maxMatches one-for-one",
        "matches deliberate offline failures only on their exact endpoint, query, and Chromium code",
        "requires timeout evidence tied to the exact failed fetch",
        "does not classify ERR_ABORTED as benign without the same request ID and AbortError signal",
        "classifies a request as navigation-superseded only after a successor commits in the same frame",
        "never lets an unknown non-abort network failure disappear",
    ):
        assert expected in source, f"failure-oracle mutation coverage is missing: {expected}"


def test_cp33_context_fixture_records_http_and_transport_failures_for_all_pages() -> None:
    fixture = _read(FIXTURES_PATH)
    for invariant in (
        "context.on('response'",
        "context.on('requestfailed'",
        "context.on('page'",
        "response.status() >= 400",
        "await assertFailureOracle(state, testInfo)",
    ):
        assert invariant in fixture, f"the shared fixture is missing context-wide behavior: {invariant}"
    assert re.search(
        r"evaluateFailureOracle\(\s*state\.failedResponses,\s*state\.networkFailures,\s*state\.allowances,\s*state\.applicationOrigin,\s*\)",
        fixture,
    ), "the automatic oracle must bind every page's failures to the fixture's application origin"


def _contract_body() -> str:
    source = _read(FIXTURES_PATH)
    match = re.search(
        r"export function expectNoFailures\((.*?)\n\): void \{(.*?)\n\}", source, re.S
    )
    assert match, f"could not find `{SHARED_CONTRACT}` in frontend/e2e/fixtures.ts"
    return match.group(2)


def test_cp33_the_shared_contract_reports_every_unaccounted_observation() -> None:
    body = _contract_body()
    assert "appConsoleErrors(consoleErrors)" in body
    assert "result.findings" in body
    assert "failedResponses" in body
    assert "evaluateFailureOracle" in body
