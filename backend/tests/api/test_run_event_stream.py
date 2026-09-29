"""The run-event WebSocket, driven through the real application.

The socket is the one part of the runtime the HTTP harness could not reach, and the code said so:
"the socket is verified manually". Manual verification is not evidence, so this module drives the
real ASGI application — the shared ``app`` fixture (the same ``create_app()`` factory production
uses, the real ``Database``, real routers and real middleware) with the real workflow runtime
executing real nodes — and reads the frames the server actually sends.

What is pinned here is the contract a browser client depends on:

* ``stream_opened`` is the first frame, and the socket then tails the durable log;
* reconnecting with ``after_seq`` resumes without gaps and without duplicates — the reason the event
  log is a table rather than an in-memory queue;
* a run that reaches a terminal state ends with ``stream_closed``, so a client stops reconnecting
  instead of following a finished run forever;
* a client that leaves frees the server-side stream immediately, and the handler ends by itself;
* refusals are distinguishable: 4401 not authorized, 4403 ``workflow.read`` missing, 4404 run not
  found;
* a development identity may arrive in the query string (a browser cannot set handshake headers),
  and **with authentication enabled that parameter grants nothing**.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from tests.api.conftest import headers
from tests.api.ws import WebSocketRefused, WebSocketSession

from drillai.core.clock import UTC
from drillai.db.models import RunEvent, WorkflowRun

GATE_GRAPH = {
    "nodes": [
        {"id": "prepare", "type": "logic.set_variables", "config": {"variables": {"step": 1}}},
        {
            "id": "approve_plan",
            "type": "human.approval",
            "name": "Approve the plan",
            "config": {
                "title": "Approve the daily plan",
                "description": "Review the computed state before the report is issued.",
                "action_level": "L3",
                "required_role": "drilling_supervisor",
                "risk_notes": ["The report reaches the rig site once approved."],
            },
        },
        {"id": "issue", "type": "output.report", "config": {"title": "Daily report", "sections": {}}},
    ],
    "edges": [
        {"id": "e1", "source": "prepare", "target": "approve_plan"},
        {"id": "e2", "source": "approve_plan", "target": "issue"},
    ],
}


async def seed_gate_run(client, *, key: str) -> tuple[str, str]:
    """Create a project, a well, a published gate workflow and start a run of it.

    Container objects are created by the development administrator identity and the run is started by
    the supervisor: the roles really do differ, and the run's own action ceiling is the point of the
    gate.
    """
    project = await client.post("/api/v1/projects", json={"name": "WS project"}, headers=headers())
    assert project.status_code == 201, project.text
    well = await client.post(
        "/api/v1/wells",
        json={"project_id": project.json()["id"], "name": "WS-1", "well_type": "development"},
        headers=headers(),
    )
    assert well.status_code == 201, well.text
    created = await client.post(
        "/api/v1/workflows",
        headers=headers("drilling_supervisor"),
        json={"key": key, "name": "WS gate", "graph": GATE_GRAPH},
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    published = await client.post(
        f"/api/v1/workflows/{workflow_id}/publish", headers=headers("drilling_supervisor")
    )
    assert published.status_code == 200, published.text
    run = await client.post(
        f"/api/v1/workflows/{workflow_id}/runs",
        headers=headers("drilling_supervisor"),
        json={"well_id": well.json()["id"]},
    )
    assert run.status_code == 201, run.text
    assert run.json()["status"] == "waiting_approval", run.text
    envelope = await client.get(f"/api/v1/runs/{run.json()['id']}", headers=headers("drilling_supervisor"))
    pending = envelope.json()["pending_approval"]
    assert pending is not None and pending["status"] == "pending", envelope.text
    return run.json()["id"], pending["id"]


async def events(client, run_id: str) -> dict:
    response = await client.get(f"/api/v1/runs/{run_id}/events", headers=headers("drilling_supervisor"))
    assert response.status_code == 200, response.text
    return response.json()


async def decide(client, approval_id: str, *, decision: str = "approved", note: str = "Reviewed") -> dict:
    response = await client.post(
        f"/api/v1/approvals/{approval_id}/decide",
        headers=headers("well_manager"),
        json={"decision": decision, "note": note, "resume": True},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def read_until_closed(socket, *, limit: int = 200) -> list[dict]:
    frames: list[dict] = []
    for _ in range(limit):
        try:
            frames.append(await socket.receive())
        except WebSocketRefused as closed:
            assert frames, f"the stream closed before sending anything: {closed}"
            return frames
        if frames[-1]["type"] in {"stream_closed", "stream_error"}:
            return frames
    raise AssertionError(f"the stream did not end after {limit} frames")


async def test_the_socket_tails_the_durable_log_and_closes_when_the_run_ends(client, websocket):
    run_id, approval_id = await seed_gate_run(client, key="ws_gate")
    logged = await events(client, run_id)
    assert logged["total"] > 0

    async with websocket(
        f"/api/v1/runs/{run_id}/events/stream", after_seq="0", dev_roles="drilling_supervisor"
    ) as socket:
        opened = await socket.receive()
        assert opened == {
            "type": "stream_opened",
            "run_id": run_id,
            "status": "waiting_approval",
            "after_seq": 0,
        }

        # The socket first replays what is already durable, in sequence order.
        replayed = await socket.frames(logged["total"])
        assert [frame["type"] for frame in replayed] == ["run_event"] * logged["total"]
        seen = [frame["event"]["seq"] for frame in replayed]
        assert seen == sorted(seen) == [row["seq"] for row in logged["items"]]

        # A decision by another identity produces more durable events, which arrive on the same
        # socket — no reconnect needed, and no client-side replay.
        await decide(client, approval_id)
        frames = await read_until_closed(socket)

    assert frames[-1]["type"] == "stream_closed", frames[-1]
    assert frames[-1]["status"] == "succeeded"

    streamed = [frame["event"]["seq"] for frame in frames if frame["type"] == "run_event"]
    assert streamed == sorted(streamed)
    assert set(streamed).isdisjoint(seen), "a durable event must not be sent twice"
    assert min(streamed) == seen[-1] + 1, "the tail continues exactly where the replay stopped"

    # The socket delivered the whole log, in order, once — and it is the same log a client gets by
    # reading REST with a cursor, which is what makes the stream a transport rather than a source.
    final = await events(client, run_id)
    assert seen + streamed == [row["seq"] for row in final["items"]]


async def test_reconnecting_with_after_seq_loses_no_event_and_duplicates_none(client, websocket):
    run_id, approval_id = await seed_gate_run(client, key="ws_resume")

    async with websocket(
        f"/api/v1/runs/{run_id}/events/stream", after_seq="0", dev_roles="drilling_supervisor"
    ) as first:
        assert (await first.receive())["type"] == "stream_opened"
        before = await events(client, run_id)
        replayed = [frame["event"]["seq"] for frame in await first.frames(before["total"])]
    last_seq = replayed[-1]

    # The run advances while nobody is listening — this is the case a cursor exists for.
    await decide(client, approval_id)

    async with websocket(
        f"/api/v1/runs/{run_id}/events/stream",
        after_seq=str(last_seq),
        dev_roles="drilling_supervisor",
    ) as second:
        opened = await second.receive()
        assert opened["type"] == "stream_opened"
        assert opened["after_seq"] == last_seq
        frames = await read_until_closed(second)

    resumed = [frame["event"]["seq"] for frame in frames if frame["type"] == "run_event"]
    assert resumed, "the reconnect must deliver what happened while disconnected"
    assert min(resumed) == last_seq + 1, "no gap: the first event after the cursor is last+1"
    assert resumed == sorted(resumed)
    assert frames[-1]["type"] == "stream_closed"

    everything = await events(client, run_id)
    assert replayed + resumed == [row["seq"] for row in everything["items"]]
    assert len(set(replayed + resumed)) == len(replayed + resumed) == everything["total"]


async def test_closing_the_socket_frees_the_server_side_task(client, websocket):
    """A browser that closes the tab must not leave a task polling the log behind.

    The ``websocket`` fixture is what asserts this: its ``__aexit__`` sends the disconnect and then
    waits for the handler to end by itself, so a handler that only writes — and therefore never
    notices the client is gone — fails here instead of leaking a task per abandoned connection.
    """
    run_id, _approval_id = await seed_gate_run(client, key="ws_disconnect")
    async with websocket(
        f"/api/v1/runs/{run_id}/events/stream", after_seq="0", dev_roles="drilling_supervisor"
    ) as socket:
        assert (await socket.receive())["type"] == "stream_opened"
        # Two frames in, with the run idle and the log fully replayed: the handler is now in its
        # poll loop, which is exactly when an abandoned socket used to keep querying forever.
        assert (await socket.receive())["type"] == "run_event"

    # `__aexit__` has already sent the disconnect and waited for the handler to end. What it must not
    # have done is end it by force: a handler that returns has noticed the client left.
    assert socket.handler_state == "returned", socket.handler_state
    leftover = {
        task
        for task in asyncio.all_tasks()
        if not task.done() and "stream_run_events" in str(task.get_coro())
    }
    assert leftover == set(), f"the stream task outlived its client: {leftover}"


async def test_the_socket_refuses_an_unknown_run_and_a_role_without_read(client, websocket):
    run_id, _approval_id = await seed_gate_run(client, key="ws_refusals")

    async with websocket(
        "/api/v1/runs/run_does_not_exist/events/stream", dev_roles="drilling_supervisor"
    ) as socket:
        with pytest.raises(WebSocketRefused) as unknown:
            await socket.receive()
    assert unknown.value.code == 4404, unknown.value

    # `data_manager` holds no `workflow.read`, so the socket is refused rather than silently empty.
    async with websocket(f"/api/v1/runs/{run_id}/events/stream", dev_roles="data_manager") as socket:
        with pytest.raises(WebSocketRefused) as forbidden:
            await socket.receive()
    assert forbidden.value.code == 4403, forbidden.value
    assert forbidden.value.reason == "workflow.read is required"


async def test_the_server_only_sends_documented_frame_types(client, websocket):
    """The client may ignore what it does not know; the server must send only what it documents."""
    run_id, _approval_id = await seed_gate_run(client, key="ws_frames")
    logged = await events(client, run_id)
    allowed = {"stream_opened", "run_event", "stream_idle", "stream_closed", "stream_error"}

    async with websocket(
        f"/api/v1/runs/{run_id}/events/stream", after_seq="0", dev_roles="drilling_supervisor"
    ) as socket:
        frames = [await socket.receive() for _ in range(1 + logged["total"])]

    assert [frame["type"] for frame in frames] == ["stream_opened"] + ["run_event"] * logged["total"]
    assert {frame["type"] for frame in frames} <= allowed
    for frame in frames[1:]:
        assert set(frame) == {"type", "event"}, frame
        assert {"seq", "type", "level", "message", "occurred_at"} <= set(frame["event"])


async def test_a_repeated_sequence_number_is_refused_by_the_database(client, db):
    """The uniqueness of ``(run_id, seq)`` is what makes a cursor meaningful.

    The runtime numbers events from the durable maximum, so a resumed run continues the log; the
    constraint is the second line of defence, and it has to be in the schema rather than in a
    convention — a duplicate would be an event a resuming client never sees.
    """
    run_id, _approval_id = await seed_gate_run(client, key="ws_duplicate")
    logged = await events(client, run_id)
    assert logged["items"], "the run must have a log to duplicate"

    async with db.session() as session:
        run = (
            await session.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))
        ).scalar_one()
        session.add(
            RunEvent(
                org_id=run.org_id,
                run_id=run_id,
                seq=logged["items"][0]["seq"],
                type="duplicate_attempt",
                message="this row must not be accepted",
                payload={},
                level="info",
                occurred_at=dt.datetime.now(tz=UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


async def test_with_authentication_enabled_a_dev_roles_parameter_grants_nothing(monkeypatch):
    """The development identity channel must not survive into a configured deployment.

    The same query string that names a role in development is presented to an app built with
    authentication on, with no bearer token: it has to be refused before anything else happens.
    """
    from drillai.api.app import create_app
    from drillai.core.config import get_settings, reset_settings_cache

    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "true")
    reset_settings_cache()
    secured = create_app(get_settings())
    try:
        socket = WebSocketSession(
            secured,
            "/api/v1/runs/run_whatever/events/stream",
            {"dev_roles": "well_manager", "after_seq": "0"},
        )
        async with socket:
            with pytest.raises(WebSocketRefused) as refused:
                await socket.receive()
        # 4401: refused before the run is even looked up — the parameter carried no authority.
        assert refused.value.code == 4401, refused.value
        assert "dev_roles" not in (refused.value.reason or "")
    finally:
        await secured.state.database.dispose()
        reset_settings_cache()


async def test_an_unknown_sequence_cursor_yields_no_replay(client, websocket):
    """A cursor past the end of the log is not an error: it is a client that is up to date."""
    run_id, _approval_id = await seed_gate_run(client, key="ws_cursor_past_end")
    logged = await events(client, run_id)
    beyond = logged["items"][-1]["seq"] + 50

    async with websocket(
        f"/api/v1/runs/{run_id}/events/stream",
        after_seq=str(beyond),
        dev_roles="drilling_supervisor",
    ) as socket:
        opened = await socket.receive()
        assert opened == {
            "type": "stream_opened",
            "run_id": run_id,
            "status": "waiting_approval",
            "after_seq": beyond,
        }
        await decide(client, _approval_id)
        frames = await read_until_closed(socket)

    streamed = [frame["event"]["seq"] for frame in frames if frame["type"] == "run_event"]
    # The cursor is the client's claim about what it already has, and an honest client only moves it
    # forward: a cursor past the end means "I am up to date", not "send me everything again". The
    # tail the run produced on resume is behind that claim, so it is deliberately not re-sent.
    assert streamed == [], streamed
    assert frames[-1]["type"] == "stream_closed"
