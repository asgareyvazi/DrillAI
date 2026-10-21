"""CP31 CI deployment probe contract.

CI failures are reported by GitHub Actions as an exit code and an annotation, rarely as a readable
reason. The Linux Docker Compose deployment smoke gate therefore failed with a bare ``exit code 22``
even though the defect was a two-character mistake in a ``curl`` invocation: the gate probed the
GET-only readiness route with ``HEAD`` (``curl -fsSI``). FastAPI registers an ``APIRoute`` for the
declared methods only — unlike Starlette's plain ``Route``, it does **not** add ``HEAD`` to a ``GET``
route — so the probe was answered with ``405 Method Not Allowed``. ``curl -f`` turns an HTTP error
into exit code 22, and ``set -o pipefail`` propagates that status out of the pipeline, so the whole
job died at the gate with no indication of which probe had failed.

This module makes that class of defect impossible to ship again. It reads the workflow that CI
actually executes, extracts every ``curl`` probe it makes against the DrillAI API, and holds two
invariants:

1. **The probe is servable.** Every API path a probe targets must exist in the application's route
   table, and the HTTP method the probe uses must be one of the methods that path is registered
   for. A probe that is not servable is a gate that fails for a reason unrelated to the deployment.
2. **The probe's assertion holds.** When a probe pipes its response into ``grep``, the pattern it
   greps for must actually be present in the response the application produces (in the body, or in
   the response headers when the probe dumps them with ``-D -``).

Both invariants are checked against the real application — the same ``create_app()`` factory, route
table and security headers that the deployed container serves — so the guard fails before the commit
reaches CI rather than after it.

The checks are deliberately generic: they read whatever probes the workflow contains, so a future
probe that regresses in the same way is caught without touching this file.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from drillai.api.app import create_app
from drillai.core.config import get_settings, reset_settings_cache
from drillai.db.models import Base

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Probes against these paths are served by the application and can therefore be replayed locally.
# Probes against anything else (a static asset, an external service) are out of scope.
API_PATH_PREFIX = "/api/"

# The CI deployment gate runs the container stack with authentication disabled, exactly as the
# workflow's step environment sets it. The guard replays the probes under the same condition so a
# probe that only works because of a development fallback is visible here rather than in CI.
CI_AUTH_ENABLED = False

# Minimum number of API probes the workflow must contain. This is a floor on the *parser*, not on
# the workflow: if the extraction logic ever stops finding probes, the two invariants above would
# silently pass over an empty set, so the count is asserted explicitly.
MIN_EXPECTED_API_PROBES = 5

_CURL_TOKEN = re.compile(r"\bcurl\b")


# ---------------------------------------------------------------------------------------------
# Workflow parsing
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Probe:
    """A single ``curl`` invocation the workflow makes against the DrillAI API."""

    method: str
    path: str
    headers: dict[str, str]
    dumps_headers: bool
    grep_pattern: str | None
    grep_ignore_case: bool
    source: str

    @property
    def label(self) -> str:
        return f"{self.method} {self.path}"


@dataclass
class WorkflowProbes:
    probes: list[Probe] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)


def _iter_run_scripts(node: Any) -> list[str]:
    """Collect every ``run:`` script in the workflow, whatever the nesting."""
    scripts: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run" and isinstance(value, str):
                scripts.append(value)
            else:
                scripts.extend(_iter_run_scripts(value))
    elif isinstance(node, list):
        for item in node:
            scripts.extend(_iter_run_scripts(item))
    return scripts


def _cut_at_top_level(text: str, delimiters: tuple[str, ...]) -> str:
    """Return the leading part of ``text`` up to the first top-level delimiter.

    Delimiters inside single or double quotes are ignored, so a ``|`` inside a ``grep`` pattern does
    not end the command.
    """
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == quote:
                quote = None
            elif char == "\\" and quote == '"':
                index += 1
        elif char in {"'", '"'}:
            quote = char
        else:
            for delimiter in delimiters:
                if text.startswith(delimiter, index):
                    return text[:index]
        index += 1
    return text


def _split_top_level(text: str, delimiter: str) -> list[str]:
    """Split ``text`` on ``delimiter`` occurrences that are not inside quotes."""
    parts: list[str] = []
    remainder = text
    while True:
        quote: str | None = None
        index = 0
        cut = -1
        while index < len(remainder):
            char = remainder[index]
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in {"'", '"'}:
                quote = char
            elif remainder.startswith(delimiter, index):
                cut = index
                break
            index += 1
        if cut < 0:
            parts.append(remainder)
            return parts
        parts.append(remainder[:cut])
        remainder = remainder[cut + len(delimiter) :]


# Short curl options that consume the following token as their value. Every other short option is a
# boolean flag, so a cluster such as ``-fsSI`` expands to ``-f -s -S -I``. Note that lowercase ``f``
# is curl's boolean "fail on HTTP error" flag, while uppercase ``F`` is ``--form`` and does take a
# value — conflating them would hide the very ``-fsSI`` cluster this module exists to catch.
_SHORT_OPTIONS_WITH_VALUE = frozenset("XHDobcuAewrtTEKxyz")

_LONG_OPTIONS_WITH_VALUE = frozenset(
    {
        "--request",
        "--header",
        "--dump-header",
        "--output",
        "--data",
        "--user",
        "--cookie",
        "--url",
        "--write-out",
        "--form",
        "--config",
        "--proxy",
        "--referer",
        "--max-time",
        "--retry",
        "--retry-delay",
        "--connect-timeout",
    }
)


def _expand_curl_arguments(tokens: list[str]) -> list[tuple[str, str | None]]:
    """Expand curl's argument syntax into ``(option, value)`` pairs.

    curl accepts boolean short options in a cluster (``-fsSI`` is ``-f -s -S -I``) and both
    ``-X POST`` and ``-XPOST``. The defect this module guards against was written as ``-fsSI``, so a
    parser that only recognised a standalone ``-I`` would not have seen it.
    """
    expanded: list[tuple[str, str | None]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1

        if token.startswith("--"):
            name, _, inline = token.partition("=")
            if inline:
                expanded.append((name, inline))
                continue
            if name in _LONG_OPTIONS_WITH_VALUE and index < len(tokens):
                expanded.append((name, tokens[index]))
                index += 1
            else:
                expanded.append((name, None))
            continue

        if token.startswith("-") and len(token) > 1:
            cluster = token[1:]
            position = 0
            while position < len(cluster):
                letter = cluster[position]
                remainder = cluster[position + 1 :]
                if letter in _SHORT_OPTIONS_WITH_VALUE:
                    if remainder:
                        expanded.append((f"-{letter}", remainder))
                    elif index < len(tokens):
                        expanded.append((f"-{letter}", tokens[index]))
                        index += 1
                    else:
                        expanded.append((f"-{letter}", None))
                    position = len(cluster)
                    continue
                expanded.append((f"-{letter}", None))
                position += 1
            continue

        expanded.append((token, None))
    return expanded


def _parse_curl(command: str, source: str) -> Probe | None:
    """Parse one ``curl`` command into a :class:`Probe` (or ``None`` when it is not an API probe)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens or tokens[0] != "curl":
        return None

    method = "GET"
    headers: dict[str, str] = {}
    dumps_headers = False
    url: str | None = None

    for option, value in _expand_curl_arguments(tokens[1:]):
        if option in {"-I", "--head"}:
            method = "HEAD"
            # `curl -I` writes the response headers to stdout and no body, so a probe that greps
            # the output of a HEAD request is reading headers even though it did not ask for them.
            dumps_headers = True
        elif option in {"-X", "--request"} and value:
            method = value.upper()
        elif option in {"-H", "--header"} and value:
            name, _, header_value = value.partition(":")
            headers[name.strip()] = header_value.strip()
        elif option in {"-D", "--dump-header"}:
            dumps_headers = True
        elif option.startswith("http://") or option.startswith("https://"):
            url = option

    if url is None:
        return None
    path = httpx.URL(url).path
    if not path.startswith(API_PATH_PREFIX):
        return None

    return Probe(
        method=method,
        path=path,
        headers=headers,
        dumps_headers=dumps_headers,
        grep_pattern=None,
        grep_ignore_case=False,
        source=source,
    )


def _parse_grep(command: str) -> tuple[str | None, bool]:
    """Return the pattern and case-sensitivity of a ``grep`` the probe is piped into."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None, False
    if not tokens or tokens[0] != "grep":
        return None, False
    ignore_case = any(token in {"-i", "--ignore-case"} for token in tokens[1:])
    for token in tokens[1:]:
        if token.startswith("-") and len(token) > 1 and not token.startswith("--"):
            continue
        if token.startswith("--"):
            continue
        return token, ignore_case
    return None, ignore_case


def extract_probes(workflow_text: str) -> WorkflowProbes:
    """Extract every API probe the workflow makes, with the assertion each one carries."""
    workflow = yaml.safe_load(workflow_text)
    scripts = _iter_run_scripts(workflow)
    probes: list[Probe] = []
    unparsed: list[str] = []

    for script in scripts:
        # Join shell line continuations so a probe split across two lines is seen as one command.
        joined = re.sub(r"\\\s*\n", " ", script)
        for line in joined.splitlines():
            for match in _CURL_TOKEN.finditer(line):
                tail = line[match.start() :]
                command = _cut_at_top_level(tail, ("|", "&&", "||", ";"))
                probe = _parse_curl(command, line.strip())
                if probe is None:
                    continue
                remainder = tail[len(command) :].strip()
                if remainder.startswith("|"):
                    pattern, ignore_case = _parse_grep(remainder[1:].strip())
                    probe = Probe(
                        method=probe.method,
                        path=probe.path,
                        headers=probe.headers,
                        dumps_headers=probe.dumps_headers,
                        grep_pattern=pattern,
                        grep_ignore_case=ignore_case,
                        source=probe.source,
                    )
                probes.append(probe)

    return WorkflowProbes(probes=probes, unparsed=unparsed)


def _route_templates(app: Any) -> dict[str, set[str]]:
    """Map every served path template to the HTTP methods it is registered for."""
    templates: dict[str, set[str]] = {}
    for path, operations in app.openapi().get("paths", {}).items():
        methods = {method.upper() for method in operations if method.lower() in {"get", "post", "put", "patch", "delete", "head", "options"}}
        if methods:
            templates[path] = methods
    return templates


def _template_regex(template: str) -> re.Pattern[str]:
    """Build a matcher for an OpenAPI path template such as ``/api/v1/wells/{well_id}``."""
    pattern = re.escape(template)
    pattern = re.sub(r"\\\{[^}]+\\\}", r"[^/]+", pattern)
    return re.compile(f"^{pattern}$")


def _matching_methods(templates: dict[str, set[str]], path: str) -> set[str] | None:
    """Return the methods the application serves for ``path``, or ``None`` when it serves no such path."""
    for template, methods in templates.items():
        if _template_regex(template).match(path):
            return methods
    return None


# ---------------------------------------------------------------------------------------------
# Application harness
# ---------------------------------------------------------------------------------------------


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false" if not CI_AUTH_ENABLED else "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/cp31_probe.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_RUN_EVENT_STREAM_POLL_SECONDS", "0.05")
    reset_settings_cache()
    return create_app(get_settings())


async def _create_schema(app: Any) -> None:
    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _header_block(response: httpx.Response) -> str:
    """Render the response headers the way ``curl -D -`` writes them to stdout."""
    return "\n".join(f"{name}: {value}" for name, value in response.headers.multi_items())


def _matches(pattern: str, text: str, ignore_case: bool) -> bool:
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.search(pattern, text, flags) is not None
    except re.error:
        return pattern in text.lower() if ignore_case else pattern in text


# ---------------------------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------------------------


async def test_cp31_ci_01_every_workflow_api_probe_is_served_by_the_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No CI probe may target an API path the application does not serve with that method.

    This is the invariant the Linux deployment gate violated: ``curl -fsSI`` asked for ``HEAD`` on a
    route registered for ``GET`` only, and FastAPI answered ``405``, which ``curl -f`` reports as
    exit code 22.
    """
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    extracted = extract_probes(workflow_text)

    assert len(extracted.probes) >= MIN_EXPECTED_API_PROBES, (
        f"expected at least {MIN_EXPECTED_API_PROBES} API probes in {WORKFLOW_PATH}, "
        f"found {len(extracted.probes)}: {[p.label for p in extracted.probes]}"
    )

    app = _make_app(tmp_path, monkeypatch)
    await _create_schema(app)
    try:
        templates = _route_templates(app)
        assert templates, "the application must expose an OpenAPI route table"

        offenders: list[str] = []
        for probe in extracted.probes:
            served = _matching_methods(templates, probe.path)
            if served is None:
                offenders.append(
                    f"{probe.label}: the application serves no route at {probe.path} "
                    f"(probe source: {probe.source})"
                )
                continue
            if probe.method not in served:
                offenders.append(
                    f"{probe.label}: the route {probe.path} is registered for "
                    f"{sorted(served)} only, so this probe is answered with 405 Method Not Allowed "
                    f"and `curl -f` fails the job with exit code 22 "
                    f"(probe source: {probe.source})"
                )
        assert not offenders, "CI probes that the application cannot serve:\n" + "\n".join(offenders)
    finally:
        await app.state.database.dispose()


async def test_cp31_ci_02_workflow_probe_assertions_hold_against_the_real_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every ``grep`` a workflow probe is piped into must match the real response.

    The deployment gate asserts on the readiness payload and on the security headers. Those
    assertions only hold if the probe reads the response the application actually sends, so this
    replays each probe and re-checks its pattern against the body (or the dumped headers).
    """
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    extracted = extract_probes(workflow_text)

    asserted = [probe for probe in extracted.probes if probe.grep_pattern]
    assert asserted, f"no asserted API probe found in {WORKFLOW_PATH}; the parser is broken"

    app = _make_app(tmp_path, monkeypatch)
    await _create_schema(app)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://ci-probe") as client:
            failures: list[str] = []
            for probe in asserted:
                response = await client.request(probe.method, probe.path, headers=probe.headers)
                haystack = _header_block(response) if probe.dumps_headers else response.text
                if not _matches(probe.grep_pattern, haystack, probe.grep_ignore_case):
                    failures.append(
                        f"{probe.label}: pattern {probe.grep_pattern!r} is absent from the "
                        f"{'response headers' if probe.dumps_headers else 'response body'} "
                        f"(status {response.status_code}, body {response.text[:200]!r})"
                    )
            assert not failures, "CI probe assertions that do not hold:\n" + "\n".join(failures)
    finally:
        await app.state.database.dispose()


def test_cp31_ci_03_no_api_probe_uses_head_against_a_get_only_route() -> None:
    """Head-probing a GET-only route is the specific defect that broke the Linux gate.

    FastAPI's ``APIRoute`` registers exactly the declared methods, so a ``HEAD`` probe of a
    ``GET`` route is answered with ``405``. The guard above is generic; this one states the
    concrete rule that was broken, so the workflow's intent is readable without archaeology.
    """
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    extracted = extract_probes(workflow_text)

    head_probes = [probe for probe in extracted.probes if probe.method == "HEAD"]
    assert not head_probes, (
        "CI probes the DrillAI API with HEAD; FastAPI registers only the declared methods, so a "
        "HEAD probe of a GET-only route is answered with 405 and `curl -f` fails the job:\n"
        + "\n".join(f"{probe.label} (probe source: {probe.source})" for probe in head_probes)
    )


async def test_cp31_ci_04_the_readiness_route_the_gate_probes_answers_get_with_the_expected_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The readiness route the deployment gate probes must answer ``GET`` with a healthy payload.

    This pins the route the gate depends on: a ``GET`` must return ``200`` with ``"status":"ok"``
    and the security headers the gate greps for, so the gate's assertions have something real to
    match against.
    """
    app = _make_app(tmp_path, monkeypatch)
    await _create_schema(app)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://ci-probe") as client:
            response = await client.get("/api/v1/health/ready")
            assert response.status_code == 200
            assert '"status":"ok"' in response.text
            assert "nosniff" in response.headers.get("x-content-type-options", "")
            assert response.headers.get("content-security-policy", "")

            # The gate dumps the headers and greps them; the same headers must be present there.
            assert _matches("x-content-type-options: nosniff", _header_block(response), True)
            assert _matches("content-security-policy:", _header_block(response), True)
    finally:
        await app.state.database.dispose()
