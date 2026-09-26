"""Tracing, metrics and the evaluation hook — the observability foundation.

Design:

* **OpenTelemetry-shaped, dependency-free.** Spans carry ``gen_ai.*`` semantic-convention
  attributes for LLM/tool/agent calls (see ``docs/AI_CONTRACTS.md``) so an OTel collector can
  be attached later without changing call sites. The platform does not depend on an exporter
  in-process; spans are persisted (``traces``/``span`` tables) and mirrored to a sink.
* **One trace per request/run**, propagated through ``core.events.TraceContext`` and W3C
  ``traceparent`` headers, so an API request, the workflow run it starts, the engine runs and
  the LLM calls that follow are one searchable trace.
* **Metrics are plain counters/histograms** with labels; a Prometheus/OTLP exporter is a
  deployment concern, not an application dependency.
* **Evaluations are first-class**: :func:`record_evaluation` writes an ``EvaluationResult``
  record so quality is measured on the same data the platform produces, not in a notebook.
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import secrets
import time
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from drillai.core.clock import UTC
from drillai.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "Counter",
    "Histogram",
    "MetricsRegistry",
    "Span",
    "SpanSink",
    "Tracer",
    "current_trace",
    "format_traceparent",
    "gen_ai_attributes",
    "get_metrics",
    "get_tracer",
    "parse_traceparent",
    "record_evaluation",
]

# --------------------------------------------------------------------------- ids / context


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


@dataclass(frozen=True)
class Trace:
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    workflow_run_id: str | None = None
    agent_run_id: str | None = None
    org_id: str | None = None

    def child(self, *, span_id: str | None = None, **overrides: Any) -> Trace:
        return Trace(
            trace_id=self.trace_id,
            span_id=span_id or new_span_id(),
            parent_span_id=self.span_id,
            workflow_run_id=overrides.get("workflow_run_id", self.workflow_run_id),
            agent_run_id=overrides.get("agent_run_id", self.agent_run_id),
            org_id=overrides.get("org_id", self.org_id),
        )


_TRACE: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("drillai_trace", default=None)


def current_trace() -> Trace | None:
    return _TRACE.get()


@contextlib.contextmanager
def use_trace(trace: Trace | None) -> Iterator[Trace | None]:
    token = _TRACE.set(trace)
    try:
        yield trace
    finally:
        _TRACE.reset(token)


def format_traceparent(trace: Trace) -> str:
    return f"00-{trace.trace_id}-{trace.span_id}-01"


def parse_traceparent(value: str | None) -> Trace | None:
    """Parse a W3C ``traceparent`` header; returns ``None`` for anything malformed."""
    if not value:
        return None
    parts = value.strip().split("-")
    if len(parts) != 4 or parts[0] != "00":
        return None
    trace_id, span_id, flags = parts[1], parts[2], parts[3]
    if len(trace_id) != 32 or len(span_id) != 16 or not flags:
        return None
    try:
        int(trace_id, 16)
        int(span_id, 16)
    except ValueError:
        return None
    return Trace(trace_id=trace_id, span_id=span_id)


# --------------------------------------------------------------------------- spans


@runtime_checkable
class SpanSink(Protocol):
    """Where finished spans go. Implementations must not raise."""

    def record(self, span: Span) -> None:  # pragma: no cover - protocol
        ...


@dataclass
class Span:
    name: str
    kind: str = "internal"  # internal | server | client | producer | consumer
    trace: Trace = field(default_factory=lambda: current_trace() or Trace(new_trace_id(), new_span_id()))
    started_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(tz=UTC))
    finished_at: dt.datetime | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "unset"  # unset | ok | error
    error: str | None = None

    @property
    def duration_ms(self) -> float:
        end = self.finished_at or dt.datetime.now(tz=UTC)
        return (end - self.started_at).total_seconds() * 1000.0

    def set_attribute(self, key: str, value: Any) -> Span:
        self.attributes[key] = value
        return self

    def set_attributes(self, values: dict[str, Any]) -> Span:
        self.attributes.update(values)
        return self

    def add_event(self, name: str, **attributes: Any) -> Span:
        self.events.append({"name": name, "at": dt.datetime.now(tz=UTC).isoformat(), "attributes": attributes})
        return self

    def record_error(self, error: BaseException | str) -> Span:
        self.status = "error"
        self.error = str(error)[:2000]
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace.trace_id,
            "span_id": self.trace.span_id,
            "parent_span_id": self.trace.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at.isoformat(),
            "finished_at": (self.finished_at or dt.datetime.now(tz=UTC)).isoformat(),
            "duration_ms": round(self.duration_ms, 3),
            "attributes": self.attributes,
            "events": self.events,
            "workflow_run_id": self.trace.workflow_run_id,
            "agent_run_id": self.trace.agent_run_id,
            "org_id": self.trace.org_id,
        }


class Tracer:
    """Creates spans and forwards them to sinks (persistence, live UI, exporters)."""

    def __init__(self, *, sinks: list[SpanSink] | None = None, sample_ratio: float = 1.0) -> None:
        if not 0.0 <= sample_ratio <= 1.0:
            raise ValueError("sample_ratio must be within [0, 1]")
        self.sinks = list(sinks or [])
        self.sample_ratio = sample_ratio
        self.recording_enabled = sample_ratio > 0

    def add_sink(self, sink: SpanSink) -> None:
        self.sinks.append(sink)

    @contextlib.contextmanager
    def span(
        self,
        name: str,
        *,
        kind: str = "internal",
        trace: Trace | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Iterator[Span]:
        parent = trace or current_trace()
        if parent is None:
            span_trace = Trace(trace_id=new_trace_id(), span_id=new_span_id())
        else:
            span_trace = parent.child()
        span = Span(name=name, kind=kind, trace=span_trace, attributes=dict(attributes or {}))
        token = _TRACE.set(span_trace)
        try:
            yield span
        except Exception as exc:
            span.record_error(exc)
            raise
        finally:
            span.finished_at = dt.datetime.now(tz=UTC)
            if span.status == "unset":
                span.status = "ok"
            _TRACE.reset(token)
            self._finish(span)

    def _finish(self, span: Span) -> None:
        if not self.sinks or not self.recording_enabled:
            return
        payload = span.to_dict()
        for sink in self.sinks:
            try:
                sink.record(span)
            except Exception as exc:
                logger.warning("span sink failed", extra={"extra_fields": {"sink": type(sink).__name__, "error": str(exc), "span": payload["name"]}})


_tracer = Tracer()


def get_tracer() -> Tracer:
    return _tracer


def gen_ai_attributes(
    *,
    operation: str,
    provider: str | None = None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    finish_reasons: list[str] | None = None,
    tool_name: str | None = None,
    agent_name: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Attributes following the OpenTelemetry GenAI semantic conventions (``gen_ai.*``)."""
    attributes: dict[str, Any] = {"gen_ai.operation.name": operation}
    if provider:
        attributes["gen_ai.provider.name"] = provider
    if model:
        attributes["gen_ai.request.model"] = model
        attributes["gen_ai.response.model"] = model
    if input_tokens is not None:
        attributes["gen_ai.usage.input_tokens"] = input_tokens
    if output_tokens is not None:
        attributes["gen_ai.usage.output_tokens"] = output_tokens
    if finish_reasons:
        attributes["gen_ai.response.finish_reasons"] = finish_reasons
    if tool_name:
        attributes["gen_ai.tool.name"] = tool_name
    if agent_name:
        attributes["gen_ai.agent.name"] = agent_name
    attributes.update(extra or {})
    return attributes


# --------------------------------------------------------------------------- metrics


class Counter:
    def __init__(self, name: str, description: str = "", labels: dict[str, str] | None = None) -> None:
        self.name = name
        self.description = description
        self.labels = labels or {}
        self._values: dict[tuple[tuple[str, str], ...], float] = defaultdict(float)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        self._values[_label_key({**self.labels, **labels})] += amount

    def value(self, **labels: str) -> float:
        return self._values.get(_label_key({**self.labels, **labels}), 0.0)

    def snapshot(self) -> dict[str, float]:
        return {"|".join(f"{k}={v}" for k, v in key): value for key, value in self._values.items()}


class Histogram:
    def __init__(self, name: str, description: str = "", labels: dict[str, str] | None = None) -> None:
        self.name = name
        self.description = description
        self.labels = labels or {}
        self._values: dict[tuple[tuple[str, str], ...], list[float]] = defaultdict(list)

    def observe(self, value: float, **labels: str) -> None:
        self._values[_label_key({**self.labels, **labels})].append(float(value))

    def snapshot(self) -> dict[str, dict[str, float]]:
        out: dict[str, dict[str, float]] = {}
        for key, values in self._values.items():
            ordered = sorted(values)
            out["|".join(f"{k}={v}" for k, v in key)] = {
                "count": float(len(ordered)),
                "sum": float(sum(ordered)),
                "min": ordered[0],
                "max": ordered[-1],
                "p50": ordered[len(ordered) // 2],
                "p95": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
            }
        return out


def _label_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


class MetricsRegistry:
    def __init__(self) -> None:
        self._counters: dict[str, Counter] = {}
        self._histograms: dict[str, Histogram] = {}

    def counter(self, name: str, description: str = "") -> Counter:
        if name not in self._counters:
            self._counters[name] = Counter(name, description)
        return self._counters[name]

    def histogram(self, name: str, description: str = "") -> Histogram:
        if name not in self._histograms:
            self._histograms[name] = Histogram(name, description)
        return self._histograms[name]

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": {name: counter.snapshot() for name, counter in self._counters.items()},
            "histograms": {name: histogram.snapshot() for name, histogram in self._histograms.items()},
        }


_metrics = MetricsRegistry()


def get_metrics() -> MetricsRegistry:
    return _metrics


# --------------------------------------------------------------------------- evaluations


@dataclass(frozen=True)
class EvaluationRecord:
    """One measured expectation. Written to the ``evaluation_results`` table by the caller."""

    suite_key: str
    case_key: str
    subject_kind: str
    subject_id: str
    passed: bool
    score: float | None
    metrics: dict[str, Any]
    notes: str | None = None
    measured_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(tz=UTC))


_EVALUATIONS: list[EvaluationRecord] = []


def record_evaluation(record: EvaluationRecord) -> None:
    """In-process evaluation log (dashboards + tests read this; persistence is the caller's job)."""
    _EVALUATIONS.append(record)
    get_metrics().counter("drillai.evaluations", "evaluation cases executed").inc(
        1, suite=record.suite_key, result="pass" if record.passed else "fail"
    )
    if record.score is not None:
        get_metrics().histogram("drillai.evaluation.score", "evaluation scores").observe(record.score, suite=record.suite_key)


def recorded_evaluations() -> list[EvaluationRecord]:
    return list(_EVALUATIONS)


def reset_evaluations() -> None:
    _EVALUATIONS.clear()


@contextlib.contextmanager
def timed(histogram: Histogram, **labels: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        histogram.observe((time.perf_counter() - started) * 1000.0, **labels)
