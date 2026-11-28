"""Application-owned AI providers must not be replaced by another application."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from drillai.api.app import create_app
from drillai.core.config import Settings
from drillai.core.errors import ConfigurationError


def _config(name: str) -> Settings:
    return Settings(
        environment="test",
        auth_enabled=False,
        database_url="sqlite+aiosqlite:///:memory:",
        blob_backend="memory",
        llm_provider="stub",
        llm_default_model=f"{name}-model",
        embedding_model=f"{name}-embedding",
    )


@pytest.mark.parametrize("order", [("alpha", "beta"), ("beta", "alpha")])
async def test_provider_endpoints_are_isolated_across_apps_and_creation_order(order):
    apps = {name: create_app(_config(name)) for name in order}
    clients = {name: httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") for name, app in apps.items()}
    try:
        for app in apps.values():
            await app.state.database.create_all()
        # Initialize the development organization before parallel requests.
        for client in clients.values():
            assert (await client.get("/api/v1/platform/providers")).status_code == 200

        async def check(name):
            for _ in range(3):
                providers = await clients[name].get("/api/v1/platform/providers")
                assert providers.status_code == 200
                models = {model["model"] for provider in providers.json()["items"] for model in provider["models"]}
                assert f"{name}-model" in models
                assert f"{name}-embedding" in models
                other = "beta" if name == "alpha" else "alpha"
                assert f"{other}-model" not in models
                routing = await clients[name].get("/api/v1/platform/providers/routing")
                assert routing.status_code == 200
                chat = next(item for item in routing.json()["items"] if item["profile"] == "chat")
                assert chat["model"] == f"{name}-model"
                await asyncio.sleep(0)

        results = await asyncio.gather(*(check(name) for name in order), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
    finally:
        for client in clients.values():
            await client.aclose()
        for app in apps.values():
            await app.state.database.dispose()


async def test_session_registry_fails_closed_without_application_owner():
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from drillai.ai.providers import registry_for_session

    app = create_app(_config("alpha"))
    try:
        async with app.state.database.session() as session:
            registry = registry_for_session(session)
            assert any(model.model == "alpha-model" for model in registry.models())
        factory = async_sessionmaker(app.state.database.engine)
        async with factory() as unowned:
            with pytest.raises(ConfigurationError):
                registry_for_session(unowned)
    finally:
        await app.state.database.dispose()


async def test_workflow_nodes_use_and_audit_the_owning_app_provider():
    from sqlalchemy import select

    from drillai.db.models import LlmCall, Organization
    from drillai.security.actions import ActionLevel, Principal
    from drillai.workflow.nodes import AiCompleteConfig, NodeContext, _ai_complete

    apps = {name: create_app(_config(name)) for name in ("alpha", "beta")}
    try:
        for app in apps.values():
            await app.state.database.create_all()
            async with app.state.database.session() as session:
                session.add(Organization(id="org_shared", slug="shared", name="Shared"))

        async def execute(name):
            async with apps[name].state.database.session() as session:
                context = NodeContext(
                    session=session, org_id="org_shared",
                    principal=Principal(id="test-user", max_action_level=ActionLevel.OBSERVE),
                    run_id=f"run-{name}", node_id="ai", node_name="AI", inputs={},
                    variables={}, node_outputs={}, services={},
                )
                result = await _ai_complete(context, AiCompleteConfig(user_prompt="Summarize the available report."))
                assert result.outputs["model"] == f"{name}-model"
            async with apps[name].state.database.session() as session:
                calls = (await session.execute(select(LlmCall))).scalars().all()
                assert len(calls) == 1 and calls[0].model == f"{name}-model"

        await asyncio.gather(execute("alpha"), execute("beta"))
    finally:
        for app in apps.values():
            await app.state.database.dispose()


async def test_semantic_retrieval_uses_session_owned_embedding_model():
    import hashlib
    from types import SimpleNamespace

    from drillai.ai.providers import LLMCapability
    from drillai.rag.retriever import _embedding_space_id, _semantic_scores_with_backend

    apps = {name: create_app(_config(name)) for name in ("alpha", "beta")}
    try:
        chunks = {}
        for name, app in apps.items():
            registry = app.state.database.provider_registry
            provider = next(item for item in registry.providers().values() if item.supports(LLMCapability.EMBEDDING))
            text = "Directional drilling pressure profile"
            vector = (await provider.embed([text]))[0]
            chunks[name] = SimpleNamespace(
                id=f"chunk-{name}", text=text, embedding=vector, embedding_dim=len(vector),
                embedding_model=_embedding_space_id(provider),
                embedding_content_hash=hashlib.sha256(text.encode()).hexdigest(),
            )
        async def check(name, other):
            async with apps[name].state.database.session() as session:
                scores, backend, _ = await _semantic_scores_with_backend(session, [chunks[name]], chunks[name].text)
                assert backend == "python_cosine" and scores[0] == pytest.approx(1.0)
                _, reason, _ = await _semantic_scores_with_backend(session, [chunks[other]], chunks[other].text)
                assert reason == "incompatible_model"
        await asyncio.gather(check("alpha", "beta"), check("beta", "alpha"))
    finally:
        for app in apps.values():
            await app.state.database.dispose()
