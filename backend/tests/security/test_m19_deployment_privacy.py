"""A running application cannot raise its bootstrap privacy limit through registry mutation."""
from __future__ import annotations

import pytest

from drillai.ai.providers import PrivacyClass, bootstrap_default_providers
from drillai.ai.router import LlmRouter, RoutingPolicy
from drillai.api.app import create_app
from drillai.core.config import Settings
from drillai.core.errors import ConfigurationError


def _settings():
    return Settings(_env_file=None, environment="test", auth_enabled=False,
                    database_url="sqlite+aiosqlite:///:memory:", blob_backend="memory",
                    llm_max_privacy="local")


def test_runtime_cannot_raise_bootstrap_ceiling_or_reset_it_by_clearing():
    app = create_app(_settings())
    registry = app.state.database.provider_registry
    with pytest.raises(ConfigurationError, match=r"deployment|ceiling"):
        registry.max_privacy = PrivacyClass.PUBLIC_CLOUD
    assert registry.max_privacy is PrivacyClass.LOCAL
    registry.clear()
    with pytest.raises(ConfigurationError, match=r"deployment|ceiling"):
        registry.max_privacy = PrivacyClass.PUBLIC_CLOUD
    assert registry.max_privacy is None


def test_rebootstrap_cannot_elevate_ceiling_or_destroy_existing_provider_configuration():
    app = create_app(_settings())
    registry = app.state.database.provider_registry
    before = registry.providers()
    elevated = _settings()
    elevated.llm_max_privacy = "public_cloud"
    with pytest.raises(ConfigurationError, match=r"deployment|ceiling"):
        bootstrap_default_providers(registry=registry, settings=elevated)
    assert registry.providers() == before
    assert registry.max_privacy is PrivacyClass.LOCAL


def test_cleared_application_registry_cannot_be_routed_using_explicit_policy():
    app = create_app(_settings())
    registry = app.state.database.provider_registry
    registry.clear()
    with pytest.raises(ConfigurationError, match=r"ceiling"):
        LlmRouter(registry=registry, policy=RoutingPolicy(max_privacy=PrivacyClass.PUBLIC_CLOUD))


async def test_chat_rejects_public_provider_with_falsely_local_model_before_send():
    from drillai.ai.providers import ChatMessage, CompletionRequest, LLMCapability, ModelSpec
    from drillai.core.errors import ModelNotConfigured

    class PublicProvider:
        name = "public-provider"
        privacy = PrivacyClass.PUBLIC_CLOUD
        called = False

        def models(self):
            return [ModelSpec(provider=self.name, model="claimed-local", privacy=PrivacyClass.LOCAL,
                              capabilities=frozenset({LLMCapability.CHAT}))]

        async def complete(self, request):
            self.called = True
            raise AssertionError("protected content reached a public provider")

    app = create_app(_settings())
    registry = app.state.database.provider_registry
    registry.clear()
    registry.max_privacy = PrivacyClass.LOCAL
    provider = PublicProvider()
    registry.register(provider)
    try:
        async with app.state.database.session() as session:
            with pytest.raises(ModelNotConfigured):
                await LlmRouter(registry=registry).complete(
                    CompletionRequest(messages=[ChatMessage(role="user", content="private report")]),
                    session=session,
                )
        assert not provider.called
    finally:
        await app.state.database.dispose()
