"""CP33 end-to-end response-oracle coverage.

CP32 gave the suite a platform-independent verdict: ``expectNoFailures`` in
``frontend/e2e/fixtures.ts`` asks two questions, and neither is about the browser's network log —
did the application write an error of its own, and did the page receive a failing response it did
not provoke. Ten journeys were converted then. The remaining seven could still pass while a call
they never provoked failed, because they judged only ``appConsoleErrors``.

They are converted here. The measurement is the point: the suite was run with the failing responses
recorded per journey, and the twenty journeys that provoke a refusal on purpose are exactly the ones
that needed a tolerance. The other hundred assert with no tolerance at all, which is a stronger
statement than they made before — a background read that starts failing now fails its journey.

Two things could quietly undo this, and both are what this module refuses:

1. **A journey going back to the console-only verdict.** The seven converted specs are named below,
   and each must assert through the shared contract.
2. **A tolerance that is not a tolerance of anything in particular.** ``allowed`` used to take URL
   substrings, which tolerates every method and every status at that URL — including a ``500`` this
   suite exists to catch. Every entry is now an object that must carry a ``reason`` and at least one
   narrowing field, and ``expectNoFailures`` applies the same entries to its server-error check, so
   a ``500`` is tolerable only where a journey names it.

The checks are static: they read the files the suite runs, so a regression fails here before it
reaches a runner.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
E2E_DIR = REPO_ROOT / "frontend" / "e2e"
FIXTURES_PATH = E2E_DIR / "fixtures.ts"

SHARED_CONTRACT = "expectNoFailures"
MATCHER = "provokedFailure"

# The journeys that judged only the application's console lines before this checkpoint. Each one
# provokes refusals on purpose and now states them: a deep link to a well that does not exist, a
# connector edit from a stale revision, an SSRF-refused destination, a run or document that names
# nothing, an approval decision by an identity the server does not allow, an upload the server
# rejects, an injected server fault, and an authentication-enabled deployment where every read
# before sign-in is a 401.
CONVERTED_SPECS = (
    "checkpoint5.spec.ts",
    "connectors-platform.spec.ts",
    "context-deeplinks.spec.ts",
    "error-auth.spec.ts",
    "error-faults.spec.ts",
    "error-matrix.spec.ts",
    "identity-permissions.spec.ts",
)

# An entry that tolerates whatever arrives. Not a tolerance of a refusal the journey provokes.
NARROWING_FIELDS = ("url:", "method:", "status:")


def _spec_files() -> list[Path]:
    return sorted(E2E_DIR.glob("*.spec.ts"))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _array_body(source: str, start: int) -> str:
    """The text between the `[` at `start` and its matching `]`, quotes respected."""
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
    raise AssertionError("unterminated array")


def _object_bodies(body: str) -> list[str]:
    """Every `{ ... }` at the top level of an allowance array."""
    entries: list[str] = []
    depth = 0
    opened: int | None = None
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
        elif char == "{":
            if depth == 0:
                opened = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and opened is not None:
                entries.append(body[opened : index + 1])
                opened = None
        index += 1
    return entries


def _allowances(source: str) -> list[str]:
    """The body of every `allowed: [...]` in a spec."""
    bodies = []
    for match in re.finditer(r"allowed:\s*\[", source):
        bodies.append(_array_body(source, match.end() - 1))
    return bodies


# ---------------------------------------------------------------------------------------------
# 1. Every journey that judges a page judges its responses
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", CONVERTED_SPECS)
def test_cp33_the_converted_journeys_assert_on_the_responses(name: str) -> None:
    """The seven journeys that only read the console must now judge the wire as well."""
    path = E2E_DIR / name
    assert path.exists(), f"{name} must exist"
    source = _read(path)
    assert SHARED_CONTRACT in source, (
        f"{name} judges only the application's console lines. A journey that does not assert on "
        f"`{SHARED_CONTRACT}` passes while a call it never provoked fails, which is the gap this "
        "checkpoint closed."
    )
    assert "expect(appConsoleErrors(consoleErrors)" not in source, (
        f"{name} is back to the console-only verdict: `expect(appConsoleErrors(consoleErrors))` "
        f"says nothing about the responses. Use `{SHARED_CONTRACT}`."
    )


@pytest.mark.parametrize("spec", _spec_files(), ids=lambda path: path.name)
def test_cp33_no_spec_judges_only_the_console(spec: Path) -> None:
    """A spec that collects console errors may not judge them without the responses."""
    source = _read(spec)
    if "consoleErrors" not in source:
        # Nothing collected, nothing to judge. This returns rather than skipping: both
        # certification jobs require the backend suite to report no skips at all.
        return
    assert SHARED_CONTRACT in source, (
        f"{spec.name} collects console errors but never asserts through `{SHARED_CONTRACT}`, so it "
        "cannot tell a refusal the journey provoked from one it did not."
    )


@pytest.mark.parametrize("spec", _spec_files(), ids=lambda path: path.name)
def test_cp33_no_spec_passes_a_bare_string_as_a_tolerance(spec: Path) -> None:
    """`allowed: ['/publish']` tolerates every method and every status at that URL."""
    source = _read(spec)
    for body in _allowances(source):
        without_entries = body
        for entry in _object_bodies(body):
            without_entries = without_entries.replace(entry, "")
        assert "'" not in without_entries and '"' not in without_entries, (
            f"{spec.name} tolerates a bare string in `allowed`. A string names a URL and nothing "
            "else: it would also tolerate a `500` at that URL. Every tolerance must be an object "
            "with a `reason` and at least one narrowing field."
        )


@pytest.mark.parametrize("spec", _spec_files(), ids=lambda path: path.name)
def test_cp33_every_tolerance_says_what_it_tolerates_and_why(spec: Path) -> None:
    """An allowance is a justification, not a silence."""
    source = _read(spec)
    for body in _allowances(source):
        for entry in _object_bodies(body):
            assert "reason:" in entry, (
                f"{spec.name} tolerates a failure without saying why: {entry.strip()[:120]}"
            )
            assert any(field in entry for field in NARROWING_FIELDS), (
                f"{spec.name} tolerates a failure without narrowing it to a request: "
                f"{entry.strip()[:120]}"
            )
            # A status is what makes a tolerance meaningful for a refusal: without one the entry
            # would accept a `500` where the journey expected a `404`.
            assert "status:" in entry, (
                f"{spec.name} tolerates a failure without naming the status it expects: "
                f"{entry.strip()[:120]}"
            )


# ---------------------------------------------------------------------------------------------
# 2. The contract applies the tolerances to every verdict it makes
# ---------------------------------------------------------------------------------------------


def _contract_body() -> str:
    source = _read(FIXTURES_PATH)
    match = re.search(r"export function expectNoFailures\((.*?)\n\): void \{(.*?)\n\}", source, re.S)
    assert match, f"could not find `{SHARED_CONTRACT}` in frontend/e2e/fixtures.ts"
    return match.group(2)


def test_cp33_the_tolerances_are_matched_on_the_request_not_the_url_alone() -> None:
    """A tolerance names the request: a method, a status, or both."""
    source = _read(FIXTURES_PATH)
    match = re.search(r"export function provokedFailure\((.*?)\n\): .*? \{(.*?)\n\}", source, re.S)
    assert match, f"frontend/e2e/fixtures.ts must export `{MATCHER}`"
    body = match.group(2)
    assert "entry.method !== undefined && response.method !== entry.method" in body, (
        f"`{MATCHER}` must refuse a tolerance whose method does not match: without it a `PATCH` "
        "tolerance would also cover a `POST` to the same URL."
    )
    assert "statuses.includes(response.status)" in body, (
        f"`{MATCHER}` must refuse a tolerance whose status does not match: otherwise a journey that "
        "expects a `409` would tolerate the `500` this suite exists to catch."
    )


def test_cp33_a_server_error_is_tolerable_only_where_the_journey_names_it() -> None:
    """The 5xx check applies the same allowances, so an injected fault is the only 500 allowed."""
    body = _contract_body()
    assert "status >= 500" in body, (
        f"`{SHARED_CONTRACT}` must never let a server error pass unremarked"
    )
    server_check = body.split("status >= 500", 1)[1]
    assert f"{MATCHER}(response, allowed)" in server_check, (
        f"`{SHARED_CONTRACT}` must filter its server-error check through `{MATCHER}`: an allowance "
        "for a `404` must not also excuse a `500`, and a `500` a journey does not name must fail."
    )


def test_cp33_the_tolerance_type_requires_a_reason() -> None:
    """`reason` is not optional, so a tolerance cannot be written without one."""
    source = _read(FIXTURES_PATH)
    match = re.search(r"export interface ProvokedFailure \{(.*?)\n\}", source, re.S)
    assert match, "frontend/e2e/fixtures.ts must declare the `ProvokedFailure` entry type"
    body = match.group(1)
    assert re.search(r"\breason: string", body), (
        "a tolerance must carry a `reason`; `reason?: string` would let one be written without one"
    )
    assert not re.search(r"\breason\?:", body), "`reason` must not be optional"


# ---------------------------------------------------------------------------------------------
# 3. The measurement that justified each tolerance still exists
# ---------------------------------------------------------------------------------------------


def test_cp33_the_suite_tolerates_only_failures_it_provokes() -> None:
    """Every allowance in the suite is a refusal a journey provokes on purpose.

    Static check: an allowance may not name a URL that no journey asserts a refusal on. This is a
    spelling check, not a semantic one — the E2E run is what proves the tolerance is the one that
    occurred — but a typo would otherwise turn a tolerance into decoration, and the journey would
    silently go back to tolerating nothing while claiming to tolerate the right thing.
    """
    sections = []
    for spec in _spec_files():
        source = _read(spec)
        for body in _allowances(source):
            for entry in _object_bodies(body):
                url = re.search(r"url: '([^']+)'", entry)
                status = re.search(r"status: (\d+)", entry)
                assert url or status, f"{spec.name} has a tolerance that names neither a url nor a status"
                sections.append((spec.name, url.group(1) if url else "", entry))
    assert sections, "the suite must state the refusals its journeys provoke"
    # Every tolerated URL appears in the spec's own source at least once outside the allowance —
    # the journey has to name the thing it is provoking.
    for name, url, entry in sections:
        if not url:
            continue
        source = _read(E2E_DIR / name)
        assert source.count(url) >= 2, (
            f"{name} tolerates {url!r}, but the journey never names that URL itself: {entry.strip()[:100]}"
        )
