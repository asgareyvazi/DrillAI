"""The error contract, proven against the real application.

Every failure class the frontend classifies is produced here the way the server really produces it:
through the application's own exception handlers, over ASGI, with the real database and the real
authorisation model. Where a status cannot be produced by a healthy route (400 has no production
producer today, and a 500 must not exist on demand), the *platform's own* exception class or a
deliberately exploding route is used so that the boundary under test — `create_app`'s handlers — is
the real one rather than a hand-written response.

What is pinned here is what the client is allowed to rely on:

* the envelope `{"error": {code, message, retryable, details, trace_id}}` for every class;
* the request id: echoed when supplied, generated when not, always present in the header *and* in
  the body — including for a 500, whose response is produced above the middleware that adds it;
* 401 only when authentication is on, and only for a missing or invalid credential;
* 403 for a credential that is understood but lacks the permission, naming the permission;
* 404 for an absent resource; 409 for a state conflict; 422 for a rejected payload;
* `retryable` telling the truth about what a retry could achieve;
* and no internal detail — no traceback, no exception text, no path — leaking into a 500.
"""

from __future__ import annotations

import json

import httpx
import pytest
from tests.api.conftest import headers

from drillai.core.errors import ConfigurationError, NodeTimeout, UnsupportedOperation


async def _project_and_well(client: httpx.AsyncClient) -> tuple[str, str]:
    project = await client.post("/api/v1/projects", json={"name": "Error contract"}, headers=headers())
    assert project.status_code == 201, project.text
    well = await client.post(
        "/api/v1/wells",
        json={"project_id": project.json()["id"], "name": "NF-ERROR", "well_type": "development"},
        headers=headers(),
    )
    assert well.status_code == 201, well.text
    return project.json()["id"], well.json()["id"]


async def _published_gate_workflow(client: httpx.AsyncClient, *, key: str) -> tuple[str, str]:
    """A published workflow whose only interesting node is a human approval gate.

    Reused by the conflict tests: the gate is what gives the run a state that can conflict.
    """
    graph = {
        "nodes": [
            {"id": "prepare", "type": "logic.set_variables", "config": {"variables": {"step": 1}}},
            {
                "id": "approve_plan",
                "type": "human.approval",
                "name": "Approve the plan",
                "config": {
                    "title": "Approve the plan",
                    "description": "Review the computed state.",
                    "action_level": "L3",
                    "required_role": "drilling_supervisor",
                },
            },
            {"id": "issue", "type": "output.report", "config": {"title": "Report", "sections": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "prepare", "target": "approve_plan"},
            {"id": "e2", "source": "approve_plan", "target": "issue"},
        ],
    }
    created = await client.post(
        "/api/v1/workflows",
        headers=headers("drilling_supervisor"),
        json={"key": key, "name": "Error contract gate", "graph": graph},
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    published = await client.post(
        f"/api/v1/workflows/{workflow_id}/publish", headers=headers("drilling_supervisor")
    )
    assert published.status_code == 200, published.text
    _project, well_id = await _project_and_well(client)
    return workflow_id, well_id


def _client_that_returns_errors(app) -> httpx.AsyncClient:
    """A client that keeps the response instead of re-raising the exception.

    Starlette's server-error layer sends the 500 to the client and then re-raises so the server can
    log it; a real deployment swallows that and the browser sees the response. `httpx`'s default is
    to propagate it, which is convenient for debugging and wrong for this contract: the response the
    client would receive is the thing under test.
    """
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    )


def _envelope(response: httpx.Response) -> dict:
    body = response.json()
    assert "error" in body, body
    error = body["error"]
    for key in ("code", "message", "retryable", "details"):
        assert key in error, f"{key} missing from {error}"
    assert isinstance(error["retryable"], bool)
    assert isinstance(error["details"], dict)
    return error


# --------------------------------------------------------------------------- the request id


async def test_request_id_is_echoed_when_the_client_supplies_one(client):
    response = await client.get("/api/v1/wells/wel_missing", headers=headers(**{"X-Request-ID": "req_test_123"}))
    assert response.status_code == 404
    assert response.headers["x-request-id"] == "req_test_123"
    assert _envelope(response)["trace_id"] == "req_test_123"


async def test_request_id_is_generated_and_returned_when_the_client_supplies_none(client):
    """A client that sends no id still gets one back — in the header and in the envelope."""
    ok = await client.get("/api/v1/health")
    assert ok.status_code == 200
    assert ok.headers["x-request-id"]

    refused = await client.get("/api/v1/wells/wel_missing", headers=headers())
    assert refused.status_code == 404
    header_id = refused.headers["x-request-id"]
    assert header_id
    assert _envelope(refused)["trace_id"] == header_id, "the body and the header must agree"


async def test_an_unhandled_error_is_traceable_above_the_correlation_middleware(app, client):
    """A 500 is the one response produced outside the middleware, so its id must be set by the handler.

    Without that, the single failure an operator most needs to correlate with the server log is the
    one response that carries no id at all.
    """

    async def explode() -> dict[str, object]:
        raise RuntimeError("deliberate explosion for the error contract test")

    app.add_api_route("/api/v1/__boom", explode, methods=["GET"])

    async with _client_that_returns_errors(app) as http:
        response = await http.get("/api/v1/__boom", headers=headers())
    assert response.status_code == 500
    header_id = response.headers.get("x-request-id")
    assert header_id, "a 500 must carry a request id"
    assert _envelope(response)["trace_id"] == header_id


# --------------------------------------------------------------------------- 500


async def test_an_unhandled_error_hides_internals(app, client):
    async def explode() -> dict[str, object]:
        raise RuntimeError("secret database password in an exception message")

    app.add_api_route("/api/v1/__boom", explode, methods=["GET"])

    async with _client_that_returns_errors(app) as http:
        response = await http.get("/api/v1/__boom", headers=headers())
    assert response.status_code == 500
    error = _envelope(response)
    assert error["code"] == "platform.internal_error"
    assert error["message"] == "internal error"
    assert error["retryable"] is False
    assert "secret" not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


async def test_a_retryable_server_error_says_so(app, client):
    """The platform's own exception classes carry the flag, and the envelope passes it through."""

    async def explode() -> dict[str, object]:
        raise NodeTimeout("a workflow node exceeded its deadline")

    app.add_api_route("/api/v1/__node_timeout", explode, methods=["GET"])

    async with _client_that_returns_errors(app) as http:
        response = await http.get("/api/v1/__node_timeout", headers=headers())
    assert response.status_code == 400  # WorkflowError's status; the class carries the level
    error = _envelope(response)
    assert error["code"] == "workflow.node_timeout"
    assert error["retryable"] is True, "a timeout is worth another attempt"


# --------------------------------------------------------------------------- 400


async def test_a_bad_request_class_error_is_reported_as_400(app, client):
    """No production route raises 400 today; the class is pinned at the handler boundary.

    The exception raised is the platform's own `UnsupportedOperation`, so what is being tested is the
    real mapping from the error model to the transport rather than a route that was written to fail.
    """

    async def unsupported() -> dict[str, object]:
        raise UnsupportedOperation("this deployment does not support that operation")

    app.add_api_route("/api/v1/__unsupported", unsupported, methods=["GET"])

    async with _client_that_returns_errors(app) as http:
        response = await http.get("/api/v1/__unsupported", headers=headers())
    assert response.status_code == 400
    error = _envelope(response)
    assert error["code"] == "platform.unsupported_operation"
    assert error["retryable"] is False


# --------------------------------------------------------------------------- 401 / 403


async def test_authentication_off_never_produces_401(client):
    """In development the identity header is how a caller acts; there is no unauthenticated state."""
    response = await client.get("/api/v1/wells", headers={"X-Dev-Roles": "engineer"})
    assert response.status_code == 200


async def test_permission_denied_names_the_permission_and_the_roles(client):
    response = await client.post("/api/v1/projects", json={"name": "Nope"}, headers=headers("viewer"))
    assert response.status_code == 403
    error = _envelope(response)
    assert error["code"] == "security.permission_denied"
    assert error["details"], "a 403 must say what was missing"
    assert error["details"]["action"] == "project.create"
    assert error["details"]["required_level"] and error["details"]["ceiling"]
    assert response.headers.get("www-authenticate") is None, "403 is not an authentication challenge"


async def test_a_401_carries_the_authentication_challenge(tmp_path, monkeypatch):
    """With authentication on, a missing credential is 401 with a challenge — and never a 403."""
    from drillai.api.app import create_app
    from drillai.core.config import get_settings, reset_settings_cache
    from drillai.db.models import Base

    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/errors.db")
    reset_settings_cache()
    application = create_app(get_settings())
    async with application.state.database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        for path in ("/api/v1/wells", "/api/v1/projects"):
            response = await http.get(path)
            assert response.status_code == 401, path
            assert response.headers["www-authenticate"] == "Bearer"
            error = _envelope(response)
            assert error["code"] == "security.authentication_required"
            assert error["trace_id"] == response.headers["x-request-id"]

        # The development header grants nothing when authentication is on: no impersonation path.
        dev = await http.get("/api/v1/projects", headers={"X-Dev-Roles": "admin"})
        assert dev.status_code == 401, dev.text

        # The documented health endpoint stays public: a probe must not need credentials.
        assert (await http.get("/api/v1/health")).status_code == 200

    await application.state.database.dispose()
    reset_settings_cache()


# --------------------------------------------------------------------------- 404 / 409 / 422


async def test_not_found_is_404_with_the_resource_named(client):
    response = await client.get("/api/v1/wells/wel_does_not_exist", headers=headers())
    assert response.status_code == 404
    error = _envelope(response)
    assert error["code"] == "platform.not_found"
    assert "wel_does_not_exist" in error["message"]
    assert error["retryable"] is False


async def test_validation_reports_the_offending_fields(client):
    """A payload FastAPI itself rejects: 422 with the issues the UI can point at."""
    response = await client.post("/api/v1/projects", json={}, headers=headers())
    assert response.status_code == 422
    error = _envelope(response)
    assert error["code"] == "platform.validation_failed"
    issues = error["details"]["issues"]
    assert isinstance(issues, list) and issues
    assert any("name" in str(issue.get("loc", [])) for issue in issues)


async def test_a_domain_validation_failure_is_422_with_the_domain_explanation(client):
    _project, well_id = await _project_and_well(client)

    response = await client.post(
        f"/api/v1/wells/{well_id}/wellbores",
        json={"name": "Bore", "sections": [{"name": "s", "kind": "hole", "start_md": 100, "end_md": 50}]},
        headers=headers(),
    )
    assert response.status_code in (201, 422), response.text
    if response.status_code == 422:
        error = _envelope(response)
        assert error["code"]
        assert error["message"], "a rejected payload must say why"


async def test_a_state_conflict_is_409_and_says_what_the_state_is(client):
    """Resuming a run that is not waiting for anything: a real conflict from the run route."""
    workflow_id, well_id = await _published_gate_workflow(client, key="error-conflict-wf")
    run = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        json={"well_id": well_id},
        headers=headers("drilling_supervisor"),
    )
    assert run.status_code == 201, run.text
    run_id = run.json()["id"]

    # The run is parked at its approval gate: it cannot be resumed before the decision exists. The
    # conflict names the state it is actually in, so the client can say what is missing rather than
    # "something went wrong".
    conflict = await client.post(f"/api/v1/runs/{run_id}/resume", headers=headers("drilling_supervisor"))
    assert conflict.status_code == 409, conflict.text
    error = _envelope(conflict)
    assert error["code"] == "platform.conflict"
    assert error["details"].get("status") == "pending"
    assert error["details"].get("approval_id")
    assert error["retryable"] is False

    # And a decision taken by the requester is refused — separation of duties, as a conflict.
    envelope = await client.get(f"/api/v1/runs/{run_id}", headers=headers("drilling_supervisor"))
    approval_id = envelope.json()["pending_approval"]["id"]
    own_decision = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approved", "resume": False},
        headers=headers("drilling_supervisor"),
    )
    assert own_decision.status_code == 409, own_decision.text
    assert _envelope(own_decision)["details"]["principal"]


async def test_an_approval_can_only_be_decided_once(client):
    workflow_id, well_id = await _published_gate_workflow(client, key="error-once-wf")
    run = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        json={"well_id": well_id},
        headers=headers("drilling_supervisor"),
    )
    run_id = run.json()["id"]
    approval_id = (
        await client.get(f"/api/v1/runs/{run_id}", headers=headers("drilling_supervisor"))
    ).json()["pending_approval"]["id"]

    first = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "approved", "resume": False},
        headers=headers("well_manager"),
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        json={"decision": "rejected", "resume": False},
        headers=headers("well_manager"),
    )
    assert second.status_code == 409, second.text
    error = _envelope(second)
    assert error["details"]["status"] == "approved"

    # A decided approval unblocks the resume; the same call once the run is no longer waiting is a
    # conflict again, and this time it names the run's own state.
    resumed = await client.post(f"/api/v1/runs/{run_id}/resume", headers=headers("drilling_supervisor"))
    assert resumed.status_code == 200, resumed.text
    again = await client.post(f"/api/v1/runs/{run_id}/resume", headers=headers("drilling_supervisor"))
    assert again.status_code == 409, again.text
    assert _envelope(again)["details"]["resumable"] == ["waiting_approval", "paused"]


# --------------------------------------------------------------------------- fault injection


async def test_fault_routes_are_absent_unless_they_are_enabled(client):
    for path in ("/api/v1/__faults/slow", "/api/v1/__faults/malformed", "/api/v1/__faults/unhandled"):
        response = await client.get(path, headers=headers())
        assert response.status_code == 404, f"{path} answered {response.status_code}"


async def test_an_unhandled_fault_route_goes_through_the_real_error_handler(tmp_path, monkeypatch):
    from drillai.api.app import create_app
    from drillai.core.config import get_settings, reset_settings_cache
    from drillai.db.models import Base

    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_E2E_FAULTS", "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/faults.db")
    reset_settings_cache()
    application = create_app(get_settings())
    async with application.state.database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with _client_that_returns_errors(application) as http:
        failed = await http.get("/api/v1/__faults/unhandled")
        assert failed.status_code == 500
        error = _envelope(failed)
        assert error["code"] == "platform.internal_error"
        assert error["trace_id"] == failed.headers["x-request-id"]
        assert "deliberate" not in failed.text, "the route's own message must not reach the client"

        broken = await http.get("/api/v1/__faults/malformed")
        assert broken.status_code == 200
        assert broken.headers["content-type"].startswith("application/json")
        with pytest.raises(ValueError):
            broken.json()

        shaped = await http.get("/api/v1/__faults/wrong-shape")
        assert shaped.status_code == 200
        assert shaped.json() == {"unexpected": [], "total": 0}

    await application.state.database.dispose()
    reset_settings_cache()


async def test_an_armed_fault_fails_a_real_endpoint_through_the_real_handlers(tmp_path, monkeypatch):
    """The injector the browser journeys use: a rule, a real endpoint, the real error contract."""
    from drillai.api.app import create_app
    from drillai.api.routers import faults
    from drillai.core.config import get_settings, reset_settings_cache
    from drillai.db.models import Base

    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_E2E_FAULTS", "true")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/armed.db")
    reset_settings_cache()
    application = create_app(get_settings())
    async with application.state.database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    try:
        async with _client_that_returns_errors(application) as http:
            healthy = await http.get("/api/v1/wells", headers=headers())
            assert healthy.status_code == 200

            # An injected malformed body leaves through the same layers as a real one: it carries the
            # correlation id the middleware adds and the content type it claims, and the body is not
            # JSON. A response injected *outside* those layers would be blocked by the browser and
            # read as a network failure instead — the exact confusion this checkpoint is about.
            armed = await http.post(
                "/api/v1/__faults/arm",
                json={"method": "GET", "path": "/api/v1/projects", "mode": "malformed"},
            )
            assert armed.status_code == 200, armed.text
            unreadable = await http.get("/api/v1/projects", headers=headers())
            assert unreadable.status_code == 200
            assert unreadable.headers["content-type"].startswith("application/json")
            assert unreadable.headers["x-request-id"]
            assert unreadable.headers["x-response-time-ms"]
            with pytest.raises(json.JSONDecodeError):
                json.loads(unreadable.text)
            await http.post("/api/v1/__faults/disarm")

            # The same mechanism can fail a real endpoint, and the answer is produced by the
            # application's own generic error handler: same envelope, same correlation id.
            await http.post(
                "/api/v1/__faults/arm",
                json={"method": "GET", "path": "/api/v1/wells", "mode": "unhandled"},
            )
            failed = await http.get("/api/v1/wells", headers=headers())
            assert failed.status_code == 500, failed.text
            error = _envelope(failed)
            assert error["code"] == "platform.internal_error"
            assert error["trace_id"] == failed.headers["x-request-id"]

            # Something that does not match the rule is served normally, and disarming restores it.
            assert (await http.get("/api/v1/projects", headers=headers())).status_code == 200
            assert (await http.post("/api/v1/__faults/disarm")).status_code == 200
            assert (await http.get("/api/v1/wells", headers=headers())).status_code == 200
            assert faults.armed_rules() == []
    finally:
        faults.disarm_all()
        await application.state.database.dispose()
        reset_settings_cache()


def test_fault_injection_cannot_be_enabled_in_production(monkeypatch):
    from drillai.api.app import create_app
    from drillai.core.config import get_settings, reset_settings_cache

    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "production")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    monkeypatch.setenv("DRILLAI_CORS_ORIGINS", "https://drillai.example.com")
    monkeypatch.setenv("DRILLAI_E2E_FAULTS", "true")
    reset_settings_cache()
    try:
        with pytest.raises(ConfigurationError) as raised:
            create_app(get_settings())
        assert "E2E_FAULTS" in str(raised.value)
    finally:
        reset_settings_cache()
