"""Structured logging with correlation context.

Log records are emitted as single-line JSON so they can be shipped to any log store and
joined with spans/traces by ``trace_id``. Context is carried in a ``ContextVar`` and set
by the API middleware, background jobs, workflow runs and connector pollers.

Security: secret-looking keys are redacted automatically, and ``log_request_bodies`` is
deliberately not configurable — payload capture belongs in the trace layer with explicit
consent, not in logs.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any

from drillai.core.clock import UTC

# The default is a read-only empty mapping: a mutable literal would be shared by every
# task that never sets context, and a stray mutation would leak across requests.
_EMPTY: Mapping[str, Any] = MappingProxyType({})
_CONTEXT: ContextVar[Mapping[str, Any]] = ContextVar("drillai_log_context", default=_EMPTY)

_REDACT_KEYS = {
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "private_key",
    "bot_token",
    "credential",
}
_MAX_VALUE_CHARS = 2000


def set_log_context(**values: Any) -> None:
    ctx = dict(_CONTEXT.get())
    ctx.update({k: v for k, v in values.items() if v is not None})
    _CONTEXT.set(ctx)


def clear_log_context() -> None:
    _CONTEXT.set({})


@contextmanager
def log_context(**values: Any) -> Iterator[None]:
    token = _CONTEXT.set({**_CONTEXT.get(), **{k: v for k, v in values.items() if v is not None}})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_log_context() -> dict[str, Any]:
    return dict(_CONTEXT.get())


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if k.lower() in _REDACT_KEYS else _redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    if isinstance(value, str) and len(value) > _MAX_VALUE_CHARS:
        return value[:_MAX_VALUE_CHARS] + f"...<{len(value) - _MAX_VALUE_CHARS} chars truncated>"
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: _redact(v) for k, v in _CONTEXT.get().items()})
        for key in ("engine", "node", "provider", "method", "path", "status_code", "duration_ms"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update({k: _redact(v) for k, v in extra.items()})
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = _CONTEXT.get()
        ctx_str = " ".join(f"{k}={v}" for k, v in ctx.items() if k in {"trace_id", "run_id", "org_id"})
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: {record.getMessage()}"
        return f"{base} {ctx_str}".rstrip()


_CONFIGURED = False


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """Configure the root logger exactly once."""
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED:
        root.setLevel(level.upper())
        return
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter() if json_output else ConsoleFormatter())
    root.handlers = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore", "sqlalchemy.engine.Engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not _CONFIGURED:  # pragma: no cover - convenience for direct module use
        configure_logging()
    return logger


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """Log with structured fields attached (stored under ``extra_fields``)."""
    logger.log(level, message, extra={"extra_fields": fields})
