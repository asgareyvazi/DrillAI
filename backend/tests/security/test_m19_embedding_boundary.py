"""Embedding calls must enforce application ownership and privacy before sending text."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from drillai.ai.providers import LLMCapability, ModelSpec, PrivacyClass
from drillai.api.app import create_app
from drillai.core.config import Settings
from drillai.core.errors import ConfigurationError
from drillai.rag.retriever import _embedding_space_id, _semantic_scores_with_backend, index_chunk_embeddings


class RecordingEmbedding:
    name = "recording-embedding"
    model_name = "recording-model"

    def __init__(self, privacy: PrivacyClass):
        self.privacy = privacy
        self.calls: list[list[str]] = []

    def supports(self, capability):
        return capability == LLMCapability.EMBEDDING

    def models(self):
        return [ModelSpec(provider=self.name, model=self.model_name,
                          capabilities=frozenset({LLMCapability.EMBEDDING}), privacy=self.privacy)]

    async def embed(self, texts):
        self.calls.append(texts)
        return [[1.0, 0.0] for _ in texts]


def _app():
    return create_app(Settings(_env_file=None, environment="test", auth_enabled=False,
                               database_url="sqlite+aiosqlite:///:memory:", blob_backend="memory",
                               llm_max_privacy="local"))


def _chunk(provider):
    text = "Confidential drilling report"
    return SimpleNamespace(id="chunk", org_id="org-a", text=text, embedding=[1.0, 0.0], embedding_dim=2,
                           embedding_model=_embedding_space_id(provider),
                           embedding_content_hash=hashlib.sha256(text.encode()).hexdigest())


async def test_index_rejects_foreign_explicit_embedding_provider_before_sending():
    app = _app()
    foreign = RecordingEmbedding(PrivacyClass.LOCAL)
    try:
        async with app.state.database.session() as session:
            with pytest.raises(ConfigurationError, match=r"owner|registry"):
                await index_chunk_embeddings(session, [_chunk(foreign)], provider=foreign)
        assert foreign.calls == []
    finally:
        await app.state.database.dispose()


@pytest.mark.parametrize("operation", ["index", "query"])
@pytest.mark.parametrize("ceiling", [PrivacyClass.LOCAL, None])
async def test_disallowed_or_missing_ceiling_never_sends_embedding_text(operation, ceiling):
    app = _app()
    provider = RecordingEmbedding(PrivacyClass.PUBLIC_CLOUD)
    registry = app.state.database.provider_registry
    registry.clear()
    registry.max_privacy = ceiling
    registry.register(provider)
    try:
        async with app.state.database.session() as session:
            with pytest.raises(ConfigurationError, match=r"privacy|ceiling"):
                if operation == "index":
                    await index_chunk_embeddings(session, [_chunk(provider)])
                else:
                    await _semantic_scores_with_backend(session, [_chunk(provider)], "confidential query")
        assert provider.calls == []
    finally:
        await app.state.database.dispose()


async def test_owned_local_embedding_still_indexes_and_searches():
    app = _app()
    provider = RecordingEmbedding(PrivacyClass.LOCAL)
    registry = app.state.database.provider_registry
    registry.clear()
    registry.max_privacy = PrivacyClass.LOCAL
    registry.register(provider)
    chunk = _chunk(provider)
    try:
        async with app.state.database.session() as session:
            assert await index_chunk_embeddings(session, [chunk], provider=provider) == 1
            scores, backend, identities = await _semantic_scores_with_backend(session, [chunk], "query")
            assert scores == pytest.approx([1.0])
            assert backend == "python_cosine"
            assert identities == {"chunk"}
        assert len(provider.calls) == 2
    finally:
        await app.state.database.dispose()


@pytest.mark.parametrize("operation", ["index", "query"])
async def test_embedding_privacy_change_during_call_rejects_the_result(operation):
    app = _app()
    registry = app.state.database.provider_registry
    provider = RecordingEmbedding(PrivacyClass.LOCAL)
    registry.clear()
    registry.max_privacy = PrivacyClass.LOCAL
    registry.register(provider)
    chunk = _chunk(provider)
    original_embedding = chunk.embedding

    async def encode(texts):
        provider.calls.append(texts)
        registry.max_privacy = None
        return [[0.0, 1.0]]

    provider.embed = encode
    try:
        async with app.state.database.session() as session:
            with pytest.raises(ConfigurationError, match="privacy"):
                if operation == "index":
                    await index_chunk_embeddings(session, [chunk], provider=provider)
                else:
                    await _semantic_scores_with_backend(session, [chunk], "query")
        assert chunk.embedding is original_embedding
        assert len(provider.calls) == 1
    finally:
        await app.state.database.dispose()


@pytest.mark.parametrize("operation", ["index", "query"])
async def test_embedding_model_change_during_call_cannot_persist_or_score_wrong_space(operation):
    app = _app()
    registry = app.state.database.provider_registry
    provider = RecordingEmbedding(PrivacyClass.LOCAL)
    registry.clear()
    registry.max_privacy = PrivacyClass.LOCAL
    registry.register(provider)
    chunk = _chunk(provider)
    original_embedding = chunk.embedding

    async def encode(texts):
        provider.calls.append(texts)
        provider.model_name = "different-model"
        return [[0.0, 1.0]]

    provider.embed = encode
    try:
        async with app.state.database.session() as session:
            with pytest.raises(ConfigurationError, match="changed"):
                if operation == "index":
                    await index_chunk_embeddings(session, [chunk], provider=provider)
                else:
                    await _semantic_scores_with_backend(session, [chunk], "query")
        assert chunk.embedding is original_embedding
    finally:
        await app.state.database.dispose()


async def test_index_rejects_mixed_tenant_batch_before_sending_text():
    app = _app()
    registry = app.state.database.provider_registry
    provider = RecordingEmbedding(PrivacyClass.LOCAL)
    registry.clear()
    registry.max_privacy = PrivacyClass.LOCAL
    registry.register(provider)
    first, second = _chunk(provider), _chunk(provider)
    second.org_id = "org-b"
    try:
        async with app.state.database.session() as session:
            with pytest.raises(ConfigurationError, match="tenant"):
                await index_chunk_embeddings(session, [first, second], provider=provider)
        assert provider.calls == []
    finally:
        await app.state.database.dispose()
