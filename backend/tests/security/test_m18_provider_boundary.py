"""Adversarial checks for cross-application routing and model selection."""
from __future__ import annotations

import pytest

from drillai.ai.providers import ChatMessage, CompletionRequest, CompletionResponse, Usage
from drillai.ai.router import LlmRouter
from drillai.api.app import create_app
from drillai.core.config import Settings
from drillai.core.errors import ConfigurationError
from drillai.security.actions import ActionLevel, Principal
from drillai.workflow.nodes import AiCompleteConfig, NodeContext, _ai_complete


def _settings(name: str) -> Settings:
    return Settings(_env_file=None, environment="test", auth_enabled=False,
                    database_url="sqlite+aiosqlite:///:memory:", blob_backend="memory",
                    llm_provider="stub", llm_default_model=f"{name}-model")


def _request(**kwargs):
    return CompletionRequest(messages=[ChatMessage(role="user", content="Summarize the report.")], **kwargs)


@pytest.mark.parametrize("order", [("alpha", "beta"), ("beta", "alpha")])
@pytest.mark.parametrize("path", ["direct", "workflow"])
async def test_foreign_router_denied_before_direct_and_workflow_calls(order, path, monkeypatch):
    apps = {name: create_app(_settings(name)) for name in order}
    a, b = apps[order[0]], apps[order[1]]
    calls = []
    router = LlmRouter(registry=b.state.database.provider_registry)
    provider = router.registry.get("echo")
    original = provider.complete

    async def record(request):
        calls.append(request)
        return await original(request)

    monkeypatch.setattr(provider, "complete", record)
    try:
        await a.state.database.create_all()
        from drillai.db.models import Organization
        async with a.state.database.session() as session:
            session.add(Organization(id="org_test", slug="test", name="Test"))
        async with a.state.database.session() as session:
            with pytest.raises(ConfigurationError, match=r"owner|registry"):
                if path == "direct":
                    await router.complete(_request(), session=session, org_id="org_test")
                else:
                    context = NodeContext(
                        session=session, org_id="org_test", principal=Principal(id="user", max_action_level=ActionLevel.OBSERVE),
                        run_id="run", node_id="ai", node_name="AI", inputs={}, variables={}, node_outputs={},
                        services={"llm_router": router},
                    )
                    await _ai_complete(context, AiCompleteConfig(user_prompt="Summarize the report."))
        assert calls == []
    finally:
        for app in apps.values():
            await app.state.database.dispose()


async def test_unregistered_requested_model_is_not_sent_to_provider(monkeypatch):
    app = create_app(_settings("alpha"))
    router = LlmRouter(registry=app.state.database.provider_registry)
    calls = []
    provider = router.registry.get("echo")
    async def record(request):
        calls.append(request)
        return CompletionResponse(text="ok", model=request.model, provider="echo", usage=Usage())
    monkeypatch.setattr(provider, "complete", record)
    try:
        async with app.state.database.session() as session:
            with pytest.raises(ConfigurationError, match="model"):
                await router.complete(_request(model="not-registered"), session=session)
        assert calls == []
    finally:
        await app.state.database.dispose()


async def test_privacy_change_during_structured_retry_stops_second_call(monkeypatch):
    app = create_app(_settings("alpha"))
    registry = app.state.database.provider_registry
    router = LlmRouter(registry=registry)
    provider = registry.get("echo")
    calls = []
    async def record(request):
        calls.append(request)
        registry.max_privacy = None
        return CompletionResponse(text="not json", model=request.model, provider="echo", usage=Usage())
    monkeypatch.setattr(provider, "complete", record)
    try:
        async with app.state.database.session() as session:
            with pytest.raises(ConfigurationError, match=r"privacy|ceiling"):
                await router.complete(_request(response_schema={"type": "object"}), session=session)
        assert len(calls) == 1
    finally:
        await app.state.database.dispose()


async def test_router_rejects_absent_and_unowned_sessions():
    from sqlalchemy.ext.asyncio import async_sessionmaker

    app = create_app(_settings("alpha"))
    router = LlmRouter(registry=app.state.database.provider_registry)
    try:
        with pytest.raises(ConfigurationError, match=r"owner|registry"):
            await router.complete(_request())
        async with async_sessionmaker(app.state.database.engine)() as session:
            with pytest.raises(ConfigurationError, match=r"owner|registry"):
                await router.complete(_request(), session=session)
    finally:
        await app.state.database.dispose()


async def test_unowned_injected_workflow_router_cannot_bypass_boundary():
    app = create_app(_settings("alpha"))
    class Impostor:
        called = False
        async def complete(self, request, **kwargs):
            self.called = True
            return CompletionResponse(text="ok", model="x", provider="x", usage=Usage()), None
    impostor = Impostor()
    try:
        async with app.state.database.session() as session:
            context = NodeContext(
                session=session, org_id="org_test", principal=Principal(id="user", max_action_level=ActionLevel.OBSERVE),
                run_id="run", node_id="ai", node_name="AI", inputs={}, variables={}, node_outputs={},
                services={"llm_router": impostor},
            )
            with pytest.raises(ConfigurationError, match=r"router|owner"):
                await _ai_complete(context, AiCompleteConfig(user_prompt="Summarize the report."))
            assert not impostor.called
    finally:
        await app.state.database.dispose()


async def test_advisor_rejects_foreign_router_before_narration():
    import datetime as dt

    from drillai.drilling.advisor import AdvisorAnswer, Fact, OperationsAdvisor

    a, b = create_app(_settings("alpha")), create_app(_settings("beta"))
    try:
        async with a.state.database.session() as session:
            answer = AdvisorAnswer(question="where_are_we", well_id="well", generated_at=dt.datetime.now(dt.UTC),
                                   facts=[Fact(key="depth", statement="Depth is 100 m.", value=100,
                                               unit="m", source="report", source_id="report")])
            advisor = OperationsAdvisor(session, "org", llm_router=LlmRouter(registry=b.state.database.provider_registry))
            with pytest.raises(ConfigurationError, match=r"owner|registry"):
                await advisor._narrate(answer)
    finally:
        await a.state.database.dispose()
        await b.state.database.dispose()


async def test_advisor_rejects_unowned_injected_router_before_narration():
    import datetime as dt

    from drillai.drilling.advisor import AdvisorAnswer, Fact, OperationsAdvisor

    class Impostor:
        called = False
        async def complete(self, request, **kwargs):
            self.called = True
            return CompletionResponse(text="Depth is 100 m.", model="x", provider="x", usage=Usage()), None

    app = create_app(_settings("alpha"))
    impostor = Impostor()
    try:
        async with app.state.database.session() as session:
            answer = AdvisorAnswer(question="where_are_we", well_id="well", generated_at=dt.datetime.now(dt.UTC),
                                   facts=[Fact(key="depth", statement="Depth is 100 m.", value=100,
                                               unit="m", source="report", source_id="report")])
            advisor = OperationsAdvisor(session, "org", llm_router=impostor)
            with pytest.raises(ConfigurationError, match=r"router|owner"):
                await advisor._narrate(answer)
            assert not impostor.called
    finally:
        await app.state.database.dispose()


async def test_shutdown_invalidates_old_registry_and_recreated_app_is_independent():
    from drillai.ai.providers import registry_for_session
    from drillai.ai.router import TaskProfile

    app = create_app(_settings("alpha"))
    async with app.router.lifespan_context(app):
        session = app.state.database.session_factory()
        old_registry = registry_for_session(session)
        old_router = LlmRouter(registry=old_registry)
        assert old_router.route(profile=TaskProfile.CHAT).spec.model == "alpha-model"
    try:
        with pytest.raises(ConfigurationError, match=r"closed|shutdown"):
            registry_for_session(session)
        with pytest.raises(ConfigurationError, match=r"closed|shutdown"):
            old_router.route(profile=TaskProfile.CHAT)
        replacement = create_app(_settings("beta"))
        try:
            assert replacement.state.database.provider_registry is not old_registry
            assert LlmRouter(registry=replacement.state.database.provider_registry).route(profile=TaskProfile.CHAT).spec.model == "beta-model"
        finally:
            await replacement.state.database.dispose()
    finally:
        await session.close()


async def test_provider_replacement_during_retry_is_rejected(monkeypatch):
    from drillai.ai.providers import EchoProvider

    app = create_app(_settings("alpha"))
    registry = app.state.database.provider_registry
    router = LlmRouter(registry=registry)
    old = registry.get("echo")
    calls = []

    async def record(request):
        calls.append(request)
        registry.register(EchoProvider(model="replacement"), replace=True)
        return CompletionResponse(text="not json", model=request.model, provider="echo", usage=Usage())

    monkeypatch.setattr(old, "complete", record)
    try:
        async with app.state.database.session() as session:
            with pytest.raises(ConfigurationError, match="routing changed"):
                await router.complete(_request(response_schema={"type": "object"}), session=session)
        assert len(calls) == 1
    finally:
        await app.state.database.dispose()
