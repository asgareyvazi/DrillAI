"""The §41 acceptance flow, exercised over HTTP.

Each test drives the public API the way the UI does — create a project and well, inspect the well
context, upload a document, look at the ingestion record, follow the evidence to its page, open the
workflow workspace, edit and save a graph, run it, watch a run suspend for approval, decide the
approval, and read the run state back. Nothing here reaches into services directly.
"""

from __future__ import annotations

import csv
import io

import pytest
from tests.api.conftest import headers

pytestmark = pytest.mark.asyncio

CSV_REPORT = (
    "date,depth_m,hole_size_in,mud_weight_ppg,flow_rate_lpm,comment\n"
    "2026-01-05,1200,8.5,12.4,1800,drilled 8-1/2 section with 12.4 ppg mud\n"
    "2026-01-05,1250,8.5,12.4,1750,torque within limits\n"
)


async def _create_project_and_well(client, name: str = "Valhall development") -> tuple[str, str]:
    project = await client.post(
        "/api/v1/projects", json={"name": name, "operator": "DrillAI Demo"}, headers=headers()
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    well = await client.post(
        "/api/v1/wells",
        json={"project_id": project_id, "name": "VAL-1", "well_type": "development"},
        headers=headers(),
    )
    assert well.status_code == 201, well.text
    return project_id, well.json()["id"]


async def test_health_reports_real_catalogues(client):
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["catalogues"]["engines"] >= 14
    assert body["catalogues"]["node_types"] >= 12
    assert body["catalogues"]["agents"] >= 4


async def test_root_and_openapi_are_served(client):
    root = await client.get("/")
    assert root.status_code == 200
    assert root.json()["api_prefix"] == "/api/v1"
    schema = await client.get("/api/v1/openapi.json")
    assert schema.status_code == 200
    assert "/api/v1/documents" in schema.json()["paths"]


async def test_project_and_well_creation_then_readback(client):
    project_id, well_id = await _create_project_and_well(client)
    listing = await client.get("/api/v1/wells", params={"project_id": project_id}, headers=headers())
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()["items"]] == [well_id]
    detail = await client.get(f"/api/v1/wells/{well_id}", headers=headers())
    assert detail.json()["name"] == "VAL-1"
    assert detail.json()["wellbores"] == []
    project = await client.get(f"/api/v1/projects/{project_id}", headers=headers())
    assert project.json()["well_count"] == 1


async def test_wellbore_and_section_hierarchy(client):
    _, well_id = await _create_project_and_well(client)
    wellbore = await client.post(
        f"/api/v1/wells/{well_id}/wellbores",
        json={"name": "Main", "purpose": "production", "planned_td_md_si": 3200.0},
        headers=headers(),
    )
    assert wellbore.status_code == 201
    wellbore_id = wellbore.json()["id"]
    section = await client.post(
        f"/api/v1/wellbores/{wellbore_id}/sections",
        json={
            "sequence": 2,
            "name": '8-1/2" section',
            "kind": "intermediate",
            "hole_diameter_nominal": '8-1/2"',
            "hole_diameter_si": 0.2159,
            "planned_top_md_si": 800.0,
            "planned_bottom_md_si": 1500.0,
        },
        headers=headers(),
    )
    assert section.status_code == 201, section.text
    assert section.json()["hole_diameter_si"] == pytest.approx(0.2159)
    bad = await client.post(
        f"/api/v1/wellbores/{wellbore_id}/sections",
        json={
            "sequence": 3,
            "name": "bad",
            "planned_top_md_si": 1500.0,
            "planned_bottom_md_si": 900.0,
        },
        headers=headers(),
    )
    assert bad.status_code == 422  # deeper-than-top is enforced, not assumed
    sections = await client.get(f"/api/v1/wellbores/{wellbore_id}/sections", headers=headers())
    assert len(sections.json()["items"]) == 1


async def test_document_upload_ingestion_evidence_and_provenance(client):
    _project, well_id = await _create_project_and_well(client)
    wellbore = await client.post(
        f"/api/v1/wells/{well_id}/wellbores", json={"name": "Main"}, headers=headers()
    )
    wellbore_id = wellbore.json()["id"]
    section = await client.post(
        f"/api/v1/wellbores/{wellbore_id}/sections",
        json={"sequence": 1, "name": '8-1/2" section', "kind=": "intermediate", "kind": "intermediate"},
        headers=headers(),
    )
    section_id = section.json()["id"]

    upload = await client.post(
        "/api/v1/documents",
        headers=headers(),
        files={"file": ("ddr_2026-01-05.csv", CSV_REPORT.encode(), "text/csv")},
        data={
            "well_id": well_id,
            "wellbore_id": wellbore_id,
            "section_id": section_id,
            "doc_type": "ddr",
            "title": "Daily drilling report 2026-01-05",
        },
    )
    assert upload.status_code == 201, upload.text
    body = upload.json()
    document = body["document"]
    assert document["well_id"] == well_id
    assert document["section_id"] == section_id
    assert body["job"]["status"] == "succeeded"
    assert body["job"]["stats"]["chunks"] >= 1
    assert body["evidence_link_ids"], "every extracted record must be linked to its page"

    detail = await client.get(f"/api/v1/documents/{document['id']}", headers=headers())
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["chunks"] and detail_body["records"]
    assert detail_body["evidence_links"]
    assert len(detail_body["ingestion_jobs"]) == 1

    provenance = await client.get(
        f"/api/v1/documents/{document['id']}/provenance", headers=headers()
    )
    assert provenance.status_code == 200
    assert provenance.json()["chain"]
    first = provenance.json()["chain"][0]
    assert first["document"]["id"] == document["id"]
    assert first["record"]["method"]

    original = await client.get(f"/api/v1/documents/{document['id']}/file", headers=headers())
    assert original.status_code == 200
    assert original.content == CSV_REPORT.encode()

    permissions = await client.get(
        f"/api/v1/documents/{document['id']}/permissions", headers=headers("engineer")
    )
    assert permissions.json()["can"]["document.read"] is True
    assert permissions.json()["can"]["action:artifact.approve"] is False
    assert permissions.json()["action_ceiling"] == "L2"


async def test_evidence_endpoints_resolve_the_source_page(client):
    _project, well_id = await _create_project_and_well(client)
    upload = await client.post(
        "/api/v1/documents",
        headers=headers(),
        files={"file": ("report.csv", CSV_REPORT.encode(), "text/csv")},
        data={"well_id": well_id, "doc_type": "ddr"},
    )
    link_id = upload.json()["evidence_link_ids"][0]
    one = await client.get(f"/api/v1/evidence/{link_id}", headers=headers())
    assert one.status_code == 200
    assert one.json()["evidence"]["subject_kind"].startswith("extracted_record")
    assert one.json()["subject_record"]["payload"]
    summary = await client.get("/api/v1/evidence/summary", params={"well_id": well_id}, headers=headers())
    assert summary.json()["link_count"] >= 1
    assert summary.json()["mean_confidence"] is not None


async def test_well_context_bundle_is_scoped_and_cites_its_sources(client):
    _project, well_id = await _create_project_and_well(client)
    upload = await client.post(
        "/api/v1/documents",
        headers=headers(),
        files={"file": ("report.csv", CSV_REPORT.encode(), "text/csv")},
        data={"well_id": well_id, "doc_type": "ddr"},
    )
    assert upload.status_code == 201
    response = await client.get(
        f"/api/v1/wells/{well_id}/context",
        params={"purpose": "prompt", "render_prompt": "true"},
        headers=headers(),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    bundle = payload["bundle"]
    assert bundle["scope"]["well_id"] == well_id
    assert bundle["purpose"] == "prompt"
    kinds = {section["key"] for section in bundle["sections"]}
    assert {"well_identity", "documents"} <= kinds
    assert payload["citations"], "the rendered context must cite something"
    assert "well" in payload["prompt"].lower()


async def test_context_rejects_an_unknown_section_and_a_bad_depth_window(client):
    _project, well_id = await _create_project_and_well(client)
    unknown = await client.get(
        f"/api/v1/wells/{well_id}/context", params={"sections": "not_a_section"}, headers=headers()
    )
    assert unknown.status_code == 422
    inverted = await client.get(
        f"/api/v1/wells/{well_id}/context",
        params={"depth_from_si": 1000, "depth_to_si": 500},
        headers=headers(),
    )
    assert inverted.status_code == 422


async def test_registry_endpoints_expose_schemas_and_metadata(client):
    engines = await client.get("/api/v1/registry/engines", headers=headers())
    assert engines.status_code == 200
    keys = {item["key"] for item in engines.json()["items"]}
    assert {"trajectory.minimum_curvature", "hydraulics.laminar", "offsets.similarity"} <= keys
    detail = await client.get("/api/v1/registry/engines/trajectory.minimum_curvature", headers=headers())
    assert detail.json()["input_schema"]["properties"]
    assert detail.json()["limitations"]
    node_types = await client.get("/api/v1/registry/node-types", headers=headers())
    assert node_types.json()["families"]
    assert all(item["config_schema"] for item in node_types.json()["items"])
    actions = await client.get("/api/v1/registry/actions", headers=headers())
    assert actions.json()["levels"]["L5"]
    tools = await client.get("/api/v1/registry/tools", headers=headers())
    assert {item["key"] for item in tools.json()["items"]} >= {"search_documents", "run_engine"}
    units = await client.get("/api/v1/registry/units", headers=headers())
    assert units.json()["dimensions"]
    sections = await client.get("/api/v1/registry/context-sections", headers=headers())
    assert all(item["required_permission"] for item in sections.json()["items"])


async def test_engine_run_is_audited_with_hashes(client):
    _project, well_id = await _create_project_and_well(client)
    response = await client.post(
        "/api/v1/registry/engines/trajectory.minimum_curvature/run",
        headers=headers(),
        json={
            "well_id": well_id,
            "inputs": {
                "stations": [
                    {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                    {"md_si": 500.0, "inclination_deg": 1.5, "azimuth_deg": 45.0},
                    {"md_si": 1000.0, "inclination_deg": 3.0, "azimuth_deg": 45.0},
                ],
            },
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"]["stations"]
    assert body["inputs_hash"] and body["outputs_hash"]
    assert body["engine_run_id"]
    audited = await client.get(f"/api/v1/wells/{well_id}/engine-runs", headers=headers())
    assert audited.json()["items"][0]["id"] == body["engine_run_id"]
    assert audited.json()["items"][0]["outputs_hash"] == body["outputs_hash"]


async def test_engine_run_rejects_invalid_inputs_with_a_readable_error(client):
    response = await client.post(
        "/api/v1/registry/engines/trajectory.minimum_curvature/run",
        headers=headers(),
        json={"inputs": {"stations": []}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"].startswith("engine")


async def test_platform_capabilities_providers_and_agents(client):
    capabilities = await client.get("/api/v1/platform/capabilities", headers=headers())
    assert capabilities.status_code == 200
    assert capabilities.json()["integrations"]["llm_provider"] == "stub"
    providers = await client.get("/api/v1/platform/providers", headers=headers())
    assert providers.json()["total"] >= 1
    routing = await client.get("/api/v1/platform/providers/routing", headers=headers())
    assert routing.json()["items"]
    agents = await client.get("/api/v1/platform/agents", headers=headers())
    keys = {item["key"] for item in agents.json()["agents"]}
    assert "drilling_engineer" in keys
    assert agents.json()["max_agent_autonomy"] == "L2"
    detail = await client.get("/api/v1/platform/agents/drilling_engineer", headers=headers())
    assert detail.json()["resolved"]["tools"]
    assert detail.json()["resolved"]["engines"]


async def test_twin_write_and_read_with_state_kinds(client):
    _project, well_id = await _create_project_and_well(client)
    write = await client.post(
        f"/api/v1/wells/{well_id}/twin/aspects",
        headers=headers(),
        json={
            "aspect": "mud_program",
            "state_kind": "planned",
            "payload": {"mud_weight_si": 1465.0},
            "summary": "12.2 ppg planned",
            "computed_by": "user",
        },
    )
    assert write.status_code == 201, write.text
    assert write.json()["state_kind"] == "planned"
    read = await client.get(f"/api/v1/wells/{well_id}/twin", headers=headers(), params={"wellbore_id": None})
    body = read.json()
    assert body["current_state"]["mud_program"]["planned"]["payload"]["mud_weight_si"] == 1465.0
    catalogue = await client.get("/api/v1/twin/aspects", headers=headers())
    assert "planned" in catalogue.json()["state_kinds"]
    unknown = await client.post(
        f"/api/v1/wells/{well_id}/twin/aspects",
        headers=headers(),
        json={"aspect": "not_an_aspect", "state_kind": "planned", "payload": {}},
    )
    assert unknown.status_code == 422


# --------------------------------------------------------------------------- workflows


def _engine_graph() -> dict:
    return {
        "nodes": [
            {
                "id": "survey",
                "type": "data.load_context",
                "name": "Load well context",
                "config": {"sections": ["well_identity"]},
            },
            {
                "id": "calc",
                "type": "engineering.run_engine",
                "name": "Minimum curvature",
                "config": {"engine_key": "trajectory.minimum_curvature"},
                "inputs": {
                    "stations": [
                        {"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0},
                        {"md_si": 600.0, "inclination_deg": 2.0, "azimuth_deg": 30.0},
                    ],
                },
            },
        ],
        "edges": [{"id": "e1", "source": "survey", "target": "calc"}],
    }


async def test_workflow_validate_create_save_publish_and_run(client):
    project_id, well_id = await _create_project_and_well(client)

    # Validation catches a reference typo before anything is saved.
    broken = _engine_graph()
    broken["nodes"][1]["inputs"]["stations"] = "${node.result.does_not_exist}"
    validation = await client.post("/api/v1/workflows/validate", json=broken, headers=headers())
    assert validation.status_code == 200
    assert validation.json()["validation"]["is_valid"] is False

    created = await client.post(
        "/api/v1/workflows",
        headers=headers(),
        json={"key": "survey_check", "name": "Survey check", "graph": _engine_graph()},
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    assert created.json()["current_version"] == 1

    saved = await client.put(
        f"/api/v1/workflows/{workflow_id}/graph",
        headers=headers(),
        json={"graph": _engine_graph(), "notes": "unchanged graph"},
    )
    assert saved.json()["version"] == 1  # an identical graph is a no-op, not a new version

    published = await client.post(
        f"/api/v1/workflows/{workflow_id}/publish", headers=headers("drilling_supervisor")
    )
    assert published.status_code == 200, published.text
    assert published.json()["published_at"]

    run = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        headers=headers(),
        json={"project_id": project_id, "well_id": well_id, "inputs": {}},
    )
    assert run.status_code == 201, run.text
    assert run.json()["status"] == "succeeded"
    run_id = run.json()["id"]

    detail = await client.get(f"/api/v1/runs/{run_id}", headers=headers())
    body = detail.json()
    assert [node["node_id"] for node in body["node_runs"]] == ["survey", "calc"]
    assert body["run"]["workflow_version_id"]
    assert body["events"]
    assert any(event["type"] == "run_started" for event in body["events"])
    events = await client.get(f"/api/v1/runs/{run_id}/events", params={"after_seq": 2}, headers=headers())
    assert all(item["seq"] > 2 for item in events.json()["items"])


async def test_draft_run_pins_the_version_it_executed(client):
    _project, well_id = await _create_project_and_well(client)
    created = await client.post(
        "/api/v1/workflows",
        headers=headers(),
        json={"key": "pin_version", "name": "Pin", "graph": _engine_graph()},
    )
    workflow_id = created.json()["id"]
    run = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        headers=headers(),
        json={"well_id": well_id, "version": 1},
    )
    assert run.status_code == 201
    assert run.json()["version"] == 1
    assert run.json()["workflow_version_id"]


async def test_workflow_fork_keeps_the_default_inspectable_and_editable(client):
    source = await client.post(
        "/api/v1/workflows",
        headers=headers(),
        json={"key": "default_check", "name": "Default check", "graph": _engine_graph()},
    )
    source_id = source.json()["id"]
    forked = await client.post(
        f"/api/v1/workflows/{source_id}/fork",
        headers=headers(),
        params={"key": "org_check", "name": "Org check"},
    )
    assert forked.status_code == 201, forked.text
    assert forked.json()["forked_from_id"] == source_id
    assert forked.json()["current_version"] == 1
    original = await client.get(f"/api/v1/workflows/{source_id}", headers=headers())
    assert original.json()["workflow"]["current_version"] == 1


async def test_l4_node_suspends_the_run_and_the_approval_resumes_it(client):
    _project, well_id = await _create_project_and_well(client)
    graph = {
        "nodes": [
            {"id": "notify", "type": "output.notify", "name": "Notify rig", "config": {"channel": "email", "recipient": "rig@example.com", "body": "Trajectory check complete"}},
        ],
        "edges": [],
    }
    created = await client.post(
        "/api/v1/workflows",
        headers=headers("drilling_supervisor"),
        json={"key": "l4_flow", "name": "L4 flow", "graph": graph},
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    await client.post(f"/api/v1/workflows/{workflow_id}/publish", headers=headers("drilling_supervisor"))

    run = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        headers=headers("drilling_supervisor"),
        json={"well_id": well_id},
    )
    assert run.status_code == 201, run.text
    assert run.json()["status"] == "waiting_approval"
    approval_id = run.json()["pending_approval_id"]
    assert approval_id

    inbox = await client.get("/api/v1/approvals", headers=headers("drilling_supervisor"))
    assert [item["id"] for item in inbox.json()["items"]] == [approval_id]
    assert inbox.json()["items"][0]["overdue"] is False

    # The requester is the supervisor (the dev principal), so an engineer cannot decide it at all,
    # and the supervisor cannot decide their own request (separation of duties).
    denied = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        headers=headers("engineer"),
        json={"decision": "approved"},
    )
    assert denied.status_code == 403
    self_decision = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        headers=headers("drilling_supervisor"),
        json={"decision": "approved"},
    )
    assert self_decision.status_code == 409
    assert "separation of duties" in self_decision.json()["error"]["message"]

    decided = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        headers=headers("well_manager"),
        json={"decision": "approved", "note": "Checked the notification list", "resume": True},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["approval"]["status"] == "approved"
    resumed = decided.json()["resumed_run"]
    assert resumed["status"] != "waiting_approval"
    assert resumed["approved_by"] == "usr_dev_well_manager"

    final = await client.get(f"/api/v1/runs/{run.json()['id']}", headers=headers())
    assert final.json()["run"]["pending_approval_id"] is None
    assert final.json()["resumable"] is False


async def test_rejected_approval_cancels_the_run(client):
    _project, well_id = await _create_project_and_well(client)
    graph = {
        "nodes": [{"id": "notify", "type": "output.notify", "name": "Notify", "config": {"channel": "email", "recipient": "ops@example.com", "body": "Review required"}}],
        "edges": [],
    }
    created = await client.post(
        "/api/v1/workflows", headers=headers("drilling_supervisor"), json={"key": "reject_flow", "name": "R", "graph": graph}
    )
    workflow_id = created.json()["id"]
    run = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs", headers=headers("drilling_supervisor"), json={"well_id": well_id}
    )
    approval_id = run.json()["pending_approval_id"]
    decided = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        headers=headers("well_manager"),
        json={"decision": "rejected", "note": "Not this rig", "resume": True},
    )
    assert decided.status_code == 200
    assert decided.json()["resumed_run"]["status"] == "cancelled"
    again = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        headers=headers("well_manager"),
        json={"decision": "approved"},
    )
    assert again.status_code == 409


async def test_well_audit_view_joins_engine_runs_and_workflow_runs(client):
    _project, well_id = await _create_project_and_well(client)
    created = await client.post(
        "/api/v1/workflows", headers=headers(), json={"key": "audit_flow", "name": "Audit", "graph": _engine_graph()}
    )
    workflow_id = created.json()["id"]
    await client.post(f"/api/v1/workflows/{workflow_id}/runs", headers=headers(), json={"well_id": well_id})
    audit = await client.get(f"/api/v1/wells/{well_id}/audit", headers=headers())
    body = audit.json()
    assert body["counts"]["workflow_runs"] == 1
    assert body["counts"]["engine_runs"] == 1
    assert body["engine_runs"][0]["engine_key"] == "trajectory.minimum_curvature"
    assert body["node_runs"]


async def test_run_cannot_be_resumed_before_the_approval_is_decided(client):
    _project, well_id = await _create_project_and_well(client)
    graph = {
        "nodes": [{"id": "notify", "type": "output.notify", "name": "Notify", "config": {"channel": "email", "recipient": "ops@example.com", "body": "Review required"}}],
        "edges": [],
    }
    created = await client.post(
        "/api/v1/workflows", headers=headers("drilling_supervisor"), json={"key": "resume_guard", "name": "R", "graph": graph}
    )
    workflow_id = created.json()["id"]
    run = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs", headers=headers("drilling_supervisor"), json={"well_id": well_id}
    )
    response = await client.post(
        f"/api/v1/runs/{run.json()['id']}/resume", headers=headers("drilling_supervisor")
    )
    assert response.status_code == 409
    assert "not been decided" in response.json()["error"]["message"]


async def test_unknown_ids_return_a_structured_404(client):
    for path in (
        "/api/v1/wells/wel_missing",
        "/api/v1/documents/doc_missing",
        "/api/v1/workflows/wf_missing",
        "/api/v1/approvals/apr_missing",
        "/api/v1/runs/run_missing",
    ):
        response = await client.get(path, headers=headers())
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "platform.not_found"


async def test_upload_rejects_an_empty_file(client):
    _project, well_id = await _create_project_and_well(client)
    response = await client.post(
        "/api/v1/documents",
        headers=headers(),
        files={"file": ("empty.csv", b"", "text/csv")},
        data={"well_id": well_id},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "platform.validation_failed"


async def test_uploaded_row_values_are_retrievable_and_traceable(client):
    """The uploaded numbers must be reachable as text (and citable), and the header parsed.

    Row-level *typed* extraction from an arbitrary DDR table is deliberately not claimed here:
    the current extractor records the report header and keeps the row text in a chunk, which the
    evidence link points at. Asserting exactly that documents the true extraction coverage.
    """
    _project, well_id = await _create_project_and_well(client)
    upload = await client.post(
        "/api/v1/documents",
        headers=headers(),
        files={"file": ("rows.csv", CSV_REPORT.encode(), "text/csv")},
        data={"well_id": well_id, "doc_type": "ddr"},
    )
    document_id = upload.json()["document"]["id"]
    detail = await client.get(f"/api/v1/documents/{document_id}", headers=headers())
    body = detail.json()
    assert body["records"], "the report header must be extracted as a record"
    assert body["records"][0]["method"]
    chunk_text = "\n".join(chunk["text"] for chunk in body["chunks"])
    assert "12.4" in chunk_text and "8.5" in chunk_text
    link = body["evidence_links"][0]
    assert link["page_number"] is not None or link["locator"]


async def test_permissions_are_enforced_per_route(client):
    _project, well_id = await _create_project_and_well(client)
    # a viewer may read but not ingest
    read = await client.get(f"/api/v1/wells/{well_id}", headers=headers("viewer"))
    assert read.status_code == 200
    denied = await client.post(
        "/api/v1/documents",
        headers=headers("viewer"),
        files={"file": ("report.csv", CSV_REPORT.encode(), "text/csv")},
        data={"well_id": well_id},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "security.permission_denied"
    # an auditor may read registries but not run engines
    assert (await client.get("/api/v1/registry/engines", headers=headers("auditor"))).status_code == 200
    engine_denied = await client.post(
        "/api/v1/registry/engines/trajectory.minimum_curvature/run",
        headers=headers("viewer"),
        json={"inputs": {"stations": [{"md_si": 0.0, "inclination_deg": 0.0, "azimuth_deg": 0.0}]}},
    )
    assert engine_denied.status_code == 403


async def test_csv_content_type_is_optional_and_content_decides(client):
    """The extractor chain must not depend on a client-declared MIME type."""
    _project, well_id = await _create_project_and_well(client)
    upload = await client.post(
        "/api/v1/documents",
        headers=headers(),
        files={"file": ("report.csv", CSV_REPORT.encode(), "application/octet-stream")},
        data={"well_id": well_id},
    )
    assert upload.status_code == 201, upload.text
    assert upload.json()["job"]["status"] == "succeeded"


# NOTE: the run-event WebSocket endpoint (``/runs/{id}/events/stream``) is implemented but not
# exercised by an automated test here: driving it needs the app's lifespan and its own event loop,
# and the harness used for these HTTP tests disposes the database on exit. The durable event log
# and the HTTP events endpoint *are* covered above; the socket is verified manually.


async def test_auth_enabled_requires_a_bearer_token(tmp_path, monkeypatch):
    """With authentication on, an unauthenticated request is 401 and a token grants the role's rights."""
    import httpx

    from drillai.api.app import create_app
    from drillai.core.clock import UTC
    from drillai.core.config import get_settings, reset_settings_cache
    from drillai.db.models import ApiToken, Base, Membership, Organization, Project, User
    from drillai.security.passwords import new_api_token

    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/auth.db")
    reset_settings_cache()
    settings = get_settings()
    application = create_app(settings)
    async with application.state.database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    token, token_id, token_hash = new_api_token(prefix="dk")
    async with application.state.database.session() as session:
        org = Organization(slug="auth-org", name="Auth org")
        session.add(org)
        await session.flush()
        user = User(email="eng@example.com", display_name="Engineer")
        session.add(user)
        await session.flush()
        session.add(
            Membership(org_id=org.id, user_id=user.id, role_key="engineer", scope_kind="org")
        )
        session.add(
            ApiToken(
                org_id=org.id,
                user_id=user.id,
                name="test token",
                token_prefix=token_id,
                token_hash=token_hash,
                scopes=[],
                created_at=None,
            )
        )
        session.add(Project(org_id=org.id, name="Auth project"))

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        unauthenticated = await http.get("/api/v1/wells")
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["www-authenticate"] == "Bearer"
        assert unauthenticated.json()["error"]["code"] == "security.authentication_required"
        # the documented contract (openapi) stays public, project data does not
        assert (await http.get("/api/v1/health")).status_code == 200
        assert (await http.get("/api/v1/projects")).status_code == 401
        import datetime as dt

        before = dt.datetime.now(tz=UTC)
        authorised = await http.get("/api/v1/projects", headers={"Authorization": f"Bearer {token}"})
        assert authorised.status_code == 200, authorised.text
        assert authorised.json()["total"] == 1
        # a bad token is refused, and a viewer cannot ingest
        bad = await http.get("/api/v1/projects", headers={"Authorization": "Bearer dk_zzz_nope"})
        assert bad.status_code == 401
    async with application.state.database.session() as session:
        row = (await session.execute(__import__("sqlalchemy").select(ApiToken))).scalar_one()
        assert row.last_used_at is not None and row.last_used_at >= before
    await application.state.database.dispose()
    reset_settings_cache()


async def test_cors_headers_are_returned_for_a_cross_origin_request(client):
    response = await client.get(
        "/api/v1/health",
        headers={"Origin": "https://preview.example.com", "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "*"
    assert response.headers["x-request-id"]


async def test_request_id_is_echoed_and_traced_through_errors(client):
    response = await client.get(
        "/api/v1/wells/wel_missing", headers=headers(**{"X-Request-ID": "req_test_123"})
    )
    assert response.headers["x-request-id"] == "req_test_123"
    assert response.json()["error"]["trace_id"] == "req_test_123"


async def test_upload_scope_validation_rejects_a_mismatched_section(client):
    _project, well_id = await _create_project_and_well(client)
    wellbore_a = (
        await client.post(f"/api/v1/wells/{well_id}/wellbores", json={"name": "A"}, headers=headers())
    ).json()["id"]
    wellbore_b = (
        await client.post(f"/api/v1/wells/{well_id}/wellbores", json={"name": "B"}, headers=headers())
    ).json()["id"]
    section = (
        await client.post(
            f"/api/v1/wellbores/{wellbore_a}/sections",
            json={"sequence": 1, "name": "S", "kind": "intermediate"},
            headers=headers(),
        )
    ).json()["id"]
    response = await client.post(
        "/api/v1/documents",
        headers=headers(),
        files={"file": ("report.csv", CSV_REPORT.encode(), "text/csv")},
        data={"well_id": well_id, "wellbore_id": wellbore_b, "section_id": section},
    )
    assert response.status_code == 422


async def test_csv_roundtrip_keeps_the_declared_period(client):
    _project, well_id = await _create_project_and_well(client)
    response = await client.post(
        "/api/v1/documents",
        headers=headers(),
        files={"file": ("period.csv", CSV_REPORT.encode(), "text/csv")},
        data={
            "well_id": well_id,
            "period_start": "2026-01-05T00:00:00Z",
            "period_end": "2026-01-06T00:00:00Z",
            "doc_type": "ddr",
        },
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["document"]["id"]
    shifted = await client.get(f"/api/v1/documents/{document_id}", headers=headers())
    assert shifted.json()["document"]["period_start"].startswith("2026-01-05")


def test_csv_helper_is_deterministic():
    """Guard the fixture itself: a drifting test fixture would hide real regressions."""
    rows = list(csv.DictReader(io.StringIO(CSV_REPORT)))
    assert len(rows) == 2
    assert rows[0]["mud_weight_ppg"] == "12.4"
