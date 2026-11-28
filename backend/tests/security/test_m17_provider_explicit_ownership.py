"""A provider registry and privacy policy must never be inherited from process globals."""
from __future__ import annotations

import pytest

from drillai.ai.providers import PrivacyClass, ProviderRegistry, bootstrap_default_providers
from drillai.ai.router import LlmRouter, RoutingPolicy
from drillai.core.config import Settings
from drillai.core.errors import ConfigurationError


def test_router_without_explicit_registry_fails_closed():
    with pytest.raises(ConfigurationError):
        LlmRouter()


def test_router_without_privacy_policy_or_registry_ceiling_fails_closed():
    with pytest.raises(ConfigurationError):
        LlmRouter(registry=ProviderRegistry())


def test_explicit_routing_policy_cannot_weaken_owning_app_privacy_ceiling():
    registry = ProviderRegistry()
    bootstrap_default_providers(
        registry=registry,
        settings=Settings(_env_file=None, environment="test", llm_max_privacy="local"),
    )
    with pytest.raises(ConfigurationError):
        LlmRouter(registry=registry, policy=RoutingPolicy(max_privacy=PrivacyClass.PUBLIC_CLOUD))
    assert LlmRouter(registry=registry, policy=RoutingPolicy(max_privacy=PrivacyClass.LOCAL)).policy.max_privacy is PrivacyClass.LOCAL


def test_bootstrap_requires_explicit_owner_and_settings():
    with pytest.raises((ConfigurationError, TypeError)):
        bootstrap_default_providers()
    with pytest.raises((ConfigurationError, TypeError)):
        bootstrap_default_providers(registry=ProviderRegistry())

def test_registry_ceiling_is_enforced_after_router_creation():
    from drillai.ai.router import TaskProfile
    from drillai.core.errors import ModelNotConfigured

    registry = ProviderRegistry()
    bootstrap_default_providers(
        registry=registry,
        settings=Settings(
            _env_file=None, environment="test", llm_provider="openai_compatible",
            llm_privacy="public_cloud", llm_max_privacy="public_cloud",
        ),
    )
    router = LlmRouter(registry=registry)
    assert router.route(profile=TaskProfile.CHAT).provider.name == "openai_compatible"
    registry.max_privacy = PrivacyClass.LOCAL
    with pytest.raises(ModelNotConfigured):
        router.route(profile=TaskProfile.CHAT)
    registry.max_privacy = None
    with pytest.raises(ConfigurationError):
        router.route(profile=TaskProfile.CHAT)


def test_bootstrap_rejects_unowned_or_invalid_configuration():
    settings = Settings(_env_file=None, environment="test")
    with pytest.raises(ConfigurationError):
        bootstrap_default_providers(registry=None, settings=settings)
    with pytest.raises(ConfigurationError):
        bootstrap_default_providers(registry=ProviderRegistry(), settings=None)


def test_no_legacy_process_global_provider_api():
    import drillai.ai as public_api
    import drillai.ai.providers as providers

    assert not hasattr(providers, "provider_registry")
    assert not hasattr(providers, "_REGISTRY")
    assert not hasattr(public_api, "provider_registry")
