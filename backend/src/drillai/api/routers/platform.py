"""Platform-level surfaces: model providers, agents, skills, and the capability summary.

These endpoints exist to make the replaceable components *visible*: which LLM providers are
configured, which model each task profile routes to and why, which agents and skills are
registered and what they are allowed to touch, and what the deployment's integrations currently
resolve to. Every answer is computed from the live registries and settings — none of it is a
static document that can drift from the code.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends

from drillai.api.deps import AuthContext, require
from drillai.core.config import get_settings

router = APIRouter(tags=["platform"])


@router.get("/platform/capabilities", summary="What this deployment can do, resolved live")
async def capabilities(
    _: Annotated[AuthContext, Depends(require("registry.read"))],
) -> dict[str, Any]:
    from drillai.ai.agents import agent_catalogue
    from drillai.ai.router import TaskProfile
    from drillai.ai.tools import registered_tools
    from drillai.context.builder import registered_section_keys
    from drillai.engines.registry import load_default_engines
    from drillai.ingestion.extractors import extractor_catalogue
    from drillai.security.actions import registered_actions
    from drillai.workflow.nodes import registered_node_types

    settings = get_settings()
    engines = load_default_engines()
    by_category: dict[str, int] = {}
    for engine in engines:
        by_category[engine.spec.category] = by_category.get(engine.spec.category, 0) + 1
    return {
        "engines": {"total": len(engines), "by_category": by_category},
        "node_types": len(registered_node_types()),
        "tools": len(registered_tools()),
        "actions": len(registered_actions()),
        "context_sections": len(registered_section_keys()),
        "extractors": len(extractor_catalogue()),
        "agents": len(agent_catalogue()["agents"]),
        "skills": len(agent_catalogue()["skills"]),
        "task_profiles": [profile.value for profile in TaskProfile],
        "integrations": {
            "llm_provider": settings.llm_provider,
            "embedding_provider": settings.embedding_provider,
            "vector_backend": settings.vector_backend,
            "blob_backend": settings.blob_backend,
            "email_provider": settings.email_provider,
            "telegram_provider": settings.telegram_provider,
            "scheduler_enabled": settings.scheduler_enabled,
        },
        "authorization": {
            "auth_enabled": settings.auth_enabled,
            "unknown_action_level": "L4",
            "automation_default": "off (L5 requires an org envelope)",
        },
    }


#: Roles the development identity switch may assume. Only meaningful when authentication is disabled;
#: the backend still evaluates every request against the resulting principal.
DEV_ROLE_PRESETS: tuple[str, ...] = (
    "viewer",
    "engineer",
    "drilling_supervisor",
    "integrity_engineer",
    "well_manager",
    "data_manager",
    "auditor",
    "admin",
)


@router.get("/platform/identity", summary="Who the caller is, and what they may do")
async def identity(
    auth: Annotated[AuthContext, Depends(require("well.read"))],
) -> dict[str, Any]:
    """The caller's principal as the platform resolved it.

    A user interface has to know its own authorization context to render affordances honestly
    (a publish button that always fails is worse than no button), but it must never *decide*
    authorization: this endpoint reports what the server already computed, and every request is
    still checked server-side. No credential material is returned — only the permission patterns
    and the action-level ceiling.
    """
    from drillai.security.rbac import ROLE_CATALOGUE

    settings = get_settings()
    principal = auth.principal
    return {
        "principal_id": principal.id,
        "principal_kind": getattr(principal, "kind", "user"),
        "org_id": auth.org_id,
        "role_keys": list(principal.role_keys),
        "roles": [
            {
                "key": role.key,
                "name": role.name,
                "description": role.description,
                "max_action_level": role.max_action_level.value,
            }
            for role in ROLE_CATALOGUE
            if role.key in principal.role_keys
        ],
        "available_roles": [
            {
                "key": role.key,
                "name": role.name,
                "description": role.description,
                "max_action_level": role.max_action_level.value,
                "permissions": list(role.permissions),
            }
            for role in ROLE_CATALOGUE
        ],
        "permissions": sorted(principal.permissions),
        "max_action_level": principal.max_action_level.value,
        "auth_enabled": settings.auth_enabled,
        "identity_source": "bearer_token" if settings.auth_enabled else "development_header",
        "development_presets": list(DEV_ROLE_PRESETS),
        "locale": auth.locale,
        "note": (
            "Permissions are patterns (for example 'well.*'). The server evaluates them on every "
            "request; the UI uses them only to avoid offering actions that will be refused."
        ),
    }


@router.get("/platform/providers", summary="LLM providers and models")
async def providers(
    _: Annotated[AuthContext, Depends(require("registry.read"))],
) -> dict[str, Any]:
    from drillai.ai.providers import provider_registry

    registry = provider_registry()
    providers = registry.providers()
    return {
        "items": [
            {
                "name": name,
                "privacy_class": provider.privacy.value,
                "models": [
                    {
                        "model": spec.model,
                        "provider": spec.provider,
                        "capabilities": [capability.value for capability in spec.capabilities],
                        "context_window_tokens": spec.context_window_tokens,
                        "max_output_tokens": spec.max_output_tokens,
                        "cost_per_1k_input": spec.cost_per_1k_input,
                        "cost_per_1k_output": spec.cost_per_1k_output,
                        "typical_latency_ms": spec.typical_latency_ms,
                        "privacy_class": spec.privacy.value,
                        "notes": spec.notes,
                    }
                    for spec in provider.models()
                ],
            }
            for name, provider in providers.items()
        ],
        "total": len(providers),
        "configured_provider": get_settings().llm_provider,
    }


@router.get("/platform/providers/routing", summary="How each task profile routes today")
async def provider_routing(
    _: Annotated[AuthContext, Depends(require("registry.read"))],
) -> dict[str, Any]:
    """Show the routing decision per task profile.

    Routing is policy-driven (capability, cost, latency, privacy), so the answer depends on the
    configured providers — this endpoint reports the decision the router would actually make, not
    the policy nobody runs.
    """
    from drillai.ai.providers import provider_registry
    from drillai.ai.router import LlmRouter, RoutingPolicy, TaskProfile

    registry = provider_registry()
    router_ = LlmRouter(registry=registry, policy=RoutingPolicy())
    items = []
    for profile in TaskProfile:
        try:
            decision = router_.route(profile=profile)
            items.append(
                {
                    "profile": profile.value,
                    "provider": decision.provider.name,
                    "model": decision.spec.model,
                    "rationale": list(decision.rationale),
                    "cost_per_1k_input": decision.spec.cost_per_1k_input,
                    "privacy_class": decision.spec.privacy.value,
                    "considered": list(decision.considered),
                }
            )
        except Exception as exc:  # a profile with no capable provider is reported, not hidden
            items.append({"profile": profile.value, "error": str(exc)})
    return {"items": items, "total": len(items)}


@router.get("/platform/agents", summary="Agent and skill registry")
async def agents(
    _: Annotated[AuthContext, Depends(require("registry.read"))],
) -> dict[str, Any]:
    from drillai.ai.agents import agent_catalogue, install_default_agents

    install_default_agents()
    return agent_catalogue()


@router.get("/platform/agents/{agent_key}", summary="One agent, with resolved capabilities")
async def agent_detail(
    agent_key: str,
    _: Annotated[AuthContext, Depends(require("registry.read"))],
) -> dict[str, Any]:
    from drillai.ai.agents import agent_spec, install_default_agents
    from drillai.ai.tools import registered_tools
    from drillai.engines.registry import load_default_engines
    from drillai.security.actions import registered_actions

    install_default_agents()
    spec = agent_spec(agent_key)
    tools = registered_tools()
    engines = {engine.spec.key: engine.spec for engine in load_default_engines()}
    actions = registered_actions()
    return {
        "agent": spec.describe(),
        "resolved": {
            "tools": [
                {
                    "key": key,
                    "description": tools[key].description,
                    "action_level": tools[key].action_level.value,
                    "permission": tools[key].resolved_permission,
                }
                for key in spec.tools
                if key in tools
            ],
            "engines": [
                {
                    "key": key,
                    "version": engines[key].version,
                    "action_level": str(engines[key].action_level),
                    "limitations": list(engines[key].limitations),
                }
                for key in spec.engines
                if key in engines
            ],
            "actions": [
                {
                    "key": key,
                    "level": action.level.value,
                    "permission": action.permission,
                }
                for key, action in sorted(actions.items())
                if key.startswith("tool.") and key.split(".", 1)[1] in spec.tools
            ],
        },
    }


@router.get("/platform/integrations", summary="Integration status (no secrets, no fake health)")
async def integrations(
    _: Annotated[AuthContext, Depends(require("registry.read"))],
) -> dict[str, Any]:
    """Report each outbound integration's *configuration state*, and whether it is enabled.

    No provider is contacted here: a status endpoint that makes outbound calls reports its own
    timeouts as integration failures. Reachability is a separate, explicit action.
    """
    settings = get_settings()
    return {
        "items": [
            {
                "integration": "llm",
                "provider": settings.llm_provider,
                "enabled": settings.llm_provider != "stub",
                "configured": settings.llm_provider != "stub" and bool(settings.llm_base_url),
                "note": "the stub provider answers deterministically from supplied context; it does not call a model",
            },
            {
                "integration": "embeddings",
                "provider": settings.embedding_provider,
                "enabled": settings.embedding_provider != "deterministic",
                "configured": True,
                "note": "deterministic embeddings are reproducible but not semantic",
            },
            {
                "integration": "vector_store",
                "provider": settings.vector_backend,
                "enabled": settings.vector_backend != "memory",
                "configured": True,
            },
            {
                "integration": "email",
                "provider": settings.email_provider,
                "enabled": settings.email_provider != "disabled",
                "configured": settings.email_provider == "disabled" or bool(settings.smtp_host),
            },
            {
                "integration": "telegram",
                "provider": settings.telegram_provider,
                "enabled": settings.telegram_provider != "disabled",
                "configured": settings.telegram_provider == "disabled" or bool(settings.telegram_bot_token),
            },
            {
                "integration": "witsml",
                "provider": "witsml" if settings.witsml_connector_enabled else "disabled",
                "enabled": settings.witsml_connector_enabled,
                "configured": settings.witsml_connector_enabled,
            },
            {
                "integration": "etp",
                "provider": "etp" if settings.etp_connector_enabled else "disabled",
                "enabled": settings.etp_connector_enabled,
                "configured": settings.etp_connector_enabled,
            },
        ]
    }
