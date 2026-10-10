"""LLM layer: provider abstraction, routing, guardrails and the tool registry.

Nothing outside this package may import a vendor SDK or name a model. Consumers ask for a
capability (structured output, tools, long context) and a privacy class; the router decides.
"""

from drillai.ai.guardrails import (
    GuardrailFinding,
    GuardrailReport,
    check_no_authority,
    evaluate_output,
    ground_numbers,
    require_citations,
    scan_for_injection,
    validate_structured,
)
from drillai.ai.providers import (
    ChatMessage,
    CompletionRequest,
    CompletionResponse,
    EchoProvider,
    LLMCapability,
    LLMProvider,
    ModelSpec,
    OpenAICompatibleProvider,
    PrivacyClass,
    ProviderRegistry,
    Usage,
    bootstrap_default_providers,
    provider_registry,
)
from drillai.ai.router import LlmRouter, RouteDecision, RoutingPolicy, TaskProfile
from drillai.ai.tools import (
    ToolContext,
    ToolResult,
    ToolSpec,
    install_default_tools,
    invoke_tool,
    register_tool,
    registered_tools,
    tool_catalogue,
    tool_schemas_for_llm,
)

__all__ = [
    "ChatMessage",
    "CompletionRequest",
    "CompletionResponse",
    "EchoProvider",
    "GuardrailFinding",
    "GuardrailReport",
    "LLMCapability",
    "LLMProvider",
    "LlmRouter",
    "ModelSpec",
    "OpenAICompatibleProvider",
    "PrivacyClass",
    "ProviderRegistry",
    "RouteDecision",
    "RoutingPolicy",
    "TaskProfile",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "bootstrap_default_providers",
    "check_no_authority",
    "evaluate_output",
    "ground_numbers",
    "install_default_tools",
    "invoke_tool",
    "provider_registry",
    "register_tool",
    "registered_tools",
    "require_citations",
    "scan_for_injection",
    "tool_catalogue",
    "tool_schemas_for_llm",
    "validate_structured",
]
