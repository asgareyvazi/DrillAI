"""LLM provider abstraction.

The platform is *LLM-agnostic by construction*: everything above this module talks to
:class:`LLMProvider`, and only this module knows about HTTP APIs, model names or vendors.

Two implementations ship with the code:

* :class:`EchoProvider` — a deterministic, offline provider used by tests, CI, air-gapped
  deployments and demos. It never invents engineering numbers: it can only restate structured
  values it was given or emit a schema-shaped answer. This makes "no LLM in the physics" testable.
* :class:`OpenAICompatibleProvider` — one client for any OpenAI-compatible endpoint
  (OpenAI, Azure OpenAI, vLLM, Ollama, LiteLLM gateway, Bedrock proxy…). A deployment points it
  at a URL and credentials held in a :class:`SecretRef`; no provider SDK is required.

Capabilities are declared per model so routing (cost/latency/privacy/capability) can be a
data-driven decision rather than an if/else on model name.
"""

from __future__ import annotations

import abc
import datetime as dt
import enum
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from drillai.core.clock import UTC
from drillai.core.errors import ModelNotConfigured, ProviderError, ProviderUnavailable
from drillai.core.logging import get_logger
from drillai.observability.tracing import gen_ai_attributes, get_tracer

logger = get_logger(__name__)

__all__ = [
    "ChatMessage",
    "CompletionRequest",
    "CompletionResponse",
    "EchoProvider",
    "LLMCapability",
    "LLMProvider",
    "ModelSpec",
    "OpenAICompatibleProvider",
    "PrivacyClass",
    "ProviderRegistry",
    "Usage",
    "provider_registry",
]


class LLMCapability(enum.StrEnum):
    CHAT = "chat"
    STRUCTURED_OUTPUT = "structured_output"
    TOOL_CALLING = "tool_calling"
    LONG_CONTEXT = "long_context"
    VISION = "vision"
    EMBEDDING = "embedding"
    CODE = "code"


class PrivacyClass(enum.StrEnum):
    """How far data may travel. Routing refuses to send data to a provider outside the class."""

    LOCAL = "local"  # runs on the operator's own hardware
    PRIVATE_CLOUD = "private_cloud"  # dedicated tenancy / no training on data
    PUBLIC_CLOUD = "public_cloud"  # third-party multi-tenant endpoint


class ModelSpec(BaseModel):
    """What a model can do and what it costs — the input to routing decisions."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    capabilities: frozenset[LLMCapability] = Field(default_factory=lambda: frozenset({LLMCapability.CHAT}))
    privacy: PrivacyClass = PrivacyClass.PRIVATE_CLOUD
    context_window_tokens: int = 32_000
    max_output_tokens: int = 4096
    cost_per_1k_input: float = 0.0
    cost_per_1k_output: float = 0.0
    typical_latency_ms: int = 1500
    supports_temperature: bool = True
    notes: str | None = None


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str = Field(pattern="^(system|user|assistant|tool)$")
    content: str
    name: str | None = None
    tool_call_id: str | None = None


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage]
    model: str | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    response_schema: dict[str, Any] | None = Field(
        default=None, description="JSON Schema the answer must satisfy (structured output)"
    )
    tools: list[dict[str, Any]] = Field(default_factory=list)
    stop: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = 60.0


class Usage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0

    @classmethod
    def estimate(cls, request: CompletionRequest, response_text: str, spec: ModelSpec | None) -> Usage:
        """Rough fallback when a provider does not report usage (~4 chars/token)."""
        input_tokens = sum(len(message.content) // 4 + 4 for message in request.messages)
        output_tokens = len(response_text) // 4
        cost = 0.0
        if spec is not None:
            cost = (input_tokens / 1000) * spec.cost_per_1k_input + (output_tokens / 1000) * spec.cost_per_1k_output
        return cls(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            cost_usd=round(cost, 6),
        )


class CompletionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    model: str
    provider: str
    finish_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    structured: dict[str, Any] | None = None
    latency_ms: float = 0.0
    raw: dict[str, Any] = Field(default_factory=dict)
    cached: bool = False


@dataclass
class ProviderHealth:
    provider: str
    healthy: bool
    checked_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(tz=UTC))
    detail: str | None = None


class LLMProvider(abc.ABC):
    """The contract every provider implements."""

    name: str = "provider"
    privacy: PrivacyClass = PrivacyClass.PUBLIC_CLOUD

    @abc.abstractmethod
    def models(self) -> list[ModelSpec]:  # pragma: no cover - abstract
        ...

    @abc.abstractmethod
    async def complete(self, request: CompletionRequest) -> CompletionResponse:  # pragma: no cover - abstract
        ...

    async def health(self) -> ProviderHealth:  # pragma: no cover - default implementation
        return ProviderHealth(provider=self.name, healthy=True)

    def supports(self, capability: LLMCapability) -> bool:
        return any(capability in spec.capabilities for spec in self.models())


# --------------------------------------------------------------------------- echo (offline)


class EchoProvider(LLMProvider):
    """Deterministic provider for tests, CI and air-gapped demos.

    Behaviour is intentionally boring and safe: it never fabricates a number that was not in
    the prompt. For structured requests it returns the *values it was given*, and when a field
    cannot be grounded it responds with ``null`` and an explicit ``"insufficient_context"``
    reason — which is exactly what a good model should do, and what tests can assert.
    """

    name = "echo"
    privacy = PrivacyClass.LOCAL

    def __init__(self, *, model: str = "echo-deterministic-v1") -> None:
        self._model = model

    def models(self) -> list[ModelSpec]:
        return [
            ModelSpec(
                provider=self.name,
                model=self._model,
                capabilities=frozenset(
                    {
                        LLMCapability.CHAT,
                        LLMCapability.STRUCTURED_OUTPUT,
                        LLMCapability.TOOL_CALLING,
                        LLMCapability.LONG_CONTEXT,
                    }
                ),
                privacy=PrivacyClass.LOCAL,
                context_window_tokens=200_000,
                max_output_tokens=8192,
                cost_per_1k_input=0.0,
                cost_per_1k_output=0.0,
                typical_latency_ms=5,
                notes="offline deterministic provider used for tests and demos",
            )
        ]

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        started = time.perf_counter()
        user_text = "\n".join(message.content for message in request.messages if message.role == "user")
        grounded = _extract_numbered_facts(user_text)
        if request.response_schema:
            structured = _fill_schema(request.response_schema, grounded)
            text = json.dumps(structured, indent=2, sort_keys=True)
            finish_reason = "stop"
        else:
            text = _summarize_deterministically(request, grounded)
            structured = None
            finish_reason = "stop"
        return CompletionResponse(
            text=text,
            model=request.model or self._model,
            provider=self.name,
            finish_reason=finish_reason,
            usage=Usage.estimate(request, text, self.models()[0]),
            structured=structured,
            latency_ms=(time.perf_counter() - started) * 1000,
            raw={"mode": "deterministic"},
        )

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, healthy=True, detail="offline provider always available")


_NUMBER_FACT = "([A-Za-z0-9_ /%().-]{2,60}?)\\s*=\\s*(-?\\d+(?:\\.\\d+)?)\\s*([A-Za-z0-9/%°._-]*)\\s*(?:\\[([^\\]]+)\\])?"


def _extract_numbered_facts(text: str) -> dict[str, tuple[float, str | None, str | None]]:
    import re

    facts: dict[str, tuple[float, str | None, str | None]] = {}
    for match in re.finditer(_NUMBER_FACT, text):
        key = match.group(1).strip().lower().replace(" ", "_")
        try:
            value = float(match.group(2))
        except ValueError:  # pragma: no cover - regex guarantees a number
            continue
        facts[key] = (value, match.group(3) or None, match.group(4) or None)
    return facts


def _fill_schema(schema: dict[str, Any], grounded: dict[str, tuple[float, str | None, str | None]]) -> dict[str, Any]:
    """Fill a JSON-schema object using only grounded values; unknown fields become null."""
    properties = schema.get("properties") or {}
    result: dict[str, Any] = {}
    for name, definition in properties.items():
        candidates = [key for key in grounded if name.lower() in key or key in name.lower()]
        value: Any = None
        if candidates:
            value = grounded[sorted(candidates)[0]][0]
        elif definition.get("type") == "array":
            value = []
        elif definition.get("type") == "string" and definition.get("enum"):
            value = definition["enum"][0]
        elif definition.get("type") == "boolean":
            value = False
        result[name] = value
    if "reasoning" in result or "rationale" in result:
        key = "reasoning" if "reasoning" in result else "rationale"
        if not result[key]:
            result[key] = "insufficient_context: no grounded value was supplied in the prompt"
    if "confidence" in result and result.get("confidence") is None:
        result["confidence"] = 0.0
    return result


def _summarize_deterministically(request: CompletionRequest, grounded: dict[str, tuple[float, str | None, str | None]]) -> str:
    lines: list[str] = []
    for message in request.messages:
        if message.role == "system":
            continue
        body = " ".join(message.content.split())
        if len(body) > 600:
            body = body[:600] + " …"
        lines.append(f"{message.role.upper()}: {body}")
    if grounded:
        lines.append("")
        lines.append("GROUNDED VALUES (verbatim from the context; nothing else is asserted):")
        for key, (value, unit, source) in sorted(grounded.items())[:40]:
            suffix = f" {unit}" if unit else ""
            origin = f" [{source}]" if source else ""
            lines.append(f"- {key} = {value}{suffix}{origin}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- HTTP provider


class OpenAICompatibleProvider(LLMProvider):
    """Any OpenAI-compatible ``/chat/completions`` endpoint.

    Configuration comes from the environment or from a stored :class:`SecretRef`; credentials are
    never read from workflow configuration, never logged, and never returned by the API.
    """

    name = "openai_compatible"
    privacy = PrivacyClass.PRIVATE_CLOUD

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        models: list[ModelSpec] | None = None,
        privacy: PrivacyClass = PrivacyClass.PRIVATE_CLOUD,
        timeout_seconds: float = 60.0,
        name: str | None = None,
    ) -> None:
        if not base_url:
            raise ModelNotConfigured("base_url is required for an OpenAI-compatible provider")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.privacy = privacy
        self.timeout_seconds = timeout_seconds
        self.name = name or self.name
        self._models = models or [
            ModelSpec(provider=self.name, model="default", privacy=privacy, context_window_tokens=32_000)
        ]

    def models(self) -> list[ModelSpec]:
        return self._models

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        import httpx

        if not self._api_key:
            raise ModelNotConfigured(
                f"provider {self.name!r} has no API key configured; store one as a secret reference"
            )
        payload: dict[str, Any] = {
            "model": request.model or self._models[0].model,
            "messages": [message.model_dump(exclude_none=True) for message in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop
        if request.tools:
            payload["tools"] = request.tools
        if request.response_schema:
            payload["response_format"] = {"type": "json_schema", "json_schema": request.response_schema}

        tracer = get_tracer()
        with tracer.span(
            "gen_ai.chat",
            kind="client",
            attributes=gen_ai_attributes(
                operation="chat",
                provider=self.name,
                model=payload["model"],
                extra={"gen_ai.request.temperature": request.temperature},
            ),
        ) as span:
            started = time.perf_counter()
            try:
                async with httpx.AsyncClient(timeout=request.timeout_seconds or self.timeout_seconds) as client:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers={"Authorization": f"Bearer {self._api_key}"},
                    )
            except httpx.HTTPError as exc:
                span.record_error(exc)
                raise ProviderUnavailable(f"{self.name} request failed: {exc}") from exc
            if response.status_code >= 400:
                span.record_error(f"HTTP {response.status_code}")
                raise ProviderError(
                    f"{self.name} returned HTTP {response.status_code}",
                    details={"body": response.text[:500]},
                )
            data = response.json()

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        structured = None
        if request.response_schema:
            try:
                structured = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ProviderError("provider returned text that is not valid JSON", details={"text": text[:500]}) from exc
        usage_payload = data.get("usage") or {}
        spec = next((item for item in self._models if item.model == payload["model"]), None)
        usage = Usage(
            input_tokens=int(usage_payload.get("prompt_tokens", 0)),
            output_tokens=int(usage_payload.get("completion_tokens", 0)),
            total_tokens=int(usage_payload.get("total_tokens", 0)),
            cost_usd=(
                (int(usage_payload.get("prompt_tokens", 0)) / 1000) * spec.cost_per_1k_input
                + (int(usage_payload.get("completion_tokens", 0)) / 1000) * spec.cost_per_1k_output
                if spec
                else 0.0
            ),
        )
        return CompletionResponse(
            text=text,
            model=data.get("model", payload["model"]),
            provider=self.name,
            finish_reason=choice.get("finish_reason"),
            usage=usage,
            tool_calls=message.get("tool_calls") or [],
            structured=structured,
            latency_ms=(time.perf_counter() - started) * 1000,
            raw={"id": data.get("id")},
        )

    async def health(self) -> ProviderHealth:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/models")
            return ProviderHealth(provider=self.name, healthy=response.status_code < 500, detail=f"HTTP {response.status_code}")
        except Exception as exc:
            return ProviderHealth(provider=self.name, healthy=False, detail=str(exc)[:200])


# --------------------------------------------------------------------------- registry


class ProviderRegistry:
    """Holds configured providers and their model catalogue."""

    def __init__(self) -> None:
        self._providers: dict[str, LLMProvider] = {}

    def register(self, provider: LLMProvider, *, replace: bool = False) -> None:
        if provider.name in self._providers and not replace:
            raise ProviderError(f"provider {provider.name!r} is already registered")
        self._providers[provider.name] = provider

    def get(self, name: str) -> LLMProvider:
        provider = self._providers.get(name)
        if provider is None:
            raise ModelNotConfigured(
                f"provider {name!r} is not configured",
                details={"configured": sorted(self._providers)},
            )
        return provider

    def providers(self) -> dict[str, LLMProvider]:
        return dict(self._providers)

    def models(self) -> list[ModelSpec]:
        return [spec for provider in self._providers.values() for spec in provider.models()]

    def find_model(self, model: str) -> tuple[LLMProvider, ModelSpec] | None:
        for provider in self._providers.values():
            for spec in provider.models():
                if spec.model == model:
                    return provider, spec
        return None

    async def health(self) -> list[ProviderHealth]:
        reports: list[ProviderHealth] = []
        for provider in self._providers.values():
            try:
                reports.append(await provider.health())
            except Exception as exc:
                reports.append(ProviderHealth(provider=provider.name, healthy=False, detail=str(exc)[:200]))
        return reports


_REGISTRY = ProviderRegistry()


def provider_registry() -> ProviderRegistry:
    return _REGISTRY


def bootstrap_default_providers() -> None:
    """Register the offline provider always; HTTP providers come from configuration."""
    _REGISTRY.register(EchoProvider(), replace=True)
    base_url = os.environ.get("DRILLAI_LLM_BASE_URL")
    if base_url:
        _REGISTRY.register(
            OpenAICompatibleProvider(
                base_url=base_url,
                api_key=os.environ.get("DRILLAI_LLM_API_KEY"),
                name=os.environ.get("DRILLAI_LLM_PROVIDER_NAME", "openai_compatible"),
            ),
            replace=True,
        )


bootstrap_default_providers()
