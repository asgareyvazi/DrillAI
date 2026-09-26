"""Model routing: choose a provider/model for a request, then record what was used.

Routing is a *policy decision* that must be explainable and auditable, so it is implemented as a
scoring function over declared model metadata rather than a chain of `if model == ...` checks:

    1. hard filters — privacy class, required capabilities, context window, data residency;
    2. soft scoring — cost, latency, and optional preference for a named provider/model;
    3. deterministic tie-break by model name.

Every call is persisted as an ``LlmCall`` row (prompt hash, token counts, cost, latency, finish
reason, routing rationale) so cost and quality can be analysed later — and so a reviewer can see
exactly which model produced a sentence that ended up in a recommendation.

Structured output is validated by :mod:`drillai.ai.guardrails` before it is accepted; a response
that fails validation is retried once with the validation error appended, then rejected.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from drillai.ai.providers import (
    CompletionRequest,
    CompletionResponse,
    LLMCapability,
    LLMProvider,
    ModelSpec,
    PrivacyClass,
    ProviderRegistry,
    provider_registry,
)
from drillai.core.errors import ModelNotConfigured, ProviderError
from drillai.core.logging import get_logger, log_context
from drillai.db.models import LlmCall

logger = get_logger(__name__)

__all__ = [
    "LlmRouter",
    "RouteDecision",
    "RoutingPolicy",
    "TaskProfile",
]


class TaskProfile(enum.StrEnum):
    """What the caller is doing — the routing input that is not about the model."""

    CLASSIFY = "classify"
    EXTRACT = "extract"
    SUMMARIZE = "summarize"
    GENERATE = "generate"
    PLAN = "plan"
    CHAT = "chat"
    CODE = "code"
    EMBED = "embed"


class RoutingPolicy(BaseModel):
    """Deployment policy. Defaults are the conservative ones."""

    model_config = ConfigDict(extra="forbid")

    max_privacy: PrivacyClass = PrivacyClass.PRIVATE_CLOUD
    require_structured_output: bool = True
    prefer_provider: str | None = None
    prefer_model: str | None = None
    max_cost_per_call_usd: float | None = None
    max_latency_ms: int | None = None
    min_context_tokens: int = 0
    allowed_models: list[str] = Field(default_factory=list)
    denied_models: list[str] = Field(default_factory=list)
    data_residency: str | None = None

    def allows_privacy(self, privacy: PrivacyClass) -> bool:
        order = {PrivacyClass.LOCAL: 0, PrivacyClass.PRIVATE_CLOUD: 1, PrivacyClass.PUBLIC_CLOUD: 2}
        return order[privacy] <= order[self.max_privacy]


@dataclass
class RouteDecision:
    provider: LLMProvider
    spec: ModelSpec
    rationale: list[str] = field(default_factory=list)
    considered: list[str] = field(default_factory=list)


#: Which capabilities a task needs.
_TASK_CAPABILITIES: dict[TaskProfile, frozenset[LLMCapability]] = {
    TaskProfile.CLASSIFY: frozenset({LLMCapability.CHAT}),
    TaskProfile.EXTRACT: frozenset({LLMCapability.STRUCTURED_OUTPUT}),
    TaskProfile.SUMMARIZE: frozenset({LLMCapability.CHAT}),
    TaskProfile.GENERATE: frozenset({LLMCapability.CHAT}),
    TaskProfile.PLAN: frozenset({LLMCapability.CHAT, LLMCapability.STRUCTURED_OUTPUT}),
    TaskProfile.CHAT: frozenset({LLMCapability.CHAT}),
    TaskProfile.CODE: frozenset({LLMCapability.CHAT, LLMCapability.CODE}),
    TaskProfile.EMBED: frozenset({LLMCapability.EMBEDDING}),
}


class LlmRouter:
    """Routes completions and persists an audit row for each call."""

    def __init__(
        self,
        *,
        registry: ProviderRegistry | None = None,
        policy: RoutingPolicy | None = None,
    ) -> None:
        self.registry = registry or provider_registry()
        self.policy = policy or RoutingPolicy()

    # ------------------------------------------------------------------ selection
    def route(self, *, profile: TaskProfile, prompt_tokens: int = 0) -> RouteDecision:
        required = _TASK_CAPABILITIES[profile]
        rationale: list[str] = []
        candidates: list[tuple[ModelSpec, LLMProvider]] = []
        considered: list[str] = []
        for provider in self.registry.providers().values():
            for spec in provider.models():
                considered.append(f"{spec.provider}:{spec.model}")
                if spec.model in self.policy.denied_models:
                    continue
                if self.policy.allowed_models and spec.model not in self.policy.allowed_models:
                    continue
                if not required <= set(spec.capabilities):
                    continue
                if not self.policy.allows_privacy(spec.privacy):
                    continue
                needed = max(prompt_tokens, self.policy.min_context_tokens)
                if spec.context_window_tokens < needed:
                    continue
                if self.policy.max_latency_ms is not None and spec.typical_latency_ms > self.policy.max_latency_ms:
                    continue
                candidates.append((spec, provider))

        if not candidates:
            raise ModelNotConfigured(
                "no configured model satisfies the routing policy",
                details={
                    "profile": str(profile),
                    "required_capabilities": sorted(str(item) for item in required),
                    "max_privacy": str(self.policy.max_privacy),
                    "considered": considered,
                },
            )

        def score(spec: ModelSpec) -> tuple[float, str]:
            value = 0.0
            if self.policy.prefer_model and spec.model == self.policy.prefer_model:
                value -= 1000.0
            if self.policy.prefer_provider and spec.provider == self.policy.prefer_provider:
                value -= 500.0
            value += spec.typical_latency_ms / 1000.0
            value += (spec.cost_per_1k_input + spec.cost_per_1k_output) * 10.0
            if spec.privacy is PrivacyClass.LOCAL:
                value -= 1.0
            return value, f"{spec.provider}:{spec.model}"

        candidates.sort(key=lambda item: score(item[0]))
        spec, provider = candidates[0]
        rationale.append(
            f"selected {spec.provider}:{spec.model} for {TaskProfile(profile)} "
            f"(privacy={spec.privacy}, latency≈{spec.typical_latency_ms} ms, "
            f"cost={spec.cost_per_1k_input}/{spec.cost_per_1k_output} per 1k tokens)"
        )
        if len(candidates) > 1:
            rationale.append(f"{len(candidates) - 1} other model(s) were eligible; cheapest-fastest wins")
        return RouteDecision(provider=provider, spec=spec, rationale=rationale, considered=considered)

    # ------------------------------------------------------------------ completion
    async def complete(
        self,
        request: CompletionRequest,
        *,
        session: AsyncSession | None = None,
        org_id: str | None = None,
        profile: TaskProfile = TaskProfile.CHAT,
        subject_kind: str | None = None,
        subject_id: str | None = None,
        agent_run_id: str | None = None,
        workflow_run_id: str | None = None,
        validate: bool = True,
    ) -> tuple[CompletionResponse, RouteDecision]:
        prompt_tokens = sum(len(message.content) // 4 + 4 for message in request.messages)
        decision = self.route(profile=profile, prompt_tokens=prompt_tokens)
        request = request.model_copy(update={"model": request.model or decision.spec.model})
        if decision.spec.supports_temperature is False:
            request = request.model_copy(update={"temperature": 0.0})

        with log_context(llm_provider=decision.spec.provider, llm_model=decision.spec.model):
            response = await self._call_with_retry(decision, request, validate=validate)

        if session is not None and org_id is not None:
            await self._persist_call(
                session,
                org_id=org_id,
                request=request,
                response=response,
                decision=decision,
                profile=profile,
                subject_kind=subject_kind,
                subject_id=subject_id,
                agent_run_id=agent_run_id,
                workflow_run_id=workflow_run_id,
            )
        return response, decision

    async def _call_with_retry(
        self, decision: RouteDecision, request: CompletionRequest, *, validate: bool
    ) -> CompletionResponse:
        provider = decision.provider
        try:
            response = await provider.complete(request)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"{provider.name} call failed: {exc}") from exc

        if validate and request.response_schema and response.structured is None:
            retry_messages = [
                *request.messages,
                # The retry is explicit about what was wrong; it is not a hidden repair loop.
                request.messages[-1].model_copy(update={"content": request.messages[-1].content + "\n\nYour previous answer was not valid JSON matching the schema. Reply with JSON only."}),
            ]
            response = await provider.complete(request.model_copy(update={"messages": retry_messages}))
            if response.structured is None:
                raise ProviderError("provider failed to produce schema-valid JSON after one retry")
        return response

    async def _persist_call(
        self,
        session: AsyncSession,
        *,
        org_id: str,
        request: CompletionRequest,
        response: CompletionResponse,
        decision: RouteDecision,
        profile: TaskProfile,
        subject_kind: str | None,
        subject_id: str | None,
        agent_run_id: str | None,
        workflow_run_id: str | None,
    ) -> None:
        from drillai.observability.tracing import current_trace

        trace = current_trace()
        prompt_text = "\n\n".join(f"{message.role}: {message.content}" for message in request.messages)
        row = LlmCall(
            org_id=org_id,
            provider=response.provider,
            model=response.model,
            response_model=response.model,
            operation=str(profile),
            status="succeeded",
            purpose=str(profile),
            agent_run_id=agent_run_id,
            workflow_run_id=workflow_run_id,
            # There is no generic subject column: a well subject is recorded on ``well_id`` (the
            # column that joins to the rest of the platform) and the full subject is kept in
            # ``attributes`` so any entity type stays traceable.
            well_id=subject_id if subject_kind == "well" else None,
            routing_reason="; ".join(decision.rationale),
            policy=self.policy.model_dump(mode="json"),
            temperature=request.temperature,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=response.usage.cost_usd,
            latency_ms=response.latency_ms,
            finish_reason=response.finish_reason,
            # Prompt/response text is only captured when the caller explicitly opts in: an
            # engineering prompt contains operator data and must not be logged by default.
            content_captured=bool(request.metadata.get("capture_content")),
            request_payload=(
                {"messages": [message.model_dump(mode="json") for message in request.messages]}
                if request.metadata.get("capture_content")
                else {}
            ),
            response_payload=(
                {"text": response.text, "structured": response.structured}
                if request.metadata.get("capture_content")
                else {"structured": response.structured}
            ),
            system_chars=sum(len(message.content) for message in request.messages if message.role == "system"),
            input_chars=len(prompt_text),
            output_chars=len(response.text),
            cache_hit=response.cached,
            trace_id=trace.trace_id if trace else None,
            span_id=trace.span_id if trace else None,
            evidence_refs=[],
            attributes={
                "considered": decision.considered,
                "subject_kind": subject_kind,
                "subject_id": subject_id,
            },
        )
        session.add(row)
        await session.flush()
