"""Canonical error model.

One error type hierarchy is used by every subsystem (API, domain services, engines,
workflow runtime, ingestion, AI providers) so that failures are:

* machine-readable (stable ``code`` + ``http_status``),
* auditable (``details`` is persisted to the audit/observability layers),
* safe by default (internal failures never leak stack traces into API payloads).

Error codes follow ``<area>.<condition>`` naming, e.g. ``workflow.node_timeout``.
"""

from __future__ import annotations

from typing import Any


class DrillAIError(Exception):
    """Base class for all platform errors."""

    code: str = "platform.error"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_payload(self, *, trace_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            }
        }
        if trace_id:
            payload["error"]["trace_id"] = trace_id
        return payload

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# --------------------------------------------------------------------------- 4xx


class ValidationFailed(DrillAIError):
    code = "platform.validation_failed"
    http_status = 422


class NotFound(DrillAIError):
    code = "platform.not_found"
    http_status = 404


class Conflict(DrillAIError):
    code = "platform.conflict"
    http_status = 409


class PermissionDenied(DrillAIError):
    code = "security.permission_denied"
    http_status = 403


class AuthenticationRequired(DrillAIError):
    code = "security.authentication_required"
    http_status = 401


class ApprovalRequired(DrillAIError):
    """Raised when an action is attempted at a level that needs human approval."""

    code = "security.approval_required"
    http_status = 428  # Precondition Required


class UnsupportedOperation(DrillAIError):
    code = "platform.unsupported_operation"
    http_status = 400


# --------------------------------------------------------------------------- domain


class DomainError(DrillAIError):
    code = "domain.error"
    http_status = 400


class UnitError(DomainError):
    code = "domain.unit_error"


class TwinStateError(DomainError):
    """The digital twin cannot answer the question (missing/inconsistent state)."""

    code = "twin.state_error"


class ContextError(DomainError):
    code = "context.error"


# --------------------------------------------------------------------------- engines


class EngineError(DrillAIError):
    code = "engine.error"
    http_status = 422


class EngineNotFound(EngineError):
    code = "engine.not_found"
    http_status = 404


class EngineInputInvalid(EngineError):
    code = "engine.input_invalid"


class EngineConvergenceError(EngineError):
    """A numerical model failed to converge / produced non-physical output."""

    code = "engine.convergence_failed"


class DependencyCycleError(EngineError):
    code = "engine.dependency_cycle"


# --------------------------------------------------------------------------- workflow


class WorkflowError(DrillAIError):
    code = "workflow.error"
    http_status = 400


class WorkflowDefinitionInvalid(WorkflowError):
    code = "workflow.definition_invalid"


class NodeTypeNotFound(WorkflowError):
    code = "workflow.node_type_not_found"
    http_status = 404


class NodeExecutionFailed(WorkflowError):
    code = "workflow.node_failed"


class NodeTimeout(WorkflowError):
    code = "workflow.node_timeout"
    retryable = True


class RunNotResumable(WorkflowError):
    code = "workflow.run_not_resumable"
    http_status = 409


# --------------------------------------------------------------------------- ai / data


class ProviderError(DrillAIError):
    code = "ai.provider_error"
    http_status = 502
    retryable = True


class ProviderUnavailable(ProviderError):
    code = "ai.provider_unavailable"
    http_status = 503


class ModelNotConfigured(ProviderError):
    code = "ai.model_not_configured"
    http_status = 400


class RetrievalError(DrillAIError):
    code = "ai.retrieval_error"
    http_status = 502


class IngestionError(DrillAIError):
    code = "ingestion.error"
    http_status = 422


class UnsupportedFormat(IngestionError):
    code = "ingestion.unsupported_format"
    http_status = 415


class ExtractionFailed(IngestionError):
    code = "ingestion.extraction_failed"


class ConfigurationError(DrillAIError):
    code = "platform.configuration_error"
    http_status = 500


class IntegrationError(DrillAIError):
    code = "integration.error"
    http_status = 502
    retryable = True


class RateLimited(DrillAIError):
    code = "platform.rate_limited"
    http_status = 429
    retryable = True
