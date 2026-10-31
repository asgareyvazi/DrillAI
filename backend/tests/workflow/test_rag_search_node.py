"""Workflow RAG nodes must preserve the run's authorization scope."""

from __future__ import annotations

import pytest

from drillai.rag.retriever import RetrievalResult
from drillai.security.actions import ActionLevel, Principal
from drillai.workflow.nodes import NodeContext, RagSearchConfig, _rag_search


@pytest.mark.asyncio
async def test_project_only_workflow_context_passes_project_scope_to_retrieval(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_retrieve(session, org_id, query):
        captured.update(session=session, org_id=org_id, query=query)
        return RetrievalResult(hits=[], mode_used="lexical", filters_applied={"project_id": query.project_id})

    monkeypatch.setattr("drillai.rag.retriever.retrieve", fake_retrieve)
    context = NodeContext(
        session=None,
        org_id="org_test",
        principal=Principal(id="usr_test", max_action_level=ActionLevel.OBSERVE),
        run_id="run_project_only_rag",
        node_id="rag_search",
        node_name="RAG search",
        inputs={},
        variables={},
        node_outputs={},
        project_id="project_test",
    )

    outcome = await _rag_search(context, RagSearchConfig(query="pressure trend", mode="lexical"))

    query = captured["query"]
    assert captured["session"] is None
    assert captured["org_id"] == "org_test"
    assert query.project_id == "project_test"
    assert query.well_id is None
    assert outcome.outputs["hit_count"] == 0
    assert outcome.outputs["filters"]["project_id"] == "project_test"
